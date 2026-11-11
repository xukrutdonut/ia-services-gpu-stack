# Inferencia directa y orquestación

No hay gateway central. Cada cliente configura el proveedor OpenAI-compatible,
su **ID real** de modelo y autenticación solo si ese proveedor la requiere.
Hermes usa la configuración de su home: [integración](hermes-webui/README.md).
El orquestador ofrece jobs/fan-out, **no** un proxy `/v1/chat/completions`.

## Proveedores comprobados durante la retirada (2026-11-11)

| Proveedor | Base desde el host | Modelos publicados por `/v1/models` |
|---|---|---|
| OVMS del stack | `http://127.0.0.1:8006/v1` | `Qwen3-30B-A3B-Instruct-2507-int4-ov`, `qwen3-embedding-0.6b-int4-ov`, `nomic-embed-text-v1.5-ov` |
| Hailo externo | `http://192.168.0.80:8089/v1` | `ocr-hailo-8l`, `clip-resnet-50x4-hailo` |
| NPU externa | `http://127.0.0.1:8012/v1` | `qwen3-embedding-0.6b-int4-ov` |

Las consultas devolvieron 200 sin autenticación; no se copia ninguna clave del
antiguo gateway. Esto no equivale a probar inferencia de cada modelo. Desde el
bridge Compose, OVMS se alcanza por `http://ia-gpu-intel-openvino:8000/v1`.
En la UI con red host se usa la URL del host, no ese nombre DNS.
No intercambiar índices NPU/OVMS: usan pooling diferente aunque el nombre del
modelo coincida. CLIP es imagen-imagen, no embeddings de texto.

`ninfer` en `:8090` **no estaba disponible** (conexión rechazada, contenedor
parado). Se conserva `ninfer-serve.sh`, pero el stack no levanta ese motor ni lo
requiere. Tampoco publica un alias `vision` que OVMS no pueda satisfacer.
Para incorporarlo después, comprobar su `/v1/models` y una generación real,
añadir una entrada a `MODEL_ROUTES` con ese modelo/URL accesible desde Docker y
su sonda. `host.docker.internal` requiere añadir
`extra_hosts: ["host.docker.internal:host-gateway"]` en Linux si se usa.

## mesh-orchestrator

`MODEL_ROUTES` es un objeto JSON `{alias: {base_url, model}}` en Compose. Los
aliases `chat`, `deep`, `lms27`, `worker-big`, `arc`, `worker`, `tiny` apuntan al
OVMS disponible. Son nombres de compatibilidad, no motores distintos ni IDs
para configurar en Hermes o al llamar al proveedor directamente. `ocr` apunta
a Hailo y requiere imagen en el contenido de la petición. Hailo y NPU son
externos; no son dependencias de arranque de Hermes.
Las rutas actuales no implementan claves por proveedor: si necesita autenticación,
adaptar primero el transporte de forma segura, no introducir una clave ficticia.

Se conserva SQLite en `mesh-orchestrator-data`, cola acotada por alias,
`LANE_CONCURRENCY`, `MAX_QUEUE_PER_LANE`, timeout y sondas `LANE_HEALTH`.
`LANE_FALLBACK={}`: no desviar un fallo de OVMS a otro alias del mismo servidor.
Los límites por alias no son un límite global del hardware: varios aliases
comparten proveedor. Ajustar la concurrencia de clientes. El antiguo rate-limit,
cooldown y fallback global del gateway ya no existen. `ALLOW_SHELL=0` mantiene
desactivada la herramienta de shell del orquestador.

```bash
docker compose up -d --build mesh-orchestrator
curl --fail http://127.0.0.1:4010/healthz
curl --fail http://127.0.0.1:4010/v1/queue
curl --fail http://127.0.0.1:4010/v1/jobs -H 'Content-Type: application/json' \
  -d '{"model":"worker","tasks":[{"id":"prueba","prompt":"Responde solo OK"}],"max_concurrency":1}'
# Consultar el job_id devuelto con GET /v1/jobs/<job_id>
```

- `POST /v1/fanout`: `{task, items, worker, planner, max_concurrency}`; cada
  elemento recibe contexto propio y el planner sintetiza en `payload.final`.
- `GET /v1/jobs?limit=20`: lista de trabajos.
- `DELETE /v1/jobs/{id}`: borra solo ese trabajo y resultados.
- `GET /healthz`: salud **local**, no readiness de todos los proveedores.
  Comprobar también `/v1/models` e inferencia real. Los errores de inferencia
  se reflejan en los resultados de cada tarea.

La [migración](docs/retirada-openwebui-litellm.md) explica retirada de contenedores,
datos y clientes externos. Las mediciones y procedimientos previos se preservan
en el [archivo histórico no operativo](docs/history/inference-before-hermes.md).
