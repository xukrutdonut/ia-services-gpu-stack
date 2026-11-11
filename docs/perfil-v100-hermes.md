# Perfil Hermes `v100`: contexto mínimo y máxima velocidad

Hermes con la V100 (ninfer, `:8090`) tardaba decenas de segundos en empezar a
responder. No era la red ni los tool calls: era el **prompt fijo** que se reenvía en
cada turno. ninfer hace prefill a ~284 tok/s (medido, CLI greedy), así que 20k tokens
de prompt son ~70 s antes del primer token. El trabajo, por tanto, es recortar ese
prompt de forma permanente, no acelerar la GPU.

Perfil dedicado: `~/.hermes/profiles/v100` (wrapper `v100`, alias de
`hermes -p v100`). Aislado del perfil `default`: config, sesiones, memoria y skills
propios. El `default` sigue apuntando a ollama-cloud.

## Medición (no estimación)

`hermes -p v100 -m <modelo> -z 'Responde solo: ok' --usage-file /tmp/u.json` reporta
`input_tokens` del turno completo (system prompt + esquemas de herramientas + índice
de skills + memoria). Mismo modelo y mismo prompt en todas las filas:

| Configuración | input_tokens | Δ |
|---|---:|---:|
| `profile create v100 --clone` (clon exacto del default) | 20 154 | — |
| + toolsets recortados a terminal/file/code_execution/web | 6 629 | −67 % |
| + toolset `skills` con el índice vacío | 7 938 | |
| + 6 skills relevantes en el índice (estado actual) | **8 059** | **−60 %** |
| suelo: sin ninguna herramienta | 1 624 | |

Coste marginal por toolset, midiendo uno solo cada vez sobre el suelo de 1 624:

| Toolset | Coste | ¿Activo en `v100`? |
|---|---:|---|
| `clarify` | +0 | no (gratis, pero sin valor aquí) |
| `todo` | +1 709 | no |
| `code_execution` | +1 834 | **sí** |
| `web` | +1 834 | **sí** |
| `browser` | +1 815 | no |
| `delegation` | +2 122 | no |
| `kanban` | +2 260 | no |
| `session_search` | +2 392 | no |
| `terminal` | +2 511 | **sí** |
| `file` | +2 675 | **sí** |
| `connections` | +3 006 | no |
| `cronjob` | +3 373 | no |
| `memory` | +4 365 | no |
| `skills` | +4 529 | **sí** (índice recortado) |

Los dos mayores eran `memory` (inyecta `MEMORY.md`, `USER.md` y los hechos) y
`skills` (índice de 157 skills, 8 651 caracteres ≈ 2 400 tok). En `v100` la memoria
está apagada a propósito: ese perfil no carga las notas del homelab. Si alguna vez
las necesitas, se copian a `~/.hermes/profiles/v100/memories/` y se habilita el
toolset; cuesta ~4,3k tokens por turno.

## Estado aplicado

```bash
hermes -p v100 tools disable browser memory session_search todo kanban delegation \
  cronjob context_engine connections clarify
hermes -p v100 tools enable terminal file code_execution web skills
hermes -p v100 config set model.default ninfer
hermes -p v100 config set model.aliases.ninfer.context_length 32768
```

Skills: 151 deshabilitadas en `skills.disabled`; quedan en el índice
`hermes-agent`, `ninfer-inference-ops`, `local-llm-vram-budgeting`,
`strata-inference-ops`, `llama-cpp` y `homelab-inference-mesh`. Con el índice vacío el
bloque del prompt baja de 8 651 a 1 266 caracteres.

`context_length` 262 144 → 32 768: no cambia el prefill por token, pero el KV de un
contexto de 256k no cabe con holgura en 32 GB de VRAM y cualquier turno largo arrastra
minutos de prefill. 32k es el mismo límite que usa el servidor (`--max-context`).

## Activar una skill bajo demanda

`skill_view` resuelve nombres ignorando el filtro `skills.disabled`, así que una skill
deshabilitada se puede cargar nombrándola ("carga la skill `seafile-swarm-admin`"). Lo
que no funciona es `--skills <nombre>` con la skill deshabilitada: aborta con
`unknown_skill` antes de arrancar.

Para que una skill vuelva al índice de forma permanente:

```bash
hermes -p v100 config get skills.disabled          # lista actual
hermes -p v100 skills config                       # menú interactivo enable/disable
```

Y para desactivar otra sin tocar las demás, usa `hermes -p v100 skills config`. No
edites `skills.disabled` a mano salvo con `hermes config set`, que valida la lista.

## Servidor ninfer para este perfil

El perfil manda prompts de ~8k tokens; con 32k de contexto y una sola conversación no
hay razón para reservar 256k de KV ni dos slots. Preset recomendado:

```bash
./ninfer-serve.sh <modelo> 32768 1 3     # ctx, concurrencia, draft tokens
```

- `--max-context 32768`: mismo valor que `model.aliases.ninfer.context_length`.
- `--max-concurrency 1`: el decodificador especulativo atiende un stream; con dos
  peticiones simultáneas no hay ganancia y se reparte el ancho de banda.
- `--draft-tokens 3`: probado con aceptación alta; bajar a 2 solo si la aceptación cae.
- El primer turno tras arrancar paga la carga del artefacto: un `curl` de calentamiento
  a `/v1/models` antes de abrir el chat evita confundir esa espera con lentitud.

## Arranque y comprobación

```bash
docker compose --profile ninfer up -d ninfer-server
./ninfer-serve.sh
curl --noproxy '*' -fsS http://127.0.0.1:8090/v1/models
v100 chat                                   # perfil con ninfer por defecto
v100 -m deepseek-v4.1-flash:1017 chat        # si la V100 no está enumerada
```

Si la eGPU no aparece (`nvidia-smi` no comunica, `NVRM: No NVIDIA GPU found`,
`failed to stat CDI host device "/dev/nvidia-uvm"`), no arranques el perfil `ninfer`:
`docker compose up -d` aborta con el resto del stack. El servicio está en ese perfil
justo por eso.
