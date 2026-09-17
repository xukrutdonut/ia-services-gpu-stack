import subprocess
import os
import re
import time

CONTAINER = "ia-gpu-amd-rx480"

models = [
    ("Llama 3.2 1B Instruct", "llama-3.2-1b-instruct"),
    ("Qwen 2.5 3B Instruct", "qwen2.5-3b-instruct"),
    ("Llama 3.2 3B Instruct", "llama-3.2-3b-instruct"),
    ("Gemma 4 E4B IT", "gemma-4-e4b-it"),
    ("Qwen 2.5 Coder 7B", "qwen2.5-coder-7b-instruct"),
    ("Ministral 8B Instruct", "ministral-8b-instruct-2410"),
    ("Meta Llama 3.1 8B", "meta-llama-3.1-8b-instruct"),
    ("Qwen 1.5 MoE A2.7B Chat", "qwen1.5-moe-a2.7b-chat"),
]

prompt = "Explain in 3 paragraphs the theory of general relativity and its implications for modern astrophysics."

print(f"=== INICIANDO BENCHMARK EN CONTENEDOR {CONTAINER} (AMD RX 480 Vulkan) ===", flush=True)

results = []

for name, key in models:
    print(f"\n[+] Evaluando modelo: {name} ({key})...", flush=True)
    
    # 1. Unload preventivo del modelo
    subprocess.run(
        ["docker", "exec", CONTAINER, "/root/.lmstudio/bin/lms", "unload", key],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(2)
    
    # 2. Load with --gpu max
    print(f"  -> Cargando en GPU...", flush=True)
    t0_load = time.time()
    load_res = subprocess.run(
        ["docker", "exec", CONTAINER, "/root/.lmstudio/bin/lms", "load", key, "--gpu", "max", "-y"],
        capture_output=True, text=True
    )
    load_time = time.time() - t0_load
    
    if load_res.returncode != 0:
        print(f"  ❌ Error cargando {key}: {load_res.stderr.strip() or load_res.stdout.strip()}", flush=True)
        continue
    print(f"  ✓ Modelo cargado en {load_time:.2f}s.", flush=True)
    time.sleep(2)
    
    # 3. Run chat with stats
    print(f"  -> Ejecutando inferencia de prueba...", flush=True)
    chat_cmd = f"docker exec {CONTAINER} /root/.lmstudio/bin/lms chat {key} --prompt \"{prompt}\" --stats"
    proc = subprocess.run(chat_cmd, shell=True, capture_output=True, text=True)
    
    combined_output = proc.stdout + "\n" + proc.stderr
    
    tok_sec = None
    pred_tokens = None
    ttft = None
    
    match_tok = re.search(r"Tokens/Second:\s*([0-9.]+)", combined_output)
    match_pred = re.search(r"Predicted Tokens:\s*([0-9.]+)", combined_output)
    match_ttft = re.search(r"Time to First Token:\s*([0-9.]+)s", combined_output)
    
    if match_tok:
        tok_sec = float(match_tok.group(1))
    if match_pred:
        pred_tokens = int(float(match_pred.group(1)))
    if match_ttft:
        ttft = float(match_ttft.group(1))
            
    if tok_sec:
        ttft_str = f"{ttft:.3f}s" if ttft else "N/A"
        print(f"  🚀 Rendimiento: {tok_sec:.2f} tok/s | Tokens: {pred_tokens} | TTFT: {ttft_str}", flush=True)
        results.append((name, key, tok_sec, pred_tokens, ttft_str, f"{load_time:.1f}s"))
    else:
        print(f"  ⚠️ No se pudo extraer stats. Salida:\n{combined_output[-400:]}", flush=True)
        
    # 4. Unload para liberar VRAM antes del siguiente
    print(f"  -> Descargando modelo de VRAM...", flush=True)
    subprocess.run(
        ["docker", "exec", CONTAINER, "/root/.lmstudio/bin/lms", "unload", key],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(3)

print("\n\n================ RESULTADOS FINALES DEL BENCHMARK AMD RX 480 ================", flush=True)
print("| Modelo | Clave LMS | Rendimiento (tok/s) | Tokens Generados | TTFT | Tiempo Carga |", flush=True)
print("| :--- | :--- | :--- | :--- | :--- | :--- |", flush=True)
for res in results:
    print(f"| **{res[0]}** | `{res[1]}` | **{res[2]:.2f} tok/s** | {res[3]} | {res[4]} | {res[5]} |", flush=True)
