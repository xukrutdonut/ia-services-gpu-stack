#!/usr/bin/env python3
"""
oi_mcp_proxy.py — Proxy API entre Open Interpreter y OpenVINO GenAI.

Sitúa OI detrás de un endpoint OpenAI-compatible (localhost:8007) que:
  1. Inyecta las tools de multiples servidores MCP en el schema tools
     de cada /v1/chat/completions.
  2. Pasa la petición al backend OpenVINO (localhost:8006).
  3. Si el modelo responde con tool_call de una tool MCP → la ejecuta via SSE
     contra el servidor MCP correspondiente, inyecta el resultado como mensaje
     "tool", y reconsulta el modelo.
  4. Si el modelo llama `execute` (tool nativa de OI) → pasa transparente.

Servidores MCP soportados:
  - openterminal-mcp (localhost:8003): execute_command, terminal_read_file, terminal_list_files
  - searxng-mcp      (localhost:8092): search_web
  - nano-fs-tools-mcp (localhost:8096): read_file, write_file, edit_file, list_dir,
    create_dir, delete_file, delete_dir, move_file, file_info, plan_create, plan_status,
    plan_update, audit_plan, subagent_task

Uso:
  python3 oi_mcp_proxy.py
  # OI se conecta a --api_base http://localhost:8007/v1
"""

import json
import os
import asyncio
import logging
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

# ── Configuración ────────────────────────────────────────────────────────────
BACKEND_URL = os.getenv("OVINO_BACKEND", "http://localhost:8006")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8007"))
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "10"))

# Lista de servidores MCP (nombre => SSE URL)
MCP_SERVERS: dict[str, str] = {
    "openterminal-mcp": os.getenv("MCP_SSE_URL", "http://localhost:8003/sse"),
    "searxng-mcp": os.getenv("SEARXNG_SSE_URL", "http://localhost:8092/sse"),
    "nano-fs-tools-mcp": os.getenv("NANO_FS_SSE_URL", "http://localhost:8096/sse"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("oi-mcp-proxy")

app = FastAPI(title="OI-MCP Proxy")

# Cache: tool_name => (server_name, sse_url). Se llena al arrancar.
_TOOL_REGISTRY: dict[str, str] = {}


# ── MCP client (conexion fresca por llamada) ────────────────────────────────

async def _mcp_list_tools(sse_url: str) -> list[dict]:
    """Conecta via SSE a un servidor MCP y devuelve sus tools."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(sse_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.inputSchema if hasattr(t, "inputSchema") else t.input_schema,  # type: ignore[attr-defined]
                }
                for t in tools.tools
            ]


async def _mcp_call_tool(sse_url: str, tool_name: str, arguments: dict) -> str:
    """Conecta via SSE, ejecuta la tool, devuelve texto."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(sse_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            parts = []
            for c in result.content:
                if hasattr(c, "text"):
                    parts.append(c.text)
                else:
                    parts.append(str(c))
            return "\n".join(parts)


# ── Multi-MCP: agrega tools de todos los servidores ──────────────────────────

async def fetch_all_mcp_tools() -> list[dict]:
    """Conecta a todos los servidores MCP, fusiona sus tools en schema OpenAI."""
    schemas: list[dict] = []
    _TOOL_REGISTRY.clear()

    for server_name, sse_url in MCP_SERVERS.items():
        try:
            raw_tools = await _mcp_list_tools(sse_url)
            for t in raw_tools:
                _TOOL_REGISTRY[t["name"]] = sse_url
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t["description"],
                            "parameters": t["input_schema"],
                        },
                    }
                )
                log.info("MCP [%s] tool registered: %s", server_name, t["name"])
        except Exception as e:
            log.warning("MCP [%s] (%s) unavailable: %s", server_name, sse_url, e)

    log.info("Total MCP tools injected: %d", len(schemas))
    return schemas


async def call_mcp_tool(name: str, arguments: dict) -> str:
    """Busca el servidor que tiene la tool y la ejecuta."""
    sse_url = _TOOL_REGISTRY.get(name)
    if not sse_url:
        raise ValueError(f"Tool '{name}' not found in any MCP server")
    return await _mcp_call_tool(sse_url, name, arguments)


# ── Proxy endpoints ──────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup():
    """Precarga el registry de tools al arrancar."""
    log.info("OI-MCP Proxy starting on port %d", PROXY_PORT)
    log.info("Backend: %s", BACKEND_URL)
    log.info("MCP servers: %s", MCP_SERVERS)
    try:
        await fetch_all_mcp_tools()
    except Exception as e:
        log.error("Failed to preload MCP tools: %s", e)


@app.get("/v1/models")
async def list_models():
    """Pasa transparente al backend."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{BACKEND_URL}/v1/models")
        return JSONResponse(content=r.json(), status_code=r.status_code)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Proxy principal: inyecta tools MCP, maneja tool_calls en bucle."""
    body = await request.json()
    stream = body.get("stream", False)

    # Inyectar tools de todos los MCP
    try:
        mcp_schemas = await fetch_all_mcp_tools()
    except Exception as e:
        log.warning("No se pudieron obtener tools MCP: %s — operando sin tools", e)
        mcp_schemas = []

    existing_tools = body.get("tools", [])
    merged_tools = existing_tools + mcp_schemas
    body["tools"] = merged_tools

    if merged_tools and "tool_choice" not in body:
        body["tool_choice"] = "auto"

    if stream:
        return StreamingResponse(
            _stream_proxy(body),
            media_type="text/event-stream",
        )
    else:
        return await _non_stream_proxy(body)


def _is_mcp_tool(name: str) -> bool:
    return name in _TOOL_REGISTRY


async def _non_stream_proxy(body: dict) -> JSONResponse:
    """Bucle no-streaming: envía al backend, maneja tool_calls MCP, reconsulta."""
    async with httpx.AsyncClient(timeout=300) as client:
        data = None
        for round_n in range(MAX_TOOL_ROUNDS):
            log.info("Round %d — sending to backend", round_n)
            r = await client.post(
                f"{BACKEND_URL}/v1/chat/completions",
                json=body,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code != 200:
                return JSONResponse(content=r.json(), status_code=r.status_code)

            data = r.json()
            choice = data["choices"][0]
            msg = choice["message"]

            tool_calls = msg.get("tool_calls", [])
            mcp_calls = [tc for tc in tool_calls if _is_mcp_tool(tc["function"]["name"])]

            if not mcp_calls:
                return JSONResponse(content=data, status_code=200)

            log.info("MCP tool calls: %s", [tc["function"]["name"] for tc in mcp_calls])

            messages = body["messages"]
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content", ""),
                    "tool_calls": tool_calls,
                }
            )

            for tc in mcp_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                try:
                    result_text = await call_mcp_tool(name, args)
                    log.info("MCP %s result: %s", name, result_text[:200])
                except Exception as e:
                    result_text = f"ERROR executing {name}: {e}"
                    log.error("MCP %s failed: %s", name, e)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_text,
                    }
                )

            body["messages"] = messages

        if data is None:
            return JSONResponse(
                content={"error": "No response from backend"}, status_code=502
            )
        log.warning("MAX_TOOL_ROUNDS reached, returning last response")
        return JSONResponse(content=data, status_code=200)


async def _stream_proxy(body: dict):
    """Streaming: convierte el bucle interno en SSE chunks."""
    async with httpx.AsyncClient(timeout=300) as client:
        for round_n in range(MAX_TOOL_ROUNDS):
            body_stream_off = {**body, "stream": False}
            r = await client.post(
                f"{BACKEND_URL}/v1/chat/completions",
                json=body_stream_off,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code != 200:
                yield f"data: {json.dumps(r.json())}\n\n"
                yield "data: [DONE]\n\n"
                return

            data = r.json()
            choice = data["choices"][0]
            msg = choice["message"]

            tool_calls = msg.get("tool_calls", [])
            mcp_calls = [tc for tc in tool_calls if _is_mcp_tool(tc["function"]["name"])]

            if not mcp_calls:
                content = msg.get("content", "")
                if content:
                    chunk = {
                        "id": data.get("id", "chatcmpl-proxy"),
                        "object": "chat.completion.chunk",
                        "created": data.get("created", 0),
                        "model": data.get("model", ""),
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": content, "role": "assistant"},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"

                if tool_calls:
                    chunk = {
                        "id": data.get("id", "chatcmpl-proxy"),
                        "object": "chat.completion.chunk",
                        "created": data.get("created", 0),
                        "model": data.get("model", ""),
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "tool_calls": tool_calls,
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"

                chunk = {
                    "id": data.get("id", "chatcmpl-proxy"),
                    "object": "chat.completion.chunk",
                    "created": data.get("created", 0),
                    "model": data.get("model", ""),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": choice.get("finish_reason", "stop"),
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
                return

            log.info(
                "Stream round %d — MCP tool calls: %s",
                round_n,
                [tc["function"]["name"] for tc in mcp_calls],
            )

            messages = body["messages"]
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content", ""),
                    "tool_calls": tool_calls,
                }
            )

            for tc in mcp_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                try:
                    result_text = await call_mcp_tool(name, args)
                    log.info("MCP %s result: %s", name, result_text[:200])
                except Exception as e:
                    result_text = f"ERROR executing {name}: {e}"
                    log.error("MCP %s failed: %s", name, e)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_text,
                    }
                )

            body["messages"] = messages

        yield "data: [DONE]\n\n"


if __name__ == "__main__":
    # Precarga el registry en el arranque
    asyncio.run(fetch_all_mcp_tools())
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT, log_level="info")