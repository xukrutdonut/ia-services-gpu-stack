#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_rx480.py -- Benchmark seguro de la RX 480 (Polaris, Vulkan/RADV).

Protocolo anti-cuelgue
======================
La RX 480 se cuelga (ring timeout + page fault -> cold boot obligatorio) cuando
acumula computo sostenido: el DIAGNOSTICO documenta el fallo tras ~10-13 min de
decodificacion continua, y con batches grandes (100 nodos tardan >2 s por
submit). Este harness mantiene cada carga MUY por debajo de ese umbral y aborta
ante la primera senal de bloqueo:

  * lock exclusivo (flock): nunca dos benches a la vez.
  * rechaza arrancar si hay un llama-server.real vivo (pelea por VRAM).
  * un modelo por proceso: carga -> mide -> descarga. Nunca dos en VRAM.
  * carga minima: -p 64 -n 32 -r 1 -b 64 -ub 16 (segundos de computo).
  * GGML_VK_MAX_NODES_PER_SUBMIT=1 / GGML_VK_DISABLE_ASYNC=1 (submit <100 ms).
  * canary: prueba de vida minima antes del bench; si no responde, aborta.
  * timeout duro por modelo: al expirar se mata y NO se reintenta.
  * vigilancia de devcoredump: si amdgpu vuelca un coredump, aborta al instante.
  * si el proceso no muere tras SIGKILL -> GPU WEDGED (exit 3, cold boot).
  * cooldown entre modelos para que el driver libere VRAM y fences.

Uso
===
  [host] python3 bench_rx480.py --check              # diagnostico, NO toca la GPU
  [cont] /opt/bench/bench_rx480.py                   # bench completo
  [cont] /opt/bench/bench_rx480.py --dry-run         # imprime el plan y sale
  [cont] /opt/bench/bench_rx480.py --canary-only     # solo prueba de vida
  [cont] /opt/bench/bench_rx480.py --models 0.6b,1.7b
  [cont] /opt/bench/bench_rx480.py --telemetry       # lee temp/fan/potencia (SMU)

Codigos de salida
=================
  0 ok | 2 abortado (timeout/señal) | 3 GPU wedged (cold boot) | 4 preflight
  5 lock ocupado | 6 sin modelos.
"""

import argparse
import fcntl
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------
BENCH_CANDIDATES = [
    os.environ.get("BENCH_BIN", ""),
    "/opt/llama-bench/llama-b11382/llama-bench",
    "/opt/llama-bench/llama-bench",
]
MODELS_DIR = os.environ.get("MODELS_DIR", "/root/.lmstudio/models/rx480")
RESULTS_DIR = os.environ.get("BENCH_RESULTS", "/opt/bench-results")
LOCK_FILE = "/tmp/bench-rx480.lock"
DEVCD = "/sys/class/devcoredump"

# Modelos del contenedor (nombre logico -> ruta relativa a MODELS_DIR)
MODELS = {
    "0.6b": "Qwen3-0.6B-Instruct/Qwen_Qwen3-0.6B-Q4_K_M.gguf",
    "1.7b": "Qwen3-1.7B-Instruct/Qwen3-1.7B-Q4_K_M.gguf",
    "olmoe": "OLMoE-1B-7B-Instruct/olmoe-1b-7b-0924-instruct-q4_k_m.gguf",
}

# Entorno Vulkan con el que la tarjeta es mas estable (identico al del compose)
SAFE_ENV = {
    "MESA_VK_DEVICE_SELECT": "1002:67df!",
    "DRI_PRIME": "1",
    "VK_ICD_FILENAMES": "/usr/share/vulkan/icd.d/radeon_icd.json",
    "GGML_VK_VISIBLE_DEVICES": "0",
    "GGML_VK_MAX_NODES_PER_SUBMIT": "1",
    "GGML_VK_DISABLE_ASYNC": "1",
    "GGML_VK_FORCE_MAX_ALLOCATION_SIZE": "2147483648",
}

DEFAULTS = dict(prompt=64, gen=32, batch=64, ubatch=16, reps=1,
                per_model_timeout=180, canary_timeout=120, cooldown=20,
                total_budget=1200, ngl=99)


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------
# Utilidades sysfs (seguras: NO pasan por la SMU salvo hwmon, que es opt-in)
# --------------------------------------------------------------------------
def find_amd_card():
    """Localiza el DRM device de la RX 480 (1002:67df) escaneando /sys/class/drm."""
    for vend in sorted(glob.glob("/sys/class/drm/card[0-9]*/device/vendor")):
        base = os.path.dirname(vend)
        try:
            with open(vend) as f:
                v = f.read().strip()
            with open(os.path.join(base, "device")) as f:
                d = f.read().strip()
        except OSError:
            continue
        if v.lower() == "0x1002" and d.lower() == "0x67df":
            return base
    return None


def rd(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def vram_mib(base):
    used = rd(os.path.join(base, "mem_info_vram_used"))
    total = rd(os.path.join(base, "mem_info_vram_total"))
    try:
        return int(used) / 2**20, int(total) / 2**20
    except (TypeError, ValueError):
        return None, None


def sclk(base):
    """Reloj actual (linea marcada con '*'). Atributo DRM, no pasa por SMU."""
    txt = rd(os.path.join(base, "pp_dpm_sclk"))
    if not txt:
        return None
    for line in txt.splitlines():
        if "*" in line:
            return line.strip()
    return None


def hwmon_telemetry(base):
    """temp/fan/potencia. OPT-IN: pasar por hwmon hace hablar a la SMU y puede
    generar los avisos 'failed ret is 65535' (Causa A del diagnostico)."""
    out = {}
    for hw in glob.glob(os.path.join(base, "hwmon", "hwmon*")):
        for key, name in (("temp1_input", "temp_c"), ("power1_input", "power_uw"),
                          ("fan1_input", "fan_rpm")):
            val = rd(os.path.join(hw, key))
            if val is not None:
                try:
                    v = int(val)
                    out[name] = v / 1000.0 if name in ("temp_c",) else (
                        v / 1e6 if name == "power_uw" else v)
                except ValueError:
                    pass
    return out


def devcd_entries():
    try:
        return set(os.listdir(DEVCD)) - {"disabled"}
    except OSError:
        return set()


# --------------------------------------------------------------------------
# Diagnostico (--check): no ejecuta computo en la GPU
# --------------------------------------------------------------------------
def cmd_check(args):
    base = find_amd_card()
    if not base:
        log("FAIL: no encuentro la RX 480 (1002:67df) en /sys/class/drm")
        return 4
    used, total = vram_mib(base)
    up = rd("/proc/uptime")
    verdict_ok = True
    print("== RX 480 / diagnostico ==")
    print("  device        : %s" % base)
    print("  vram          : %s / %s MiB" % (round(used) if used else "?",
                                             round(total) if total else "?"))
    sc = sclk(base)
    print("  sclk          : %s" % (sc or "VACIO (SMU no responde -> sucio)"))
    if not sc:
        verdict_ok = False
    print("  uptime host   : %s" % (up.split()[0] if up else "?"))
    if args.telemetry:
        print("  telemetria    : %s" % hwmon_telemetry(base))

    # 1) kernel log: ring timeouts / PRT / SMU muerta desde el arranque.
    #    'journalctl -k' funciona sin sudo si el usuario esta en 'adm' o
    #    'systemd-journal'; dmesg y 'sudo -n dmesg' son alternativas.
    klog, klog_src = None, None
    for name, cmd in (("journalctl -k", ["journalctl", "-k", "-b", "--no-pager"]),
                      ("dmesg", ["dmesg", "-T"]),
                      ("sudo -n dmesg", ["sudo", "-n", "dmesg", "-T"])):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.SubprocessError):
            continue
        if p.returncode == 0 and p.stdout:
            klog, klog_src = p.stdout, name
            break
    if klog is None:
        print("  kernel log    : NO accesible (journalctl -k / dmesg / sudo -n). "
              "El canary hara de prueba de vida.")
    else:
        lines = klog.splitlines()
        # Fatales: ring timeout real y PRT ('Disabling VM faults' deja la GPU
        # sin red de seguridad). 'GPU recovery disabled.' es ruido de INIT del
        # driver con gpu_recovery=0 (se imprime 2x en cada arranque limpio, antes
        # de 'hwmgr_sw_init'); NO es una recuperacion y no debe contar.
        hits = [l for l in lines
                if re.search(r"ring .* timeout|Disabling VM faults", l)]
        boot_noise = sum(1 for l in lines if "GPU recovery disabled." in l)
        spam = sum(1 for l in lines if "failed ret is 65535" in l)
        print("  kernel log    : via %s" % klog_src)
        if hits:
            verdict_ok = False
            print("  !! RING TIMEOUTS / PRT en este arranque (%d):" % len(hits))
            for l in hits[-4:]:
                print("     %s" % l.strip())
            print("     -> cold boot obligatorio antes de cualquier bench.")
        else:
            print("  ring timeouts : ninguno en este arranque (limpio).")
        if boot_noise:
            print("  boot noise    : %d lineas 'GPU recovery disabled.' "
                  "(init del driver, gpu_recovery=0; se ignoran)." % boot_noise)
        if spam > 5:
            verdict_ok = False
            print("  !! SMU no responde: %d lineas 'failed ret is 65535' -> "
                  "GPU sucia (cold boot)." % spam)

    # 2) coredumps pendientes
    cd = devcd_entries()
    if cd:
        verdict_ok = False
        print("  !! coredump amdgpu pendiente: %s -> la GPU ya se cayo." % sorted(cd))
    else:
        print("  coredump      : ninguno pendiente.")

    # 3) VRAM ocupada (si hay servidor cargado, mejor pararlo)
    if used and total and used > 100:
        print("  aviso         : hay %d MiB en VRAM (modelo cargado). "
              "El bench hara 'lms unload --all'." % round(used))

    print("  VEREDICTO     : %s" % ("LISTO (seguir)" if verdict_ok
                                    else "SUCIO (cold boot antes de medir)"))
    return 0 if verdict_ok else 1


# --------------------------------------------------------------------------
# Guardas de ejecucion
# --------------------------------------------------------------------------
class Abort(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


class Sampler(threading.Thread):
    """Muestrea VRAM y sclk cada 2 s (atributos DRM, sin SMU)."""

    def __init__(self, base):
        super().__init__(daemon=True)
        self.base = base
        self.stop = threading.Event()
        self.peak_vram = 0
        self.samples = 0
        self.last_sclk = None

    def run(self):
        while not self.stop.is_set():
            used, _ = vram_mib(self.base)
            if used:
                self.peak_vram = max(self.peak_vram, used)
                self.samples += 1
            self.last_sclk = sclk(self.base) or self.last_sclk
            self.stop.wait(2)


def kill_tree(p):
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.kill()
        except OSError:
            pass


def ensure_gpu_free():
    """Descarga modelos y mata servidores para dejar la VRAM libre."""
    if shutil.which("lms"):
        try:
            subprocess.run(["lms", "unload", "--all"], capture_output=True,
                           timeout=45)
        except (subprocess.SubprocessError, OSError):
            log("aviso: 'lms unload --all' no respondio a tiempo")
    try:
        subprocess.run(["pkill", "-f", "llama-server.real"], capture_output=True,
                       timeout=15)
    except (subprocess.SubprocessError, OSError):
        pass
    time.sleep(3)
    left = subprocess.run(["pgrep", "-f", "llama-server.real"],
                          capture_output=True, text=True).stdout.split()
    if left:
        log("aviso: sigue habiendo llama-server.real: %s" % left)


def run_llama_bench(binary, model_path, a, label):
    """Ejecuta llama-bench para UN modelo con timeout duro y vigilancia."""
    args = [binary, "-m", model_path, "-ngl", str(a.ngl),
            "-p", str(a.prompt), "-n", str(a.gen), "-r", str(a.reps),
            "-b", str(a.batch), "-ub", str(a.ubatch), "-o", "json"]
    env = dict(os.environ)
    env.update(SAFE_ENV)
    log("  cmd: %s" % " ".join(args[1:]))

    base = find_amd_card()
    base_cd = devcd_entries()
    s = Sampler(base) if base else None
    if s:
        s.start()

    out_f = "/tmp/llama-bench-%s.out" % label
    err_f = "/tmp/llama-bench-%s.err" % label
    t0 = time.time()
    fo = open(out_f, "wb")
    fe = open(err_f, "wb")
    p = subprocess.Popen(args, stdout=fo, stderr=fe, env=env,
                         start_new_session=True)
    status, note = "ok", ""
    try:
        while True:
            if p.poll() is not None:
                break
            time.sleep(2)
            if devcd_entries() - base_cd:
                status = "abortado"
                note = "amdgpu volco un coredump durante el bench"
                break
            if time.time() - t0 > a.per_model_timeout:
                status = "abortado"
                note = "timeout duro (%ds) excedido" % a.per_model_timeout
                break
    finally:
        elapsed = time.time() - t0
        if p.poll() is None:
            log("  ! ABORT: %s -> SIGKILL" % note)
            kill_tree(p)
            time.sleep(5)
            if p.poll() is None:
                fe.close(); fo.close()
                if s:
                    s.stop.set()
                raise Abort("el proceso no muere tras SIGKILL: GPU WEDGED", 3)
        fe.close()
        fo.close()
        if s:
            s.stop.set()

    res = dict(model=os.path.basename(model_path), label=label, status=status,
               note=note, elapsed_s=round(elapsed, 1),
               peak_vram_mib=round(s.peak_vram) if s and s.samples else None,
               sclk=s.last_sclk if s else None)

    if status != "ok":
        res["stderr_tail"] = tail(err_f, 6)
        return res

    # parseo del JSON de llama-bench
    try:
        txt = open(out_f).read()
        data = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
    except (OSError, ValueError) as e:
        res["status"] = "abortado"
        res["note"] = "no pude parsear el JSON de llama-bench (%s)" % e
        res["stderr_tail"] = tail(err_f, 6)
        return res

    for entry in data:
        if entry.get("n_prompt"):
            res["pp_ts"] = round(entry.get("avg_ts", 0), 1)
            res["pp_sd"] = round(entry.get("stddev_ts", 0), 2)
            res["pp_n"] = entry.get("n_prompt")
        elif entry.get("n_gen"):
            res["tg_ts"] = round(entry.get("avg_ts", 0), 1)
            res["tg_sd"] = round(entry.get("stddev_ts", 0), 2)
            res["tg_n"] = entry.get("n_gen")
        res.setdefault("model_size_gib", round(entry.get("model_size", 0) / 2**30, 2))
        res.setdefault("n_params", entry.get("model_n_params"))
        res.setdefault("backend", (entry.get("backends") or "").split()[0]
                       if entry.get("backends") else None)
        res.setdefault("ngl", entry.get("n_gpu_layers"))
    return res


def tail(path, n):
    try:
        with open(path) as f:
            return [l.rstrip() for l in f.readlines()[-n:]]
    except OSError:
        return []


# --------------------------------------------------------------------------
# Bench
# --------------------------------------------------------------------------
def find_binary():
    for c in BENCH_CANDIDATES:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def cmd_bench(args):
    binary = find_binary()
    if not binary:
        log("FAIL: no encuentro llama-bench. Probados: %s" % BENCH_CANDIDATES)
        return 4

    keys = [k.strip() for k in args.models.split(",")] if args.models \
        else list(MODELS)
    missing = [k for k in keys if k not in MODELS]
    if missing:
        log("FAIL: modelos desconocidos %s (validos: %s)" % (missing, list(MODELS)))
        return 6
    plan = [(k, MODELS[k], os.path.join(MODELS_DIR, MODELS[k])) for k in keys]

    log("bench llama-bench : %s" % binary)
    log("carga              : -p %d -n %d -r %d -b %d -ub %d -ngl %d"
        % (args.prompt, args.gen, args.reps, args.batch, args.ubatch, args.ngl))
    log("timeout/modelo     : %ds   cooldown: %ds   presupuesto: %ds"
        % (args.per_model_timeout, args.cooldown, args.total_budget))
    for k, rel, path in plan:
        ok = "OK" if os.path.isfile(path) else "FALTA"
        log("  [%s] %s (%s)" % (ok, path, rel))

    if args.dry_run:
        log("--dry-run: no ejecuto nada. (Quita --dry-run para medir.)")
        return 0

    # lock exclusivo
    lf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("FAIL: ya hay un bench en marcha (lock %s)" % LOCK_FILE)
        return 5

    base = find_amd_card()
    if not base:
        log("FAIL: no encuentro la RX 480")
        return 4
    t_start = time.time()

    log("liberando GPU (unload de modelos)...")
    ensure_gpu_free()

    results = []
    try:
        if args.canary and not args.no_canary:
            small = plan[0]
            log("canary: prueba de vida (-p 8 -n 8) sobre %s" % small[0])
            ca = argparse.Namespace(**vars(args))
            ca.prompt, ca.gen, ca.reps = 8, 8, 1
            ca.per_model_timeout = args.canary_timeout
            r = run_llama_bench(binary, small[2], ca, "canary")
            if r["status"] != "ok":
                log("FAIL del canary: %s" % r.get("note", ""))
                for l in r.get("stderr_tail", []):
                    log("   %s" % l)
                raise Abort("la GPU no responde a la carga minima: "
                            "cold boot antes de medir", 4)
            log("canary OK (%ss)" % r["elapsed_s"])
            time.sleep(args.cooldown)
        if args.canary_only:
            log("--canary-only: hecho.")
            return 0

        for k, rel, path in plan:
            if not os.path.isfile(path):
                results.append(dict(model=rel, label=k, status="sin-archivo"))
                continue
            if time.time() - t_start > args.total_budget:
                log("presupuesto total agotado; paro aqui.")
                break
            log("== modelo %s ==" % k)
            r = run_llama_bench(binary, path, args, k)
            r["label"] = k
            if args.telemetry:
                r["telemetry_after"] = hwmon_telemetry(base)
            results.append(r)
            if r["status"] == "ok":
                log("  pp%s: %s tok/s   tg%s: %s tok/s   vram pico: %s MiB   %ss"
                    % (r.get("pp_n"), r.get("pp_ts"), r.get("tg_n"),
                       r.get("tg_ts"), r.get("peak_vram_mib"), r["elapsed_s"]))
            else:
                log("  ! %s: %s" % (r["status"], r.get("note")))
                raise Abort("abortado en %s: %s" % (k, r.get("note")), 2)
            log("  cooldown %ds" % args.cooldown)
            time.sleep(args.cooldown)
    except Abort as e:
        log("##### ABORTADO: %s" % e)
        save_results(results, args, aborted=True)
        ensure_gpu_free()
        return e.code

    save_results(results, args, aborted=False)
    log("descargando modelos...")
    ensure_gpu_free()
    print_table(results)
    return 0


def print_table(results):
    print("\n%-10s %10s %10s %9s %7s %8s %s"
          % ("modelo", "pp tok/s", "tg tok/s", "VRAM MiB", "tiempo", "ngl", "estado"))
    for r in results:
        print("%-10s %10s %10s %9s %7s %8s %s"
              % (r.get("label", "?"), r.get("pp_ts", "-"), r.get("tg_ts", "-"),
                 r.get("peak_vram_mib", "-"), r.get("elapsed_s", "-"),
                 r.get("ngl", "-"), r.get("status", "?")))


def save_results(results, args, aborted):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        aborted=aborted,
        params=dict(prompt=args.prompt, gen=args.gen, reps=args.reps,
                    batch=args.batch, ubatch=args.ubatch, ngl=args.ngl),
        host=utf_node(),
        results=results,
    )
    j = os.path.join(RESULTS_DIR, "bench-%s.json" % stamp)
    with open(j, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    with open(os.path.join(RESULTS_DIR, "results.jsonl"), "a") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    m = os.path.join(RESULTS_DIR, "bench-%s.md" % stamp)
    with open(m, "w") as f:
        f.write(render_markdown(payload))
    log("resultados -> %s (+ .md)" % j)


def render_markdown(payload):
    """Informe legible de una corrida (mismo contenido que el .json)."""
    p = payload["params"]
    L = ["# Bench RX 480 (Vulkan) — %s" % payload["timestamp"],
         "",
         "Host: `%s` · abortado: %s" % (payload.get("host", "?"),
                                        payload.get("aborted")),
         "Protocolo: `-p %(prompt)s -n %(gen)s -r %(reps)s -b %(batch)s "
         "-ub %(ubatch)s -ngl %(ngl)s`" % p,
         "",
         "| Modelo | Clave | pp tok/s | tg tok/s | VRAM pico (MiB) | "
         "Tamano (GiB) | sclk | Tiempo (s) | Estado |",
         "| :--- | :--- | ---: | ---: | ---: | ---: | :--- | ---: | :--- |"]
    for r in payload["results"]:
        L.append("| %s | `%s` | %s | %s | %s | %s | %s | %s | %s |"
                 % (r.get("model", "?"), r.get("label", "?"),
                    r.get("pp_ts", "-"), r.get("tg_ts", "-"),
                    r.get("peak_vram_mib", "-"), r.get("model_size_gib", "-"),
                    r.get("sclk", "-"), r.get("elapsed_s", "-"),
                    r.get("status", "?")))
    L += ["",
          "Backend: Vulkan · `n_params`/`pp_sd`/`tg_sd` en el .json hermano.",
          ""]
    return "\n".join(L)


def utf_node():
    try:
        return subprocess.run(["hostname"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "?"


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Bench seguro RX 480 (Vulkan)")
    ap.add_argument("--check", action="store_true",
                    help="diagnostico sin tocar la GPU (host)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--canary-only", action="store_true")
    ap.add_argument("--no-canary", action="store_true")
    ap.add_argument("--canary", action="store_true", default=True)
    ap.add_argument("--telemetry", action="store_true",
                    help="leer temp/fan/potencia (hace hablar a la SMU)")
    ap.add_argument("--models", default="", help="p.ej. 0.6b,1.7b,olmoe")
    ap.add_argument("--prompt", type=int, default=DEFAULTS["prompt"])
    ap.add_argument("--gen", type=int, default=DEFAULTS["gen"])
    ap.add_argument("--batch", type=int, default=DEFAULTS["batch"])
    ap.add_argument("--ubatch", type=int, default=DEFAULTS["ubatch"])
    ap.add_argument("--reps", type=int, default=DEFAULTS["reps"])
    ap.add_argument("--ngl", type=int, default=DEFAULTS["ngl"])
    ap.add_argument("--per-model-timeout", type=int,
                    default=DEFAULTS["per_model_timeout"])
    ap.add_argument("--canary-timeout", type=int, default=DEFAULTS["canary_timeout"])
    ap.add_argument("--cooldown", type=int, default=DEFAULTS["cooldown"])
    ap.add_argument("--total-budget", type=int, default=DEFAULTS["total_budget"])
    args = ap.parse_args()

    if args.prompt > 256 or args.gen > 128:
        log("AVISO: peticion grande (-p %d -n %d). La tarjeta se cuelga con "
            "computo sostenido; usa valores <=256/128." % (args.prompt, args.gen))
    return cmd_check(args) if args.check else cmd_bench(args)


if __name__ == "__main__":
    sys.exit(main())
