FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastmcp==3.4.7 mcp==1.28.1 httpx beautifulsoup4 ddgs html2text
COPY app.py .
CMD ["python", "app.py"]


