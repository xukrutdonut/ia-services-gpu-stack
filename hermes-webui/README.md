# Hermes WebUI integrado

## Arranque habitual y qué se inicializa

Desde la raíz de `ia-services-stack`:

```bash
cp .env.example .env     # solo primera instalación; no sobrescribir .env existente
# Ajustar HERMES_UID/GID a id -u / id -g y las rutas absolutas si corresponde.
mkdir -p "$HOME/.hermes" "$HOME/workspace"
docker compose config -q
docker compose up -d
curl --noproxy '*' -fsS http://127.0.0.1:8787/health
```

`docker compose up -d hermes-webui` permite arrancar solo esta interfaz y su
inicializador sin encender los demás servicios. No requiere un Compose hermano,
`start.sh`, ni una instancia del gateway/API de Hermes.

El repositorio no tiene `.gitmodules` ni lanzador recursivo propio. Su punto de
entrada documentado es el Compose raíz. Aquí «recursivo» significa que ese mismo
arranque obtiene el código del agente y sus posibles submódulos, y después inicia
la WebUI: `hermes-agent-init` clona el agente en `hermes-agent-src`, comprueba el
commit fijado y ejecuta `git submodule update --init --recursive`. La WebUI espera
su finalización correcta (`service_completed_successfully`); su imagen instala
las dependencias Python del agente y ejecuta el chat **in-process**. Una fuente
incompleta no debe confundirse con una UI sana: comprobar también onboarding.

La imagen y el SHA del agente están fijados en Compose y `.env.example`. Para
actualizar, cambiar ambos deliberadamente y recrear los contenedores (`up -d
--force-recreate hermes-webui` incluye init). Nunca modifica el checkout del
agente en el host. El init rechaza cambios locales en su volumen en vez de
borrarlos. Repetir el arranque no vuelve a clonar un checkout ya fijado.

## Persistencia, red y proveedor

- `HERMES_HOME` (por defecto `${HOME}/.hermes`) se monta en
  `/home/hermeswebui/.hermes`: `config.yaml`, `.env` del agente, credenciales,
  memoria, skills y sesiones. Estado propio de la UI: subdirectorio `webui/`.
  No copiar secretos al repositorio. No se reescribe una configuración existente.
- `hermes-agent-src` es un volumen de código separado, de solo lectura para la UI.
  Su montaje anidado oculta el checkout del host dentro del contenedor sin
  alterarlo. No usar `down -v` para mantenimiento ordinario.
- `HERMES_WORKSPACE` se monta como `/workspace` y en su ruta absoluta original
  para que `terminal.cwd` del CLI siga siendo válido. Solo esas rutas son visibles;
  otras rutas, claves SSH o ejecutables del host no aparecen automáticamente.
- UID/GID deben coincidir con el dueño de los binds. El entrypoint oficial hace
  preparación de permisos como root y luego ejecuta la UI sin privilegios.
  `HERMES_SKIP_CHMOD=1` evita cambiar los modos de credenciales compartidas; no
  deshabilita el ajuste de ownership del entrypoint.
- Linux con `network_mode: host`: sin mapeo `ports` ni DNS del bridge. La UI escucha
  **127.0.0.1:8787** por defecto. Así `127.0.0.1:8006/v1` alcanza OVMS del stack,
  `127.0.0.1:8090/v1` alcanza ninfer externo y las URLs MCP publicadas alcanzan sus
  servicios. Para proveedores en otro nodo usar su URL LAN real.
- Copiar/adaptar `config.example.yaml` a `$HERMES_HOME/config.yaml` **solo si no
  existe**. Configura OVMS directamente; el ID debe coincidir con `/v1/models`.
  Alternativamente configurar el proveedor/modelo con Hermes o en Settings →
  Providers. Si requiere credencial, introducirla localmente en el home de Hermes.
  El `.env` raíz de Compose no es el `.env` del agente y no inyecta claves del LLM.
- Los MCP del ejemplo son opcionales para chat. Open Terminal no se configura
  como MCP (es una API REST); no se necesita para el backend `terminal: local`.
  No hay dependencia de OpenWebUI, LiteLLM, `HERMES_API_URL` ni un puerto gateway.
  Un modelo externo no tiene `depends_on`; debe estar disponible al conversar.

## Requisitos e impedimentos explícitos

Docker Engine Linux + Compose v2 (no `docker stack deploy`: no ejecuta init ni
sus condiciones de dependencia). Internet al primer inicio: GHCR, GitHub, PyPI,
Astral/descarga Python; la imagen crea un venv e instala dependencias en runtime.
El primer inicio puede tardar varios minutos; la recreación puede reinstalarlas.
Necesita disco/RAM para dependencias, permisos de escritura en home/workspace,
puerto libre y un proveedor/modelo funcional. No necesita GPU dentro del
contenedor WebUI; cada backend tiene sus requisitos propios. Sin modelo o
credenciales válidos puede abrir onboarding pero **no** se garantiza chat.

Para acceso LAN, fijar `HERMES_WEBUI_HOST=0.0.0.0` y contraseña fuerte localmente;
usar TLS/reverse proxy o túnel SSH. La UI permite ejecutar herramientas: no
exponer sin protección. Linux host networking no es portable a Swarm ni a Docker
Desktop sin soporte explícito de host networking.

Ya existe una instancia independiente en `../hermes-webui` usando 8787. Antes
de trasladar producción, detener **solo** aquel servicio con su Compose (sin `-v`)
y arrancar el nuevo. Nunca ejecutar dos UI sobre el mismo home/puerto. Esta
integración no detiene esa instancia automáticamente porque ejecuta las tareas
actuales. El viejo `docker-autostart.service` en el repositorio padre apunta a
un script ausente y no está instalado: no constituye el arranque de este stack.

## Comprobaciones

```bash
python3 -m unittest discover -s hermes-webui -p 'test_*.py' -v
bash -n hermes-webui/init-agent.sh
docker compose --env-file .env.example config -q
docker compose ps -a hermes-agent-init hermes-webui
curl --noproxy '*' -fsS http://127.0.0.1:8787/health
# Sin contraseña solo para prueba local; autenticarse normalmente si está activada.
curl --noproxy '*' -fsS http://127.0.0.1:8787/api/onboarding/status
```

El test usa repositorios Git locales reales con un submódulo, comprueba su
inicialización recursiva y repite el arranque con el origen desconectado. No
requiere red, Docker ni credenciales. Para pruebas Docker usar un proyecto
`-p` distinto, home/workspace aislados y otro puerto; nunca probar un onboarding
limpio sobre el home real.

Referencias: [Hermes Agent](https://hermes-agent.nousresearch.com/docs),
[WebUI upstream](https://github.com/nesquena/hermes-webui),
[Compose upstream](https://github.com/nesquena/hermes-webui/blob/master/docker-compose.yml).
Esta WebUI es la interfaz comunitaria nesquena, no el dashboard administrativo
integrado de Hermes Agent.
