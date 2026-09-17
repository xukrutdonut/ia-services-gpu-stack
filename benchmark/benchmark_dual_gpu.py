#!/usr/bin/env python3
"""
Benchmark simultaneo dual-GPU (Intel Arc vs AMD RX480) via LM Studio HTTP API.

Disenado para ejecutarse dentro de un container Docker (ia-services-stack).
- Intel Arc: LM Studio host, puerto 1234
- AMD RX480: contenedor ia-gpu-amd-rx480, puerto 1235
- Usa /v1/chat/completions con stream=true para medir TTFT y tok/s reales
- No necesita lms CLI: la API auto-carga el modelo al recibir la peticion

Uso:
  docker compose --profile benchmark run --rm dual-gpu-benchmark [-- --max-tokens 300]
  docker run --rm --network host ia-dual-gpu-benchmark:latest --max-tokens 200
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

# ─── Configuracion via env vars ──────────────────────────────────────────────

INTEL_URL = os.environ.get("INTEL_URL", "http://127.0.0.1:1234")
AMD_URL   = os.environ.get("AMD_URL",   "http://127.0.0.1:1235")

OUTPUT_DIR = os.environ.get("BENCHMARK_OUTPUT_DIR", "/output")

DEFAULT_PROMPT = "Explica en detalle el funcionamiento del algoritmo de ordenacion quicksort, incluyendo su complejidad temporal y espacial, casos de uso, y una implementacion en Python comentada."

# Modelos comunes a ambas GPUs (deben estar descargados en ambos LM Studio)
DEFAULT_MODELS = [
    "llama-3.2-1b-instruct",
    "llama-3.2-3b-instruct",
    "qwen2.5-3b-instruct",
    "qwen2.5-coder-7b-instruct",
    "meta-llama-3.1-8b-instruct",
    "ministral-8b-instruct-2410",
    "qwen1.5-moe-a2.7b-chat",
]


# ─── Utilidades HTTP ─────────────────────────────────────────────────────────

def api_get(url, path, timeout=10):
    """GET request a la API de LM Studio. Devuelve (data, error)."""
    try:
        req = urllib.request.Request(
            f"{url}{path}",
            headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except Exception as e:
        return None, str(e)[:300]


def api_post_stream(url, model, prompt, max_tokens, timeout=300):
    """
    POST /v1/chat/completions con stream=true.
    Mide:
      - load_time: tiempo hasta el primer byte (incluye carga automatica del modelo)
      - ttft: Time To First Token (primer chunk con content)
      - total_time: tiempo total de generacion
      - tokens_generated: numero de tokens generados
      - tok_s: tokens por segundo (total_time - ttft) / tokens
      - tok_s_overall: tokens por segundo (total_time / tokens)
    Devuelve dict con metricas o error.
    """
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
    }).encode()

    t0 = time.time()
    first_byte_time = None
    first_token_time = None
    tokens_generated = 0
    full_text = ""
    prompt_tokens = 0
    completion_tokens = 0

    try:
        req = urllib.request.Request(
            f"{url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Leer stream SSE
            buffer = ""
            for raw_line in resp:
                if first_byte_time is None:
                    first_byte_time = time.time()

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue

                data_str = line[6:]
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Capturar usage si viene en el chunk final
                if chunk.get("usage"):
                    u = chunk["usage"]
                    prompt_tokens = u.get("prompt_tokens", 0)
                    completion_tokens = u.get("completion_tokens", 0)

                # Extraer content del delta
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        if first_token_time is None:
                            first_token_time = time.time()
                        tokens_generated += 1
                        full_text += content

            total_time = time.time() - t0

            # Si no se capturo completion_tokens via usage, usar el contador
            if completion_tokens == 0:
                completion_tokens = tokens_generated

            if first_token_time is None:
                return {
                    "error": "No se generaron tokens",
                    "load_time": first_byte_time - t0 if first_byte_time else 0,
                    "total_time": total_time,
                }

            ttft = first_token_time - t0
            generation_time = total_time - ttft
            tok_s = completion_tokens / generation_time if generation_time > 0 else 0
            tok_s_overall = completion_tokens / total_time if total_time > 0 else 0

            return {
                "load_time": (first_byte_time - t0) if first_byte_time else 0,
                "ttft": ttft,
                "total_time": total_time,
                "generation_time": generation_time,
                "tokens_generated": completion_tokens,
                "prompt_tokens": prompt_tokens,
                "tok_s": tok_s,
                "tok_s_overall": tok_s_overall,
                "response_preview": full_text[:200],
                "error": None,
            }

    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {err_body}", "total_time": time.time() - t0}
    except Exception as e:
        return {"error": str(e)[:300], "total_time": time.time() - t0}


def unload_model(url, model_id, timeout=30):
    """Descarga un modelo via API si el endpoint existe. Devuelve (ok, error)."""
    try:
        payload = json.dumps({"model": model_id}).encode()
        req = urllib.request.Request(
            f"{url}/api/v0/models/unload",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, None
    except Exception:
        # Si no existe el endpoint, no es critico: LM Studio descarga por TTL
        return False, "endpoint not available"


def get_loaded_models(url, timeout=10):
    """Devuelve lista de IDs de modelos cargados."""
    data, err = api_get(url, "/api/v0/models", timeout)
    if err:
        return []
    return [m["id"] for m in data.get("data", []) if m.get("state") == "loaded"]


def cleanup_stale(url, label, current_model, timeout=30):
    """Descarga cualquier modelo cargado que no sea el actual."""
    loaded = get_loaded_models(url, timeout)
    cleaned = []
    for mid in loaded:
        # No descargar el modelo que vamos a probar
        if current_model in mid or mid in current_model:
            continue
        ok, _ = unload_model(url, mid, timeout)
        if ok:
            cleaned.append(mid)
    if cleaned:
        print(f"  [{label}] Limpiados: {', '.join(cleaned)}")
    return cleaned


# ─── Benchmark principal ─────────────────────────────────────────────────────

def benchmark_single(url, model, prompt, max_tokens, label):
    """Ejecuta una inferencia streaming y devuelve metricas."""
    print(f"  [{label}] Inferencia streaming ({max_tokens} tokens max)...")

    result = api_post_stream(url, model, prompt, max_tokens)

    if result.get("error"):
        print(f"  [{label}] FAIL: {result['error'][:120]}")
        return result

    ttft = result["ttft"]
    tok_s = result["tok_s"]
    tokens = result["tokens_generated"]
    total = result["total_time"]

    print(f"  [{label}] OK: {tokens} tokens en {total:.1f}s | "
          f"TTFT={ttft:.2f}s | tok/s={tok_s:.1f}")

    return result


def benchmark_model(model, prompt, max_tokens):
    """Ejecuta benchmark simultaneo para un modelo en ambas GPUs."""
    print(f"\n{'='*70}")
    print(f"  MODELO: {model}")
    print(f"{'='*70}")

    # Limpiar modelos stale en ambas GPUs
    print(f"  [cleanup] Limpiando modelos stale en ambas GPUs...")
    cleanup_stale(INTEL_URL, "Intel", model)
    cleanup_stale(AMD_URL, "AMD", model)

    # Inferencia simultanea
    print(f"  [bench] Lanzando inferencia simultanea Intel Arc + AMD RX480...")
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_intel = pool.submit(benchmark_single, INTEL_URL, model, prompt, max_tokens, "Intel")
        fut_amd   = pool.submit(benchmark_single, AMD_URL,   model, prompt, max_tokens, "AMD  ")

        intel_result = fut_intel.result()
        amd_result   = fut_amd.result()

    # Tabla comparativa
    print(f"\n  ── RESULTADO: {model} ──")
    if intel_result.get("error") or amd_result.get("error"):
        if intel_result.get("error"):
            print(f"  Intel Arc:  ERROR - {intel_result['error'][:80]}")
        else:
            print(f"  Intel Arc:  {intel_result['tokens_generated']:4d} tok | "
                  f"TTFT={intel_result['ttft']:.2f}s | "
                  f"tok/s={intel_result['tok_s']:.1f} | "
                  f"total={intel_result['total_time']:.1f}s")
        if amd_result.get("error"):
            print(f"  AMD RX480:  ERROR - {amd_result['error'][:80]}")
        else:
            print(f"  AMD RX480:  {amd_result['tokens_generated']:4d} tok | "
                  f"TTFT={amd_result['ttft']:.2f}s | "
                  f"tok/s={amd_result['tok_s']:.1f} | "
                  f"total={amd_result['total_time']:.1f}s")
    else:
        i_tok = intel_result["tok_s"]
        a_tok = amd_result["tok_s"]
        winner = "Intel" if i_tok > a_tok else "AMD" if a_tok > i_tok else "EMPATE"
        diff = abs(i_tok - a_tok)
        pct = (diff / min(i_tok, a_tok) * 100) if min(i_tok, a_tok) > 0 else 0

        print(f"  Intel Arc:  {intel_result['tokens_generated']:4d} tok | "
              f"TTFT={intel_result['ttft']:.2f}s | "
              f"tok/s={i_tok:.1f} | "
              f"total={intel_result['total_time']:.1f}s")
        print(f"  AMD RX480:  {amd_result['tokens_generated']:4d} tok | "
              f"TTFT={amd_result['ttft']:.2f}s | "
              f"tok/s={a_tok:.1f} | "
              f"total={amd_result['total_time']:.1f}s")
        print(f"  Ganador: {winner} (+{pct:.0f}%)")

    # Descargar modelo de ambas GPUs para liberar VRAM
    print(f"  [cleanup] Descargando {model} de ambas GPUs...")
    unload_model(INTEL_URL, model)
    unload_model(AMD_URL, model)
    time.sleep(3)

    return {
        "model": model,
        "intel": intel_result,
        "amd": amd_result,
    }


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Benchmark dual-GPU Intel Arc vs AMD RX480")
    parser.add_argument("--models", nargs="*", default=DEFAULT_MODELS,
                        help="Lista de modelos a probar")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT,
                        help="Prompt para la inferencia")
    parser.add_argument("--max-tokens", type=int, default=200,
                        help="Maximo numero de tokens a generar")
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  BENCHMARK SIMULTANEO DUAL-GPU")
    print(f"  Intel Arc (puerto {INTEL_URL}) vs AMD RX480 (puerto {AMD_URL})")
    print(f"  Modelos: {len(args.models)} | Max tokens: {args.max_tokens}")
    print(f"{'='*70}")

    # Verificar conectividad
    intel_data, intel_err = api_get(INTEL_URL, "/api/v0/models")
    amd_data, amd_err = api_get(AMD_URL, "/api/v0/models")

    if intel_err or intel_data is None:
        print(f"\n  ERROR: No se puede conectar a Intel Arc en {INTEL_URL}: {intel_err}")
        sys.exit(1)
    if amd_err or amd_data is None:
        print(f"\n  ERROR: No se puede conectar a AMD RX480 en {AMD_URL}: {amd_err}")
        sys.exit(1)

    print(f"  [init] Intel Arc: conectado ({len(intel_data.get('data', []))} modelos)")
    print(f"  [init] AMD RX480: conectado ({len(amd_data.get('data', []))} modelos)")

    # Limpiar modelos residuales
    print(f"\n  [init] Limpiando modelos residuales de benchmarks anteriores...")
    cleanup_stale(INTEL_URL, "Intel", "")
    cleanup_stale(AMD_URL, "AMD", "")

    # Ejecutar benchmark para cada modelo
    results = []
    for model in args.models:
        result = benchmark_model(model, args.prompt, args.max_tokens)
        results.append(result)

    # Resumen final
    print(f"\n\n{'='*70}")
    print(f"  RESUMEN FINAL")
    print(f"{'='*70}")
    print(f"  {'Modelo':<35} {'Intel tok/s':>12} {'AMD tok/s':>12} {'Ganador':>10}")
    print(f"  {'-'*35} {'-'*12} {'-'*12} {'-'*10}")

    for r in results:
        model = r["model"]
        i = r.get("intel", {})
        a = r.get("amd", {})

        i_tok = f"{i['tok_s']:.1f}" if not i.get("error") else "ERR"
        a_tok = f"{a['tok_s']:.1f}" if not a.get("error") else "ERR"

        if i.get("error") or a.get("error"):
            winner = "N/A"
        else:
            winner = "Intel" if i["tok_s"] > a["tok_s"] else "AMD" if a["tok_s"] > i["tok_s"] else "="

        print(f"  {model:<35} {i_tok:>12} {a_tok:>12} {winner:>10}")

    # Guardar resultados JSON
    output_file = os.path.join(OUTPUT_DIR, f"benchmark_dual_{int(time.time())}.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(output_file, "w") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "max_tokens": args.max_tokens,
            "prompt": args.prompt[:200],
            "results": results,
        }, f, indent=2, ensure_ascii=False)

    print(f"\n  Resultados guardados en: {output_file}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()