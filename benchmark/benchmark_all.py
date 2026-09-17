#!/usr/bin/env python3
"""
Benchmark unificado de los 3 backends de inferencia del homelab:

  1. OpenVINO GenAI  (Intel Arc iGPU Xe2)  -> puerto 8006
  2. LM Studio Local (Intel Arc Vulkan)    -> puerto 1234
  3. LM Studio RX480 (AMD RX480 Vulkan)    -> puerto 1235

Mide por modelo y backend:
  - TTFT (Time To First Token)
  - tok/s (tokens/segundo durante generacion)
  - tok/s_overall (incluyendo carga)
  - total_time

Los modelos OpenVINO se testean secuencialmente (GPU compartida).
Los modelos LM Studio Intel + AMD se testean simultaneamente (GPUs independientes).

Uso:
  docker compose --profile benchmark run --rm dual-gpu-benchmark -- --max-tokens 200
  docker compose --profile benchmark run --rm dual-gpu-benchmark -- --models llama-3.2-3b-instruct
  docker compose --profile benchmark run --rm dual-gpu-benchmark -- --skip-openvino
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

OPENVINO_URL = os.environ.get("OPENVINO_URL", "http://127.0.0.1:8006")
INTEL_URL    = os.environ.get("INTEL_URL",    "http://127.0.0.1:1234")
AMD_URL      = os.environ.get("AMD_URL",      "http://127.0.0.1:1235")

# API tokens (opcionales). LM Studio Intel puede tener auth Bearer activada.
INTEL_API_TOKEN = os.environ.get("INTEL_API_TOKEN", "")
AMD_API_TOKEN   = os.environ.get("AMD_API_TOKEN", "")

OUTPUT_DIR = os.environ.get("BENCHMARK_OUTPUT_DIR", "/output")


def _auth_headers(url, extra=None):
    """Devuelve headers con Authorization Bearer si la URL tiene token configurado."""
    h = dict(extra) if extra else {}
    if url == INTEL_URL and INTEL_API_TOKEN:
        h["Authorization"] = f"Bearer {INTEL_API_TOKEN}"
    elif url == AMD_URL and AMD_API_TOKEN:
        h["Authorization"] = f"Bearer {AMD_API_TOKEN}"
    return h

DEFAULT_PROMPT = (
    "Explica en detalle el funcionamiento del algoritmo de ordenacion quicksort, "
    "incluyendo su complejidad temporal y espacial, casos de uso, y una "
    "implementacion en Python comentada."
)

# Modelos OpenVINO GenAI (puerto 8006) - nombres con sufijo -int4-ov
OPENVINO_MODELS = [
    "qwen2.5-3b-instruct-int4-ov",
    "qwen2.5-7b-instruct-int4-ov",
    "qwen2.5-coder-7b-instruct-int4-ov",
    "qwen1.5-moe-a2.7b-chat-int4-ov",
    "gpt-oss-20b-int4-ov",
]

# Modelos GGUF comunes a ambos LM Studio (puerto 1234 y 1235)
LMS_MODELS = [
    "llama-3.2-1b-instruct",
    "llama-3.2-3b-instruct",
    "qwen2.5-3b-instruct",
    "qwen2.5-coder-7b-instruct",
    "meta-llama-3.1-8b-instruct",
    "ministral-8b-instruct-2410",
    "qwen1.5-moe-a2.7b-chat",
    "gemma-4-e4b-it",
]

# Mapping para comparar modelos equivalentes entre backends
MODEL_EQUIVALENCE = {
    "qwen2.5-3b-instruct":            "qwen2.5-3b-instruct-int4-ov",
    "qwen2.5-coder-7b-instruct":      "qwen2.5-coder-7b-instruct-int4-ov",
    "qwen1.5-moe-a2.7b-chat":         "qwen1.5-moe-a2.7b-chat-int4-ov",
    # No hay equivalente GGUF directo en LM Studio para estos:
    # qwen2.5-7b-instruct -> solo OpenVINO
    # gpt-oss-20b -> solo OpenVINO
}


# ─── Utilidades HTTP ─────────────────────────────────────────────────────────

def api_get(url, path, timeout=10):
    try:
        req = urllib.request.Request(f"{url}{path}", headers=_auth_headers(url, {"Accept": "application/json"}))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except Exception as e:
        return None, str(e)[:300]


def api_post_stream(url, model, prompt, max_tokens, timeout=600):
    """
    POST /v1/chat/completions con stream=true.
    Devuelve dict con metricas: load_time, ttft, total_time, tok_s, tok_s_overall, etc.
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
            headers=_auth_headers(url, {"Content-Type": "application/json", "Accept": "text/event-stream"}),
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buffer = ""
            for raw_line in resp:
                if first_byte_time is None:
                    first_byte_time = time.time()

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data: "):
                    continue

                data_str = line[6:]
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    u = chunk["usage"]
                    prompt_tokens = u.get("prompt_tokens", 0)
                    completion_tokens = u.get("completion_tokens", 0)

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

            if completion_tokens == 0:
                completion_tokens = tokens_generated

            if first_token_time is None:
                return {
                    "error": "No se generaron tokens",
                    "load_time": (first_byte_time - t0) if first_byte_time else 0,
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


def unload_model_lms(url, model_id, timeout=30):
    """Descarga un modelo via LM Studio API si el endpoint existe."""
    try:
        payload = json.dumps({"model": model_id}).encode()
        req = urllib.request.Request(
            f"{url}/api/v0/models/unload",
            data=payload,
            headers=_auth_headers(url, {"Content-Type": "application/json"}),
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, None
    except Exception:
        return False, "endpoint not available"


def get_loaded_models_lms(url, timeout=10):
    data, err = api_get(url, "/api/v0/models", timeout)
    if err:
        return []
    return [m["id"] for m in data.get("data", []) if m.get("state") == "loaded"]


def cleanup_stale_lms(url, label, current_model, timeout=30):
    loaded = get_loaded_models_lms(url, timeout)
    cleaned = []
    for mid in loaded:
        if current_model in mid or mid in current_model:
            continue
        ok, _ = unload_model_lms(url, mid, timeout)
        if ok:
            cleaned.append(mid)
    if cleaned:
        print(f"  [{label}] Limpiados: {', '.join(cleaned)}")
    return cleaned


# ─── Benchmark: OpenVINO GenAI ───────────────────────────────────────────────

def benchmark_openvino(models, prompt, max_tokens):
    """Benchmark secuencial de modelos OpenVINO (GPU compartida, no paralelizable)."""
    results = []

    # Verificar conectividad
    data, err = api_get(OPENVINO_URL, "/v1/models")
    if err or data is None:
        print(f"\n  ERROR: No se puede conectar a OpenVINO GenAI ({OPENVINO_URL}): {err}")
        return results

    available = [m["id"] for m in data.get("data", [])]
    print(f"  [init] OpenVINO GenAI: conectado ({len(available)} modelos disponibles)")

    for model in models:
        if model not in available:
            print(f"\n  [OpenVINO] SKIP {model} (no disponible)")
            results.append({"model": model, "openvino": {"error": "modelo no disponible"}})
            continue

        print(f"\n  [OpenVINO] {model} -> inferencia ({max_tokens} tokens)...")
        result = api_post_stream(OPENVINO_URL, model, prompt, max_tokens)

        if result.get("error"):
            print(f"  [OpenVINO] FAIL: {result['error'][:120]}")
        else:
            print(f"  [OpenVINO] OK: {result['tokens_generated']} tok | "
                  f"TTFT={result['ttft']:.2f}s | "
                  f"tok/s={result['tok_s']:.1f} | "
                  f"total={result['total_time']:.1f}s")

        results.append({"model": model, "openvino": result})
        time.sleep(2)

    return results


# ─── Benchmark: LM Studio dual-GPU ───────────────────────────────────────────

def benchmark_lms_single(url, model, prompt, max_tokens, label):
    print(f"  [{label}] {model} -> inferencia ({max_tokens} tokens)...")
    result = api_post_stream(url, model, prompt, max_tokens)
    if result.get("error"):
        print(f"  [{label}] FAIL: {result['error'][:120]}")
    else:
        print(f"  [{label}] OK: {result['tokens_generated']} tok | "
              f"TTFT={result['ttft']:.2f}s | "
              f"tok/s={result['tok_s']:.1f} | "
              f"total={result['total_time']:.1f}s")
    return result


def benchmark_lms_dual(models, prompt, max_tokens):
    """Benchmark simultaneo Intel Arc (p1234) + AMD RX480 (p1235)."""
    results = []

    # Verificar conectividad - cada backend de forma independiente
    intel_data, intel_err = api_get(INTEL_URL, "/api/v0/models")
    amd_data, amd_err = api_get(AMD_URL, "/api/v0/models")

    intel_ok = not (intel_err or intel_data is None)
    amd_ok = not (amd_err or amd_data is None)

    if not intel_ok:
        print(f"\n  WARN: LM Studio Intel no disponible ({INTEL_URL}): {intel_err}")
        print(f"  -> Continuando solo con AMD RX480")
    if not amd_ok:
        print(f"\n  WARN: LM Studio AMD no disponible ({AMD_URL}): {amd_err}")

    if not intel_ok and not amd_ok:
        print(f"\n  ERROR: Ningun backend LM Studio disponible. Saltando fase LMS.")
        return results

    if intel_ok:
        print(f"  [init] LM Studio Intel: conectado ({len(intel_data.get('data', []))} modelos)")
    if amd_ok:
        print(f"  [init] LM Studio AMD:   conectado ({len(amd_data.get('data', []))} modelos)")

    # Filtrar modelos disponibles
    intel_available = [m["id"] for m in intel_data.get("data", [])] if intel_ok else []
    amd_available = [m["id"] for m in amd_data.get("data", [])] if amd_ok else []

    for model in models:
        skip_intel = not intel_ok or model not in intel_available
        skip_amd = not amd_ok or model not in amd_available

        if skip_intel and skip_amd:
            print(f"\n  [LMS] SKIP {model} (no disponible en ningun backend)")
            results.append({"model": model, "intel": {"error": "no disponible"}, "amd": {"error": "no disponible"}})
            continue

        print(f"\n{'='*70}")
        print(f"  MODELO: {model}")
        print(f"{'='*70}")

        # Cleanup stale
        if not skip_intel:
            cleanup_stale_lms(INTEL_URL, "Intel", model)
        if not skip_amd:
            cleanup_stale_lms(AMD_URL, "AMD", model)

        # Inferencia: simultanea si ambos disponibles, individual si solo uno
        intel_result = {"error": "skip (backend no disponible)"} if skip_intel else None
        amd_result = {"error": "skip (backend no disponible)"} if skip_amd else None

        if not skip_intel and not skip_amd:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_intel = pool.submit(benchmark_lms_single, INTEL_URL, model, prompt, max_tokens, "Intel")
                fut_amd = pool.submit(benchmark_lms_single, AMD_URL, model, prompt, max_tokens, "AMD  ")
                intel_result = fut_intel.result()
                amd_result = fut_amd.result()
        elif not skip_intel:
            intel_result = benchmark_lms_single(INTEL_URL, model, prompt, max_tokens, "Intel")
        elif not skip_amd:
            amd_result = benchmark_lms_single(AMD_URL, model, prompt, max_tokens, "AMD  ")

        # Mostrar comparativa
        if not intel_result.get("error") and not amd_result.get("error"):
            i_tok = intel_result["tok_s"]
            a_tok = amd_result["tok_s"]
            winner = "Intel" if i_tok > a_tok else "AMD" if a_tok > i_tok else "EMPATE"
            diff = abs(i_tok - a_tok)
            pct = (diff / min(i_tok, a_tok) * 100) if min(i_tok, a_tok) > 0 else 0
            print(f"  Ganador: {winner} (+{pct:.0f}%)")
        else:
            if intel_result.get("error"):
                print(f"  Intel Arc: SKIP/ERROR - {intel_result['error'][:80]}")
            if amd_result.get("error"):
                print(f"  AMD RX480: SKIP/ERROR - {amd_result['error'][:80]}")

        # Descargar modelo de las GPUs activas
        if not skip_intel:
            unload_model_lms(INTEL_URL, model)
        if not skip_amd:
            unload_model_lms(AMD_URL, model)
        time.sleep(3)

        results.append({"model": model, "intel": intel_result, "amd": amd_result})

    return results


# ─── Resumen ─────────────────────────────────────────────────────────────────

def print_summary(ov_results, lms_results):
    print(f"\n\n{'='*90}")
    print(f"  RESUMEN FINAL - BENCHMARK 3 BACKENDS")
    print(f"{'='*90}")

    # Tabla OpenVINO
    if ov_results:
        print(f"\n  OPENVINO GENAI (Intel Arc iGPU Xe2 - puerto 8006)")
        print(f"  {'Modelo':<40} {'Tokens':>7} {'TTFT':>7} {'tok/s':>8} {'Total':>7}")
        print(f"  {'-'*40} {'-'*7} {'-'*7} {'-'*8} {'-'*7}")
        for r in ov_results:
            model = r["model"]
            ov = r.get("openvino", {})
            if ov.get("error"):
                print(f"  {model:<40} {'ERR':>7} {'--':>7} {'--':>8} {'--':>7}")
            else:
                print(f"  {model:<40} {ov['tokens_generated']:>7} {ov['ttft']:>6.2f}s {ov['tok_s']:>7.1f} {ov['total_time']:>6.1f}s")

    # Tabla LM Studio dual
    if lms_results:
        print(f"\n  LM STUDIO DUAL-GPU (Intel Arc Vulkan p1234 vs AMD RX480 Vulkan p1235)")
        print(f"  {'Modelo':<35} {'Intel tok/s':>12} {'AMD tok/s':>12} {'Ganador':>10}")
        print(f"  {'-'*35} {'-'*12} {'-'*12} {'-'*10}")
        for r in lms_results:
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

    # Tabla comparativa cross-backend (modelos equivalentes)
    if ov_results and lms_results:
        print(f"\n  COMPARATIVA CROSS-BACKEND (modelos equivalentes)")
        print(f"  {'Modelo base':<35} {'OpenVINO':>10} {'LMS Intel':>10} {'LMS AMD':>10} {'Mejor':>10}")
        print(f"  {'-'*35} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

        for lms_r in lms_results:
            base = lms_r["model"]
            ov_equiv = MODEL_EQUIVALENCE.get(base)
            if not ov_equiv:
                continue

            ov_tok = None
            for ov_r in ov_results:
                if ov_r["model"] == ov_equiv and not ov_r.get("openvino", {}).get("error"):
                    ov_tok = ov_r["openvino"]["tok_s"]
                    break

            i_tok = lms_r.get("intel", {}).get("tok_s") if not lms_r.get("intel", {}).get("error") else None
            a_tok = lms_r.get("amd", {}).get("tok_s") if not lms_r.get("amd", {}).get("error") else None

            ov_str = f"{ov_tok:.1f}" if ov_tok else "--"
            i_str = f"{i_tok:.1f}" if i_tok else "ERR"
            a_str = f"{a_tok:.1f}" if a_tok else "ERR"

            valid = {k: v for k, v in [("OV", ov_tok), ("Intel", i_tok), ("AMD", a_tok)] if v}
            best = max(valid, key=valid.get) if valid else "N/A"

            print(f"  {base:<35} {ov_str:>10} {i_str:>10} {a_str:>10} {best:>10}")

    print(f"\n{'='*90}\n")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Benchmark 3 backends: OpenVINO + LM Studio Intel + LM Studio AMD")
    parser.add_argument("--models", nargs="*", default=None,
                        help="Modelos GGUF a probar en LM Studio (default: lista completa)")
    parser.add_argument("--openvino-models", nargs="*", default=None,
                        help="Modelos OpenVINO a probar (default: lista completa)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt para la inferencia")
    parser.add_argument("--max-tokens", type=int, default=200, help="Maximo numero de tokens a generar")
    parser.add_argument("--skip-openvino", action="store_true", help="Saltar benchmark OpenVINO")
    parser.add_argument("--skip-lms", action="store_true", help="Saltar benchmark LM Studio dual-GPU")
    args = parser.parse_args()

    lms_models = args.models if args.models else LMS_MODELS
    ov_models = args.openvino_models if args.openvino_models else OPENVINO_MODELS

    print(f"\n{'='*90}")
    print(f"  BENCHMARK UNIFICADO 3 BACKENDS")
    print(f"  OpenVINO GenAI  ({OPENVINO_URL})  - Intel Arc iGPU Xe2")
    print(f"  LM Studio Intel ({INTEL_URL})  - Intel Arc Vulkan")
    print(f"  LM Studio AMD   ({AMD_URL})  - AMD RX480 Vulkan")
    print(f"  Modelos OpenVINO: {len(ov_models)} | Modelos LMS: {len(lms_models)} | Max tokens: {args.max_tokens}")
    print(f"{'='*90}")

    # ─── Fase 1: OpenVINO GenAI ──────────────────────────────────────────────
    ov_results = []
    if not args.skip_openvino:
        print(f"\n{'─'*90}")
        print(f"  FASE 1: OpenVINO GenAI (Intel Arc iGPU)")
        print(f"{'─'*90}")
        ov_results = benchmark_openvino(ov_models, args.prompt, args.max_tokens)

    # ─── Fase 2: LM Studio dual-GPU ──────────────────────────────────────────
    lms_results = []
    if not args.skip_lms:
        print(f"\n{'─'*90}")
        print(f"  FASE 2: LM Studio dual-GPU (Intel Arc vs AMD RX480)")
        print(f"{'─'*90}")
        lms_results = benchmark_lms_dual(lms_models, args.prompt, args.max_tokens)

    # ─── Resumen ─────────────────────────────────────────────────────────────
    print_summary(ov_results, lms_results)

    # ─── Guardar JSON ────────────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_file = os.path.join(OUTPUT_DIR, f"benchmark_all_{int(time.time())}.json")
    with open(output_file, "w") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "max_tokens": args.max_tokens,
            "prompt": args.prompt[:200],
            "backends": {
                "openvino": {"url": OPENVINO_URL, "gpu": "Intel Arc iGPU Xe2 128EU"},
                "lms_intel": {"url": INTEL_URL, "gpu": "Intel Arc Vulkan"},
                "lms_amd": {"url": AMD_URL, "gpu": "AMD RX480 Vulkan"},
            },
            "openvino_results": ov_results,
            "lms_results": lms_results,
        }, f, indent=2, ensure_ascii=False)

    print(f"  Resultados guardados en: {output_file}")


if __name__ == "__main__":
    main()