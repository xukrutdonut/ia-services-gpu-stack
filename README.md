# IA Services Stack

Docker Swarm stack para servicios de IA en homelab con GPUs Intel Arc (Xe2 128EU iGPU) y AMD RX480 (Polaris Vulkan).

## Servicios

| Servicio | Puerto | Descripción |
|---|---|---|
| **ia-gpu-intel-openvino** | 8006 | Servidor OpenVINO GenAI (Intel Arc iGPU) compatible OpenAI API + panel admin en `/admin` |
| **ia-gpu-amd-rx480** | 1235 | Nodo LM Studio (AMD RX480 Vulkan) aislado del host, con LMLink |
| **open-webui** | 3000 | Frontend unificado para ambos backends GPU |
| **dual-gpu-benchmark** | - | Benchmark dual-GPU (perfil `benchmark`) |
| **searxng-mcp** | 8092 | MCP server: búsqueda web via SearXNG |
| **openterminal-mcp** | 8003 | MCP server: terminal remoto |
| **fetch-mcp** | 8095 | MCP server: fetch HTTP |
| **nano-fs-tools-mcp** | 8096 | MCP server: filesystem + delegación IA a GPUs |
| **zotero-mcp** | 8765 | MCP server: Zotero local |
| **moodle-ia-mcp** | 8097 | MCP server: Moodle + IA |

## Arquitectura GPU Dual

```
Host (khazad-dum)
├── Intel Arc iGPU Xe2 128EU (PCI 00:02.0, renderD129)
│   └── ia-gpu-intel-openvino (OpenVINO GenAI, Level-Zero, ipc:host)
│       ├── API: http://localhost:8006/v1
│       └── Admin: http://localhost:8006/admin
│
├── AMD RX480 8GB (PCI 01:00.0, renderD128)
│   └── ia-gpu-amd-rx480 (LM Studio, Vulkan, namespace IPC propio)
│       └── API: http://localhost:1235/v1
│
└── open-webui:3000 → une ambos backends OpenAI-compatible
    ├── Backend 0: intel-arc (OpenVINO GenAI)
    └── Backend 1: amd-rx480 (LM Studio)
```

Por qué contenedores separados (no fusionados):
- Intel necesita `ipc: host` para Level-Zero shared memory
- AMD necesita namespace IPC propio para aislar LM Studio del host
- Stacks de drivers incompatibles (oneAPI/Level-Zero vs Mesa Vulkan/radeon)
- Restart policies opuestas (`unless-stopped` vs `"no"` por ring timeouts Polaris)

## Panel de Administración GPU

El contenedor `ia-gpu-intel-openvino` expone un panel de gestión en:

```
http://localhost:8006/admin
```

Incluye:
- Monitorización GPU Intel Arc vía sysfs (frecuencia, RC6, throttling)
- Gestión de modelos (cargar/descargar/warmup/estado cache)
- Info del sistema (RAM, disco, dispositivos, NPU)
- Benchmark rápido
- Tail de logs del servidor
- SPA estilo LM Studio (dark/light theme, sidebar 280px)

## Estructura

```
ia-services-stack/
├── docker-compose.yml          # Stack principal (11 servicios)
├── .env.example                # Template de variables (copiar a .env)
├── ia-gpu-intel-openvino/      # Backend Intel Arc (OpenVINO GenAI)
│   ├── Dockerfile
│   ├── server.py               # API OpenAI-compatible + admin router
│   ├── admin_dashboard.py      # Panel gestión GPU (/admin)
│   ├── entrypoint.sh
│   ├── convert_models.py       # Conversión HF -> OpenVINO IR
│   └── convert_one.py
├── ia-gpu-amd-rx480/           # Backend AMD RX480 (LM Studio Vulkan)
│   ├── Dockerfile
│   ├── entrypoint.sh
│   ├── run_benchmark.py
│   └── README.md               # Docs detalladas RX480 + benchmarks
├── benchmark/                  # Benchmark dual-GPU unificado
├── mcp-servers/                # 6 MCP servers
├── lm-chat/                    # Chat ligero alternativo
├── mcp-bridge/                 # Bridge MCP proxy
├── comfyui/                    # ComfyUI Dockerfile
├── searxng/                    # SearXNG config
└── ssrf_proxy/                 # Proxy Squid anti-SSRF
```

## Despliegue

```bash
# 1. Configurar variables
cp .env.example .env
# Editar .env con tus valores reales

# 2. NPU drivers (necesario para ia-gpu-intel-openvino)
# Obtener libs del snap intel-npu-driver:
#   /snap/intel-npu-driver/current/usr/lib/x86_64-linux-gnu/
# Copiar a ia-gpu-intel-openvino/npu-drivers/ antes del build

# 3. Construir y desplegar
docker compose build
docker compose up -d

# 4. Inicializar LM Studio en AMD (solo primera vez)
docker volume create ia-services-stack_lms_amd_profile
docker run --rm -it -v ia-services-stack_lms_amd_profile:/root/.lmstudio \
  ia-gpu-amd-rx480:latest /root/.lmstudio/bin/lms login
```

## Optimizaciones OpenVINO (Intel Arc)

- `PERFORMANCE_HINT=LATENCY` — más rápido para single-request (+10% medido)
- `KV_CACHE_PRECISION=f16` — mitiga el ancho de banda KV vs f32
- `GPU_ENABLE_SDPA_OPTIMIZATION` — fused Scaled Dot Product Attention
- `DYNAMIC_QUANTIZATION_GROUP_SIZE=32` — cuantización dinámica en grupos de 32
- `CACHE_DIR=/tmp/ov_cache` — cache de compilación persistente (volumen `ov_cache`)

## Rendimiento real

| GPU | Modelo | tok/s |
|---|---|---|
| Intel Arc (OpenVINO) | Qwen3-30B-A3B (3.3B activos) | 21 |
| Intel Arc (OpenVINO) | Dense 7B INT4 | 21 |
| Intel Arc (OpenVINO) | Dense 3B INT4 | 35 |
| AMD RX480 (Vulkan) | Llama 3.2 1B Q4_K_M | 94 |
| AMD RX480 (Vulkan) | Qwen 2.5 3B Q4_K_M | 43 |
| AMD RX480 (Vulkan) | Ministral 8B Q4_K_S | 21 |