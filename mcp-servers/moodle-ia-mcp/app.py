import os
import sys
import json
import re
import asyncio
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
AMD_NODE_URL = os.environ.get("AMD_NODE_URL", "http://192.168.0.80:1234/v1")

DEFAULT_MODEL_INTEL = os.environ.get("DEFAULT_MODEL_INTEL", "qwen2.5-7b-instruct-int4-ov")
DEFAULT_MODEL_AMD = os.environ.get("DEFAULT_MODEL_AMD", "qwen2.5-7b-instruct")

# ==========================================
# CLIENTE LLM LOCAL (INTEL ARC & AMD NODE)
# ==========================================
async def query_llm(
    prompt: str,
    system_instruction: str = "Eres un asistente experto en educación médica, neuropediatría y diseño pedagógico para plataformas Moodle.",
    model_target: str = "auto",
    temperature: float = 0.3,
    max_tokens: int = 3500
) -> str:
    """Envía peticiones de inferencia a los motores locales (Intel Arc / AMD RX480)."""
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": prompt}
    ]
    
    endpoints = []
    if model_target.lower() == "amd":
        endpoints = [(AMD_NODE_URL, DEFAULT_MODEL_AMD, "AMD RX480")]
    elif model_target.lower() == "intel":
        endpoints = [
            (INTEL_OPENVINO_URL, DEFAULT_MODEL_INTEL, "Intel Arc (Direct)"),
            (INTEL_FALLBACK_URL, DEFAULT_MODEL_INTEL, "Intel Arc (Fallback LAN)")
        ]
    else:  # auto: Intel primero, luego AMD
        endpoints = [
            (INTEL_OPENVINO_URL, DEFAULT_MODEL_INTEL, "Intel Arc (Direct)"),
            (INTEL_FALLBACK_URL, DEFAULT_MODEL_INTEL, "Intel Arc (Fallback LAN)"),
            (AMD_NODE_URL, DEFAULT_MODEL_AMD, "AMD RX480")
        ]

    last_err = None
    for url, model, name in endpoints:
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
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
                    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                    return content
                else:
                    last_err = f"Endpoint {name} returned status {res.status_code}: {res.text}"
        except Exception as e:
            last_err = f"Error connecting to {name} ({url}): {e}"
            continue

    raise RuntimeError(f"Fallo en todos los endpoints LLM locales. Último error: {last_err}")


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
    model_target: str = "auto"
) -> Dict[str, Any]:
    """Genera una propuesta de temario estructurado para un curso Moodle con títulos, resúmenes y lecciones recomendadas."""
    prompt = f"""Diseña la estructura completa para un curso formativo en Moodle.
Tema general: {topic}
Público objetivo: {target_audience}
Número de módulos/secciones: {num_modules}
Resultados de aprendizaje esperados: {learning_outcomes or 'Formación clínica integral, actualización basada en evidencia y aplicación práctica'}

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
"""
    try:
        raw_json = await query_llm(
            prompt,
            system_instruction="Eres un diseñador curricular médico de alto nivel. Responde siempre con JSON estricto.",
            model_target=model_target,
            temperature=0.2
        )
        # Extraer JSON limpio si viene rodeado de ```json ... ```
        clean_json = re.sub(r"^```(?:json)?\n?", "", raw_json.strip())
        clean_json = re.sub(r"\n?```$", "", clean_json.strip())
        syllabus = json.loads(clean_json)
        return {"ok": True, "syllabus": syllabus}
    except Exception as e:
        return {"ok": False, "raw_response": raw_json if 'raw_json' in locals() else None, "error": str(e)}


@mcp.tool()
async def moodle_generate_and_add_page(
    course_id: int,
    section_num: int,
    page_title: str,
    topic_description: str,
    clinical_focus: str = "",
    model_target: str = "auto"
) -> Dict[str, Any]:
    """Genera con LLM local una lección en formato HTML profesional (estilo Bootstrap/Moodle) y la publica en el curso."""
    prompt = f"""Elabora el contenido completo y exhaustivo para una página de lección en Moodle.
Título de la lección: {page_title}
Objetivo y temas a cubrir: {topic_description}
Enfoque clínico/práctico: {clinical_focus or 'Evidencia actualizada, tablas comparativas, perlas clínicas y algoritmo de decisión'}

Estructura obligatoria en HTML limpio (compatible con Moodle / Bootstrap 4/5):
- <h2> y <h3> para estructurar secciones claras.
- Bloques de alerta visuales como `<div class="alert alert-info"><strong>💡 Perla Clínica:</strong> ...</div>` y `<div class="alert alert-warning"><strong>⚠️ Precaución / Red Flags:</strong> ...</div>`.
- Tablas comparativas o de dosificación/criterios cuando aplique (`<table class="table table-bordered table-striped">`).
- Listas ordenadas/desordenadas claras.
- Sección final: `<div class="card p-3 bg-light"><h4>📌 Puntos Clave para Llevar a la Práctica</h4><ul>...</ul></div>`.

IMPORTANTE: Devuelve ÚNICAMENTE el código HTML dentro del cuerpo de la lección (sin etiquetas <html>, <head> o <body>).
"""
    try:
        html_content = await query_llm(
            prompt,
            system_instruction="Eres un profesor especialista en educación médica y redacción clínica estructurada.",
            model_target=model_target,
            temperature=0.3,
            max_tokens=3800
        )
        # Limpieza básica de bloques markdown si el modelo los incluyó
        html_content = re.sub(r"^```(?:html)?\n?", "", html_content.strip())
        html_content = re.sub(r"\n?```$", "", html_content.strip())

        # Publicar en Moodle
        moodle_res = await moodle_add_page_content(
            course_id=course_id,
            section_num=section_num,
            name=page_title,
            content_html=html_content,
            intro_html=f"<p>Lección formativa: {page_title}</p>"
        )
        return {
            "ok": moodle_res.get("ok", False),
            "page_title": page_title,
            "section_num": section_num,
            "moodle_result": moodle_res.get("result"),
            "html_length": len(html_content)
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def moodle_generate_and_import_quiz(
    course_id: int,
    section_num: int,
    quiz_name: str,
    topic: str,
    num_questions: int = 5,
    difficulty: str = "Intermedio / Clínico",
    model_target: str = "auto"
) -> Dict[str, Any]:
    """Genera preguntas de opción múltiple con casos clínicos en formato Moodle XML, las importa al banco y crea el Quiz en el curso."""
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

Devuelve EXCLUSIVAMENTE el XML válido (sin explicaciones adicionales).
"""
    try:
        xml_raw = await query_llm(
            prompt,
            system_instruction="Eres un examinador médico experto en crear evaluaciones formativas en formato Moodle XML.",
            model_target=model_target,
            temperature=0.2,
            max_tokens=3500
        )
        xml_clean = re.sub(r"^```(?:xml)?\n?", "", xml_raw.strip())
        xml_clean = re.sub(r"\n?```$", "", xml_clean.strip())

        # 1. Importar preguntas al banco
        import_params = {
            "courseid": course_id,
            "xmlcontent": xml_clean,
            "categoryid": 0
        }
        import_res = await call_moodle_ws("local_ia_moodle_editor_import_questions", import_params)

        # 2. Crear cuestionario y asociar las preguntas
        quiz_params = {
            "courseid": course_id,
            "sectionnum": section_num,
            "name": quiz_name,
            "categoryname": "top",
            "numquestions": num_questions
        }
        quiz_res = await call_moodle_ws("local_ia_moodle_editor_add_quiz_with_questions", quiz_params)

        return {
            "ok": True,
            "imported_questions": import_res.get("questionids", []),
            "quiz_result": quiz_res
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "raw_xml": xml_raw if 'xml_raw' in locals() else None}


@mcp.tool()
async def moodle_direct_llm_assist(
    prompt: str,
    system_instruction: str = "Eres un asistente de IA para educación médica y plataformas Moodle.",
    model_target: str = "auto"
) -> Dict[str, Any]:
    """Permite realizar consultas directas y canalizar prompts a los nodos LLM locales (Intel Arc / AMD RX480)."""
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
