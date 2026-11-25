import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_load_actua_sobre_el_router_sin_matar_el_contenedor():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(("GET", self.path))
            body = {"data": [{"id": "qwen3.8-35b-a3b", "status": {"value": "unloaded"}}]}
            self.respond(body)

        def do_POST(self):
            calls.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.respond({"success": True})

        def respond(self, body):
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {**os.environ, "NINFER_API_URL": f"http://127.0.0.1:{server.server_port}"}
        result = subprocess.run([str(ROOT / "ninfer/ninfer-mgr.sh"), "load", "qwen3.8-35b-a3b"], env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert ("/models/load", {"model": "qwen3.8-35b-a3b"}) in calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
