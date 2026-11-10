#!/usr/bin/env python3
"""mesh-orchestrator: cola acotada + subagentes sobre la malla LiteLLM.

Dos piezas:

1) COLA. Cada carril (modelo) tiene un semaforo (concurrencia real) y una cola
   acotada. Si la cola pasa de MAX_QUEUE, se responde 429 con Retry-After en vez
   de aceptar trabajo que no se va a poder atender. GET /v1/queue expone la
   presion de cada carril para que el cliente decida.

2) SUBAGENTES. Un job es una lista de tareas independientes (cada una con su
   propio contexto) o un fan-out: N subagentes procesan N elementos y un planner
   sintetiza. Los subagentes pueden usar la herramienta `run_command` dentro de
   este contenedor (aislado, sin acceso al host) si ALLOW_SHELL=1.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

BASE = os.environ.get("LITELLM_BASE", "http://litellm:4000/v1")
KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-mesh")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
MAX_QUEUE = int(os.environ.get("MAX_QUEUE_PER_LANE", "100"))
CONCURRENCY = json.loads(os.environ.get("LANE_CONCURRENCY", '{"chat":2,"deep":1,"worker":4,"worker-big":2,"tiny":4}'))
# Sonda de salud por carril: si el nodo que lo sirve esta caido, no esperamos al
# timeout del gateway (20s tirados por peticion): se salta al carril de respaldo
# durante DOWN_TTL segundos. Formato JSON {carril: url_que_debe_dar_200}.
LANE_HEALTH = json.loads(os.environ.get("LANE_HEALTH", "{}"))
LANE_FALLBACK = json.loads(os.environ.get("LANE_FALLBACK", "{}"))
DOWN_TTL = float(os.environ.get("LANE_DOWN_TTL", "60"))
HEALTH_TIMEOUT = float(os.environ.get("LANE_HEALTH_TIMEOUT", "3"))
ALLOW_SHELL = os.environ.get("ALLOW_SHELL", "0") == "1"
TASK_TIMEOUT = float(os.environ.get("TASK_TIMEOUT", "900"))

DB = os.path.join(DATA_DIR, "jobs.db")
os.makedirs(DATA_DIR, exist_ok=True)


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, check_same_thread=False)
    c.execute(
        """CREATE TABLE IF NOT EXISTS jobs(
             id TEXT PRIMARY KEY, kind TEXT, model TEXT, planner TEXT,
             total INTEGER, done INTEGER, status TEXT, created REAL, updated REAL, payload TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS tasks(
             job TEXT, tid TEXT, prompt TEXT, status TEXT, output TEXT, error TEXT,
             latency REAL, tokens INTEGER, PRIMARY KEY(job, tid))"""
    )
    c.commit()
    return c


class Lane:
    """Semaforo + contador de cola por carril."""

    def __init__(self, name: str, conc: int) -> None:
        self.name = name
        self.sem = asyncio.Semaphore(conc)
        self.conc = conc
        self.running = 0
        self.queued = 0
        self.rejected = 0
        self.lock = asyncio.Lock()
        self.health_url = LANE_HEALTH.get(name)
        self.fallback = LANE_FALLBACK.get(name)
        self.down_until = 0.0      # epoch hasta el que se le considera caido
        self.health_fails = 0
        self.rerouted = 0          # peticiones desviadas al respaldo

    def snapshot(self) -> dict[str, Any]:
        return {
            "lane": self.name,
            "concurrency": self.conc,
            "running": self.running,
            "queued": self.queued,
            "max_queue": MAX_QUEUE,
            "rejected": self.rejected,
            "saturated": self.queued >= MAX_QUEUE,
            "health_url": self.health_url,
            "fallback": self.fallback,
            "down": time.time() < self.down_until,
            "rerouted": self.rerouted,
        }


LANES: dict[str, Lane] = {}


def lane(model: str) -> Lane:
    if model not in LANES:
        LANES[model] = Lane(model, int(CONCURRENCY.get(model, 2)))
    return LANES[model]


async def probe(ln: Lane) -> bool:
    """Sonda barata del nodo que sirve el carril (solo si tiene health_url)."""
    if not ln.health_url:
        return True
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as c:
            r = await c.get(ln.health_url)
        if r.status_code < 300:
            ln.health_fails = 0
            ln.down_until = 0.0
            return True
    except Exception:  # noqa: BLE001
        pass
    ln.health_fails += 1
    ln.down_until = time.time() + DOWN_TTL
    return False


async def resolve_model(model: str) -> str:
    """Si el nodo del carril no responde, devuelve el carril de respaldo."""
    ln = lane(model)
    if time.time() < ln.down_until:      # ya sabemos que esta caido: sin sonda
        if ln.fallback:
            ln.rerouted += 1
            return ln.fallback
        return model
    if await probe(ln):
        return model
    if ln.fallback:
        ln.rerouted += 1
        return ln.fallback
    return model


async def call_llm(model: str, messages: list[dict], tools: list[dict] | None = None,
                   json_mode: bool = False, max_tokens: int = 1500) -> dict:
    """Una llamada al gateway con admision de cola, sonda de salud y timeout."""
    model = await resolve_model(model)
    ln = lane(model)
    async with ln.lock:
        if ln.queued >= MAX_QUEUE:
            ln.rejected += 1
            raise HTTPException(429, f"carril {model} saturado ({ln.queued} en cola)", headers={"Retry-After": "30"})
        ln.queued += 1
    entered = False
    try:
        async with ln.sem:
            entered = True
            async with ln.lock:
                ln.queued -= 1
                ln.running += 1
            try:
                body: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                }
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                if tools:
                    body["tools"] = tools
                    body["tool_choice"] = "auto"
                async with httpx.AsyncClient(timeout=TASK_TIMEOUT) as c:
                    r = await c.post(f"{BASE}/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {KEY}"})
                if r.status_code >= 300:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                return r.json()
            except Exception:
                # el nodo no respondio: fuera del reparto DOWN_TTL segundos
                if ln.health_url:
                    ln.health_fails += 1
                    ln.down_until = time.time() + DOWN_TTL
                raise
            finally:
                async with ln.lock:
                    ln.running -= 1
    finally:
        if not entered:  # cancelado/fallido esperando turno: no dejar el contador inflado
            async with ln.lock:
                ln.queued = max(0, ln.queued - 1)



def text_of(resp: dict) -> tuple[str, int]:
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = (msg.get("content") or "").strip()
    usage = resp.get("usage") or {}
    if not content and msg.get("reasoning_content"):
        content = "[solo razonamiento, sin respuesta final]"
    return content, int(usage.get("total_tokens") or 0)


def extract_json(txt: str) -> Any:
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1]
        txt = txt[4:] if txt.startswith("json") else txt
    try:
        return json.loads(txt)
    except Exception:
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(txt[i:j + 1])
            except Exception:
                pass
    return None


SUBAGENT_SYS = (
    "Eres un subagente especializado. Recibes UNA tarea acotada y devuelves el resultado, "
    "no conversacion. Se preciso y breve. Si no puedes resolverla, dilo en una frase.\n"
    "Puedes usar la herramienta run_command para inspeccionar el entorno cuando aporte."
)

RUN_CMD_TOOL = [{
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "Ejecuta un comando de shell en el sandbox del orquestador (/data, sin host).",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
    },
}]


async def run_command(cmd: str) -> str:
    if not ALLOW_SHELL:
        return "run_command deshabilitado (ALLOW_SHELL=0)"
    try:
        p = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=DATA_DIR)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=60)
        return out.decode("utf-8", "replace")[:8000] or "(sin salida)"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


async def subagent(model: str, prompt: str, system: str = SUBAGENT_SYS, tools: bool = True,
                   max_tokens: int = 1200) -> tuple[str, int]:
    """Un subagente: contexto propio + hasta 3 vueltas de herramienta."""
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    tl = RUN_CMD_TOOL if (tools and ALLOW_SHELL) else None
    tokens = 0
    for _ in range(3):
        resp = await call_llm(model, msgs, tools=tl, max_tokens=max_tokens)
        tokens += int((resp.get("usage") or {}).get("total_tokens") or 0)
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip(), tokens
        msgs.append(msg)
        for c in calls:
            fn = c.get("function") or {}
            args = fn.get("arguments") or "{}"
            try:
                parsed = json.loads(args)
            except Exception:
                parsed = {"cmd": ""}
            out = await run_command(parsed.get("cmd", ""))
            msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
    content, t = text_of(await call_llm(model, msgs + [{"role": "user", "content": "Da ya la respuesta final."}], max_tokens=max_tokens))
    return content, tokens + t


class TaskIn(BaseModel):
    id: str | None = None
    prompt: str
    system: str | None = None
    model: str | None = None


class JobIn(BaseModel):
    model: str = "worker"
    tasks: list[TaskIn] = Field(min_length=1, max_length=500)
    max_concurrency: int | None = None
    planner: str | None = None  # si viene, se sintetiza el resultado


class FanoutIn(BaseModel):
    task: str
    items: list[str] = Field(min_length=1, max_length=200)
    worker: str = "worker"
    planner: str = "chat"
    max_concurrency: int = 4


async def _run_job(job_id: str, kind: str, model: str, planner: str | None,
                   tasks: list[dict], conc: int, payload: dict) -> None:
    c = db()
    sem = asyncio.Semaphore(max(1, conc))
    outputs: list[dict] = []

    async def one(t: dict) -> None:
        async with sem:
            t0 = time.time()
            try:
                if t.get("system"):
                    out, tok = await subagent(model, t["prompt"], system=t["system"])
                else:
                    out, tok = await subagent(model, t["prompt"])
                st, err = "done", None
            except Exception as e:  # noqa: BLE001
                out, tok, st, err = "", 0, "error", str(e)[:400]
            lat = round(time.time() - t0, 1)
            c.execute("UPDATE tasks SET status=?,output=?,error=?,latency=?,tokens=? WHERE job=? AND tid=?",
                      (st, out, err, lat, tok, job_id, t["id"]))
            c.execute("UPDATE jobs SET done=done+1, updated=? WHERE id=?", (time.time(), job_id))
            c.commit()
            outputs.append({"id": t["id"], "status": st, "output": out, "error": err, "latency": lat, "tokens": tok})

    await asyncio.gather(*(one(t) for t in tasks))

    if planner:
        try:
            done = [o for o in outputs if o["status"] == "done"]
            blob = "\n\n".join(f"### {o['id']}\n{o['output'][:2000]}" for o in done)[:24000]
            final, tok = await subagent(
                planner,
                f"Sintetiza estos resultados parciales en una respuesta unica, sin repetir la lista:\n\n{blob}",
                system="Eres el planificador. Fusionas resultados de subagentes en una respuesta coherente.",
                tools=False, max_tokens=1500)
            c.execute("UPDATE jobs SET payload=? WHERE id=?",
                      (json.dumps({**payload, "final": final}, ensure_ascii=False), job_id))
        except Exception as e:  # noqa: BLE001
            c.execute("UPDATE jobs SET payload=? WHERE id=?",
                      (json.dumps({**payload, "final_error": str(e)[:300]}, ensure_ascii=False), job_id))

    c.execute("UPDATE jobs SET status='done', updated=? WHERE id=?", (time.time(), job_id))
    c.commit()
    c.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db().close()
    for name, conc in CONCURRENCY.items():
        lane(name)
    yield


app = FastAPI(title="mesh-orchestrator", version="1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{BASE}/models", headers={"Authorization": f"Bearer {KEY}"})
        gw = r.status_code == 200
    except Exception:  # noqa: BLE001
        gw = False
    return {"ok": gw, "gateway": BASE, "allow_shell": ALLOW_SHELL, "lanes": [l.snapshot() for l in LANES.values()]}


@app.get("/v1/queue")
async def queue() -> dict:
    lanes = [l.snapshot() for l in LANES.values()]
    return {
        "lanes": lanes,
        "total_running": sum(l["running"] for l in lanes),
        "total_queued": sum(l["queued"] for l in lanes),
        "saturated": any(l["saturated"] for l in lanes),
    }


@app.post("/v1/jobs", status_code=202)
async def submit(job: JobIn) -> dict:
    jid = uuid.uuid4().hex[:12]
    if len(job.tasks) > MAX_QUEUE * 2:
        raise HTTPException(429, "demasiadas tareas en un solo job", headers={"Retry-After": "60"})
    tasks = [{"id": t.id or f"t{i}", "prompt": t.prompt, "system": t.system} for i, t in enumerate(job.tasks)]
    c = db()
    c.execute("INSERT INTO jobs VALUES(?,?,?,?,?,0,'running',?,?,?)",
              (jid, "tasks", job.model, job.planner, len(tasks), time.time(), time.time(), "{}"))
    c.executemany("INSERT INTO tasks(job,tid,prompt,status,output,error,latency,tokens) VALUES(?,?,?,'pending','',NULL,0,0)",
                  [(jid, t["id"], t["prompt"]) for t in tasks])
    c.commit()
    c.close()
    asyncio.create_task(_run_job(jid, "tasks", job.model, job.planner, tasks,
                                 job.max_concurrency or CONCURRENCY.get(job.model, 2), {}))
    return {"job_id": jid, "total": len(tasks), "model": job.model, "queue": [l.snapshot() for l in LANES.values()]}


@app.post("/v1/fanout", status_code=202)
async def fanout(f: FanoutIn) -> dict:
    jid = uuid.uuid4().hex[:12]
    tasks = [{"id": f"item{i}", "prompt": f"{f.task}\n\n--- ELEMENTO ---\n{it[:8000]}",
              "system": "Eres un subagente. Procesa SOLO el elemento que recibes y devuelve el resultado."}
             for i, it in enumerate(f.items)]
    c = db()
    c.execute("INSERT INTO jobs VALUES(?,?,?,?,?,0,'running',?,?,?)",
              (jid, "fanout", f.worker, f.planner, len(tasks), time.time(), time.time(),
               json.dumps({"task": f.task[:500]}, ensure_ascii=False)))
    c.executemany("INSERT INTO tasks(job,tid,prompt,status,output,error,latency,tokens) VALUES(?,?,?,'pending','',NULL,0,0)",
                  [(jid, t["id"], t["prompt"]) for t in tasks])
    c.commit()
    c.close()
    asyncio.create_task(_run_job(jid, "fanout", f.worker, f.planner, tasks,
                                 f.max_concurrency, {"task": f.task[:500]}))
    return {"job_id": jid, "total": len(tasks), "worker": f.worker, "planner": f.planner}


@app.get("/v1/jobs/{jid}")
async def job_status(jid: str) -> dict:
    c = db()
    row = c.execute("SELECT id,kind,model,planner,total,done,status,payload FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        raise HTTPException(404, "job desconocido")
    tasks = c.execute("SELECT tid,status,output,error,latency,tokens FROM tasks WHERE job=? ORDER BY tid", (jid,)).fetchall()
    c.close()
    return {
        "job_id": row[0], "kind": row[1], "model": row[2], "planner": row[3],
        "total": row[4], "done": row[5], "status": row[6],
        "payload": json.loads(row[7] or "{}"),
        "results": [{"id": t[0], "status": t[1], "output": t[2], "error": t[3], "latency": t[4], "tokens": t[5]} for t in tasks],
    }


@app.get("/v1/jobs")
async def jobs(limit: int = 20) -> dict:
    c = db()
    rows = c.execute("SELECT id,kind,model,total,done,status,created FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return {"jobs": [{"job_id": r[0], "kind": r[1], "model": r[2], "total": r[3], "done": r[4],
                      "status": r[5], "created": r[6]} for r in rows]}


@app.delete("/v1/jobs/{jid}")
async def job_delete(jid: str) -> dict:
    c = db()
    c.execute("DELETE FROM tasks WHERE job=?", (jid,))
    c.execute("DELETE FROM jobs WHERE id=?", (jid,))
    c.commit()
    c.close()
    return {"deleted": jid}
