# Inferencia directa

No hay gateway central ni orquestador. Cada cliente configura el proveedor
OpenAI-compatible, su **ID real** de modelo y autenticación solo si ese proveedor
la requiere. Hermes usa la configuración de su home: [integración](hermes-webui/README.md).

## Proveedores

| Proveedor | Base desde el host | Modelos publicados por `/v1/models` |
|---|---|---|
| OVMS del stack | `http://127.0.0.1:8006/v1` | `Qwen3-30B-A3B-Instruct-2507-int4-ov`, `qwen3-embedding-0.6b-int4-ov`, `nomic-embed-text-v1.5-ov` |
| ninfer (V100, en el stack) | `http://127.0.0.1:8090/v1` | IDs configurados en `ninfer/ninfer-router.ini` |
| Hailo externo | `http://192.168.0.80:8089/v1` | `ocr-hailo-8l`, `clip-resnet-50x4-hailo` |
| NPU externa | `http://127.0.0.1:8012/v1` | `qwen3-embedding-0.6b-int4-ov` |

OVMS responde sin autenticación. Desde el bridge Compose se alcanza por
`http://ia-gpu-intel-openvino:8000/v1`; en clientes con red host, por la URL del
host. No intercambiar índices NPU/OVMS: usan pooling diferente aunque el nombre
del modelo coincida. CLIP es imagen-imagen, no embeddings de texto.

Comprobar siempre `/v1/models` **y** una generación real: el listado no prueba
inferencia.

## ninfer en la V100 (`:8090`)

`ninfer-server` pertenece al Compose principal y ejecuta el router como proceso
principal. Monta el preset desde `ninfer/ninfer-router.ini` y los modelos desde
`/mnt/data/modelos/ninfer`. Tras reiniciar el contenedor el motor vuelve a arrancar;
una unidad de usuario reintenta el arranque si DATA o Docker aún no están listos.
El modelo se carga bajo demanda. El servicio requiere la GPU CDI: si la eGPU no
está enumerada, el `up` del stack puede fallar al iniciarlo.

```bash
docker compose up -d ninfer-server
./ninfer/ninfer-mgr.sh status
./ninfer/ninfer-mgr.sh load qwen3.8-35b-a3b
curl --fail http://127.0.0.1:8090/v1/models
```

Para aplicar cambios al preset: `./ninfer/ninfer-router.sh restart`.

La [migración](docs/retirada-openwebui-litellm.md) explica la retirada de
contenedores, datos y clientes externos. Las mediciones y procedimientos previos
se preservan en el [archivo histórico no operativo](docs/history/inference-before-hermes.md).
