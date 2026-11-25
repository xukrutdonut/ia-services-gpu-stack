"""Regresión del arranque del servicio ninfer integrado en Compose."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def service():
    data = subprocess.check_output(
        ["docker", "compose", "--profile", "*", "config", "--format", "json"],
        cwd=ROOT,
        text=True,
    )
    return json.loads(data)["services"]["ninfer-server"]


def test_ninfer_se_arranca_como_servicio_principal():
    svc = service()
    assert not svc.get("profiles"), "ninfer debe arrancar con el stack principal"
    assert svc["restart"] == "always"
    assert "sleep" not in " ".join(svc["command"])
    assert any("ninfer" in item for item in svc["command"])


def test_ninfer_usa_montaje_estable_y_preset():
    volumes = {item["target"]: item for item in service()["volumes"]}
    assert volumes["/models"]["source"] == "/mnt/data/modelos/ninfer"
    assert volumes["/models"]["read_only"] is True
    assert volumes["/models"]["bind"].get("create_host_path", False) is False
    assert volumes["/config/ninfer-router.ini"]["source"] == str(ROOT / "ninfer/ninfer-router.ini")
    assert volumes["/config/ninfer-router.ini"]["read_only"] is True
