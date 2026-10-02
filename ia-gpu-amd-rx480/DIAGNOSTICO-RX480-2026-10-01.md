# Diagnóstico RX 480 (Polaris) — Khazad-dum — 2026-10-01

## Resumen ejecutivo

El síntoma reportado ("la RX480 crashea al usarla en `ia-gpu-amd-rx480`") tiene
**dos causas independientes**, ambas identificadas y verificadas. La principal
NO es un fallo de hardware:

### Causa A — cadena de falsos positivos del watchdog que acababa zombificando la GPU

```
conky (cada 3 s)
  └─ get_amdgpu_val.sh GPU_POWER lee hwmon/power1_average|power1_input
       └─ el driver pide el registro de potencia a la SMU (msg 0x282)
            └─ en Polaris vía riser x4 esa lectura falla intermitentemente
                 └─ kernel: "amdgpu 0000:01:00.0:" + "last message was failed ret is 0"
                      └─ gpu-watchdog trataba ESE mensaje como "fallo de enlace/anillo GPU"
                           └─ a los 3 avisos → kill_gpu_processes + full_recovery
                                └─ full_recovery → SBR (setpci al bridge 00:06.0)
                                     └─ el ATOM BIOS no se re-inicializa tras SBR
                                        con el bus alimentado
                                          └─ GPU ZOMBIE hasta cold boot manual
```

### Causa B — falsos positivos del watchdog en el arranque (independiente de A)

```
arranque del host
  └─ dmesg contiene 2× "amdgpu 0000:01:00.0: GPU recovery disabled."
     (una vez por arranque, parámetro gpu_recovery=0)
       └─ el watchdog hacía `dmesg --time-format=delta | tail -100` SIN filtro
          temporal, así que esas 2 líneas viejas entraban en CADA pasada
            └─ fail_msg_count +1 cada 3 s → ≥3 en ~9 s
              └─ full_recovery + SBR en el primer minuto tras arrancar
```

Además `initial last_busy=-1` hacía que con la GPU idle (busy=0) el contador de
stall (120 s) arrancase a contar solo → "GPU zombie" a los 2 minutos de arrancar.

**Evidencia del falso positivo (log real):**

```
[gpu-watchdog] 2026-10-01 19:31:47   Fallo de enlace/anillo GPU detectado (contador: 1/3)
[gpu-watchdog] 2026-10-01 19:31:47 ALERTA CRITICA: GPU failure detectada en kernel log!
[gpu-watchdog] 2026-10-01 19:31:47 ACCION: mitigando procesos en GPU RX480...
[gpu-watchdog] 2026-10-01 19:31:47   Forzando docker stop ia-gpu-amd-rx480 (timeout=5s)...
[gpu-watchdog] 2026-10-01 19:31:56   GPU no responde (zombie). Saltando fuser para evitar D-state hang.
[gpu-watchdog] 2026-10-01 19:32:03 RECUPERACIÓN: Intento 1 (máx 3 antes de cold reboot)
[gpu-watchdog] 2026-10-01 19:32:03   Paso 1: Unbind amdgpu (config space válido)...
```

Es decir: **un dato cosmético de conky (potencia) terminaba matando la GPU**.

### Causa C — el fallo real bajo carga sostenida: ring timeout en cómputo (hardware/driver)

Al margen del watchdog, la tarjeta **sí** tiene un fallo propio de cómputo. Lo
reproduje con un soak de 15 min de inferencia continua:

```
amdgpu 0000:01:00.0: ring comp_1.2.0 timeout, signaled seq=4676674, emitted seq=4676677
amdgpu 0000:01:00.0: GPU recovery disabled.
amdgpu 0000:01:00.0: Dumping IP State
amdgpu 0000:01:00.0: [drm] AMDGPU device coredump file has been created
```

Coredump (`/home/arkantu/workspace/gpu-coredump-2310.bin`):

```
process_name: llama-server.re PID: 2263917
Ring timed out details
IP Type: 1 Ring Name: comp_1.2.0
[gfxhub] Page fault observed
Faulty page start: 0x0   Protection fault status: 0x0
vbios: 67DFHB.15.50.0.0.AS39
```

Es **un page fault del GFXHUB en el anillo de cómputo** que el kernel no puede
recuperar (`gpu_recovery=0`, y con razón: el reset no funciona en esta Polaris).
Aparece tras ~10-13 min de decodificación continua (≈1500-2000 peticiones).

> Nota: la sesión de soak terminó justo cuando el usuario tuvo un problema con la
> fuente de alimentación. Ese evento concreto NO se usa como evidencia; el ring
> timeout y el coredump son anteriores a esa caída.

## Pruebas de que la GPU está sana a corto/medio plazo

- Cargas sostenidas de cientos de peticiones sin error antes del ring timeout.
- 1310 MHz / 2000 MHz estables, 58-61 °C.
- **No hay ni un solo ring timeout ni GPU reset en todo el histórico previo** de
  kern.log: el "hang sistemático a los 15 min" que motivó retirar la GPU de
  producción era la Causa A/B (watchdog), no el hardware.
- Enlace PCIe correcto: x4 (M.2/OCuLink, esperado), 8 GT/s, sin errores fatales
  AER (solo `AdvNonFatalErr+`, asesor y enmascarado).
- Sin correlación con los resets ATA del Penta SATA HAT (NCQ off sigue aplicado).

## Config muerta detectada (verificada, no supuesta)

| Variable | Veredicto | Prueba |
|---|---|---|
| `RADV_PERFTEST=no_sam` | **No existe** | `strings libvulkan_radeon.so \| grep -x no_sam` → 0; el token real es `nosam` |
| `RADV_PERFTEST=nosam` | No-op en Mesa 25.2.8 | en esta versión ya no es perftest sino driconf option; `vulkaninfo` sigue con `sparseResidency=true` |
| `GGML_VK_ALLOW_EXOTIC_SUBGROUP_OPS` | **No existe** | `strings libggml-vulkan.so` → 0 ocurrencias |
| `DRI_PRIME=1` | Inocuo pero inútil | `MESA_VK_DEVICE_SELECT=1002:67df!` ya fija el dispositivo |

## PRT / sparse residency (hallazgo importante)

`vulkaninfo` **no es una comprobación inocua**: crea un device Vulkan con sparse
residency → `amdgpu_gem_va_ioctl` → `gmc_v8_0_set_prt(1)` → el kernel imprime

```
amdgpu 0000:01:00.0: Disabling VM faults because of PRT request!
```

y **desactiva las VM faults para TODO el arranque** (no hay "Re-enabling VM
faults" en el driver). Quita la red de seguridad del kernel ante page faults.
Verificado con ftrace/kprobe sobre `gmc_v8_0_set_prt`:

```
gmc_v8_0_set_prt ← amdgpu_vm_bo_insert_map ← amdgpu_vm_bo_map ← amdgpu_gem_va_ioctl
```

`llama-server.real` **no** crea sparse resources (0 llamadas a `set_prt` en carga
+ inferencia), así que la ruta de producción no dispara el PRT.

## Cambios aplicados

1. **`~/.config/conky/get_amdgpu_val.sh`** — `GPU_POWER` devuelve `0` sin tocar
   el kernel (elimina el trigger del error SMU, Causa A). El resto de métricas
   (`GPU_BUSY`, `GPU_TEMP`, `GPU_FREQ`, `VRAM_*`) quedan intactas: son lecturas
   seguras, verificadas sin efecto sobre la SMU.
2. **`/usr/local/sbin/gpu-watchdog.sh`** (backup `.bak.pre-fix-202610012301`)
   - `check_ring_timeout` **solo** considera fatales `ring.*timeout` y
     `GPU reset begin`. El fallo de mailbox SMU es informativo y ya no acumula
     hacia recuperación; `device lost from bus` (cierre normal de cliente Vulkan)
     y `GPU recovery disabled` (texto de arranque) se ignoran. → mata Causa A y B.
   - `last_busy` inicial `0` (antes `-1`) y `0` tras recuperación → sin falso
     "zombie" en idle.
   - **`full_recovery` ya no hace unbind ni SBR** salvo `ALLOW_SBR=1`. Con
     `ALLOW_SBR=0` (por defecto) solo detiene la carga y pide cold boot. Motivo:
     ambos dejan esta Polaris fuera de servicio.
3. **`/etc/systemd/system/gpu-watchdog.service`** (backup `.bak.pre-fix-20261001`)
   - `Environment=GPU_WATCHDOG_ALLOW_SBR=0` explícito.
   *El servicio sigue `disabled` (quedó parado tras el incidente de alimentación
   y las pruebas). Activarlo con `systemctl start gpu-watchdog` tras el cold boot.*
4. **`docker-compose.yml`** — `ia-gpu-amd-rx480`: `restart: "no"` (se había
   reactivado y hubo que revertirlo, ver abajo) y eliminadas
   `RADV_PERFTEST=no_sam` + `GGML_VK_ALLOW_EXOTIC_SUBGROUP_OPS` con nota.
5. **`ia-gpu-amd-rx480/entrypoint.sh`** — eliminados los dos `export` muertos y
   documentado el token real y el aviso sobre `vulkaninfo`.

## Verificación

| Comprobación | Resultado |
|---|---|
| `bash -n` de los scripts | OK (origen y destino) |
| `docker compose config` + parseo YAML | OK; solo variables válidas |
| Test del patrón fatal con dmesg real | `GPU recovery disabled` presente → 0 fatales |
| Watchdog corriendo con el fix | sin recuperaciones falsas en 20 s |
| Detección del ring timeout real | ✅ la disparó correctamente (Causa C) |
| `full_recovery` con `ALLOW_SBR=0` | detiene la carga, no toca el hardware |

## Incidente durante la sesión (transparencia)

Durante el soak hubo una **caída de la fuente de alimentación** (según el
usuario). Consecuencia: la GPU quedó wedgeada (config space `ffffffff`, 16
kworkers `ttm` en D-state). Como el compose estaba en `unless-stopped`, el script
de autostart la reinició en bucle cada 6 s hasta que lo paré. Por eso
`restart` quedó en `"no"` de nuevo.

**Secuencia recomendada tras el cold boot:**

1. `sudo systemctl start gpu-watchdog`  (con `ALLOW_SBR=0`)
2. `cd /home/arkantu/produccion/ia-services-stack && docker compose up -d ia-gpu-amd-rx480`
3. Cargar el modelo y hacer un soak corto (10-15 min) confirmando que **no**
   aparecen `ring .* timeout` ni `[gfxhub] Page fault` en `dmesg`.
4. Si el soak pasa: cambiar `restart` a `unless-stopped`.

## Pendiente / a vigilar

- **Causa C (ring timeout de cómputo) sigue sin resolver**: no es de software de
  configuración. Opciones a explorar, por orden de coste:
  a) Bajar la agresividad del compute: `--flash-attn off` ya está; probar
     `GGML_VK_DISABLE_FUSION`, `GGML_VK_DISABLE_MMVQ`, `GGML_VK_DISABLE_COOPMAT*`
     (existen en `libggml-vulkan.so`) para reducir presión sobre el anillo de
     cómputo.
  b) Limitar `--n-gpu-layers` (dejar capas en CPU) y ver si el ring aguanta.
  c) Reducir `-c` (contexto) y `-b/-ub` (ya capados en el wrapper).
  d) Como parche de operación: **reiniciar el contenedor cada N minutos** antes
     de que el ring se agote (~10 min medidos). Sucio pero efectivo y sin tocar
     hardware.
  e) Hardware: revisar la alimentación/riser (coincide con que el usuario acaba
     de tener un problema de fuente). Un page fault a 0x0 en GFXHUB con
     protección 0 encaja con problema de enlace/alimentación o VRAM.
- El error de mailbox SMU seguirá apareciendo si **algo** lee `power1_*`
  (`sensors`, `psensor`, `rocm-smi` manual). El watchdog ya no lo considera fatal.
- DPM: `auto` es lo correcto. **No existe `pp_od_clk_voltage`** en esta tarjeta
  (driver sin soporte OD para este vBIOS) → **no se puede hacer undervolt**.
- `gpu-recovery.sh` (`gpu-recovery.service`, también `disabled`) arranca con SBR
  como primera estrategia. Si algún día se activa, aplicar el mismo criterio:
  **SBR no recupera esta GPU**.
- Restos de la sesión de pruebas: `/home/arkantu/workspace/gpu-coredump-2310.bin`
  (coredump de GPU, 5,7 MB) y `soak.log`/`soak.out`.
