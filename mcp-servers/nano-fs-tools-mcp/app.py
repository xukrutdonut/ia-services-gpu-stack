import os
import sys
import json
import asyncio
import subprocess
import httpx
from pathlib import Path
from typing import List, Optional
from fastmcp import FastMCP

mcp = FastMCP("Nano-FS & Subagent Orchestrator")

WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace")).resolve()

# LM Studio central con LMlink: modelos intel-arc-* (OpenVINO GenAI) y rx480/* (AMD Vulkan)
LMSTUDIO_URL = os.environ.get("LMSTUDIO_URL", "http://192.168.0.100:1234/v1")
# OpenVINO GenAI directo (intel-gemma4-optimum container)
OPENVINO_GENAI_URL = os.environ.get("OPENVINO_GENAI_URL", "http://192.168.0.100:8006/v1")
# Alias compatibilidad
INTEL_OPENVINO_URL = os.environ.get("INTEL_OPENVINO_URL", LMSTUDIO_URL)
INTEL_HOST_URL = os.environ.get("INTEL_HOST_URL", LMSTUDIO_URL)
AMD_NODE_URL = os.environ.get("AMD_NODE_URL", LMSTUDIO_URL)

def safe_path(p: str) -> Path:
    target = (WORKSPACE_DIR / p).resolve()
    # Permitir workspace o rutas dentro del contenedor
    return target

# ==========================================
# 1. HERRAMIENTAS DE PLANIFICACIÓN (PLAN TOOLS)
# ==========================================

@mcp.tool()
def plan_create(title: str, tasks: List[str]) -> dict:
    """Create a new project task plan in workspace (plan.md)."""
    try:
        plan_file = safe_path("plan.md")
        lines = [f"# {title}", "", "## Tareas"]
        for idx, t in enumerate(tasks, 1):
            lines.append(f"- [ ] T{idx}: {t}")
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"ok": True, "path": str(plan_file), "total": len(tasks), "msg": f"Plan created with {len(tasks)} tasks"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.tool()
def plan_status() -> dict:
    """Read task plan progress and current tasks status."""
    try:
        plan_file = safe_path("plan.md")
        if not plan_file.exists():
            return {"ok": False, "error": "plan.md does not exist"}
        content = plan_file.read_text(encoding="utf-8")
        tasks = []
        idx = 1
        for line in content.splitlines():
            line = line.strip()
            if line.startswith(("- [ ]", "- [x]", "- [X]", "* [ ]", "* [x]", "* [X]")):
                done = "[x]" in line.lower()
                text = line[5:].strip()
                tasks.append({"index": idx, "text": text, "completed": done})
                idx += 1
        total = len(tasks)
        done_count = sum(1 for t in tasks if t["completed"])
        pct = round((done_count / total * 100)) if total > 0 else 0
        return {
            "ok": True,
            "progress": f"{done_count}/{total} ({pct}%)",
            "all_done": done_count == total and total > 0,
            "tasks": tasks
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.tool()
def plan_update(task_index: int, completed: bool) -> dict:
    """Mark a task as completed (true) or pending (false) in plan.md."""
    try:
        plan_file = safe_path("plan.md")
        if not plan_file.exists():
            return {"ok": False, "error": "plan.md does not exist"}
        lines = plan_file.read_text(encoding="utf-8").splitlines()
        current_idx = 1
        updated = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(("- [ ]", "- [x]", "- [X]", "* [ ]", "* [x]", "* [X]")):
                if current_idx == task_index:
                    mark = "x" if completed else " "
                    # Reemplazar la casilla de verificación
                    clean_text = line.strip()[5:].strip()
                    new_lines.append(f"- [{mark}] {clean_text}")
                    updated = True
                else:
                    new_lines.append(line)
                current_idx += 1
            else:
                new_lines.append(line)
        if not updated:
            return {"ok": False, "error": f"Task #{task_index} not found"}
        plan_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return {"ok": True, "task": f"T{task_index} -> {'DONE' if completed else 'PENDING'}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@mcp.tool()
def audit_plan(files_to_check: Optional[List[str]] = None) -> dict:
    """Audit project status: verifies required files exist and checks plan.md completion."""
    try:
        missing = []
        present = []
        if files_to_check:
            for f in files_to_check:
                p = safe_path(f)
                if p.exists():
                    present.append(f"{f} ({p.stat().st_size}B)")
                else:
                    missing.append(f)
        status_res = plan_status()
        plan_done = status_res.get("all_done", False) if status_res.get("ok") else False
        passed = (len(missing) == 0) and (plan_done or not status_res.get("ok"))
        return {
            "status": "PASSED" if passed else "FAILED",
            "plan_progress": status_res.get("progress", "No plan"),
            "missing_files": missing if missing else None,
            "verified_files": present
        }
    except Exception as e:
        return {"status": "FAILED", "error": str(e)}

# ==========================================
# 2. DISPATCHER SUBAGENTICO MULTI-HARDWARE
# ==========================================

LMSTUDIO_TOKEN = os.environ.get("LMSTUDIO_TOKEN", "sk-lm-lmchat01:akelarreakelarre1234")

def get_auth_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LMSTUDIO_TOKEN}"
    }

async def get_target_model(base_url: str, match_keywords: list, fallback: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url}/models", headers=get_auth_headers())
            if resp.status_code == 200:
                models = [m.get("id", "") for m in resp.json().get("data", [])]
                for kw in match_keywords:
                    for m in models:
                        if kw.lower() in m.lower():
                            return m
                if models:
                    return models[0]
    except Exception:
        pass
    return fallback

@mcp.tool()
async def subagent_task(
    task_description: str,
    subagent: str = "auto",
    max_tokens: int = 800
) -> dict:
    """Delegate a specialized sub-task to a cluster subagent via LMlink: 'coder' (Intel Arc OpenVINO Qwen2.5-Coder-7B), 'reasoning' (Intel Arc MoE/Qwen2.5), 'fast' (Intel Arc Llama-3.2-3B), 'rx480' (AMD RX480 Vulkan), 'openvino' (OpenVINO GenAI directo), or 'auto'."""
    role = subagent.lower()

    if role in ("rx480", "amd"):
        # RX480 via LMlink (prefijo rx480/)
        base_url = AMD_NODE_URL
        model = await get_target_model(base_url, ["rx480/", "rx480-"], "rx480/qwen1.5-moe-a2.7b-chat@q4_k_m")
        sys_prompt = "You are a lightning-fast subagent running on dedicated AMD Radeon RX 480 Vulkan GPU hardware via LMlink. Return minimal token output and execute tasks directly."
    elif role == "openvino":
        # OpenVINO GenAI directo (intel-gemma4-optimum container, sin LMlink)
        base_url = OPENVINO_GENAI_URL
        model = await get_target_model(base_url, ["qwen2.5-coder", "qwen2.5-3b", "qwen1.5-moe"], "qwen2.5-3b-instruct-int4-ov")
        sys_prompt = "You are a subagent running on Intel Arc GPU via OpenVINO GenAI. Be concise and direct."
    elif role == "coder" or ("code" in task_description.lower() and role == "auto"):
        # Coder via LMlink (prefijo intel-arc-)
        base_url = INTEL_OPENVINO_URL
        model = await get_target_model(base_url, ["intel-arc-qwen2.5-coder", "intel-arc-coder"], "intel-arc-qwen2.5-coder-7b-instruct")
        sys_prompt = "You are an Expert Coder subagent running on Intel Arc GPU (OpenVINO) via LMlink. Write clean, optimal, bug-free, and production-ready code with exact syntax. Be concise and direct."
    elif role in ("reasoning", "auditor") or ("audit" in task_description.lower() and role == "auto"):
        base_url = INTEL_OPENVINO_URL
        model = await get_target_model(base_url, ["intel-arc/qwen1.5-moe", "intel-arc-moe", "intel-arc-qwen2.5-3b"], "intel-arc/qwen1.5-moe-a2.7b-chat@q4_k_m")
        sys_prompt = "You are a Principal Software Architect and Logic Auditor subagent running on Intel Arc GPU (OpenVINO) via LMlink. Rigorously analyze logic, detect edge cases, and verify correctness."
    else:
        # Fast/default: Llama-3.2-3B en Intel Arc
        base_url = INTEL_HOST_URL
        model = await get_target_model(base_url, ["intel-arc-llama-3.2-3b", "intel-arc-llama-3.2-1b"], "intel-arc-llama-3.2-3b-instruct")
        sys_prompt = "You are a concise precision assistant subagent running on Intel Arc GPU via LMlink. Provide rapid, concise summaries, structured data extraction, and direct answers without conversational preamble."

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"TASK: {task_description}"}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=get_auth_headers())
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {
                "ok": True,
                "subagent_target": role,
                "endpoint": base_url,
                "model_used": model,
                "result": content.strip()
            }
    except Exception as e:
        # Fallback a Intel Host :1234 si falla el nodo primario
        try:
            fallback_model = await get_target_model(INTEL_HOST_URL, ["rx480", "llama-3.2-3b"], "llama-3.2-3b-instruct [rx480 49.46t-s tool]")
            payload["model"] = fallback_model
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(f"{INTEL_HOST_URL}/chat/completions", json=payload, headers=get_auth_headers())
                resp.raise_for_status()
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {
                    "ok": True,
                    "subagent_target": "fallback-intel-host",
                    "model_used": fallback_model,
                    "result": content.strip()
                }
        except Exception as e2:
            return {"ok": False, "error": f"Primary subagent failed: {e}. Fallback failed: {e2}"}



# ==========================================
# 3. HERRAMIENTAS DE ARCHIVOS Y EJECUCIÓN (FS TOOLS)
# ==========================================

@mcp.tool()
def read_file(path: str) -> str:
    """Read full content of a file in workspace."""
    p = safe_path(path)
    if not p.exists() or not p.is_file():
        return f"Error: File '{path}' not found."
    return p.read_text(encoding="utf-8", errors="replace")

@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Create or overwrite a file in workspace."""
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Successfully wrote {len(content)} characters to {path}"

@mcp.tool()
def edit_file(path: str, target: str, replacement: str) -> str:
    """Replace an exact unique substring in a file with new content."""
    p = safe_path(path)
    if not p.exists() or not p.is_file():
        return f"Error: File '{path}' not found."
    orig = p.read_text(encoding="utf-8")
    if target not in orig:
        return f"Error: Target substring not found in {path}"
    if orig.count(target) > 1:
        return f"Error: Target substring occurs multiple times ({orig.count(target)}) in {path}. Provide more context."
    new_content = orig.replace(target, replacement, 1)
    p.write_text(new_content, encoding="utf-8")
    return f"Successfully updated {path}"

@mcp.tool()
def list_dir(path: str = ".") -> dict:
    """List entries in a directory with file sizes."""
    p = safe_path(path)
    if not p.exists() or not p.is_dir():
        return {"error": f"Directory '{path}' not found."}
    entries = []
    for item in p.iterdir():
        entries.append({
            "name": item.name,
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else None
        })
    return {"path": path, "entries": entries}

@mcp.tool()
def create_dir(path: str) -> str:
    """Create a directory (and any missing parent directories) in workspace."""
    p = safe_path(path)
    if p.exists():
        return f"Directory already exists: {path}"
    p.mkdir(parents=True, exist_ok=True)
    return f"Successfully created directory: {path}"

@mcp.tool()
def delete_file(path: str) -> str:
    """Delete a file from workspace. Fails if the path is a directory."""
    import shutil
    p = safe_path(path)
    if not p.exists():
        return f"Error: File '{path}' not found."
    if p.is_dir():
        return f"Error: '{path}' is a directory, use delete_dir instead."
    p.unlink()
    return f"Successfully deleted file: {path}"

@mcp.tool()
def delete_dir(path: str, recursive: bool = True) -> str:
    """Delete a directory from workspace. If recursive=true, removes all contents recursively."""
    import shutil
    p = safe_path(path)
    if not p.exists():
        return f"Error: Directory '{path}' not found."
    if not p.is_dir():
        return f"Error: '{path}' is a file, use delete_file instead."
    if recursive:
        shutil.rmtree(p)
        return f"Successfully deleted directory (recursive): {path}"
    else:
        try:
            p.rmdir()
            return f"Successfully deleted directory: {path}"
        except OSError as e:
            return f"Error: Directory not empty (use recursive=true): {e}"

@mcp.tool()
def move_file(src: str, dst: str) -> str:
    """Move or rename a file/directory from src to dst within workspace."""
    import shutil
    s = safe_path(src)
    d = safe_path(dst)
    if not s.exists():
        return f"Error: Source '{src}' not found."
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return f"Successfully moved '{src}' to '{dst}'"

@mcp.tool()
def file_info(path: str) -> dict:
    """Get detailed info about a file or directory: size, type, permissions."""
    import os
    p = safe_path(path)
    if not p.exists():
        return {"error": f"Path '{path}' not found."}
    st = p.stat()
    return {
        "path": path,
        "exists": True,
        "is_dir": p.is_dir(),
        "is_file": p.is_file(),
        "size": st.st_size,
        "permissions": oct(st.st_mode & 0o777)[2:],
        "modified": st.st_mtime,
    }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--transport", type=str, default="streamable-http", choices=["sse", "streamable-http"])
    args = parser.parse_args()
    mcp.run(transport=args.transport, host="0.0.0.0", port=args.port)
