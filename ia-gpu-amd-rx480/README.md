# Nodo GPU AMD RX 480 para LM Studio y LMLink

Este contenedor proporciona un nodo aislado de inferencia para **LM Studio / llmster** utilizando la tarjeta grafica **AMD RX 480** a traves de Vulkan, conectandolo automaticamente a la red personal de **LMLink**.

## Estructura de modelos

```
/home/arkantu/modelos/
  moe/                          # Modelos grandes MoE (>8GB, solo host)
    DeepSeek-Coder-V2-Lite-Instruct/
    DeepSeek-V2-Lite-Chat/
    Mixtral-8x7B-Instruct-v0.1/
    Qwen1.5-MoE-A2.7B-Chat/
  rx480/                        # Modelos pequenos (<8GB, host + contenedor)
    Llama-3.2-1B-Instruct/
    Llama-3.2-3B-Instruct/
    Qwen2.5-3B-Instruct/
    Qwen2.5-Coder-7B-Instruct/
    Meta-Llama-3.1-8B-Instruct/
    Ministral-8B-Instruct-2410/
    gemma-4-E4B-it/
```

- **Host (LM Studio desktop, Intel Arc)**: ve `~/.lmstudio/models` -> `/home/arkantu/modelos/` (todos: moe + rx480)
- **Contenedor (RX480)**: monta solo `/home/arkantu/modelos/rx480/` como `/root/.lmstudio/models:ro`

Esto evita que el contenedor RX480 intente cargar modelos >8GB que no caben en VRAM.

## Arquitectura y Aislamiento

Para permitir la ejecucion simultanea de LM Studio en el sistema Desktop (Intel Arc) y el contenedor aislado (AMD RX480) sin conflictos:

1. **Aislamiento de Asiento y Display Server en Udev (`/etc/udev/rules.d/90-exclude-amd-rx480-egpu.rules`)**:
   El host desasocia la eGPU de `seat0` para que GNOME Shell, Mutter y Xwayland nunca la abran como dispositivo de pantalla o render offload:
   ```udev
   ACTION=="add|change", SUBSYSTEM=="pci", ATTR{vendor}=="0x1002", ATTR{device}=="0x67df", ENV{ID_FOR_SEAT}="", TAG-="seat", TAG-="master-of-seat"
   ACTION=="add|change", SUBSYSTEM=="drm", KERNEL=="card*", KERNELS=="0000:01:00.0", ENV{ID_FOR_SEAT}="", TAG-="seat", TAG-="master-of-seat"
   ACTION=="add|change", SUBSYSTEM=="drm", KERNEL=="renderD*", KERNELS=="0000:01:00.0", ENV{ID_FOR_SEAT}="", TAG-="seat"
   ```

2. **Aislamiento Vulkan en el Host (`/etc/environment`)**:
   El host ignora la tarjeta AMD mediante variables de entorno globales:
   ```ini
   VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/intel_icd.json
   MESA_VK_DEVICE_SELECT=8086:7d51!
   DRI_PRIME=0
   ```

3. **Mapeo DRI en Docker (`docker-compose.yml`)**:
   El contenedor accede a los dispositivos DRI y se vincula a la RX 480 por ID de dispositivo (`MESA_VK_DEVICE_SELECT=1002:67df!`):
   ```yaml
   devices:
     - /dev/dri:/dev/dri
     - /dev/kfd:/dev/kfd
   ```

4. **Namespace IPC separado** (sin `ipc: host`):
   El GUI del host no detecta el LM Studio del contenedor como instancia local.
   LMLink los conecta por red TCP (puerto 41344) como maquinas separadas.

## Endpoints

| Servicio | Puerto | URL |
| :--- | :--- | :--- |
| LM Studio Host (Intel Arc) | 1234 | `http://127.0.0.1:1234/v1` |
| LM Studio RX480 (Contenedor) | 1235 | `http://127.0.0.1:1235/v1` |
| LMLink | 41344 | TCP |

## Instalacion y Construccion desde Cero

1. **Crear la Imagen Base** (`Dockerfile`):
   ```bash
   docker build -t lms-node-amd:latest .
   ```

2. **Instalar LM Studio Headless** (dentro del contenedor o via volumen):
   ```bash
   curl -fsSL https://lmstudio.ai/install.sh | sh
   /root/.lmstudio/bin/lms bootstrap -y
   ```

3. **Autenticacion Inicial en LMLink** (guardada en volumen persistente):
   ```bash
   docker volume create lms_amd_profile
   docker run --rm -it -v lms_amd_profile:/root/.lmstudio lms-node-amd:latest /root/.lmstudio/bin/lms login
   ```

4. **Iniciar el contenedor**:
   ```bash
   docker compose up -d
   ```

## Resultados del Benchmark GPU AMD RX 480

Resultados reales de inferencia en el contenedor acelerado por Vulkan sobre AMD Radeon RX 480 (8 GB):

| Modelo | Tamano / Cuantizacion | Rendimiento (tok/s) |
| :--- | :--- | :--- |
| Qwen 2.5 3B Instruct | 3B (Q4_K_M) | 58.99 tok/s |
| Hermes 3 (Llama 3.2 3B) | 3B (Q4_K_M) | 58.14 tok/s |
| Gemma 4 E4B IT | 7.5B (Q4_K_M) | 31.51 tok/s |
| Qwen 2.5 Coder 7B | 7B (Q4_K_M) | 28.56 tok/s |
| Ministral 8B Instruct | 8B (Q4_K_S) | 28.26 tok/s |
| Meta Llama 3.1 8B | 8B (Q4_K_M) | 25.57 tok/s |

## Dispositivos en LMLink

- **LM Studio (Desktop Host)**: Intel Arc integrada como `Khazad-dum-Intel-ARC` (puerto 1234)
- **Khazad-dum-AMD-eGPU (Contenedor)**: RX480 como nodo independiente en LMLink (puerto 1235)
### Resultados del Benchmark Actualizados (Septiembre 2026)
Con `gpu-watchdog` configurado a 300s y aislamiento de Vulkan activo:

| Modelo | Clave LMS | Rendimiento (tok/s) | Tokens Generados | TTFT | Tiempo Carga |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Llama 3.2 1B Instruct** | `llama-3.2-1b-instruct` | **94.24 tok/s** | 375 | 0.065s | 1.6s |
| **Llama 3.2 3B Instruct** | `llama-3.2-3b-instruct` | **44.37 tok/s** | 297 | 0.257s | 7.2s |
| **Qwen 2.5 3B Instruct** | `qwen2.5-3b-instruct` | **43.32 tok/s** | 366 | 0.166s | 8.2s |
| **Qwen 2.5 Coder 7B** | `qwen2.5-coder-7b-instruct` | **22.10 tok/s** | 360 | 0.351s | 14.2s |
| **Meta Llama 3.1 8B** | `meta-llama-3.1-8b-instruct` | **21.81 tok/s** | 277 | 0.965s | 14.9s |
| **Ministral 8B Instruct** | `ministral-8b-instruct-2410` | **21.07 tok/s** | 265 | 0.329s | 3.1s |
| **Qwen 1.5 MoE A2.7B Chat** | `qwen1.5-moe-a2.7b-chat` | **11.43 tok/s** | 409 | 0.824s | 16.2s |

