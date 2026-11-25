# NInfer en ia-services-stack

`ninfer-server` arranca el router como PID 1 y monta modelos desde `/mnt/data/modelos/ninfer`. El preset activo es `ninfer-router.ini`; para aplicar cambios usa `./ninfer/ninfer-router.sh restart`. El gestor `ninfer-mgr.sh` opera contra la API del router; solo puede cargar IDs presentes en el preset.

Los enlaces de compatibilidad `ninfer-router.ini`, `ninfer-router.sh`, `ninfer-mgr.sh` y `ninfer-serve.sh` de la raíz deben permanecer: mantienen referencias antiguas y el servicio systemd del sistema. El lanzador monomodelo antiguo ya no acepta parámetros de modelo; para cambiar el catálogo hay que editar el preset.

La unidad de usuario `ninfer-startup.service` está habilitada con linger y reintenta el arranque si Docker o DATA no están disponibles inicialmente. La unidad heredada del sistema continúa habilitada; sus rutas se conservan por compatibilidad. El arranque completo tras reboot aún no se ha probado.

## Limpieza

Los archivos obsoletos y copias previas se han retirado de este directorio a `~/.local/share/Trash/files/ia-services-stack-ninfer-20261125/`: el launcher y gestor anteriores, tres backups/bench de router, el ejemplar obsoleto de unidad systemd, el antiguo estado del gestor y el helper de arranque previo. Se conservan de forma reversible; no se ha borrado ningún modelo ni dato. El preset vigente ya estaba modificado antes de esta reorganización y conserva esos cambios.

La actualización diaria se programa por Hermes para las 03:30, con compilación candidata, comprobación de inferencia y vuelta atrás si falla.
