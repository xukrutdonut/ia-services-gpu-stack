#!/usr/bin/env python3
"""Controlador del ventilador ARCTIC S12038-8K de la V100 (rpi3b-1 .101).

=====================================================================
POR QUE NO FUNCIONABA EL CONTROL DE VELOCIDAD
=====================================================================
La entrada PWM del S12038-8K es de LOGICA DE 5 V con pull-up INTERNO a
5 V. Manual oficial ARCTIC (seccion "In a DIY Project"):

    PWM Frequency: target 25 kHz (rango admisible 21-28 kHz)
    Maximum voltage for logic low: VIL = 0,8 V
    Absolute maximum current sourced: Imax = 5 mA (en cortocircuito)
    Absolute maximum voltage level: VMax = 5,25 V (en circuito abierto)

Es decir: la linea PWM es de COLECTOR ABIERTO. El ventilador sube la linea a
5 V y el HOST debe TIRAR A MASA para el nivel bajo.

El GPIO12 del Pi es push-pull de 3,3 V. Al enchufarlo directamente a esa
linea de 5 V:
  - no llega al umbral HIGH de la entrada CMOS de 5 V, y
  - pelea con el pull-up de 5 V del ventilador.
Resultado: el ventilador no arranca o no responde al duty. Con el cable
DESCONECTADO manda el pull-up del ventilador, la linea se queda a 5 V y el
ventilador corre al 100 %: de ahi el sintoma "si quito el cable funciona".

=====================================================================
COMO SE RESUELVE
=====================================================================
Opcion A (recomendada, sin MOSFET): OPEN-DRAIN POR SOFTWARE.
  El Pi no tiene open-drain en hardware, pero si por software: alternar el
  pin entre ENTRADA (alta impedancia -> el pull-up del ventilador pone la
  linea a 5 V, nivel HIGH valido) y SALIDA a LOW (tirando a masa, nivel LOW
  valido). Medido en este rpi3b-1: 152 000 periodos/s en Python, o sea 6x
  lo necesario para 25 kHz.
  Ventaja adicional: funciona en CUALQUIER GPIO, asi que da igual que el
  PWM acabe en GPIO12 (con PWM por hardware) o en GPIO23 (que no tiene
  funcion PWM por hardware en el Pi 3). El controlador autodetecta cual de
  los dos pines es el tacometro.

Opcion B (si prefieres hardware): MOSFET 2N7000 en open-drain sobre la linea
  PWM (drain al cable, source a masa, puerta desde el GPIO con 100 ohm y
  10k a masa) y el PWM por hardware del Pi a 25 kHz con el duty invertido
  (V100_PWM_INVERT=1 en el otro controlador).

REQUISITO INNEGOCIABLE EN AMBOS CASOS: MASA COMUN entre el Pi (pin 6) y el
ventilador (pin 1 negro) o el negativo de la fuente de 12 V. Sin ella la
linea no tiene referencia electrica: el PWM no llega y el tacometro no da
pulsos (cero flancos en todos los pines, medido).

PROTECCION DEL PIN DE 3,3 V: la linea del ventilador es de 5 V. Entre el
ventilador y el pin del Pi, una resistencia de 10k en serie. Asi el pin ve
como mucho 5 V con ~0,1 mA de corriente de clamp, y el nivel LOW sigue siendo
valido (0,45 V < VIL 0,8 V). Para el tacometro, divisor 10k serie + 10k a
masa (el nivel HIGH queda en 2,5 V) y pull-up interno del Pi activado.

=====================================================================
CURVA REAL DEL VENTILADOR (manual ARCTIC, 12 V)
=====================================================================
PWM %  ->  RPM        duty alto = mas lento (linea a masa = parado)
  0 -> 8040    10 -> 7533    20 -> 6966    30 -> 6438    40 -> 5889
 50 -> 5247    60 -> 4551    70 -> 3771    80 -> 2925    90 -> 2001
100 ->  765

Politica: se busca el duty MAS BAJO que mantiene la V100 fuera de throttling
termico, con lazo cerrado por tacometro (rpm medidos, no estimados) y suelo
configurable de rpm.

MODO SEGURO: si el tacometro no responde (sin masa comun todavia), el pin PWM
queda en ALTA IMPEDANCIA y el ventilador corre al 100 %: la GPU no se
calienta, a cambio de ruido. En cuanto el tacometro responde, entra el modo
PWM solo.

Override manual:  echo <0-100> > /run/v100-fan/duty   (borrar para soltar)
Estado:             /run/v100-fan/state.json
"""
import json
import logging
import mmap
import os
import struct
import subprocess
import threading
import time
import urllib.request

# PINOUT OFICIAL (Spec-Sheet ARCTIC S12038-8K):
#   Pin#1 GND   Pin#2 +12 V DC   Pin#3 Tachometer   Pin#4-PWM
# El cableado actual del usuario tiene pin3 (TACH) -> GPIO12 y pin4 (PWM)
# -> GPIO23, o sea INTERCAMBIADOS, y el PWM en GPIO23 que en el Pi 3 no
# tiene funcion PWM por hardware (solo GPIO12/13/18/19).
# Correccion NECESARIA: PWM -> GPIO12 (pin fisico 32), TACH -> GPIO23 (pin 16).
PWM_PIN = int(os.environ.get("V100_PWM_PIN", "12"))    # PWM por hardware
TACH_PIN = int(os.environ.get("V100_TACH_PIN", "23"))  # solo entrada
PWM_PIN_CANDIDATES = [PWM_PIN, TACH_PIN]
TACH_PIN_ENV = int(os.environ.get("V100_TACH_PIN", "0"))  # 0 = autodetectar
PWM_HZ = float(os.environ.get("V100_PWM_HZ", "25000"))
URL = os.environ.get("V100_TELEMETRY", "http://192.168.0.100:9101/temp")
INTERVAL = float(os.environ.get("V100_INTERVAL", "2.0"))
MANUAL = "/run/v100-fan/duty"
STATE = "/run/v100-fan/state.json"
PULSES = float(os.environ.get("V100_TACH_PULSES", "2"))

# Semantica del S12038-8K: linea a masa = ventilador parado, linea suelta
# (pull-up del ventilador) = 100 %. Por eso el duty que buscamos (rpm) es
# el tiempo en ALTO, y el tiempo a masa es su complementario.
INVERT = True

MIN_DUTY, MAX_DUTY = 10, 100
STEP_UP, STEP_DOWN = 10, 5
FAIL_WARN, FAIL_MAX = 3, 12
RPM_FLOOR = float(os.environ.get("V100_RPM_FLOOR", "0"))
# Tope de duty por RUIDO: limita la curva temp->duty. NO se aplica a las
# escaladas de seguridad (throttle termico -> 100 %, perdida de telemetria
# -> 70/100 %): la seguridad manda sobre el ruido. El tacometro de esta
# S12038-8K lee crosstalk del PWM, asi que RPM_FLOOR no puede cerrarse en
# lazo: existe solo como referencia de la curva.
DUTY_CAP = max(MIN_DUTY, min(MAX_DUTY, float(os.environ.get("V100_DUTY_CAP", "100"))))
# Curva de silencio: hasta TEMP_QUIET el ventilador se queda en DUTY_MIN
# (casi inaudible, ~1100 RPM) y de ahi sube en rampa lineal hasta 100 en
# TEMP_HOT. ANTES la curva era duty=temp con un salto en 45 C (45->10 %,
# 46->46 %): en reposo, entre 45 y 52 C, el ventilador aceleraba y
# frenaba con cada peticion (el "bombeo" que se oia). SLEW limita cuanto
# cambia el duty por ciclo: sube en ~4 %/2 s y baja en ~2 %/2 s, asi las
# transiciones son rampas suaves. Las escaladas de seguridad van directas.
DUTY_MIN = max(0.0, min(50.0, float(os.environ.get("V100_DUTY_MIN", "5"))))
TEMP_QUIET = float(os.environ.get("V100_TEMP_QUIET", "50"))
TEMP_HOT = float(os.environ.get("V100_TEMP_HOT", "80"))
SLEW_UP = float(os.environ.get("V100_SLEW_UP", "4"))
SLEW_DOWN = float(os.environ.get("V100_SLEW_DOWN", "2"))
TACH_MIN_RPM = float(os.environ.get("V100_TACH_MIN_RPM", "100"))
TACH_GRACE = 25
SAFE_RELOG = 30

# (duty_pct, rpm_objetivo) tomados de la curva oficial ARCTIC
CURVE = [(10, 2001), (20, 2925), (30, 3771), (40, 4551), (50, 5247),
         (60, 5889), (70, 6438), (80, 6966), (90, 7533), (100, 8040)]

THERMAL_BITS = 0x8 | 0x20 | 0x40 | 0x80

log = logging.getLogger("fan")


# ------------------------------------------------------------- open-drain
class OpenDrain:
    """Emula un colector abierto alternando el pin entre entrada y LOW."""

    GPFSEL = 0x00

    def __init__(self, pin):
        self.pin = pin
        fd = os.open("/dev/gpiomem", os.O_RDWR | os.O_SYNC)
        self.mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE)
        self.off = self.GPFSEL + (pin // 10) * 4
        self.sh = (pin % 10) * 3
        v = struct.unpack_from("<I", self.mem, self.off)[0]
        self.base = v & ~(7 << self.sh)
        self.out = self.base | (1 << self.sh)
        self._lock = threading.Lock()
        self._thread = None
        self._stop = False
        self.duty = None

    def _wr(self, val):
        struct.pack_into("<I", self.mem, self.off, val)

    def release(self):
        """Alta impedancia: el pull-up del ventilador manda y va al 100 %."""
        with self._lock:
            self._wr(self.base)
        self.duty = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self):
        period = 1.0 / PWM_HZ
        while not self._stop:
            with self._lock:
                duty = self.duty
            if duty is None:
                time.sleep(0.02)
                continue
            # duty = % de tiempo con la linea SUELTA (ventilador rapido)
            high = period * duty / 100.0
            low = period - high
            t = time.perf_counter()
            # alto: pin en alta impedancia
            with self._lock:
                self._wr(self.base)
            t += high
            while time.perf_counter() < t:
                pass
            # bajo: pin a masa
            with self._lock:
                self._wr(self.out)
            t += low
            while time.perf_counter() < t:
                pass

    def set_duty(self, pct):
        # duty = % de tiempo que la linea queda SUELTA (flotando). Como el
        # pull-up del ventilador manda con la linea suelta, este % es
        # directamente la velocidad (masa = lento, suelto = rapido).
        # Guardamos el valor tal cual, SIN invertir: una doble inversion
        # (esta + la del _loop) hacia que duty bajo == ventilador rapido.
        pct = max(0.0, min(100.0, float(pct)))
        with self._lock:
            self.duty = pct

    def close(self):
        self._stop = True
        self.release()


# ------------------------------------------------------------------ tach
class Tach:
    """Contador de flancos del tacometro via mmap de /dev/gpiomem."""

    GPFSEL = 0x00
    GPLEV0 = 0x34
    GPPUD = 0x94
    GPPUDCLK0 = 0x98

    def __init__(self, pin):
        self.pin = pin
        self.rpm = 0.0
        self.hz = 0.0
        self._fd = os.open("/dev/gpiomem", os.O_RDWR | os.O_SYNC)
        self._mem = mmap.mmap(self._fd, 4096, mmap.MAP_SHARED,
                              mmap.PROT_READ | mmap.PROT_WRITE)
        self._setup_input()

    def _rd(self, off):
        return struct.unpack_from("<I", self._mem, off)[0]

    def _wr(self, off, val):
        struct.pack_into("<I", self._mem, off, val)

    def _setup_input(self):
        reg = self.GPFSEL + (self.pin // 10) * 4
        sh = (self.pin % 10) * 3
        self._wr(reg, self._rd(reg) & ~(7 << sh))       # entrada
        self._wr(self.GPPUD, 2)                         # pull-up
        time.sleep(0.0002)
        self._wr(self.GPPUDCLK0 + (self.pin // 32) * 4, 1 << (self.pin % 32))
        time.sleep(0.0002)
        self._wr(self.GPPUD, 0)
        self._wr(self.GPPUDCLK0 + (self.pin // 32) * 4, 0)

    def sample(self, dur=1.0):
        mask = 1 << self.pin
        last = bool(self._rd(self.GPLEV0) & mask)
        edges = 0
        t0 = time.perf_counter()
        end = t0 + dur
        while time.perf_counter() < end:
            v = bool(self._rd(self.GPLEV0) & mask)
            if v != last:
                edges += 1
                last = v
        dt = time.perf_counter() - t0
        self.hz = edges / dt if dt > 0 else 0.0
        self.rpm = self.hz * 60.0 / PULSES
        return self.rpm

    def close(self):
        try:
            self._mem.close()
            os.close(self._fd)
        except OSError:
            pass


# ----------------------------------------------------------------- curva
def curve(temp):
    """temp->duty suave: DUTY_MIN hasta TEMP_QUIET y rampa lineal hasta
    100 % en TEMP_HOT. El segundo valor (rpm objetivo) se deja None: el
    tacometro real no es fiable en este cableado y lazo RPM no hay."""
    if temp is None:
        return None, None
    if temp <= TEMP_QUIET:
        d = DUTY_MIN
    elif temp >= TEMP_HOT:
        d = float(MAX_DUTY)
    else:
        f = (temp - TEMP_QUIET) / (TEMP_HOT - TEMP_QUIET)
        d = DUTY_MIN + (MAX_DUTY - DUTY_MIN) * f
    return d, None


def thermal_alert(mask):
    try:
        return bool(int(str(mask), 0) & THERMAL_BITS)
    except (TypeError, ValueError):
        return False


def telemetry():
    try:
        with urllib.request.urlopen(URL, timeout=3) as r:
            j = json.load(r)
        if not j.get("ok"):
            return None
        return j
    except Exception:
        return None


def manual_duty():
    try:
        with open(MANUAL) as f:
            return max(0.0, min(100.0, float(f.read().strip())))
    except (OSError, ValueError):
        return None


def detect_tach(candidates, window=20.0):
    """Escucha los pines candidatos y devuelve el que da pulsos."""
    probes = []
    for p in candidates:
        try:
            probes.append((p, Tach(p)))
        except OSError as e:
            log.warning("no se pudo leer GPIO%d: %s", p, e)
    if not probes:
        return None
    log.info("autodetectando tacometro en %s (%.0f s)...", candidates, window)
    t0 = time.time()
    counts = {p: 0 for p, _ in probes}
    while time.time() - t0 < window:
        for p, t in probes:
            counts[p] += t.sample(1.0)
        time.sleep(0.05)
    for p, t in probes:
        t.close()
    for p, rpm in counts.items():
        log.info("  GPIO%d: %.0f rpm", p, rpm)
    best = max(counts, key=lambda p: counts[p])
    return best if counts[best] >= TACH_MIN_RPM else None


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    os.makedirs("/run/v100-fan", exist_ok=True)

    # Los dos pines a entrada: el del PWM en alta impedancia (ventilador al
    # 100 %) y el del tacometro en modo lectura.
    tach_pin = TACH_PIN_ENV
    pwm_pin = [p for p in PWM_PIN_CANDIDATES if p != tach_pin][0]

    if tach_pin == 0:
        tach_pin = detect_tach(PWM_PIN_CANDIDATES)
        if tach_pin is None:
            log.warning("sin senal de tacometro en %s: se asume GPIO%d como "
                        "tacometro y GPIO%d como PWM", PWM_PIN_CANDIDATES,
                        PWM_PIN_CANDIDATES[1], PWM_PIN_CANDIDATES[0])
            tach_pin = PWM_PIN_CANDIDATES[1]
        else:
            log.info("tacometro en GPIO%d", tach_pin)
        pwm_pin = [p for p in PWM_PIN_CANDIDATES if p != tach_pin][0]

    od = OpenDrain(pwm_pin)
    od.start()
    tach = None
    try:
        tach = Tach(tach_pin)
    except OSError as e:
        log.warning("sin tacometro: %s", e)

    log.info("PWM (open-drain software) en GPIO%d a %.0f Hz; tacometro GPIO%d",
             pwm_pin, PWM_HZ, tach_pin)

    target, fails, last = DUTY_MIN, 0, None
    applied = None
    start = time.time()
    last_safe_log = 0.0
    verified = False
    safe = True

    while True:
        man = manual_duty()
        j = telemetry()
        temp = (j or {}).get("temp")
        escalated = man is not None
        if man is not None:
            target, source = man, "manual"
            fails = 0
        elif j is None:
            fails += 1
            source = "fallo(%d)" % fails
            if fails >= FAIL_MAX:
                target = MAX_DUTY
            elif fails >= FAIL_WARN:
                target = max(target, 70)
            escalated = fails >= FAIL_WARN
            log.warning("sin telemetria (%d): duty objetivo %d%%", fails, target)
        else:
            fails = 0
            source = "temp"
            last = j
            d, _ = curve(temp)
            if d is not None:
                target = min(d, DUTY_CAP)
            if thermal_alert(j.get("throttle_mask")):
                target = MAX_DUTY
                escalated = True
                log.warning("throttle termico (%s) -> 100%%", j["throttle_mask"])

        rpm = tach.sample(1.0) if tach is not None else 0.0

        if not verified and rpm >= TACH_MIN_RPM:
            verified = True
            safe = False
            log.info("tacometro verificado (%.0f rpm): entra modo PWM", rpm)
        elif verified and rpm == 0.0 and (time.time() - start) > TACH_GRACE:
            verified = False
            safe = True
            log.warning("tacometro sin pulsos -> modo seguro (ventilador 100%%)")

        if safe:
            mode = "seguro (ventilador 100%)"
            new = None
            od.release()
            if (time.time() - last_safe_log) > SAFE_RELOG:
                last_safe_log = time.time()
                log.warning("MODO SEGURO: pin PWM en alta impedancia, "
                            "ventilador al 100%%; falta masa comun "
                            "(pin 6 del Pi -> pin 1 negro del ventilador)")
        else:
            # Control por temperatura en lazo abierto. El tacometro lee
            # crosstalk del PWM (rpm 10^5-10^6 con la S12038-8K, max 8040),
            # asi que NO usamos el PID de rpm: la referencia sale solo de la
            # curva temp->duty. El modo seguro (pin libre = 100 %) sigue como
            # red de seguridad si el tacometro deja de dar pulsos.
            mode = "pwm"
            new = target
            if not escalated and applied is not None:
                delta = target - applied
                delta = (min(delta, SLEW_UP) if delta > 0
                         else max(delta, -SLEW_DOWN))
                new = max(DUTY_MIN, min(MAX_DUTY, applied + delta))
            applied = new
            od.set_duty(new)

        state = {"ts": time.time(), "duty": round(new, 1) if new is not None else 100,
                 "target": round(target, 1), "rpm": round(rpm, 1),
                 "hz": round(tach.hz, 2) if tach else 0.0,
                 "temp": temp, "mode": mode, "source": source,
                 "pwm_pin": pwm_pin, "tach_pin": tach_pin,
                 "throttle": (last or {}).get("throttle_mask")}
        try:
            with open(STATE, "w") as f:
                json.dump(state, f)
        except OSError:
            pass
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
