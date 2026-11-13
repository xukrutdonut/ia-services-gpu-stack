# Ventilador de la Tesla V100 (control remoto por GPIO)

La V100 PCIe es **pasiva**: sin flujo de aire se cae del bus (Xid 79) a partir
de ~80 °C sostenidos, incluso en reposo. Este subsistema le pone un **ARCTIC
S12038-8K** (120 mm, 12 V, PWM 4 hilos, 800–8000 rpm) sobre el disipador y lo
controla desde un Raspberry Pi de la red, leyendo la temperatura de la GPU por
HTTP. Sin relación PID ni lazo RPM: curva de temperatura con meseta baja,
slew suave y escaladas de seguridad.

## Ficheros

| Fichero | Dónde va | Qué es |
|---|---|---|
| `fanctl.py` | Pi (`/opt/v100-fan/fanctl.py`) | Controlador: emulación open-drain del PWM + tacómetro + curva + failsafe + HTTP :9102 |
| `v100-fan.service` | Pi (`/etc/systemd/system/`) | Unit del controlador (`EnvironmentFile=/etc/default/v100-fan`) |
| `v100-fan.default.sample` | Pi (`/etc/default/v100-fan`) | Config de serie (ajustar `V100_TELEMETRY` al host GPU) |
| `exporter.py` | Host GPU (`/opt/v100-fan/exporter.py`) | `nvidia-smi` → JSON HTTP `:9101` (`/temp`: temp, mem_temp, power_w, sm_mhz, util, throttle_mask) |
| `v100-telemetry.service` | Host GPU (`/etc/systemd/system/`) | Unit del exporter |

## Cableado (Pi ↔ ventilador)

El pin 4 (PWM) del S12038-8K es una entrada de **colector abierto con
pull-up interno a 5 V** (`VIL max 0,8 V`, `VMax 5,25 V`, `Imax 5 mA`). Un GPIO
push-pull de 3,3 V no puede excitarla: un 0 lógico del GPIO (≈0 V) sí la
afirma, pero un 1 (3,3 V) deja la línea a medias y el ventilador no arranca
(queda al 100 % con línea suelta). `fanctl.py` emula open-drain por software
alternando el pin entre **entrada (Hi-Z → el pull-up del ventilador manda)** y
**LOW a masa**, en cualquier GPIO.

- Pin 1 (GND) → GND del Pi **y** GND de la fuente de 12 V (masa común obligatoria).
- Pin 2 (+12 V) → **fuente externa de 12 V** (≥3 A). Nunca del Pi.
- Pin 3 (Tach) → GPIO23 (físico 16). 2 pulsos/vuelta (`V100_TACH_PULSES=2`).
- Pin 4 (PWM) → GPIO12 (físico 32).

El tacómetro de esta unidad devuelve armónicos del propio PWM (crosstalk
7–10 kHz) a casi todo duty: **no cerrar lazo con él**; existe sólo como
señal de vida (el controlador verifica el pin durante la autodetección).

## Portadora del PWM: 100 Hz, no 25 kHz

Regla discovered a sangre: la emulación open-drain es un hilo Python que
escribe registros mmap en busy-wait; cada flanco cuesta ~1–10 µs reales. A
25 kHz (periodo 40 µs) un duty 5 % pide un pulso de 2 µs — más corto que el
overhead — y el duty **medio** que ve el ventilador queda dominado por el
jitter de planificación (GIL, telemetría, otros servicios del Pi). El driver
interno del ventilador, que es un lazo cerrado sobre el valor medio, caza ese
temblor: se oye acelerar y frenar con el duty de software clavado. Con
`V100_PWM_HZ=100` (la spec Intel permite 20–100 Hz) el periodo es 10 ms y el
jitter cae a ~±2 % del setpoint. Silencio restored.

## Curva y protecciones (valores desplegados)

```
duty <= V100_DUTY_MIN (5)                     mientras temp <= V100_TEMP_QUIET (55 °C)
duty rampa lineal 5 -> 100                     entre 55 y 80 °C (V100_TEMP_HOT)
duty cortado a V100_DUTY_CAP (45)              (la curva nunca lo supera)
escaladas INSTANTANEAS, sin tope ni slew:
  - throttle_mask con bits termicos (0x8/0x20/0x40/0x80) -> 100 %
  - >=3 ciclos sin telemetria -> 70 %;  >=12 -> 100 %
slew normal: +4 % / -2 % por ciclo de 2 s      (V100_SLEW_UP/DOWN)
```

Medido 2026-11-13 con carga sostenida real de inferencia (≈185 W):

| duty | meseta GPU | SM MHz | veredicto |
|---:|---|---:|---|
| 45 | 71 °C | 1380 | sin throttling — **tope actual** |
| 36 | 78 °C | 1095 | HW slowdown `0x4` — no válido |
| 30 | 77 °C | — | aparece `0x4` |

Idle real de esta unidad: 40–52 °C a duty 5.

## Despliegue

```bash
# Host GPU:
sudo mkdir -p /opt/v100-fan && sudo cp exporter.py /opt/v100-fan/
sudo cp v100-telemetry.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now v100-telemetry
curl -s localhost:9101/temp

# Pi (scp puede fallar sin sftp: usar `cat > /tmp/f`):
ssh pi@<PI> 'cat > /tmp/fanctl.py' < fanctl.py
ssh pi@<PI> 'sudo cp /tmp/fanctl.py /opt/v100-fan/fanctl.py && sudo cp v100-fan.service /etc/systemd/system/ && sudo cp /etc/default/v100-fan{,.bak} 2>/dev/null; ...'
# editar /etc/default/v100-fan (V100_TELEMETRY=http://<HOST-GPU>:9101/temp)
ssh pi@<PI> 'sudo systemctl daemon-reload && sudo systemctl enable --now v100-fan'
```

El controlador arranca en modo seguro y **autodetecta el pin del tacómetro**
(~20 s con el PWM en Hi-Z: el cable que pulsa a 25–270 Hz es el tach) antes de
activar ningún PWM — no se puede dejar el ventilador desatendido con un pin
equivocado.

## Operación

- Estado vivo del Pi: `cat /run/v100-fan/state.json` o `http://<PI>:9102/`.
- Override manual (raíz, ignorado fuera de servicio): `echo 30 | sudo tee /run/v100-fan/duty`; borrar con `sudo rm /run/v100-fan/duty` para volver a la curva.
- Logs: `journalctl -u v100-fan`. Cada reinicio del exporter en el host GPU
  provoca un escalado a 70/100 % durante ~6 s: **es el failsafe, es
  intencional y suena**.
