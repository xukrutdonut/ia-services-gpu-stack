FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git curl build-essential && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'zotero-mcp-server[semantic]' fastmcp
ENV ZOTERO_LOCAL=true
CMD ["zotero-mcp", "serve", "--transport", "sse", "--host", "0.0.0.0", "--port", "8765"]
