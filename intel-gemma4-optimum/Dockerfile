FROM python:3.11-slim

# 1. Instalar dependencias del sistema y drivers Intel Compute Runtime (NEO / Level-Zero / OpenCL / Vulkan)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gnupg \
    ca-certificates \
    wget \
    curl \
    git \
    procps \
    iproute2 \
    net-tools \
    libatomic1 \
    libgomp1 \
    clinfo \
    ocl-icd-libopencl1 \
    mesa-vulkan-drivers \
    vulkan-tools \
    && wget -qO- https://repositories.intel.com/gpu/intel-graphics.key | gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu noble client" > /etc/apt/sources.list.d/intel-gpu.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       intel-level-zero-gpu \
       intel-opencl-icd \
       libze1 \
       libigc1 \
       libigdgmm12 \
    && rm -rf /var/lib/apt/lists/*

# 2. Instalar versiones exactas y optimizadas para evitar backtracking en pip
RUN pip install --no-cache-dir --default-timeout=100 \
    openvino==2026.3.0 \
    openvino-genai==2026.3.0.0 \
    openvino-tokenizers==2026.3.0.0 \
    optimum-intel==1.27.0 \
    optimum==2.1.0 \
    transformers==4.57.6 \
    accelerate \
    huggingface_hub \
    nncf==3.3.0 \
    fastapi \
    uvicorn \
    pydantic

# 3. Instalar LM Studio CLI (lms / llmster) para soporte LMLink nativo
RUN curl -fsSL https://lmstudio.ai/install.sh | bash

ENV PATH="/root/.lmstudio/bin:/root/.cache/lm-studio/bin:${PATH}"

# NPU: LD_LIBRARY_PATH must be in ENV (not just entrypoint export) so that
# docker exec commands and subprocesses also find the NPU Level-Zero libs.
ENV LD_LIBRARY_PATH="/usr/local/lib/python3.11/site-packages/openvino/libs:${LD_LIBRARY_PATH}"
ENV NPU_PLATFORM="3720"

WORKDIR /app

# 4. Copiar drivers NPU (Intel AI Boost / Meteor Lake NPU 3720) del snap intel-npu-driver
#    Estas libs se obtuvieron de /snap/intel-npu-driver/current/usr/lib/x86_64-linux-gnu/
#    - libopenvino_intel_npu_compiler_loader.so + compiler.so: compilador NPU para OpenVINO
#    - libze_intel_npu.so: driver Level-Zero NPU
#    - libze_loader.so: Level-Zero loader compatible (v1.28.2, no el v1.20.6 de apt)
#    - libze_tracing_layer.so: tracing layer compatible (v1.28.2)
COPY npu-drivers/ /opt/npu-drivers/
RUN OV_LIBS=/usr/local/lib/python3.11/site-packages/openvino/libs && \
    cp /opt/npu-drivers/libopenvino_intel_npu_compiler_loader.so $OV_LIBS/ && \
    cp /opt/npu-drivers/libopenvino_intel_npu_compiler.so $OV_LIBS/ && \
    cp -P /opt/npu-drivers/libze_intel_npu.so* $OV_LIBS/ && \
    cp -P /opt/npu-drivers/libze_loader.so* $OV_LIBS/ && \
    cp -P /opt/npu-drivers/libze_tracing_layer.so* $OV_LIBS/ && \
    cp -P /opt/npu-drivers/libtbb*.so* $OV_LIBS/ && \
    cp -P /opt/npu-drivers/libhwloc.so* $OV_LIBS/ && \
    mkdir -p /usr/local/lib/level-zero && \
    ln -sf $OV_LIBS/libze_intel_npu.so.1.35.0 /usr/local/lib/level-zero/libze_intel_npu.so.1 && \
    ldconfig

COPY server.py /app/server.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 1234 8000

ENTRYPOINT ["/entrypoint.sh"]
