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

@mcp.tool()
def terminal_write_file(path: str, content: str) -> str:
    """Write content to a file in the terminal sandbox. Creates parent directories if needed. Overwrites existing content."""
    try:
        p = safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written {len(content)} bytes to {p}"
    except Exception as e:
        return f"Error writing file: {e}"

@mcp.tool()
def terminal_append_file(path: str, content: str) -> str:
    """Append content to an existing file in the terminal sandbox. Creates the file if it doesn't exist."""
    try:
        p = safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended {len(content)} bytes to {p}"
    except Exception as e:
        return f"Error appending to file: {e}"

@mcp.tool()
def terminal_remove(path: str, recursive: bool = False) -> str:
    """Remove a file or directory from the terminal sandbox. Use recursive=True for directories."""
    try:
        p = safe_path(path)
        if not p.exists():
            return f"Error: '{path}' not found."
        if p.is_dir():
            if not recursive:
                return f"Error: '{path}' is a directory. Use recursive=True to remove it."
            import shutil
            shutil.rmtree(p)
            return f"Removed directory {p} (recursive)"
        else:
            p.unlink()
            return f"Removed file {p}"
    except Exception as e:
        return f"Error removing path: {e}"

@mcp.tool()
def terminal_mkdir(path: str, parents: bool = True) -> str:
    """Create a directory in the terminal sandbox. Use parents=True to create parent directories as needed."""
    try:
        p = safe_path(path)
        p.mkdir(parents=parents, exist_ok=True)
        return f"Created directory {p}"
    except Exception as e:
        return f"Error creating directory: {e}"

@mcp.tool()
def terminal_move(src: str, dst: str) -> str:
    """Move or rename a file/directory inside the terminal sandbox."""
    try:
        s = safe_path(src)
        d = safe_path(dst)
        if not s.exists():
            return f"Error: Source '{src}' not found."
        d.parent.mkdir(parents=True, exist_ok=True)
        s.rename(d)
        return f"Moved {s} -> {d}"
    except Exception as e:
        return f"Error moving path: {e}"

@mcp.tool()
def terminal_copy(src: str, dst: str, recursive: bool = True) -> str:
    """Copy a file or directory inside the terminal sandbox. Use recursive=True for directories."""
    try:
        s = safe_path(src)
        d = safe_path(dst)
        if not s.exists():
            return f"Error: Source '{src}' not found."
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            if not recursive:
                return f"Error: '{src}' is a directory. Use recursive=True to copy it."
            import shutil
            shutil.copytree(s, d, dirs_exist_ok=True)
            return f"Copied directory {s} -> {d}"
        else:
            import shutil
            shutil.copy2(s, d)
            return f"Copied file {s} -> {d}"
    except Exception as e:
        return f"Error copying path: {e}"

@mcp.tool()
def terminal_file_info(path: str) -> str:
    """Get detailed info about a file or directory: size, permissions, modification time."""
    try:
        import os, time
        p = safe_path(path)
        if not p.exists():
            return f"Error: '{path}' not found."
        stat = p.stat()
        info = {
            "path": str(p),
            "type": "directory" if p.is_dir() else "file",
            "size_bytes": stat.st_size,
            "permissions": oct(stat.st_mode & 0o777)[2:],
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_ctime)),
        }
        return str(info)
    except Exception as e:
        return f"Error getting file info: {e}"

@mcp.tool()
def terminal_search_files(path: str = "/workspace", pattern: str = "*") -> list:
    """Search for files matching a glob pattern inside a directory in the terminal sandbox."""
    try:
        p = safe_path(path)
        if not p.exists() or not p.is_dir():
            return [f"Error: Directory '{path}' not found."]
        matches = sorted(str(m.relative_to(p)) for m in p.rglob(pattern))
        return matches if matches else [f"No files matching '{pattern}' in {path}"]
    except Exception as e:
        return [f"Error searching files: {e}"]

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8003)
