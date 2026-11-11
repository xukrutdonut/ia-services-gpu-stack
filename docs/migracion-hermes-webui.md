# Instalación, arranque y migración a Hermes WebUI

Esta es la guía operativa de `ia-services-stack`. Para puertos, imágenes, mounts y
perfiles concretos prevalece `docker-compose.yml`. Las pruebas y sus resultados se
conservan aparte en [VERIFICATION.md](../hermes-webui/VERIFICATION.md); la retirada
histórica de los gateways está en [retirada-openwebui-litellm.md](retirada-openwebui-litellm.md).

## Arquitectura actual

`docker compose up -d` en la raíz inicia `hermes-agent-init`, espera su salida
correcta (`service_completed_successfully`) y después arranca `hermes-webui`.
El inicializador clona Hermes Agent en el volumen `hermes-agent-src`, comprueba el
commit completo fijado y actualiza sus submódulos recursivamente. La interfaz monta
ese checkout como solo lectura y ejecuta el agente **in-process**: no necesita un
Compose hermano, `start.sh`, un gateway Hermes, OpenWebUI ni LiteLLM.

La imagen `ghcr.io/open-webui/open-terminal` de Open Terminal es una herramienta
distinta del servidor OpenWebUI; conservarla no reactiva el servicio retirado. El
orquestador usa proveedores OpenAI directos, documentados en [MESH.md](../MESH.md).

## Instalación limpia

Requisitos: Docker Engine Linux y Compose v2; no usar `docker stack deploy`. El
primer inicio necesita acceso a GHCR, GitHub y las descargas de Python/PyPI/Astral,
puede tardar varios minutos y requiere espacio para las dependencias. Cada proveedor
de inferencia conserva sus propios requisitos.

Para las rutas predeterminadas del ejemplo:

```bash
cd ~/produccion/ia-services-stack
cp -n .env.example .env
# Editar .env: UID/GID, rutas, puerto y credenciales locales necesarias.
install -d -m 700 "$HOME/.hermes"
install -d "$HOME/workspace"
# Solo crea la configuración si no existe; nunca reemplaza la del usuario.
cp -n hermes-webui/config.example.yaml "$HOME/.hermes/config.yaml"
docker compose config -q
docker compose up -d --build
```

Ajustar `HERMES_UID`/`HERMES_GID` al dueño de los binds (`id -u`, `id -g`). Si se
personalizan `HERMES_HOME` o `HERMES_WORKSPACE`, crear esas rutas absolutas en vez
de las predeterminadas y copiar allí la configuración inicial. `HERMES_AGENT_REF`
debe ser un SHA completo; no usar ramas flotantes. Actualizar imagen y agente es un
cambio deliberado seguido de `docker compose up -d --force-recreate hermes-webui`.

OVMS consume el directorio de modelos montado en el servicio
`ia-gpu-intel-openvino` del Compose; no lo descarga este arranque. Los demás binds
externos indicados en el archivo deben existir y tener permisos adecuados. El token
Moodle real es necesario solo para usar ese MCP; no bloquea el chat de Hermes.

## Configuración, red y persistencia

- `HERMES_HOME` se monta en `/home/hermeswebui/.hermes`: conserva configuración,
  credenciales, memorias, skills, sesiones y estado WebUI (`webui/`). El `.env` de
  Compose no es el `.env` del agente ni inyecta automáticamente claves de LLM.
- `HERMES_WORKSPACE` aparece como `/workspace` y en su ruta absoluta original para
  mantener válido `terminal.cwd`. Otros directorios y claves SSH del host no se
  comparten automáticamente.
- `hermes-agent-src` separa el código del home. El init no toca un checkout del
  host y rechaza cambios locales en su volumen en vez de borrarlos. No usar
  `docker compose down -v` ni `docker volume prune` como mantenimiento.
- El entrypoint de la imagen prepara permisos como root y ejecuta la UI sin ellos.
  `HERMES_SKIP_CHMOD=1` conserva los modos de credenciales, pero no impide todos
  los ajustes de ownership.
- En Linux, `network_mode: host` permite usar los proveedores/MCP ya publicados en
  `127.0.0.1`. No usar nombres DNS del bridge desde esta UI. La UI escucha en
  `127.0.0.1:8787` por defecto y no declara mapeo `ports`.

`config.example.yaml` selecciona directamente OVMS en `http://127.0.0.1:8006/v1`.
Su `model.default` debe ser un ID de `GET /v1/models`, no un alias del orquestador.
También se puede conservar otra configuración de proveedor compatible; las
credenciales se introducen localmente en Hermes o en Settings → Providers. Los MCP
del ejemplo son opcionales para conversar y Open Terminal no es un MCP.

Para exposición LAN, configurar `HERMES_WEBUI_HOST=0.0.0.0`, una contraseña fuerte y
TLS/reverse proxy o túnel SSH. La interfaz puede ejecutar herramientas: no exponerla
sin autenticación. Host networking no es portable a Swarm ni a Docker Desktop sin
soporte explícito.

## Comprobación y segundo arranque

```bash
docker compose ps -a
curl --noproxy '*' -fsS http://127.0.0.1:8787/health
curl --noproxy '*' -fsS http://127.0.0.1:8787/api/onboarding/status
# Si la UI tiene contraseña, el último endpoint exige autenticación (HTTP 401).

python3 -m unittest discover -s tests -v
DATA_DIR="$PWD/.test-data" python3 -m unittest discover -s mesh-orchestrator -v
python3 -m unittest discover -s hermes-webui -p 'test_*.py' -v
bash -n hermes-webui/init-agent.sh
```

`/health` solo certifica la interfaz. En onboarding deben comprobarse por separado
`hermes_found`, `imports_ok`, `provider_ready` y `chat_ready`; una instalación sin
proveedor válido puede abrir la UI sin estar lista para conversar. Confirmar además
`/v1/models` y una completion corta desde el contenedor o desde la propia UI.

Repetir `docker compose up -d` es la operación normal de rearranque. Debe quedar una
sola WebUI, sana, con el init en `Exited (0)` y sin volver a clonar el checkout ya
fijado. No son necesarios comandos manuales dentro de los contenedores. Si cambia la
configuración de Compose, Compose puede recrear el contenedor afectado; el init es
un recurso one-shot y no una segunda interfaz.

## Migración desde `../hermes-webui` sin perder datos

Al 2026-11-11 la instancia activa todavía pertenece a `../hermes-webui`, utiliza la
misma ruta de home/workspace y el puerto 8787. No se cortó durante la validación
porque ejecuta las tareas actuales. El cambio de propietario es una ventana breve y
controlada; no deben coexistir ambas sobre el mismo home/puerto.

1. Esperar a que no haya conversaciones/runs activos y guardar una copia privada de
   `config.yaml` y del estado importante. No copiar secretos al repositorio.
2. Revisar el Compose antiguo y trasladar **solo** ajustes deseados: el antiguo
   publica en `0.0.0.0`; el nuevo usa loopback salvo que se configure lo contrario.
   Mantener una contraseña si el servicio es accesible desde LAN. Los binds nuevos
   ya coinciden con el home y workspace existentes; no copiar una configuración de
   ejemplo encima de la real.
3. Desde el Compose antiguo, detener únicamente esa interfaz y eliminar su
   contenedor, **sin `-v`**:

   ```bash
   docker compose -f ../hermes-webui/docker-compose.yml down
   ```

4. Inmediatamente, desde la raíz nueva:

   ```bash
   docker compose config -q
   docker compose up -d hermes-webui
   docker compose ps -a hermes-agent-init hermes-webui
   curl --noproxy '*' -fsS http://127.0.0.1:8787/health
   ```

5. Autenticarse en la UI si procede; comprobar onboarding, los modelos y una
   conversación real. El primer init descarga el checkout en un volumen nuevo y la
   instalación de dependencias puede prolongar esta ventana. No se migra ni convierte
   el historial de OpenWebUI: es un producto distinto.

   Antes de cualquier ensayo paralelo, recordar que las variables Compose exportadas
   en el shell prevalecen sobre `.env`: fijar home/workspace inline y revisar los
   mounts con `docker compose config`, o se podría montar el home productivo sin
   querer. Esto no afecta al arranque normal si no hay overrides heredados.
6. No reactivar el arranque antiguo. No hay una unidad systemd operativa que deba
   migrarse en este repositorio; el punto de entrada admitido es el Compose raíz.

Rollback ante fallo: detener solo la nueva WebUI, arrancar la antigua sin `-v` y
conservar `hermes-agent-src` para diagnóstico. No restaurar un Compose legacy
completo encima de una WebUI activa ni ejecutar ambas a la vez.

## OpenWebUI/LiteLLM: retirada no destructiva

Los servicios y sus dependencias ya no están en ningún perfil del Compose y el
orquestador no conserva sus URLs/claves. No son necesarios para Hermes, OVMS, Open
Terminal, búsqueda/MCP ni el orquestador. El proxy de OpenWebUI fue desactivado con
backup; no se redirige automáticamente a Hermes. Sus certificados/DNS compartidos no
se eliminan.

El volumen `ia-services-stack_open-webui-data` se conserva como material antiguo,
junto con los datos compartidos. Inspección y copia opcional:

```bash
docker volume inspect ia-services-stack_open-webui-data
docker ps -a --filter volume=ia-services-stack_open-webui-data
install -d -m 700 "$HOME/backups/openwebui-retired"
docker run --rm --network none \
  -v ia-services-stack_open-webui-data:/source:ro \
  -v "$HOME/backups/openwebui-retired:/backup" alpine \
  sh -c 'umask 077; tar czf /backup/openwebui-data.tar.gz -C /source .'
```

Verificar y probar la restauración en un volumen nuevo antes de cualquier borrado.
El formato de chats/usuarios/adjuntos de OpenWebUI no se importa automáticamente a
Hermes. Solo tras decidir perder el original puede ejecutarse
`docker volume rm ia-services-stack_open-webui-data`; no forma parte de la migración.
Las imágenes retiradas también son limpieza opcional, nunca mediante prune global.

## Referencias

- [Hermes Agent](https://hermes-agent.nousresearch.com/docs).
- [Hermes WebUI comunitaria nesquena](https://github.com/nesquena/hermes-webui),
  distinta del dashboard administrativo integrado de Hermes Agent.
- [Compose upstream](https://github.com/nesquena/hermes-webui/blob/master/docker-compose.yml).
