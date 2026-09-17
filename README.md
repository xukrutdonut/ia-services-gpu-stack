# IA Services Stack

Docker Swarm stack para servicios de IA en homelab con GPUs Intel Arc (Xe2 128EU) y AMD RX480.

## Servicios

| Servicio | Puerto | Descripción |
|---|---|---|
| **intel-gemma4-optimum** | 8006 | Servidor OpenVINO GenAI (Intel Arc iGPU) compatible OpenAI API + panel admin en `/admin` |
| **open-webui** | 3000 | Frontend unificado para ambos backends GPU (Intel Arc + AMD RX480) |
| **searxng-mcp** | 8092 | MCP server para busqueda web via SearXNG |
| **openterminal-mcp** | 8003 | MCP server para ejecucion de comandos terminal |
| **fetch-mcp** | 8095 | MCP server para fetch de URLs |
| **nano-fs-tools-mcp** | - | MCP server para operaciones de filesystem |
| **zotero-mcp** | - | MCP server para integracion Zotero |
| **moodle-ia-mcp** | - | MCP server para integracion Moodle |
| **dual-gpu-benchmark** | - | Benchmark unificado dual-GPU (perfil `benchmark`) |
| **comfyui** | 8188 | Generacion de imagenes via ComfyUI |
| **openvino-npu-embeddings** | 8283 | Embeddings server via NPU Intel (perfil `npu-embeddings`) |

## Admin Dashboard GPU

El contenedor `intel-gemma4-optimum` expone un panel de gestion en:

```
http://localhost:8006/admin
```

Funcionalidades:
- Monitorizacion GPU Intel Arc Xe2 via sysfs (frecuencia, RC6, throttling)
- Gestion de modelos (carga/descarga/cache/warmup)
- Informacion del sistema (RAM, disk, dispositivos, NPU)
- Benchmark rapido
- Tail de logs del servidor
- GUI SPA estilo LM Studio con tema oscuro/claro

## Requisitos

- Docker + Docker Swarm (node hostname: `khazad-dum`)
- Intel Compute Runtime (Level-Zero / NEO) en el host
- Drivers NPU Intel AI Boost (Meteor Lake 3720) - ver seccion NPU Drivers
- Modelos OpenVINO IR en `/home/arkantu/produccion/openvino-models`
- Red externa `lms-amd-rx480_default` para backend AMD RX480

## NPU Drivers

Los binarios de drivers NPU (`npu-drivers/`) no se incluyen en el repo (146MB).

Obtenerlos del snap `intel-npu-driver`:

```bash
sudo snap install intel-npu-driver
cp /snap/intel-npu-driver/current/usr/lib/x86_64-linux-gnu/libopenvino_intel_npu_compiler_loader.so npu-drivers/
cp /snap/intel-npu-driver/current/usr/lib/x86_64-linux-gnu/libopenvino_intel_npu_compiler.so npu-drivers/
cp -P /snap/intel-npu-driver/current/usr/lib/x86_64-linux-gnu/libze_intel_npu.so* npu-drivers/
cp -P /snap/intel-npu-driver/current/usr/lib/x86_64-linux-gnu/libze_loader.so* npu-drivers/
cp -P /snap/intel-npu-driver/current/usr/lib/x86_64-linux-gnu/libze_tracing_layer.so* npu-drivers/
cp -P /snap/intel-npu-driver/current/usr/lib/x86_64-linux-gnu/libtbb*.so* npu-drivers/
cp -P /snap/intel-npu-driver/current/usr/lib/x86_64-linux-gnu/libhwloc.so* npu-drivers/
```

Repetir para `intel-gemma4-optimum/npu-drivers/`.

## Uso

```bash
# Copiar .env
cp .env.example .env
# Editar .env con tus credenciales

# Desplegar stack completo
docker compose up -d

# Solo benchmark dual-GPU
docker compose --profile benchmark run --rm dual-gpu-benchmark

# Solo NPU embeddings
docker compose --profile npu-embeddings up -d openvino-npu-embeddings
```

## Configuracion OpenVINO GenAI

Optimizaciones aplicadas (Intel Arc Xe2 128EU):
- `PERFORMANCE_HINT=LATENCY` (mejor para single-request, +10% vs throughput)
- `KV_CACHE_PRECISION=f16` (mitad de bandwidth KV)
- `INFERENCE_PRECISION_HINT=f16`
- `GPU_ENABLE_SDPA_OPTIMIZATION` (fused attention)
- `DYNAMIC_QUANTIZATION_GROUP_SIZE=32`
- Cache de compilacion persistente en volumen `ov_cache`

## Estructura

```
ia-services-stack/
  docker-compose.yml          # Stack completo
  .env.example                # Template de configuracion
  intel-gemma4-optimum/       # Servidor OpenVINO GenAI + admin dashboard
    Dockerfile
    server.py                 # API OpenAI-compatible
    admin_dashboard.py        # Panel gestion GPU (/admin)
    entrypoint.sh
    convert_models.py         # Conversion HF -> OpenVINO IR
  mcp-servers/                # MCP servers (searxng, fetch, zotero, etc)
  benchmark/                  # Benchmark dual-GPU
  comfyui/                    # ComfyUI
  searxng/                    # Config SearXNG
  ssfr_proxy/                 # Proxy SSRF
```