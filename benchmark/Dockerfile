FROM python:3.12-slim

LABEL description="Benchmark unificado 3 backends: OpenVINO GenAI + LM Studio Intel Arc + LM Studio AMD RX480"

WORKDIR /app

COPY benchmark_all.py /app/benchmark_all.py
COPY benchmark_dual_gpu.py /app/benchmark_dual_gpu.py

RUN mkdir -p /output

ENTRYPOINT ["python3", "/app/benchmark_all.py"]