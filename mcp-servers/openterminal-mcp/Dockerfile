FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl wget git ca-certificates procps build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastmcp
RUN mkdir -p /workspace
COPY app.py .
CMD ["python", "app.py"]
