import csv
import io
import os
import time
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_file
import dataiku
import pandas as pd

import bundles_scan as bs

# ==========================================
# CONFIGURACIÓN Y CONSTANTES
# ==========================================
# Un solo managed folder por instancia. Su conexión apunta al bucket correcto:
#   - Servidores del flujo nuevo (UAT): bucket dataiku-files-2
#   - Servidores legacy (PROD):         bucket dataiku-files
# La detección del servidor decide cómo interpretar las rutas del folder.
S3_FOLDER_NAME = bs.S3_FOLDER_NAME
INVENTORY_DATASET_NAME = bs.INVENTORY_DATASET_NAME
INVENTORY_STALE_HOURS = float(
    os.environ.get("INVENTORY_STALE_HOURS", str(24 * 95))
)

HISTORICAL_DATASET_NAME = "historical"
HISTORICAL_COLUMNS = [
    "id", "s3_path", "flow", "proyecto",
    "fecha_creacion_s3", "fecha_periodo_limpieza", "size",
]
VALID_ESTADOS = ("Conservado", "Descartado")
DEFAULT_CUTOFF_DAYS = bs.DEFAULT_CUTOFF_DAYS


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


def detect_current_server(hostname=None):
    return bs.detect_current_server(hostname)


def path_belongs_to_server(path, server):
    meta = parse_s3_path(path)
    if not meta:
        return False
    if meta.get("flow") != server.get("flow"):
        return False
    if server.get("flow") == "NEW":
        return meta.get("nickname") == server.get("nickname")
    return True


def historical_matches_server(row, server):
    path = row.get("s3_path", "") if isinstance(row, dict) else ""
    if not path:
        return True
    meta = parse_s3_path(path)
    if not meta:
        return True
    if meta.get("flow") != server.get("flow"):
        return False
    if server.get("flow") == "NEW":
        return meta.get("nickname") == server.get("nickname")
    return True


def _coerce_creation_date(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def md5_of_path(s3_path):
    return bs.md5_of_path(s3_path)


def extract_date_from_name(name):
    return bs.extract_date_from_name(name)


# ==========================================
# FUNCIONES HELPER
# ==========================================
def parse_s3_path(path):
    return bs.parse_s3_path(path)


def _inventory_source():
    return (os.environ.get("INVENTORY_SOURCE") or "dataset").strip().lower()


def _stale_warning(scan_ts):
    if not scan_ts:
        return None
    try:
        ts = datetime.strptime(str(scan_ts)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    age_hours = (datetime.now() - ts).total_seconds() / 3600.0
    if age_hours <= INVENTORY_STALE_HOURS:
        return None
    return (
        f"El inventario {INVENTORY_DATASET_NAME} tiene {age_hours:.0f} h de antigüedad "
        f"(último escaneo: {scan_ts})."
    )


def fetch_s3_bundles(cutoff_days=DEFAULT_CUTOFF_DAYS, server=None):
    cutoff_date = get_six_months_ago(cutoff_days)
    server = dict(server or detect_current_server())
    warnings = []
    rows = None
    scan_ts = None

    if not server.get("known"):
        warnings.append(
            f"Servidor no identificado ('{server.get('hostname')}'); se asume PROD."
        )

    source = _inventory_source()

    if source != "s3":
        try:
            rows = bs.load_inventory(INVENTORY_DATASET_NAME)
        except Exception as e:
            warnings.append(
                f"No se pudo leer {INVENTORY_DATASET_NAME}: {e}. Se usa escaneo en vivo."
            )
            rows = None

    if rows is not None:
        if not rows:
            warnings.append(
                f"El inventario {INVENTORY_DATASET_NAME} está vacío; ejecute el scenario de escaneo."
            )
        scan_ts = bs.latest_scan_ts(rows, server)
    else:
        started = time.monotonic()
        try:
            folder = dataiku.Folder(S3_FOLDER_NAME)
            budget = bs.ScanBudget()
            rows, scan_warnings = bs.collect_bundles(folder, server, budget=budget)
            warnings.extend(scan_warnings)
            scan_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            warnings.append(f"No se pudo acceder al folder {S3_FOLDER_NAME}: {e}")
            rows = []
        else:
            elapsed = time.monotonic() - started
            try:
                print(
                    f"[scan-bundles] source=s3 folder={S3_FOLDER_NAME} "
                    f"flow={server.get('flow')} host={server.get('hostname')} "
                    f"rows={len(rows)} elapsed={elapsed:.1f}s"
                )
            except Exception:
                pass

    bundles, recent_bundles, skipped = bs.split_inventory(rows, cutoff_date, server)
    if skipped > 0:
        warnings.append(
            f"{skipped} bundle(s) del folder {S3_FOLDER_NAME} se omitieron por no poder determinar su fecha"
        )

    stale = _stale_warning(scan_ts)
    if stale:
        warnings.append(stale)

    server["last_scan"] = scan_ts or ""
    server["inventory_source"] = "s3" if source == "s3" else "dataset"
    server["inventory_rows"] = len(rows or [])
    return bundles, recent_bundles, warnings, server


# ==========================================
# GESTIÓN DEL HISTORICAL
# ==========================================
def get_historical_records():
    # Leer el dataset administrado 'historical' y retorna una lista de dicts
    try:
        dataset = dataiku.Dataset(HISTORICAL_DATASET_NAME)
        df = dataset.get_dataframe()
        records = df.to_dict(orient="records")
    except Exception:
        # Retornar lista vacía si el dataset aún no se ha inicializado
        return []

    # Compatibilidad con datasets creados con la columna 'size_mb'.
    for row in records:
        current = row.get("size")
        if current in (None, ""):
            row["size"] = row.get("size_mb", 0)
    return records


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
    server = detect_current_server()
    s3_bundles, recent_bundles, warnings, server = fetch_s3_bundles(
        cutoff_days=cutoff_days, server=server
    )
    historical = get_historical_records()
    historical_paths = {
        str(h["s3_path"]).rstrip("/")
        for h in historical
        if "s3_path" in h and h["s3_path"] and historical_matches_server(h, server)
    }
    candidates = [
        b for b in s3_bundles if str(b["s3_path"]).rstrip("/") not in historical_paths
    ]
    recent_visible = [
        b
        for b in recent_bundles
        if str(b["s3_path"]).rstrip("/") not in historical_paths
    ]
    recent_visible.sort(
        key=lambda x: (x.get("dias_para_limpieza", 0), x.get("fecha_creacion_s3", ""))
    )

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
        "recent_bundles": recent_visible,
        "recent_window": {
            "dias": cutoff_days,
            "hasta": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "warnings": warnings,
        "server": server,
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
    server = detect_current_server()
    historical = get_historical_records()
    final_bundles = []

    for row in historical:
        if not historical_matches_server(row, server):
            continue

        path = row.get("s3_path", "")
        meta = parse_s3_path(path) if path else None
        stored_flow = str(row.get("flow", "") or "")
        flow = meta["flow"] if meta else (stored_flow or server.get("flow"))

        if meta:
            env = "UAT" if flow == "NEW" else "PROD"
            nickname = meta["nickname"] if flow == "NEW" else server.get("hostname")
            proyecto = meta["proyecto"] or row.get("proyecto", "")
            filename = meta["filename"]
        else:
            env = "UAT" if flow == "NEW" else ("PROD" if flow == "LEGACY" else "")
            nickname = (
                server.get("hostname", "")
                if flow == "LEGACY"
                else (server.get("nickname") or "")
            )
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
            "source_folder": S3_FOLDER_NAME,
        })

    return jsonify({
        "status": "success",
        "bundles": final_bundles,
        "server": server,
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

    server = detect_current_server()
    invalid_paths = [
        b.get("s3_path")
        for b in items
        if not path_belongs_to_server(b.get("s3_path"), server)
    ]
    if invalid_paths:
        return jsonify({
            "status": "error",
            "message": "Hay bundles que no pertenecen al servidor/flujo actual",
            "server": server,
            "paths": [str(p) for p in invalid_paths[:20]],
        }), 400

    # Candado de política: no eliminar bundles con menos de 180 días.
    cutoff_date = datetime.now() - timedelta(days=DEFAULT_CUTOFF_DAYS)
    too_recent = []
    for b in items:
        if b.get("estado") != "Descartado":
            continue
        creation_date = _coerce_creation_date(b.get("fecha_creacion_s3"))
        if creation_date is None:
            creation_date = extract_date_from_name(b.get("filename", ""))
        if creation_date is None:
            creation_date = extract_date_from_name(b.get("s3_path", ""))
        if creation_date is None or creation_date >= cutoff_date:
            too_recent.append(b.get("s3_path"))
    if too_recent:
        return jsonify({
            "status": "error",
            "message": (
                "No se pueden eliminar bundles con menos de "
                f"{DEFAULT_CUTOFF_DAYS} días de antigüedad"
            ),
            "server": server,
            "paths": [str(p) for p in too_recent[:20]],
        }), 400

    current_period = datetime.now().strftime("%Y-%m-%d")
    folder = dataiku.Folder(S3_FOLDER_NAME)

    def get_folder_for(bundle):
        return folder

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

    if _inventory_source() != "s3":
        removed_paths = {
            str(b["s3_path"]).rstrip("/")
            for b in to_delete
            if b.get("Estado") == "Eliminado"
        }
        removed_paths.update(str(b["s3_path"]).rstrip("/") for b in to_preserve)
        try:
            bs.trim_inventory(removed_paths, INVENTORY_DATASET_NAME)
        except Exception as e:
            print(f"A No se pudo sincronizar {INVENTORY_DATASET_NAME}: {e}")

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
    server = detect_current_server()
    s3_bundles, _recent_bundles, warnings, server = fetch_s3_bundles(
        cutoff_days=cutoff_days, server=server
    )
    if warnings:
        try:
            app.logger.warning(" | ".join(warnings))
        except Exception:
            print(" | ".join(warnings))

    historical = get_historical_records()
    historical_by_path = {
        str(h["s3_path"]).rstrip("/"): h
        for h in historical
        if "s3_path" in h and h["s3_path"] and historical_matches_server(h, server)
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
