# COMPLETADO — Bench seguro RX 480 (handoff cold boot)

**Fecha del handoff:** 2026-10-04 · **Resuelto:** 2026-10-04 ~11:04.
**Disparador:** el cold reboot físico (solo el usuario puede hacerlo).

> **RESUELTO.** Cold boot a las 10:55 → arranque limpio (sin ring timeouts, PRT
> ni `failed ret is 65535`). Contenedor recreado con la imagen nueva (`python3`
> + montajes) y publicado en el registry `.101:5555/ia-gpu-amd-rx480:{latest,2026-10-04}`.
> `--check` → LISTO, canary OK, bench completo OK (3/3 modelos). Sin timeouts
> tras la corrida, `devcoredump` vacío, sclk coherente. Medidas reales en el
> README y en `results/bench-20261004-090402.{json,md}`. `gpu-watchdog` v5.8
> activo (ALLOW_SBR=0). Queda pendiente solo la Causa C (estrés sostenido), que
> este bench de carga mínima no ejercita por diseño.
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
