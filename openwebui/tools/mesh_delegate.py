"""
title: Malla: delegar a hardware secundario
author: hermes
version: 1.0
required_open_webui_version: 0.13.0
description: Delega subtareas al hardware que mejor las resuelve (iGPU Intel/OVMS, Hailo-8L para OCR y CLIP, NPU Intel para embeddings, subagentes en paralelo).
"""

import asyncio
import json
import os

import httpx
from pydantic import BaseModel, Field

CAPABILITIES = {
    "openvino": "Qwen3-30B-A3B en la iGPU Intel (OVMS). Texto barato y paralelo: clasificar, extraer campos, resumir lotes, segunda opinion, subagente de un paso.",
    "ocr": "Hailo-8L: extrae el texto de una imagen (URL http(s) o data URI base64).",
    "embed_text": "Qwen3-Embedding 0.6B en la NPU Intel (1024d): similitud semantica entre textos.",
    "embed_image": "CLIP ResNet-50x4 en el Hailo-8L (640d): similitud entre imagenes.",
    "fanout": "Subagentes en paralelo en la iGPU: cada elemento de attachments se procesa por separado y un planificador sintetiza.",
    "status": "Estado de las colas de la malla (que carril esta ocupado o caido).",
}


class Tools:
    class Valves(BaseModel):
        base_url: str = Field(
            default="http://litellm:4000/v1",
            description="Puerta OpenAI-compatible de la malla (LiteLLM).",
        )
        api_key: str = Field(default="", description="Clave de la malla. Vacio = la del entorno de Open WebUI.")
        orchestrator_url: str = Field(
            default="http://mesh-orchestrator:4010",
            description="mesh-orchestrator (cola + subagentes).",
        )
        openvino_model: str = Field(default="worker", description="Alias de la iGPU para texto.")
        ocr_model: str = Field(default="ocr", description="Alias del OCR Hailo.")
        embed_text_model: str = Field(default="embed-npu", description="Alias de embeddings de texto (NPU).")
        embed_image_model: str = Field(default="embed-vision", description="Alias de embeddings de imagen (Hailo).")
        planner_model: str = Field(default="arc", description="Alias que sintetiza el fanout.")
        timeout: int = Field(default=600, description="Segundos maximos por delegacion.")
        max_chars: int = Field(default=6000, description="Recorte de cada respuesta devuelta al modelo.")

    def __init__(self):
        self.valves = self.Valves(
            api_key=os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEYS", ""),
        )
        self.citation = False

    # ---------- helpers ----------

    def _key(self) -> str:
        """La clave nunca se guarda en la tool: se lee del entorno del contenedor.

        Open WebUI aplica los valves guardados en la BD DESPUES de __init__, asi que
        un valve api_key vacio pisaba la clave del entorno y todas las delegaciones
        fallaban con 401/'No connected db'.
        """
        return (
            self.valves.api_key
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("OPENAI_API_KEYS", "")
        )

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json"}

    async def _chat(self, model: str, prompt: str, max_tokens: int = 1536, system: str | None = None) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        async with httpx.AsyncClient(timeout=self.valves.timeout) as c:
            r = await c.post(
                f"{self.valves.base_url}/chat/completions",
                headers=self._headers(),
                json={"model": model, "messages": messages, "max_tokens": max_tokens},
            )
        if r.status_code >= 400:
            return f"ERROR {r.status_code}: {r.text[:300]}"
        data = r.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or "(vacio)"

    async def _embeddings(self, model: str, inputs: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self.valves.timeout) as c:
            r = await c.post(
                f"{self.valves.base_url}/embeddings",
                headers=self._headers(),
                json={"model": model, "input": inputs},
            )
        if r.status_code >= 400:
            raise RuntimeError(f"ERROR {r.status_code}: {r.text[:300]}")
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]

    @staticmethod
    def _cos(a: list[float], b: list[float]) -> float:
        num = sum(x * y for x, y in zip(a, b))
        da = sum(x * x for x in a) ** 0.5 or 1.0
        db = sum(y * y for y in b) ** 0.5 or 1.0
        return num / (da * db)

    async def _notify(self, emitter, description: str, status: str = "in_progress") -> None:
        if emitter:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": status == "complete"}})
            except Exception:
                pass

    # ---------- herramienta ----------

    async def delegate(
        self,
        capability: str,
        task: str = "",
        attachments: list[str] | None = None,
        __event_emitter__=None,
    ) -> str:
        """Delega una subtarea al hardware secundario que mejor la resuelve.

        Usala SIEMPRE que la tarea encaje en una capacidad de la malla en vez de
        hacerla tu mismo: libera la V100 y usa el motor especializado.

        :param capability: Una de: "openvino" (texto en la iGPU: clasificar, resumir lotes, extraer campos, segunda opinion, subagente de un paso), "ocr" (texto de una imagen con el Hailo-8L), "embed_text" (similitud semantica entre textos con la NPU), "embed_image" (similitud entre imagenes con el Hailo-8L), "fanout" (N subagentes en paralelo + sintesis), "status" (estado de las colas).
        :param task: La instruccion o pregunta. En embed_text/embed_image es la consulta de referencia. En fanout es la instruccion comun a todos los elementos.
        :param attachments: URLs http(s) o data URI base64. En "ocr" y "embed_image": las imagenes. En "embed_text": textos a comparar con task. En "fanout": un elemento por subagente (max 200).
        :return: Texto con el resultado real del carril, o "ERROR ..." si el carril no responde. No inventes el resultado si devuelve ERROR.
        """
        cap = (capability or "").strip().lower()
        attachments = attachments or []
        task = (task or "").strip()

        if cap not in CAPABILITIES:
            return "ERROR: capability desconocida. Usa una de: " + ", ".join(sorted(CAPABILITIES))

        try:
            if cap == "status":
                async with httpx.AsyncClient(timeout=20) as c:
                    r = await c.get(f"{self.valves.orchestrator_url}/v1/queue")
                lanes = r.json().get("lanes", [])
                lines = [
                    f"{l['lane']}: running={l['running']} queued={l['queued']} conc={l['concurrency']}"
                    + (" DOWN" if l.get("down") else "")
                    for l in lanes
                ]
                return "Colas de la malla:\n" + "\n".join(lines)

            if cap == "openvino":
                if not task:
                    return "ERROR: 'openvino' necesita 'task' con la instruccion."
                await self._notify(__event_emitter__, "Delegando a la iGPU Intel (Qwen3-30B)...")
                out = await self._chat(
                    self.valves.openvino_model,
                    task,
                    system="Eres un subagente de la malla. Responde solo a lo que se te pide, sin preambulos.",
                )
                await self._notify(__event_emitter__, "iGPU completada", "complete")
                return f"[openvino/{self.valves.openvino_model}]\n{out[: self.valves.max_chars]}"

            if cap == "ocr":
                if not attachments:
                    return "ERROR: 'ocr' necesita 'attachments' con la URL http(s) o data URI de la imagen."
                await self._notify(__event_emitter__, "OCR en el Hailo-8L...")
                content = [{"type": "text", "text": task or "Extrae todo el texto de la imagen, respetando lineas y cifras."}]
                content += [{"type": "image_url", "image_url": {"url": u}} for u in attachments[:4]]
                async with httpx.AsyncClient(timeout=self.valves.timeout) as c:
                    r = await c.post(
                        f"{self.valves.base_url}/chat/completions",
                        headers=self._headers(),
                        json={"model": self.valves.ocr_model, "messages": [{"role": "user", "content": content}]},
                    )
                if r.status_code >= 400:
                    return f"ERROR {r.status_code} en OCR: {r.text[:300]}"
                out = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
                await self._notify(__event_emitter__, "OCR completado", "complete")
                return f"[ocr/{self.valves.ocr_model}]\n{out[: self.valves.max_chars]}"

            if cap == "embed_text":
                texts = ([task] if task else []) + attachments
                if len(texts) < 2:
                    return "ERROR: 'embed_text' necesita 'task' (consulta) y al menos un texto en 'attachments'."
                vecs = await self._embeddings(self.valves.embed_text_model, texts)
                base, rest = vecs[0], vecs[1:]
                rows = sorted(
                    ((self._cos(base, v), i) for i, v in enumerate(rest)), reverse=True
                )
                lines = [f"dim={len(base)} consulta={texts[0][:60]!r}"]
                for score, i in rows:
                    lines.append(f"  cos={score:.4f}  {texts[i + 1][:120]!r}")
                return "[embed_text/" + self.valves.embed_text_model + "]\n" + "\n".join(lines)

            if cap == "embed_image":
                if len(attachments) < 1:
                    return "ERROR: 'embed_image' necesita imagenes en 'attachments'."
                inputs = ([task] if task else []) + attachments
                if len(inputs) < 2:
                    return "ERROR: 'embed_image' necesita al menos dos imagenes (o 'task' con una imagen de referencia)."
                vecs = await self._embeddings(self.valves.embed_image_model, inputs)
                base, rest = vecs[0], vecs[1:]
                rows = sorted(((self._cos(base, v), i) for i, v in enumerate(rest)), reverse=True)
                lines = [f"dim={len(base)} referencia={inputs[0][:60]!r}"]
                for score, i in rows:
                    lines.append(f"  cos={score:.4f}  img#{i + 1}")
                return "[embed_image/" + self.valves.embed_image_model + "]\n" + "\n".join(lines)

            if cap == "fanout":
                if not task or len(attachments) < 2:
                    return "ERROR: 'fanout' necesita 'task' (instruccion comun) y >=2 elementos en 'attachments'."
                await self._notify(__event_emitter__, f"Lanzando {len(attachments)} subagentes en la iGPU...")
                async with httpx.AsyncClient(timeout=60) as c:
                    r = await c.post(
                        f"{self.valves.orchestrator_url}/v1/fanout",
                        json={
                            "task": task,
                            "items": attachments[:200],
                            "worker": self.valves.openvino_model,
                            "planner": self.valves.planner_model,
                            "max_concurrency": 4,
                        },
                    )
                if r.status_code >= 400:
                    return f"ERROR {r.status_code} al encolar el fanout: {r.text[:300]}"
                jid = r.json()["job_id"]
                deadline = asyncio.get_event_loop().time() + self.valves.timeout
                data = {}
                async with httpx.AsyncClient(timeout=30) as c:
                    while asyncio.get_event_loop().time() < deadline:
                        await asyncio.sleep(3)
                        jr = await c.get(f"{self.valves.orchestrator_url}/v1/jobs/{jid}")
                        if jr.status_code >= 400:
                            return f"ERROR {jr.status_code} consultando el job {jid}"
                        data = jr.json()
                        if data.get("status") == "done":
                            break
                await self._notify(__event_emitter__, "Fanout completado", "complete")
                out = [f"[fanout job={jid} estado={data.get('status')}]"]
                for o in data.get("results", [])[:20]:
                    body = (o.get("output") or o.get("error") or "")[:800]
                    out.append(f"- {o.get('id')}: {body}")
                final = (data.get("payload") or {}).get("final")
                if final:
                    out.append("\nSINTESIS DEL PLANIFICADOR:\n" + final[: self.valves.max_chars])
                return "\n".join(out)

            return "ERROR: capacidad no implementada."

        except Exception as e:  # noqa: BLE001
            return f"ERROR inesperado en '{cap}': {type(e).__name__}: {str(e)[:300]}"
