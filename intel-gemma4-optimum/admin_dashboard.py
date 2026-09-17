"""
OpenVINO GenAI Admin Dashboard - Management API + SPA GUI.

Extends the existing server.py with:
  - Model management endpoints (load/unload/warmup/cache state)
  - GPU monitoring via sysfs (Intel Arc Xe2 iGPU)
  - System info (RAM, disk, devices, NPU)
  - Quick benchmark
  - Server logs tail
  - SPA dashboard at /admin (LM Studio-style GUI)
"""

import os
import re
import time
import json
import asyncio
import subprocess
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import psutil
import urllib.request
import urllib.error

# Import shared state from server.py
# These are module-level globals in server.py that we access at call time
# to avoid stale references.

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers: sysfs GPU metrics for Intel Arc Xe2 (iGPU at 0000:00:02.0, card2)
# ---------------------------------------------------------------------------
_IGT_PATH = "/sys/class/drm/card2/device/drm/card2"
_IGT_GT_PATH = "/sys/class/drm/card2/device/drm/card2/gt"


def _read_sysfs(path: str) -> Optional[str]:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _read_sysfs_int(path: str) -> Optional[int]:
    val = _read_sysfs(path)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            return None
    return None


def _get_gpu_tile_info(tile_dir: str) -> Dict[str, Any]:
    """Read frequency and throttle info for a GT tile."""
    info: Dict[str, Any] = {}
    for key in ["rps_act_freq_mhz", "rps_cur_freq_mhz", "rps_max_freq_mhz",
                "rps_min_freq_mhz", "rps_boost_freq_mhz", "punit_req_freq_mhz"]:
        val = _read_sysfs_int(os.path.join(tile_dir, key))
        if val is not None:
            info[key] = val

    rc6 = _read_sysfs_int(os.path.join(tile_dir, "rc6_residency_ms"))
    if rc6 is not None:
        info["rc6_residency_ms"] = rc6

    # Throttle reasons (1 = throttling active)
    throttle_reasons = {}
    for key in ["throttle_reason_status", "throttle_reason_pl1", "throttle_reason_pl2",
                "throttle_reason_pl4", "throttle_reason_thermal", "throttle_reason_prochot",
                "throttle_reason_ratl", "throttle_reason_vr_tdc", "throttle_reason_vr_thermalert"]:
        val = _read_sysfs_int(os.path.join(tile_dir, key))
        if val is not None and val != 0:
            throttle_reasons[key.replace("throttle_reason_", "")] = val
    if throttle_reasons:
        info["throttle_reasons"] = throttle_reasons

    return info


def _get_gpu_status() -> Dict[str, Any]:
    """Collect GPU metrics from sysfs."""
    status: Dict[str, Any] = {
        "device": "Intel Arc Xe2 128EU (iGPU)",
        "pci_id": "8086:7d51",
        "pci_slot": "0000:00:02.0",
        "driver": "i915",
    }

    # Overall GT freq
    for key in ["gt_act_freq_mhz", "gt_cur_freq_mhz", "gt_max_freq_mhz",
                "gt_min_freq_mhz", "gt_boost_freq_mhz"]:
        val = _read_sysfs_int(os.path.join(_IGT_PATH, key))
        if val is not None:
            status[key] = val

    # Per-tile info (gt0 = render/compute, gt1 = media)
    tiles = {}
    for tile in ["gt0", "gt1"]:
        tile_dir = os.path.join(_IGT_GT_PATH, tile)
        if os.path.isdir(tile_dir):
            tile_info = _get_gpu_tile_info(tile_dir)
            if tile_info:
                tiles[tile] = tile_info
    if tiles:
        status["tiles"] = tiles

    # GPU memory (shared system RAM on iGPU - report system RAM used by GPU processes)
    # iGPU uses shared RAM, no dedicated VRAM. We estimate GPU-allocated memory
    # by looking at the process memory of this server (the main GPU consumer).
    try:
        proc = psutil.Process()
        status["gpu_process_memory_mb"] = round(proc.memory_info().rss / 1024 / 1024, 1)
    except Exception:
        pass

    return status


# ---------------------------------------------------------------------------
# Helpers: AMD RX480 monitoring via sysfs (amdgpu at 0000:01:00.0, card1)
# ---------------------------------------------------------------------------

_AMD_CARD = "/sys/class/drm/card1/device"
_AMD_HWMON = "/sys/class/drm/card1/device/hwmon"

# LM Studio API (reachable via Docker host gateway)
_LMS_API_URL = os.environ.get("LMS_API_URL", "http://172.18.0.1:1235")


def _get_amd_status() -> Dict[str, Any]:
    """Collect AMD RX480 metrics from amdgpu sysfs + LM Studio API."""
    status: Dict[str, Any] = {
        "device": "AMD Radeon RX 480 8GB",
        "pci_id": "1002:67df",
        "pci_slot": "0000:01:00.0",
        "driver": "amdgpu",
        "available": False,
    }

    # Check if the AMD GPU sysfs exists
    if not os.path.isdir(_AMD_CARD):
        status["error"] = "AMD GPU sysfs no encontrado"
        return status

    status["available"] = True

    # GPU and memory busy percentages
    gpu_busy = _read_sysfs_int(os.path.join(_AMD_CARD, "gpu_busy_percent"))
    mem_busy = _read_sysfs_int(os.path.join(_AMD_CARD, "mem_busy_percent"))
    if gpu_busy is not None:
        status["gpu_busy_percent"] = gpu_busy
    if mem_busy is not None:
        status["mem_busy_percent"] = mem_busy

    # VRAM usage
    vram_used = _read_sysfs_int(os.path.join(_AMD_CARD, "mem_info_vram_used"))
    vram_total = _read_sysfs_int(os.path.join(_AMD_CARD, "mem_info_vram_total"))
    if vram_used is not None and vram_total is not None:
        status["vram_used_mb"] = round(vram_used / 1024 / 1024, 1)
        status["vram_total_mb"] = round(vram_total / 1024 / 1024, 1)
        status["vram_percent"] = round(vram_used / vram_total * 100, 1) if vram_total > 0 else 0

    # GTT (shared system memory used by amdgpu)
    gtt_used = _read_sysfs_int(os.path.join(_AMD_CARD, "mem_info_gtt_used"))
    gtt_total = _read_sysfs_int(os.path.join(_AMD_CARD, "mem_info_gtt_total"))
    if gtt_used is not None and gtt_total is not None:
        status["gtt_used_mb"] = round(gtt_used / 1024 / 1024, 1)
        status["gtt_total_mb"] = round(gtt_total / 1024 / 1024, 1)

    # Power DPM state
    dpm_state = _read_sysfs(os.path.join(_AMD_CARD, "power_dpm_state"))
    if dpm_state:
        status["power_dpm_state"] = dpm_state

    # HWMON metrics (freq, power, temp, fan, voltage)
    hwmon_dir = None
    try:
        for h in os.listdir(_AMD_HWMON):
            full = os.path.join(_AMD_HWMON, h)
            if os.path.isdir(full):
                hwmon_dir = full
                break
    except (FileNotFoundError, PermissionError, OSError):
        pass

    if hwmon_dir:
        hwmon: Dict[str, Any] = {}

        # Clocks (freq1=sclk GPU, freq2=mclk memory) in Hz
        freq1 = _read_sysfs_int(os.path.join(hwmon_dir, "freq1_input"))
        freq1_label = _read_sysfs(os.path.join(hwmon_dir, "freq1_label"))
        if freq1 is not None:
            hwmon["sclk_mhz"] = round(freq1 / 1_000_000, 1)
            if freq1_label:
                hwmon["sclk_label"] = freq1_label

        freq2 = _read_sysfs_int(os.path.join(hwmon_dir, "freq2_input"))
        freq2_label = _read_sysfs(os.path.join(hwmon_dir, "freq2_label"))
        if freq2 is not None:
            hwmon["mclk_mhz"] = round(freq2 / 1_000_000, 1)
            if freq2_label:
                hwmon["mclk_label"] = freq2_label

        # Power (in microwatts)
        power = _read_sysfs_int(os.path.join(hwmon_dir, "power1_input"))
        if power is not None:
            hwmon["power_w"] = round(power / 1_000_000, 2)
        power_cap = _read_sysfs_int(os.path.join(hwmon_dir, "power1_cap"))
        if power_cap is not None:
            hwmon["power_cap_w"] = round(power_cap / 1_000_000, 2)

        # Voltage (in mV)
        vddgfx = _read_sysfs_int(os.path.join(hwmon_dir, "in0_input"))
        if vddgfx is not None:
            hwmon["vddgfx_mv"] = vddgfx

        # Fan (RPM)
        fan = _read_sysfs_int(os.path.join(hwmon_dir, "fan1_input"))
        if fan is not None:
            hwmon["fan_rpm"] = fan
        fan_target = _read_sysfs_int(os.path.join(hwmon_dir, "fan1_target"))
        if fan_target is not None:
            hwmon["fan_target_rpm"] = fan_target

        # Temperature (in millidegrees)
        temp = _read_sysfs_int(os.path.join(hwmon_dir, "temp1_input"))
        if temp is not None:
            hwmon["temp_c"] = round(temp / 1000, 1)
        temp_label = _read_sysfs(os.path.join(hwmon_dir, "temp1_label"))
        if temp_label:
            hwmon["temp_label"] = temp_label

        status["hwmon"] = hwmon

    # Container status: check if LM Studio API responds (acts as liveness)
    # docker CLI is not available inside the container, so we use the API itself.
    # This is set below when we query LM Studio.

    # LM Studio API: list models + liveness
    try:
        import urllib.request
        url = f"{_LMS_API_URL}/v1/models"
        req = urllib.request.Request(url, headers={"User-Agent": "admin-dashboard"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            models = [m.get("id", "?") for m in data.get("data", [])]
            status["lms_models"] = models
            status["lms_api_online"] = True
    except Exception as e:
        status["lms_api_online"] = False
        status["lms_error"] = str(e)[:120]

    return status


def _get_system_info() -> Dict[str, Any]:
    """Collect system info via psutil."""
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/models" if os.path.exists("/models") else "/")
    net = psutil.net_io_counters()

    # CPU info
    cpu_info = {
        "cores_physical": psutil.cpu_count(logical=False) or 0,
        "cores_logical": psutil.cpu_count(logical=True) or 0,
        "load_percent": psutil.cpu_percent(interval=0.5),
        "freq_mhz": 0,
    }
    try:
        freq = psutil.cpu_freq()
        if freq:
            cpu_info["freq_mhz"] = round(freq.current)
    except Exception:
        pass

    # OpenVINO devices
    devices = []
    try:
        import openvino as ov
        core = ov.Core()
        devices = core.available_devices
    except Exception:
        pass

    return {
        "cpu": cpu_info,
        "memory": {
            "total_mb": round(vm.total / 1024 / 1024),
            "used_mb": round(vm.used / 1024 / 1024),
            "available_mb": round(vm.available / 1024 / 1024),
            "percent": vm.percent,
        },
        "disk": {
            "total_gb": round(disk.total / 1024**3, 1),
            "used_gb": round(disk.used / 1024**3, 1),
            "free_gb": round(disk.free / 1024**3, 1),
            "percent": disk.percent,
            "path": "/models" if os.path.exists("/models") else "/",
        },
        "network": {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
        },
        "openvino_devices": devices,
        "uptime_seconds": round(time.time() - psutil.boot_time()),
    }


# ---------------------------------------------------------------------------
# Helpers: model discovery and cache state
# ---------------------------------------------------------------------------

def _get_available_models() -> List[Dict[str, Any]]:
    """List all models in MODELS_DIR with metadata."""
    # Access MODELS_DIR from server.py at call time
    import sys
    server_mod = sys.modules.get("server")
    models_dir = getattr(server_mod, "MODELS_DIR", os.environ.get("MODELS_DIR", "/models/.openvino-ir"))

    models = []
    if not os.path.isdir(models_dir):
        return models

    # Get currently loaded models from cache
    loaded_paths = set()
    if server_mod:
        loaded_paths = set(getattr(server_mod, "_pipeline_cache", {}).keys())

    embedding_models = set()
    if server_mod:
        for em in [getattr(server_mod, "EMBEDDING_MODEL_NAME", ""),
                   os.environ.get("EMBEDDING_MODEL_NAME_2", "")]:
            if em:
                embedding_models.add(em)

    for entry in sorted(os.listdir(models_dir)):
        full = os.path.join(models_dir, entry)
        if not os.path.isdir(full):
            continue

        has_model = (os.path.exists(os.path.join(full, "openvino_model.xml")) or
                     os.path.exists(os.path.join(full, "openvino_language_model.xml")))
        if not has_model:
            continue

        # Calculate size
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(full):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    try:
                        total_size += os.path.getsize(fp)
                    except OSError:
                        pass

        # Detect model type
        is_multimodal = os.path.exists(os.path.join(full, "openvino_vision_embeddings_model.xml"))
        is_embedding = entry in embedding_models or "embed" in entry.lower() or "minilm" in entry.lower()

        if is_multimodal:
            model_type = "VLM (Multimodal)"
        elif is_embedding:
            model_type = "Embedding"
        else:
            model_type = "LLM"

        models.append({
            "name": entry,
            "path": full,
            "size_mb": round(total_size / 1024 / 1024, 1),
            "type": model_type,
            "loaded": full in loaded_paths,
            "multimodal": is_multimodal,
        })

    return models


def _get_cache_state() -> Dict[str, Any]:
    """Get current LRU cache state."""
    import sys
    server_mod = sys.modules.get("server")
    if not server_mod:
        return {"models": [], "max_cached": 3}

    cache = getattr(server_mod, "_pipeline_cache", {})
    max_cached = getattr(server_mod, "MAX_CACHED_MODELS", 3)

    models = []
    # OrderedDict: first = LRU, last = MRU
    for i, (path, pipe) in enumerate(cache.items()):
        name = os.path.basename(path)
        models.append({
            "name": name,
            "path": path,
            "lru_position": i,  # 0 = least recently used
            "is_lru": i == 0,   # next to be evicted
        })

    return {
        "models": models,
        "count": len(models),
        "max_cached": max_cached,
        "slots_used": f"{len(models)}/{max_cached}",
    }


# ---------------------------------------------------------------------------
# Management API Endpoints
# ---------------------------------------------------------------------------

@router.get("/v1/admin/system", response_class=JSONResponse)
async def admin_system_info():
    """System information: CPU, RAM, disk, OpenVINO devices."""
    return _get_system_info()


@router.get("/v1/admin/gpu/status", response_class=JSONResponse)
async def admin_gpu_status():
    """GPU metrics from sysfs (Intel Arc Xe2 iGPU)."""
    return _get_gpu_status()


@router.get("/v1/admin/models", response_class=JSONResponse)
async def admin_list_models():
    """List all available models with metadata and loaded status."""
    return {"models": _get_available_models()}


@router.get("/v1/admin/cache", response_class=JSONResponse)
async def admin_cache_state():
    """Current LRU cache state."""
    return _get_cache_state()


@router.post("/v1/admin/models/{model_name}/load", response_class=JSONResponse)
async def admin_load_model(model_name: str):
    """Load a model into the LRU cache (async, non-blocking)."""
    import sys
    server_mod = sys.modules.get("server")
    if not server_mod:
        raise HTTPException(500, "server module not available")

    models = _get_available_models()
    found = [m for m in models if m["name"] == model_name]
    if not found:
        raise HTTPException(404, f"Model '{model_name}' not found")

    # Check if already loaded
    cache = getattr(server_mod, "_pipeline_cache", {})
    models_dir = getattr(server_mod, "MODELS_DIR", "/models/.openvino-ir")
    target_path = os.path.join(models_dir, model_name)
    if target_path in cache:
        return {"status": "already_loaded", "model": model_name}

    # Load in thread to avoid blocking the event loop
    def _load():
        try:
            server_mod.get_or_load_pipeline(model_name)
        except Exception as e:
            server_mod.log.error(f"Admin load failed for '{model_name}': {e}")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _load)
    return {"status": "loaded", "model": model_name}


@router.post("/v1/admin/models/{model_name}/unload", response_class=JSONResponse)
async def admin_unload_model(model_name: str):
    """Unload a model from the LRU cache."""
    import sys
    server_mod = sys.modules.get("server")
    if not server_mod:
        raise HTTPException(500, "server module not available")

    models_dir = getattr(server_mod, "MODELS_DIR", "/models/.openvino-ir")
    target_path = os.path.join(models_dir, model_name)

    cache = getattr(server_mod, "_pipeline_cache", {})
    cache_lock = getattr(server_mod, "_cache_lock", None)

    if target_path not in cache:
        raise HTTPException(404, f"Model '{model_name}' not in cache")

    if cache_lock:
        with cache_lock:
            if target_path in cache:
                pipe = cache.pop(target_path)
                del pipe
                server_mod.log.info(f"Admin unloaded model: {model_name}")
    else:
        pipe = cache.pop(target_path, None)
        if pipe:
            del pipe

    return {"status": "unloaded", "model": model_name}


@router.post("/v1/admin/models/{model_name}/warmup", response_class=JSONResponse)
async def admin_warmup_model(model_name: str):
    """Warmup a model (load + generate 1 token to pay cold-start cost)."""
    import sys
    server_mod = sys.modules.get("server")
    if not server_mod:
        raise HTTPException(500, "server module not available")

    def _warmup():
        try:
            pipe = server_mod.get_or_load_pipeline(model_name)
            import openvino_genai as ov_genai
            cfg = ov_genai.GenerationConfig()
            cfg.max_new_tokens = 1
            pipe.generate("Hello", cfg)
            server_mod.log.info(f"Admin warmup completed: {model_name}")
        except Exception as e:
            server_mod.log.error(f"Admin warmup failed for '{model_name}': {e}")
            raise

    loop = asyncio.get_event_loop()
    t0 = time.time()
    await loop.run_in_executor(None, _warmup)
    elapsed = time.time() - t0
    return {"status": "warmup_complete", "model": model_name, "elapsed_seconds": round(elapsed, 2)}


@router.post("/v1/admin/benchmark", response_class=JSONResponse)
async def admin_benchmark(request: Request):
    """Quick benchmark a model: measure tokens/second."""
    body = await request.json()
    model_name = body.get("model")
    prompt = body.get("prompt", "Explain the concept of recursion in programming, with examples.")
    max_tokens = body.get("max_tokens", 128)

    if not model_name:
        raise HTTPException(400, "model is required")

    import sys
    server_mod = sys.modules.get("server")
    if not server_mod:
        raise HTTPException(500, "server module not available")

    def _bench():
        try:
            pipe = server_mod.get_or_load_pipeline(model_name)
            import openvino_genai as ov_genai
            cfg = ov_genai.GenerationConfig()
            cfg.max_new_tokens = max_tokens
            cfg.temperature = 0.7
            cfg.top_p = 0.9

            t0 = time.time()
            result = pipe.generate(prompt, cfg)
            elapsed = time.time() - t0

            text = result if isinstance(result, str) else str(result)
            tokens_generated = len(text.split())  # rough estimate
            tps = max_tokens / elapsed if elapsed > 0 else 0

            return {
                "model": model_name,
                "prompt": prompt[:100] + "..." if len(prompt) > 100 else prompt,
                "max_tokens": max_tokens,
                "elapsed_seconds": round(elapsed, 2),
                "tokens_per_second": round(tps, 1),
                "output_preview": text[:500],
            }
        except Exception as e:
            raise RuntimeError(str(e))

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _bench)
        return result
    except Exception as e:
        raise HTTPException(500, f"Benchmark failed: {e}")


@router.get("/v1/admin/logs", response_class=JSONResponse)
async def admin_logs(lines: int = 100):
    """Tail server logs."""
    log_path = "/tmp/openvino_server.log"
    try:
        with open(log_path, "r") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"lines": [l.rstrip() for l in tail], "total": len(all_lines)}
    except FileNotFoundError:
        return {"lines": [], "total": 0, "error": "log file not found"}


@router.get("/v1/admin/amd/status", response_class=JSONResponse)
async def admin_amd_status():
    """AMD RX480 metrics from amdgpu sysfs + LM Studio API."""
    return _get_amd_status()


# ---------------------------------------------------------------------------
# LM Studio model management (AMD RX480 backend)
# ---------------------------------------------------------------------------

def _lms_base_url() -> str:
    return os.environ.get("LMS_API_URL", "http://172.18.0.1:1235")


@router.get("/v1/admin/lms/models", response_class=JSONResponse)
async def admin_lms_models():
    """List models available in LM Studio with loaded/not-loaded state."""
    base = _lms_base_url()
    try:
        url = f"{base}/api/v1/models"
        req = urllib.request.Request(url, headers={"User-Agent": "admin-dashboard"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        models = []
        for m in data.get("models", []):
            instances = m.get("loaded_instances", [])
            models.append({
                "key": m.get("key", ""),
                "display_name": m.get("display_name", m.get("key", "")),
                "type": m.get("type", ""),
                "architecture": m.get("architecture", ""),
                "quantization": m.get("quantization", {}).get("name", ""),
                "size_mb": round(m.get("size_bytes", 0) / 1048576, 1),
                "params": m.get("params_string", ""),
                "max_context": m.get("max_context_length", 0),
                "loaded": len(instances) > 0,
                "instance_id": instances[0] if instances else None,
                "capabilities": {
                    "vision": m.get("capabilities", {}).get("vision", False),
                    "tool_use": m.get("capabilities", {}).get("trained_for_tool_use", False),
                },
            })
        return {"models": models, "api_online": True}
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"models": [], "api_online": False, "error": str(e)},
        )


@router.post("/v1/admin/lms/models/{model_key}/load", response_class=JSONResponse)
async def admin_lms_load(model_key: str):
    """Load a model in LM Studio."""
    base = _lms_base_url()
    try:
        url = f"{base}/api/v1/models/load"
        body = json.dumps({"model": model_key}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return JSONResponse(status_code=e.code, content={"error": e.read().decode()[:500]})
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@router.post("/v1/admin/lms/models/{model_key}/unload", response_class=JSONResponse)
async def admin_lms_unload(model_key: str):
    """Unload a model from LM Studio."""
    base = _lms_base_url()
    try:
        url = f"{base}/api/v1/models/unload"
        body = json.dumps({"instance_id": model_key}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return JSONResponse(status_code=e.code, content={"error": e.read().decode()[:500]})
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@router.post("/v1/admin/lms/benchmark", response_class=JSONResponse)
async def admin_lms_benchmark(request: Request):
    """Benchmark a model served by LM Studio via OpenAI-compatible /v1/chat/completions."""
    body = await request.json()
    model_key = body.get("model", "")
    prompt = body.get("prompt", "Explain the concept of recursion in programming, with examples.")
    max_tokens = int(body.get("max_tokens", 128))
    if not model_key:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    base = _lms_base_url()
    try:
        # Ensure model is loaded first
        load_url = f"{base}/api/v1/models/load"
        load_body = json.dumps({"model": model_key}).encode()
        load_req = urllib.request.Request(load_url, data=load_body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(load_req, timeout=120) as resp:
                json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # 409 = already loaded, that's fine
            if e.code != 409:
                err_body = e.read().decode()[:300]
                return JSONResponse(status_code=e.code, content={"error": f"Load failed: {err_body}"})

        # Run chat completion with timing
        chat_url = f"{base}/v1/chat/completions"
        chat_body = json.dumps({
            "model": model_key,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": False,
        }).encode()
        chat_req = urllib.request.Request(chat_url, data=chat_body, headers={"Content-Type": "application/json"}, method="POST")

        t0 = time.time()
        with urllib.request.urlopen(chat_req, timeout=300) as resp:
            chat_data = json.loads(resp.read().decode())
        elapsed = round(time.time() - t0, 2)

        usage = chat_data.get("usage", {})
        completion_tokens = usage.get("completion_tokens", max_tokens)
        tps = round(completion_tokens / elapsed, 2) if elapsed > 0 else 0
        output_text = ""
        choices = chat_data.get("choices", [])
        if choices:
            output_text = choices[0].get("message", {}).get("content", "")[:800]

        return {
            "model": model_key,
            "tokens_per_second": tps,
            "elapsed_seconds": elapsed,
            "completion_tokens": completion_tokens,
            "max_tokens": max_tokens,
            "output_preview": output_text,
        }
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Dashboard HTML/CSS/JS (single-file SPA, no external dependencies)
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    """LM Studio-style management dashboard."""
    return _DASHBOARD_HTML


# ---------------------------------------------------------------------------
# Dashboard HTML/CSS/JS (single-file SPA, no external dependencies)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenVINO GenAI - Panel de Gestion</title>
<style>
:root {
  --bg-primary: #1a1a2e;
  --bg-secondary: #16213e;
  --bg-tertiary: #0f3460;
  --bg-card: #1e2a4a;
  --bg-hover: #2a3a5c;
  --text-primary: #e0e0e0;
  --text-secondary: #a0a0b0;
  --text-muted: #6a6a7a;
  --accent: #4e9af5;
  --accent-hover: #6bb0ff;
  --success: #4caf50;
  --warning: #ff9800;
  --error: #f44336;
  --border: #2a2a4a;
  --radius: 8px;
  --sidebar-width: 280px;
  --font-size-label: 12px;
  --font-size-input: 13px;
  --font-size-body: 13px;
  --font-size-heading: 15px;
}
[data-theme="light"] {
  --bg-primary: #f5f5f5;
  --bg-secondary: #ffffff;
  --bg-tertiary: #e8e8e8;
  --bg-card: #ffffff;
  --bg-hover: #e0e0e0;
  --text-primary: #1a1a2e;
  --text-secondary: #444;
  --text-muted: #888;
  --border: #ddd;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-size: var(--font-size-body);
  overflow: hidden;
  height: 100vh;
}
.app { display: flex; height: 100vh; }
/* Sidebar */
.sidebar {
  width: var(--sidebar-width);
  background: var(--bg-secondary);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.sidebar-header {
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.sidebar-header h1 {
  font-size: var(--font-size-heading);
  font-weight: 600;
  color: var(--accent);
}
.theme-toggle {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 4px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 11px;
}
.theme-toggle:hover { background: var(--bg-hover); }
/* Hardware selector */
.hw-selector {
  display: flex; flex-direction: column; gap: 4px; padding: 8px;
}
.hw-btn {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; border-radius: var(--radius);
  cursor: pointer; border: 1px solid var(--border);
  background: var(--bg-tertiary); transition: all 0.15s;
}
.hw-btn:hover { background: var(--bg-hover); }
.hw-btn.active {
  border-color: var(--accent); background: rgba(78,154,245,0.15);
}
.hw-icon { font-size: 18px; flex-shrink: 0; }
.hw-label { font-size: 13px; font-weight: 500; line-height: 1.3; }
.hw-label small { color: var(--text-muted); font-weight: 400; }
.hw-btn.active .hw-label { color: var(--accent); }
.sidebar-section-title {
  padding: 8px 14px 4px;
  font-size: var(--font-size-label);
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  font-weight: 600;
}
.model-list { flex: 1; overflow-y: auto; padding: 4px 8px; }
.model-item {
  padding: 10px 12px;
  margin: 2px 0;
  border-radius: var(--radius);
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.15s;
}
.model-item:hover { background: var(--bg-hover); }
.model-item.selected { border-color: var(--accent); background: var(--bg-tertiary); }
.model-item-header { display: flex; align-items: center; justify-content: space-between; }
.model-name { font-size: var(--font-size-body); font-weight: 500; word-break: break-all; }
.model-badge {
  font-size: 10px; padding: 2px 6px; border-radius: 4px; white-space: nowrap; margin-left: 6px;
}
.badge-loaded { background: rgba(76,175,80,0.2); color: var(--success); }
.badge-unloaded { background: rgba(160,160,176,0.15); color: var(--text-muted); }
.badge-type { background: var(--bg-tertiary); color: var(--text-secondary); }
.model-meta {
  font-size: var(--font-size-label); color: var(--text-muted); margin-top: 4px;
  display: flex; gap: 10px; flex-wrap: wrap;
}
.model-actions { margin-top: 6px; display: flex; gap: 6px; }
.btn-mini {
  padding: 3px 10px; font-size: 11px; border-radius: 4px;
  border: 1px solid var(--border); background: var(--bg-tertiary);
  color: var(--text-primary); cursor: pointer; transition: all 0.15s;
}
.btn-mini:hover { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn-mini.btn-danger:hover { background: var(--error); border-color: var(--error); }
.btn-mini:disabled { opacity: 0.4; cursor: not-allowed; }
/* Main area */
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.topbar {
  padding: 10px 16px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 16px;
}
.topbar h2 { font-size: var(--font-size-heading); font-weight: 600; }
.tabs { display: flex; gap: 2px; }
.tab {
  padding: 6px 16px; font-size: var(--font-size-label); border-radius: 6px 6px 0 0;
  cursor: pointer; color: var(--text-secondary); border-bottom: 2px solid transparent;
  transition: all 0.15s;
}
.tab:hover { color: var(--text-primary); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.content { flex: 1; overflow-y: auto; padding: 16px; }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
/* Cards */
.card {
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px; margin-bottom: 14px;
}
.card-title { font-size: var(--font-size-heading); font-weight: 600; margin-bottom: 12px; }
/* Metrics grid */
.metrics-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.metric-box {
  background: var(--bg-tertiary); border-radius: 6px; padding: 12px;
}
.metric-label { font-size: var(--font-size-label); color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.3px; }
.metric-value { font-size: 18px; font-weight: 600; margin-top: 4px; }
.metric-sub { font-size: var(--font-size-label); color: var(--text-secondary); margin-top: 2px; }
/* Progress bars */
.progress-bar {
  height: 6px; background: var(--bg-primary); border-radius: 3px;
  margin-top: 6px; overflow: hidden;
}
.progress-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
.progress-green { background: var(--success); }
.progress-orange { background: var(--warning); }
.progress-red { background: var(--error); }
.progress-blue { background: var(--accent); }
/* Cache display */
.cache-bar {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  background: var(--bg-tertiary); border-radius: 6px; margin-bottom: 10px;
  font-size: var(--font-size-label);
}
.cache-slot {
  flex: 1; height: 24px; border-radius: 4px; border: 1px solid var(--border);
  display: flex; align-items: center; justify-content: center;
  font-size: 10px; background: var(--bg-primary); color: var(--text-muted);
  text-align: center; padding: 0 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cache-slot.filled { background: rgba(78,154,245,0.2); color: var(--accent); border-color: var(--accent); }
.cache-slot.lru { border-style: dashed; }
/* Logs */
.log-viewer {
  background: #0d0d1a; color: #b0b0c0; border-radius: 6px;
  padding: 12px; font-family: 'Courier New', monospace; font-size: 12px;
  max-height: 500px; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
}
[data-theme="light"] .log-viewer { background: #1a1a2e; color: #c0c0d0; }
.log-line { padding: 1px 0; }
.log-line.error { color: var(--error); }
.log-line.warning { color: var(--warning); }
/* Benchmark */
.benchmark-form { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 14px; }
.form-field { display: flex; flex-direction: column; gap: 4px; }
.form-field label { font-size: var(--font-size-label); color: var(--text-muted); }
.form-field input, .form-field select, .form-field textarea {
  background: var(--bg-tertiary); border: 1px solid var(--border); color: var(--text-primary);
  padding: 6px 10px; border-radius: 4px; font-size: var(--font-size-input);
}
.form-field textarea { min-height: 60px; resize: vertical; min-width: 300px; }
.btn-primary {
  padding: 8px 20px; background: var(--accent); color: #fff; border: none;
  border-radius: 6px; cursor: pointer; font-size: var(--font-size-input); font-weight: 500;
  transition: background 0.15s;
}
.btn-primary:hover { background: var(--accent-hover); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.benchmark-result {
  background: var(--bg-tertiary); border-radius: 6px; padding: 14px; margin-top: 12px;
}
.tps-big { font-size: 28px; font-weight: 700; color: var(--success); }
/* Throttle warnings */
.throttle-warning {
  background: rgba(244,67,54,0.15); border: 1px solid var(--error);
  border-radius: 6px; padding: 8px 12px; margin-top: 8px;
  font-size: var(--font-size-label); color: var(--error);
}
/* Loading spinner */
.spinner {
  display: inline-block; width: 14px; height: 14px;
  border: 2px solid var(--border); border-top-color: var(--accent);
  border-radius: 50%; animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
/* Status indicator */
.status-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px;
}
.dot-green { background: var(--success); }
.dot-red { background: var(--error); }
.dot-yellow { background: var(--warning); }
/* GPU freq chart */
.freq-chart {
  display: flex; align-items: flex-end; gap: 0px; height: 80px;
  margin-top: 10px; padding: 4px; background: var(--bg-primary); border-radius: 4px;
  overflow: hidden;
}
.freq-bar {
  flex: 1 1 0; min-width: 1px; width: 0; background: var(--accent); border-radius: 0;
  transition: height 0.3s; opacity: 0.8;
}
.freq-bar.peak { background: var(--warning); }
</style>
</head>
<body data-theme="dark">
<div class="app">
  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sidebar-header">
      <button class="theme-toggle" onclick="toggleTheme()">&#9728;</button>
    </div>
    <!-- Hardware selector -->
    <div class="hw-selector">
      <div class="hw-btn active" id="hw-intel" onclick="switchHardware('intel')">
        <span class="hw-icon">&#9889;</span>
        <span class="hw-label">OpenVINO GenAI<br><small>Intel Arc Xe2</small></span>
      </div>
      <div class="hw-btn" id="hw-amd" onclick="switchHardware('amd')">
        <span class="hw-icon">&#9672;</span>
        <span class="hw-label">LM Studio<br><small>AMD RX480 8GB</small></span>
      </div>
    </div>

    <!-- Intel sidebar content -->
    <div id="sidebar-intel">
      <div class="sidebar-section-title">Modelos disponibles</div>
      <div class="model-list" id="modelList">
        <div style="padding:12px;color:var(--text-muted);text-align:center;"><span class="spinner"></span> Cargando...</div>
      </div>
      <div class="sidebar-section-title">Cache LRU</div>
      <div style="padding:4px 12px 10px;">
        <div id="cacheBar"></div>
      </div>
    </div>

    <!-- AMD sidebar content -->
    <div id="sidebar-amd" style="display:none;">
      <div class="sidebar-section-title">Modelos LM Studio</div>
      <div class="model-list" id="lmsModelList">
        <div style="padding:12px;color:var(--text-muted);text-align:center;"><span class="spinner"></span> Cargando...</div>
      </div>
      <div class="sidebar-section-title">Estado GPU AMD</div>
      <div style="padding:6px 12px;font-size:12px;">
        <div style="display:flex;justify-content:space-between;margin-bottom:3px;"><span>Carga GPU</span><span id="amdSbGpuBusy">--</span></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:3px;"><span>VRAM</span><span id="amdSbVram">--</span></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:3px;"><span>Temperatura</span><span id="amdSbTemp">--</span></div>
        <div style="display:flex;justify-content:space-between;"><span>Potencia</span><span id="amdSbPower">--</span></div>
      </div>
    </div>
  </div>

  <!-- Main -->
  <div class="main">
    <div class="topbar">
      <h2 id="topbarTitle">Panel de Gestion</h2>
      <div class="tabs">
        <div class="tab active" data-tab="gpu" onclick="switchTab('gpu')">Monitor GPU</div>
        <div class="tab" data-tab="system" onclick="switchTab('system')">Sistema</div>
        <div class="tab" data-tab="benchmark" onclick="switchTab('benchmark')">Benchmark</div>
        <div class="tab" data-tab="logs" onclick="switchTab('logs')">Logs</div>
      </div>
      <div style="margin-left:auto;display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-muted);">
        <span class="status-dot" id="statusDot"></span>
        <span id="statusText">Conectando...</span>
      </div>
    </div>
    <div class="content">
      <!-- GPU Monitor -->
      <div class="tab-panel active" id="tab-gpu">
        <div class="card">
          <div class="card-title">Intel Arc Xe2 128EU - Monitor de GPU</div>
          <div class="metrics-grid" id="gpuMetrics">
            <div class="metric-box"><div class="metric-label">Frecuencia actual</div><div class="metric-value" id="gpuFreq">--</div><div class="metric-sub">MHz</div></div>
            <div class="metric-box"><div class="metric-label">Frecuencia maxima</div><div class="metric-value" id="gpuMaxFreq">--</div><div class="metric-sub">MHz</div></div>
            <div class="metric-box"><div class="metric-label">Memoria proceso GPU</div><div class="metric-value" id="gpuMem">--</div><div class="metric-sub">MB RAM compartida</div></div>
            <div class="metric-box"><div class="metric-label">RC6 Residency (gt0)</div><div class="metric-value" id="gpuRc6">--</div><div class="metric-sub">% ahorro energia</div></div>
          </div>
          <div class="freq-chart" id="freqChart"></div>
          <div id="throttleWarnings"></div>
        </div>
        <div class="card">
          <div class="card-title">Tiles de GPU</div>
          <div id="tilesInfo" style="font-size:13px;color:var(--text-secondary);">Cargando...</div>
        </div>
      </div>

      <!-- System -->
      <div class="tab-panel" id="tab-system">
        <div class="card">
          <div class="card-title">Memoria del sistema</div>
          <div class="metrics-grid">
            <div class="metric-box"><div class="metric-label">Total</div><div class="metric-value" id="memTotal">--</div><div class="metric-sub">MB</div></div>
            <div class="metric-box"><div class="metric-label">Usada</div><div class="metric-value" id="memUsed">--</div><div class="metric-sub">MB</div></div>
            <div class="metric-box"><div class="metric-label">Disponible</div><div class="metric-value" id="memAvail">--</div><div class="metric-sub">MB</div></div>
            <div class="metric-box"><div class="metric-label">Uso</div><div class="metric-value" id="memPercent">--</div><div class="metric-sub">%</div></div>
          </div>
          <div class="progress-bar"><div class="progress-fill" id="memBar" style="width:0%"></div></div>
        </div>
        <div class="card">
          <div class="card-title">CPU</div>
          <div class="metrics-grid">
            <div class="metric-box"><div class="metric-label">Nucleos fisicos</div><div class="metric-value" id="cpuPhysical">--</div></div>
            <div class="metric-box"><div class="metric-label">Nucleos logicos</div><div class="metric-value" id="cpuLogical">--</div></div>
            <div class="metric-box"><div class="metric-label">Frecuencia</div><div class="metric-value" id="cpuFreq">--</div><div class="metric-sub">MHz</div></div>
            <div class="metric-box"><div class="metric-label">Carga</div><div class="metric-value" id="cpuLoad">--</div><div class="metric-sub">%</div></div>
          </div>
          <div class="progress-bar"><div class="progress-fill" id="cpuBar" style="width:0%"></div></div>
        </div>
        <div class="card">
          <div class="card-title">Almacenamiento</div>
          <div class="metrics-grid">
            <div class="metric-box"><div class="metric-label">Total</div><div class="metric-value" id="diskTotal">--</div><div class="metric-sub">GB</div></div>
            <div class="metric-box"><div class="metric-label">Usado</div><div class="metric-value" id="diskUsed">--</div><div class="metric-sub">GB</div></div>
            <div class="metric-box"><div class="metric-label">Libre</div><div class="metric-value" id="diskFree">--</div><div class="metric-sub">GB</div></div>
            <div class="metric-box"><div class="metric-label">Uso</div><div class="metric-value" id="diskPercent">--</div><div class="metric-sub">%</div></div>
          </div>
          <div class="progress-bar"><div class="progress-fill" id="diskBar" style="width:0%"></div></div>
        </div>
        <div class="card">
          <div class="card-title">Dispositivos OpenVINO</div>
          <div id="ovDevices" style="font-size:13px;"></div>
        </div>
      </div>

      <!-- Benchmark -->
      <div class="tab-panel" id="tab-benchmark">
        <div class="card">
          <div class="card-title">Benchmark rapido</div>
          <p style="font-size:12px;color:var(--text-muted);margin-bottom:12px;">
            Mide tokens/segundo de un modelo. El modelo se carga automaticamente si no esta en cache.
          </p>
          <div class="benchmark-form">
            <div class="form-field">
              <label>Modelo</label>
              <select id="benchModel" style="min-width:200px;"></select>
            </div>
            <div class="form-field">
              <label>Max tokens</label>
              <input type="number" id="benchMaxTokens" value="128" style="width:80px;">
            </div>
            <button class="btn-primary" id="benchBtn" onclick="runBenchmark()">Ejecutar benchmark</button>
          </div>
          <div class="form-field" style="margin-top:8px;">
            <label>Prompt</label>
            <textarea id="benchPrompt">Explain the concept of recursion in programming, with examples.</textarea>
          </div>
          <div id="benchResult"></div>
        </div>
      </div>

      <!-- Logs -->
      <div class="tab-panel" id="tab-logs">
        <div class="card">
          <div class="card-title">
            Logs del servidor
            <button class="btn-mini" style="margin-left:8px;" onclick="refreshLogs()">Actualizar</button>
            <label style="font-size:12px;margin-left:12px;color:var(--text-muted);">
              <input type="checkbox" id="autoScrollLogs" checked> Auto-actualizar (5s)
            </label>
          </div>
          <div class="log-viewer" id="logViewer">Cargando...</div>
        </div>
      </div>

      <!-- AMD RX480 Monitor -->
      <div class="tab-panel" id="tab-amd">
        <div class="card">
          <div class="card-title">AMD Radeon RX 480 8GB - Monitor de GPU</div>
          <div class="metrics-grid" id="amdMetrics">
            <div class="metric-box"><div class="metric-label">Carga GPU</div><div class="metric-value" id="amdGpuBusy">--</div><div class="metric-sub">% uso</div></div>
            <div class="metric-box"><div class="metric-label">Carga memoria</div><div class="metric-value" id="amdMemBusy">--</div><div class="metric-sub">% uso</div></div>
            <div class="metric-box"><div class="metric-label">VRAM usada</div><div class="metric-value" id="amdVram">--</div><div class="metric-sub">MB / 8192 MB</div></div>
            <div class="metric-box"><div class="metric-label">SCLK (GPU)</div><div class="metric-value" id="amdSclk">--</div><div class="metric-sub">MHz</div></div>
            <div class="metric-box"><div class="metric-label">MCLK (memoria)</div><div class="metric-value" id="amdMclk">--</div><div class="metric-sub">MHz</div></div>
            <div class="metric-box"><div class="metric-label">Temperatura</div><div class="metric-value" id="amdTemp">--</div><div class="metric-sub">&deg;C edge</div></div>
            <div class="metric-box"><div class="metric-label">Potencia</div><div class="metric-value" id="amdPower">--</div><div class="metric-sub">W / 130W cap</div></div>
            <div class="metric-box"><div class="metric-label">Ventilador</div><div class="metric-value" id="amdFan">--</div><div class="metric-sub">RPM</div></div>
          </div>
          <div style="margin-top:10px;font-size:12px;color:var(--text-muted);">
            Carga GPU historica:
          </div>
          <div class="freq-chart" id="amdBusyChart"></div>
        </div>
        <div class="card">
          <div class="card-title">LM Studio - Modelos cargados (RX480)</div>
          <div id="amdModels" style="font-size:13px;color:var(--text-secondary);">Cargando...</div>
          <div id="amdContainerInfo" style="margin-top:10px;font-size:12px;color:var(--text-muted);"></div>
        </div>
      </div>

      <!-- AMD RX480 Benchmark -->
      <div class="tab-panel" id="tab-bench-amd">
        <div class="card">
          <div class="card-title">Benchmark LM Studio (AMD RX480)</div>
          <p style="font-size:12px;color:var(--text-muted);margin-bottom:12px;">
            Mide tokens/segundo de un modelo LM Studio. El modelo se carga automaticamente si no esta cargado.
          </p>
          <div class="benchmark-form">
            <div class="form-field">
              <label>Modelo</label>
              <select id="amdBenchModel" style="min-width:200px;"></select>
            </div>
            <div class="form-field">
              <label>Max tokens</label>
              <input type="number" id="amdBenchMaxTokens" value="128" style="width:80px;">
            </div>
            <button class="btn-primary" id="amdBenchBtn" onclick="runAmdBenchmark()">Ejecutar benchmark</button>
          </div>
          <div class="form-field" style="margin-top:8px;">
            <label>Prompt</label>
            <textarea id="amdBenchPrompt">Explain the concept of recursion in programming, with examples.</textarea>
          </div>
          <div id="amdBenchResult"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const API = '';
let selectedModel = null;
let gpuFreqHistory = [];
let amdBusyHistory = [];
let logsTimer = null;
let gpuTimer = null;
let amdTimer = null;
let lmsTimer = null;
let currentHardware = 'intel'; // 'intel' or 'amd'

// ---- Theme ----
function toggleTheme() {
  const body = document.body;
  const cur = body.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : 'dark';
  body.setAttribute('data-theme', next);
  document.querySelector('.theme-toggle').textContent = next === 'dark' ? '\u2600' : '\u263D';
}

// ---- Hardware switching ----
function switchHardware(hw) {
  if (hw === currentHardware) return;
  currentHardware = hw;

  // Toggle selector buttons
  document.getElementById('hw-intel').classList.toggle('active', hw === 'intel');
  document.getElementById('hw-amd').classList.toggle('active', hw === 'amd');

  // Toggle sidebar content
  document.getElementById('sidebar-intel').style.display = hw === 'intel' ? '' : 'none';
  document.getElementById('sidebar-amd').style.display = hw === 'amd' ? '' : 'none';

  // Toggle topbar tabs: same 4 tabs for both hardware, content changes
  // (no need to hide/show tabs — switchTab handles panel routing)

  // Stop all timers, then start the right one
  if (gpuTimer) { clearInterval(gpuTimer); gpuTimer = null; }
  if (amdTimer) { clearInterval(amdTimer); amdTimer = null; }
  if (lmsTimer) { clearInterval(lmsTimer); lmsTimer = null; }
  if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }

  if (hw === 'intel') {
    switchTab('gpu');
    refreshModels();
  } else {
    switchTab('gpu'); // will route to tab-amd panel
    refreshLmsModels();
    lmsTimer = setInterval(refreshLmsModels, 10000);
    // AMD metrics polling (sidebar + main panel)
    refreshAMD();
    amdTimer = setInterval(refreshAMD, 3000);
  }
}

// ---- Tabs ----
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  // Route to hardware-specific panel
  let panelId = 'tab-' + tab;
  if (currentHardware === 'amd') {
    if (tab === 'gpu') panelId = 'tab-amd';
    else if (tab === 'benchmark') panelId = 'tab-bench-amd';
  }
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === panelId));
  // Stop auto-refresh when leaving GPU tab
  if (tab !== 'gpu' && gpuTimer) { clearInterval(gpuTimer); gpuTimer = null; }
  if (tab === 'gpu' && !gpuTimer && currentHardware === 'intel') { refreshGPU(); gpuTimer = setInterval(refreshGPU, 2000); }
  if (tab === 'logs' && !logsTimer) { refreshLogs(); logsTimer = setInterval(refreshLogs, 5000); }
  if (tab !== 'logs' && logsTimer) { clearInterval(logsTimer); logsTimer = null; }
  // AMD RX480 polling when on gpu tab (shows amd panel)
  if (tab !== 'gpu' && amdTimer) { clearInterval(amdTimer); amdTimer = null; }
  if (tab === 'gpu' && !amdTimer && currentHardware === 'amd') { refreshAMD(); amdTimer = setInterval(refreshAMD, 3000); }
}

// ---- API helpers ----
async function apiGet(url) {
  const r = await fetch(API + url);
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}
async function apiPost(url, body) {
  const r = await fetch(API + url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})
  });
  if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || r.statusText); }
  return r.json();
}

// ---- Status ----
async function checkStatus() {
  try {
    await apiGet('/health');
    document.getElementById('statusDot').className = 'status-dot dot-green';
    document.getElementById('statusText').textContent = 'Online';
  } catch {
    document.getElementById('statusDot').className = 'status-dot dot-red';
    document.getElementById('statusText').textContent = 'Offline';
  }
}

// ---- Models ----
async function refreshModels() {
  try {
    const data = await apiGet('/v1/admin/models');
    const cache = await apiGet('/v1/admin/cache');
    renderModels(data.models, cache);
    renderCacheBar(cache);
    // Populate benchmark select
    const sel = document.getElementById('benchModel');
    sel.innerHTML = data.models.filter(m => m.type === 'LLM' || m.type === 'VLM (Multimodal)')
      .map(m => `<option value="${m.name}">${m.name} (${m.size_mb}MB)</option>`).join('');
  } catch (e) {
    document.getElementById('modelList').innerHTML =
      `<div style="padding:12px;color:var(--error);">Error: ${e.message}</div>`;
  }
}

function renderModels(models, cache) {
  const loadedNames = new Set(cache.models.map(m => m.name));
  const html = models.map(m => {
    const loaded = loadedNames.has(m.name);
    const badge = loaded
      ? `<span class="model-badge badge-loaded">cargado</span>`
      : `<span class="model-badge badge-unloaded">en disco</span>`;
    const typeBadge = `<span class="model-badge badge-type">${m.type}</span>`;
    const actions = loaded
      ? `<button class="btn-mini btn-danger" onclick="unloadModel('${m.name}')">Descargar</button>
         <button class="btn-mini" onclick="warmupModel('${m.name}')">Warmup</button>`
      : `<button class="btn-mini" onclick="loadModel('${m.name}')">Cargar</button>`;
    return `
      <div class="model-item ${selectedModel === m.name ? 'selected' : ''}" onclick="selectModel('${m.name}')">
        <div class="model-item-header">
          <span class="model-name">${m.name}</span>
        </div>
        <div style="margin-top:4px;">${badge} ${typeBadge}</div>
        <div class="model-meta">
          <span>${m.size_mb < 1024 ? m.size_mb + ' MB' : (m.size_mb/1024).toFixed(1) + ' GB'}</span>
          <span>${m.multimodal ? 'Multimodal' : 'Texto'}</span>
        </div>
        <div class="model-actions">${actions}</div>
      </div>`;
  }).join('');
  document.getElementById('modelList').innerHTML = html || '<div style="padding:12px;color:var(--text-muted);">No hay modelos</div>';
}

function selectModel(name) {
  selectedModel = name;
  document.getElementById('topbarTitle').textContent = name;
  refreshModels();
}

async function loadModel(name) {
  const btn = event.target; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Cargando...';
  try {
    await apiPost(`/v1/admin/models/${name}/load`);
    refreshModels();
  } catch (e) {
    alert('Error cargando modelo: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Cargar';
  }
}

async function unloadModel(name) {
  const btn = event.target; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  try {
    await apiPost(`/v1/admin/models/${name}/unload`);
    refreshModels();
  } catch (e) {
    alert('Error descargando modelo: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Descargar';
  }
}

async function warmupModel(name) {
  const btn = event.target; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Warmup...';
  try {
    const r = await apiPost(`/v1/admin/models/${name}/warmup`);
    alert(`Warmup completado en ${r.elapsed_seconds}s`);
  } catch (e) {
    alert('Error en warmup: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Warmup';
  }
}

// ---- Cache bar ----
function renderCacheBar(cache) {
  const slots = [];
  for (let i = 0; i < cache.max_cached; i++) {
    const m = cache.models[i];
    if (m) {
      const lruClass = m.is_lru ? ' lru' : '';
      slots.push(`<div class="cache-slot filled${lruClass}" title="${m.name}${m.is_lru ? ' (LRU - siguiente a expulsar)' : ''}">${m.name}</div>`);
    } else {
      slots.push(`<div class="cache-slot">vacio</div>`);
    }
  }
  document.getElementById('cacheBar').innerHTML = `
    <div class="cache-bar">
      <span>Cache LRU (${cache.slots_used})</span>
      ${slots.join('')}
    </div>`;
}

// ---- GPU Monitor ----
async function refreshGPU() {
  try {
    const gpu = await apiGet('/v1/admin/gpu/status');
    const actFreq = gpu.gt_act_freq_mhz || (gpu.tiles && gpu.tiles.gt0 && gpu.tiles.gt0.rps_act_freq_mhz) || 0;
    const maxFreq = gpu.gt_max_freq_mhz || (gpu.tiles && gpu.tiles.gt0 && gpu.tiles.gt0.rps_max_freq_mhz) || 0;

    document.getElementById('gpuFreq').textContent = actFreq;
    document.getElementById('gpuMaxFreq').textContent = maxFreq;
    document.getElementById('gpuMem').textContent = gpu.gpu_process_memory_mb || '--';

    // RC6 residency
    if (gpu.tiles && gpu.tiles.gt0 && gpu.tiles.gt0.rc6_residency_ms != null) {
      // RC6 residency ms is cumulative - show raw value
      document.getElementById('gpuRc6').textContent =
        (gpu.tiles.gt0.rc6_residency_ms / 1000).toFixed(0) + 's';
    }

    // Freq history chart — acumula todo el historico sin limite
    gpuFreqHistory.push(actFreq);
    renderFreqChart(maxFreq);

    // Throttle warnings
    let throttleHtml = '';
    if (gpu.tiles) {
      for (const [tileName, tile] of Object.entries(gpu.tiles)) {
        if (tile.throttle_reasons) {
          for (const [reason, val] of Object.entries(tile.throttle_reasons)) {
            throttleHtml += `<div class="throttle-warning">${tileName}: ${reason} activo (${val})</div>`;
          }
        }
      }
    }
    document.getElementById('throttleWarnings').innerHTML = throttleHtml;

    // Tiles info
    let tilesHtml = '';
    if (gpu.tiles) {
      for (const [tileName, tile] of Object.entries(gpu.tiles)) {
        tilesHtml += `<div style="margin-bottom:8px;"><strong>${tileName}</strong>: `
          + `actual=${tile.rps_act_freq_mhz || '--'}MHz, `
          + `solicitada=${tile.rps_cur_freq_mhz || '--'}MHz, `
          + `min=${tile.rps_min_freq_mhz || '--'}MHz, `
          + `max=${tile.rps_max_freq_mhz || '--'}MHz`
          + (tile.punit_req_freq_mhz != null ? `, punit=${tile.punit_req_freq_mhz}MHz` : '')
          + `</div>`;
      }
    }
    document.getElementById('tilesInfo').innerHTML = tilesHtml || 'Sin datos de tiles';
  } catch (e) {
    document.getElementById('gpuMetrics').innerHTML =
      `<div style="color:var(--error);">Error leyendo GPU: ${e.message}</div>`;
  }
}

function renderFreqChart(maxFreq) {
  const chart = document.getElementById('freqChart');
  const bars = gpuFreqHistory.map(f => {
    const pct = maxFreq > 0 ? (f / maxFreq * 100) : 0;
    const cls = pct > 80 ? ' peak' : '';
    return `<div class="freq-bar${cls}" style="height:${Math.max(pct, 2)}%"></div>`;
  });
  chart.innerHTML = bars.join('');
}

// ---- AMD RX480 ----
async function refreshAMD() {
  try {
    const amd = await apiGet('/v1/admin/amd/status');
    if (!amd.available) {
      document.getElementById('amdGpuBusy').textContent = 'N/A';
      document.getElementById('amdModels').textContent = 'AMD GPU no disponible: ' + (amd.error || 'no detectada');
      return;
    }
    // GPU/Mem busy
    document.getElementById('amdGpuBusy').textContent = (amd.gpu_busy_percent ?? '--') + '%';
    document.getElementById('amdMemBusy').textContent = (amd.mem_busy_percent ?? '--') + '%';
    // VRAM
    if (amd.vram_used_mb != null) {
      document.getElementById('amdVram').textContent = amd.vram_used_mb + ' / ' + amd.vram_total_mb;
    }
    // HWMON
    const h = amd.hwmon || {};
    document.getElementById('amdSclk').textContent = h.sclk_mhz ?? '--';
    document.getElementById('amdMclk').textContent = h.mclk_mhz ?? '--';
    document.getElementById('amdTemp').textContent = h.temp_c ?? '--';
    document.getElementById('amdPower').textContent = (h.power_w ?? '--') + ' / ' + (h.power_cap_w ?? '130');
    document.getElementById('amdFan').textContent = h.fan_rpm ?? '--';

    // Sidebar mini-metrics (always update, even if AMD tab not active)
    const sbGpu = document.getElementById('amdSbGpuBusy');
    const sbVram = document.getElementById('amdSbVram');
    const sbTemp = document.getElementById('amdSbTemp');
    const sbPower = document.getElementById('amdSbPower');
    if (sbGpu) sbGpu.textContent = (amd.gpu_busy_percent ?? '--') + '%';
    if (sbVram) sbVram.textContent = (amd.vram_used_mb ?? '--') + ' MB';
    if (sbTemp) sbTemp.textContent = (h.temp_c ?? '--') + ' °C';
    if (sbPower) sbPower.textContent = (h.power_w ?? '--') + ' W';

    // Busy history chart
    if (amd.gpu_busy_percent != null) {
      amdBusyHistory.push(amd.gpu_busy_percent);
      renderAmdBusyChart();
    }

    // LM Studio models
    const modelsEl = document.getElementById('amdModels');
    if (amd.lms_api_online) {
      const models = amd.lms_models || [];
      if (models.length > 0) {
        modelsEl.innerHTML = models.map(m =>
          `<div style="padding:6px 0;border-bottom:1px solid var(--border);">${m}</div>`
        ).join('');
      } else {
        modelsEl.textContent = 'No hay modelos cargados en LM Studio';
      }
    } else {
      modelsEl.innerHTML = '<span style="color:var(--error);">LM Studio API offline</span>' +
        (amd.lms_error ? '<div style="font-size:11px;margin-top:4px;">' + amd.lms_error + '</div>' : '');
    }

    // Container info
    const cInfo = document.getElementById('amdContainerInfo');
    if (amd.container_status) {
      cInfo.textContent = 'Contenedor: ' + amd.container_status +
        (amd.container_started_at ? ' | Iniciado: ' + amd.container_started_at : '');
    }
  } catch (e) {
    console.error('AMD refresh error:', e);
  }
}

function renderAmdBusyChart() {
  const chart = document.getElementById('amdBusyChart');
  if (!chart) return;
  const bars = amdBusyHistory.map(v => {
    const cls = v > 80 ? ' peak' : '';
    return `<div class="freq-bar${cls}" style="height:${Math.max(v, 2)}%"></div>`;
  });
  chart.innerHTML = bars.join('');
}

// ---- LM Studio models (AMD sidebar) ----
async function refreshLmsModels() {
  try {
    const data = await apiGet('/v1/admin/lms/models');
    if (!data.api_online) {
      document.getElementById('lmsModelList').innerHTML =
        `<div style="padding:12px;color:var(--error);">LM Studio offline${data.error ? '<br><small>' + data.error + '</small>' : ''}</div>`;
      return;
    }
    renderLmsModels(data.models);
  } catch (e) {
    document.getElementById('lmsModelList').innerHTML =
      `<div style="padding:12px;color:var(--error);">Error: ${e.message}</div>`;
  }
}

function renderLmsModels(models) {
  const html = models.map(m => {
    const loaded = m.loaded;
    const badge = loaded
      ? `<span class="model-badge badge-loaded">cargado</span>`
      : `<span class="model-badge badge-unloaded">en disco</span>`;
    const typeBadge = `<span class="model-badge badge-type">${m.type}</span>`;
    const actions = loaded
      ? `<button class="btn-mini btn-danger" onclick="unloadLmsModel('${m.key}')">Descargar</button>`
      : `<button class="btn-mini" onclick="loadLmsModel('${m.key}')">Cargar</button>`;
    const caps = [];
    if (m.capabilities && m.capabilities.tool_use) caps.push('tool_use');
    if (m.capabilities && m.capabilities.vision) caps.push('vision');
    const capStr = caps.length ? ' · ' + caps.join(', ') : '';
    return `
      <div class="model-item">
        <div class="model-item-header">
          <span class="model-name">${m.display_name}</span>
        </div>
        <div style="margin-top:4px;">${badge} ${typeBadge}</div>
        <div class="model-meta">
          <span>${m.size_mb < 1024 ? m.size_mb + ' MB' : (m.size_mb/1024).toFixed(1) + ' GB'}</span>
          <span>${m.params}</span>
          <span>${m.architecture}</span>
          <span>ctx ${m.max_context}</span>
        </div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:2px;">${m.quantization}${capStr}</div>
        <div class="model-actions">${actions}</div>
      </div>`;
  }).join('');
  document.getElementById('lmsModelList').innerHTML = html || '<div style="padding:12px;color:var(--text-muted);">No hay modelos</div>';

  // Populate AMD benchmark model select
  const sel = document.getElementById('amdBenchModel');
  if (sel) {
    sel.innerHTML = models.map(m =>
      `<option value="${m.key}">${m.display_name} (${m.params})</option>`
    ).join('');
  }
}

async function loadLmsModel(key) {
  const btn = event.target; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Cargando...';
  try {
    await apiPost(`/v1/admin/lms/models/${key}/load`);
    refreshLmsModels();
  } catch (e) {
    alert('Error cargando modelo LM Studio: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Cargar';
  }
}

async function unloadLmsModel(key) {
  const btn = event.target; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  try {
    await apiPost(`/v1/admin/lms/models/${key}/unload`);
    refreshLmsModels();
  } catch (e) {
    alert('Error descargando modelo LM Studio: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Descargar';
  }
}

// ---- System ----
async function refreshSystem() {
  try {
    const sys = await apiGet('/v1/admin/system');
    // Memory
    document.getElementById('memTotal').textContent = sys.memory.total_mb;
    document.getElementById('memUsed').textContent = sys.memory.used_mb;
    document.getElementById('memAvail').textContent = sys.memory.available_mb;
    document.getElementById('memPercent').textContent = sys.memory.percent;
    const memBar = document.getElementById('memBar');
    memBar.style.width = sys.memory.percent + '%';
    memBar.className = 'progress-fill ' + (sys.memory.percent > 85 ? 'progress-red' : sys.memory.percent > 70 ? 'progress-orange' : 'progress-green');

    // CPU
    document.getElementById('cpuPhysical').textContent = sys.cpu.cores_physical;
    document.getElementById('cpuLogical').textContent = sys.cpu.cores_logical;
    document.getElementById('cpuFreq').textContent = sys.cpu.freq_mhz;
    document.getElementById('cpuLoad').textContent = sys.cpu.load_percent;
    const cpuBar = document.getElementById('cpuBar');
    cpuBar.style.width = sys.cpu.load_percent + '%';
    cpuBar.className = 'progress-fill ' + (sys.cpu.load_percent > 85 ? 'progress-red' : sys.cpu.load_percent > 70 ? 'progress-orange' : 'progress-blue');

    // Disk
    document.getElementById('diskTotal').textContent = sys.disk.total_gb;
    document.getElementById('diskUsed').textContent = sys.disk.used_gb;
    document.getElementById('diskFree').textContent = sys.disk.free_gb;
    document.getElementById('diskPercent').textContent = sys.disk.percent;
    const diskBar = document.getElementById('diskBar');
    diskBar.style.width = sys.disk.percent + '%';
    diskBar.className = 'progress-fill ' + (sys.disk.percent > 85 ? 'progress-red' : sys.disk.percent > 70 ? 'progress-orange' : 'progress-green');

    // OpenVINO devices
    const devColors = {'GPU': 'var(--success)', 'NPU': 'var(--warning)', 'CPU': 'var(--accent)'};
    document.getElementById('ovDevices').innerHTML = sys.openvino_devices
      .map(d => `<span class="model-badge badge-type" style="font-size:13px;padding:4px 12px;margin-right:6px;color:${devColors[d]||'var(--text-primary)'};">${d}</span>`)
      .join(' ') || 'Sin dispositivos';
  } catch (e) {
    console.error('System error:', e);
  }
}

// ---- Benchmark ----
async function runBenchmark() {
  const btn = document.getElementById('benchBtn');
  const model = document.getElementById('benchModel').value;
  const maxTokens = parseInt(document.getElementById('benchMaxTokens').value);
  const prompt = document.getElementById('benchPrompt').value;

  if (!model) { alert('Selecciona un modelo'); return; }

  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Ejecutando...';
  document.getElementById('benchResult').innerHTML =
    '<div style="padding:12px;color:var(--text-muted);"><span class="spinner"></span> Ejecutando benchmark (puede tardar varios segundos)...</div>';

  try {
    const r = await apiPost('/v1/admin/benchmark', { model, prompt, max_tokens: maxTokens });
    document.getElementById('benchResult').innerHTML = `
      <div class="benchmark-result">
        <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap;">
          <div>
            <div class="metric-label">Tokens por segundo</div>
            <div class="tps-big">${r.tokens_per_second}</div>
          </div>
          <div>
            <div class="metric-label">Tiempo total</div>
            <div class="metric-value">${r.elapsed_seconds}s</div>
          </div>
          <div>
            <div class="metric-label">Tokens generados</div>
            <div class="metric-value">${r.max_tokens}</div>
          </div>
          <div>
            <div class="metric-label">Modelo</div>
            <div style="font-size:13px;font-weight:500;">${r.model}</div>
          </div>
        </div>
        <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border);">
          <div class="metric-label">Salida (preview)</div>
          <div style="margin-top:6px;font-size:13px;color:var(--text-secondary);max-height:150px;overflow-y:auto;">${r.output_preview}</div>
        </div>
      </div>`;
  } catch (e) {
    document.getElementById('benchResult').innerHTML =
      `<div style="padding:12px;color:var(--error);">Error: ${e.message}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = 'Ejecutar benchmark';
  }
}

async function runAmdBenchmark() {
  const btn = document.getElementById('amdBenchBtn');
  const model = document.getElementById('amdBenchModel').value;
  const maxTokens = parseInt(document.getElementById('amdBenchMaxTokens').value);
  const prompt = document.getElementById('amdBenchPrompt').value;

  if (!model) { alert('Selecciona un modelo'); return; }

  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Ejecutando...';
  document.getElementById('amdBenchResult').innerHTML =
    '<div style="padding:12px;color:var(--text-muted);"><span class="spinner"></span> Cargando modelo y ejecutando benchmark...</div>';

  try {
    const r = await apiPost('/v1/admin/lms/benchmark', { model, prompt, max_tokens: maxTokens });
    document.getElementById('amdBenchResult').innerHTML = `
      <div class="benchmark-result">
        <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap;">
          <div>
            <div class="metric-label">Tokens por segundo</div>
            <div class="tps-big">${r.tokens_per_second}</div>
          </div>
          <div>
            <div class="metric-label">Tiempo total</div>
            <div class="metric-value">${r.elapsed_seconds}s</div>
          </div>
          <div>
            <div class="metric-label">Tokens generados</div>
            <div class="metric-value">${r.completion_tokens}</div>
          </div>
          <div>
            <div class="metric-label">Modelo</div>
            <div style="font-size:13px;font-weight:500;">${r.model}</div>
          </div>
        </div>
        <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border);">
          <div class="metric-label">Salida (preview)</div>
          <div style="margin-top:6px;font-size:13px;color:var(--text-secondary);max-height:150px;overflow-y:auto;">${r.output_preview}</div>
        </div>
      </div>`;
  } catch (e) {
    document.getElementById('amdBenchResult').innerHTML =
      `<div style="padding:12px;color:var(--error);">Error: ${e.message}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = 'Ejecutar benchmark';
  }
}

// ---- Logs ----
async function refreshLogs() {
  try {
    const data = await apiGet('/v1/admin/logs?lines=150');
    const viewer = document.getElementById('logViewer');
    const html = data.lines.map(l => {
      const cls = l.includes('ERROR') ? ' error' : l.includes('WARNING') || l.includes('WARN') ? ' warning' : '';
      return `<div class="log-line${cls}">${l.replace(/</g,'&lt;')}</div>`;
    }).join('');
    viewer.innerHTML = html || 'Sin logs';
    if (document.getElementById('autoScrollLogs').checked) {
      viewer.scrollTop = viewer.scrollHeight;
    }
  } catch (e) {
    document.getElementById('logViewer').innerHTML = `Error: ${e.message}`;
  }
}

// ---- Init ----
(async function init() {
  await checkStatus();
  await refreshModels();
  await refreshSystem();
  refreshGPU();
  gpuTimer = setInterval(refreshGPU, 2000);
  // Refresh models and system periodically
  setInterval(refreshModels, 10000);
  setInterval(refreshSystem, 10000);
  setInterval(checkStatus, 10000);
})();
</script>
</body>
</html>
"""