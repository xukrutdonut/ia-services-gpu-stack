# Malla de inferencia — operación

Una sola puerta OpenAI en `:4000` (LiteLLM). El **nombre del modelo** elige hardware;
ningún cliente conoce IPs ni rutas.

```
cliente (Hermes, pi, docker-agent, Open WebUI, scripts)
        │  http://<host>:4000/v1   Bearer $LITELLM_MASTER_KEY
        ▼
     LiteLLM  ── alias ──▶ carril ──▶ backend
        │                              ├── V100  ninfer-serve :8090   ← MOTOR PRINCIPAL
        │                              ├── Arc iGPU (OVMS :8006 → interno :8000)
        │                              ├── Hailo-8L Pi5 .80:8089  (OCR + CLIP imagen)
        │                              ├── NPU Intel :8012        (embeddings texto)
        │                              └── RX480 Pi5 .80:1237     (Vulkan, solo pi-nas)
        ▼
  mesh-orchestrator :4010   cola acotada + subagentes (jobs / fanout)
        ▼
  Open WebUI :3000 (host) → :8080 (contenedor)   frontend, habla solo con LiteLLM
```

**Desde 2026-11-09 todos los alias de TEXTO apuntan al mismo motor**: `ninfer-serve`
en la V100 (:8090, 262k de contexto). Los nombres viejos se mantienen para no romper
clientes (`~/.pi`, wp-translator, wp-editorial, scripts), pero `chat`, `deep`, `fast`,
`lms27`, `worker-big` y `ninfer` son el **mismo backend**: ya no hay swap de modelos ni
dos motores en la V100 (el 35B ocupa 28 de los 32 GB, no cabe otro).

## Alias

| alias        | backend real             | fallback     | uso                                  |
|--------------|--------------------------|--------------|--------------------------------------|
| `ninfer`     | V100 ninfer-serve 262k   | Arc          | motor principal (135 t/s decode)     |
| `chat`       | V100 ninfer-serve 262k   | Arc          | chat general, tool-calling           |
| `deep`       | V100 ninfer-serve 262k   | Arc          | documentos largos, código            |
| `fast`       | V100 ninfer-serve 262k   | worker       | alias histórico (ya no es un 4B)     |
| `lms27`      | V100 ninfer-serve 262k   | Arc          | alias histórico (ya no es un 27B)    |
| `worker-big` | V100 ninfer-serve 262k   | worker       | fan-out con más calidad              |
| `vision`     | V100 ninfer `--vision`   | —            | imágenes y vídeo (ctx 32k por petición) |
| `worker`     | Arc Qwen3-30B-A3B-int4   | arc          | subagente en la iGPU, no toca la V100 |
| `arc`        | Arc Qwen3-30B-A3B-int4   | —            | tareas de fondo de Open WebUI (título/tags/follow-up) y respaldo |
| `embed`      | Arc qwen3-embedding     | nomic        | RAG                                  |
| `embed-npu`  | NPU qwen3-embedding int4 | —           | RAG que libera la iGPU (ver nota de pooling) |
| `embed-vision` | Hailo-8L CLIP ResNet-50x4 | —        | embeddings de IMAGEN (640 d) en el Pi5 .80 |
| `ocr`        | Hailo-8L det+rec        | —            | OCR: se le pasa la imagen, devuelve el texto |

## Enrutado por velocidad (bench 2026-11-08, prompt único 2k, temp 0)

| backend            | prefill   | decode    | TTFT con 3k de contexto |
|--------------------|-----------|-----------|--------------------------|
| V100 35B-A3B Q4 + MTP | ~660 t/s | 92-106 t/s | 4,4 s                  |
| V100 35B-A3B Q4 (sin spec) | 725-742 t/s | 88-94 t/s | 4,0 s            |
| V100 27B denso     | ~250 t/s  | 32 t/s    | ~12 s                    |
| V100 Qwen3.5-4B    | 2207 t/s  | 110 t/s   | 1,3 s (calidad baja, solo aux) |
| Arc Qwen3-30B int4 | 182-213 t/s | 21-24 t/s | 16 s                     |

**MTP (decodificación especulativa nativa)**: el 35B trae cabeza `nextn`; con
`--spec-type draft-mtp --spec-draft-n-max 2` genera 2 tokens por pasada. Barrido
n-max: 2 → 119,7 tok/s | 3 → 99,8 | 4 → 77,9 | 6 → 54,6. Más borrador empeora:
el acierto del experto cae. Cuesta ~10% de prefill (661 vs 742 t/s) y gana 15-35%
de decode; sale a cuenta porque en conversación el prefijo va cacheado y lo que
manda es la decodificación.

Regla: **generación y prefill mandan la V100; la Arc solo sostiene lo que debe estar
siempre encendido** (embeddings, tareas de fondo, respaldo). `chat` y `deep` comparten
el mismo servidor de llama-swap, así que alternar entre ellos no recarga modelo.

## Embeddings en la NPU (`embed-npu`)

`npu-embeddings.service` (systemd --user, `~/workspace/npu-embeddings`, puerto 8012,
bind `0.0.0.0`) sirve qwen3-embedding-0.6b-int4 en la NPU con una única forma estática
`1x512` (la NPU no admite shapes dinámicos: cada longitud nueva cuesta ~30 s de
recompilación). El arranque carga el modelo en ~7 s.

Coste medido: ~510 ms por texto de 11 tokens, ~310 ms/texto en lotes (la inferencia va
serializada: `max_parallel_requests: 1` en el mesh). Es más lento que la iGPU por token
(pool de 512 fijo) pero deja la Arc libre para generación.

**Pooling distinto → vectores no intercambiables.** El servicio de la NPU usa
last-token (lo que recomienda Qwen3); el grafo de OVMS de la iGPU usa CLS
(coseno entre ambos espacios: 0.36). Por eso no hay fallback cruzado entre `embed` y
`embed-npu`: un índice RAG debe construirse y consultarse con el mismo alias.
`zotero-index.service` ya indexa contra la NPU (:8012).

## Hailo-8L del Pi5 (192.168.0.80)

El Hailo-8L solo tiene modelos de **visión**: no existe tower de texto, así que no
puede hacer embeddings de texto. Aporta dos cosas al mesh:

- `embed-vision` — CLIP ResNet-50x4 (HEF del zoo v2.17.0), 640 d, ~195 ms/imagen
  (340 ms cruzando el mesh). El `input` son **imágenes** (data URI base64, URL o
  base64 suelto); un texto devuelve 400. Es similitud imagen-imagen y zero-shot a
  mano, no búsqueda con palabras: para eso haría falta el tower de texto de CLIP
  en CPU, que no está desplegado.
- `ocr` — det+rec de PaddleOCR en la NPU. Se consume como chat: la imagen va en
  `content[].image_url` y la respuesta es el texto. ~0,9 s una captura limpia,
  ~5,7 s una página escaneada completa. Sin imagen responde 400.

Ambos viven en el mismo servicio que ya hacía OCR (`:8089`, contenedor
`ocr-pdf-hailo`, unidad `ocr-pdf-hailo.service`): se le añadieron
`/v1/embeddings`, `/v1/chat/completions` y `/v1/models`, y se montó el HEF de CLIP
en `/app/clip_models`. El motivo es que **el dispositivo Hailo es exclusivo de un
proceso**: la app tiene el `VDevice` abierto desde el arranque y un segundo proceso
recibe `HAILO_OUT_OF_PHYSICAL_DEVICES` (error 74) — probado, también con
`group_id="SHARED"` y con el servicio multi-proceso. CLIP se configura como una
tercera red del mismo `VDevice` (round-robin, grupo SHARED), que sí convive con
det+rec: configurar las tres a la vez tarda 0,3 s.

El HEF es `clip_resnet_50x4.hef` de `ModelZoo/Compiled/v2.17.0/hailo8l/` (126 MB);
v2.18 y posteriores responden 403 en el S3 del zoo.

### Arc/OVMS: barrido de modelos rápidos (2026-11-08)

Pregunta: ¿puede la iGPU servir un carril rápido con otro modelo? Medido con OVMS
(`:8009`, perfil `chatfast`, para no tocar el carril de producción):

| modelo en la Arc | decode | prefill | latencia real (JSON corto) |
|---|---|---|---|
| Qwen3-30B-A3B-Instruct int4 (actual) | 21,9 tok/s | 190 tok/s | **2,8 s / 36 tok** |
| LFM2.5-8B-A1B int4 (8,3B, 1,5B activos) | 41-43 tok/s | 560 tok/s | 6,7 s / 244 tok |
| LFM2.5-8B-A1B **con el 30B cargado** | 21 tok/s | — | — |

Tres hechos que cierran el tema:

1. **Techo de ancho de banda**: la iGPU usa la DDR5 del equipo (~37 GB/s
   efectivos). Un token del 30B int4 mueve ~1,7 GB; el techo teórico son ~22-40
   tok/s y ya estamos en 22. Ningún ajuste de servidor lo rompe.
2. **Más tok/s ≠ menos latencia**: el LFM2.5-8B-A1B decodifica al doble, pero es un
   modelo híbrido que **razona siempre** (244-384 tokens para un JSON trivial o un
   cálculo de una línea) y sin instrucción de idioma contesta en inglés. En latencia
   por petición sale 2-5x peor que el 30B, que es instruct puro y no gasta thinking.
3. **No caben dos LLM**: con el 30B cargado, el LFM cae de 43 a 21 tok/s. La iGPU
   es un solo motor; el segundo modelo no añade carril, lo reparte.

Conclusión: la Arc se queda como está (30B para `worker`/`arc`/fallback). El
carril rápido de verdad está en la V100 (`fast`, Qwen3.5-4B, 110 tok/s). El IR de
LFM2.5 queda en `~/modelos/openvino/ir/LFM2.5-8B-A1B-int4-ov/1` por si se quiere
repetir la prueba: `docker compose --profile chatfast up -d ia-gpu-intel-fast`
(puerto 8009, arranca con `--model_name/--model_path` porque OVMS no acepta este
repo como versión numérica en `config.json`).

Si el escritorio se apaga (llama-swap parado), `chat` cae solo a la Arc en ~3 s
(cooldown 60 s), y vuelve a la V100 cuando el backend revive.

### ¿Dividir una tarea compleja la acelera? (medido 2026-11-08)

Sí, pero el factor es **~1,5x con 4 ramas**, no 4x. El servidor del 35B corre con
`--parallel 4 --kv-unified` (antes 2), 32.768 tokens de contexto total y 24,5 GB de VRAM.

| ramas en vuelo | agregado | TTFT | fin de cada rama (400 tok) |
|---|---|---|---|
| 1 | 116 tok/s | 0,15 s | 3,4 s |
| 2 | 152 tok/s | 0,32 s | 5,3 s |
| 4 | 172 tok/s | 0,72 s | 8,6-9,5 s (todas a la vez) |
| 6 | 171 tok/s | 0,59 s / 8,97 s | 4 acaban a ~9 s, 2 esperan otros 9 s |

Tres reglas que salen de la tabla:

1. **El techo del batching es ~172 tok/s**: la cuarta secuencia ya no añade nada y la
   quinta solo hace cola. Dividir en más de 4 ramas empeora la latencia de las últimas
   sin ganar nada. `max_parallel_requests` de `chat`/`worker-big` = 4 (antes 2, que
   encolaba la mitad del fan-out en LiteLLM).
2. **El prefill no se divide**. Medido: 6,4k tokens → 9,1 s y 28,9k → 45,1 s (~650-700
   tok/s, plano con la concurrencia). Partir un documento de 29k en 4 ramas de 7k lee
   los mismos 29k tokens y cuesta los mismos ~45 s. Solo se ahorra si las ramas comparten
   un **resumen** previo (o RAG por trozos) en vez del documento entero.
3. **Una respuesta de 1.600 tokens**: monolítica ≈ 14 s; en 4 ramas de 400 ≈ 9,7 s
   (1,45x); en 6 ramas empeora. La fusión de las ramas cuesta un turno más, así que la
   ganancia neta realista es **1,3-1,4x** en trabajo de generación.

Verificado a través de la puerta: 4 peticiones de 200 tok por LiteLLM acaban todas en
4,5 s (antes 2+2 tardaban ~7,3 s). El contexto máximo sigue siendo 32k: un prompt de
~29k entra, uno de ~14k palabras (≈50k tokens) devuelve 400.

## Cola y backpressure (dos niveles)

1. **LiteLLM** (vale para cualquier cliente)
   - `max_parallel_requests` por deployment = concurrencia real; el resto espera en FIFO.
   - `rpm` = válvula: por encima devuelve 429.
   - `timeout` por deployment; los carriles del nodo `.80` usan **20 s** y `num_retries: 0`
     (el proxy de `.80` reintenta hasta 300 s: sin este corte, cliente y cola se cuelgan).
   - `allowed_fails: 1` + `cooldown_time: 60`: un fallo saca al backend de rotación y entra el fallback.
2. **mesh-orchestrator** (solo trabajos por lotes)
   - Semáforo por carril (`LANE_CONCURRENCY`) y cola acotada `MAX_QUEUE_PER_LANE`
     (def. 100): al llenarse, `429` con `Retry-After: 30`.
   - **Sonda de salud por carril** (`LANE_HEALTH`): si el nodo no da 200, el carril se
     desvía a `LANE_FALLBACK` durante `LANE_DOWN_TTL` (60 s) sin esperar el timeout.
     Evita perder 20 s por tarea cuando un nodo está caído.
   - `GET /v1/queue` expone `running`, `queued`, `rejected`, `down`, `rerouted` por carril.

## Subagentes

```
POST :4010/v1/jobs     {"model":"chat","tasks":[{"id","prompt","system?"}],"max_concurrency":2,"planner":"chat"}
POST :4010/v1/fanout   {"task":"...","items":["..."],"worker":"chat","planner":"chat","max_concurrency":2}
GET  :4010/v1/jobs/{id}       -> results[] + payload.final (síntesis del planner)
GET  :4010/v1/jobs?limit=20
DELETE :4010/v1/jobs/{id}
```

Cada subagente tiene su propio contexto. `planner` sintetiza al final. `ALLOW_SHELL=0`
mantiene la herramienta `run_command` desactivada (aislada en el contenedor).

## Arranque / parada

```bash
cd ~/produccion/ia-services-stack
docker compose up -d litellm open-webui mesh-orchestrator
systemctl --user status llama-swap.service     # V100 (imprescindible)
```

La V100 no va en Docker: llama-swap necesita CUDA del host. `ttl: 3600` en el 35B
(modelo de chat) lo mantiene caliente 1 h; el resto de modelos usa `ttl: 900`
(15 min sin uso) y liberan la VRAM. Arranque en frío del 35B: ~12 s.

## Diagnóstico rápido

```bash
curl -s :4000/v1/models -H "Authorization: Bearer $K" | jq -r '.data[].id'   # alias vivos
curl -s :4010/v1/queue | jq                                                 # presión por carril
nvidia-smi --query-gpu=memory.used,temperature.gpu,clocks.sm --format=csv   # V100
docker logs litellm --since 5m | grep -i fallback
```

## Trampas conocidas

- `du -sh /var/lib/docker` miente si el `data-root` está en otro sitio: usar
  `docker info --format '{{.DockerRootDir}}'`.
- El 35B de la V100 razona en inglés y se come el presupuesto de tokens: los alias de la
  V100 llevan `extra_body: {chat_template_kwargs: {enable_thinking: false}}`.
  Quien quiera razonamiento lo pide en la propia petición.
- La RX480 del Pi5 se cae del bus PCIe (`lspci` sin dispositivo): el rescan no la
  devuelve, hace falta corte de alimentación real. Mientras tanto `worker`/`tiny` se
  desvían solos por la sonda de salud.
- **Los modelos de la UI llevan prefijo `mesh.`** (`mesh.chat`, `mesh.arc`, ...). Poner
  `task.model.default = "arc"` deja las tareas de fondo (título, tags, follow-up) sin
  modelo válido y caen al modelo del chat, robando un slot de la V100. El valor correcto
  es `"mesh.arc"`. Comprobar con `GET /api/models` (ids reales), no a ojo.

## Compresión de contexto en Open WebUI (activa 2026-11-08)

Nativa (`Admin → Settings → Chat → Context Compaction`), guardada en `webui.db`
(`ENABLE_PERSISTENT_CONFIG=true`, no hay variables de entorno):

```
chat.context_compaction.enable                = true
chat.context_compaction.token_threshold       = 24000   # 75% del ctx real de 32k
chat.context_compaction.retention_percentage  = 40
chat.context_compaction.model                 = "mesh.chat"
chat.context_compaction.token_cap             = null
chat.context_compaction.prompt_template       = ""
```

- El umbral por defecto (80.000) **nunca se dispara** con `--ctx-size 32768`: hay que
  bajarlo. 24k deja ~8k de margen para el prompt de sistema y la respuesta.
- El resumen lo hace el mismo 35B (~650-700 t/s de prefill): una conversación de 24k
  cuesta ~35 s de resumen, una vez, en un slot. `mesh.fast` sería más rápido pero
  obligaría a descargar el 35B (`ttl: 3600`), así que no compensa.
- `rag.full_context = false`: los PDF adjuntos entran por top-k de fragmentos, no
  enteros. Es lo que evita prompts de 29k tokens (~45 s de prefill).
- Verificar que la app lo cargó (no solo la BD): `GET /api/v1/configs/export` con JWT
  admin devuelve `chat.context_compaction.*` con las claves planas y puntos.

## Contexto 64k y el 400 que tumbaba los chats largos (2026-11-08)

Sintoma: al enviar en un chat antiguo, la UI devolvia error y el log de llama-swap
registraba `status=400`:

```
request (48912 tokens) exceeds the available context size (32768 tokens), try increasing it
type: exceed_context_size_error
```

Causa: `chat`/`deep` corrian con `--ctx-size 32768` y habia chats de 294 kB de JSON
(~46-49k tokens). Open WebUI **no trunca el historial** (no existe `context_length`
ni `prompt_token_limit` en su backend), y su compactacion tampoco puede rescatar un
chat que ya excede el ctx: `_generate_summary` mete `COMPACTED_MESSAGES` entero en el
prompt, asi que el propio resumen vuelve a dar 400. El `except` del emisor propaga el
fallo y la peticion muere.

Arreglo aplicado en `~/.config/llama-swap/config.yaml` (35B):

```
--ctx-size 65536 --parallel 4 --kv-unified --cache-type-k q8_0 --cache-type-v q8_0 -fa on
```

Coste medido: **+244 MiB de VRAM** (24.454 -> 24.698 MiB con el modelo cargado). El KV
en q8 ocupa lo mismo con 64k que en fp16 con 32k. Carga en frio 10,7 s, pico 24.786 MiB
de 32.768. LiteLLM pasa a declarar `max_input_tokens: 65536` (lo deriva de
`context_length` de llama-swap, hay que **reiniciar litellm** para que lo relea).

Pruebas:

| prueba | antes | despues |
|---|---|---|
| 46.412 tok directo a llama-swap | 400 | 200 en 74,3 s (prefill frio) |
| 46.416 tok via Open WebUI (`/api/chat/completions`) | 400 | 200 en 1,6 s (prefijo caliente) |

Chats existentes rescatados con el endpoint nativo (no hace falta tocar la BD a mano):

```
POST /api/v1/chats/<id>/compact   {"model":"mesh.chat"}   # JWT del dueno del chat
```

- `Ataxia Congenita Revision`: 46k -> 10.750 tok (45% del umbral).
- `Document Improvement`: 6.855 tok (29%).

Devuelve `context_usage` con `tokens`, `threshold` y `percent`; el resumen se guarda
como `contextSummary` en el mensaje checkpoint (`chat_message.context_summary`), y a
partir de ahi cada envio solo manda resumen + cola reciente.

Ojo con el 500 "Open WebUI: Server Connection Error" (`Connect call failed
('172.18.0.5', 4000)`): no es la V100, es LiteLLM reiniciandose en ese instante. Se
resuelve solo en ~10 s; no requiere tocar nada.

## LM Studio como único motor (2026-11-08)

- **llama-swap eliminado**: binario (`~/.local/bin/llama-swap`), unit systemd y `~/.config/llama-swap/` borrados. Copia en `~/.hermes/cache/scratch/llama-swap-backup/` (referencia de flags).
- **LM Studio `:1234` es el único backend de la V100**. Carga: `lms load qwen3.6-35b-a3b-mtp --gpu max -c 65536 --parallel 4 --speculative-draft-mtp --speculative-draft-max-tokens 2 --identifier qwen3.6-35b-a3b-mtp --ttl 86400 -y`. VRAM 25.174-25.238 MiB, carga 10-11 s.
- **La CLI no persiste la config por modelo**: si el JIT recarga el modelo, vuelve a **8192** de contexto. Por eso `~/.local/bin/mesh-load-35b.sh` + `~/.config/autostart/mesh-load-35b.desktop` (reintenta, fija `LMS_SERVER_HOST=0.0.0.0` y carga con ctx 64k).
- **El servidor de LM Studio vuelve a `127.0.0.1` al reiniciarse**: con ese bind los contenedores NO llegan (`host.docker.internal`) y todo cuelga hasta el timeout (90 s). El `.desktop` de LM Studio lleva ahora `Exec=env LMS_SERVER_HOST=0.0.0.0 ...`; verificar con `ss -ltnp | grep :1234`.
- **LiteLLM `:4000` sigue siendo el único punto de entrada** (LiteLLM → `host.docker.internal:1234/v1`). `extra_body: {reasoning_effort: none}` porque LM Studio ignora `chat_template_kwargs.enable_thinking` y `/no_think`.
- **`.105` canalizado por el enrutador**: `wp-editorial_reviewer` y `wp-translator_translator` con `LLM_API_URL=http://192.168.0.100:4000/v1`, `LLM_MODEL=chat`, `LLM_API_KEY`, imágenes `:lmstudio` (Authorization condicional + `reasoning_effort: none` en el payload). YAML sincronizados.
- Medido: `.105` → `:4000` → `:1234` = **0,5 s** (antes del arreglo del bind: timeout de 90 s).

## Motor único `ninfer` + delegación por capacidad (2026-11-09)

### Arranque del motor (V100)

`ninfer-serve` corre **dentro** del contenedor `ninfer-serve`, cuyo CMD es
`sleep infinity`: `docker restart ninfer-serve` NO levanta el motor. Relanzar a mano:

```
docker exec -d ninfer-serve sh -c 'setsid /src/build-v100/apps/ninfer-serve \
  /models/qwen3_6_35b_a3b_v2.ninfer --host 0.0.0.0 --port 8090 \
  --max-context 262144 --max-concurrency 2 --default-max-tokens 8192 \
  --spec mtp --draft-tokens 2 > /tmp/serve.log 2>&1 < /dev/null'
```

- `--host 0.0.0.0` es obligatorio: por defecto escucha en 127.0.0.1 y LiteLLM
  (contenedor) no lo alcanza.
- Estado real: `docker exec ninfer-serve tail -5 /tmp/serve.log` → `engine ready`,
  `max_model_len 262144`, KV 262.144 tok bf16 / 2 carriles, 28,4/32,8 GB de VRAM.
- Nunca `docker exec ... pkill -f ninfer-serve`: el patrón coincide con el propio
  `bash -c` del exec y mata también el exec. Usar
  `pgrep -f '^/src/build-v100/apps/ninfer-serve'` + `kill`.
- Coste del contexto: 70k de prompt = 3m35s de TTFT (325 t/s de prefill);
  200k ≈ 10 min. El prefijo cacheado hace que el turno siguiente sea casi gratis.

### LiteLLM (`:4000`)

- Todos los alias de texto → `http://host.docker.internal:8090/v1`, modelo
  `openai/qwen3.6-35b-a3b`, `max_parallel_requests: 2` (el motor tiene 2 carriles),
  `model_info: {context_length: 262144, max_input_tokens: 253952, max_output_tokens: 8192}`.
- `router_settings.allowed_fails: 5`, `cooldown_time: 30`: con `allowed_fails: 1` un
  400 puntual (prompt gigante) sacaba `ninfer` de rotación 60 s y **todo** devolvía
  429 «No deployments available».
- `fallbacks`: `ninfer/chat/deep/lms27 → [arc]`, `fast/worker-big → [worker]`,
  `worker → [arc]`. El respaldo real es la iGPU: no hay un segundo motor V100.
- Verificar: `for m in ninfer chat deep fast lms27 worker-big; do curl -s :4000/v1/chat/completions -d '{"model":"'$m'",...}'; done`.

### Open WebUI (`:3000` en el host → `:8080` en el contenedor)

- **Modelo por defecto**: `ui.default_models = ["ninfer-malla"]` (preset creado por API).
- Preset `ninfer-malla` («ninfer + malla», `base_model_id: mesh.ninfer`) con
  `meta.toolIds: ["mesh_delegate"]`: chat sobre la V100 con la herramienta de
  delegación ya enchufada. Cambiar el default en caliente requiere tocar
  `webui.db` (`config` → `ui.default_models`) + `docker restart open-webui`.
- `chat.context_compaction.token_threshold`: 24000 → **180000** (el ctx real ya no es
  32k). El resumen lo hace `mesh.chat`, que es el propio ninfer.
- `task.model.default = "mesh.arc"`: títulos/tags/follow-up se generan en la iGPU.
- La UI **no** está en `:8080` del host: `docker port open-webui` → `3000`.

### La herramienta `mesh_delegate`

Fuente versionada: `openwebui/tools/mesh_delegate.py` (registrada en OWUI como tool
`mesh_delegate`, id del mismo nombre). Una sola función `delegate(capability, task,
attachments)`:

| capability   | va a                          | medido                                        |
|--------------|-------------------------------|-----------------------------------------------|
| `openvino`   | `worker` (Arc Qwen3-30B)      | clasificación clínica en 1,9 s                |
| `ocr`        | `ocr` (Hailo-8L)              | «FACTURA 2026-0042 total 199» de un PNG       |
| `embed_text` | `embed-npu` (NPU, 1024 d)     | cos 0,71 visa ↔ tarjeta vs 0,58 vuelo         |
| `embed_image`| `embed-vision` (Hailo, 640 d) | cos 0,99 imagen casi igual vs 0,71 distinta   |
| `fanout`     | `mesh-orchestrator /v1/fanout`| 3 documentos → 3 subagentes (2 s c/u) + síntesis del planificador `arc` |
| `status`     | `/v1/queue`                   | carriles ocupados / caídos                    |

Flujo real en la UI (chat «Text Classification»): ninfer pidió `delegate(openvino)`,
la UI lo ejecutó (1,9 s), devolvió «Clasificación: Síntoma cardíaco sospechoso de
infarto agudo de miocardio» y ninfer lo redactó. El bucle de tool-calling lo hace el
backend de Open WebUI (`process_chat_response`), no el modelo.

### Trampas nuevas

- **Los valves de una tool pisan `__init__`**: OWUI aplica los valves guardados en la
  BD *después* de instanciar la clase. Guardar `api_key` en el valve lo deja vacío y
  todas las delegaciones fallan con `LocalProtocolError: Illegal header value b'Bearer '`
  (o `400 No connected db`). La clave se lee **en cada llamada** del entorno del
  contenedor (`OPENAI_API_KEYS`) y el valve solo la sobrescribe si trae valor.
- **`POST /api/chat/completions` NO ejecuta las tools**: devuelve el `tool_calls` y
  termina (`finish_reason: "tool_calls"`). El bucle de ejecución + segunda pasada vive
  en la UI y necesita `chat_id`/`session_id`. Para probar de verdad: navegador o la UI.
- La tool necesita `tool_ids` en el body si se prueba por API; con el preset ya vienen
  en `meta.toolIds`.
- `chat.tool_permissions.enable = false` (sin aprobación manual) es lo que permite que
  las delegaciones se ejecuten solas.
- OWUI manda **41 tools** por turno (MCP searxng + builtins): ~8,1k tokens de prompt
  fijos en cada petición. Con 262k de contexto es irrelevante, pero encarece el prefill
  en frío.

## Modelos de ninfer: catálogo, cambio y ampliación (2026-11-09)

NInfer **no carga GGUF**: sirve artefactos `.ninfer` (pesos + tokenizer + chat template +
recursos del frontend de visión en un solo fichero). Un proceso = un modelo residente.

### Catálogo oficial (todos incluyen torre de visión registrada)

| artefacto | peso | fichero | notas |
|---|---:|---:|---|
| `qwen3_6_35b_a3b_v2.ninfer` | 35B MoE (3B act.) | 22,78 GB | el que corre ahora, 139 tok/s decode |
| `qwen3_6_35b_a3b.ninfer` | 35B MoE | 22,79 GB | **v3, no cargable** en este fork (lee v2) |
| `qwen3_6_27b.ninfer` | 27B denso int | 17,50 GB | 54 tok/s |
| `qwen3_6_27b_nvfp4.ninfer` | 27B NVFP4 | 18,32 GB | 55 tok/s |
| `qwen3_8_27b.ninfer` | 27B denso int | 20,44 GB | 131 tok/s |
| `qwen3_8_27b_nvfp4.ninfer` | 27B NVFP4 | 23,72 GB | **219 tok/s** (el más rápido), 99% aceptación MTP |

Descarga: `hf download neroued/<Modelo>-NInfer <fichero>.ninfer --local-dir ~/workspace/ninfer-v100/models`
(el manifiesto con sha256 está en `model-cards/<Modelo>-NInfer/artifact-manifest.json` dentro de `/src`).

### Cambiar de modelo

```
~/produccion/ia-services-stack/ninfer-serve.sh qwen3_8_27b.nvfp4.ninfer 262144 2
```

El script mata el proceso y relanza con los flags de producción (incluido `--vision`).
LiteLLM **no se toca**: mismo puerto :8090 y mismo model id, así que todos los alias siguen
funcionando. Sí hay que ajustar `model_info.context_length` si el artefacto no aguanta 262k.
Un `docker restart ninfer-serve` deja el puerto cerrado: el CMD del contenedor es `sleep infinity`.

### Añadir modelos nuevos

`/src/tools/convert` trae un conversor por familia: `qwen3_6`, `qwen3_6_27b`,
`qwen3_6_35b_a3b`, `qwen3_8_27b`. Convierte un checkpoint HF a `.ninfer` con el layout y la
cuantización que espera el runtime (groupwise-int / nvfp4). Es decir:

- Mismo modelo, otra cuantización o revisión → sí, es el camino (`tools/convert` + `artifact`).
- Otra familia (Llama, Mistral, Gemma, un VLM de terceros) → no hay conversor: exige kernels y
  registro de arquitectura en C++ (el README lo dice: "the runtime is intentionally narrow").

### Visión (imágenes y vídeo)

El artefacto del 35B-A3B **ya trae** la torre de visión; solo estaba apagada. Con `--vision`:

- Acepta `image_url` y `video_url` (HTTP(S), data URI o ruta local) en Chat Completions/Responses.
- Coste medido: +330 MiB de VRAM (28.446 → 28.776 MiB usados) y `free` baja de 3,96 a 3,63 GiB.
- Una petición con imagen se limita a `min(--max-context, 32768)` tokens de contexto.
- `--media-cache-mib 1024` (caché LRU de payloads ya decodificados) y `--media-live-mib 2048`
  (BF16 de parches vivos); 16 workers de preproceso.
- Probado: «FACTURA 2026-0042 / total 199 EUR» de un PNG por `model=vision` y `model=ninfer`,
  0,2-0,9 s, 89-144 tokens de prompt.

Con esto la malla ya no necesita un VLM aparte: no cabe otro modelo en la V100 (quedan 3,6 GiB)
y el 35B ve imágenes sin ceder contexto ni carril.
