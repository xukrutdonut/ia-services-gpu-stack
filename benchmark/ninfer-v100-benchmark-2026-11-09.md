# NInfer V100 — benchmark y configuración (2026-11-09)

Hardware: V100 PCIe 32GB (Khazad-dum), container `ninfer-serve` (imagen `ninfer-v100:r1`),
motor compilado en `/src/build-v100/apps/`. Servicios: endpoint OpenAI-compat en `:8090`,
consumido por LiteLLM/Open WebUI con id `ninfer`.

Artefactos (HuggingFace `neroued/*`), disponibles en el registro local `.101:5555`:

| Artefacto | Tamaño | Formato | sha256 (prefijo) |
|---|---|---|---|
| `qwen3_6_35b_a3b_v2.ninfer` (**producción**) | 22 GiB | A3B MoE groupwise-int | `1fb9ea0b...` |
| `qwen3_8_27b_nvfp4.ninfer` (ELIMINADO: re-descargable de HF) | 23 GiB | nvfp4 + torre visión | `1fb9... ver sha256.txt` |

## Resultados CLI (greedy, prompt real en español, 4096 ctx / 128 new tokens)

| Modelo | Config | Prefill | Decode | Overall |
|---|---|---|---|---|
| 35B a3b | bf16 KV, sin MTP | 284.8 | 135.0 | 125.7 |
| 35B a3b | bf16 + MTP K=2 | 297.1 | **146.2** | 135.8 |
| 27B nvfp4 | bf16, sin MTP | 184.4 | 29.4 | 27.5 |
| 27B nvfp4 | bf16 + MTP K=2 | 178.5 | 54.9 | 47.9 |
| 27B nvfp4 | int8 + MTP K=1 | 179.1 | 50.0 | 44.2 |
| 27B nvfp4 | int8 + lm-head K=2 | 178.8 | 59.8 | 51.6 |
| 27B nvfp4 | **int8 + lm-head K=3 (óptimo)** | 178.3 | **68.1** | 57.6 |
| 27B nvfp4 | int8 + lm-head K=5/6 | 175-177 | 66.3-66.8 | — |
| 27B nvfp4 | int8 + DFlash2 K=7 | 179.8 | 64.2 (acept. 36.6%) | 54.9 |

Aceptación MTP en texto real (27B): 87% K=1 / 67% K=2 / 60% K=3 — muy lejos del 99% del
corpus sintético que publican los READMEs (`218 tok/s` de https://github.com/geoffwatts/ninfer-v100
es ceiling de continuación determinista; ver Reddit r/LocalLLM 1wdn4om, donde el propio autor
publica sweep honesto: 67.7 tok/s MTP K=3 en contexto 2K — coincide con nuestra medición de 68.1).

## Fork JimmyMax — offload de expertos a host RAM (2026-11-10)

Probado `JimmyMax/ninfer-v100` (compilado sm_70 con CUDA 12.8, imagen `ninfer-jimmy:bench`,
fuentes en `~/workspace/ninfer-jimmy`, desechable). Artefacto Q2_0 39 GiB + tabla n-gram 27 GiB
(HF `WaveCut/Qwen3.8-Flash-Next-*Q2_0*` y `*ngram-table*`, sha256 verificados).

| Config Q2_0 (--expert-residency host, int8 KV, MTP 3, ctx 8192) | decode |
|---|---|
| CLI disparo frío | 18.4 tok/s (caché 86.8% hits, transfers 28 GB) |
| serve, petición tibia | 58.8 |
| serve, petición estable | **65.6 tok/s** (TTFT 42 ms, prefill 122) |

1. El offload funciona y reproducimos su cifra publicada (69.6 en corpus de código).
2. **No sustituye a Strata** (IQ3_S 3.5 bits, 32K ctx, 99.5% hits: 54.7 sostenido / 81 ráfaga):
   mismo partido, peor cuantización (2.4 bits), y no permite "ambos mundos" — la RAM solo
   aloja una pila de expertos (~65 GB de 91) y la VRAM no da para 35B (29 GB) + caché de
   expertos (25.3 GB). Siempre toca alternar.
3. La carga inicial desde el disco SATA va a ~370 MB/s (1m43); primer turno siempre ~6-19 tok/s
   por los misses fríos.
4. Desechable: `rm -rf ~/workspace/ninfer-jimmy` libera ~66 GB (y `docker rmi ninfer-jimmy:bench`).

## Conclusiones

1. **35B-A3B gana en todo**: 135-146 tok/s decode vs 68 máximo del 27B. Producción = 35B.
2. El 27B solo aporta calidad en GPQA (90.91%), no velocidad. Config óptima si se usa:
   `--kv-dtype int8 --spec mtp --lm-head-draft --draft-tokens 3`.
3. Sin ofloading de expertos a RAM: el motor no lo soporta y no hace falta (el MoE cabría
   íntegro en VRAM). El offload de pesos existe solo en el fork JimmyMax (GGUF Flash-Next).
4. Forks revisados: `tuxKOH/ninfer-v100xN` (multi-GPU TP2/TP4, kernels sm70 +38% — lo único
   portable, requiere port), `b-iurea/ninfer-v100-turbo` (FP16 activation staging, +18%
   sintético). Ninguno adoptado.

## Config actual de producción

`ninfer-serve.sh` (defecto tras el benchmark):

    ninfer-serve /models/qwen3_6_35b_a3b_v2.ninfer --model-id ninfer \
      --host 0.0.0.0 --port 8090 --max-context 262144 --max-concurrency 2 \
      --default-max-tokens 8192 --kv-dtype bf16 --prefill-chunk 2048 \
      --spec mtp --draft-tokens 2 --vision --media-cache-mib 1024 --media-live-mib 2048

Salud: `curl http://127.0.0.1:8090/health` → `{"status":"ok"}`; engine ready ~1 min,
weights 20.7 GiB, KV 262144 tok en 8.49 GiB. Multimodal con `--vision` (~330 MiB extra).

Imagen Docker guardada en el registro local con la config actual: `192.168.0.101:5555/ninfer-v100:r2-35b`
(base `r1` + binarios de `build-v100/apps`; los pesos NO van dentro de la imagen, se pasan por
el bind mount `/models`).
