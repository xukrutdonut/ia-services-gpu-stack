FROM python:3.11-slim
WORKDIR /app
COPY server.py .
COPY static ./static
ENV PYTHONUNBUFFERED=1 PORT=8098 DATA_DIR=/data/jobs
EXPOSE 8098
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8098/api/health',timeout=8)"
CMD ["python", "server.py"]
