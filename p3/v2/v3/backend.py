import csv
import hashlib
import io
import math
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta

from flask import jsonify, request, send_file
import dataiku
import pandas as pd

S3_FOLDER_NAME = os.environ.get("S3_FOLDER_NAME", "S3_Bundle_Backup_Path")
HISTORICAL_DATASET_NAME = "historical"
HISTORICAL_COLUMNS = [
    "id", "s3_path", "flow", "proyecto",
    "fecha_creacion_s3", "fecha_periodo_limpieza", "size_mb",
]
VALID_ESTADOS = ("Conservado", "Descartado")
DEFAULT_CUTOFF_DAYS = 180
NEW_FLOW_EXTENSIONS = (".zip", ".gz", ".tar.gz", ".tgz")
PROJECT_BUNDLES_SEGMENT = "project_bundles"
FALLBACK_NEW_FLOW_ENVS = ("UAT", "PROD-1", "PROD", "LAB", "DEV", "QA", "DESIGN")
MB_DIVISOR = 1024 * 1024
BUNDLE_DATE_REGEX = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:[-_](\d{2})-(\d{2})-(\d{2}))?"
)

SCAN_TIME_BUDGET_S = float(os.environ.get("SCAN_TIME_BUDGET_S", "30"))
SCAN_MAX_WORKERS = max(1, int(os.environ.get("SCAN_MAX_WORKERS", "8")))
SCAN_MAX_DETAIL_CALLS = max(1, int(os.environ.get("SCAN_MAX_DETAIL_CALLS", "4000")))
SCAN_CACHE_TTL_S = max(0.0, float(os.environ.get("SCAN_CACHE_TTL_S", "300")))
LEGACY_ALLOW_FALLBACK = str(
    os.environ.get("LEGACY_ALLOW_FALLBACK", "0")
).strip().lower() in ("1", "true", "yes")

NEW_FLOW_HOST_MAP = {
    "sd-7u15-eilw": ("PROD-1", "DKUF"),
    "sd-mn7t-ccgf": ("PROD-1", "RISP"),
    "sd-yslj-ri55": ("PROD-1", "SEGP"),
    "sd-9r2x-8twu": ("UAT", "BUAU"),
    "sd-fkub-coxa": ("UAT", "DKUU"),
    "sd-2edj-g004": ("UAT", "RISU"),
    "sd-4c64-tuzr": ("UAT", "SEGU"),
    "sd-zqo7-s06c": ("UAT", "DKUD"),
}
LEGACY_SERVER_MAP = {
    "sd-90d4-afu8": "PROD",
    "sd-xt24-jvpi": "COB",
}

_SCAN_CACHE = {
    "key": None,
    "ts": 0.0,
    "records": None,
    "warnings": [],
    "scan_ts": "",
}


def get_six_months_ago(cutoff_days=DEFAULT_CUTOFF_DAYS):
    try:
        cutoff_days = int(cutoff_days)
    except (TypeError, ValueError):
        cutoff_days = DEFAULT_CUTOFF_DAYS
    if cutoff_days < 0:
        cutoff_days = 0
    return datetime.now() - timedelta(days=cutoff_days)


def get_cutoff_days_from_request():
    try:
        raw = request.args.get("cutoff_days", DEFAULT_CUTOFF_DAYS)
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_CUTOFF_DAYS


def _request_wants_refresh():
    return str(request.args.get("refresh", "")).strip().lower() in ("1", "true", "yes")


def detect_current_server(hostname=None):
    candidates = [hostname, os.environ.get("HOSTNAME"), os.environ.get("DSS_HOST")]
    host = ""
    for candidate in candidates:
        value = str(candidate or "").strip().lower()
        if not value:
            continue
        if not host:
            host = value
        for ident, display_name in LEGACY_SERVER_MAP.items():
            if ident.lower() in value:
                return {
                    "hostname": value,
                    "flow": "LEGACY",
                    "environment": "PROD",
                    "nickname": display_name or value,
                    "known": True,
                }
        for ident, (env_label, nickname) in NEW_FLOW_HOST_MAP.items():
            if ident.lower() in value:
                return {
                    "hostname": value,
                    "flow": "NEW",
                    "environment": env_label,
                    "nickname": nickname,
                    "known": True,
                }
    if not host:
        host = "unknown"
    return {
        "hostname": host,
        "flow": "LEGACY",
        "environment": "PROD",
        "nickname": host,
        "known": False,
    }


def md5_of_path(s3_path):
    return hashlib.md5(s3_path.encode("utf-8")).hexdigest()


def parse_s3_path(path):
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

    if normalized[-1] == PROJECT_BUNDLES_SEGMENT:
        return None

    if normalized[0].upper() in FALLBACK_NEW_FLOW_ENVS:
        return None

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


def _is_legacy_bundle_path(path):
    clean = str(path or "").lstrip("/").rstrip("/")
    if not clean or clean.endswith("/"):
        return False
    parts = clean.split("/")
    if parts and parts[0].lower() == "dataiku":
        parts = parts[1:]
    if len(parts) < 3:
        return False
    if parts[1] == PROJECT_BUNDLES_SEGMENT:
        return True
    return LEGACY_ALLOW_FALLBACK


def is_bundle_path(path, flow=None, is_folder_bundle=False):
    if not path:
        return False
    if is_folder_bundle:
        return True
    if flow == "NEW":
        return path.lower().endswith(NEW_FLOW_EXTENSIONS)
    if flow == "LEGACY":
        return _is_legacy_bundle_path(path)
    lowered = path.lower()
    if lowered.endswith(NEW_FLOW_EXTENSIONS):
        return True
    clean = path.lstrip("/").rstrip("/")
    return "/" in clean and not clean.endswith("/")


def extract_date_from_name(name):
    if not name:
        return None
    matches = list(BUNDLE_DATE_REGEX.finditer(str(name)))
    if not matches:
        return None
    match = matches[-1]
    try:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        hour = int(match.group(4)) if match.group(4) is not None else 0
        minute = int(match.group(5)) if match.group(5) is not None else 0
        second = int(match.group(6)) if match.group(6) is not None else 0
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def coerce_creation_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:
        pass
    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none", "null"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _bundle_label(meta):
    return meta.get("bundle_name") or meta.get("filename", "")


def _entry_creation_date(meta):
    return extract_date_from_name(_bundle_label(meta))


def _get_detail(folder, path):
    try:
        details = folder.get_path_details(path) or {}
    except Exception:
        return (0, 0, 0)
    if not isinstance(details, dict):
        return (0, 0, 0)

    last_modified_ms = details.get("lastModified", 0) or 0
    size_bytes = details.get("size", 0) or 0
    children_total = 0
    children = details.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                children_total += child.get("size", 0) or 0
    return (last_modified_ms, size_bytes, children_total)


def _fetch_details(folder, paths, deadline):
    results = {}
    calls = 0
    timed_out = False
    if not paths:
        return results, calls, timed_out

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return results, calls, True

    executor = ThreadPoolExecutor(max_workers=min(SCAN_MAX_WORKERS, len(paths)))
    futures = {}
    try:
        for path in paths:
            futures[executor.submit(_get_detail, folder, path)] = path
        calls = len(futures)
        try:
            for future in as_completed(futures, timeout=remaining):
                path = futures[future]
                try:
                    results[path] = future.result()
                except Exception:
                    results[path] = (0, 0, 0)
        except FuturesTimeoutError:
            timed_out = True
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False)
    return results, calls, timed_out


def _build_entries(folder, server):
    paths = folder.list_paths_in_partition()
    file_entries = []
    folder_bundles = {}

    for path in paths:
        meta = parse_s3_path(path)
        if not meta:
            continue
        if meta.get("flow") != server.get("flow"):
            continue
        if server.get("flow") == "NEW" and meta.get("nickname") != server.get("nickname"):
            continue
        if not is_bundle_path(
            path,
            flow=meta.get("flow"),
            is_folder_bundle=meta.get("is_folder_bundle"),
        ):
            continue

        if meta.get("is_folder_bundle"):
            bundle_path = meta["bundle_path"]
            info = folder_bundles.get(bundle_path)
            if info is None:
                info = {"meta": meta, "child_count": 0}
                folder_bundles[bundle_path] = info
            info["child_count"] += 1
            continue

        file_entries.append((path, meta))

    return paths, file_entries, folder_bundles


def _make_record(meta, s3_path, creation_date, size_bytes, server,
                 bundle_name, is_folder_bundle, child_count):
    flow = meta.get("flow")
    env = meta.get("env") if flow == "NEW" else "PROD"
    nickname = meta.get("nickname") if flow == "NEW" else server.get("hostname")
    return {
        "id": md5_of_path(s3_path),
        "s3_path": s3_path,
        "source_folder": S3_FOLDER_NAME,
        "flow": flow,
        "env": env,
        "nickname": nickname,
        "proyecto": meta.get("proyecto", ""),
        "bundle_name": bundle_name,
        "filename": meta.get("filename", "") or bundle_name,
        "is_folder_bundle": bool(is_folder_bundle),
        "child_count": int(child_count or 0),
        "creation_date": creation_date,
        "size_mb": round(float(size_bytes or 0) / MB_DIVISOR, 2),
    }


def _scan_s3(server):
    started = time.monotonic()
    deadline = started + SCAN_TIME_BUDGET_S
    scan_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        folder = dataiku.Folder(S3_FOLDER_NAME)
        paths, file_entries, folder_bundles = _build_entries(folder, server)
    except Exception as e:
        return [], [f"No se pudo acceder al folder {S3_FOLDER_NAME}: {e}"], scan_ts, started

    file_order = []
    for path, meta in file_entries:
        creation_date = _entry_creation_date(meta)
        file_order.append((creation_date, path))
    file_order.sort(key=lambda item: (item[0] is not None, item[0] or datetime.max))

    folder_order = []
    for bundle_path, info in folder_bundles.items():
        creation_date = _entry_creation_date(info["meta"])
        folder_order.append((creation_date, bundle_path))
    folder_order.sort(key=lambda item: (item[0] is not None, item[0] or datetime.max))

    detail_paths = []
    seen = set()
    for _, path in file_order + folder_order:
        if path in seen:
            continue
        seen.add(path)
        detail_paths.append(path)
    detail_paths = detail_paths[:SCAN_MAX_DETAIL_CALLS]

    details, calls, timed_out = _fetch_details(folder, detail_paths, deadline)

    records = []
    skipped_no_date = 0

    for path, meta in file_entries:
        creation_date = _entry_creation_date(meta)
        last_modified_ms, size_bytes, _children_total = details.get(path, (0, 0, 0))
        if last_modified_ms > 0:
            creation_date = datetime.fromtimestamp(last_modified_ms / 1000.0)
        if creation_date is None:
            skipped_no_date += 1
            continue
        records.append(_make_record(
            meta, path, creation_date, size_bytes, server,
            _bundle_label(meta), False, 0,
        ))

    for bundle_path, info in folder_bundles.items():
        meta = info["meta"]
        creation_date = _entry_creation_date(meta)
        last_modified_ms, size_bytes, children_total = details.get(bundle_path, (0, 0, 0))
        if last_modified_ms > 0:
            creation_date = datetime.fromtimestamp(last_modified_ms / 1000.0)
        if creation_date is None:
            skipped_no_date += 1
            continue
        records.append(_make_record(
            meta, bundle_path, creation_date, size_bytes or children_total, server,
            _bundle_label(meta), True, info.get("child_count", 0),
        ))

    warnings = []
    if skipped_no_date > 0:
        warnings.append(
            f"{skipped_no_date} bundle(s) del folder {S3_FOLDER_NAME} se omitieron por no poder determinar su fecha"
        )
    if timed_out:
        warnings.append(
            f"El escaneo de S3 superó {SCAN_TIME_BUDGET_S:.0f}s; algunos tamaños pueden ser 0 o estimados."
        )
    elif calls >= SCAN_MAX_DETAIL_CALLS:
        warnings.append(
            f"Se alcanzó el límite de {SCAN_MAX_DETAIL_CALLS} consultas de detalle; algunos tamaños pueden ser 0 o estimados."
        )

    elapsed = time.monotonic() - started
    try:
        print(
            f"[scan-s3] folder={S3_FOLDER_NAME} host={server.get('hostname')} "
            f"flow={server.get('flow')} paths={len(paths)} records={len(records)} "
            f"detail_calls={calls} elapsed={elapsed:.1f}s"
        )
    except Exception:
        pass

    return records, warnings, scan_ts, started


def _scan_cache_key(server):
    return (
        server.get("hostname"),
        server.get("flow"),
        server.get("nickname"),
        S3_FOLDER_NAME,
    )


def get_scan_records(server, force=False):
    cache_key = _scan_cache_key(server)
    if (
        not force
        and SCAN_CACHE_TTL_S > 0
        and _SCAN_CACHE["key"] == cache_key
        and _SCAN_CACHE["records"] is not None
        and (time.monotonic() - _SCAN_CACHE["ts"]) <= SCAN_CACHE_TTL_S
    ):
        return _SCAN_CACHE["records"], list(_SCAN_CACHE["warnings"]), _SCAN_CACHE["scan_ts"], True

    records, warnings, scan_ts, _started = _scan_s3(server)
    if records:
        _SCAN_CACHE["key"] = cache_key
        _SCAN_CACHE["ts"] = time.monotonic()
        _SCAN_CACHE["records"] = records
        _SCAN_CACHE["warnings"] = warnings
        _SCAN_CACHE["scan_ts"] = scan_ts
    return records, warnings, scan_ts, False


def _invalidate_scan_cache():
    _SCAN_CACHE["key"] = None
    _SCAN_CACHE["ts"] = 0.0
    _SCAN_CACHE["records"] = None
    _SCAN_CACHE["warnings"] = []
    _SCAN_CACHE["scan_ts"] = ""


def _row_belongs_to_server(row, server):
    if not row or not server:
        return False
    if row.get("flow") != server.get("flow"):
        return False
    if server.get("flow") == "NEW":
        return row.get("nickname") == server.get("nickname")
    return True


def _response_record(row, creation_date):
    return {
        "id": row.get("id") or md5_of_path(row.get("s3_path", "")),
        "s3_path": row.get("s3_path", ""),
        "flow": row.get("flow", ""),
        "env": row.get("env", "PROD"),
        "nickname": row.get("nickname", ""),
        "proyecto": row.get("proyecto", ""),
        "filename": row.get("filename") or row.get("bundle_name", ""),
        "fecha_creacion_s3": creation_date.strftime("%Y-%m-%d %H:%M:%S"),
        "size_mb": round(
            float(row.get("size_mb") or row.get("size") or 0), 2
        ),
        "source_folder": row.get("source_folder") or S3_FOLDER_NAME,
    }


def split_records(records, cutoff_date, server=None, now=None):
    now = now or datetime.now()
    bundles = []
    recent = []
    skipped = 0

    for row in records or []:
        if server is not None and not _row_belongs_to_server(row, server):
            continue
        creation_date = coerce_creation_date(row.get("creation_date"))
        if creation_date is None:
            skipped += 1
            continue

        record = _response_record(row, creation_date)
        if creation_date < cutoff_date:
            bundles.append(record)
        else:
            days_left = math.ceil(
                (creation_date + timedelta(days=DEFAULT_CUTOFF_DAYS) - now).total_seconds()
                / 86400.0
            )
            record["dias_para_limpieza"] = max(0, min(DEFAULT_CUTOFF_DAYS, days_left))
            recent.append(record)

    return bundles, recent, skipped


def fetch_s3_bundles(cutoff_days=DEFAULT_CUTOFF_DAYS, server=None, force=False):
    cutoff_date = get_six_months_ago(cutoff_days)
    server = dict(server or detect_current_server())
    warnings = []

    if not server.get("known"):
        warnings.append(
            f"Servidor no identificado ('{server.get('hostname')}'); se asume PROD."
        )

    records, scan_warnings, scan_ts, cache_hit = get_scan_records(server, force=force)
    warnings.extend(scan_warnings)

    bundles, recent_bundles, skipped = split_records(records, cutoff_date, server)
    if skipped > 0:
        warnings.append(
            f"{skipped} bundle(s) del folder {S3_FOLDER_NAME} se omitieron por no poder determinar su fecha"
        )

    server["last_scan"] = scan_ts or ""
    server["scan_source"] = "live"
    server["scan_rows"] = len(records or [])
    server["cache_hit"] = cache_hit
    server["folder"] = S3_FOLDER_NAME
    server["can_authorize"] = bool(server.get("known"))
    server["authorize_block_reason"] = (
        ""
        if server.get("can_authorize")
        else "Servidor no identificado. Verifique la conexión y el hostname."
    )
    server["shared_pool"] = server.get("flow") == "LEGACY"
    return bundles, recent_bundles, warnings, server


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


def get_historical_records():
    try:
        dataset = dataiku.Dataset(HISTORICAL_DATASET_NAME)
        df = dataset.get_dataframe()
        records = df.to_dict(orient="records")
    except Exception:
        return []

    for row in records:
        if row.get("size_mb") in (None, ""):
            row["size_mb"] = row.get("size", 0)
    return records


def save_historical_records(records):
    dataset = dataiku.Dataset(HISTORICAL_DATASET_NAME)
    df = pd.DataFrame(records, columns=HISTORICAL_COLUMNS)
    dataset.write_with_schema(df)


@app.route("/scan-bundles", methods=["GET"])
def api_scan_bundles():
    cutoff_days = get_cutoff_days_from_request()
    force = _request_wants_refresh()
    server = detect_current_server()
    s3_bundles, recent_bundles, warnings, server = fetch_s3_bundles(
        cutoff_days=cutoff_days, server=server, force=force
    )
    historical = get_historical_records()
    historical_paths = {
        _norm_s3_path(h["s3_path"])
        for h in historical
        if "s3_path" in h and h["s3_path"] and historical_matches_server(h, server)
    }
    candidates = [
        b for b in s3_bundles if _norm_s3_path(b["s3_path"]) not in historical_paths
    ]
    recent_visible = [
        b
        for b in recent_bundles
        if str(b["s3_path"]).rstrip("/") not in historical_paths
    ]
    recent_visible.sort(
        key=lambda x: (x.get("dias_para_limpieza", 0), x.get("fecha_creacion_s3", ""))
    )

    groups = {}
    for b in candidates:
        group_key = (b["flow"], b["env"], b["nickname"], b["proyecto"])
        if group_key not in groups:
            groups[group_key] = []
        groups[group_key].append(b)

    final_bundles = []

    for _group_key, items in groups.items():
        items.sort(
            key=lambda x: (x.get("fecha_creacion_s3", ""), x.get("filename", "")),
            reverse=True,
        )

        for idx, item in enumerate(items):
            item["estado"] = "Conservado" if idx == 0 else "Descartado"
            final_bundles.append(item)

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
            env = meta.get("env") or (
                "PROD" if flow == "LEGACY" else (server.get("environment") or "")
            )
            nickname = meta["nickname"] if flow == "NEW" else server.get("hostname")
            proyecto = meta["proyecto"] or row.get("proyecto", "")
            filename = meta["filename"]
        else:
            env = (
                "PROD"
                if flow == "LEGACY"
                else (server.get("environment") or "")
            )
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
            "size_mb": row.get("size_mb") or row.get("size") or 0,
            "estado": "Conservado",
            "source_folder": S3_FOLDER_NAME,
        })

    return jsonify({
        "status": "success",
        "bundles": final_bundles,
        "server": server,
        "periodo_ejecucion": datetime.now().strftime("%b %Y"),
    })


def _parse_report_format(default="csv"):
    fmt = (request.args.get("format") or default).strip().lower()
    if fmt in ("excel", "xlsx"):
        return "xlsx"
    if fmt == "csv":
        return "csv"
    return None


def _ambiente_label(server):
    env = str((server or {}).get("environment", "")).strip().upper()
    if env.startswith("PROD"):
        return "PROD"
    if env == "UAT":
        return "UAT"
    return "PROD" if (server or {}).get("flow") == "LEGACY" else ""


def _report_columns():
    return [
        "ambiente" if c == "flow" else c for c in HISTORICAL_COLUMNS
    ] + ["Estado"]


def _report_response(rows, columns, base_name, fmt, sheet_name="Reporte"):
    """Genera el reporte en CSV o XLSX con el mismo formato de hoja."""
    if fmt == "xlsx":
        try:
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter

            df = pd.DataFrame(rows, columns=columns)
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name=sheet_name)
                ws = writer.sheets[sheet_name]

                header_fill = PatternFill("solid", fgColor="F2D5D7")
                for cell in ws[1]:
                    cell.font = Font(bold=True, color="7A0A12")
                    cell.fill = header_fill
                    cell.alignment = Alignment(vertical="center")

                ws.freeze_panes = "A2"
                ws.auto_filter.ref = ws.dimensions

                for idx, column in enumerate(columns, start=1):
                    max_len = max(
                        [len(str(column))]
                        + [len(str(row.get(column, ""))) for row in rows]
                    )
                    ws.column_dimensions[get_column_letter(idx)].width = min(
                        max(12, max_len + 2), 60
                    )

                if "size_mb" in columns:
                    size_col = columns.index("size_mb") + 1
                    for (cell,) in ws.iter_rows(
                        min_row=2, min_col=size_col, max_col=size_col
                    ):
                        cell.number_format = "#,##0.00"

            buffer.seek(0)
            return send_file(
                buffer,
                mimetype=(
                    "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"
                ),
                as_attachment=True,
                download_name=f"{base_name}.xlsx",
            )
        except Exception:
            app.logger.exception("No fue posible generar el archivo Excel.")
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "No fue posible generar el archivo Excel.",
                    }
                ),
                500,
            )

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{base_name}.csv",
    )


def _norm_s3_path(path):
    return "/" + str(path or "").lstrip("/").rstrip("/")


def _delete_bundle_path(folder, path, is_folder_bundle=False):
    clean = str(path or "").strip()
    if not clean:
        return
    norm = "/" + clean.lstrip("/").rstrip("/")
    if not is_folder_bundle:
        folder.delete_path(norm)
        return
    prefix = norm + "/"
    paths = folder.list_paths_in_partition() or []
    targets = [
        p
        for p in paths
        if _norm_s3_path(p) == norm or _norm_s3_path(p).startswith(prefix)
    ]
    if not targets:
        folder.delete_path(norm)
        return
    for target in targets:
        folder.delete_path(target)


@app.route("/authorize-cleanup", methods=["POST"])
def api_authorize_cleanup():
    fmt = _parse_report_format("xlsx")
    if fmt is None:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Formato no válido. Use csv o xlsx.",
                }
            ),
            400,
        )

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "JSON inválido"}), 400
    items = data.get("bundles", [])
    if not isinstance(items, list):
        return jsonify({"status": "error", "message": "'bundles' debe ser una lista"}), 400

    for b in items:
        if not isinstance(b, dict) or not b.get("s3_path") or b.get("estado") not in VALID_ESTADOS:
            return jsonify({
                "status": "error",
                "message": "Cada bundle requiere 's3_path' y estado Conservado/Descartado",
            }), 400

    server = detect_current_server()
    if not server.get("known"):
        return jsonify({
            "status": "error",
            "message": (
                "Servidor no identificado. No se puede autorizar "
                "la eliminación en S3."
            ),
            "server": server,
        }), 409

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

    try:
        records, _, _, _ = get_scan_records(server, force=False)
    except Exception:
        records = []
    if not records:
        try:
            records, _, _, _ = get_scan_records(server, force=True)
        except Exception:
            records = []

    scan_by_path = {}
    for row in records or []:
        row_path = _norm_s3_path(row.get("s3_path"))
        if row.get("s3_path"):
            scan_by_path[row_path] = row

    unknown_paths = []
    wrong_folder = []
    for b in items:
        sp = str(b.get("s3_path") or "")
        if _norm_s3_path(sp) not in scan_by_path:
            unknown_paths.append(sp)
            continue
        source_folder = b.get("source_folder")
        if source_folder and source_folder != S3_FOLDER_NAME:
            wrong_folder.append(sp)
    if unknown_paths:
        return jsonify({
            "status": "error",
            "message": (
                "Hay bundles que no existen en el último escaneo de S3. "
                "Actualice el escaneo e intente nuevamente."
            ),
            "server": server,
            "paths": [str(p) for p in unknown_paths[:20]],
        }), 409
    if wrong_folder:
        return jsonify({
            "status": "error",
            "message": "Hay bundles que no pertenecen al folder configurado.",
            "server": server,
            "paths": [str(p) for p in wrong_folder[:20]],
        }), 400

    cutoff_date = datetime.now() - timedelta(days=DEFAULT_CUTOFF_DAYS)
    too_recent = []
    for b in items:
        if b.get("estado") != "Descartado":
            continue
        record = scan_by_path.get(_norm_s3_path(b.get("s3_path")))
        creation_date = record.get("creation_date") if record else None
        if creation_date is None:
            creation_date = _coerce_item_date(b)
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

    to_delete = [b for b in items if b.get("estado") == "Descartado"]
    to_preserve = [b for b in items if b.get("estado") == "Conservado"]

    for b in to_delete:
        try:
            record = scan_by_path.get(_norm_s3_path(b["s3_path"])) or {}
            _delete_bundle_path(
                folder,
                b["s3_path"],
                bool(record.get("is_folder_bundle")),
            )
            b["Estado"] = "Eliminado"
        except Exception as e:
            b["Estado"] = f"Error: {str(e)}"
    for b in to_preserve:
        b["Estado"] = "Conservado"

    delete_paths = {_norm_s3_path(b["s3_path"]) for b in to_delete}
    merged = {}
    for row in get_historical_records():
        sp = row.get("s3_path")
        if sp and _norm_s3_path(sp) not in delete_paths:
            merged[_norm_s3_path(sp)] = {k: row.get(k, "") for k in HISTORICAL_COLUMNS}
    for b in to_preserve:
        sp = b["s3_path"]
        norm_sp = _norm_s3_path(sp)
        merged[norm_sp] = {
            "id": b.get("id") or md5_of_path(sp),
            "s3_path": sp,
            "flow": b.get("flow", ""),
            "proyecto": b.get("proyecto", ""),
            "fecha_creacion_s3": b.get("fecha_creacion_s3", b.get("fecha_creacion", "")),
            "fecha_periodo_limpieza": current_period,
            "size_mb": b.get("size_mb") or b.get("size") or 0,
        }
    save_historical_records([merged[k] for k in sorted(merged)])
    _invalidate_scan_cache()

    columns = _report_columns()
    rows = []
    for b in items:
        sp = b["s3_path"]
        rows.append({
            "id": b.get("id") or md5_of_path(sp),
            "s3_path": sp,
            "ambiente": _ambiente_label(server),
            "proyecto": b.get("proyecto", ""),
            "fecha_creacion_s3": b.get("fecha_creacion_s3", b.get("fecha_creacion", "")),
            "fecha_periodo_limpieza": current_period,
            "size_mb": b.get("size_mb") or b.get("size") or 0,
            "Estado": b.get("Estado", b.get("estado", "")),
        })

    return _report_response(
        rows,
        columns,
        f"Reporte_Limpieza_{current_period}",
        fmt,
        sheet_name="Reporte de limpieza",
    )


def _coerce_item_date(bundle):
    creation_date = coerce_creation_date(bundle.get("fecha_creacion_s3"))
    if creation_date is None:
        creation_date = extract_date_from_name(bundle.get("filename", ""))
    if creation_date is None:
        creation_date = extract_date_from_name(bundle.get("s3_path", ""))
    return creation_date


@app.route("/global-report", methods=["GET"])
def api_global_report():
    fmt = _parse_report_format("csv")
    if fmt is None:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Formato no válido. Use csv o xlsx.",
                }
            ),
            400,
        )

    cutoff_days = get_cutoff_days_from_request()
    force = _request_wants_refresh()
    server = detect_current_server()
    s3_bundles, _recent_bundles, warnings, server = fetch_s3_bundles(
        cutoff_days=cutoff_days, server=server, force=force
    )
    if warnings:
        try:
            app.logger.warning(" | ".join(warnings))
        except Exception:
            print(" | ".join(warnings))

    historical = get_historical_records()
    historical_by_path = {
        _norm_s3_path(h["s3_path"]): h
        for h in historical
        if "s3_path" in h and h["s3_path"] and historical_matches_server(h, server)
    }
    current_period = datetime.now().strftime("%Y-%m-%d")

    columns = _report_columns()
    rows = []
    for b in s3_bundles:
        sp = b["s3_path"]
        norm_sp = _norm_s3_path(sp)
        if norm_sp in historical_by_path:
            h = historical_by_path[norm_sp]
            rows.append({
                "id": h.get("id", b["id"]),
                "s3_path": sp,
                "ambiente": _ambiente_label(server),
                "proyecto": h.get("proyecto", b["proyecto"]),
                "fecha_creacion_s3": h.get("fecha_creacion_s3", b.get("fecha_creacion_s3", "")),
                "fecha_periodo_limpieza": h.get("fecha_periodo_limpieza", ""),
                "size_mb": h.get("size_mb") or h.get("size") or b.get("size_mb") or b.get("size") or 0,
                "Estado": "Conservado",
            })
        else:
            rows.append({
                "id": b["id"],
                "s3_path": sp,
                "ambiente": _ambiente_label(server),
                "proyecto": b["proyecto"],
                "fecha_creacion_s3": b.get("fecha_creacion_s3", ""),
                "fecha_periodo_limpieza": current_period,
                "size_mb": b.get("size_mb") or b.get("size") or 0,
                "Estado": "Próximo a eliminar",
            })

    stamp = datetime.now().strftime("%Y%m%d")
    return _report_response(
        rows,
        columns,
        f"Consulta_Global_S3_{stamp}",
        fmt,
        sheet_name="Consulta global",
    )
