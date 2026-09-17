import os
import asyncio
from pathlib import Path
from fastmcp import FastMCP

mcp = FastMCP("Open-Terminal-Sandbox")
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace")).resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

def safe_path(p: str) -> Path:
    if os.path.isabs(p):
        return Path(p).resolve()
    return (WORKSPACE_DIR / p).resolve()

@mcp.tool()
async def execute_command(command: str, cwd: str = "/workspace", wait: int = 30) -> str:
    """Execute a bash command directly in the terminal container sandbox."""
    work_dir = cwd if os.path.exists(cwd) else str(WORKSPACE_DIR)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=float(wait))
            output = stdout.decode("utf-8", errors="replace").strip()
            return output or f"Process completed with exit code {proc.returncode}"
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return f"Command timed out after {wait} seconds."
    except Exception as e:
        return f"Error executing command: {e}"

@mcp.tool()
def terminal_read_file(path: str) -> str:
    """Read a text file from the terminal sandbox container."""
    try:
        p = safe_path(path)
        if not p.exists() or not p.is_file():
            return f"Error: File '{path}' not found."
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading file: {e}"

@mcp.tool()
def terminal_list_files(path: str = "/workspace") -> list:
    """List files in directory inside terminal sandbox container."""
    try:
        p = safe_path(path)
        if not p.exists() or not p.is_dir():
            return [f"Error: Directory '{path}' not found."]
        entries = []
        for item in p.iterdir():
            suffix = "/" if item.is_dir() else ""
            entries.append(f"{item.name}{suffix}")
        return sorted(entries)
    except Exception as e:
        return [f"Error listing files: {e}"]

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8003)
