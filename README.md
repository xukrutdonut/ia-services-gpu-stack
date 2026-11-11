# IA Services Stack

Servicios de IA del homelab, gestionados con **Docker Compose v2 en Linux**.
No desplegar este archivo con `docker stack deploy`: utiliza builds locales,
dispositivos del host y un inicializador con condición de finalización.

## Servicios

| Servicio | Puerto del host | Función |
|---|---:|---|
| `hermes-webui` | 8787, loopback por defecto | Interfaz de Hermes; proveedor configurado en su home |
| `hermes-agent-init` | — | Inicializa el agente y submódulos antes de la UI |
| `ia-gpu-intel-openvino` | 8006 | OVMS: chat y embeddings compatibles OpenAI, `/v1/models` |
| `mesh-orchestrator` | 4010 | Trabajos por lotes y fan-out sobre proveedores directos |
| `open-terminal` | 8003 | API REST de herramientas, no MCP |
| `searxng` | 8085 | Buscador compartido |
| `searxng-mcp` / `searxng-mcp-http` | 8092 / 8093 | Adaptadores MCP de búsqueda |
| `fetch-mcp` | 8095 | Fetch HTTP |
| `nano-fs-tools-mcp` | 8096 | Filesystem y delegación |
| `moodle-ia-mcp` | 8097 | Moodle; requiere token real |
| `zotero-mcp` | 8765 | Opcional, perfil `zotero`, acceso al Zotero del host |
| `docker-agent` | — | Runner opcional, perfil `agent`; código montado externamente |

La fuente de verdad de puertos, mounts y perfiles es `docker-compose.yml`.
OpenWebUI y LiteLLM están retirados, no son requisitos del stack. Su volumen
antiguo no se elimina: ver [migración y retirada opcional](docs/retirada-openwebui-litellm.md).

## Inicio

```bash
# Solo instalación nueva; NO sobrescribir un .env existente.
cp .env.example .env
# Ajustar rutas, UID/GID y credenciales locales necesarias.
docker compose config -q
docker compose up -d --build
```

Requisitos: Docker Engine/Compose v2, acceso a Internet para imágenes/fuentes,
permisos sobre los binds del host y los dispositivos Intel del Compose. OVMS
monta los modelos desde `${HOME}/modelos/openvino`; no los descarga este arranque.
Open Terminal requiere `${HOME}/openproject/jobs` y conserva su volumen de datos.
No usar `down -v` para mantenimiento.

**Hermes WebUI forma parte del Compose raíz** y se inicia con el mismo
`docker compose up -d`: abre `http://127.0.0.1:8787`. El inicializador obtiene el
agente y sus submódulos antes de arrancar la interfaz, sin Compose independiente
ni gateway intermedio. Seguir [requisitos, configuración directa del modelo,
persistencia y migración](hermes-webui/README.md). El backend se configura en el
home de Hermes, no mediante una clave de gateway en el `.env` de Compose.
No arrancar dos interfaces sobre el mismo puerto/home.

**Inferencia:** [MESH.md](MESH.md) describe conexiones directas, modelos reales
y API del orquestador. Hailo, NPU y ninfer son servicios externos, no levantados
por este Compose. Los aliases del orquestador no son IDs del proveedor ni
modelos para configurar en Hermes.

## Comprobaciones

```bash
docker compose --env-file .env.example config -q
python3 -m unittest discover -s tests -v
DATA_DIR="$PWD/.test-data" python3 -m unittest discover -s mesh-orchestrator -v
python3 -m unittest discover -s hermes-webui -p 'test_*.py' -v
curl --fail http://127.0.0.1:8006/v1/models
curl --fail http://127.0.0.1:4010/healthz
docker compose ps -a
```

Las pruebas del orquestador usan un proveedor HTTP local de prueba; no sustituyen
una generación real en OVMS. `/healthz` indica salud local del orquestador, no
que todos los proveedores estén listos. Los benchmarks en `benchmark/` y el
[archivo de la malla anterior](docs/history/inference-before-hermes.md) son
históricos, no instrucciones del despliegue actual.
