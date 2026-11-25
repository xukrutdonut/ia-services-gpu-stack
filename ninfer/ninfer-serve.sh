#!/bin/sh
# Compatibilidad: el motor pertenece al servicio Compose, no a docker exec.
set -eu
ROUTER=/home/arkantu/produccion/ia-services-stack/ninfer/ninfer-router.sh
case "${1:-start}" in
  stop) exec "$ROUTER" stop ;;
  start|restart) exec "$ROUTER" "${1:-start}" ;;
  *)
    echo 'La selección por archivo en ninfer-serve.sh ya no está disponible.' >&2
    echo 'Configura el modelo en ninfer/ninfer-router.ini y reinicia con ninfer/ninfer-router.sh restart.' >&2
    exit 2
    ;;
esac
