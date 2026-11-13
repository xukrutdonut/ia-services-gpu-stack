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
| `ninfer-server` | 8090 | Motor ninfer en la V100 (perfil `ninfer`); contenedor `ninfer-serve`, el motor se lanza con `./ninfer-serve.sh` |
| `open-terminal` | 8003 | API REST de herramientas, no MCP |
| `searxng` | 8085 | Buscador compartido |
| `searxng-mcp` | 8092 | Adaptador MCP de búsqueda (SSE) |
| `moodle-ia-mcp` | 8097 | Moodle; requiere token real |
| `zotero-mcp` | 8765 | Opcional, perfil `zotero`, acceso al Zotero del host |
| `docker-agent` | — | Runner opcional, perfil `agent`; código montado externamente |

La fuente de verdad de puertos, mounts y perfiles es `docker-compose.yml`.
OpenWebUI y LiteLLM están retirados, no son requisitos del stack. Consulta la
[guía única de instalación, arranque, migración y datos antiguos](docs/migracion-hermes-webui.md).

Puesta en marcha completa de la **Tesla V100** (hardware, driver, motor
ninfer, ventilador y verificación): [V100-HOWTO.md](V100-HOWTO.md). El
subsistema del ventilador vive en [`v100-fan/`](v100-fan/README.md).

## Inicio

Los pasos siguientes usan las rutas predeterminadas. La instalación limpia y la
configuración inicial de Hermes se detallan en la guía enlazada; no sobrescribas `.env`
ni `$HOME/.hermes/config.yaml` existentes.

```bash
cp -n .env.example .env
# Ajustar rutas, UID/GID y credenciales locales necesarias.
docker compose config -q
docker compose up -d --build
```

Requisitos: Docker Engine/Compose v2, acceso a Internet para imágenes/fuentes,
permisos sobre los binds del host y los dispositivos Intel del Compose. OVMS monta
los modelos desde la ruta del servicio `ia-gpu-intel-openvino`; no los descarga este
arranque. Los demás binds externos del Compose deben existir. No usar `down -v` para
mantenimiento.

**Hermes WebUI forma parte del Compose raíz** y se inicia con el mismo
`docker compose up -d`: abre `http://127.0.0.1:8787`. El inicializador obtiene el
agente y sus submódulos antes de arrancar la interfaz, sin Compose independiente ni
gateway intermedio. El backend se configura en el home de Hermes. No arrancar dos
interfaces sobre el mismo puerto/home.

**Inferencia:** [MESH.md](MESH.md) describe las conexiones directas y los IDs
reales de modelo. Para usar Hermes contra la V100 hay un perfil dedicado con el
prompt recortado (60 % menos tokens por turno) y el preset de arranque del motor:
[perfil v100](docs/perfil-v100-hermes.md). Hailo y la NPU son externos; ninfer sí lo levanta este Compose
(`ninfer-server`, perfil `ninfer` porque la V100 es una eGPU que no siempre está
enumerada), aunque su motor se arranca aparte con `./ninfer-serve.sh`.
No hay orquestador ni gateway intermedio: cada cliente apunta al proveedor.

## Comprobaciones

```bash
docker compose --env-file .env.example config -q
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s hermes-webui -p 'test_*.py' -v
curl --fail http://127.0.0.1:8006/v1/models
# con el motor ninfer lanzado (./ninfer-serve.sh):
curl --fail http://127.0.0.1:8090/v1/models
docker compose ps -a
```

Las pruebas del orquestador usan un proveedor HTTP local de prueba; no sustituyen
una generación real en OVMS. `/healthz` indica salud local del orquestador, no
que todos los proveedores estén listos. Los benchmarks en `benchmark/` y el
[archivo de la malla anterior](docs/history/inference-before-hermes.md) son
históricos, no instrucciones del despliegue actual.
