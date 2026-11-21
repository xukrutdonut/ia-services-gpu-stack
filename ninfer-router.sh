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
# Artefactos cargados desde RAM: recopia al /dev/shm (tmpfs del contenedor)
# si faltan o cambiaron (el tmpfs se limpia al reiniciar el CONTENEDOR, no el router).
copy_to_shm() {  # copia $1 (src en /models) -> $2 (dst en /dev/shm) si falta o difiere
  local SRC="$1" DST="$2"
  local SZ DZ; SZ=$(docker exec ninfer-serve stat -c%s "$SRC" 2>/dev/null || echo 0)
  DZ=$(docker exec ninfer-serve sh -c "test -f $DST && stat -c%s $DST" 2>/dev/null || echo 0)
  if [ -n "$SZ" ] && [ "$SZ" != "$DZ" ]; then
    echo "ninfer: copiando $(basename "$SRC") al /dev/shm (RAM) para swaps rápidos..."
    docker exec ninfer-serve cp "$SRC" "$DST"
  fi
}
# Solo gemma en RAM (produccion) para swaps rapidos; qwen se carga bajo demanda
# desde disco (swap mas lento pero libera ~22 GB de RAM / zram).
# copy_to_shm /models/qwen38-35b-a3b-distill.ninfer /dev/shm/qwen.ninfer
copy_to_shm /models/gemma-4-26B-A4B-it-QAT-Q4_0-MTP-ninfer-v3.ninfer /dev/shm/gemma.ninfer

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
