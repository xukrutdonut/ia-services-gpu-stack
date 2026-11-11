# Verificación de integración Hermes — 2026-11-11

Ejecutado sobre el Compose raíz, sin cambiar la instancia de producción:

- `python3 -m unittest discover -s hermes-webui -p 'test_*.py' -v`: **2 tests OK**.
  El test recursivo falló antes de crear el inicializador; después clonó un
  repositorio Git real con submódulo y repitió el inicio sin su origen disponible.
- `bash -n hermes-webui/init-agent.sh`: OK.
- `docker compose --env-file .env.example config -q`: OK.
- `git diff --check`: OK.
- `docker manifest inspect ghcr.io/nesquena/hermes-webui:0.52.666`: OK (tag disponible).

Prueba aislada de arranque (home y workspace vacíos bajo `$TMPDIR`, puerto 18787):

```bash
HERMES_HOME="$TMPDIR/hermes-stack-trial/home" \
HERMES_WORKSPACE="$TMPDIR/hermes-stack-trial/workspace" \
HERMES_WEBUI_PORT=18787 \
HERMES_WEBUI_IMAGE=ghcr.io/nesquena/hermes-webui@sha256:120aba063334b15d8f2d25249665d7e7c69cecb99cfcffb4d7f85d7c601e41a6 \
docker compose -p hermes-integration-test up -d hermes-webui
```

Resultados reales:

- Init terminó 0; imprimió `Hermes Agent ready: 0ea6ea941aee31b20fffeb6dfa0d0f29f4370e2e`.
- La UI instaló Python 3.14.10 y dependencias del agente; quedó **healthy**.
- `/health`: HTTP 200, `status=ok`.
- Home vacío: onboarding informó `hermes_found=true`, `imports_ok=true`,
  `config_exists=false`, `provider_ready=false`, `chat_ready=false`, como corresponde.
- Tras copiar `config.example.yaml` al home aislado, onboarding informó
  `config_exists=true`, `provider_ready=true`, `chat_ready=true`; URL directa OVMS
  `http://127.0.0.1:8006/v1`, sin proxy LLM.
- `/v1/models` solicitado desde Python del contenedor devolvió el modelo del
  ejemplo y dos modelos de embedding.
- `/v1/chat/completions` desde el contenedor al mismo backend, con prompt
  `Reply OK.`, `max_tokens=8`, devolvió **`OK.`**, finish_reason `stop`,
  11 tokens de entrada y 3 de salida. No se prueba aquí la conversación HTTP de
  la propia WebUI ni el uso de herramientas MCP.
- Repetir el mismo `up -d hermes-webui` mantuvo la UI existente y sana; init terminó
  otra vez sin clonar de nuevo. No se arrancaron OpenWebUI ni LiteLLM en el
  proyecto de prueba.

Se eliminaron solo los dos contenedores, volumen de código y red del proyecto de
prueba. La instancia de producción seguía `running healthy`. Queda pendiente la
migración de esa instancia independiente al Compose raíz: usa el mismo puerto y
está ejecutando las tareas actuales. La validación del arranque completo de todos
los servicios y retirada/migración de interfaces antiguas corresponde a la tarea
conjunta posterior, no a este ensayo dirigido de Hermes.
