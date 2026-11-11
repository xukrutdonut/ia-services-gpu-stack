# Inferencia directa

No hay gateway central ni orquestador. Cada cliente configura el proveedor
OpenAI-compatible, su **ID real** de modelo y autenticación solo si ese proveedor
la requiere. Hermes usa la configuración de su home: [integración](hermes-webui/README.md).

## Proveedores

| Proveedor | Base desde el host | Modelos publicados por `/v1/models` |
|---|---|---|
| OVMS del stack | `http://127.0.0.1:8006/v1` | `Qwen3-30B-A3B-Instruct-2507-int4-ov`, `qwen3-embedding-0.6b-int4-ov`, `nomic-embed-text-v1.5-ov` |
| ninfer (V100, en el stack) | `http://127.0.0.1:8090/v1` | `ninfer` |
| Hailo externo | `http://192.168.0.80:8089/v1` | `ocr-hailo-8l`, `clip-resnet-50x4-hailo` |
| NPU externa | `http://127.0.0.1:8012/v1` | `qwen3-embedding-0.6b-int4-ov` |

OVMS responde sin autenticación. Desde el bridge Compose se alcanza por
`http://ia-gpu-intel-openvino:8000/v1`; en clientes con red host, por la URL del
host. No intercambiar índices NPU/OVMS: usan pooling diferente aunque el nombre
del modelo coincida. CLIP es imagen-imagen, no embeddings de texto.

Comprobar siempre `/v1/models` **y** una generación real: el listado no prueba
inferencia.

## ninfer en la V100 (`:8090`)

`ninfer-server` forma parte del Compose: mantiene el contenedor `ninfer-serve`
(imagen `ninfer-v100:bench`, GPU por CDI, `network_mode: host`, modelos y grafts
montados en solo lectura). El motor **no** arranca solo; el contenedor es el
runtime y `./ninfer-serve.sh [modelo] [ctx] [concurrencia] [draft]` lo lanza o
relanza dentro con `docker exec`. model-id fijo `ninfer`; vision incluida con
`--vision`. El motor ignora SIGTERM, por eso el script hace TERM → espera →
SIGKILL antes de relanzar.

El servicio está en el perfil `ninfer` porque la V100 es una eGPU: cuando no está
enumerada (sin `/dev/nvidia-uvm`, `NVRM: No NVIDIA GPU found`), la inyección CDI
falla y abortaría el `up -d` de todo el stack. Sin el perfil, el resto arranca igual.

```bash
docker compose --profile ninfer up -d ninfer-server   # host con la GPU presente
./ninfer-serve.sh
curl --fail http://127.0.0.1:8090/v1/models
```

La [migración](docs/retirada-openwebui-litellm.md) explica la retirada de
contenedores, datos y clientes externos. Las mediciones y procedimientos previos
se preservan en el [archivo histórico no operativo](docs/history/inference-before-hermes.md).
