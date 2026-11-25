#!/usr/bin/env python3
"""Gestiona modelos del router sin matar el proceso principal del contenedor."""
import configparser
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
URL = os.environ.get("NINFER_API_URL", "http://127.0.0.1:8090").rstrip("/")


def request(path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(URL + path, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def configured_name(name, ids):
    if name in ids:
        return name
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    cfg.read(ROOT / "ninfer-router.ini")
    aliases = {"qwen38": "qwen3.8-35b-a3b", "gemma": "gemma-4"}
    if name in aliases and aliases[name] in ids:
        return aliases[name]
    for ident in ids:
        if cfg.has_option(ident, "model") and Path(cfg.get(ident, "model")).name == name:
            return ident
    raise ValueError(f"Modelo {name!r} no está en el preset. Disponibles: {', '.join(ids)}")


def main(args):
    command = args[0] if args else "help"
    if command in ("help", "--help", "-h"):
        print("Uso: ninfer-mgr.sh list|status|ps|load <id>|unload|restart")
        return
    if command == "ps":
        subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader"], check=True)
        return
    if command == "restart":
        subprocess.run([str(ROOT / "ninfer-router.sh"), "restart"], check=True)
        return
    models = request("/models")["data"]
    if command in ("list", "status"):
        for model in models:
            print(f"{model['id']}: {model.get('status', {}).get('value', 'desconocido')}")
        return
    if command == "load":
        if len(args) != 2:
            raise ValueError("Uso: load <id>. Contexto y parámetros se configuran en ninfer-router.ini.")
        ident = configured_name(args[1], [m["id"] for m in models])
        print(json.dumps(request("/models/load", {"model": ident}), ensure_ascii=False))
        return
    if command in ("unload", "stop"):
        for model in models:
            if model.get("status", {}).get("value") in ("loaded", "sleeping"):
                print(model["id"], json.dumps(request("/models/unload", {"model": model["id"]})))
        return
    raise ValueError(f"Comando desconocido: {command}")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (OSError, ValueError, urllib.error.URLError, subprocess.CalledProcessError) as exc:
        print(f"ninfer: {exc}", file=sys.stderr)
        sys.exit(1)
