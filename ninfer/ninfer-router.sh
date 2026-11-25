#!/bin/sh
# Compatibilidad con la unidad systemd y los comandos anteriores.
# El router es ahora el proceso principal del servicio Compose.
set -eu
cd /home/arkantu/produccion/ia-services-stack
case "${1:-start}" in
  stop)
    exec docker compose stop ninfer-server
    ;;
  start)
    exec docker compose up -d --no-deps ninfer-server
    ;;
  restart)
    # Recreate para que cambios atómicos en el preset montado se vean.
    exec docker compose up -d --no-deps --force-recreate ninfer-server
    ;;
  *)
    echo "Uso: $0 [start|restart|stop]" >&2
    exit 2
    ;;
esac
