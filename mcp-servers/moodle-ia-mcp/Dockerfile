FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastmcp httpx
COPY app.py .
CMD ["python", "app.py", "--port", "8097"]