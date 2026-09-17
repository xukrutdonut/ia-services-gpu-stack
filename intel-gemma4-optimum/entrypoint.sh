#!/bin/bash
set -e

# Configuración óptima para Intel Arc GPU / Arrow Lake / Level-Zero / OpenVINO
export MESA_VK_DEVICE_SELECT="8086:7d51!"
export GGML_VK_VISIBLE_DEVICES=0
export ONEAPI_DEVICE_SELECTOR="level_zero:gpu"
export ZES_ENABLE_SYSMAN=1
export UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS=1
export SYCL_PI_LEVEL_ZERO_USM_RESIDENT=1
export NEOReadDebugKeys=1
export OverrideGpuAddressSpace=48
export DisableImplicitScaling=0
export IGC_EnableDPEmulation=0
export OMP_NUM_THREADS=16
export KMP_AFFINITY="granularity=fine,compact,1,0"

# --- NPU Intel AI Boost (Meteor Lake NPU 3720) ---
# Las libs del snap intel-npu-driver se copian a openvino/libs en el Dockerfile.
# El tracing layer del snap (v1.28.2) debe tener prioridad sobre el de apt (v1.20.6),
# sino zeInit falla con ZE_RESULT_ERROR_UNINITIALIZED.
export NPU_PLATFORM="3720"
export OV_NPU_LIBS_PATH="/usr/local/lib/python3.11/site-packages/openvino/libs"

if [ -f /opt/intel/oneapi/setvars.sh ]; then
    echo "[intel-gemma4-optimum] Cargando entorno Intel oneAPI..."
    source /opt/intel/oneapi/setvars.sh --force >/dev/null 2>&1 || true
fi
export LD_LIBRARY_PATH="$OV_NPU_LIBS_PATH:/opt/intel/oneapi/compiler/latest/lib:/opt/intel/oneapi/mkl/latest/lib:/opt/intel/oneapi/tbb/latest/lib:/opt/llama-sycl/bin:$LD_LIBRARY_PATH"

echo "[intel-gemma4-optimum] Verificando Level-Zero / OpenCL / GPU Intel..."
if command -v clinfo > /dev/null 2>&1; then
    clinfo -l || true
fi

echo "[intel-gemma4-optimum] Iniciando servidor OpenAI API OpenVINO GenAI en 0.0.0.0:8000..."
export PYTHONPATH="/app:${PYTHONPATH}"
python3 -m uvicorn server:app --app-dir /app --host 0.0.0.0 --port 8000 > /tmp/openvino_server.log 2>&1 &
sleep 2

echo "[intel-gemma4-optimum] Contenedor activo y listo. Transmitiendo logs..."
tail -f /tmp/openvino_server.log /dev/null