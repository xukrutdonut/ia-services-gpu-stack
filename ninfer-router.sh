#!/bin/sh
# ninfer-router.sh: relanza ninfer en modo ROUTER sobre la V100 (:8090).
# Sirve todos los artefactos del preset ninfer-router.ini; cada uno es elegible
# por su id en /v1/chat/completions (model field) y por tanto desde el desplegable
# de Hermes (discover_models). --models-max 1: al pedir un modelo no cargado,
# el LRU se DESCARGA (no suspend — gemma no soporta suspend) y carga el nuevo;
# el swap tarda 45-70s.
#
# Uso: ./ninfer-router.sh [stop]
set -eu
INI=/home/arkantu/produccion/ia-services-stack/ninfer-router.ini
BIN=/build/apps/ninfer-serve

# Modo stop: mismo TERM/KILL anclado que ninfer-serve.sh
if [ "${1:-}" = stop ]; then
  docker exec ninfer-serve sh -c "for p in \$(pgrep -f '^/build/apps/ninfer-serve'); do kill \"\$p\" 2>/dev/null; done
    for i in \$(seq 1 10); do pgrep -f '^/build/apps/ninfer-serve' >/dev/null || break; sleep 1; done
    for p in \$(pgrep -f '^/build/apps/ninfer-serve'); do kill -9 \"\$p\" 2>/dev/null; done; true" || true
  echo "ninfer: router detenido"
  exit 0
fi

docker cp "$INI" ninfer-serve:/tmp/ninfer-router.ini
docker exec ninfer-serve sh -c "for p in \$(pgrep -f '^$BIN'); do kill \"\$p\" 2>/dev/null; done
  for i in \$(seq 1 30); do pgrep -f '^$BIN' >/dev/null || break; sleep 1; done
  for p in \$(pgrep -f '^$BIN'); do kill -9 \"\$p\" 2>/dev/null; done; true"
docker exec -d ninfer-serve sh -c "setsid $BIN --models-preset /tmp/ninfer-router.ini \
  --host 0.0.0.0 --port 8090 --models-max 1 \
  > /tmp/serve.log 2>&1 < /dev/null"
echo "ninfer: router subido -> :8090 (preset 3 modelos, models-max 1, swap por descarga)"
