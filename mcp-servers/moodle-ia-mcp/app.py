import os
import sys
import json
import re
import asyncio
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
import httpx
from typing import List, Dict, Any, Optional
from fastmcp import FastMCP

mcp = FastMCP("Moodle-IA Content Orchestrator & LLM Engine")

# ==========================================
# CONFIGURACIÓN DE ENTORNO Y ENDPOINTS
# ==========================================
MOODLE_URL = os.environ.get("MOODLE_URL", "https://moodle.neuropedialab.org").rstrip("/")
MOODLE_TOKEN = os.environ.get("MOODLE_TOKEN", "734d7e02718f02570a70bdbc87698c78")

# Endpoints de inferencia local
INTEL_OPENVINO_URL = os.environ.get("INTEL_OPENVINO_URL", "http://lms-optimum-intel:8000/v1")
INTEL_FALLBACK_URL = os.environ.get("INTEL_FALLBACK_URL", "http://192.168.0.100:8006/v1")

DEFAULT_MODEL_INTEL = os.environ.get("DEFAULT_MODEL_INTEL", "Qwen3-30B-A3B-Instruct-2507-int4-ov")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "900"))

# ==========================================
# CLIENTE LLM LOCAL (INTEL ARC)
# ==========================================
async def query_llm(
    prompt: str,
    system_instruction: str = "Eres un asistente experto en educación médica, neuropediatría y diseño pedagógico para plataformas Moodle.",
    model_target: str = "auto",
    temperature: float = 0.3,
    max_tokens: int = 3500
) -> str:
    """Envía peticiones de inferencia a los motores locales (Intel Arc)."""
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": prompt}
    ]
    
    endpoints = []
    if model_target.lower() == "intel":
        endpoints = [
            (INTEL_OPENVINO_URL, DEFAULT_MODEL_INTEL, "Intel Arc (Direct)"),
            (INTEL_FALLBACK_URL, DEFAULT_MODEL_INTEL, "Intel Arc (Fallback LAN)")
        ]
    else:  # auto / cualquier otro valor: Intel directo y su fallback LAN
        endpoints = [
            (INTEL_OPENVINO_URL, DEFAULT_MODEL_INTEL, "Intel Arc (Direct)"),
            (INTEL_FALLBACK_URL, DEFAULT_MODEL_INTEL, "Intel Arc (Fallback LAN)")
        ]

    last_err = None
    # Reintentos con espera: si el motor local está reiniciando (OVMS tarda
    # ~60-75 s en recargar el modelo, o el contenedor acaba de recrearse) un
    # único barrido de endpoints aborta la generación entera. Reintentamos el
    # barrido completo en vez de dar el paso por fallido.
    attempts = max(1, int(os.environ.get("LLM_RETRIES", "3")))
    wait = float(os.environ.get("LLM_RETRY_WAIT", "45"))
    for attempt in range(1, attempts + 1):
        for url, model, name in endpoints:
            try:
                async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
                    res = await client.post(
                        f"{url}/chat/completions",
                        json={
                            "model": model,
                            "messages": messages,
                            "temperature": temperature,
                            "max_tokens": max_tokens
                        }
                    )
                    if res.status_code == 200:
                        data = res.json()
                        content = data["choices"][0]["message"]["content"]
                        # Limpiar tags de razonamiento si existen
                        content = re.sub(r" thinking.*?</think>", "", content, flags=re.DOTALL).strip()
                        return content
                    else:
                        last_err = f"Endpoint {name} returned status {res.status_code}: {res.text}"
            except Exception as e:
                last_err = f"Error connecting to {name} ({url}): {e}"
                continue

        if attempt < attempts:
            print(f"[llm] barrido {attempt}/{attempts} sin éxito ({last_err}); reintento en {wait:.0f}s", flush=True)
            await asyncio.sleep(wait)

    raise RuntimeError(f"Fallo en todos los endpoints LLM locales tras {attempts} intentos. Último error: {last_err}")


# ==========================================
# CLIENTE WEBSERVICES MOODLE REST
# ==========================================
async def call_moodle_ws(wsfunction: str, params: Dict[str, Any]) -> Any:
    """Llamada base a los servicios web REST de Moodle."""
    endpoint = f"{MOODLE_URL}/webservice/rest/server.php"
    payload = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json"
    }
    payload.update(params)

    async with httpx.AsyncClient(timeout=60.0) as client:
        res = await client.post(endpoint, data=payload)
        if res.status_code != 200:
            raise RuntimeError(f"Moodle HTTP error {res.status_code}: {res.text}")
        try:
            data = res.json()
        except Exception:
            raise RuntimeError(f"Invalid JSON response from Moodle: {res.text}")

        if isinstance(data, dict) and (data.get("exception") or data.get("errorcode")):
            raise RuntimeError(f"Moodle WebService Error [{data.get('errorcode')}]: {data.get('message')}")
        return data


# ==========================================
# HERRAMIENTAS MCP: GESTIÓN DE CURSOS MOODLE
# ==========================================

@mcp.tool()
async def moodle_list_courses() -> Dict[str, Any]:
    """Lista todos los cursos existentes en el campus Moodle."""
    try:
        courses = await call_moodle_ws("core_course_get_courses", {})
        simplified = []
        for c in courses:
            simplified.append({
                "id": c.get("id"),
                "fullname": c.get("fullname"),
                "shortname": c.get("shortname"),
                "categoryid": c.get("categoryid"),
                "summary": (c.get("summary") or "")[:150]
            })
        return {"ok": True, "count": len(simplified), "courses": simplified}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def moodle_list_categories() -> Dict[str, Any]:
    """Lista las categorías de cursos del campus (para elegir destino al crear un curso)."""
    try:
        cats = await call_moodle_ws("core_course_get_categories", {})
        out = [{
            "id": c.get("id"),
            "name": c.get("name"),
            "parent": c.get("parent"),
            "depth": c.get("depth"),
            "coursecount": c.get("coursecount"),
        } for c in cats]
        return {"ok": True, "count": len(out), "categories": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def moodle_get_course_structure(course_id: int) -> Dict[str, Any]:
    """Obtiene las secciones, módulos y páginas de un curso específico de Moodle."""
    try:
        data = await call_moodle_ws("core_course_get_contents", {"courseid": course_id})
        sections = []
        for s in data:
            modules = []
            for m in s.get("modules", []):
                modules.append({
                    "id": m.get("id"),
                    "name": m.get("name"),
                    "modname": m.get("modname"),
                    "url": m.get("url")
                })
            sections.append({
                "section_number": s.get("section"),
                "name": s.get("name"),
                "summary": s.get("summary"),
                "modules_count": len(modules),
                "modules": modules
            })
        return {"ok": True, "course_id": course_id, "sections": sections}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def moodle_create_course(
    fullname: str,
    shortname: str,
    category_id: int = 1,
    summary: str = "",
    format: str = "topics"
) -> Dict[str, Any]:
    """Crea un nuevo curso en Moodle."""
    try:
        params = {
            "courses[0][fullname]": fullname,
            "courses[0][shortname]": shortname,
            "courses[0][categoryid]": category_id,
            "courses[0][format]": format,
            "courses[0][summary]": summary
        }
        res = await call_moodle_ws("core_course_create_courses", params)
        return {"ok": True, "created": res}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def moodle_update_section(
    course_id: int,
    section_num: int,
    name: str,
    summary_html: str
) -> Dict[str, Any]:
    """Actualiza el título y la descripción/resumen HTML de una sección en un curso usando local_ia_moodle_editor."""
    try:
        params = {
            "courseid": course_id,
            "sectionnum": section_num,
            "name": name,
            "summary": summary_html
        }
        res = await call_moodle_ws("local_ia_moodle_editor_update_section", params)
        return {"ok": True, "result": res}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def moodle_add_page_content(
    course_id: int,
    section_num: int,
    name: str,
    content_html: str,
    intro_html: str = ""
) -> Dict[str, Any]:
    """Publica directamente una página con contenido HTML en una sección de un curso de Moodle."""
    try:
        params = {
            "courseid": course_id,
            "sectionnum": section_num,
            "name": name,
            "intro": intro_html,
            "content": content_html
        }
        res = await call_moodle_ws("local_ia_moodle_editor_add_page", params)
        return {"ok": True, "result": res}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ==========================================
# HERRAMIENTAS MCP: GENERACIÓN CON IA LOCAL
# ==========================================

@mcp.tool()
async def moodle_generate_syllabus(
    topic: str,
    target_audience: str = "Profesionales sanitarios y neuropediatras",
    num_modules: int = 4,
    learning_outcomes: str = "",
    model_target: str = "auto",
    temperature: float = 0.2,
    questions_per_module: int = 5,
    language: str = "español"
) -> Dict[str, Any]:
    """Genera una propuesta de temario estructurado para un curso Moodle con títulos, resúmenes y lecciones recomendadas."""
    prompt = f"""Diseña la estructura completa para un curso formativo en Moodle.
Tema general: {topic}
Público objetivo: {target_audience}
Número de módulos/secciones: {num_modules}
Resultados de aprendizaje esperados: {learning_outcomes or 'Formación clínica integral, actualización basada en evidencia y aplicación práctica'}
Idioma de salida: {language}

Devuelve EXCLUSIVAMENTE un objeto JSON válido con la siguiente estructura (sin bloques markdown ni explicaciones adicionales):
{{
  "course_title": "Nombre completo del curso",
  "course_shortname": "Código corto",
  "course_summary": "Resumen general del curso (HTML limpio)",
  "sections": [
    {{
      "section_num": 1,
      "name": "Módulo 1: ...",
      "summary": "Resumen introductorio del módulo en HTML",
      "lessons": [
        {{
          "title": "Lección 1.1: ...",
          "learning_goals": "...",
          "key_topics": ["...", "..."]
        }}
      ],
      "quiz_title": "Evaluación Módulo 1"
    }}
  ]
}}
Cada módulo debe tener exactamente una lección principal y su cuestionario de {questions_per_module} preguntas.
"""
    raw_json = None
    try:
        raw_json = await query_llm(
            prompt,
            system_instruction="Eres un diseñador curricular médico de alto nivel. Responde siempre con JSON estricto.",
            model_target=model_target,
            temperature=temperature
        )
        # Extraer JSON limpio si viene rodeado de ```json ... ```
        clean_json = re.sub(r"^```(?:json)?\n?", "", raw_json.strip())
        clean_json = re.sub(r"\n?```$", "", clean_json.strip())
        syllabus = json.loads(clean_json)
        return {"ok": True, "syllabus": syllabus}
    except Exception as e:
        return {"ok": False, "raw_response": raw_json, "error": str(e)}


@mcp.tool()
async def moodle_generate_and_add_page(
    course_id: int,
    section_num: int,
    page_title: str,
    topic_description: str,
    clinical_focus: str = "",
    model_target: str = "auto",
    temperature: float = 0.3,
    max_tokens: int = 3800,
    language: str = "español",
    content_html: str = "",
    dry_run: bool = False
) -> Dict[str, Any]:
    """Genera con LLM local una lección en formato HTML profesional (estilo Bootstrap/Moodle) y la publica en el curso.

    - `content_html`: si se envía, no se llama al LLM (publicación de un borrador ya generado).
    - `dry_run`: devuelve el HTML sin escribir nada en Moodle.
    """
    prompt = f"""Elabora el contenido completo y exhaustivo para una página de lección en Moodle.
Título de la lección: {page_title}
Objetivo y temas a cubrir: {topic_description}
Enfoque clínico/práctico: {clinical_focus or 'Evidencia actualizada, tablas comparativas, perlas clínicas y algoritmo de decisión'}
Idioma de salida: {language}

Estructura obligatoria en HTML limpio (compatible con Moodle / Bootstrap 4/5):
- <h2> y <h3> para estructurar secciones claras.
- Bloques de alerta visuales como `<div class="alert alert-info"><strong>💡 Perla Clínica:</strong> ...</div>` y `<div class="alert alert-warning"><strong>⚠️ Precaución / Red Flags:</strong> ...</div>`.
- Tablas comparativas o de dosificación/criterios cuando aplique (`<table class="table table-bordered table-striped">`).
- Listas ordenadas/desordenadas claras.
- Sección final: `<div class="card p-3 bg-light"><h4>📌 Puntos Clave para Llevar a la Práctica</h4><ul>...</ul></div>`.

IMPORTANTE: Devuelve ÚNICAMENTE el código HTML dentro del cuerpo de la lección (sin etiquetas <html>, <head> o <body>).
"""
    try:
        if content_html.strip():
            html_content = content_html.strip()
            source = "provided"
        else:
            html_content = await query_llm(
                prompt,
                system_instruction="Eres un profesor especialista en educación médica y redacción clínica estructurada.",
                model_target=model_target,
                temperature=temperature,
                max_tokens=max_tokens
            )
            # Limpieza básica de bloques markdown si el modelo los incluyó
            html_content = re.sub(r"^```(?:html)?\n?", "", html_content.strip())
            html_content = re.sub(r"\n?```$", "", html_content.strip())
            source = "llm"

        result: Dict[str, Any] = {
            "ok": True,
            "dry_run": bool(dry_run),
            "page_title": page_title,
            "section_num": section_num,
            "source": source,
            "html": html_content,
            "html_length": len(html_content),
        }

        if dry_run:
            return result

        moodle_res = await moodle_add_page_content(
            course_id=course_id,
            section_num=section_num,
            name=page_title,
            content_html=html_content,
            intro_html=f"<p>Lección formativa: {page_title}</p>"
        )
        result["ok"] = moodle_res.get("ok", False)
        result["moodle_result"] = moodle_res.get("result")
        result["error"] = moodle_res.get("error")
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ==========================================
# VALIDACIÓN / REPARACIÓN DE XML DE PREGUNTAS
# ==========================================
_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


def _parse_questions(xml_text: str):
    """Devuelve la lista de preguntas del XML, o lanza excepción si no es parseable."""
    root = ET.fromstring(xml_text)
    return [q for q in root.findall("question") if (q.get("type") or "") != "category"]


# CDATA / comentarios / processing-instructions / declaraciones, o una etiqueta normal
_TAG_RE = re.compile(
    r"<!\[CDATA\[.*?\]\]>|<!--.*?-->|<\?.*?\?>|<![^>]*>"
    r"|<(/?)([A-Za-z_][\w.:-]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>",
    re.DOTALL,
)


def _balance_tags(xml_text: str) -> str:
    """Reequilibra el anidamiento de etiquetas de una salida imperfecta del LLM.

    Corrige los dos defectos que se ven en la práctica:
      - cierre mal nombrado (`</feedback>` cerrando `<generalfeedback>`);
      - bloque abierto sin cerrar (`<feedback>` sin `</feedback>` antes de `</answer>`),
        en cuyo caso se cierran implícitamente los hijos pendientes.
    Las etiquetas de cierre huérfanas se descartan en lugar de romper el documento.
    """
    out, stack, pos = [], [], 0
    for m in _TAG_RE.finditer(xml_text):
        out.append(xml_text[pos:m.start()])
        pos = m.end()
        raw = m.group(0)
        name = m.group(2)
        if name is None:  # CDATA, comentario, declaración: se copia tal cual
            out.append(raw)
            continue
        if m.group(1) == "/":
            target = name
            if target not in stack:
                # typo típico: prefijo/sufijo de una etiqueta abierta (feedback/generalfeedback)
                target = next((t for t in reversed(stack)
                               if t.endswith(name) or name.endswith(t)), None)
                if target is None:
                    continue  # cierre huérfano: se descarta
            while stack and stack[-1] != target:
                out.append(f"</{stack.pop()}>")
            if stack:
                stack.pop()
            out.append(f"</{target}>")
        elif raw.endswith("/>"):
            out.append(raw)
        else:
            stack.append(name)
            out.append(raw)
    out.append(xml_text[pos:])
    while stack:
        out.append(f"</{stack.pop()}>")
    return "".join(out)


def _salvage_quiz_xml(xml_text: str) -> str:
    """Intenta convertir una salida imperfecta del LLM en XML de Moodle parseable.

    1) Escapa '&' sueltos.  2) Reequilibra etiquetas mal cerradas (_balance_tags).
    3) Recorta desde el primer <question> hasta el último </question> y lo envuelve en <quiz>.
    4) Último recurso: conserva solo las preguntas que parsean por separado.
    """
    variants = []

    def add(cand):
        if cand and cand not in variants:
            variants.append(cand)

    for base in (xml_text, _AMP.sub("&amp;", xml_text)):
        add(base)
        try:
            add(_balance_tags(base))
        except Exception:
            pass
        first, last = base.find("<question"), base.rfind("</question>")
        if first != -1 and last > first:
            wrapped = f"<quiz>{base[first:last + len('</question>')]}</quiz>"
            add(wrapped)
            try:
                add(_balance_tags(wrapped))
            except Exception:
                pass

    for cand in variants:
        try:
            _parse_questions(cand)
            return cand
        except Exception:
            continue

    # Último recurso: reconstruir pregunta a pregunta las que sí estén completas
    first, last = xml_text.find("<question"), xml_text.rfind("</question>")
    if first != -1 and last > first:
        blocks = re.findall(r"<question\b.*?</question>", xml_text[first:last + len("</question>")], re.DOTALL)
        good = []
        for b in blocks:
            for cand in (b, _AMP.sub("&amp;", b)):
                try:
                    bal = _balance_tags(cand)
                except Exception:
                    bal = cand
                for final in (cand, bal):
                    try:
                        ET.fromstring(f"<quiz>{final}</quiz>")
                        good.append(final)
                        break
                    except Exception:
                        continue
                else:
                    continue
                break
        if good:
            return "<quiz>\n" + "\n".join(good) + "\n</quiz>"
    raise ValueError("no se pudo reparar el XML")


async def _ensure_valid_quiz_xml(xml_text: str, num_questions: int, model_target: str, allow_llm: bool = True) -> tuple:
    """Valida el XML y, si falla, pide al LLM una versión corregida (1 reintento) o lo repara."""
    try:
        return xml_text, len(_parse_questions(xml_text)), "llm"
    except Exception as first_err:
        # 1) Reparación local
        try:
            fixed = _salvage_quiz_xml(xml_text)
            return fixed, len(_parse_questions(fixed)), "repaired"
        except Exception:
            pass
        if not allow_llm:
            raise ValueError(f"XML inválido y no reparable localmente: {first_err}")
        # 2) Reintento pidiendo corrección explícita
        fix_prompt = f"""El siguiente XML de preguntas Moodle NO es válido ({first_err}).
Corrígelo manteniendo el contenido. Devuelve EXCLUSIVAMENTE el XML completo y bien formado,
empezando por <quiz> y terminando por </quiz>, con exactamente {num_questions} preguntas multichoice.

XML a corregir:
{xml_text[:12000]}
"""
        fixed_raw = await query_llm(
            fix_prompt,
            system_instruction="Eres un experto en Moodle XML. Devuelves solo XML válido y bien formado.",
            model_target=model_target,
            temperature=0.0,
            max_tokens=4000
        )
        fixed_raw = re.sub(r"^```(?:xml)?\n?", "", fixed_raw.strip())
        fixed_raw = re.sub(r"\n?```$", "", fixed_raw.strip())
        try:
            return fixed_raw, len(_parse_questions(fixed_raw)), "llm-retry"
        except Exception as second_err:
            try:
                salvaged = _salvage_quiz_xml(fixed_raw)
                return salvaged, len(_parse_questions(salvaged)), "llm-retry-repaired"
            except Exception:
                raise ValueError(f"XML inválido tras reintento: {second_err}")


@mcp.tool()
async def moodle_generate_and_import_quiz(
    course_id: int,
    section_num: int,
    quiz_name: str,
    topic: str,
    num_questions: int = 5,
    difficulty: str = "Intermedio / Clínico",
    model_target: str = "auto",
    temperature: float = 0.2,
    category_name: str = "",
    xml_content: str = "",
    dry_run: bool = False,
    attempts: int = 1
) -> Dict[str, Any]:
    """Genera preguntas de opción múltiple con casos clínicos en formato Moodle XML, las importa al banco y crea el Quiz en el curso.

    - `category_name`: categoría del banco donde importar. Si está vacía se deriva del nombre del quiz.
      Se inyecta en el XML como `<question type="category">` para que el import la cree en el contexto del curso
      (import_questions con categoryid=0 usa la categoría por defecto del sistema).
    - `xml_content`: si se envía, no se llama al LLM (publicación de un borrador ya generado).
    - `dry_run`: devuelve el XML validado sin escribir nada en Moodle.
    - `attempts`: intentos permitidos en el quiz (1 = un único intento, nota = mejor intento).
    """
    prompt = f"""Genera {num_questions} preguntas tipo test de opción múltiple con justificación clínica en formato estándar MOODLE XML.
Tema evaluado: {topic}
Nivel / Dificultad: {difficulty}

Reglas estrictas de formato Moodle XML:
1. El XML debe comenzar con `<quiz>` y terminar con `</quiz>`.
2. Cada pregunta debe ser `<question type="multichoice">`.
3. Debe incluir `<name><text>Nombre corto</text></name>`.
4. El texto de la pregunta va en `<questiontext format="html"><text><![CDATA[...]]></text></questiontext>`.
5. 4 opciones por pregunta: una con `fraction="100"` (correcta) y tres con `fraction="0"` (incorrectas).
6. Cada opción `<answer>` debe incluir `<feedback format="html"><text><![CDATA[Justificación...]]></text></feedback>`.
7. `<single>true</single>`, `<shuffleanswers>1</shuffleanswers>`.
8. Cada pregunta debe incluir `<generalfeedback format="html"><text><![CDATA[Comentario global]]></text></generalfeedback>`.

Devuelve EXCLUSIVAMENTE el XML válido (sin explicaciones adicionales).
"""
    xml_raw = None
    try:
        if xml_content.strip():
            xml_clean = xml_content.strip()
            source = "provided"
        else:
            xml_raw = await query_llm(
                prompt,
                system_instruction="Eres un examinador médico experto en crear evaluaciones formativas en formato Moodle XML.",
                model_target=model_target,
                temperature=temperature,
                max_tokens=3500
            )
            xml_clean = re.sub(r"^```(?:xml)?\n?", "", xml_raw.strip())
            xml_clean = re.sub(r"\n?```$", "", xml_clean.strip())
            source = "llm"

        # Validación (con reparación / reintento) antes de tocar Moodle
        try:
            xml_clean, n_questions, validation = await _ensure_valid_quiz_xml(
                xml_clean, num_questions, model_target, allow_llm=(source != "provided")
            )
        except Exception as e:
            return {"ok": False, "error": f"XML inválido: {e}", "raw_xml": xml_clean, "source": source}
        if n_questions < 1:
            return {"ok": False, "error": "El XML no contiene preguntas <question type=\"multichoice\">", "raw_xml": xml_clean, "source": source}

        cat = (category_name or "").strip() or f"IA {quiz_name}"[:60]
        cat_escaped = xml_escape(cat)

        result: Dict[str, Any] = {
            "ok": True,
            "dry_run": bool(dry_run),
            "source": source,
            "quiz_name": quiz_name,
            "section_num": section_num,
            "category_name": cat,
            "question_count": n_questions,
            "validation": validation,
            "xml": xml_clean,
        }
        if dry_run:
            return result

        # 1. Importar preguntas al banco dentro de la categoría propia del quiz
        category_node = (
            '<question type="category">'
            f'<category><text>$course$/{cat_escaped}</text></category>'
            '<info format="html"><text></text></info>'
            '</question>'
        )
        if "type=\"category\"" not in xml_clean:
            xml_clean = xml_clean.replace("<quiz>", "<quiz>\n" + category_node, 1)

        import_res = await call_moodle_ws("local_ia_moodle_editor_import_questions", {
            "courseid": course_id,
            "xmlcontent": xml_clean,
            "categoryid": 0
        })

        # 2. Crear cuestionario y asociar exactamente las preguntas de esa categoría
        quiz_res = await call_moodle_ws("local_ia_moodle_editor_add_quiz_with_questions", {
            "courseid": course_id,
            "sectionnum": section_num,
            "name": quiz_name,
            "categoryname": cat,
            "numquestions": n_questions
        })

        # 3. Fijar intentos (el plugin crea el quiz con intentos ilimitados)
        quiz_instance = quiz_res.get("instanceid")
        settings_res = None
        if quiz_instance and attempts and attempts > 0:
            try:
                settings_res = await call_moodle_ws("local_ia_moodle_editor_update_quiz_settings", {
                    "quizid": quiz_instance,
                    "attempts": attempts
                })
            except Exception as e:
                settings_res = {"warning": str(e)}

        result.update({
            "imported_questions": import_res.get("questionids", []),
            "quiz_result": quiz_res,
            "quiz_instance_id": quiz_instance,
            "coursemoduleid": quiz_res.get("coursemoduleid"),
            "settings_result": settings_res,
            "attempts": attempts or 0,
        })
        return result
    except Exception as e:
        return {"ok": False, "error": str(e), "raw_xml": xml_clean if 'xml_clean' in locals() else xml_raw}


@mcp.tool()
async def moodle_update_quiz_settings(
    quiz_id: int,
    grade: Optional[float] = None,
    attempts: Optional[int] = None
) -> Dict[str, Any]:
    """Ajusta la nota máxima y/o el número de intentos de un cuestionario existente."""
    try:
        params: Dict[str, Any] = {"quizid": quiz_id}
        if grade is not None:
            params["grade"] = grade
        if attempts is not None:
            params["attempts"] = attempts
        res = await call_moodle_ws("local_ia_moodle_editor_update_quiz_settings", params)
        return {"ok": True, "result": res}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def moodle_direct_llm_assist(
    prompt: str,
    system_instruction: str = "Eres un asistente de IA para educación médica y plataformas Moodle.",
    model_target: str = "auto"
) -> Dict[str, Any]:
    """Permite realizar consultas directas y canalizar prompts a los nodos LLM locales (Intel Arc)."""
    try:
        response = await query_llm(
            prompt=prompt,
            system_instruction=system_instruction,
            model_target=model_target
        )
        return {"ok": True, "response": response}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8097)
    args = parser.parse_args()
    mcp.run(transport="streamable-http", host="0.0.0.0", port=args.port)
