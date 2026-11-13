#!/usr/bin/env python3
"""V100 telemetry exporter for Khazad-dum. GET /temp -> JSON, 1s cache."""
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9101
FIELDS = ("temperature.gpu,temperature.memory,power.draw,clocks.sm,"
          "utilization.gpu,clocks_throttle_reasons.active,memory.used")

_lock = threading.Lock()
_cache = {"ts": 0.0, "body": None}


def sample():
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=" + FIELDS, "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    v = [x.strip() for x in out.split(",")]
    return {
        "ts": round(time.time(), 1),
        "temp": float(v[0]),
        "mem_temp": None if v[1] in ("N/A", "") else float(v[1]),
        "power_w": None if v[2] in ("N/A", "") else float(v[2]),
        "sm_mhz": float(v[3]),
        "util": float(v[4]),
        "throttle_mask": v[5],
        "mem_used_mib": float(v[6]),
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.split("?")[0] not in ("/temp", "/"):
            self.send_error(404)
            return
        now = time.time()
        with _lock:
            if now - _cache["ts"] > 1.0 or _cache["body"] is None:
                try:
                    body = {"ok": True, **sample()}
                except Exception as e:
                    body = {"ok": False, "error": str(e)[:200], "ts": round(now, 1)}
                _cache.update(ts=now, body=body)
            payload = json.dumps(_cache["body"]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
