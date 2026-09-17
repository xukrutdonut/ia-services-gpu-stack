#!/usr/bin/env python3
"""Benchmark del server OpenVINO GenAI optimizado (Intel Arc iGPU)."""
import json
import time
import urllib.request
import urllib.error
import concurrent.futures

URL = "http://localhost:8006"
MODELS = [
    "qwen2.5-3b-instruct-int4-ov",
    "qwen2.5-7b-instruct-int4-ov",
    "qwen2.5-coder-7b-instruct-int4-ov",
    "qwen1.5-moe-a2.7b-chat-int4-ov",
]
PROMPT = "Write a detailed essay about artificial intelligence and its impact on society. Include historical context, current applications, and future implications."
MAX_TOKENS = 200
CONCURRENT = [1, 2, 4]  # Test with 1, 2, 4 concurrent requests

def bench_one(model, prompt, max_tokens):
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{URL}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        elapsed = time.time() - t0
        tokens = data.get("usage", {}).get("completion_tokens", 0)
        return elapsed, tokens, None
    except Exception as e:
        elapsed = time.time() - t0
        return elapsed, 0, str(e)

print(f"Server: {URL}")
print(f"Prompt: {PROMPT[:60]}...")
print(f"Max tokens: {MAX_TOKENS}")
print()

# Single request benchmark for each model
print("=== Single request ===")
for model in MODELS:
    elapsed, tokens, err = bench_one(model, PROMPT, MAX_TOKENS)
    if err:
        print(f"  {model}: ERROR - {err}")
    else:
        tps = tokens / elapsed if elapsed > 0 else 0
        print(f"  {model}: {tokens} tokens in {elapsed:.2f}s = {tps:.1f} tok/s")

print()

# Concurrent benchmark for qwen2.5-7b
print("=== Concurrent requests (qwen2.5-7b-instruct-int4-ov) ===")
for n in CONCURRENT:
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(bench_one, "qwen2.5-7b-instruct-int4-ov", PROMPT, MAX_TOKENS) for _ in range(n)]
        results = [f.result() for f in futs]
    total_time = time.time() - t0
    total_tokens = sum(r[1] for r in results)
    errors = [r[2] for r in results if r[2]]
    if errors:
        print(f"  {n} concurrent: ERROR(s) - {errors[0]}")
    else:
        avg_tps = total_tokens / total_time
        individual = [f"{r[1]/r[0]:.1f}" for r in results]
        print(f"  {n} concurrent: {total_tokens} tokens in {total_time:.2f}s = {avg_tps:.1f} tok/s aggregate (individual: {', '.join(individual)} tok/s)")

print()
print("=== RAM usage ===")
import subprocess
result = subprocess.run(["docker", "stats", "intel-gemma4-optimum", "--no-stream", "--format", "{{.MemUsage}} {{.CPUPerc}}"], capture_output=True, text=True)
print(f"  Container: {result.stdout.strip()}")