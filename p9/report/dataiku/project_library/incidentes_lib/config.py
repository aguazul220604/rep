# -*- coding: utf-8 -*-
"""Nombres y parametros compartidos entre la webapp y el Flow."""

APP_TITLE = "Banamex | Reporte de Incidentes"

# Objetos de Dataiku. Ajustar aqui si el proyecto usa otros nombres.
UPLOAD_FOLDER_ID = "webapp_incidentes_uploads"
OUTPUT_DATASET = "incidentes_limpios"
STATUS_DATASET = "incidentes_carga_estado"

# La webapp ejecuta directamente la recipe del Flow mediante un job de Dataiku.
# No se usa un escenario.
BUILD_JOB_TYPE = "NON_RECURSIVE_FORCED_BUILD"

UPLOAD_PREFIX = "/uploads"
LATEST_POINTER = "/latest.json"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
POLL_INTERVAL_MS = 2000
JOB_TIMEOUT_SECONDS = 900

# Opcional: conservar solo las ultimas N cargas.
KEEP_UPLOADS = 5
