# Retirada de OpenWebUI y LiteLLM — 2026-11-11

## Cambios aplicados

- Eliminados los dos servicios, `depends_on`, healthchecks, puertos y variables
  propias del Compose. Ningún perfil los levanta. Eliminados YAML de LiteLLM,
  backups de Compose arrancables y herramienta `mesh_delegate.py` de OpenWebUI.
- `mesh-orchestrator` llama a proveedores directos con el ID real del modelo:
  [rutas y limitaciones](../MESH.md). No envía la antigua clave del gateway.
- Contenedores antiguos: restart desactivado, detenidos y eliminados **sin `-v`**.
  El volumen `ia-services-stack_open-webui-data` sigue existiendo. No se han borrado
  modelos, bases de datos de herramientas, chats ni volúmenes compartidos.
- `.env` local: retiradas solo claves legacy; rollback protegido `0600` en
  `$TMPDIR/ia-services-stack-retired-env.backup`. Es temporal: trasladarlo a un
  almacén de secretos/backup privado si se necesita conservarlo. No subirlo a Git.
- Proxy NPM: host 78 de OpenWebUI desactivado (`enabled=0`) en la BD y configuración
  retirada de `*.conf`; nginx validado y recargado. Backup dentro del volumen NPM:
  `/data/retired-openwebui-20261111/{database.sqlite,proxy-host-78.json,78.conf}`.
  Config original también en `/data/nginx/proxy_host/78.conf.disabled-openwebui`.
  Certificado conservado; no se han borrado entradas DNS ni el wildcard compartido.
  El dominio retirado deja de servir OpenWebUI; no se redirige automáticamente a
  Hermes ni se conserva como endpoint compatible. Las rutas Hermes no se tocaron.
- Integración Hermes conservada y entregada por t_d1f1c7c9 en el mismo commit
  coordinado. Su instancia activa independiente no se reinició; la migración del
  arranque completo corresponde a t_37849dc3. [Evidencias](../hermes-webui/VERIFICATION.md).

Se conservan Open Terminal, búsqueda/MCP, OVMS, orquestador y sus datos. La imagen
Open Terminal procede de `ghcr.io/open-webui/open-terminal`: es otro producto,
no el servidor OpenWebUI. Su paquete Python `litellm` es un SDK compartido del
entorno de programación; no inicia ni conecta por sí mismo al gateway retirado.
No se elimina ni se reconstruye esta herramienta compartida innecesariamente.

El fichero local **no versionado** `openwebui/tools/pubmed_medical_deep_research.py`
preexistía y no se ha borrado ni incluido en el commit. Es una herramienta manual
para la aplicación retirada, no un servicio/mount/script del arranque actual.
Conservarlo como material del usuario no lo convierte en integración activa.
La documentación previa está en `docs/history/`, marcada no operativa; no ejecutar
sus comandos. Los benchmarks describen entornos pasados, no proveedores actuales.

## Clientes externos

Los clientes que antes usaban el gateway deben cambiar **URL y modelo**, no solo
la URL. Elegir un proveedor de [MESH.md](../MESH.md), consultar su `/v1/models` y
probar inferencia. No reutilizar aliases como `worker` fuera del orquestador, ni
reutilizar la clave anterior como si autenticara OVMS. No se han modificado otras
aplicaciones del homelab ajenas a este stack. Las rutas proxy del stack ya no
apuntan a los servicios retirados; el DNS wildcard compartido se conserva.

## Evidencia de esta retirada

- `docker compose config -q`: válido; renderizado con todos los perfiles sin los
  servicios retirados, todas las dependencias resueltas.
- `python3 -m unittest discover -s tests -v`: 6 pruebas OK.
- `DATA_DIR="$TMPDIR/mesh-removal-tests" python3 -m unittest discover -s mesh-orchestrator -v`:
  4 pruebas OK, incluido transporte directo sin clave de gateway.
- `python3 -m unittest discover -s hermes-webui -p 'test_*.py' -v`: 2 pruebas OK.
- Orquestador reconstruido/recreado selectivamente con `--no-deps`; healthy.
- Job real `40973bb7d784`, alias `worker`: tarea `retirement-check` terminada,
  respuesta `OK`, sin error, 1,1 s y 88 tokens reportados. No se borraron otros jobs.
- Sin contenedores `open-webui`/`litellm`; volumen antiguo confirmado presente.
- No unidades `.service` locales referenciando los nombres retirados.

La validación completa desde instalación limpia y la migración de la instancia
Hermes activa quedan en t_37849dc3; no se afirma haberlas realizado aquí.

## Limpieza opcional de datos (NO ejecutada)

Primero exportar chats/usuarios/adjuntos que se quieran conservar o guardar una
copia del volumen. El formato de OpenWebUI no se importa automáticamente en Hermes.
No usar `docker volume prune` ni `docker compose down -v`: afectan a otros datos.

```bash
# Inspección sin borrar:
docker volume inspect ia-services-stack_open-webui-data
docker ps -a --filter volume=ia-services-stack_open-webui-data
# Copia en un directorio privado; requiere imagen alpine descargable:
mkdir -p "$HOME/backups/openwebui-retired"
chmod 700 "$HOME/backups/openwebui-retired"
docker run --rm --network none \
  -v ia-services-stack_open-webui-data:/source:ro \
  -v "$HOME/backups/openwebui-retired:/backup" alpine \
  sh -c 'umask 077; tar czf /backup/openwebui-data.tar.gz -C /source .'
# Verificar archivo e intentar restaurarlo en un volumen NUEVO antes de borrar.
```

**Solo tras verificar la copia y decidir perder esos datos en el volumen original:**
`docker volume rm ia-services-stack_open-webui-data`. Esto destruye usuarios,
conversaciones, adjuntos y configuración guardados allí. No forma parte del arranque
ni de esta retirada automática. Eliminar imágenes antiguas es también opcional;
no usar prune global. Mantener los backups de proxy/secretos privados.

Rollback operativo exigiría recuperar definiciones antiguas desde Git y revisar
versiones/puertos antes de arrancar; no restaurar Compose completo encima de Hermes.
Para el proxy basta restaurar **solo** la fila 78 desde su JSON y su `.conf`, validar
nginx y recargar; no reemplazar toda la BD, pues perdería cambios de otros hosts.
