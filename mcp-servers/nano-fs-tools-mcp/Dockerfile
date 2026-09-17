FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastmcp httpx beautifulsoup4 html2text
COPY app.py .
CMD ["python", "app.py", "--transport", "sse", "--port", "8096"]
