import os
import re
import time
import uuid
import json
import asyncio
import threading
import logging
from typing import List, Optional, Dict, Any, Union
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
import openvino_genai as ov_genai
import openvino as ov
import numpy as np

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OpenVINO-GenAI] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ov-server")

app = FastAPI(title="OpenVINO GenAI Intel Arc Server", version="2.3.0")

# Admin dashboard (management API + SPA GUI at /admin)
try:
    from admin_dashboard import router as admin_router
    app.include_router(admin_router)
    log.info("Admin dashboard cargado en /admin y /v1/admin/*")
except Exception as e:
    log.warning(f"admin_dashboard no disponible: {e}")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODELS_DIR = os.environ.get("MODELS_DIR", "/models/.openvino-ir")
DEVICE = os.environ.get("DEVICE", "GPU")

# Comma-separated list of models to preload at startup.
# If empty, auto-discover all models in MODELS_DIR.
PRELOAD_MODELS = os.environ.get("PRELOAD_MODELS", "")

# GPU config: LATENCY is faster for single-request (our benchmark proved +10%).
# KV_CACHE_PRECISION f16 halves KV bandwidth vs f32.
# INFERENCE_PRECISION_HINT f16: the model is already INT4, f16 for remaining ops.
# GPU_ENABLE_LARGE_ALLOCATIONS: allow >4GB allocations (92GB shared RAM).
# DYNAMIC_QUANTIZATION_GROUP_SIZE 32: dynamic activation quantization in groups of 32.
# GPU_ENABLE_SDPA_OPTIMIZATION: fused Scaled Dot Product Attention.
# CACHE_DIR: persistent compilation cache to avoid recompiling on restart.
GPU_CONFIG = {
    "PERFORMANCE_HINT": "LATENCY",
    "KV_CACHE_PRECISION": "f16",
    "INFERENCE_PRECISION_HINT": "f16",
    "GPU_ENABLE_LARGE_ALLOCATIONS": True,
    "DYNAMIC_QUANTIZATION_GROUP_SIZE": 32,
    "GPU_ENABLE_SDPA_OPTIMIZATION": True,
    "CACHE_DIR": "/tmp/ov_cache",
}

# ---------------------------------------------------------------------------#
# NPU config (Intel AI Boost / Meteor Lake NPU 3720)
# Used for embeddings and small models. NPU_PLATFORM must be set explicitly
# because AUTO_DETECT is unsupported by the compiler loader from the snap.
# ---------------------------------------------------------------------------
NPU_DEVICE = os.environ.get("NPU_DEVICE", "NPU")
NPU_PLATFORM = os.environ.get("NPU_PLATFORM", "3720")
NPU_CONFIG = {
    "NPU_PLATFORM": NPU_PLATFORM,
    "CACHE_DIR": "/tmp/ov_cache",
    "PERFORMANCE_HINT": "LATENCY",
}
EMBEDDING_DEVICE = os.environ.get("EMBEDDING_DEVICE", "NPU")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2-ov")

# ---------------------------------------------------------------------------#
# Multi-model pipeline cache with LRU eviction.
# The iGPU Xe2 has ~8GB shared VRAM. Loading all models simultaneously
# exhausts VRAM and causes OOM. We keep at most MAX_CACHED_MODELS loaded
# and evict the least-recently-used when a new one is needed.
# ---------------------------------------------------------------------------#
import collections
MAX_CACHED_MODELS = int(os.environ.get("MAX_CACHED_MODELS", "3"))
_pipeline_cache: collections.OrderedDict[str, Any] = collections.OrderedDict()
_cache_lock = threading.Lock()
# Per-model lock so concurrent requests to the SAME model serialize on the
# GPU (iGPU can't run 2 inferences simultaneously), but requests to DIFFERENT
# models don't block each other.
_model_locks: Dict[str, threading.Lock] = {}
_model_locks_guard = threading.Lock()


def _get_model_lock(model_path: str) -> threading.Lock:
    with _model_locks_guard:
        if model_path not in _model_locks:
            _model_locks[model_path] = threading.Lock()
        return _model_locks[model_path]


def _resolve_model_path(model_name: Optional[str] = None) -> str:
    """Resolve a model name to a filesystem path inside MODELS_DIR."""
    def _has_model_file(d: str) -> bool:
        """Check if a directory contains a valid OpenVINO model (standard or multimodal)."""
        return (os.path.exists(os.path.join(d, "openvino_model.xml")) or
                os.path.exists(os.path.join(d, "openvino_language_model.xml")))

    if model_name:
        # Try exact name under MODELS_DIR
        candidate = os.path.join(MODELS_DIR, model_name)
        if os.path.exists(candidate):
            return candidate
        # Try as absolute/relative path
        if os.path.exists(model_name):
            return os.path.abspath(model_name)
        log.warning(f"Model '{model_name}' not found, using default")
    # Fallback: first model in directory
    if os.path.exists(MODELS_DIR):
        for entry in sorted(os.listdir(MODELS_DIR)):
            full = os.path.join(MODELS_DIR, entry)
            if os.path.isdir(full) and _has_model_file(full):
                return full
    raise RuntimeError(f"No models found in {MODELS_DIR}")


def _is_multimodal_model(model_path: str) -> bool:
    """Check if a model directory contains vision components (multimodal)."""
    return os.path.exists(os.path.join(model_path, "openvino_vision_embeddings_model.xml"))


class VLMTextAdapter:
    """Wraps a VLMPipeline so it can be called like an LLMPipeline for text-only
    inference. VLMPipeline.generate requires images/videos as positional args,
    which LLMPipeline callers don't provide. This adapter inserts empty lists."""

    def __init__(self, vlm_pipe):
        self._pipe = vlm_pipe

    def generate(self, prompt, *args, **kwargs):
        # Separate gen_config and streamer from args (LLMPipeline call convention)
        gen_config = None
        streamer = None
        if args:
            gen_config = args[0]
        if len(args) > 1:
            streamer = args[1]
        # Also check kwargs
        gen_config = kwargs.get("generation_config", gen_config)
        streamer = kwargs.get("streamer", streamer)
        if gen_config is not None and streamer is not None:
            result = self._pipe.generate(prompt, [], [], gen_config, streamer)
        elif gen_config is not None:
            result = self._pipe.generate(prompt, [], [], gen_config)
        else:
            result = self._pipe.generate(prompt, [], [], **kwargs)
        # Normalize: VLMPipeline returns VLMDecodedResults, LLMPipeline callers
        # expect a plain string. Convert via .texts[0] or str().
        if hasattr(result, "texts"):
            return str(result.texts[0]) if result.texts else ""
        return str(result) if not isinstance(result, str) else result

    def __getattr__(self, name):
        return getattr(self._pipe, name)


def get_or_load_pipeline(model_name: Optional[str] = None):
    """Load a pipeline into the LRU cache. Returns cached if available.
    Evicts the least-recently-used model when cache is full (MAX_CACHED_MODELS).
    Uses VLMPipeline for multimodal models, LLMPipeline for text-only."""
    target_path = _resolve_model_path(model_name)

    with _cache_lock:
        if target_path in _pipeline_cache:
            # Move to end (most recently used)
            _pipeline_cache.move_to_end(target_path)
            return _pipeline_cache[target_path]

        model_id = os.path.basename(target_path)

        # Evict LRU models if cache is full
        while len(_pipeline_cache) >= MAX_CACHED_MODELS:
            evict_path, evict_pipe = _pipeline_cache.popitem(last=False)
            evict_name = os.path.basename(evict_path)
            log.info(f"Evicting LRU model from cache: {evict_name} (cache full: {len(_pipeline_cache)+1}/{MAX_CACHED_MODELS})")
            del evict_pipe

        is_multimodal = _is_multimodal_model(target_path)
        log.info(f"Cargando modelo en {DEVICE}: {model_id} (multimodal={is_multimodal})...")
        t0 = time.time()
        if is_multimodal:
            vlm_pipe = ov_genai.VLMPipeline(target_path, DEVICE, **GPU_CONFIG)
            pipe = VLMTextAdapter(vlm_pipe)
        else:
            pipe = ov_genai.LLMPipeline(target_path, DEVICE, **GPU_CONFIG)
        t_load = time.time() - t0
        log.info(f"Modelo '{model_id}' cargado en {t_load:.2f}s")

        # Warmup: compile and generate 1 token to pay the cold-start cost now
        cfg = ov_genai.GenerationConfig()
        cfg.max_new_tokens = 1
        pipe.generate("Hello", cfg)
        log.info(f"Warmup '{model_id}' completado")

        _pipeline_cache[target_path] = pipe
        return pipe


def preload_all_models():
    """Preload models into the LRU cache at startup.
    Only loads up to MAX_CACHED_MODELS to avoid thrashing the iGPU VRAM.
    If PRELOAD_MODELS is set, use that list (truncated to MAX_CACHED_MODELS).
    If empty, auto-discover and load the smallest models first."""
    models_to_load = []
    if PRELOAD_MODELS:
        models_to_load = [m.strip() for m in PRELOAD_MODELS.split(",") if m.strip()]
    elif os.path.exists(MODELS_DIR):
        for entry in sorted(os.listdir(MODELS_DIR)):
            full = os.path.join(MODELS_DIR, entry)
            if os.path.isdir(full) and (os.path.exists(os.path.join(full, "openvino_model.xml")) or
                                        os.path.exists(os.path.join(full, "openvino_language_model.xml"))):
                models_to_load.append(entry)

    # Excluir modelos de embedding (se cargan por separado en el NPU/CPU)
    embedding_models = set()
    for em in [EMBEDDING_MODEL_NAME, os.environ.get("EMBEDDING_MODEL_NAME_2", "")]:
        if em:
            embedding_models.add(em)
    models_to_load = [m for m in models_to_load if m not in embedding_models]

    # Ordenar por tamano (menor primero) para que los modelos pequenos se carguen rapido
    def _model_size(n):
        p = os.path.join(MODELS_DIR, n, "openvino_model.bin")
        if not os.path.exists(p):
            # Multimodal models (e.g. gemma-3) use openvino_language_model.bin
            p = os.path.join(MODELS_DIR, n, "openvino_language_model.bin")
        return os.path.getsize(p) if os.path.exists(p) else 0
    models_to_load.sort(key=_model_size)

    # Truncar al limite del cache LRU para evitar cargar y evictar inmediatamente
    if len(models_to_load) > MAX_CACHED_MODELS:
        skipped = models_to_load[MAX_CACHED_MODELS:]
        models_to_load = models_to_load[:MAX_CACHED_MODELS]
        log.info(f"Limitando precarga a {MAX_CACHED_MODELS} modelos (LRU cache). "
                 f"Saltando: {skipped}")

    if not models_to_load:
        log.warning("No hay modelos para precargar")
        return

    log.info(f"Precargando {len(models_to_load)} modelos en GPU (orden por tamano): {models_to_load}")
    total_t0 = time.time()
    loaded = 0
    for name in models_to_load:
        try:
            get_or_load_pipeline(name)
            loaded += 1
        except Exception as e:
            log.error(f"Error precargando '{name}': {e}")

    log.info(f"Precarga completa: {loaded}/{len(models_to_load)} modelos en {time.time()-total_t0:.1f}s")
    _log_memory_usage()


def _log_memory_usage():
    """Log current RAM usage to verify models fit in memory."""
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
            total = meminfo.get("MemTotal", 0) // 1024
            avail = meminfo.get("MemAvailable", 0) // 1024
            used = total - avail
            log.info(f"RAM: {used}MB usados / {total}MB total ({avail}MB libres)")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Embedding model cache (NPU): loads an OpenVINO IR embedding model and keeps
# the compiled model + tokenizer in memory. Uses a separate Core instance so
# NPU device discovery doesn't interfere with the LLM GPU pipelines.
# ---------------------------------------------------------------------------
_embed_core: Optional[ov.Core] = None
_embed_compiled: Optional[ov.CompiledModel] = None
_embed_tokenizer = None
_embed_lock = threading.Lock()
_embed_dim: int = 0


def _get_embed_core() -> ov.Core:
    global _embed_core
    if _embed_core is None:
        _embed_core = ov.Core()
    return _embed_core


def _resolve_embedding_model_path() -> Optional[str]:
    """Find the embedding model directory under MODELS_DIR."""
    candidate = os.path.join(MODELS_DIR, EMBEDDING_MODEL_NAME)
    if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, "openvino_model.xml")):
        return candidate
    # Search any dir containing an embedding model (heuristic: has pooler or embedding in name)
    if os.path.exists(MODELS_DIR):
        for entry in sorted(os.listdir(MODELS_DIR)):
            full = os.path.join(MODELS_DIR, entry)
            if os.path.isdir(full) and os.path.exists(os.path.join(full, "openvino_model.xml")):
                lower = entry.lower()
                if "embed" in lower or "minilm" in lower or "e5" in lower or "bge" in lower or "nomic" in lower:
                    return full
    return None


def _load_embedding_model():
    """Load and compile the embedding model on the NPU (or fallback to CPU/GPU)."""
    global _embed_compiled, _embed_tokenizer, _embed_dim

    if _embed_compiled is not None:
        return

    model_path = _resolve_embedding_model_path()
    if model_path is None:
        log.warning(f"No embedding model found in {MODELS_DIR} (looking for {EMBEDDING_MODEL_NAME})")
        return

    model_id = os.path.basename(model_path)
    core = _get_embed_core()

    # Determine device: try NPU first, fallback to CPU
    device = EMBEDDING_DEVICE
    available = core.available_devices
    if device == "NPU" and "NPU" not in available:
        log.warning(f"NPU not in available devices {available}, falling back to CPU for embeddings")
        device = "CPU"

    log.info(f"Cargando modelo de embedding '{model_id}' en {device}...")
    t0 = time.time()

    # Read the OpenVINO IR model - use explicit .xml path to avoid OpenVINO
    # trying to auto-detect format (it may pick TF/ONNX instead of IR).
    model_xml = os.path.join(model_path, "openvino_model.xml")
    if not os.path.exists(model_xml):
        log.error(f"No se encontro {model_xml}")
        return
    model = core.read_model(model_xml)
    config = dict(NPU_CONFIG) if device == "NPU" else {"CACHE_DIR": "/tmp/ov_cache"}

    _embed_compiled = core.compile_model(model, device, config)

    # Load tokenizer - use transformers AutoTokenizer (openvino_tokenizers doesn't
    # expose load_tokenizer in 2026.3; the OV tokenizer .xml is for LLM pipelines,
    # not standalone use). AutoTokenizer reads the tokenizer_config/vocab files.
    try:
        from transformers import AutoTokenizer
        _embed_tokenizer = AutoTokenizer.from_pretrained(model_path)
        log.info(f"Tokenizer cargado desde {model_path} (transformers)")
    except Exception as e:
        log.error(f"No se pudo cargar tokenizer desde {model_path}: {e}")
        return

    # Get embedding dimension from the model output
    output = _embed_compiled.outputs[0]
    _embed_dim = output.get_shape()[-1] if len(output.get_shape()) > 1 else 0
    if _embed_dim < 0 or _embed_dim == ov.Dimension.dynamic:
        # Dynamic dimension - run a test to determine
        _embed_dim = 0  # Will be determined on first inference

    t_load = time.time() - t0
    log.info(f"Modelo de embedding '{model_id}' cargado en {device} ({t_load:.2f}s)")


def _mean_pooling(token_embeddings: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """Mean pooling: average token embeddings weighted by attention mask."""
    mask = attention_mask[..., np.newaxis].astype(np.float32)
    summed = (token_embeddings * mask).sum(axis=1)
    counts = mask.sum(axis=1).clip(min=1e-9)
    return summed / counts


def _embed_texts(texts: List[str]) -> tuple:
    """Generate embeddings for a list of texts. Returns (embeddings_list, dim).
    The NPU model has fixed shape [1, 128], so we process texts one by one
    with padding to max_length=128.
    """
    with _embed_lock:
        if _embed_compiled is None:
            _load_embedding_model()
        if _embed_compiled is None:
            raise RuntimeError("Modelo de embedding no disponible")
        if _embed_tokenizer is None:
            raise RuntimeError("Tokenizer no disponible")

        MAX_SEQ = 128
        all_embeddings = []

        for text in texts:
            encoded = _embed_tokenizer(
                text, padding="max_length", max_length=MAX_SEQ, truncation=True, return_tensors="np"
            )
            inputs = {
                "input_ids": encoded["input_ids"],
                "attention_mask": encoded["attention_mask"],
            }
            if "token_type_ids" in _embed_compiled.inputs:
                tti = encoded.get("token_type_ids")
                if tti is not None:
                    inputs["token_type_ids"] = tti

            infer = _embed_compiled.create_infer_request()
            result = infer.infer(inputs)

            # Get the output tensor (last_hidden_state)
            output_name = list(result.keys())[0]
            token_embeddings = result[output_name]

            # Mean pooling weighted by attention mask
            mask = encoded["attention_mask"][..., np.newaxis].astype(np.float32)
            summed = (token_embeddings * mask).sum(axis=1)
            counts = mask.sum(axis=1).clip(min=1e-9)
            emb = summed / counts

            # L2 normalize
            norms = np.linalg.norm(emb, axis=1, keepdims=True).clip(min=1e-12)
            emb = emb / norms

            all_embeddings.append(emb[0].tolist())

        dim = len(all_embeddings[0]) if all_embeddings else 0
        return all_embeddings, dim


# ---------------------------------------------------------------------------
# API Models
# ---------------------------------------------------------------------------
class ToolCallFunction(BaseModel):
    name: str
    arguments: str  # JSON string, per OpenAI spec


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: ToolCallFunction


class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class ToolFunction(BaseModel):
    name: str
    description: Optional[str] = ""
    parameters: Optional[Dict[str, Any]] = {}


class ToolDef(BaseModel):
    type: str = "function"
    function: ToolFunction


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9
    max_tokens: Optional[int] = 512
    stream: Optional[bool] = False
    tools: Optional[List[ToolDef]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None


class EmbeddingRequest(BaseModel):
    model: Optional[str] = None
    input: Union[str, List[str]]
    encoding_format: Optional[str] = "float"


# --------------------------------------------------------------------------- 
# Tool call parsing: detect tool calls in model output and convert to OpenAI format.
# Supports three formats:
#   1. Qwen2.5: XML-style tags with JSON inside
#   2. gpt-oss: Harmony channel format "assistantcommentary to=functions.name json{...}"
#   3. Gemma-4: "call:func_name{key:val,key:val}" (special tokens stripped by OV GenAI)
# ---------------------------------------------------------------------------
TOOL_CALL_ID_PREFIX = "call_"


def _make_tool_call_id() -> str:
    return f"{TOOL_CALL_ID_PREFIX}{uuid.uuid4().hex[:24]}"


# Regex for Qwen2.5 tool call format:
# The model wraps JSON in special tool_call tags.
# We use a character class to avoid literal tag matching issues.
_TOOL_OPEN = "<" + chr(0x2F) * 0 + "tool_call" + ">"
_TOOL_CLOSE = "<" + chr(0x2F) + "tool_call" + ">"

_QWEN_TOOL_PATTERN = re.compile(
    re.escape(_TOOL_OPEN) + r"\s*(\{.*?\})\s*" + re.escape(_TOOL_CLOSE),
    re.DOTALL,
)

# Regex for gpt-oss Harmony format:
# "assistantcommentary to=functions.name json{...}"
# The model outputs reasoning in analysis channel, then tool call in commentary.
_GPT_OSS_TOOL_PATTERN = re.compile(
    r"commentary\s+to=functions\.(\S+?)\s+json\s*(\{.*?\})",
    re.DOTALL,
)

# Regex for Gemma-4 tool call format (special tokens stripped):
# "call:func_name{key:val,key:val}"
# The model uses <|tool_call>...<tool_call|> and <|"|>...<|"|> delimiters,
# but OpenVINO GenAI strips special tokens during decoding, leaving bare text.
# We match the "call:func_name{" prefix; the closing brace is found via
# brace-counting in the parser (code values may contain }).
_GEMMA4_CALL_PREFIX = re.compile(
    r"call\s*:\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\{",
)


def _extract_gemma4_tool_param_names(tools_list):
    """Extract parameter names for each tool function from the tools schema.
    Returns dict: {function_name: [param_name, ...]}
    """
    param_map = {}
    if not tools_list:
        return param_map
    for tool in tools_list:
        try:
            func = tool.get("function", tool) if isinstance(tool, dict) else {}
            name = func.get("name", "")
            params = func.get("parameters", {})
            required = params.get("required", [])
            properties = params.get("properties", {})
            # Use ordered list: required first, then other properties
            all_names = list(required) + [k for k in properties if k not in required]
            if name:
                param_map[name] = all_names
        except Exception:
            continue
    return param_map


def _parse_gemma4_args(args_str: str, param_names: list) -> dict:
    """Parse Gemma-4 argument string without special token delimiters.

    The format is: key1:val1,key2:val2
    Values are unquoted strings (the <|"|> delimiters were stripped by OV GenAI).
    We split on commas that precede a known parameter name followed by a colon.
    For unknown parameter names, fall back to comma splitting.
    """
    if not param_names:
        # Fallback: try simple comma split
        pairs = {}
        parts = args_str.split(",")
        for part in parts:
            if ":" in part:
                k, v = part.split(":", 1)
                pairs[k.strip()] = v.strip()
        return pairs

    # Build a regex that matches `,known_param:` to use as split points
    # Escape param names for regex
    alt_names = "|".join(re.escape(n) for n in param_names)
    # Split on comma followed by a known param name and colon
    # The regex captures the delimiter so we can re-attach it
    split_pattern = re.compile(r",(?=(" + alt_names + r")\s*:)")
    segments = split_pattern.split(args_str)

    # split() with capturing group produces: [text, delim1, text, delim2, text, ...]
    # Reconstruct key:value pairs
    pairs = {}
    # First segment is the first key:value pair
    if segments:
        first = segments[0]
        if ":" in first:
            k, v = first.split(":", 1)
            pairs[k.strip()] = v.strip()

    # Remaining pairs come as (param_name, text) tuples
    # re.split with lookahead: text includes the param_name: prefix
    # e.g. segments = [code_val, 'language', 'language:python']
    i = 1
    while i < len(segments):
        param_name = segments[i]
        text = segments[i + 1] if i + 1 < len(segments) else ""
        # Strip the "param_name:" prefix from text
        val = text
        prefix = param_name + ":"
        if val.startswith(prefix):
            val = val[len(prefix):]
        elif val.startswith(param_name):
            val = val[len(param_name):].lstrip(":")
        val = val.strip()
        pairs[param_name] = val
        i += 2

    return pairs


def parse_tool_calls(text: str, tools_list=None) -> tuple:
    """Parse tool calls from model output.
    Returns (tool_calls_list, cleaned_content).
    If no tool calls found, returns (None, text).
    """
    tool_calls = []

    # --- Format 1: Qwen2.5 XML-style tags ---
    # Use string search instead of regex for extraction - more robust with nested JSON
    open_tag = _TOOL_OPEN
    close_tag = _TOOL_CLOSE
    search_pos = 0
    qwen_raw_matches = []
    while True:
        opos = text.find(open_tag, search_pos)
        if opos == -1:
            break
        cpos = text.find(close_tag, opos + len(open_tag))
        if cpos == -1:
            raw = text[opos + len(open_tag):].strip()
            if raw:
                qwen_raw_matches.append(raw)
            break
        raw = text[opos + len(open_tag):cpos].strip()
        if raw:
            qwen_raw_matches.append(raw)
        search_pos = cpos + len(close_tag)

    if qwen_raw_matches:
        for match in qwen_raw_matches:
            parsed = None
            # Try parsing as-is, then with brace-balancing fixes.
            # The model sometimes omits the outer closing brace(s).
            candidates = [match]
            brace_balanced = match
            for _ in range(5):
                brace_balanced = brace_balanced + "}"
                candidates.append(brace_balanced)

            for attempt, candidate in enumerate(candidates):
                for strict_mode in (True, False):
                    try:
                        parsed = json.loads(candidate, strict=strict_mode)
                        if attempt > 0:
                            log.info(
                                f"Qwen JSON parsed after adding {attempt} "
                                f"closing brace(s). Original len={len(match)}, "
                                f"fixed len={len(candidate)}"
                            )
                        break
                    except json.JSONDecodeError:
                        pass
                if parsed is not None:
                    break

            if parsed is None:
                log.warning(
                    f"Qwen JSON parse failed after all attempts. "
                    f"Match len={len(match)}, repr={repr(match[:200])}"
                )
            if parsed is not None:
                name = parsed.get("name", "")
                arguments = parsed.get("arguments", {})
                if isinstance(arguments, dict):
                    arguments = json.dumps(arguments)
                tool_calls.append({
                    "id": _make_tool_call_id(),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments,
                    }
                })
        if tool_calls:
            cleaned = text
            while True:
                opos = cleaned.find(open_tag)
                if opos == -1:
                    break
                cpos = cleaned.find(close_tag, opos + len(open_tag))
                if cpos == -1:
                    cleaned = cleaned[:opos]
                    break
                cleaned = cleaned[:opos] + cleaned[cpos + len(close_tag):]
            cleaned = cleaned.strip()
            return tool_calls, cleaned

    # --- Format 2: gpt-oss Harmony channel format ---
    gpt_oss_matches = _GPT_OSS_TOOL_PATTERN.findall(text)
    if gpt_oss_matches:
        for name, args_str in gpt_oss_matches:
            name = name.strip().rstrip("<").strip()
            try:
                parsed_args = json.loads(args_str)
                arguments = json.dumps(parsed_args)
            except json.JSONDecodeError:
                arguments = args_str.strip()
            tool_calls.append({
                "id": _make_tool_call_id(),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments,
                }
            })
        if tool_calls:
            # Extract content before the tool call (strip analysis/reasoning)
            parts = re.split(r"commentary\s+to=functions\.", text, maxsplit=1)
            content = parts[0].strip()
            # Strip "analysis" prefix from reasoning if present
            if content.startswith("analysis"):
                content = content[len("analysis"):].strip()
            # Strip "assistantfinal" prefix if present
            content = re.sub(r"^assistantfinal\s*", "", content)
            if not content:
                content = None
            return tool_calls, content

    # --- Format 3: Gemma-4 "call:func_name{key:val,key:val}" ---
    # OpenVINO GenAI strips special tokens (<|tool_call>, <|"|>, <tool_call|>),
    # so we see the bare text without delimiters.
    # The model may output multiple tool calls separated by newlines.
    # Only attempt this format when tools were provided by the client.
    if tools_list is None:
        return None, text
    gemma4_param_map = _extract_gemma4_tool_param_names(tools_list)
    remaining_text = text
    gemma4_matches_found = False
    while True:
        m = _GEMMA4_CALL_PREFIX.search(remaining_text)
        if not m:
            break
        func_name = m.group(1)
        # Find the matching closing brace via brace counting
        # (code values may contain } characters)
        brace_start = m.end() - 1  # position of the opening {
        depth = 0
        close_pos = -1
        for i in range(brace_start, len(remaining_text)):
            ch = remaining_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    close_pos = i
                    break
        if close_pos == -1:
            # No matching closing brace; skip this match
            log.warning(
                f"Gemma-4 tool call parse: no closing brace found for "
                f"function '{func_name}'. Skipping."
            )
            break
        args_str = remaining_text[brace_start + 1:close_pos]
        param_names = gemma4_param_map.get(func_name, [])
        parsed_args = _parse_gemma4_args(args_str, param_names)
        arguments = json.dumps(parsed_args) if parsed_args else "{}"
        tool_calls.append({
            "id": _make_tool_call_id(),
            "type": "function",
            "function": {
                "name": func_name,
                "arguments": arguments,
            }
        })
        gemma4_matches_found = True
        # Remove the matched tool call from remaining_text to look for more
        remaining_text = remaining_text[:m.start()] + remaining_text[close_pos + 1:]

    if gemma4_matches_found:
        # Cleaned content is whatever text remains after removing tool calls
        cleaned = remaining_text.strip()
        # Remove leading/trailing newlines and whitespace-only remnants
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        content = cleaned if cleaned else None
        return tool_calls, content

    return None, text


def _strip_gpt_oss_reasoning(text: str) -> str:
    """For gpt-oss models, strip the analysis/reasoning prefix and extract the final response.
    The model outputs: analysis{reasoning}assistantfinal{actual_response}
    """
    if "assistantfinal" in text:
        parts = text.split("assistantfinal", 1)
        return parts[1].strip() if len(parts) > 1 else text
    if text.startswith("analysis"):
        match = re.search(r"assistant(?:final)?(.*)", text, re.DOTALL)
        if match:
            return match.group(1).strip()
    return text


def _is_gpt_oss_model(model_id: str) -> bool:
    """Check if the model is a gpt-oss model (uses Harmony format)."""
    return "gpt-oss" in model_id.lower()


# ---------------------------------------------------------------------------
# Startup: preload all models
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    loop = asyncio.get_event_loop()
    def _preload():
        preload_all_models()
    loop.run_in_executor(None, _preload)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/v1/warmup")
async def warmup_model(model: Optional[str] = None):
    """Precarga y compila un modelo. util antes de un benchmark."""
    loop = asyncio.get_event_loop()
    def _do_warmup():
        get_or_load_pipeline(model)
    await loop.run_in_executor(None, _do_warmup)
    resolved = _resolve_model_path(model)
    return {"status": "warmed_up", "model": os.path.basename(resolved)}


@app.get("/health")
@app.get("/v1/health")
async def health():
    return {
        "status": "ready" if _pipeline_cache else "loading",
        "device": DEVICE,
        "loaded_models": [os.path.basename(p) for p in _pipeline_cache],
        "cache_size": len(_pipeline_cache),
    }


@app.get("/v1/models")
async def list_models():
    models = []
    if os.path.exists(MODELS_DIR):
        for entry in sorted(os.listdir(MODELS_DIR)):
            full_path = os.path.join(MODELS_DIR, entry)
            if os.path.isdir(full_path) and (
                os.path.exists(os.path.join(full_path, "openvino_model.xml")) or
                os.path.exists(os.path.join(full_path, "openvino_language_model.xml"))
            ):
                models.append({
                    "id": entry,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "intel-arc-openvino",
                    "loaded": entry in [os.path.basename(p) for p in _pipeline_cache],
                })
    if not models:
        models.append({
            "id": "openvino-default",
            "object": "model",
            "created": int(time.time()),
            "owned_by": "intel-arc-openvino",
            "loaded": False,
        })
    return {"object": "list", "data": models}


@app.post("/v1/embeddings")
async def create_embeddings(req: EmbeddingRequest):
    """Generate embeddings using the NPU (Intel AI Boost).
    Falls back to CPU if NPU is not available.
    """
    # Normalize input to list
    if isinstance(req.input, str):
        texts = [req.input]
    else:
        texts = req.input

    if not texts:
        raise HTTPException(status_code=400, detail="Input cannot be empty")

    loop = asyncio.get_event_loop()
    try:
        embeddings_list, dim = await loop.run_in_executor(None, _embed_texts, texts)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating embeddings: {str(e)}")

    model_name = req.model or EMBEDDING_MODEL_NAME
    return {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "embedding": emb,
                "index": i,
            }
            for i, emb in enumerate(embeddings_list)
        ],
        "model": model_name,
        "usage": {
            "prompt_tokens": sum(len(t) // 4 for t in texts),
            "total_tokens": sum(len(t) // 4 for t in texts),
        },
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    loop = asyncio.get_event_loop()

    # Load model if not cached (non-blocking for other models)
    try:
        pipe = await loop.run_in_executor(None, lambda: get_or_load_pipeline(req.model))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error cargando modelo: {str(e)}")

    model_path = _resolve_model_path(req.model)
    model_lock = _get_model_lock(model_path)
    model_id = os.path.basename(model_path)

    # Build messages dict for apply_chat_template, including tool-related fields
    messages_dict = []
    for m in req.messages:
        md = {"role": m.role, "content": m.content or ""}
        if m.tool_calls:
            md["tool_calls"] = [tc.model_dump() for tc in m.tool_calls]
        if m.tool_call_id:
            md["tool_call_id"] = m.tool_call_id
        if m.name:
            md["name"] = m.name
        messages_dict.append(md)

    # Prepare tools list for apply_chat_template
    tools_list = None
    if req.tools:
        tools_list = [t.model_dump() for t in req.tools]

    # Apply chat template with tools if provided
    try:
        tokenizer = pipe.get_tokenizer()
        kwargs = {"add_generation_prompt": True}
        if tools_list:
            kwargs["tools"] = tools_list
        prompt_text = tokenizer.apply_chat_template(messages_dict, **kwargs)
    except Exception:
        # Fallback al formato manual si el chat template no esta disponible
        prompt_text = ""
        for msg in req.messages:
            prompt_text += f"<|im_start|>{msg.role}\n{msg.content or ''}<|im_end|>\n"
        prompt_text += "<|im_start|>assistant\n"

    gen_config = ov_genai.GenerationConfig()
    gen_config.max_new_tokens = req.max_tokens or 512
    if req.temperature is not None and req.temperature > 0:
        gen_config.temperature = req.temperature
    if req.top_p is not None:
        gen_config.top_p = req.top_p

    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    if not req.stream:
        def do_gen():
            with model_lock:
                return pipe.generate(prompt_text, gen_config)

        response_text = await loop.run_in_executor(None, do_gen)

        # Normalize: OpenVINO GenAI may return VLMDecodedResults or other
        # non-string objects even for LLMPipeline in some versions.
        if not isinstance(response_text, str):
            if hasattr(response_text, "texts"):
                response_text = str(response_text.texts[0]) if response_text.texts else ""
            else:
                response_text = str(response_text)

        # Parse tool calls from response
        tool_calls, cleaned_content = parse_tool_calls(response_text, tools_list)

        # For gpt-oss models without tool calls, strip reasoning prefix
        if tool_calls is None and _is_gpt_oss_model(model_id):
            cleaned_content = _strip_gpt_oss_reasoning(response_text)

        if tool_calls:
            message = {
                "role": "assistant",
                "content": cleaned_content,
                "tool_calls": tool_calls,
            }
            finish_reason = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": cleaned_content,
            }
            finish_reason = "stop"

        return {
            "id": req_id,
            "object": "chat.completion",
            "created": created_ts,
            "model": req.model or model_id,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason
                }
            ],
            "usage": {
                "prompt_tokens": len(prompt_text) // 4,
                "completion_tokens": len(response_text) // 4,
                "total_tokens": (len(prompt_text) + len(response_text)) // 4
            }
        }
    else:
        async def stream_generator():
            streamer_queue = asyncio.Queue()

            def custom_streamer(subword: str):
                streamer_queue.put_nowait(subword)
                return ov_genai.StreamingStatus.RUNNING

            def run_gen():
                with model_lock:
                    try:
                        pipe.generate(prompt_text, gen_config, custom_streamer)
                    finally:
                        streamer_queue.put_nowait(None)

            loop.run_in_executor(None, run_gen)

            # When tools are provided, we must buffer the full response to parse
            # tool_call tags. OpenAI streaming sends tool_calls as a delta in the
            # final chunk(s), not as content text. If we stream the raw tokens
            # (which include <tool_call> tags and JSON), the client sees them as
            # plain text instead of structured tool_calls.
            if tools_list:
                full_text = []
                while True:
                    token = await streamer_queue.get()
                    if token is None:
                        break
                    full_text.append(token)

                response_text = "".join(full_text)
                tool_calls, cleaned_content = parse_tool_calls(response_text, tools_list)

                # For gpt-oss models without tool calls, strip reasoning prefix
                if tool_calls is None and _is_gpt_oss_model(model_id):
                    cleaned_content = _strip_gpt_oss_reasoning(response_text)

                if tool_calls:
                    # Send tool_calls as delta (OpenAI streaming format)
                    for i, tc in enumerate(tool_calls):
                        chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": req.model or model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "tool_calls": [
                                            {
                                                "index": i,
                                                "id": tc["id"],
                                                "type": "function",
                                                "function": {
                                                    "name": tc["function"]["name"],
                                                    "arguments": tc["function"]["arguments"],
                                                },
                                            }
                                        ],
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    # Final chunk with finish_reason=tool_calls
                    chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": req.model or model_id,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                else:
                    # No tool calls found — send cleaned content as text
                    if cleaned_content:
                        chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": req.model or model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": cleaned_content},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": req.model or model_id,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"

                yield "data: [DONE]\n\n"
                return

            # No tools: standard passthrough streaming
            while True:
                token = await streamer_queue.get()
                if token is None:
                    break

                chunk = {
                    "id": req_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": req.model or model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": token},
                            "finish_reason": None
                        }
                    ]
                }
                yield f"data: {json.dumps(chunk)}\n\n"

            # Final stop chunk
            chunk = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": req.model or model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream_generator(), media_type="text/event-stream")


@app.get("/")
async def root():
    return {
        "service": "OpenVINO GenAI Intel Arc Server",
        "version": "2.2.0",
        "device": DEVICE,
        "embedding_device": EMBEDDING_DEVICE,
        "loaded_models": [os.path.basename(p) for p in _pipeline_cache],
        "endpoints": ["/v1/chat/completions", "/v1/embeddings", "/v1/models", "/health", "/v1/warmup",
                      "/admin", "/v1/admin/system", "/v1/admin/gpu/status", "/v1/admin/models",
                      "/v1/admin/cache", "/v1/admin/logs", "/v1/admin/benchmark"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)