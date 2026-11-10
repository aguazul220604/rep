import hashlib
import math
import os
import re
import socket
import time
from collections import deque
from datetime import datetime, timedelta

S3_FOLDER_NAME = "S3_Bundle_Backup_Path"
INVENTORY_DATASET_NAME = "bundles_inventory"
INVENTORY_COLUMNS = [
    "id", "s3_path", "source_folder", "flow", "env", "nickname", "proyecto",
    "bundle_name", "filename", "is_folder_bundle", "child_count",
    "fecha_creacion_s3", "has_date", "size", "scan_ts", "scan_host",
]

DEFAULT_CUTOFF_DAYS = 180
NEW_FLOW_EXTENSIONS = (".zip", ".gz", ".tar.gz", ".tgz")
PROJECT_BUNDLES_SEGMENT = "project_bundles"
FALLBACK_NEW_FLOW_ENVS = ("UAT", "PROD-1", "PROD", "LAB", "DEV", "QA", "DESIGN")
MB_DIVISOR = 1024 * 1024
SIZE_SAMPLE_PER_BUNDLE = int(os.environ.get("BUNDLE_SIZE_SAMPLE", "5"))
SCAN_MAX_DETAIL_CALLS = int(os.environ.get("SCAN_MAX_DETAIL_CALLS", "300"))
SCAN_TIME_BUDGET_S = float(os.environ.get("SCAN_TIME_BUDGET_S", "60"))

BUNDLE_DATE_REGEX = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:[-_](\d{2})-(\d{2})-(\d{2}))?"
)

NEW_FLOW_SERVER_MAP = {
    "sd-7u15-eilw": "DKUF",
    "sd-mn7t-ccgf": "RISP",
    "sd-yslj-ri55": "SEGP",
    "sd-9r2x-8twu": "BUAU",
    "sd-fkub-coxa": "DKUU",
    "sd-2edj-g004": "RISU",
    "sd-4c64-tuzr": "SEGU",
    "sd-zqo7-s06c": "DKUD",
}

LEGACY_SERVER_MAP = {
    "sd-90d4-afu8": "sd-90d4-afu8",
    "sd-xt24-jvpi": "sd-xt24-jvpi",
}


class ScanBudget:
    def __init__(self, max_calls=None, time_budget_s=None):
        self.max_calls = SCAN_MAX_DETAIL_CALLS if max_calls is None else max_calls
        self.time_budget_s = (
            SCAN_TIME_BUDGET_S if time_budget_s is None else time_budget_s
        )
        self.calls = 0
        self.deadline = time.monotonic() + max(0.0, self.time_budget_s)

    def can_call(self):
        return self.calls < self.max_calls and time.monotonic() < self.deadline

    def consume(self):
        self.calls += 1

    def out_of_time(self):
        return time.monotonic() >= self.deadline


def detect_current_server(hostname=None):
    if hostname is None:
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "unknown"

    host = (hostname or "unknown").lower()

    for ident, display_name in LEGACY_SERVER_MAP.items():
        if ident.lower() in host:
            return {
                "hostname": host,
                "flow": "LEGACY",
                "environment": "PROD",
                "nickname": display_name or host,
                "known": True,
            }

    for ident, nickname in NEW_FLOW_SERVER_MAP.items():
        if ident.lower() in host:
            return {
                "hostname": host,
                "flow": "NEW",
                "environment": "UAT",
                "nickname": nickname,
                "known": True,
            }

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
            "in_project_bundles": True,
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
            "in_project_bundles": False,
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
            "in_project_bundles": False,
        }

    return None


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


def extract_date_from_name(name):
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


def _safe_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if math.isnan(number) or math.isinf(number):
        return 0
    return number


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


def get_folder_bundle_details(folder, bundle_path, info, bundle_name, budget=None):
    creation_date = extract_date_from_name(bundle_name)
    last_modified_ms = 0
    total_size_bytes = 0

    info = info or {}
    child_count = info.get("child_count", 0) or 0
    sample_paths = []
    seen = set()
    for p in list(info.get("sample_first", [])) + list(info.get("sample_last", [])):
        if p not in seen:
            seen.add(p)
            sample_paths.append(p)

    if budget is None or budget.can_call():
        try:
            if budget is not None:
                budget.consume()
            details = folder.get_path_details(bundle_path) or {}
            if isinstance(details, dict):
                last_modified_ms = details.get("lastModified", 0) or 0
                total_size_bytes = details.get("size", 0) or 0
                if total_size_bytes == 0 and isinstance(details.get("children"), list):
                    total_size_bytes = sum(
                        (c.get("size", 0) or 0)
                        for c in details["children"]
                        if isinstance(c, dict)
                    )
        except Exception:
            pass

    if total_size_bytes == 0 and sample_paths:
        sampled_size = 0
        sampled_n = 0
        for cp in sample_paths:
            if budget is not None and not budget.can_call():
                break
            try:
                if budget is not None:
                    budget.consume()
                c_details = folder.get_path_details(cp) or {}
                if isinstance(c_details, dict):
                    c_mod = c_details.get("lastModified", 0) or 0
                    if c_mod > last_modified_ms:
                        last_modified_ms = c_mod
                    sampled_size += c_details.get("size", 0) or 0
                    sampled_n += 1
            except Exception:
                continue
        if sampled_n > 0:
            avg_size = sampled_size / sampled_n
            total_size_bytes = int(avg_size * max(child_count, sampled_n))

    if creation_date is None and last_modified_ms > 0:
        creation_date = datetime.fromtimestamp(last_modified_ms / 1000.0)

    return creation_date, total_size_bytes


def _row_belongs_to_server(row, server):
    if not row or not server:
        return False
    if row.get("flow") != server.get("flow"):
        return False
    if server.get("flow") == "NEW":
        return row.get("nickname") == server.get("nickname")
    return True


def _inventory_row(meta, s3_path, creation_date, size_bytes, server,
                   bundle_name, is_folder_bundle, child_count):
    flow = meta.get("flow")
    env = "UAT" if flow == "NEW" else "PROD"
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
        "fecha_creacion_s3": creation_date.strftime("%Y-%m-%d %H:%M:%S"),
        "has_date": True,
        "size": round(size_bytes / MB_DIVISOR, 2),
        "scan_ts": "",
        "scan_host": "",
    }


def collect_bundles(folder, server, budget=None):
    paths = folder.list_paths_in_partition()
    is_prod = server.get("flow") == "LEGACY"
    folder_bundles = {}
    file_entries = []

    for path in paths:
        meta = parse_s3_path(path)
        if not meta:
            continue
        if meta.get("flow") != server.get("flow"):
            continue
        if is_prod and not meta.get("in_project_bundles"):
            continue
        if not is_prod and meta.get("nickname") != server.get("nickname"):
            continue

        is_folder_bundle = bool(meta.get("is_folder_bundle"))
        if not is_bundle_path(path, flow=meta.get("flow"), is_folder_bundle=is_folder_bundle):
            continue

        if is_folder_bundle:
            b_path = meta["bundle_path"]
            info = folder_bundles.get(b_path)
            if info is None:
                info = {
                    "meta": meta,
                    "child_count": 0,
                    "sample_first": deque(maxlen=SIZE_SAMPLE_PER_BUNDLE),
                    "sample_last": deque(maxlen=SIZE_SAMPLE_PER_BUNDLE),
                }
                folder_bundles[b_path] = info
            info["child_count"] += 1
            info["sample_first"].append(path)
            info["sample_last"].append(path)
            continue

        file_entries.append((path, meta))

    rows = []
    skipped_no_date = 0

    for path, meta in file_entries:
        bundle_label = meta.get("bundle_name") or meta.get("filename", "")
        name_date = extract_date_from_name(bundle_label)
        last_modified_ms = 0
        file_size_bytes = 0

        if budget is None or budget.can_call():
            try:
                if budget is not None:
                    budget.consume()
                details = folder.get_path_details(path) or {}
                last_modified_ms = details.get("lastModified", 0) or 0
                file_size_bytes = details.get("size", 0) or 0
            except Exception:
                pass

        creation_date = name_date
        if last_modified_ms > 0:
            creation_date = datetime.fromtimestamp(last_modified_ms / 1000.0)

        if creation_date is None:
            skipped_no_date += 1
            continue

        rows.append(_inventory_row(
            meta, path, creation_date, file_size_bytes, server,
            bundle_label, False, 0,
        ))

    for b_path, info in folder_bundles.items():
        meta = info["meta"]
        bundle_name = meta.get("bundle_name") or meta.get("filename", "")
        creation_date, total_size_bytes = get_folder_bundle_details(
            folder, b_path, info, bundle_name, budget=budget
        )
        if creation_date is None:
            skipped_no_date += 1
            continue
        rows.append(_inventory_row(
            meta, b_path, creation_date, total_size_bytes, server,
            bundle_name, True, info.get("child_count", 0),
        ))

    warnings = []
    if skipped_no_date > 0:
        warnings.append(
            f"{skipped_no_date} bundle(s) del folder {S3_FOLDER_NAME} se omitieron por no poder determinar su fecha"
        )
    if budget is not None and budget.calls >= budget.max_calls:
        warnings.append(
            f"Se alcanzó el límite de {budget.max_calls} consultas de detalle; "
            "algunos tamaños son estimados u omitidos."
        )
    if budget is not None and budget.out_of_time():
        warnings.append(
            f"El escaneo superó {budget.time_budget_s:.0f}s; los resultados pueden ser parciales."
        )

    return rows, warnings


def build_inventory(folder, server=None, budget=None, now=None):
    server = server or detect_current_server()
    rows, warnings = collect_bundles(folder, server, budget=budget)
    now = now or datetime.now()
    scan_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    host = (server.get("hostname") or "").lower()
    for row in rows:
        row["scan_ts"] = scan_ts
        row["scan_host"] = host
    return rows, warnings


def load_inventory(dataset_name=INVENTORY_DATASET_NAME):
    import dataiku

    dataset = dataiku.Dataset(dataset_name)
    df = dataset.get_dataframe()
    if df is None or df.empty:
        return []
    return df.to_dict(orient="records")


def save_inventory(rows, dataset_name=INVENTORY_DATASET_NAME):
    import dataiku
    import pandas as pd

    df = pd.DataFrame(rows, columns=INVENTORY_COLUMNS)
    dataset = dataiku.Dataset(dataset_name)
    dataset.write_with_schema(df)
    return len(df)


def trim_inventory(paths_to_remove, dataset_name=INVENTORY_DATASET_NAME):
    import dataiku

    dataset = dataiku.Dataset(dataset_name)
    df = dataset.get_dataframe()
    if df is None or df.empty:
        return 0

    remove = {str(p).rstrip("/") for p in paths_to_remove if p}
    if not remove:
        return len(df)

    keep_mask = ~df["s3_path"].astype(str).str.rstrip("/").isin(remove)
    remaining = df[keep_mask]
    dataset.write_with_schema(remaining)
    return len(remaining)


def latest_scan_ts(rows, server=None):
    latest = None
    for row in rows or []:
        if server is not None and not _row_belongs_to_server(row, server):
            continue
        ts = row.get("scan_ts")
        if ts is None or (isinstance(ts, float) and math.isnan(ts)):
            continue
        text = str(ts).strip()
        if not text or text.lower() in ("nat", "none", "null"):
            continue
        if latest is None or text > latest:
            latest = text
    return latest


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
        "size": round(_safe_number(row.get("size")), 2),
        "source_folder": row.get("source_folder") or S3_FOLDER_NAME,
    }


def split_inventory(rows, cutoff_date, server=None, now=None):
    now = now or datetime.now()
    bundles = []
    recent = []
    skipped = 0

    for row in rows or []:
        if server is not None and not _row_belongs_to_server(row, server):
            continue
        creation_date = coerce_creation_date(row.get("fecha_creacion_s3"))
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
            record["dias_para_limpieza"] = max(
                0, min(DEFAULT_CUTOFF_DAYS, days_left)
            )
            recent.append(record)

    return bundles, recent, skipped
