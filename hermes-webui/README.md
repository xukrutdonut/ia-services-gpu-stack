# Hermes WebUI integrado

La [guía operativa única](../docs/migracion-hermes-webui.md) contiene requisitos,
instalación limpia, configuración, red, persistencia, segundo arranque, migración de
la instancia independiente y tratamiento no destructivo de los datos antiguos.

## Detalles de esta integración

- `init-agent.sh` exige un SHA completo, inicializa el agente y sus submódulos en un
  volumen dedicado y no modifica checkouts del host ni reemplaza cambios locales.
- Compose espera `service_completed_successfully` antes de iniciar la WebUI. La UI
  monta el agente como solo lectura y lo ejecuta in-process.
- `config.example.yaml` es solo una plantilla para instalaciones nuevas: configura
  OVMS directo y MCP opcionales. No debe copiarse sobre una configuración existente.
- No hay dependencia de OpenWebUI, LiteLLM, un puerto gateway ni `HERMES_API_URL`.
  Esta es la interfaz comunitaria nesquena, no el dashboard administrativo de Hermes
  Agent.

## Pruebas rápidas sin iniciar contenedores

Desde la raíz del repositorio:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s hermes-webui -p 'test_*.py' -v
bash -n hermes-webui/init-agent.sh
docker compose --env-file .env.example config -q
```

El test de init usa repositorios Git locales reales con un submódulo y repite el
arranque con el origen desconectado; no necesita red, Docker ni credenciales. Los
tests de Compose comprueban la retirada de gateways y las condiciones de arranque,
pin, persistencia y modo in-process de Hermes.

Las evidencias Docker, incluidos los límites de cada prueba, están en
[VERIFICATION.md](VERIFICATION.md). Los ensayos limpios deben usar otro proyecto de
Compose, home/workspace aislados, un puerto libre y rutas literales. Las variables
exportadas en el shell prevalecen sobre `.env`: pasar los valores del ensayo inline
(o eliminar las variables heredadas) y comprobar los mounts reales con
`docker compose config` **antes** de arrancar. Nunca sembrar archivos mediante un
`$HERMES_HOME` ambiente, que normalmente apunta al home real. No detener la instancia
que esté ejecutando la tarea actual.

Referencias: [Hermes Agent](https://hermes-agent.nousresearch.com/docs) ·
[WebUI upstream](https://github.com/nesquena/hermes-webui) ·
[Compose upstream](https://github.com/nesquena/hermes-webui/blob/master/docker-compose.yml).
