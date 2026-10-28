#!/usr/bin/env python3
"""Moodle Course Studio — frontend de configuración y ejecución del pipeline
de creación de cursos (moodle-ia-mcp + IA local).

Servidor HTTP sin dependencias externas (stdlib). Sirve la UI estática y expone:
  POST /api/call          proxy JSON-RPC al servidor MCP (moodle-ia-mcp)
  POST /api/jobs          crea y lanza un plan de generación
  GET  /api/jobs          lista de ejecuciones
  GET  /api/jobs/<id>     estado detallado + log
  POST /api/jobs/<id>/cancel
"""
import json
import os
import re
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

MCP_URL = os.environ.get("MCP_URL", "http://moodle-ia-mcp:8097/mcp")
DATA_DIR = os.environ.get("DATA_DIR", "/data/jobs")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MOODLE_URL = os.environ.get("MOODLE_PUBLIC_URL", "https://moodle.neuropedialab.org")
CALL_TIMEOUT = int(os.environ.get("CALL_TIMEOUT", "1800"))

os.makedirs(DATA_DIR, exist_ok=True)


# --------------------------------------------------------------------------
# Cliente MCP (streamable HTTP, JSON-RPC 2.0)
# --------------------------------------------------------------------------
class McpError(Exception):
    pass


class McpClient:
    def __init__(self, url):
        self.url = url
        self.sid = None
        # RLock: serializa init + llamadas. El servidor streamable-http no
        # tolera bien dos initialize simultáneos desde el mismo cliente
        # (una de las dos peticiones se queda colgada para siempre).
        self._lock = threading.RLock()

    # -- transporte ------------------------------------------------------
    def _post(self, payload, timeout=CALL_TIMEOUT):
        data = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.sid:
            headers["mcp-session-id"] = self.sid
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return resp.status, dict(resp.headers), body
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            return e.code, dict(e.headers or {}), body

    @staticmethod
    def _parse(body):
        """El servidor puede responder JSON plano o SSE (data: {...})."""
        body = body.strip()
        if not body:
            return None
        if body.startswith("{") or body.startswith("["):
            return json.loads(body)
        msgs = []
        for line in body.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        msgs.append(json.loads(chunk))
                    except ValueError:
                        pass
        return msgs[-1] if msgs else None

    # -- sesión ----------------------------------------------------------
    def init(self):
        with self._lock:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "moodle-course-studio", "version": "1.0"},
                },
            }
            status, headers, body = self._post(payload, timeout=60)
            if status >= 400:
                raise McpError(f"initialize HTTP {status}: {body[:200]}")
            sid = None
            for k, v in headers.items():
                if k.lower() == "mcp-session-id":
                    sid = v
            self.sid = sid
            if sid:
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=60)

    # -- llamadas --------------------------------------------------------
    def call(self, name, arguments, timeout=CALL_TIMEOUT):
        with self._lock:
            if not self.sid:
                self.init()
            rid = int(time.time() * 1000) % 100000000
            payload = {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
            status, headers, body = self._post(payload, timeout=timeout)
            low = body[:300].lower()
            if status in (400, 404) or ("session" in low and "not found" in low):
                # sesión caducada: renegociar una sola vez
                self.sid = None
                self.init()
                status, headers, body = self._post(payload, timeout=timeout)
            if status >= 400:
                raise McpError(f"HTTP {status}: {body[:400]}")
            msg = self._parse(body)
            if not isinstance(msg, dict):
                raise McpError(f"respuesta no interpretable: {body[:300]}")
            if "error" in msg:
                raise McpError(f"MCP error: {msg['error']}")
            result = msg.get("result", {})
            if result.get("isError"):
                raise McpError(str(result)[:400])
            chunks = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
            text = "\n".join(chunks).strip()
            try:
                return json.loads(text)
            except ValueError:
                return {"ok": True, "text": text}


MCP = McpClient(MCP_URL)


def mcp_ping(timeout=6):
    """Sonda no bloqueante: si hay una generación en curso el lock está tomado."""
    if not MCP._lock.acquire(timeout=0.5):
        return False, "MCP ocupado (generación en curso)"
    try:
        MCP.call("moodle_list_courses", {}, timeout=timeout)
        return True, None
    except Exception as e:
        return False, str(e)[:200]
    finally:
        MCP._lock.release()


# --------------------------------------------------------------------------
# Motor de ejecución
# --------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()

# jobs con un hilo realmente vivo en este proceso; un job en estado "running"
# que no está aquí es un huérfano de un reinicio y se puede reanudar.
LIVE = set()
LIVE_LOCK = threading.Lock()
AUTO_RESUME = str(os.environ.get("AUTO_RESUME", "1")).lower() not in ("0", "false", "no", "off")


def is_live(job_id):
    with LIVE_LOCK:
        return job_id in LIVE


def _job_path(job_id):
    return os.path.join(DATA_DIR, f"{job_id}.json")


def save_job(job):
    tmp = _job_path(job["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _job_path(job["id"]))


def log(job, msg, level="info"):
    job["log"].append({"t": time.strftime("%H:%M:%S"), "msg": msg, "level": level})
    job["log"] = job["log"][-400:]
    save_job(job)


def step_start(job, key, label):
    st = {"key": key, "label": label, "status": "running", "started": time.time(),
          "ended": None, "error": None, "summary": None}
    job["steps"] = [s for s in job["steps"] if s["key"] != key] + [st]
    save_job(job)
    return st


def step_end(job, key, status, summary=None, error=None):
    for s in job["steps"]:
        if s["key"] == key:
            s["status"] = status
            s["ended"] = time.time()
            s["summary"] = summary
            s["error"] = error
    save_job(job)


def _cmid(res):
    """Extrae el coursemoduleid de las respuestas variables del plugin local."""
    if isinstance(res, list) and res:
        res = res[0]
    if isinstance(res, dict):
        for k in ("coursemoduleid", "cmid", "instanceid"):
            if res.get(k):
                return res[k]
    return None


def _moodle_link(modname, cmid):
    return f"{MOODLE_URL}/mod/{modname}/view.php?id={cmid}" if cmid else None


def pending_keys(job):
    """Pasos del plan que no constan como completados."""
    done = {s.get("key") for s in job.get("steps", []) if s.get("status") == "ok"}
    return [s.get("key") for s in job.get("plan", []) if s.get("key") not in done]


def run_plan(job, only_keys=None):
    jid = job["id"]
    with LIVE_LOCK:
        if jid in LIVE:
            return  # ya hay un hilo trabajando en este job
        LIVE.add(jid)
    try:
        _run_plan(job, only_keys)
    finally:
        with LIVE_LOCK:
            LIVE.discard(jid)


def _run_plan(job, only_keys=None):
    job["status"] = "running"
    job["started"] = time.time()
    log(job, f"Plan iniciado · modelo={job['options'].get('model_target')} · "
             f"dry_run={job['options'].get('dry_run')} · pasos={len(job['plan'])}")

    course_id = job.get("course_id")
    results = job.setdefault("results", {})
    try:
        for idx, step in enumerate(job["plan"]):
            if job.get("cancel"):
                job["status"] = "cancelled"
                log(job, "Ejecución cancelada por el usuario", "warn")
                save_job(job)
                return
            job["progress"] = idx
            if only_keys and step.get("key") not in only_keys:
                # Reintento selectivo: el resto de pasos ya está aplicado en Moodle.
                continue
            kind = step.get("kind")
            label = step.get("label") or kind
            st = step_start(job, step.get("key", f"s{idx}"), label)
            t0 = time.time()
            try:
                if kind == "create_course":
                    res = MCP.call("moodle_create_course", {
                        "fullname": step["fullname"],
                        "shortname": step["shortname"],
                        "category_id": int(step.get("category_id", 1)),
                        "summary": step.get("summary", ""),
                        "format": step.get("format", "topics"),
                    })
                    created = res.get("created") or []
                    if not res.get("ok") or not created:
                        raise McpError(f"no se creó el curso: {str(res)[:300]}")
                    course_id = created[0].get("id")
                    job["course_id"] = course_id
                    results["course"] = {"id": course_id, "fullname": created[0].get("fullname"),
                                         "shortname": created[0].get("shortname")}
                    step_end(job, st["key"], "ok", f"curso id={course_id}")
                    log(job, f"Curso creado: id={course_id} ({created[0].get('shortname')})")
                    continue

                if not course_id:
                    raise McpError("no hay course_id (falta el paso de creación de curso)")

                if kind == "section":
                    res = MCP.call("moodle_update_section", {
                        "course_id": course_id,
                        "section_num": int(step["section_num"]),
                        "name": step["name"],
                        "summary_html": step.get("summary_html", ""),
                    })
                    if not res.get("ok", True):
                        raise McpError(str(res.get("error"))[:300])
                    step_end(job, st["key"], "ok", step["name"])
                    log(job, f"Sección {step['section_num']} configurada: {step['name']}")

                elif kind == "page":
                    key = f"s{step['section_num']}_page"
                    if (results.get(key) or {}).get("published"):
                        step_end(job, st["key"], "ok", "ya publicada antes del reinicio")
                        log(job, f"Sección {step['section_num']}: lección ya publicada, no se republica")
                        continue
                    gen = MCP.call("moodle_generate_and_add_page", {
                        "course_id": course_id,
                        "section_num": int(step["section_num"]),
                        "page_title": step["title"],
                        "topic_description": step.get("topic", ""),
                        "clinical_focus": step.get("clinical_focus", ""),
                        "model_target": job["options"].get("model_target", "intel"),
                        "temperature": float(job["options"].get("temperature", 0.3)),
                        "max_tokens": int(job["options"].get("max_tokens", 3800)),
                        "dry_run": True,
                    })
                    if not gen.get("ok"):
                        raise McpError(str(gen.get("error"))[:300])
                    html = gen.get("html") or ""
                    results[key] = {"html": html, "length": len(html), "title": step["title"],
                                    "section_num": step["section_num"], "published": False}
                    log(job, f"Lección generada ({len(html)} chars): {step['title']}")
                    if step.get("publish") and html:
                        pub = MCP.call("moodle_add_page_content", {
                            "course_id": course_id,
                            "section_num": int(step["section_num"]),
                            "name": step["title"],
                            "content_html": html,
                        })
                        if not pub.get("ok"):
                            raise McpError(str(pub.get("error"))[:300])
                        cmid = _cmid(pub.get("result"))
                        results[key]["published"] = True
                        results[key]["coursemoduleid"] = cmid
                        results[key]["url"] = _moodle_link("page", cmid)
                        step_end(job, st["key"], "ok", f"publicada · {len(html)} chars")
                        log(job, f"Lección publicada en sección {step['section_num']}")
                    else:
                        step_end(job, st["key"], "ok", f"borrador · {len(html)} chars")
                        log(job, "Lección en borrador (pendiente de revisión)")

                elif kind == "quiz":
                    key = f"s{step['section_num']}_quiz"
                    if (results.get(key) or {}).get("published"):
                        step_end(job, st["key"], "ok", "ya importado antes del reinicio")
                        log(job, f"Sección {step['section_num']}: quiz ya importado, no se duplica")
                        continue
                    n = int(step.get("num_questions", 5))
                    gen = MCP.call("moodle_generate_and_import_quiz", {
                        "course_id": course_id,
                        "section_num": int(step["section_num"]),
                        "quiz_name": step["name"],
                        "topic": step.get("topic", ""),
                        "num_questions": n,
                        "difficulty": step.get("difficulty", "Intermedio / Clínico"),
                        "model_target": job["options"].get("model_target", "intel"),
                        "temperature": 0.2,
                        "attempts": int(step.get("attempts", 1)),
                        "dry_run": True,
                    })
                    if not gen.get("ok"):
                        # El modelo devolvió XML no parseable: conservamos el borrador
                        # para corregirlo en la pestaña de revisión y publicarlo a mano.
                        raw = gen.get("raw_xml") or ""
                        if raw.strip():
                            results[key] = {
                                "xml": raw,
                                "invalid": True,
                                "error": str(gen.get("error"))[:400],
                                "question_count": None,
                                "category": f"IA {step['name']}"[:60],
                                "quiz_name": step["name"],
                                "section_num": step["section_num"],
                                "attempts": int(step.get("attempts", 1)),
                                "published": False,
                            }
                            log(job, "XML inválido del modelo; borrador guardado para corrección manual", "warn")
                            raise McpError("XML inválido del modelo; borrador guardado para corrección manual")
                        raise McpError(str(gen.get("error"))[:300])
                    results[key] = {
                        "xml": gen.get("xml", ""),
                        "question_count": gen.get("question_count"),
                        "category": gen.get("category_name"),
                        "quiz_name": step["name"],
                        "section_num": step["section_num"],
                        "attempts": int(step.get("attempts", 1)),
                        "published": False,
                    }
                    log(job, f"Quiz generado ({gen.get('question_count')} preguntas): {step['name']}")
                    if step.get("publish"):
                        imp = MCP.call("moodle_generate_and_import_quiz", {
                            "course_id": course_id,
                            "section_num": int(step["section_num"]),
                            "quiz_name": step["name"],
                            "topic": step.get("topic", ""),
                            "num_questions": n,
                            "category_name": gen.get("category_name", ""),
                            "xml_content": gen.get("xml", ""),
                            "attempts": int(step.get("attempts", 1)),
                            "dry_run": False,
                        })
                        if not imp.get("ok"):
                            raise McpError(str(imp.get("error"))[:300])
                        results[key]["published"] = True
                        results[key]["quiz_instance_id"] = imp.get("quiz_instance_id")
                        results[key]["coursemoduleid"] = imp.get("coursemoduleid")
                        results[key]["url"] = _moodle_link("quiz", imp.get("coursemoduleid"))
                        step_end(job, st["key"], "ok", f"{gen.get('question_count')} preguntas")
                        log(job, f"Quiz importado · instancia={imp.get('quiz_instance_id')}")
                    else:
                        step_end(job, st["key"], "ok", f"borrador · {gen.get('question_count')} preguntas")

                elif kind == "quiz_settings":
                    res = MCP.call("moodle_update_quiz_settings", {
                        "quiz_id": int(step["quiz_id"]),
                        "attempts": int(step["attempts"]),
                    })
                    if not res.get("ok", True):
                        raise McpError(str(res.get("error"))[:300])
                    step_end(job, st["key"], "ok", f"intentos={step['attempts']}")
                    log(job, f"Intentos ajustados a {step['attempts']}")

                else:
                    raise McpError(f"tipo de paso desconocido: {kind}")

            except Exception as e:
                step_end(job, st["key"], "error", error=str(e)[:500])
                log(job, f"ERROR en «{label}»: {str(e)[:400]}", "error")
                job["status"] = "error"
                job["finished"] = time.time()
                save_job(job)
                return
            finally:
                job.setdefault("durations", {})[st["key"]] = round(time.time() - t0, 1)

        job["status"] = "done"
        job["finished"] = time.time()
        log(job, f"Plan completado en {round(job['finished'] - job['started'], 1)}s")
        save_job(job)
    except Exception as e:  # pragma: no cover
        job["status"] = "error"
        job["finished"] = time.time()
        log(job, f"Fallo global: {e}", "error")
        save_job(job)


def start_job(payload):
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    job = {
        "id": job_id,
        "created": time.time(),
        "status": "queued",
        "course_id": payload.get("course_id"),
        "options": payload.get("options", {}),
        "plan": payload.get("plan", []),
        "steps": [],
        "log": [],
        "results": {},
        "progress": 0,
        "cancel": False,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    save_job(job)
    threading.Thread(target=run_plan, args=(job,), daemon=True).start()
    return job


def retry_job(job, keys=None):
    """Relanza los pasos indicados (o los pendientes), conservando lo ya aplicado."""
    plan_keys = [s.get("key") for s in job.get("plan", [])]
    todo = [k for k in (keys or []) if k in plan_keys]
    if not todo:
        todo = pending_keys(job)
    if not todo:
        return None
    job["steps"] = [s for s in job.get("steps", []) if s.get("key") not in todo]
    job["cancel"] = False
    job["status"] = "queued"
    job["finished"] = None
    log(job, "Reanudando desde el último paso completado · pendientes: " + ", ".join(todo))
    save_job(job)
    threading.Thread(target=run_plan, args=(job, todo), daemon=True).start()
    return todo


def recover_jobs():
    """Rehidrata los jobs del disco y reanuda los que quedaron a medias."""
    resumed = []
    for job in load_jobs():
        jid = job.get("id")
        if not jid:
            continue
        with JOBS_LOCK:
            JOBS[jid] = job
        if job.get("status") not in ("running", "queued") or is_live(jid):
            continue
        fixed = 0
        for s in job.get("steps", []):
            if s.get("status") != "running":
                continue
            s["ended"] = s.get("ended") or time.time()
            if ((job.get("results") or {}).get(s.get("key")) or {}).get("published"):
                s["status"] = "ok"
                s["summary"] = s.get("summary") or "publicado antes del reinicio"
            else:
                s["status"] = "interrupted"
                s["error"] = "interrumpido por el reinicio del servicio"
            fixed += 1
        job["status"] = "interrupted"
        job["finished"] = None
        log(job, f"Reinicio del servicio detectado: {fixed} paso(s) sin terminar", "warn")
        save_job(job)
        pending = pending_keys(job)
        if not pending:
            job["status"] = "done"
            job["finished"] = time.time()
            save_job(job)
            continue
        if AUTO_RESUME:
            log(job, f"Reanudación automática de {len(pending)} paso(s)", "warn")
            retry_job(job, pending)
            resumed.append(jid)
        else:
            log(job, "AUTO_RESUME desactivado: reanuda a mano desde la pestaña de revisión", "warn")
            save_job(job)
    return resumed


KIND_LABEL = {"create_course": "curso", "section": "sección", "page": "lección", "quiz": "cuestionario"}


def current_step(job):
    """Último paso marcado como running; es el que está consumiendo el LLM."""
    for s in reversed(job.get("steps") or []):
        if s.get("status") == "running":
            return s
    return None


def activity_snapshot():
    """Generaciones en curso ahora mismo, se hayan lanzado desde la UI o por API."""
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    known = {j.get("id") for j in jobs}
    for job in load_jobs():
        if job.get("id") not in known:
            jobs.append(job)
    out = []
    now = time.time()
    for job in jobs:
        jid = job.get("id")
        if not jid:
            continue
        st = job.get("status")
        live = is_live(jid)
        if not live and st not in ("running", "queued", "interrupted"):
            continue
        cur = current_step(job) or {}
        plan = job.get("plan") or []
        plan_by_key = {p.get("key"): p for p in plan}
        meta = plan_by_key.get(cur.get("key")) or {}
        opts = job.get("options") or {}
        kind = KIND_LABEL.get(meta.get("kind") or "", meta.get("kind") or "")
        sec = meta.get("section_num")
        step_label = (f"sección {sec} · {kind}" if sec else kind).strip(" ·")
        entries = job.get("log") or []
        last = next((e for e in reversed(entries) if e.get("level") in ("ok", "warn", "error")),
                    entries[-1] if entries else {})
        out.append({
            "id": jid,
            "live": live,
            "status": "running" if live else st,
            "cancel": bool(job.get("cancel")),
            "title": opts.get("title") or f"curso #{job.get('course_id') or '—'}",
            "course_id": job.get("course_id"),
            "model": opts.get("model_target"),
            "step": cur.get("key"),
            "step_label": step_label,
            "steps_ok": sum(1 for s in job.get("steps") or [] if s.get("status") == "ok"),
            "steps_total": len(plan),
            "pending": len(pending_keys(job)) if plan else 0,
            "elapsed": round(now - (job.get("started") or job.get("created") or now), 1),
            "age": round(now - (job.get("created") or now), 1),
            "step_elapsed": round(now - cur["started"], 1) if cur.get("started") else None,
            "last_msg": (last.get("msg") or "")[:180],
            "last_t": last.get("t"),
        })
    out.sort(key=lambda x: (0 if x["live"] else 1, -x["elapsed"]))
    return out


def job_summary(job):
    return {
        "id": job["id"],
        "created": job.get("created"),
        "status": job.get("status"),
        "live": is_live(job["id"]),
        "pending": len(pending_keys(job)),
        "course_id": job.get("course_id"),
        "steps_total": len(job.get("plan", [])),
        "steps_ok": sum(1 for s in job.get("steps", []) if s["status"] == "ok"),
        "steps_error": sum(1 for s in job.get("steps", []) if s["status"] == "error"),
        "duration": round((job.get("finished") or time.time()) - (job.get("started") or job["created"]), 1),
        "title": (job.get("options") or {}).get("title", ""),
    }


def load_jobs():
    out = []
    try:
        for name in os.listdir(DATA_DIR):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
                    job = json.load(f)
                out.append(job)
            except Exception:
                continue
    except FileNotFoundError:
        pass
    out.sort(key=lambda j: j.get("created", 0), reverse=True)
    return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "MoodleCourseStudio/1.0"

    def log_message(self, format, *args):
        pass

    # -- helpers ---------------------------------------------------------
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _static(self, path):
        rel = path.split("?")[0].lstrip("/")
        if not rel:
            rel = "index.html"
        target = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        with open(target, "rb") as f:
            data = f.read()
        ext = os.path.splitext(target)[1].lower()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    # -- rutas -----------------------------------------------------------
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/health":
            # Sonda rápida: nunca se bloquea detrás de una generación en curso.
            deep = "deep=1" in self.path
            if MCP._lock.acquire(timeout=0.25):
                MCP._lock.release()
                mcp_ok, err = mcp_ping(20 if deep else 5)
            else:
                mcp_ok, err = True, "generación en curso (sonda omitida)"
            with LIVE_LOCK:
                live = sorted(LIVE)
            self._json(200, {"ok": True, "mcp": mcp_ok, "mcp_url": MCP_URL, "error": err,
                             "moodle_url": MOODLE_URL, "live_jobs": live})
        elif p == "/api/activity":
            # Monitor de generación activa: incluye jobs lanzados por API.
            err = None
            try:
                jobs = activity_snapshot()
            except Exception as e:
                jobs, err = [], str(e)[:200]
            self._json(200, {"ok": True, "jobs": jobs, "error": err,
                             "polled": round(time.time(), 1)})
        elif p == "/api/jobs":
            jobs = load_jobs()
            self._json(200, {"ok": True, "jobs": [job_summary(j) for j in jobs[:40]]})
        elif p.startswith("/api/jobs/"):
            jid = p.split("/")[3]
            job = JOBS.get(jid)
            if not job:
                fp = _job_path(jid)
                if os.path.isfile(fp):
                    with open(fp, encoding="utf-8") as f:
                        job = json.load(f)
                    JOBS[jid] = job
            if not job:
                self._json(404, {"ok": False, "error": "job no encontrado"})
            else:
                self._json(200, {"ok": True, "job": job})
        else:
            self._static(self.path)

    def do_POST(self):
        p = self.path.split("?")[0]
        body = self._body()
        if p == "/api/call":
            name = body.get("name")
            if not re.fullmatch(r"[a-z0-9_]{3,60}", name or ""):
                self._json(400, {"ok": False, "error": "nombre de herramienta inválido"})
                return
            try:
                res = MCP.call(name, body.get("arguments") or {},
                               timeout=int(body.get("timeout") or CALL_TIMEOUT))
                self._json(200, {"ok": True, "result": res})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)[:800]})
        elif p == "/api/jobs":
            plan = body.get("plan") or []
            if not plan:
                self._json(400, {"ok": False, "error": "plan vacío"})
                return
            job = start_job(body)
            self._json(200, {"ok": True, "job": job_summary(job), "id": job["id"]})
        elif re.fullmatch(r"/api/jobs/[A-Za-z0-9\-]+/results", p):
            jid = p.split("/")[3]
            job = JOBS.get(jid)
            if not job:
                self._json(404, {"ok": False, "error": "job no encontrado"})
                return
            key = body.get("key")
            if not key:
                self._json(400, {"ok": False, "error": "falta key"})
                return
            item = job.setdefault("results", {}).setdefault(key, {})
            item.update(body.get("patch") or {})
            save_job(job)
            self._json(200, {"ok": True, "item": item})
        elif re.fullmatch(r"/api/jobs/[A-Za-z0-9\-]+/retry", p):
            jid = p.split("/")[3]
            job = JOBS.get(jid)
            if not job:
                self._json(404, {"ok": False, "error": "job no encontrado"})
                return
            if is_live(jid):
                self._json(409, {"ok": False, "error": "la ejecución sigue en curso"})
                return
            keys = body.get("keys") or []
            done = retry_job(job, keys)
            if not done:
                self._json(400, {"ok": False, "error": "no hay pasos pendientes que ejecutar"})
                return
            self._json(200, {"ok": True, "keys": done, "job": job_summary(job)})
        elif re.fullmatch(r"/api/jobs/[A-Za-z0-9\-]+/cancel", p):
            jid = p.split("/")[3]
            job = JOBS.get(jid)
            if not job:
                self._json(404, {"ok": False, "error": "job no encontrado"})
                return
            job["cancel"] = True
            log(job, "Cancelación solicitada (se aplica al terminar el paso en curso)", "warn")
            self._json(200, {"ok": True})
        else:
            self._json(404, {"ok": False, "error": "ruta no encontrada"})


def main():
    port = int(os.environ.get("PORT", "8098"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[studio] escuchando en :{port} · MCP={MCP_URL} · datos={DATA_DIR}", flush=True)
    try:
        resumed = recover_jobs()
        if resumed:
            print(f"[studio] reanudados {len(resumed)} job(s): {', '.join(resumed)}", flush=True)
    except Exception as e:
        print(f"[studio] recovery falló: {e}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
