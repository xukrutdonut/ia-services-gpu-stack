# COMPLETADO — Bench seguro RX 480 (handoff cold boot)

**Fecha del handoff:** 2026-10-04 · **Resuelto:** 2026-10-04 ~11:04.
**Disparador:** el cold reboot físico (solo el usuario puede hacerlo).

> **RESUELTO.** Cold boot a las 10:55 → arranque limpio (sin ring timeouts, PRT
> ni `failed ret is 65535`). Contenedor recreado con la imagen nueva (`python3`
> + montajes) y publicado en el registry `.101:5555/ia-gpu-amd-rx480:{latest,2026-10-04}`.
> `--check` → LISTO, canary OK, bench completo OK (3/3 modelos). Sin timeouts
> tras la corrida, `devcoredump` vacío, sclk coherente. Medidas reales en el
> README y en `results/bench-20261004-090402.{json,md}`. `gpu-watchdog` v5.8
> activo (ALLOW_SBR=0). **Causa C cerrada el mismo día** (ver §7): 15 min de
> cómputo sostenido sin señales de GPU.
> **Fix aplicado en este handoff:** el preflight trataba `GPU recovery disabled.`
> como fatal (falso positivo en cada arranque con `gpu_recovery=0`); ahora es
> ruido informativo y el harness también emite informe `.md`.

---

## 1. Estado de partida (por qué hay que reiniciar)

- Wedge iniciado a las **09:14** con `amdgpu 0000:01:00.0: Disabling VM faults because of PRT request!`
  (disparado por un `vulkaninfo --summary` de reconocimiento) y bucle de
  `ring comp_1.x.x / gfx timeout` con **GPU recovery disabled**.
- **SMU sin responder**: ~1663 líneas `failed ret is 65535`; `pp_dpm_sclk` vacío.
- **4217 MiB ocupados en VRAM** (modelo que quedó cargado al morir el server).
- El contenedor `ia-gpu-amd-rx480` **no se puede parar ni recrear**: hay un
  proceso dentro atascado en estado `D` sobre el ioctl de la GPU y Docker
  responde `did not receive an exit event`. Se limpia solo con el cold boot.
- `unbind` / SBR **no** recuperan esta Polaris (el ATOM BIOS no reinicializa).

Ya está todo **escrito y construido**; solo falta que el arranque limpio permita
aplicarlo. Detalle en la sección 5.

---

## 2. Pendiente: pasos tras el cold boot (en orden)

```bash
# 0) (opcional) confirmar arranque limpio -> no debe imprimir nada
journalctl -k -b --no-pager | grep -E 'ring .* timeout|PRT request|GPU recovery'

# 1) recrear el contenedor (aplica imagen nueva con python3 + montajes)
cd ~/produccion/ia-services-stack
docker compose up -d ia-gpu-amd-rx480

# 2) verificar que los montajes y python3 llegaron
docker exec ia-gpu-amd-rx480 sh -lc \
  'python3 --version; ls /opt/bench; ls /opt/llama-bench/llama-b11382; ls -d /opt/bench-results'

# 3) preflight -> debe salir "VEREDICTO: LISTO" y exit 0
cd ia-gpu-amd-rx480 && ./bench-run.sh --check

# 4) (opcional) ver el plan sin tocar la GPU
./bench-run.sh --dry-run

# 5) canary: prueba de vida sobre el modelo mas pequeño (carga minima)
./bench-run.sh --canary-only

# 6) bench completo (3 modelos, ~2-4 min)
./bench-run.sh
```

Si el paso **5** falla, **parar ahí**: la tarjeta no está sana y hay que volver
al cold boot. No saltarse el canary.

### Criterio de éxito
- `results/bench-<fecha>.md` y `.json` con `pp`/`tg` reales de los 3 modelos.
- Sin nuevas líneas `ring ... timeout` en `journalctl -k -b` durante/tras la corrida.
- `radeontop` (con GPU sana) muestra carga coherente, no un clock de GHz absurdos.

Al terminar, anotar los `pp/tg` medidos en el README y borrar las tablas
históricas o marcarlas como superadas.

---

## 3. Reglas de medida (no negociables)

| Regla | Motivo |
|---|---|
| **Nunca** `vulkaninfo` en esta máquina | re-dispara el PRT → wedge directo |
| Carga real con `radeontop` | sysfs `gpu_busy_percent` = **0% falso** en cómputo Vulkan Polaris |
| `radeontop` solo con GPU sana | con la SMU muerta devuelve basura (clock 7158 GHz) |
| VRAM/`pp_dpm_sclk` por sysfs: OK | son atributos DRM, no tocan la SMU |
| temp/fan/potencia solo con `--telemetry` | hwmon pasa por la SMU → spam/`failed ret` si se abusa |
| Kernel log con `journalctl -k` | `sudo dmesg` pide password; `journalctl -k -b` funciona sin sudo |

---

## 4. Si se vuelve a colgar

1. El harness **aborta y no reintenta** (exit `2` timeout / `3` wedge). No hay
   bucle de reintentos: eso era lo que re-expiraba el timeout.
2. Confirmar con `journalctl -k -b --no-pager | grep -E 'ring .* timeout|failed ret'`.
3. **No insistir**: cada submission nueva re-expira el ring timeout y no hay
   recuperación en caliente. Recuperación = cold boot.
4. Si el contenedor vuelve a quedar atascado, no perder tiempo con
   `docker stop/rm -f`: el cold boot lo limpia y `docker compose up -d` recrea.
5. No volver a lanzar `vulkaninfo` "para comprobar": es lo que lo dispara.

---

## 5. Ya hecho y verificado (no pendiente)

- `bench/bench_rx480.py`: lock `flock` exclusivo, GPU libre
  (`lms unload --all`, rechaza `llama-server.real` vivo), canary, carga mínima
  `-p 64 -n 32 -r 1 -b 64 -ub 16`, timeout duro por modelo sin reintentos,
  vigilancia de `devcoredump`, aborto con exit `3` si la GPU no suelta.
- `bench-run.sh`: preflight + `docker compose up -d` + `docker exec`.
- `bench-fetch-bin.sh`: baja llama-bench `b11382` si falta (bench-bin/ fuera de git, 86 MB).
- `Dockerfile`: `python3`; `docker-compose.yml`: montajes `bench/`, `bench-bin/`,
  `results/`; `lms_amd_profile` es volumen con nombre → recrear **no** borra
  llmster ni el backend Vulkan.
- **Preflight verificado contra el wedge real**: detecta los 23 ring timeouts,
  las ~1663 líneas de SMU muerta y el `sclk` vacío → `SUCIO`, exit 1.
- **Dry-run verificado dentro de la imagen**: encuentra el binario y los 3 modelos.

---

## 6. Artefactos

```
ia-gpu-amd-rx480/
├── bench/bench_rx480.py        # harness (host --check / contenedor bench)
├── bench-run.sh                # lanzador (diagnostico previo + compose + exec)
├── bench-fetch-bin.sh          # baja llama-bench b11382 si falta
├── bench-bin/                  # (git-ignored) llama-b11382/ + .so
├── results/                    # (git-ignored) bench-<fecha>.{md,json}
├── DIAGNOSTICO-RX480-2026-10-01.md
└── README.md                   # seccion "Bench seguro (bench-run.sh)"
```

Commits: `8dd88dd` en `main` (github.com/xukrutdonut/ia-services-gpu-stack).

---

## 7. Cierre de la Causa C (estrés sostenido) — 2026-10-04

El bench normal es de carga mínima y por diseño **no** ejercita el fallo por
cómputo sostenido (~10-13 min) que wedgeaba la tarjeta. Se añadió `--stress`:

```bash
cd ~/produccion/ia-services-stack/ia-gpu-amd-rx480
./bench-run.sh --stress                          # 15 min por defecto
./bench-run.sh --stress --stress-minutes 30      # por si se quiere más
```

Qué hace: bucle de chunks `-p 512 -n 1024 -r 1` (submits largos, sin la
"carga mínima" del bench normal) hasta agotar `--stress-minutes`, con
**vigilancia del kernel log dentro del contenedor** (`cap_add: SYSLOG` añadido al
servicio → `dmesg -T` funciona; antes no había ni `journalctl` ni permiso).

Aborta (exit `2`) al primer evento fatal *nuevo*: `ring ... timeout`,
`Disabling VM faults`, `GPU reset`, `[gfxhub] Page fault`, o >5 líneas nuevas de
`failed ret is 65535`. También si el `tg` cae por debajo del 50 % de referencia
durante 2 chunks seguidos (degradación) o si un chunk se cuelga / la GPU no
suelta (exit `3`, cold boot).

**Resultado** (`results/stress-20261004-093228.{json,md}`): modelo `1.7b`,
905 s sostenidos, 26 chunks. `pp512` 185→167 tok/s (-10 %), `tg1024` 44.6→42.3
(mediana 43.6), VRAM 1182 MiB, sclk 910-1310 MHz. **0 eventos fatales**,
`devcoredump` vacío, SMU viva (46 C), contenedor `healthy` al acabar.

**Veredicto: Causa C NO reproducida con las mitigaciones actuales**
(`GGML_VK_MAX_NODES_PER_SUBMIT=1` + `GGML_VK_DISABLE_ASYNC=1`).

> **Honestidad del resultado:** esto valida que *con* mitigación la tarjeta
> aguanta — no que la mitigación sea lo que lo evita. La ablation (repetir el
> estrés **sin** las `GGML_VK_*`) sigue sin hacer porque un wedge exige cold boot
> (reinicio físico). Queda como decisión del usuario.

---

## 8. Contexto de OLMoE en OpenWebUI/OpenInterpreter. Decision final: 16384

**Estado: OLMoE a 16384 tokens, KV en f16** (suelo por modelo en el wrapper,
`entrypoint.sh`, bloque `--ctx-size`). Medido en vivo: 6,40 GB de VRAM usados de
8 GiB (74%), 2,2 GB libres.

**Hecho duro:** OLMoE-1B-7B-Instruct tiene `n_ctx_train = 4096` (lo declara el
GGUF). No es un limite de config: por encima de 4096 el modelo extrapola RoPE y
la calidad degrada. La config solo decide si la API **rechaza** el prompt
(ctx 4096) o lo **acepta degradado** (8192+).

**Por que hay suelo:** LM Studio pide 4096 a OLMoE (`max_context_length` del
GGUF), y con 4096 OpenWebUI/OpenInterpreter rechazan cualquier prompt >4096. El
wrapper eleva la peticion para que no fallen. Suelo por modelo:

- Embeddings: sin suelo (ctx nativo corto; forzar 16k solo gastaria VRAM).
- OLMoE: 16384.
- Resto (Qwen3-1.7B, Qwen3-0.6B): 16384 de suelo; LM Studio ya les pide 32768,
  asi que en la practica no les afecta.

**VRAM medida (RX480 8 GiB = 8,59 GB decimales). KV de OLMoE = 128 KB/token**
(16 capas x 16 cabezas KV x 128 dim x 2 (K+V) x 2 B; pesos Q4_K_M ~4,0 GB):

  config                       VRAM usada   libre      resultado
  OLMoE @8192   KV f16          5,02 GB     3,6 GB    OK
  OLMoE @16384  KV f16          6,40 GB     2,2 GB    OK  <- ELEGIDO (74%)
  OLMoE @32768  KV f16          8,0+ GB     --        NO ARRANCA ("Engine
                                                      protocol startup was
                                                      aborted")
  OLMoE @32768  K/q8_0,V/f16    7,75 GB     0,45 GB   OK pero DESCARTADO (94%)

**Por que 16384 y no 32768:**

1. OLMoE no gana calidad por encima de 4096; 16384 ya es 4x su contexto de
   entrenamiento y cubre de sobra system prompt + esquemas de tools + historial.
2. A 32768 el margen cae a ~0,45 GB, y este contenedor tiene un fallo conocido
   de oversubscription: el wrapper puede lanzar 2-3 `llama-server.real` en
   paralelo por la carrera de arranque, y con la VRAM al limite eso da
   "Engine protocol startup was aborted" mas teardown sucio del kernel
   ("failed to clear page tables on GEM object close (-512)", "leaking bo va"),
   que es el estado previo a un wedge que exigiria cold boot fisico.

Para volver a 32768 hay que forzar `--cache-type-k q8_0` (V no se puede
cuantizar: exige `--flash-attn`, aqui forzado off) y aceptar el margen de
0,45 GB. Estuvo configurado y verificado funcionando el 2026-10-04; se descarto
por el margen.

**Para contexto largo de verdad** (OpenInterpreter/OpenWebUI mandan system prompt
+ esquemas de tools) el modelo correcto es **Qwen3-1.7B**, nativo 32768, ya
servido en :1235.

**Aparte, preexistente y pendiente:** la carrera de arranque del wrapper hace
que el primer request tras cambiar de modelo pueda devolver 400
`{"error":"terminated"}` o HTTP 000. El reintento entra siempre (verificado en
todas las pruebas del 2026-10-04). Afecta a OpenWebUI/OpenInterpreter al cambiar
de modelo.
