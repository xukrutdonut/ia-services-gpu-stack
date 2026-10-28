# Moodle Course Studio

Frontend de configuración y ejecución para el contenedor `moodle-ia-mcp`. No reimplementa la generación: actúa como proxy JSON-RPC de las herramientas MCP y como orquestador de trabajos (jobs) con revisión y publicación.

Acceso: `http://localhost:8098` (puerto publicado por el servicio `moodle-course-studio` de `docker-compose.yml`).

## Flujo de uso

1. **Curso y destino** — elegir un curso existente o crear uno nuevo (nombre corto, formato, `category_id`). El token del Web Service no permite `core_course_get_categories`, así que la UI oculta el selector y pide el id a mano (por defecto `1`).
2. **Temario** — tema, nº de módulos y preguntas por módulo; el temario lo genera la IA local (`moodle_generate_syllabus`) y se edita en tarjetas (título de lección, cuestionario, contenido, preguntas, intentos, dificultad).
3. **Generación** — modelo (`intel` = OpenVINO en el Arc), temperatura, `max_tokens`, y modo:
   - *Revisar antes de publicar* → `dry_run: true`, `publish: false`: genera borradores sin tocar Moodle.
   - *Publicar directamente* → ejecuta y publica cada paso.
4. **Revisión y publicación** — pestaña por borrador con vista previa (HTML de la página / XML del quiz), editor para corregir, y `Publicar` / `Republicar`. `Reintentar fallidos` relanza solo los pasos en estado `error`, sin repetir lo ya aplicado.
5. **Historial** — ejecuciones persistidas en `./data/jobs` (volumen `./moodle-course-studio/data:/data/jobs`).

## Pasos soportados por un job

| `kind`   | Efecto |
|----------|--------|
| `section`| Renombra una sección del curso |
| `page`   | Genera la lección y la añade como `mod_page` |
| `quiz`   | Genera el XML, valida con `dry_run` y lo importa como `mod_quiz` |

Si el modelo devuelve un XML no parseable, el job **no pierde la generación**: guarda el XML crudo como borrador marcado `XML inválido`, con el error del parser, para corregirlo a mano o reintentar el paso.

## API

- `GET  /api/health` — estado del proxy y del MCP.
- `POST /api/call` — `{name, arguments, timeout}` → proxy JSON-RPC a `moodle-ia-mcp`.
- `POST /api/jobs` — `{course_id, title, options:{model_target,temperature,max_tokens,dry_run}, plan:[...]}`.
- `GET  /api/jobs` — lista de ejecuciones.
- `GET  /api/jobs/<id>` — estado, pasos, resultados y log.
- `POST /api/jobs/<id>/retry` — `{keys:[...]}` (sin `keys` = todos los pasos en error).
- `POST /api/jobs/<id>/cancel` — cancela entre pasos.
- `POST /api/jobs/<id>/results` — `{key, patch}` para guardar ediciones del revisor.

## Variables de entorno

| Variable | Valor por defecto |
|----------|-------------------|
| `MCP_URL` | `http://moodle-ia-mcp:8097/mcp` |
| `DATA_DIR` | `/data/jobs` |
| `MOODLE_PUBLIC_URL` | `https://moodle.neuropedialab.org` |
| `CALL_TIMEOUT` | `1800` |

Sin dependencias externas: `server.py` usa solo la stdlib (`http.server`, `urllib`, `threading`).

## Rebuild

```bash
cd /home/arkantu/produccion/ia-services-stack
docker compose build moodle-course-studio
docker rm -f moodle-course-studio; docker compose up -d moodle-course-studio
curl -s localhost:8098/api/health
```

## Notas

- El cliente MCP serializa `initialize` + llamadas con `RLock` y re-inicializa la sesión si el servidor responde `400`/`404` o `session ... not found` (ocurre tras reiniciar `moodle-ia-mcp`).
- Los cursos de prueba deben crearse con `visible=0` y el sufijo `(borrar)` en el nombre completo.
