#!/usr/bin/env bash
# Smoke test for the open-terminal:tools image. Run on the docker host.
set -uo pipefail

API=http://127.0.0.1:8003
KEY=$(docker inspect open-terminal --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^OPEN_TERMINAL_API_KEY=//p')

say() { printf '\n=== %s ===\n' "$1"; }

say "imagen / contenedor"
docker inspect open-terminal --format '{{.Config.Image}} | started {{.State.StartedAt}} | health {{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}'

say "pip / python"
docker exec open-terminal sh -lc 'command -v pip pip3 python3; pip --version; python3 -m pip --version'

say "paquetes IA clave"
docker exec open-terminal sh -lc 'python3 - <<EOF
mods = ["openai","anthropic","mcp","fastmcp","litellm","pydantic_ai","smolagents","langchain","langgraph","tiktoken","datasets","huggingface_hub","duckdb","polars","pandas","numpy","scipy","sklearn","matplotlib","sqlalchemy","faiss","pypdf","pdfplumber","docx","pptx","openpyxl","trafilatura","bs4","httpx","requests","pytest","ruff","uv"]
import importlib, sys
bad=[]
for m in mods:
    try: importlib.import_module(m)
    except Exception as e: bad.append(f"{m}: {type(e).__name__}: {e}")
print(f"OK {len(mods)-len(bad)}/{len(mods)}")
for b in bad: print("FAIL", b)
EOF'

say "cli del sistema"
docker exec open-terminal sh -lc 'for c in git curl wget jq rg fd bat tree less nano vim sqlite3 psql redis-cli ssh rsync gh node npm npx tsc tsx prettier pnpm uv pipx claude codex make gcc cmake htop tmux shellcheck ffprobe pandoc; do printf "%-12s %s\n" "$c" "$(command -v "$c" || echo MISSING)"; done'

say "API /execute (como usuario app, via HTTP)"
curl -s -m 60 -X POST "$API/execute?wait=30" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"command":"whoami; pwd; python3 -m pip --version; python3 -c \"import openai, fastmcp, pandas; print(openai.__version__)\"; npm ls -g --depth=0 2>/dev/null | head -12"}'
echo
