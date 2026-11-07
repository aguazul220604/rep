import csv
import hashlib
import io
import os
import re
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_file
import dataiku
import pandas as pd

# ==========================================
# CONFIGURACIÓN Y CONSTANTES
# ==========================================
# Managed folders del proyecto de la webapp:
#   LAB  -> bucket dataiku-files-2 (flujo NEW/API: dataiku/<ENV>/<NICK>/<PROYECTO>/<archivo>)
#   PROD -> bucket dataiku-files   (flujo LEGACY: <PROYECTO>/project_bundles/...)
# Los IDs se pueden sobreescribir por variables de entorno si el proyecto usa otros.
S3_FOLDER_ID_LAB = os.environ.get("S3_FOLDER_ID_LAB", "LAB")
S3_FOLDER_ID_PROD = os.environ.get("S3_FOLDER_ID_PROD", "PROD")
S3_FOLDER_IDS = (S3_FOLDER_ID_LAB, S3_FOLDER_ID_PROD)
FLOW_TO_FOLDER = {
    "NEW": S3_FOLDER_ID_LAB,
    "LEGACY": S3_FOLDER_ID_PROD,
}

HISTORICAL_DATASET_NAME = "historical"
HISTORICAL_COLUMNS = [
    "id", "s3_path", "flow", "proyecto",
    "fecha_creacion_s3", "fecha_periodo_limpieza", "size",
]
VALID_ESTADOS = ("Conservado", "Descartado")
DEFAULT_CUTOFF_DAYS = 180
NEW_FLOW_EXTENSIONS = (".zip", ".gz", ".tar.gz", ".tgz")
PROJECT_BUNDLES_SEGMENT = "project_bundles"
FALLBACK_NEW_FLOW_ENVS = ("UAT", "PROD-1", "PROD", "LAB", "DEV", "QA", "DESIGN")

BUNDLE_DATE_REGEX = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:[-_](\d{2})-(\d{2})-(\d{2}))?"
)


def get_six_months_ago(cutoff_days=DEFAULT_CUTOFF_DAYS):
    # Se calcula por request para no congelar el corte en el import
    try:
        cutoff_days = int(cutoff_days)
    except (TypeError, ValueError):
        cutoff_days = DEFAULT_CUTOFF_DAYS
    if cutoff_days < 0:
        cutoff_days = 0
    return datetime.now() - timedelta(days=cutoff_days)


def get_cutoff_days_from_request():
    try:
        from flask import request as _request
        raw = _request.args.get("cutoff_days", DEFAULT_CUTOFF_DAYS)
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_CUTOFF_DAYS


def is_bundle_path(path, flow=None, is_folder_bundle=False):
    if not path:
        return False
    if is_folder_bundle:
        return True
    if flow == "NEW":
        return path.lower().endswith(NEW_FLOW_EXTENSIONS)
    if flow == "LEGACY":
        clean = path.lstrip("/").rstrip("/")
        return "/" in clean and not clean.endswith("/")
    lowered = path.lower()
    if lowered.endswith(NEW_FLOW_EXTENSIONS):
        return True
    clean = path.lstrip("/").rstrip("/")
    return "/" in clean and not clean.endswith("/")


def md5_of_path(s3_path):
    # hash MD5 de la ruta S3 (estable entre reinicios)
    return hashlib.md5(s3_path.encode("utf-8")).hexdigest()


def extract_date_from_name(name):
    """
    Intenta extraer la fecha de creación embebida en el nombre del bundle
    (ej. V_1_1_0_CHG0000377490-2025-06-30-23-12-23 o PerfAdminTest-2024-07-09-00-37-32).
    Retorna datetime o None si no contiene un patrón válido.
    """
    if not name:
        return None
    matches = list(BUNDLE_DATE_REGEX.finditer(name))
    if not matches:
        return None
    m = matches[-1]
    try:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour = int(m.group(4)) if m.group(4) is not None else 0
        minute = int(m.group(5)) if m.group(5) is not None else 0
        second = int(m.group(6)) if m.group(6) is not None else 0
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


# ==========================================
# FUNCIONES HELPER
# ==========================================
def parse_s3_path(path):
    """
    Parsear rutas de S3 y devolver un esquema homogéneo.
    Acepta tanto rutas con prefijo dataiku/ como sin él.

    NEW (LAB):     [dataiku/]{AMBIENTE}/{NICKNAME}/{PROYECTO}/{ARCHIVO}
    LEGACY (PROD): [dataiku/]{PROYECTO}/project_bundles/{BUNDLE}[/...]
    LEGACY fallback: [dataiku/]{PROYECTO}/...
    """
    if not path:
        return None

    raw_path = path
    clean_path = path.lstrip("/").rstrip("/")
    if not clean_path:
        return None

    parts = clean_path.split("/")
    has_dataiku_prefix = parts[0].lower() == "dataiku"
    offset = 1 if has_dataiku_prefix else 0
    normalized = parts[offset:]

    if not normalized:
        return None

    # 1. LEGACY (PROD): [dataiku/]{PROYECTO}/project_bundles/{BUNDLE}[/...]
    if len(normalized) >= 3 and normalized[1] == PROJECT_BUNDLES_SEGMENT:
        project = normalized[0]
        bundle_name = normalized[2]
        bundle_path = "/" + "/".join(parts[: offset + 3])
        is_folder_bundle = (
            len(normalized) > 3
            or not bundle_name.lower().endswith(NEW_FLOW_EXTENSIONS)
        )
        return {
            "flow": "LEGACY",
            "env": "PROD",
            "nickname": "LEGACY",
            "proyecto": project,
            "filename": bundle_name,
            "bundle_name": bundle_name,
            "bundle_path": bundle_path,
            "is_folder_bundle": is_folder_bundle,
        }

    # 2. NEW (LAB): [dataiku/]{AMBIENTE}/{NICKNAME}/{PROYECTO}/{ARCHIVO}
    if (
        len(normalized) >= 4
        and normalized[0].upper() in FALLBACK_NEW_FLOW_ENVS
        and normalized[-1].lower().endswith(NEW_FLOW_EXTENSIONS)
    ):
        return {
            "flow": "NEW",
            "env": normalized[0],
            "nickname": normalized[1],
            "proyecto": normalized[2],
            "filename": "/".join(normalized[3:]),
            "bundle_name": normalized[-1],
            "bundle_path": "/" + clean_path,
            "is_folder_bundle": False,
        }

    # Contenedores de project_bundles no son bundles
    if normalized[-1] == PROJECT_BUNDLES_SEGMENT:
        return None

    # Carpetas de ambiente NEW sin archivo de bundle no son bundles
    if normalized[0].upper() in FALLBACK_NEW_FLOW_ENVS:
        return None

    # 3. LEGACY fallback (PROD): [dataiku/]{PROYECTO}/...
    if len(normalized) >= 2:
        return {
            "flow": "LEGACY",
            "env": "PROD",
            "nickname": "LEGACY",
            "proyecto": normalized[0],
            "filename": "/".join(normalized[1:]),
            "bundle_name": normalized[-1],
            "bundle_path": "/" + clean_path,
            "is_folder_bundle": raw_path.rstrip("/") != raw_path,
        }

    return None


def get_folder_bundle_details(folder, bundle_path, child_paths, bundle_name):
    """
    Obtiene (creation_date, size_bytes) para un bundle basado en carpeta.
    Prioriza la fecha embebida en el nombre del bundle o el lastModified en S3,
    evitando consultar individualmente miles de archivos internos (.git/objects).
    """
    creation_date = extract_date_from_name(bundle_name)
    last_modified_ms = 0
    total_size_bytes = 0

    try:
        details = folder.get_path_details(bundle_path) or {}
        if isinstance(details, dict):
            last_modified_ms = details.get("lastModified", 0) or 0
            total_size_bytes = details.get("size", 0) or 0
            if total_size_bytes == 0 and isinstance(details.get("children"), list):
                total_size_bytes = sum(
                    (c.get("size", 0) or 0) for c in details["children"] if isinstance(c, dict)
                )
    except Exception:
        pass

    # Fallback a archivo hijo representativo si el directorio virtual en S3 no reporta fecha o tamaño
    if (creation_date is None and last_modified_ms <= 0) or total_size_bytes == 0:
        sample_paths = child_paths[:10] if child_paths else []
        sampled_size = 0
        for cp in sample_paths:
            try:
                c_details = folder.get_path_details(cp) or {}
                if isinstance(c_details, dict):
                    c_mod = c_details.get("lastModified", 0) or 0
                    if c_mod > last_modified_ms:
                        last_modified_ms = c_mod
                    sampled_size += c_details.get("size", 0) or 0
            except Exception:
                continue
        if total_size_bytes == 0 and sampled_size > 0:
            total_size_bytes = sampled_size

    if creation_date is None:
        if last_modified_ms > 0:
            creation_date = datetime.fromtimestamp(last_modified_ms / 1000.0)
        else:
            creation_date = datetime.fromtimestamp(0)

    return creation_date, total_size_bytes


def _bundle_record(meta, s3_path, creation_date, size_bytes, source_folder, filename=None):
    return {
        "id": md5_of_path(s3_path),
        "s3_path": s3_path,
        "flow": meta["flow"],
        "env": meta["env"],
        "nickname": meta["nickname"],
        "proyecto": meta["proyecto"],
        "filename": filename or meta.get("filename", ""),
        "fecha_creacion_s3": creation_date.strftime("%Y-%m-%d %H:%M:%S"),
        "size": round(size_bytes / (1024 * 1024), 2),
        "source_folder": source_folder,
    }


def _fetch_folder_bundles(folder, folder_id, paths, cutoff_date):
    bundles = []
    folder_bundles_map = {}

    for path in paths:
        meta = parse_s3_path(path)
        if not meta:
            continue

        is_folder_bundle = bool(meta.get("is_folder_bundle"))
        if not is_bundle_path(path, flow=meta.get("flow"), is_folder_bundle=is_folder_bundle):
            continue

        if is_folder_bundle:
            b_path = meta["bundle_path"]
            if b_path not in folder_bundles_map:
                folder_bundles_map[b_path] = {
                    "meta": meta,
                    "child_paths": [],
                }
            folder_bundles_map[b_path]["child_paths"].append(path)
            continue

        details = folder.get_path_details(path) or {}
        last_modified_ms = details.get("lastModified", 0) or 0
        file_size_bytes = details.get("size", 0) or 0
        creation_date = datetime.fromtimestamp(last_modified_ms / 1000.0)

        if creation_date < cutoff_date:
            bundles.append(
                _bundle_record(meta, path, creation_date, file_size_bytes, folder_id)
            )

    for b_path, info in folder_bundles_map.items():
        meta = info["meta"]
        child_paths = info["child_paths"]
        bundle_name = meta.get("bundle_name") or meta.get("filename", "")
        creation_date, total_size_bytes = get_folder_bundle_details(
            folder, b_path, child_paths, bundle_name
        )

        if creation_date < cutoff_date:
            bundles.append(
                _bundle_record(
                    meta,
                    b_path,
                    creation_date,
                    total_size_bytes,
                    folder_id,
                    filename=bundle_name,
                )
            )

    return bundles


def fetch_s3_bundles(cutoff_days=DEFAULT_CUTOFF_DAYS):
    # Escanea ambos folders administrados y aplica el parser estandarizado.
    cutoff_date = get_six_months_ago(cutoff_days)
    bundles = []
    warnings = []

    for folder_id in S3_FOLDER_IDS:
        try:
            folder = dataiku.Folder(folder_id)
            paths = folder.list_paths_in_partition()
        except Exception as e:
            warnings.append(f"No se pudo acceder al folder {folder_id}: {e}")
            continue

        try:
            bundles.extend(_fetch_folder_bundles(folder, folder_id, paths, cutoff_date))
        except Exception as e:
            warnings.append(f"Error al procesar el folder {folder_id}: {e}")

    return bundles, warnings


# ==========================================
# GESTIÓN DEL HISTORICAL
# ==========================================
def get_historical_records():
    # Leer el dataset administrado 'historical' y retorna una lista de dicts
    try:
        dataset = dataiku.Dataset(HISTORICAL_DATASET_NAME)
        df = dataset.get_dataframe()
        return df.to_dict(orient="records")
    except Exception:
        # Retornar lista vacía si el dataset aún no se ha inicializado
        return []


def save_historical_records(records):
    # Guardar o actualizar la lista de bundles preservados
    dataset = dataiku.Dataset(HISTORICAL_DATASET_NAME)
    df = pd.DataFrame(records, columns=HISTORICAL_COLUMNS)
    dataset.write_with_schema(df)


# ==========================================
# ENDPOINTS API REST
# ==========================================
@app.route("/scan-bundles", methods=["GET"])
def api_scan_bundles():
    cutoff_days = get_cutoff_days_from_request()
    s3_bundles, warnings = fetch_s3_bundles(cutoff_days=cutoff_days)
    historical = get_historical_records()
    historical_paths = {
        str(h["s3_path"]).rstrip("/") for h in historical if "s3_path" in h and h["s3_path"]
    }
    candidates = [
        b for b in s3_bundles if str(b["s3_path"]).rstrip("/") not in historical_paths
    ]

    # Agrupar por (FLOW, AMBIENTE, NICKNAME, PROYECTO)
    groups = {}
    for b in candidates:
        group_key = (b["flow"], b["env"], b["nickname"], b["proyecto"])
        if group_key not in groups:
            groups[group_key] = []
        groups[group_key].append(b)

    final_bundles = []

    for group_key, items in groups.items():
        # Ordenar por fecha real de creación; fallback a filename.
        items.sort(
            key=lambda x: (x.get("fecha_creacion_s3", ""), x.get("filename", "")),
            reverse=True,
        )

        for idx, item in enumerate(items):
            # la versión más reciente (idx == 0) queda Conservado.
            item["estado"] = "Conservado" if idx == 0 else "Descartado"
            final_bundles.append(item)

    # Criterio dinámico
    if cutoff_days == DEFAULT_CUTOFF_DAYS:
        criterio = "> 6 meses"
    elif cutoff_days == 0:
        criterio = "sin filtro (pruebas)"
    else:
        criterio = f"> {cutoff_days} días"

    return jsonify({
        "status": "success",
        "bundles": final_bundles,
        "warnings": warnings,
        "periodo_ejecucion": datetime.now().strftime("%b %Y"),
        "criterio_antiguedad": criterio,
        "cutoff_days": cutoff_days,
    })


@app.route("/get-historical", methods=["GET"])
def api_get_historical():
    """
    Obtiene exclusivamente las filas guardadas en el dataset 'historical'.
    Re-deriva flow, env, nickname y filename desde s3_path para corregir
    registros guardados con clasificaciones anteriores.
    """
    historical = get_historical_records()
    final_bundles = []

    for row in historical:
        path = row.get("s3_path", "")
        meta = parse_s3_path(path) if path else None
        stored_flow = str(row.get("flow", "") or "")
        flow = meta["flow"] if meta else stored_flow

        if meta:
            env = meta["env"]
            nickname = meta["nickname"]
            proyecto = meta["proyecto"] or row.get("proyecto", "")
            filename = meta["filename"]
        else:
            env = "LAB" if flow == "NEW" else ("PROD" if flow == "LEGACY" else "")
            nickname = "LAB" if flow == "NEW" else ("LEGACY" if flow == "LEGACY" else "")
            proyecto = row.get("proyecto", "")
            filename = path.rstrip("/").split("/")[-1] if path else ""

        final_bundles.append({
            "id": str(row.get("id", "")),
            "s3_path": path,
            "flow": flow,
            "env": env,
            "nickname": nickname,
            "proyecto": proyecto,
            "filename": filename,
            "fecha_creacion_s3": str(row.get("fecha_creacion_s3", "")),
            "fecha_periodo_limpieza": str(row.get("fecha_periodo_limpieza", "")),
            "size": row.get("size", 0),
            "estado": "Conservado",
            "source_folder": FLOW_TO_FOLDER.get(flow, ""),
        })

    return jsonify({
        "status": "success",
        "bundles": final_bundles,
        "periodo_ejecucion": datetime.now().strftime("%b %Y"),
    })


@app.route("/authorize-cleanup", methods=["POST"])
def api_authorize_cleanup():
    """
    Ejecutar la purga física en S3 de los marcados como 'Descartado'.
    Actualizar el dataset 'historical' de forma incremental
    (inserta Conservado, borra Descartado) y generar el reporte CSV.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "JSON inválido"}), 400
    items = data.get("bundles", [])
    if not isinstance(items, list):
        return jsonify({"status": "error", "message": "'bundles' debe ser una lista"}), 400

    # Validar esquema
    for b in items:
        if not isinstance(b, dict) or not b.get("s3_path") or b.get("estado") not in VALID_ESTADOS:
            return jsonify({
                "status": "error",
                "message": "Cada bundle requiere 's3_path' y estado Conservado/Descartado",
            }), 400

    current_period = datetime.now().strftime("%Y-%m-%d")
    folder_cache = {}

    def get_folder_for(bundle):
        folder_id = bundle.get("source_folder") or FLOW_TO_FOLDER.get(bundle.get("flow"))
        if folder_id not in S3_FOLDER_IDS:
            raise ValueError(f"Folder no identificado para {bundle.get('s3_path')}")
        if folder_id not in folder_cache:
            folder_cache[folder_id] = dataiku.Folder(folder_id)
        return folder_cache[folder_id]

    to_delete = [b for b in items if b.get("estado") == "Descartado"]
    to_preserve = [b for b in items if b.get("estado") == "Conservado"]

    # Borrado en S3
    for b in to_delete:
        try:
            folder = get_folder_for(b)
            folder.delete_path(b["s3_path"])
            b["Estado"] = "Eliminado"
        except Exception as e:
            b["Estado"] = f"Error: {str(e)}"
    for b in to_preserve:
        b["Estado"] = "Conservado"

    # Actualización incremental de 'historical'
    delete_paths = {str(b["s3_path"]).rstrip("/") for b in to_delete}
    merged = {}
    for row in get_historical_records():
        sp = row.get("s3_path")
        if sp and str(sp).rstrip("/") not in delete_paths:
            merged[str(sp).rstrip("/")] = {k: row.get(k, "") for k in HISTORICAL_COLUMNS}
    for b in to_preserve:
        sp = b["s3_path"]
        norm_sp = str(sp).rstrip("/")
        merged[norm_sp] = {
            "id": b.get("id") or md5_of_path(sp),
            "s3_path": sp,
            "flow": b.get("flow", ""),
            "proyecto": b.get("proyecto", ""),
            "fecha_creacion_s3": b.get("fecha_creacion_s3", b.get("fecha_creacion", "")),
            "fecha_periodo_limpieza": current_period,
            "size": b.get("size", b.get("size_mb", 0)),
        }
    save_historical_records([merged[k] for k in sorted(merged)])

    # CSV del periodo
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=HISTORICAL_COLUMNS + ["Estado"])
    writer.writeheader()

    for b in items:
        sp = b["s3_path"]
        writer.writerow({
            "id": b.get("id") or md5_of_path(sp),
            "s3_path": sp,
            "flow": b.get("flow", ""),
            "proyecto": b.get("proyecto", ""),
            "fecha_creacion_s3": b.get("fecha_creacion_s3", b.get("fecha_creacion", "")),
            "fecha_periodo_limpieza": current_period,
            "size": b.get("size", b.get("size_mb", 0)),
            "Estado": b.get("Estado", b.get("estado", "")),
        })

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"Reporte_Limpieza_{current_period}.csv",
    )


@app.route("/global-report", methods=["GET"])
def api_global_report():
    cutoff_days = get_cutoff_days_from_request()
    s3_bundles, warnings = fetch_s3_bundles(cutoff_days=cutoff_days)
    if warnings:
        try:
            app.logger.warning(" | ".join(warnings))
        except Exception:
            print(" | ".join(warnings))

    historical = get_historical_records()
    historical_by_path = {
        str(h["s3_path"]).rstrip("/"): h for h in historical if "s3_path" in h and h["s3_path"]
    }
    current_period = datetime.now().strftime("%Y-%m-%d")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=HISTORICAL_COLUMNS + ["Estado"])
    writer.writeheader()

    for b in s3_bundles:
        sp = b["s3_path"]
        norm_sp = str(sp).rstrip("/")
        if norm_sp in historical_by_path:
            h = historical_by_path[norm_sp]
            writer.writerow({
                "id": h.get("id", b["id"]),
                "s3_path": sp,
                "flow": h.get("flow", b["flow"]),
                "proyecto": h.get("proyecto", b["proyecto"]),
                "fecha_creacion_s3": h.get("fecha_creacion_s3", b.get("fecha_creacion_s3", "")),
                "fecha_periodo_limpieza": h.get("fecha_periodo_limpieza", ""),
                "size": h.get("size", b.get("size", 0)),
                "Estado": "Conservado",
            })
        else:
            writer.writerow({
                "id": b["id"],
                "s3_path": sp,
                "flow": b["flow"],
                "proyecto": b["proyecto"],
                "fecha_creacion_s3": b.get("fecha_creacion_s3", ""),
                "fecha_periodo_limpieza": current_period,
                "size": b.get("size", 0),
                "Estado": "Próximo a eliminar",
            })

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"Consulta_Global_S3_{datetime.now().strftime('%Y%m%d')}.csv",
    )
