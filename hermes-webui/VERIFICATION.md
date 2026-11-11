# Verificación de integración Hermes — 2026-11-11

## Validación conjunta final — t_37849dc3

Ejecutada sobre `b158a1e5e5d6e2f5cef02da642fc9542e1fe6748` y las correcciones de
regresión/documentación de esta tarea. No se cambió la propiedad del Compose ni se
detuvo la instancia activa de Hermes.

### Estáticas y unitarias

Desde la raíz del repositorio:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s hermes-webui -p 'test_*.py' -v
bash -n hermes-webui/init-agent.sh
docker compose --env-file .env.example --profile '*' config -q
git diff --check
```

Resultado: **9 + 4 + 2 = 15 pruebas OK**. Compose válido con todos los perfiles,
sintaxis shell y whitespace correctos. En la ejecución de evidencia `DATA_DIR` se
sustituyó por una ruta temporal equivalente. Las nuevas regresiones cubren los
componentes compartidos, la ausencia de perfiles en Hermes, el agente fijado, el
volumen persistente de fuentes y el chat in-process sin gateway. El test local de
init clona un Git real con submódulo y rearranca sin red.

### Arranque limpio y repetido, aislado

Proyecto temporal `ia-full-isolated-t37849dc3`, home/workspace nuevos bajo `$TMPDIR`,
UI en loopback `18787`, proveedor mock OpenAI-compatible en loopback `18006`, sin GPU
ni binds/puertos de producción. Se usó el Compose raíz con un override temporal y un
wrapper `run-compose.sh` que fijaba inline todas las variables Hermes; esto importa
porque las variables exportadas por el shell prevalecen sobre `.env`.

Comandos centrales ejecutados:

```bash
bash ../run-compose.sh config --format json > ../rendered-isolated.json
# Assert sobre el JSON: proyecto/home/workspace y mounts aislados correctos.
bash ../run-compose.sh up -d --build
curl --noproxy '*' -fsS http://127.0.0.1:18787/health
curl --noproxy '*' -fsS http://127.0.0.1:18787/api/onboarding/status
bash ../run-compose.sh ps -a --format json > ../results/ps-before-second.json
bash ../run-compose.sh up -d
bash ../run-compose.sh ps -a --format json > ../results/ps-after-second.json

docker exec -i hermes-webui python3 - <<'PY'
# urllib.request POST a http://127.0.0.1:18006/v1/chat/completions;
# modelo exacto del ejemplo, un mensaje y max_tokens=4.
PY
```

Resultados reales:

- El build limpio y `up -d --build` terminaron 0. Se crearon los 11 recursos del
  grafo, incluido el init one-shot y un mock en lugar de OVMS. Mesh quedó healthy;
  `/healthz` respondió `ok=true`.
- Se comprobó por JSON renderizado y `docker inspect` que el home era el temporal y
  que `/workspace` y su ruta absoluta montaban el workspace temporal.
- Init clonó y fijó `0ea6ea941aee31b20fffeb6dfa0d0f29f4370e2e`; terminó 0.
  La UI quedó healthy y `/health` respondió `status=ok`.
- Tras apuntar la configuración temporal al puerto publicado del mock, onboarding
  anidado en `system` informó `hermes_found=true`, `imports_ok=true`,
  `provider_ready=true` y `chat_ready=true`; `current_provider=custom` y la URL/base
  eran las del mock. La WebUI descubrió exactamente el modelo de ejemplo, sin los
  modelos de producción.
- La sonda de completion final, ejecutada con stdin (`docker exec -i`) desde el
  contenedor WebUI contra una instancia temporal del mismo mock, devolvió
  `id=isolated-validation`, contenido **`OK`** y usage `{1,1,2}` del mock. No alteró
  la configuración ni el proveedor activo. Es una prueba de protocolo, no inferencia
  real ni una conversación browser/WebUI con herramientas MCP.
- El segundo `up -d` conservó los IDs de los **11/11 contenedores**, volvió a ejecutar
  el init sin clonar (`Hermes Agent ready` con el mismo SHA), lo dejó `Exited (0)` y
  mantuvo una única WebUI healthy. No fueron necesarios pasos manuales internos.
- No había contenedores `open-webui`, `openwebui` o `litellm`. El volumen antiguo
  `ia-services-stack_open-webui-data` siguió presente.
- `bash ../run-compose.sh down -v` eliminó únicamente contenedores, redes y volúmenes
  del proyecto temporal; las comprobaciones posteriores devolvieron listas vacías.
  La UI activa siguió `status=ok`, sin runs/streams activos, y su configuración
  conservó el hash previo.

Se descartaron los ensayos preparatorios con overrides incompletos o variables de
entorno heredadas; no se usan como evidencia. La guía operativa documenta esa
precedencia para evitar montar accidentalmente el home real en un trial.

**Pendiente operativo deliberado:** la UI activa todavía pertenece al Compose hermano
porque ejecuta esta tarea. El corte está preparado paso a paso en
[la guía de migración](../docs/migracion-hermes-webui.md), con rollback y sin borrado
de datos, pero no se ejecutó sobre la instancia que sostiene al worker.

## Antecedente: sonda dirigida de Hermes y OVMS real — t_d1f1c7c9

Ejecutada sobre el Compose raíz, sin cambiar la instancia de producción:

- `python3 -m unittest discover -s hermes-webui -p 'test_*.py' -v`: **2 tests OK**.
- `bash -n hermes-webui/init-agent.sh`, `docker compose --env-file .env.example config -q`
  y `git diff --check`: OK.
- `docker manifest inspect ghcr.io/nesquena/hermes-webui:0.52.666`: OK.
- Home/workspace vacíos bajo `$TMPDIR`, puerto 18787, imagen fijada por SHA:
  init terminó 0; la UI instaló Python 3.14.10 y dependencias y quedó healthy.
- Home vacío: `hermes_found=true`, `imports_ok=true`, `config_exists=false`,
  `provider_ready=false`, `chat_ready=false`, como corresponde.
- Con `config.example.yaml`: `config_exists=true`, `provider_ready=true` y
  `chat_ready=true`; URL directa OVMS `http://127.0.0.1:8006/v1`, sin proxy LLM.
- `/v1/models` desde Python del contenedor devolvió el modelo del ejemplo y dos
  modelos de embedding.
- `/v1/chat/completions` desde el contenedor a OVMS real, prompt `Reply OK.` y
  `max_tokens=8`, devolvió **`OK.`**, `finish_reason=stop`, 11 tokens de entrada y 3
  de salida. No se probó una conversación HTTP de la propia WebUI ni herramientas MCP.
- Repetir `up -d hermes-webui` mantuvo la misma UI sana; init terminó sin clonar de
  nuevo y no arrancó OpenWebUI/LiteLLM. Se eliminaron solo los recursos del trial.
