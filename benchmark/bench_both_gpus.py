#!/usr/bin/env python3
"""Benchmark unificado de las dos GPUs discretas del homelab (Khazad-dum):

  * Intel Arc (Arrow Lake-P iGPU, Xe2)  -> OpenVINO Model Server (OVMS) HTTP :8006
        modelos int4 OpenVINO-IR en GPU, thinking se desactiva con
        chat_template_kwargs.enable_thinking=false
  * AMD RX480 (Polaris, Vulkan)         -> LM Studio / llmster HTTP :1235
        modelos GGUF Q4_K_M, JIT-load, thinking se desactiva con "/no_think"
        en el system message (LM Studio ignora chat_template_kwargs)

Mide, por modelo y GPU:
  - carga (warmup, incluye JIT + compilado de grafo)
  - TTFT (time-to-first-token)
  - tok/s de decodificacion (por usage y por mediana de gaps inter-token)
  - tok/s total end-to-end
  - escalado de concurrencia (tok/s agregados a n=1,2,4,8 peticiones)

Y para modelos de embeddings: latencia y throughput (single y batch).

Salida: JSON + Markdown en benchmark/results/.

Uso:
    python3 benchmark/bench_both_gpus.py
    python3 benchmark/bench_both_gpus.py --reps 3 --max-tokens 256
    python3 benchmark/bench_both_gpus.py --only arc --no-concurrency
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ARC_URL = os.environ.get("ARC_URL", "http://127.0.0.1:8006")
RX_URL = os.environ.get("RX_URL", "http://127.0.0.1:1235")

# --- conjuntos de modelos vivos (verificado contra /v1/models y config.json) ---
ARC_CHAT = [
    "qwen2.5-7b-instruct-int4-ov",
    "Qwen3-4B-int4-ov",
    "LFM2-8B-A1B-int4-ov",
    "Qwen3-30B-A3B-Instruct-2507-int4-ov",
]
ARC_EMB = ["all-MiniLM-L6-v2-ov", "nomic-embed-text-v1.5-ov"]

RX_CHAT = ["qwen3-0.6b-instruct", "qwen3-1.7b-instruct", "olmoe-1b-7b-instruct"]
RX_EMB = ["text-embedding-nomic-embed-text-v1.5"]

SYSTEM = (
    "Eres un asistente tecnico. Responde siempre en prosa continua y detallada, "
    "sin listas ni encabezados."
)
PROMPT = (
    "Explica con detalle como funciona una GPU moderna: su arquitectura de "
    "shaders, la jerarquia de memoria (VRAM, cache, registros), el pipeline de "
    "rasterizacion y el modelo de ejecucion SIMT. Desarrolla al menos 250 palabras."
)
EMB_TEXT = (
    "La inferencia local de modelos de lenguaje en GPUs discretas depende del "
    "ancho de banda de memoria, de la cuantizacion y del backend de computo."
)

CONCURRENCY_LEVELS = [1, 2, 4, 8]


# ───────────────────────────── HTTP helpers ──────────────────────────────────

def _post(url: str, body: dict, timeout: float = 600.0):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def stream_chat(base: str, model: str, body_extra: dict, max_tokens: int,
                timeout: float = 600.0):
    """Genera en streaming y devuelve metricas de latencia/velocidad."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": body_extra.pop("_system", SYSTEM)},
            {"role": "user", "content": PROMPT},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "top_p": 1.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    body.update(body_extra)

    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base + "/v1/chat/completions", data=data,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    first = None
    last = None
    times: list[float] = []
    usage = None
    chars = 0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices", []):
                delta = ch.get("delta") or {}
                content = delta.get("content") or ""
                reasoning = delta.get("reasoning_content") or ""
                if content or reasoning:
                    now = time.perf_counter()
                    if first is None:
                        first = now
                    last = now
                    times.append(now)
                    chars += len(content or reasoning)
    t_end = time.perf_counter()

    n_tok = (usage or {}).get("completion_tokens") or len(times)
    prompt_tok = (usage or {}).get("prompt_tokens")
    decode_usage = None
    if first is not None and last is not None and last > first and n_tok > 1:
        decode_usage = (n_tok - 1) / (last - first)
    gaps = [b - a for a, b in zip(times, times[1:])]
    decode_gap = (1.0 / statistics.median(gaps)) if gaps else None
    return {
        "prompt_tokens": prompt_tok,
        "completion_tokens": n_tok,
        "chars": chars,
        "ttft_s": (first - t0) if first else None,
        "total_s": t_end - t0,
        "decode_tps": decode_usage,
        "decode_tps_gap": decode_gap,
        "overall_tps": (n_tok / (t_end - t0)) if t_end > t0 else None,
        "n_chunks": len(times),
    }


def no_stream_chat(base: str, model: str, body_extra: dict, max_tokens: int,
                   timeout: float = 600.0):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": body_extra.pop("_system", SYSTEM)},
            {"role": "user", "content": PROMPT},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "top_p": 1.0,
        "stream": False,
    }
    body.update(body_extra)
    t0 = time.perf_counter()
    obj = _post(base + "/v1/chat/completions", body, timeout=timeout)
    dt = time.perf_counter() - t0
    usage = obj.get("usage") or {}
    return {
        "completion_tokens": usage.get("completion_tokens", 0),
        "wall_s": dt,
    }


def embeddings(base: str, model: str, inputs: list[str], timeout: float = 300.0):
    t0 = time.perf_counter()
    obj = _post(base + "/v1/embeddings",
                {"model": model, "input": inputs}, timeout=timeout)
    dt = time.perf_counter() - t0
    n = len(obj.get("data", []))
    return {
        "n": n,
        "latency_s": dt,
        "texts_per_s": (n / dt) if dt > 0 else None,
    }


# ────────────────────────── variantes thinking ───────────────────────────────

def arc_extra(model: str) -> dict:
    if model.lower().startswith("qwen3"):
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}


def rx_extra(model: str) -> dict:
    if model.lower().startswith("qwen3"):
        return {"_system": "/no_think"}
    return {}


# ────────────────────────────── hosts / GPU ──────────────────────────────────

def shell(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"<error: {exc}>"


def gpu_state() -> dict:
    st = {
        "hostname": shell("hostname"),
        "date": _dt.datetime.now().isoformat(timespec="seconds"),
        "rx480_vram_used_mib": None,
        "rx480_sclk": None,
        "rx480_temp_c": None,
    }
    for line in shell("cat /sys/class/drm/card1/device/mem_info_vram_used").splitlines():
        try:
            st["rx480_vram_used_mib"] = int(line) // (1024 * 1024)
        except ValueError:
            pass
    sclk = shell("cat /sys/class/drm/card1/device/pp_dpm_sclk")
    st["rx480_sclk"] = " | ".join(sclk.splitlines())
    return st


def kernel_events() -> list[str]:
    out = shell(
        "journalctl -k -b --no-pager 2>/dev/null | "
        "grep -E 'ring .* timeout|PRT request|GPU reset|gfxhub.*[Pp]age fault|failed ret is 65535' "
        "| tail -20"
    )
    return [ln for ln in out.splitlines() if ln.strip()]


# ────────────────────────────────── bench ────────────────────────────────────

def bench_chat(base: str, model: str, extra_fn, reps: int, max_tokens: int) -> dict:
    res = {"model": model, "status": "ok", "warmup_s": None, "runs": []}
    try:
        t0 = time.perf_counter()
        stream_chat(base, model, extra_fn(model), 8, timeout=900.0)
        res["warmup_s"] = time.perf_counter() - t0
    except Exception as exc:  # noqa: BLE001
        res["status"] = "warmup_fail"
        res["error"] = str(exc)
        return res

    for _ in range(reps):
        try:
            res["runs"].append(
                stream_chat(base, model, extra_fn(model), max_tokens, timeout=900.0)
            )
        except Exception as exc:  # noqa: BLE001
            res["status"] = "fail"
            res["error"] = str(exc)
            break

    if res["runs"]:
        for key in ("ttft_s", "decode_tps", "decode_tps_gap", "overall_tps",
                    "completion_tokens", "prompt_tokens"):
            vals = [r[key] for r in res["runs"] if r.get(key) is not None]
            res[key] = round(statistics.median(vals), 3) if vals else None
    return res


def bench_concurrency(base: str, model: str, extra_fn) -> dict:
    out = {"model": model, "levels": []}
    for n in CONCURRENCY_LEVELS:
        try:
            with ThreadPoolExecutor(max_workers=n) as ex:
                t0 = time.perf_counter()
                futs = [
                    ex.submit(no_stream_chat, base, model, extra_fn(model), 128)
                    for _ in range(n)
                ]
                rows = [f.result() for f in futs]
                wall = time.perf_counter() - t0
            total_tok = sum(r["completion_tokens"] for r in rows)
            out["levels"].append({
                "n": n,
                "wall_s": round(wall, 3),
                "total_completion_tokens": total_tok,
                "aggregate_tps": round(total_tok / wall, 2) if wall > 0 else None,
            })
        except Exception as exc:  # noqa: BLE001
            out["levels"].append({"n": n, "error": str(exc)})
            break
    if out["levels"] and out["levels"][0].get("aggregate_tps"):
        base_tps = out["levels"][0]["aggregate_tps"]
        for lv in out["levels"]:
            if lv.get("aggregate_tps"):
                lv["speedup"] = round(lv["aggregate_tps"] / base_tps, 2)
    return out


def bench_emb(base: str, model: str, reps: int) -> dict:
    res = {"model": model, "status": "ok"}
    try:
        single = []
        for _ in range(reps):
            single.append(embeddings(base, model, [EMB_TEXT]))
        batch = embeddings(base, model, [EMB_TEXT] * 32)
        res["single_median_s"] = round(
            statistics.median(x["latency_s"] for x in single), 4)
        res["batch32_s"] = round(batch["latency_s"], 4)
        res["batch32_texts_per_s"] = round(batch["texts_per_s"], 2)
        dim = None
        try:
            dim = len(_post(base + "/v1/embeddings",
                            {"model": model, "input": ["x"]})["data"][0]["embedding"])
        except Exception:  # noqa: BLE001
            pass
        res["dim"] = dim
    except Exception as exc:  # noqa: BLE001
        res["status"] = "fail"
        res["error"] = str(exc)
    return res


# ────────────────────────────────── render ───────────────────────────────────

def md_table(rows, cols):
    out = ["| " + " | ".join(cols) + " |",
           "| " + " | ".join(":---" if i == 0 else "---:" for i in range(len(cols))) + " |"]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(out)


def render(data: dict) -> str:
    L = []
    L.append(f"# Benchmark dual-GPU (Intel Arc + AMD RX480) — {data['gpu']['date']}")
    L.append("")
    L.append(f"Host: `{data['gpu']['hostname']}` · "
             f"Protocolo: `max_tokens={data['params']['max_tokens']}`, "
             f"`temperature=0`, `reps={data['params']['reps']}` "
             f"(warmup + mediana), prompt fijo de prosa.")
    L.append("")

    L.append("## 1. Generacion de texto (chat)")
    L.append("")
    for gpu, key in [("Intel Arc — OVMS :8006 (int4 OpenVINO-IR, GPU)", "arc"),
                     ("AMD RX480 — LM Studio :1235 (GGUF Q4_K_M, Vulkan)", "rx")]:
        L.append(f"### {gpu}")
        L.append("")
        rows = []
        for m in data["chat"][key]:
            if m.get("status") not in ("ok",) and "ttft_s" not in m:
                rows.append([m["model"], m.get("error", m["status"]), "-", "-", "-", "-", "-"])
                continue
            rows.append([
                m["model"],
                m.get("prompt_tokens"),
                f'{m["ttft_s"]:.3f}' if m.get("ttft_s") is not None else None,
                f'{m["decode_tps"]:.1f}' if m.get("decode_tps") is not None else None,
                f'{m["decode_tps_gap"]:.1f}' if m.get("decode_tps_gap") is not None else None,
                f'{m["overall_tps"]:.1f}' if m.get("overall_tps") is not None else None,
                f'{m["warmup_s"]:.1f}' if m.get("warmup_s") is not None else None,
            ])
        L.append(md_table(rows, ["Modelo", "prompt tok", "TTFT s", "dec tok/s",
                                 "dec tok/s (gap)", "global tok/s", "warmup s"]))
        L.append("")

    L.append("## 2. Escalado de concurrencia (tok/s agregados)")
    L.append("")
    for gpu, key in [("Intel Arc — OVMS", "arc"), ("AMD RX480 — LM Studio", "rx")]:
        L.append(f"**{gpu}**")
        L.append("")
        rows = []
        for c in data["concurrency"][key]:
            lv = {x.get("n"): x for x in c["levels"]}
            def cell(val):
                if val is None or "error" in val:
                    return "err" if val and "error" in val else "-"
                return f'{val["aggregate_tps"]:.1f}'
            rows.append([c["model"]] + [cell(lv.get(n)) for n in CONCURRENCY_LEVELS] +
                        [f'{lv.get(CONCURRENCY_LEVELS[-1], {}).get("speedup", 0):.2f}x'
                         if lv.get(CONCURRENCY_LEVELS[-1], {}).get("speedup") else "-"])
        L.append(md_table(rows, ["Modelo"] + [f"n={n}" for n in CONCURRENCY_LEVELS] + ["speedup n=8"]))
        L.append("")

    L.append("## 3. Embeddings")
    L.append("")
    for gpu, key in [("Intel Arc — OVMS", "arc"), ("AMD RX480 — LM Studio", "rx")]:
        L.append(f"**{gpu}**")
        L.append("")
        rows = []
        for m in data["emb"][key]:
            if m.get("status") != "ok":
                rows.append([m["model"], m.get("error", "fail"), "-", "-", "-"])
                continue
            rows.append([m["model"], m.get("dim"),
                         f'{m["single_median_s"]*1000:.1f} ms',
                         f'{m["batch32_s"]*1000:.1f} ms',
                         f'{m["batch32_texts_per_s"]:.1f} text/s'])
        L.append(md_table(rows, ["Modelo", "dim", "single (mediana)", "batch x32", "batch text/s"]))
        L.append("")

    L.append("## 4. Estado de la GPU RX480 al terminar")
    L.append("")
    L.append(f"- VRAM usada: {data['post_gpu']['rx480_vram_used_mib']} MiB")
    L.append(f"- sclk: `{data['post_gpu']['rx480_sclk']}`")
    L.append(f"- Eventos de kernel nuevos (ring timeout / PRT / reset): "
             f"{len(data['kernel_new'])}")
    for ln in data["kernel_new"]:
        L.append(f"    - `{ln}`")
    L.append("")
    L.append("> Nota: Arc (iGPU) y RX480 usan esquemas de cuantizacion distintos "
             "(int4 OpenVINO-IR vs Q4_K_M GGUF) y backends distintos "
             "(OpenVINO/Level-Zero vs Vulkan). Comparar dentro de cada GPU; "
             "entre GPUs la comparacion es orientativa.")
    return "\n".join(L)


# ─────────────────────────────────── main ────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--only", choices=["arc", "rx", "both"], default="both")
    ap.add_argument("--no-concurrency", action="store_true")
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "results"))
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    data = {"params": {"reps": args.reps, "max_tokens": args.max_tokens,
                       "concurrency_levels": CONCURRENCY_LEVELS if not args.no_concurrency else []},
            "gpu": gpu_state(), "kernel_before": kernel_events(),
            "chat": {"arc": [], "rx": []}, "concurrency": {"arc": [], "rx": []},
            "emb": {"arc": [], "rx": []}}

    def log(msg):
        print(msg, flush=True)

    if args.only in ("arc", "both"):
        log("== Intel Arc (OVMS :8006) — chat ==")
        for m in ARC_CHAT:
            log(f"  {m} ...")
            data["chat"]["arc"].append(bench_chat(ARC_URL, m, arc_extra, args.reps, args.max_tokens))
        if not args.no_concurrency:
            for m in ARC_CHAT:
                log(f"  concurrencia {m} ...")
                data["concurrency"]["arc"].append(bench_concurrency(ARC_URL, m, arc_extra))
        log("== Intel Arc — embeddings ==")
        for m in ARC_EMB:
            log(f"  {m} ...")
            data["emb"]["arc"].append(bench_emb(ARC_URL, m, args.reps))

    if args.only in ("rx", "both"):
        log("== AMD RX480 (LM Studio :1235) — chat ==")
        for m in RX_CHAT:
            log(f"  {m} ...")
            data["chat"]["rx"].append(bench_chat(RX_URL, m, rx_extra, args.reps, args.max_tokens))
        if not args.no_concurrency:
            for m in RX_CHAT:
                log(f"  concurrencia {m} ...")
                data["concurrency"]["rx"].append(bench_concurrency(RX_URL, m, rx_extra))
        log("== AMD RX480 — embeddings ==")
        for m in RX_EMB:
            log(f"  {m} ...")
            data["emb"]["rx"].append(bench_emb(RX_URL, m, args.reps))

    data["post_gpu"] = gpu_state()
    data["kernel_new"] = [ln for ln in kernel_events() if ln not in data["kernel_before"]]

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    jpath = os.path.join(args.out_dir, f"bench-both-{stamp}.json")
    mpath = os.path.join(args.out_dir, f"bench-both-{stamp}.md")
    with open(jpath, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    with open(mpath, "w") as f:
        f.write(render(data))
    log(f"\nJSON : {jpath}\nMD   : {mpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
