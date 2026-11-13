# HOWTO: hacer funcionar una Tesla V100 PCIe (32 GB) en este stack

Guía de puesta en marcha de la V100 que sirve los modelos en `:8090`
(`ninfer-serve`). Va de "tarjeta en la mano" a "cliente preguntando", con los
errores reales que esta config produce y sus arreglos medidos.

## 0. Qué es esto

Tesla V100 PCIe 32 GB (`10de:1db6` GV100GL, sm_70/Volta, 250 W) en el host
GPU del homelab, con driver 580.178.04, servida por **ninfer** (motor propio,
no llama.cpp ni vLLM) dentro de un contenedor, refrigerada con un ARCTIC
S12038-8K pilotado desde un Raspberry Pi ([`v100-fan/`](v100-fan/README.md))
y telemetrizada en HTTP para ese mismo controlador.

## 1. Hardware (no negociable)

- **Slot PCIe x8/x16 real.** La V100 expone un **BAR1 de 32 GiB prefetchable**:
  controladores que no puedan mapear ventanas de 32 GiB la matan en enum — el
  `brcm-pcie` del Raspberry Pi 5 (M.2) es uno probado: `BAR 1: can't assign;
  no space`, ni con dtoverlays de `ranges`/`dma-ranges`. Nada de risers M.2 en
  Pi ni docks sin root-port decente.
- **8 GT/s x4 funciona para inferencia**: decode/prefill son bound a la GPU;
  el link solo se nota al cargar los 20–28 GB del modelo desde el host.
- **Aire o muerte.** La V100 PCIe es pasiva. Sin ventilador dedicado: FP64 a
  tope la deja a 82 °C con SW Thermal Slowdown y, sobre todo, **Xid 79
  ("GPU has fallen off the bus") a partir de ~80 °C sostenidos — también en
  reposo**. El montaje de `v100-fan/` mantiene 185 W sostenidos a 71 °C.
- **Alimentación**: 8-pin PCIe propio, fuente/PSU de 12 V real. En SBC el
  síntoma de famearla es `Can't find an IRQ for your NVIDIA card!` +
  `lspci -vv` con `!!! Unknown header type 7f` y link vacío: es voltaje, no IRQ.

## 2. Driver del host

```bash
# blacklist nouveau ANTES de nada:
printf "blacklist nouveau\noptions nouveau modeset=0\n" | sudo tee /etc/modprobe.d/blacklist-nouveau.conf
# initramfs regenerado + reboot, y Data Center Driver (branch 580.x):
sudo nvidia-smi   # debe listar "Tesla V100-PCIE-32GB" y driver 580.178.04
```

Si `nvidia-smi` responde `Unable to determine the device handle ... Unknown
Error` dentro del host **y** dentro de un `docker run --device
nvidia.com/gpu=all`, la GPU está colgada: `sudo nvidia-smi -r -i <bdf>` o
reboot; reiniciar contenedores no la levanta.

## 3. Docker + CDI

`nvidia-container-toolkit` con modo CDI. **En este host `--gpus all` falla**
(spec CDI de la AMD iGPU ausente): usar siempre el device CDI explícito:

```bash
docker run --rm --device nvidia.com/gpu=all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
```

Ojo con handles ocultos que impiden descargar `nvidia_uvm` tras un cuelgue:
`openvino-embedding.service` abre `/dev/nvidia*` aunque calcule en Intel —
tiene drop-in `CUDA_VISIBLE_DEVICES=""` para evitarlo.

## 4. El motor: ninfer (sm_70)

ninfer es un runtime narrow con artefactos `.ninfer` (pesos + tokenizer +
chat template + torre de visión empaquetados). Para V100 hay que usar un
**build con kernels sm_70** — los binarios oficiales/sm_75+ fallan con
`no kernel image is available for execution on the device`.

- Imagen autocontenida de producción: `ninfer-v100:1.7.2-sm70` en el registry
  local (`<REGISTRY>:5555`, insecure HTTP). Lleva los 5 binarios en
  `/build/apps/`. Un tag en el registry NO garantiza motor dentro: comprobar
  `docker run --rm <img> sh -c 'find / -maxdepth 4 -name ninfer-serve -type f'`.
- Build desde fuente (fork sm_70 en `~/produccion/ninfer-v100/src`) dentro de
  un contenedor `nvidia/cuda:12.8.1-devel-ubuntu24.04`:

```bash
cmake -S . -B build-v100 -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc -DCMAKE_CUDA_ARCHITECTURES=70
ninja -C build-v100 -j16   # -> build-v100/apps/{ninfer,ninfer-serve,ninfer-perplexity}
```

## 5. Modelos (.ninfer)

- Cabecera mágica: `NINFER\x00\x02` = v2, `...\x00\x03` = v3 (mirar con
  `xxd -l 8`). El build Volta histórico lee **v2**; los publicados recientes
  son v3. `tools/upgrade_ninfer_v2_to_v3.py` convierte v2→v3 preservando
  pesos (35 min para 22 GB, I/O puro). Convertir GGUF→ninfer:
  `~/venvs/v100/bin/python -m tools.convert --recipe qwen3_6_35b_a3b
  --proposal --components text,vision,mtp ...` (sin `--proposal` no existe
  proposal head y `--lm-head-draft` muere en 1 s).
- Verificar sha256 contra el `SHA256SUMS` del MISMO commit antes de servir;
  `.ninfer` con talla correcta pero sha malo = reanudación sobre fichero
  disperso corrupto, rebajar entero.

## 6. Servir (este repo)

El contenedor `ninfer-serve` es solo jail (`command: sleep infinity`,
perfil `ninfer` del compose, host network, mounts RO de `models/` y
`grafts/`). El motor lo lanza un script:

```bash
./ninfer-serve.sh [modelo.ninfer] [ctx] [conc] [draft]   # mono-modelo, id fijo "ninfer"
./ninfer-router.sh                                       # catalogo: preset ninfer-router.ini, ids por seccion
./ninfer-mgr.sh list|status|load|unload|restart          # operaciones sin sudo
systemctl start ninfer-serve                             # arranque tras reboot (unit oneshot RemainAfterExit)
```

- **`ninfer-serve` escucha 127.0.0.1 por defecto**: pasar `--host 0.0.0.0`.
- El router usa `--models-max 1` y descarga (nunca suspend: Gemma no soporta
  suspend y el swap revienta con `cuMemCreate OUT_OF_MEMORY`). Swap real: 45–70 s.
- Stop/kill **siempre con patrón anclado** `pgrep -f '^/build/apps/ninfer-serve'`:
  el patrón suelto coincide con el `sh -c` que lo lanza y se auto-mata
  (`status=143` en la unit).
- Un contenedor `Up` no significa nada (su CMD es `sleep`); y `active (exited)`
  tampoco: verificar abajo.

## 7. Verificación (la prueba real, 3 pasos)

```bash
nvidia-smi --query-gpu=temperature.gpu,clocks.sm,power.draw,clocks_throttle_reasons.active --format=csv
curl -sS --max-time 5 http://127.0.0.1:8090/v1/models
curl -sS --max-time 120 http://127.0.0.1:8090/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"ninfer","messages":[{"role":"user","content":"Responde solo: OK"}],"max_tokens":8,"temperature":0}'
```

`0 MiB` en `memory.used` = motor muerto. Throttle signature en plena medida
(clock clavado ~135 MHz, 40 W, ≥80 °C, razón `0x20`): **no anotar números de
un bench throttleado** — colapsan a 1/10 y parecen plausibles.

## 8. Números medidos en esta tarjeta (para saber si lo que ves es normal)

ninfer CLI greedy / streaming, español real:

| artefacto | prefill | decode | VRAM |
|---|---:|---:|---:|
| gemma-4-26B-A4B (prod) | 1437 tok/s | **207 tok/s** | ~20 GiB |
| qwen3.6-35b-a3b v2 | 284–799 | 136–155 | 31,4 GiB (¡va al límite!) |
| qwen3.8-35b-a3b-distill | 270 | 135 | ~22 GiB |

Carga de pesos desde SATA: ~400 MiB/s (36–75 s) con disco libre; con I/O
paralela 30–60 MiB/s y la misma carga se va a 20–35 min. No confundir con
modelo colgado: mirar `vmstat` primero.

## 9. Errores conocidos y su causa

| síntoma | causa | arreglo |
|---|---|---|
| `no NVIDIA GPU found` / `Can't find an IRQ` + `header type 7f` | sin 12 V o riser sin alimentación | física, no software |
| Xid 79 / tarjeta desaparece (~80 °C) | refrigeración insuficiente | `v100-fan/`; si repite en reposo, montaje/pasta |
| `artifact magic is not NInfer v2` | artefacto v3 con build v2-only | convertir con `upgrade_ninfer_v2_to_v3.py` o bajar el commit v2 |
| `missing Use text/layers/...gate@` en 0,5 s | artefacto no encaja con el plan del build | probar CLI pelado; si repite, cambiar artefacto |
| `entry length differs from directory` | `.ninfer` truncado | comparar tamaño real con HF y rebajar |
| `cudaGetDeviceCount failed: cudaErrorNoDevice` en 2 s | GPU caída del bus (Xid 79) | reset/reboot, no reiniciar contenedor |
| `no kernel image is available` | binario sin sm_70 | imagen `1.7.2-sm70` / recompilar |
| LM Studio marca el runtime CUDA "Non Compatible" | catálogo sin Volta y sin cubins sm_70 | engine propio con CUDA 12.x compilado para 7.0 |
| el llama-server de Hermes "descarga sin fin" un modelo de la V100 | su build trae sm_75 → bucle spawn-abort | la V100 se sirve por ninfer, nunca por ese runtime |
| `HTTP 400 context length exceeded` a mitad de stream | `--default-max-tokens` (8192) reservado del contexto | subir `--max-context` en el motor y espejarlo en el cliente |

## 10. Enlaces

- Ventilador y curva silencio: [`v100-fan/README.md`](v100-fan/README.md)
- Contrato de malla/clients: [`MESH.md`](MESH.md)
- Perfil Hermes para la V100 (por qué 64k mínimo): [`docs/perfil-v100-hermes.md`](docs/perfil-v100-hermes.md)
- Benchmarks completos: `benchmark/`
