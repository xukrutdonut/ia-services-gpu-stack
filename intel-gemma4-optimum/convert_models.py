#!/usr/bin/env python3
"""Convierte modelos HuggingFace a OpenVINO IR int4 para Intel Arc."""
import os, sys, time, gc, traceback

OUTPUT_DIR = "/models/.openvino-ir"

# (HF model ID, output dir name, needs HF auth)
# Llama models need HF auth -- skip if no token. Qwen/DeepSeek are public.
MODELS = [
    ("Qwen/Qwen2.5-3B-Instruct",        "qwen2.5-3b-instruct-int4-ov",       False),
    ("Qwen/Qwen1.5-MoE-A2.7B-Chat",      "qwen1.5-moe-a2.7b-chat-int4-ov",    False),
    ("deepseek-ai/DeepSeek-V2-Lite-Chat","deepseek-v2-lite-chat-int4-ov",     False),
    ("deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct", "deepseek-coder-v2-lite-instruct-int4-ov", False),
]

def convert_model(hf_id, ov_name, needs_auth):
    out_path = os.path.join(OUTPUT_DIR, ov_name)
    if os.path.exists(os.path.join(out_path, "openvino_model.bin")):
        print(f"[SKIP] {ov_name} ya existe", flush=True)
        return True

    print(f"\n{'='*60}", flush=True)
    print(f"[CONVERT] {hf_id} -> {ov_name}", flush=True)
    print(f"{'='*60}", flush=True)
    t0 = time.time()

    try:
        from optimum.intel import OVModelForCausalLM, OVQuantizer
        from transformers import AutoTokenizer
        import openvino as ov
        import torch

        # Step 1: Export a OpenVINO IR FP16
        print(f"[1/3] Exportando {hf_id} a OpenVINO IR...", flush=True)
        model = OVModelForCausalLM.from_pretrained(
            hf_id,
            export=True,
            trust_remote_code=True,
            torch_dtype=torch.float16,
        )
        tokenizer = AutoTokenizer.from_pretrained(hf_id, trust_remote_code=True)

        # Step 2: Cuantizar a int4 con OVQuantizer
        print(f"[2/3] Cuantizando a INT4...", flush=True)
        quantizer = OVQuantizer.from_pretrained(model)
        
        # Crear dataset de calibracion simple
        calib_samples = [
            "Hello, how are you today?",
            "The quick brown fox jumps over the lazy dog.",
            "In machine learning, a neural network is a model inspired by the brain.",
            "Explain the concept of artificial intelligence in simple terms.",
            "Write a Python function to sort a list of numbers.",
        ]
        
        def calib_data(batch_size=1):
            for sample in calib_samples:
                yield tokenizer(sample, return_tensors="pt")
        
        os.makedirs(out_path, exist_ok=True)
        quantizer.quantize(
            calibration_dataset=calib_data(),
            save_directory=out_path,
            weights_only=True,
            sym=True,
            group_size=128,
        )
        
        # Guardar tokenizer
        tokenizer.save_pretrained(out_path)
        
        # Liberar memoria
        del model, tokenizer, quantizer
        gc.collect()
        
        t_total = time.time() - t0
        size_mb = os.path.getsize(os.path.join(out_path, "openvino_model.bin")) / 1024 / 1024
        print(f"[DONE] {ov_name}: {t_total:.1f}s | {size_mb:.0f}MB", flush=True)
        return True

    except Exception as e:
        print(f"[ERROR] {ov_name}: {e}", flush=True)
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print(f"Output dir: {OUTPUT_DIR}", flush=True)
    print(f"Models to convert: {len(MODELS)}", flush=True)
    
    for hf_id, ov_name, needs_auth in MODELS:
        convert_model(hf_id, ov_name, needs_auth)
    
    # Listar modelos finales
    print(f"\n{'='*60}", flush=True)
    print("Modelos OpenVino IR disponibles:", flush=True)
    print(f"{'='*60}", flush=True)
    if os.path.exists(OUTPUT_DIR):
        for d in sorted(os.listdir(OUTPUT_DIR)):
            full = os.path.join(OUTPUT_DIR, d)
            if os.path.isdir(full):
                bin_path = os.path.join(full, "openvino_model.bin")
                if os.path.exists(bin_path):
                    size_mb = os.path.getsize(bin_path) / 1024 / 1024
                    print(f"  {d}: {size_mb:.0f}MB", flush=True)
