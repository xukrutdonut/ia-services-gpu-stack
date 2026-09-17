#!/usr/bin/env python3
"""Convierte un modelo HF a OpenVINO IR INT4 usando optimum-cli + quantization_config."""
import os
import sys
import subprocess

MODEL_ID = sys.argv[1]  # e.g. "Qwen/Qwen2.5-3B-Instruct"
OUTPUT_NAME = sys.argv[2]  # e.g. "qwen2.5-3b-instruct-int4-ov"
TMP_FP32 = f"/tmp/{OUTPUT_NAME}-fp32"
OUTPUT = f"/models/.openvino-ir/{OUTPUT_NAME}"

print(f"[1/3] Exportando {MODEL_ID} a OpenVINO IR FP32...", flush=True)
subprocess.run(
    ["optimum-cli", "export", "openvino", "--model", MODEL_ID, TMP_FP32],
    check=True
)

print(f"[2/3] Cuantizando a INT4 (group_size=32, ratio=0.8)...", flush=True)
os.makedirs(OUTPUT, exist_ok=True)
from optimum.intel import OVModelForCausalLM
from transformers import AutoTokenizer

quant_config = {"bits": 4, "group_size": 32, "ratio": 0.8}
model = OVModelForCausalLM.from_pretrained(
    TMP_FP32,
    quantization_config=quant_config,
    compile=False
)
tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
model.save_pretrained(OUTPUT)
tokenizer.save_pretrained(OUTPUT)

print(f"[3/3] Limpiando temporal...", flush=True)
subprocess.run(["rm", "-rf", TMP_FP32])
print(f"Done: {OUTPUT}", flush=True)