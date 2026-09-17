FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Instalar dependencias del sistema requeridas por OpenCV, PyTorch y ComfyUI
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/ComfyUI

# Clonar ComfyUI oficial
RUN git clone https://github.com/comfyanonymous/ComfyUI.git /app/ComfyUI

# Instalar PyTorch CPU y dependencias básicas de ComfyUI
RUN pip install --upgrade pip && \
    pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cpu && \
    pip install -r requirements.txt

# Instalar extensiones esenciales por defecto en la imagen (Manager + GGUF)
RUN cd /app/ComfyUI/custom_nodes && \
    git clone https://github.com/ltdrdata/ComfyUI-Manager.git && \
    git clone https://github.com/city96/ComfyUI-GGUF.git && \
    pip install -r ComfyUI-Manager/requirements.txt -r ComfyUI-GGUF/requirements.txt || true

EXPOSE 8188

ENTRYPOINT ["python3", "main.py"]
CMD ["--listen", "0.0.0.0", "--port", "8188", "--cpu"]
