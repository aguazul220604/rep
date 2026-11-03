# ============================================================
# DATAGate Backend v6
# - Fixed os.path.splitext typo
# - /pending-approvals returns "uploads" key (matches frontend)
# - SNOW request number required on upload, validated on approve
# - DSS integration (dataiku-api-client): target-project dropdown,
#   dataset landing into "Landing Zone" on hdfs_work, Approver delete,
#   Approver project-dataset explorer, storage-increase requests
# ============================================================

import dataiku
import json
import os
import re
import hashlib
import importlib
import threading
import traceback
import tempfile
import mimetypes
from datetime import datetime
from flask import request, jsonify, send_from_directory, abort

try:
    from pii_engine.adapters.dataiku_adapter import DataikuAdapter, ModelArtifactError
    PII_ENGINE_AVAILABLE = True
    PII_IMPORT_ERROR = None
except Exception as _pii_exc:
    DataikuAdapter = None
    ModelArtifactError = RuntimeError
    PII_ENGINE_AVAILABLE = False
    PII_IMPORT_ERROR = f"{type(_pii_exc).__name__}: {_pii_exc}"

DRY_RUN = False  # Set True for dev, False for production Approver approval
STORAGE_THRESHOLD_GB = 5.0
OUTPUT_FOLDER_ID = "Outputs_Data_Gate"

# --- DSS integration (target-project landing) ---
DKU_API_KEY = os.environ.get("DKU_API_KEY", "mjfyCGdGxo3E3jiqc3gQWMPVPovYwJ07")
DKU_BASE_URL = os.environ.get("DKU_BASE_URL", "http://127.0.0.1:11000")
TARGET_CONNECTION = "hdfs_work"
LANDING_ZONE_NAME = "Landing Zone"

# --- local static assets (air-gapped Font Awesome / fonts) ----------------
# Absolute dir on the DSS box holding web assets, so no path is hard-coded.
# Export DATAGATE_ASSETS_DIR=/home/you/datagate_assets on the box; default
# ~/datagate_assets (resolves to whichever user runs DSS). The frontend asks the
# backend for /assets/fontawesome/css/all.min.css, served from here. Falls back
# to the public CDN when the dir is absent (local/online preview).
ASSETS_DIR = os.environ.get("DATAGATE_ASSETS_DIR") or os.path.expanduser("~/datagate_assets")
# Font MIME types can be missing in older Flask / sandboxed interpreters.
for _ext, _mt in ((".woff2", "font/woff2"), (".woff", "font/woff"),
                  (".ttf", "font/ttf"), (".otf", "font/otf"),
                  (".eot", "application/vnd.ms-fontobject"), (".svg", "image/svg+xml")):
    mimetypes.add_type(_mt, _ext)

# SNOW request number format: REQ/RITM/INC/CHG/PRB + 5-11 digits
# (e.g. REQ0012345, REQ0012345678, INC0012345678) - extended 2026-09-01 from
# 5-8 to cover the 10- and 11-digit REQs/INCs/CHGs ServiceNow now issues.
SNOW_REQUEST_REGEX = re.compile(r"^(REQ|RITM|INC|CHG|PRB)\d{5,11}$", re.IGNORECASE)

# Accepted upload formats. SAS/CSV stream chunk-by-chunk at landing; Excel
# workbooks are fully materialized in memory (pandas has no chunked Excel
# reader), which is why they carry their own, lower size cap.
SAS_CSV_EXTENSIONS = ('.sas7bdat', '.sas7bcat', '.xpt', '.csv')
EXCEL_EXTENSIONS = ('.xlsx', '.xlsm', '.xls')
EXCEL_MAX_BYTES = 500 * 1024 * 1024

# ============================================================
# MANAGED FOLDER
# ============================================================
try:
    output_folder = dataiku.Folder(OUTPUT_FOLDER_ID)
    FOLDER_AVAILABLE = True
    print(f"[DataGate] Connected to folder: {OUTPUT_FOLDER_ID}")
except Exception as e:
    output_folder = None
    FOLDER_AVAILABLE = False
    print(f"[DataGate] WARNING: Folder '{OUTPUT_FOLDER_ID}' unavailable: {e}")

# ============================================================
# PERSISTENT JSON DB
# ============================================================
SYSTEM_PREFIX = "system"
USERS_DB_PATH = f"{SYSTEM_PREFIX}/users.json"
RECORDS_DB_PATH = f"{SYSTEM_PREFIX}/upload_records.json"
STORAGE_REQ_DB_PATH = f"{SYSTEM_PREFIX}/storage_requests.json"

def _read_json_db(path, default):
    if not FOLDER_AVAILABLE:
        return default
    try:
        paths = output_folder.list_paths_in_partition()
        if "/" + path in paths or path in paths:
            with output_folder.get_download_stream(path) as stream:
                return json.loads(stream.read().decode('utf-8'))
    except:
        pass
    return default

def _write_json_db(path, data):
    if not FOLDER_AVAILABLE:
        return
    try:
        import io
        content = json.dumps(data, indent=2, default=str).encode('utf-8')
        output_folder.upload_stream(path, io.BytesIO(content))
    except Exception as e:
        print(f"[DataGate] DB write error ({path}): {e}")

# ============================================================
# LOAD DATA
# ============================================================
def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

DEFAULT_USERS = {
    "analyst.user": {
        "password_hash": hash_password("An@lyst2024!Sec"),
        "display_name": "Alex Analyst",
        "role": "user",
        "email": "alex.analyst@company.com",
        "deactivated": False
    },
    "Approver.admin": {
        "password_hash": hash_password("Cd@Approv3r!Xz"),
        "display_name": "Clara Approver",
        "role": "Approver",
        "email": "clara.approver@company.com",
        "deactivated": False
    }
}

USERS = _read_json_db(USERS_DB_PATH, DEFAULT_USERS)
if not USERS:
    USERS = DEFAULT_USERS
    _write_json_db(USERS_DB_PATH, USERS)

upload_records = _read_json_db(RECORDS_DB_PATH, [])
if upload_records is None:
    upload_records = []

storage_requests = _read_json_db(STORAGE_REQ_DB_PATH, [])
if storage_requests is None:
    storage_requests = []

def save_users():
    _write_json_db(USERS_DB_PATH, USERS)

# Landing runs on background threads (see _start_landing_async), so every
# upload_records read-modify-write and JSON DB flush funnels through one lock.
_RECORDS_LOCK = threading.Lock()
_LANDING_JOBS = {}  # upload_id -> live landing-job dict (see _start_landing_async)
_JOBS_LOCK = threading.Lock()

def save_records():
    with _RECORDS_LOCK:
        _write_json_db(RECORDS_DB_PATH, upload_records)

def save_storage_requests():
    _write_json_db(STORAGE_REQ_DB_PATH, storage_requests)

# ============================================================
# HELPERS
# ============================================================
def get_folder_usage():
    if not FOLDER_AVAILABLE:
        total = sum(r.get('file_size_bytes', 0) for r in upload_records if r['status'] not in ['rejected', 'deleted'])
        count = len([r for r in upload_records if r['status'] not in ['rejected', 'deleted']])
        return total, count

    try:
        total = 0
        count = 0
        for path in output_folder.list_paths_in_partition():
            if path.startswith("/" + SYSTEM_PREFIX) or path.startswith(SYSTEM_PREFIX):
                continue
            try:
                info = output_folder.get_path_details(path)
                total += info.get('size', 0)
                count += 1
            except:
                count += 1
        return total, count
    except:
        total = sum(r.get('file_size_bytes', 0) for r in upload_records if r['status'] not in ['rejected', 'deleted'])
        return total, len(upload_records)

def list_folder_files():
    files = []
    if not FOLDER_AVAILABLE:
        for r in upload_records:
            if r['status'] not in ['rejected', 'deleted']:
                files.append({'path': r.get('stored_path', r.get('filename')), 'size_bytes': r.get('file_size_bytes', 0), 'upload_date': r.get('upload_date', ''), 'status': r.get('status', 'unknown')})
        return files

    try:
        for path in output_folder.list_paths_in_partition():
            if path.startswith("/" + SYSTEM_PREFIX) or path.startswith(SYSTEM_PREFIX):
                continue
            try:
                info = output_folder.get_path_details(path)
                clean_path = path.lstrip('/')
                record = next((r for r in upload_records if r.get('stored_path') == clean_path), None)
                files.append({'path': clean_path, 'size_bytes': info.get('size', 0), 'upload_date': record['upload_date'] if record else '', 'status': record['status'] if record else 'unknown'})
            except:
                files.append({'path': path, 'size_bytes': 0, 'upload_date': '', 'status': 'unknown'})
        return files
    except:
        return files

def validate_password_strength(password):
    errors = []
    if len(password) < 12: errors.append("Min 12 characters")
    if not any(c.isupper() for c in password): errors.append("Need uppercase")
    if not any(c.islower() for c in password): errors.append("Need lowercase")
    if not any(c.isdigit() for c in password): errors.append("Need digit")
    if not any(c in '!@#$%^&*()_+-=[]{}|;:\'",.<>/?' for c in password): errors.append("Need special char")
    return errors

def validate_snow_request(value):
    """Normalize and validate a ServiceNow request number. Returns (normalized, error)."""
    normalized = (value or '').strip().upper()
    if not normalized:
        return None, "ServiceNow Request number is required."
    if not SNOW_REQUEST_REGEX.match(normalized):
        return None, f"Invalid SNOW request format: {normalized}. Expected REQ/RITM/INC/CHG/PRB + 5-11 digits, e.g. REQ0012345 or INC0012345678."
    return normalized, None

_PII_ADAPTER = None
_PII_ADAPTER_LOCK = threading.Lock()
_PII_MAX_ROWS = 50000
_PII_MAX_RECORD_COLUMNS = 200

def _get_pii_adapter():
    global _PII_ADAPTER
    if not PII_ENGINE_AVAILABLE:
        raise RuntimeError(f"PII engine unavailable: {PII_IMPORT_ERROR}")
    with _PII_ADAPTER_LOCK:
        if _PII_ADAPTER is None:
            _PII_ADAPTER = DataikuAdapter(
                models_folder=os.environ.get("PII_MODELS_FOLDER", "pii_models"),
                binary_model_path=os.environ.get("PII_BINARY_MODEL_PATH", "binary/model.pkl"),
                entity_model_path=os.environ.get("PII_ENTITY_MODEL_PATH", "entity/model.pkl"),
                content_model_path=os.environ.get("PII_CONTENT_MODEL_PATH", "content/model.pkl"),
                strict_models=True,
            )
        return _PII_ADAPTER

def _pii_ready_dataframe(df, max_rows=_PII_MAX_ROWS):
    """Normalize the validated DataFrame for the PII engine without changing the
    upload: string column names, decoded SAS bytes, unique names and a bounded
    row sample for very large SAS tables."""
    if df is None or getattr(df, "shape", (0, 0))[1] == 0:
        return df
    work = df
    try:
        if len(work) > max_rows:
            work = work.sample(n=max_rows, random_state=42)
    except Exception:
        work = work.head(max_rows)
    work = work.copy()

    seen = set()
    names = []
    for idx, original in enumerate(work.columns):
        base = str(original).strip() or f"column_{idx + 1}"
        name = base
        suffix = 2
        while name in seen:
            name = f"{base}_{suffix}"
            suffix += 1
        seen.add(name)
        names.append(name)
    work.columns = names

    for col in work.columns:
        series = work[col]
        if getattr(series, "dtype", None) == object:
            work[col] = series.map(
                lambda v: v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else v
            )
    return work

def _pii_summary_from_results(results):
    from pii_engine.core.schema import ENGINE_VERSION

    rows = []
    for result in results:
        d = result.to_dict()
        rows.append({
            "column": str(d.get("column") or ""),
            "pii": bool(d.get("pii")),
            "prob_pii": float(d.get("prob_pii") or 0.0),
            "entity": str(d.get("entity") or ""),
            "prob_entity": float(d.get("prob_entity") or 0.0),
            "evidence": str(d.get("evidence") or ""),
            "match_rate": float(d.get("match_rate") or 0.0),
            "sample_size": int(d.get("sample_size") or 0),
            "needs_review": bool(d.get("needs_review")),
            "classification": int(d.get("classification") or 1),
        })
    first = results[0].to_dict() if results else {}
    return {
        "status": "ok",
        "engine_version": str(first.get("engine_version") or ENGINE_VERSION),
        "model_version": str(first.get("model_version") or "rules_fallback"),
        "config_version": str(first.get("config_version") or "1.0"),
        "analyzed_columns": len(results),
        "pii_columns": sum(1 for r in results if bool(getattr(r, "pii", False))),
        "needs_review": sum(1 for r in results if bool(getattr(r, "needs_review", False))),
        "columns": rows,
    }

def _run_pii_analysis(df, table_name):
    """Run the PII engine over the already-parsed validation DataFrame. Never
    raises: validation must still succeed when the engine is unavailable."""
    if not PII_ENGINE_AVAILABLE:
        return {"status": "unavailable", "error": PII_IMPORT_ERROR}
    try:
        frame = _pii_ready_dataframe(df)
        if frame is None or getattr(frame, "shape", (0, 0))[1] == 0:
            return {"status": "unavailable", "error": "No tabular data to analyze."}
        results = _get_pii_adapter().analyze_dataframe(
            frame, table_name=str(table_name or "upload"), sample_size=2000
        )
        return _pii_summary_from_results(results)
    except Exception as exc:
        traceback.print_exc()
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

def _pii_float(value, default=0.0):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default

def _pii_int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default

def _sanitize_pii_summary(raw):
    """Whitelist the client-supplied summary before persisting it. Only metadata
    is accepted; any sample values or unknown fields are dropped."""
    if not isinstance(raw, dict):
        return {"status": "unavailable"}
    status = str(raw.get("status") or "unavailable").lower()
    if status != "ok":
        clean_status = status if status in ("error", "unavailable") else "unavailable"
        return {"status": clean_status, "error": str(raw.get("error") or "")[:300]}

    raw_columns = raw.get("columns")
    if not isinstance(raw_columns, list):
        raw_columns = []
    valid_columns = [item for item in raw_columns if isinstance(item, dict)]

    stored_columns = []
    for item in valid_columns:
        stored_columns.append({
            "column": str(item.get("column") or "")[:300],
            "pii": bool(item.get("pii")),
            "prob_pii": _pii_float(item.get("prob_pii")),
            "entity": str(item.get("entity") or "")[:100],
            "prob_entity": _pii_float(item.get("prob_entity")),
            "evidence": str(item.get("evidence") or "")[:40],
            "match_rate": _pii_float(item.get("match_rate")),
            "sample_size": _pii_int(item.get("sample_size")),
            "needs_review": bool(item.get("needs_review")),
            "classification": _pii_int(item.get("classification"), 1),
        })
        if len(stored_columns) >= _PII_MAX_RECORD_COLUMNS:
            break

    analyzed_columns = len(valid_columns) if valid_columns else _pii_int(raw.get("analyzed_columns"), 0)
    pii_columns = (sum(1 for c in valid_columns if c.get("pii")) if valid_columns
                   else _pii_int(raw.get("pii_columns"), 0))
    needs_review = (sum(1 for c in valid_columns if c.get("needs_review")) if valid_columns
                    else _pii_int(raw.get("needs_review"), 0))

    return {
        "status": "ok",
        "engine_version": str(raw.get("engine_version") or "")[:40],
        "model_version": str(raw.get("model_version") or "")[:300],
        "config_version": str(raw.get("config_version") or "")[:40],
        "analyzed_columns": analyzed_columns,
        "pii_columns": pii_columns,
        "needs_review": needs_review,
        "columns": stored_columns,
        "truncated": len(valid_columns) > _PII_MAX_RECORD_COLUMNS,
    }

# ============================================================
# DSS API (dataiku-api-client)
# ============================================================
def _dss_client():
    """Public-API DSS client. Uses DKU_API_KEY/DKU_BASE_URL env vars (per spec the
    key lives in .bashrc on the DSS box); falls back to the internal
    dataiku.api_client() inside DSS. Imports are lazy so the webapp still loads
    when dataiku-api-client isn't importable (e.g. local preview) - endpoints
    catch the failure and report success=False instead of crashing."""
    if DKU_API_KEY:
        import dataikuapi
        return dataikuapi.DSSClient(DKU_BASE_URL, DKU_API_KEY)
    return dataiku.api_client()

def _list_projects_payload():
    client = _dss_client()
    projects = []
    for p in client.list_projects():
        projects.append({"key": p.get("projectKey"), "name": p.get("name") or p.get("projectKey")})
    projects.sort(key=lambda x: (x["name"] or "").lower())
    return projects

def _read_excel_workbook(tmp_path, ext, sheet=None, nrows=None):
    """Read an Excel workbook with pandas. Returns (dataframe, sheet_names).
    `sheet` selects the sheet by name (None -> first sheet); `nrows` caps the
    parse for cheap validation samples (landing passes None for a full read).
    Raises RuntimeError with an actionable message when the reader engine
    (openpyxl for .xlsx/.xlsm, xlrd for legacy .xls) is missing from the
    webapp's Python environment or the requested sheet doesn't exist -
    callers degrade that to a failed validation / failed landing, never a 500."""
    import pandas as pd
    engine = 'xlrd' if ext == '.xls' else 'openpyxl'
    try:
        importlib.import_module(engine)
    except ImportError:
        label = 'legacy .xls' if ext == '.xls' else '.xlsx/.xlsm'
        raise RuntimeError(
            f"Excel ({label}) support needs the '{engine}' package in the webapp's "
            f"Python environment - ask your DSS admin to add it to the code env.")
    book = pd.ExcelFile(tmp_path, engine=engine)
    try:
        names = list(book.sheet_names)
        if not names:
            raise RuntimeError("Workbook has no sheets.")
        target = sheet or names[0]
        if target not in names:
            raise RuntimeError(
                f"Sheet '{target}' not found in workbook (available: {', '.join(names)}).")
        df = book.parse(target, nrows=nrows)
        return df, names
    finally:
        try:
            book.close()
        except Exception:
            pass

def _stream_rows_into_dataset(record, dss_dataset_name, job=None):
    """Write the stored upload into the target project's managed dataset,
    chunked so 5 GB files don't exhaust webapp memory. sas7bcat catalogs
    carry no tabular data -> reported as skipped. Excel workbooks are the
    exception: pandas has no chunked Excel reader, so the chosen sheet is
    fully materialized (bounded by EXCEL_MAX_BYTES at upload) and written
    in row-slices; its known row total makes the landing phase determinate.

    When `job` is not None (async landing, see _start_landing_async), progress
    is stamped into it for the /landing-status pollers: phase
    downloading -> landing, bytes/rows counters, determinate pct (downloading
    always; landing too for Excel, whose row total is known up-front - for
    CSV/SAS the total is unknowable without a second full parse). The final
    row count is ALSO stamped onto record['landing']: the live job dict is
    memory-only, the persisted record is the audit trail.

    Webapp/notebook context gotchas (vs a recipe): the core dataiku.Dataset
    writer uses DSS push-data streaming, whose session is bound to the current
    execution context. From a webapp that means three rules, all required to
    avoid `Streaming: push-data call failed: b'Session ... doesn't exist'`:
      * pass ignore_flow=True -- the dataset is not a declared recipe output,
        and may live in another project than the webapp's own;
      * set the schema BEFORE opening the writer (write_schema_from_dataframe),
        never let the writer infer schema during the first push;
      * use a `with` context manager so the writer always closes.
    """
    import pandas as pd
    ext = os.path.splitext(record.get('filename', ''))[1].lower()
    if ext == '.sas7bcat':
        if job is not None:
            job['phase'] = 'done'
        return "skipped: catalog file, no tabular data"
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp_path = tmp.name
    try:
        if FOLDER_AVAILABLE and record.get('stored_path'):
            if job is not None:
                job['phase'] = 'downloading'
            # Chunked download (was one whole-file read): keeps memory flat and
            # feeds the determinate download-phase bar. file_size_bytes comes
            # from the client, so the pct is clamped to 100 defensively.
            with output_folder.get_download_stream(record['stored_path']) as stream, open(tmp_path, 'wb') as out:
                while True:
                    buf = stream.read(4 * 1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
                    if job is not None:
                        job['bytes_downloaded'] += len(buf)
                        total = job.get('file_size_bytes') or 0
                        job['pct'] = round(min(100.0, job['bytes_downloaded'] * 100.0 / total), 1) if total else None
        else:
            if job is not None:
                job['phase'] = 'done'
            return "skipped: stored file unavailable"
        # ignore_flow=True is mandatory in a webapp (no recipe I/O declaration)
        # and for a dataset in another project than the webapp's own.
        ds = dataiku.Dataset(dss_dataset_name, project_key=record['target_project'], ignore_flow=True)
        landing_total = None  # Excel knows the row total up-front -> determinate bar
        if ext == '.csv':
            chunks = pd.read_csv(tmp_path, chunksize=50000)
        elif ext == '.xpt':
            chunks = pd.read_sas(tmp_path, format='xport', chunksize=50000)
        elif ext in EXCEL_EXTENSIONS:
            # No chunked Excel reader exists: the chosen sheet is fully
            # materialized, then written in row-slices through the same chunk
            # contract as the streaming readers above.
            df_full, _ = _read_excel_workbook(tmp_path, ext, sheet=record.get('sheet'))
            landing_total = len(df_full)
            chunks = (df_full.iloc[i:i + 50000] for i in range(0, landing_total, 50000))
        else:  # .sas7bdat
            chunks = pd.read_sas(tmp_path, format='sas7bdat', chunksize=50000)
        try:
            first_chunk = next(chunks, None)
        except pd.errors.EmptyDataError:
            if job is not None:
                job['phase'] = 'done'
            return "ok (empty file, 0 rows written)"
        if first_chunk is None:
            if job is not None:
                job['phase'] = 'done'
            return "ok (empty file, 0 rows written)"
        if job is not None:
            job['phase'] = 'landing'
            # Excel: determinate (landing_total known). CSV/SAS: row totals are
            # unknown until exhaustion -> indeterminate.
            job['pct'] = 0.0 if landing_total else None

        rows_written = 0  # also stamped onto record['landing'] - the live job dict is memory-only

        def _bump(n):
            nonlocal rows_written
            rows_written += n
            if job is not None:
                job['rows_written'] = rows_written
                if landing_total:
                    job['pct'] = round(min(100.0, rows_written * 100.0 / landing_total), 1)

        # Set the schema from the first chunk BEFORE opening the writer.
        # Writing into a schemaless freshly-created dataset is what destabilises
        # the push-data session on the first write_dataframe call.
        ds.write_schema_from_dataframe(first_chunk)
        with ds.get_writer() as writer:
            writer.write_dataframe(first_chunk)
            _bump(len(first_chunk))
            for chunk in chunks:
                writer.write_dataframe(chunk)
                _bump(len(chunk))
        # Persist the landed row count onto the record (improvements.md item 6):
        # the in-memory landing job dies with the process, the audit trail must not.
        if record.get('landing') is not None:
            record['landing']['rows_written'] = rows_written
        return "ok"
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass

def _land_dataset_in_project(record, job=None):
    """Create a managed dataset on hdfs_work (CSV format) in the record's target
    project, stream the uploaded data into it, and move it into the project's
    "Landing Zone" flow zone. Never raises: outcome is stamped on
    record['landing'] for the audit trail. `job` (optional) receives progress
    for /landing-status pollers; the terminal stamp happens at the end."""
    landing = {"project": record.get('target_project'), "dataset": None,
               "connection": TARGET_CONNECTION, "zone": LANDING_ZONE_NAME,
               "status": "skipped", "detail": "no target project selected"}
    record['landing'] = landing
    target_project = record.get('target_project')
    if not target_project:
        return landing
    try:
        client = _dss_client()
        project = client.get_project(target_project)
        safe_name = "".join(c for c in record.get('dataset_name', 'dataset') if c.isalnum() or c in ('_', '-')) or 'dataset'
        ds_name = f"dg_{safe_name}".lower()
        existing = {d.get('name') for d in project.list_datasets()}
        if ds_name in existing:
            ds_name = f"{ds_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        helper = project.new_managed_dataset(ds_name)
        helper.with_store_into(TARGET_CONNECTION)
        dss_dataset = helper.create()
        try:  # CSV is the required default format; apply best-effort (DSS-version tolerant)
            settings = dss_dataset.get_settings()
            if hasattr(settings, "set_csv_format"):
                settings.set_csv_format()
            else:
                settings.format_type = 'csv'
                settings.format_params = {"style": "excel", "parseHeaderRow": True}
            settings.save()
        except Exception as fmt_err:
            print(f"[DataGate] CSV format set warning ({ds_name}): {fmt_err}")
        landing["dataset"] = ds_name
        landing["status"] = "created"

        landing["detail"] = _stream_rows_into_dataset(record, ds_name, job=job)  # "ok" | "skipped: ..."

        if landing["detail"].startswith("ok"):
            # "ok" or "ok (empty file, 0 rows written)" - an empty upload still
            # created the dataset; reporting it as a mid-flight 'created' status
            # would later read as "Landing interrupted" from the persisted record.
            landing["status"] = "ok"
        elif landing["detail"].startswith("skipped"):
            landing["status"] = "ok"  # dataset exists; data write consciously skipped

        if job is not None:
            job['phase'] = 'moving_zone'
        try:
            dss_dataset.move_to_zone(LANDING_ZONE_NAME)
        except Exception as zone_err:
            landing["detail"] = f"{landing['detail']}; zone move failed: {zone_err}"

    except Exception as e:
        traceback.print_exc()
        landing["status"] = "error"
        landing["detail"] = str(e)

    if job is not None:
        # Terminal stamp - /landing-status reads this as the final state.
        job['phase'] = 'done' if landing['status'] != 'error' else 'error'
        job['pct'] = 100.0 if landing['status'] == 'ok' else None
        job['landing_status'] = landing['status']
        job['detail'] = landing['detail']
        job['error'] = landing['detail'] if landing['status'] == 'error' else None
        job['done'] = True
    return landing

# --- async landing worker ------------------------------------------------
# Landings used to run synchronously inside the HTTP request; a multi-GB file
# meant a silent spinner for minutes (Approver) or an apparently-dead upload page.
# Now the request stamps/saves the record first, spawns this worker, and the
# frontend polls /landing-status for the job dict the worker keeps fresh.

def _prune_landing_jobs():
    """Drop terminal jobs older than an hour (called on each new job) so the
    in-memory registry stays bounded."""
    now = datetime.now().timestamp()
    with _JOBS_LOCK:
        for uid in [u for u, j in _LANDING_JOBS.items()
                    if j.get('done') and j.get('finished_at') and now - j['finished_at'] > 3600]:
            _LANDING_JOBS.pop(uid, None)

def _landing_worker(record):
    job = _LANDING_JOBS.get(record['id'])
    try:
        _land_dataset_in_project(record, job=job)
    except Exception as e:  # belt and braces - _land_dataset_in_project never raises
        traceback.print_exc()
        if job is not None:
            job.update(phase='error', done=True, error=str(e),
                       detail=str(e), landing_status='error')
    finally:
        if job is not None:
            job['done'] = True
            job['finished_at'] = datetime.now().timestamp()
        save_records()  # persists record['landing']; save_records is lock-guarded

def _start_landing_async(record):
    """Start landing `record` on a background thread. Returns True when the
    thread started (frontend should poll /landing-status); on any thread
    failure, lands synchronously (old behavior) and returns False."""
    _prune_landing_jobs()
    job = {"upload_id": record['id'], "phase": "queued", "pct": None,
           "bytes_downloaded": 0, "file_size_bytes": record.get('file_size_bytes'),
           "rows_written": 0, "detail": "", "landing_status": None,
           "done": False, "error": None, "finished_at": None}
    with _JOBS_LOCK:
        _LANDING_JOBS[record['id']] = job
    try:
        threading.Thread(target=_landing_worker, args=(record,), daemon=True).start()
        return True
    except Exception:
        traceback.print_exc()
        with _JOBS_LOCK:
            _LANDING_JOBS.pop(record['id'], None)
        _land_dataset_in_project(record)
        save_records()
        return False

# ============================================================
# AUTH
# ============================================================
# Approver approvers are identified purely by DSS group membership (improvements.md
# item 4, 2026-09-01): a viewer belonging to the approver group enters as a
# Approver approver, everyone else - regardless of licensing profile (designer,
# reader, ...) or platform-admin rights - enters as a standard user.
# The request names the group in two spellings; keep both in sync with the
# group actually created on the DSS instance (matching is case-insensitive).
DEFAULT_APPROVER_GROUPS = ("DataGate_appv_team", "datagate_approver_team")

def _approver_group_names():
    """Group names whose members are Approver approvers: the built-in DataGate
    approver groups plus any extra names configured via the optional
    DATAGATE_ADMIN_GROUPS env var (comma-separated)."""
    configured = {g.strip() for g in os.environ.get("DATAGATE_ADMIN_GROUPS", "").split(",") if g.strip()}
    return configured | set(DEFAULT_APPROVER_GROUPS)

def _manual_login_allowed():
    """Manual username/password sign-in (the analyst.user / Approver.admin demo
    accounts) exists ONLY for the local standalone preview: there the shimmed
    `dataiku` module has no `api_client`. On a real DSS deployment, START
    (/auth/whoami DSS profile detection) is the ONLY way in - improvements.md
    item 8: on the public webapp, anyone on the intranet could otherwise sign
    in as the Approver.admin demo account. DATAGATE_ALLOW_MANUAL_LOGIN=1 is an
    explicit admin override (kept off on production)."""
    if os.environ.get("DATAGATE_ALLOW_MANUAL_LOGIN", "").strip().lower() in ("1", "true", "yes"):
        return True
    return getattr(dataiku, "api_client", None) is None

def _viewer_auth_info():
    """Identify the DSS user *viewing* the webapp from the browser request
    headers (the DSS proxy forwards their session cookie). Returns None when
    unavailable: local preview (the dataiku shim has no api_client), an older
    DSS without get_auth_info_from_browser_headers, or the "hide access
    tokens" security setting. NOTE: dataiku.api_client().get_auth_info() with
    no arguments would report the backend's run-as user, NOT the viewer."""
    try:
        api_client = getattr(dataiku, "api_client", None)
        if api_client is None:
            return None
        return api_client().get_auth_info_from_browser_headers(dict(request.headers))
    except Exception as e:
        print(f"[DataGate] viewer auth-info failed: {e}")
        return None

def _dss_user_details(client, login):
    """(display_name, profile, email, groups) of a DSS login, or
    (None, None, None, []) when the lookup fails (non-admin API key, ...).
    userProfile is DSS's "licensing" profile (DESIGNER, READER, ...), not
    admin rights."""
    try:
        user = client.get_user(login)
        try:
            d = user.get_definition()
        except Exception:
            d = user.get_settings().get_raw()
        return (d.get("displayName") or None, d.get("userProfile") or None,
                d.get("email") or None, list(d.get("groups") or []))
    except Exception as e:
        print(f"[DataGate] DSS user lookup failed for '{login}': {e}")
        return (None, None, None, [])

def _viewer_role(login, auth_groups, client):
    """Map a detected DSS viewer to a DataGate role: Approver approver iff the
    viewer belongs to an approver group (DEFAULT_APPROVER_GROUPS, plus the
    optional DATAGATE_ADMIN_GROUPS env var), standard user otherwise -
    DSS licensing profiles and platform-admin flags no longer grant any role
    (improvements.md item 4). The browser-headers auth info already carries
    the viewer's group list; the user-definition lookup (needs a capable API
    key) only completes display name / profile / email and the groups when
    the auth info omitted them. Returns
    (role, is_approver, profile, display_name, email, groups); never raises."""
    display_name = profile = email = None
    groups = list(auth_groups or [])

    if client is not None:
        display_name, profile, email, user_groups = _dss_user_details(client, login)
        for g in (user_groups or []):
            if g not in groups:
                groups.append(g)

    approver_groups = {str(g).lower() for g in _approver_group_names()}
    is_approver = any(str(g).lower() in approver_groups for g in groups)

    return ("Approver" if is_approver else "user"), is_approver, profile, display_name, email, groups

@app.route('/auth/whoami', methods=['GET'])
def auth_whoami():
    """Single sign-on: identify the DSS user viewing the webapp and route
    them to a role by GROUP membership - members of the DataGate approver
    group (DataGate_appv_team) enter as Approver approvers, everyone else as a
    standard user. detected=False splits two honest cases (item 8): on the
    local standalone preview (manual_login_allowed=True) the demo accounts
    appear; on a real DSS deployment the viewer is simply not signed in to
    DSS and the UI points them at the DSS login (SSO) - no fallback users."""
    try:
        auth_info = _viewer_auth_info()
        login = (auth_info or {}).get("authIdentifier") or ""
        if not login:
            if auth_info is None:
                reason = ("Local preview: no DSS profile detection here - sign in with a demo account."
                          if _manual_login_allowed() else
                          "No Dataiku DSS session in this browser - sign in to Dataiku DSS first, then retry.")
            else:
                reason = "DSS could not identify the webapp viewer."
            return jsonify({"success": True, "detected": False, "reason": reason,
                            "manual_login_allowed": _manual_login_allowed(), "dry_run": DRY_RUN})

        client = None
        try:
            client = _dss_client()
        except Exception as e:
            print(f"[DataGate] DSS client unavailable for whoami: {e}")

        role, is_approver, profile, display_name, email, groups = \
            _viewer_role(login, (auth_info or {}).get("groups"), client)

        # Upsert the viewer into USERS so every existing role lookup
        # (/upload-sas auto-approve, /storage-requests, /my-uploads, ...)
        # keeps working with zero route changes. DSS is the source of truth
        # for display name and role; password/email/deactivated are kept.
        user = USERS.get(login)
        if not user:
            user = {
                "display_name": display_name or login,
                "role": role,
                "email": email or "",
                "deactivated": False,
                "source": "dss",
                "dss_profile": profile or ""
            }
            USERS[login] = user
            save_users()
        elif (user.get("display_name") != (display_name or login)
              or user.get("role") != role
              or user.get("dss_profile") != (profile or "")):
            user["display_name"] = display_name or login
            user["role"] = role
            user["dss_profile"] = profile or ""
            save_users()

        return jsonify({
            "success": True,
            "detected": True,
            "user": {
                "username": login,
                "display_name": user.get("display_name") or login,
                "role": user.get("role", role),
                "email": user.get("email", ""),
                "deactivated": user.get("deactivated", False)
            },
            "dss": {"profile": profile or "", "groups": groups, "is_approver": is_approver},
            "manual_login_allowed": _manual_login_allowed(),
            "dry_run": DRY_RUN
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": True, "detected": False, "reason": str(e),
                        "manual_login_allowed": _manual_login_allowed(), "dry_run": DRY_RUN})

@app.route('/auth/login', methods=['POST'])
def login():
    try:
        # improvements.md item 8: no demo-account sign-in on a real DSS
        # deployment - START via DSS profile detection is the only way in.
        if not _manual_login_allowed():
            return jsonify({"success": False,
                            "error": "Manual sign-in is disabled on this deployment. "
                                     "Click START to sign in with your Dataiku DSS account."}), 403

        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '')
        if not username or not password:
            return jsonify({"success": False, "error": "Username and password required."}), 400
        user = USERS.get(username)
        if not user:
            return jsonify({"success": False, "error": "Invalid username or password."}), 401
        # DSS-synced users (see /auth/whoami) carry no password_hash: they
        # sign in via START, never with a password.
        if not user.get("password_hash") or user["password_hash"] != hash_password(password):
            return jsonify({"success": False, "error": "Invalid username or password."}), 401
        return jsonify({
            "success": True,
            "user": {
                "username": username,
                "display_name": user["display_name"],
                "role": user["role"],
                "email": user.get("email", ""),
                "deactivated": user.get("deactivated", False)
            },
            "dry_run": DRY_RUN
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================
# VALIDATE FILE
# ============================================================
@app.route('/validate-file', methods=['POST'])
def validate_file():
    try:
        if 'file' not in request.files:
            return jsonify({"valid": False, "error": "No file"}), 400
        file = request.files['file']
        ext = os.path.splitext(file.filename)[1].lower()
        import pandas as pd
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        df = None; checks = []; error_msg = None; excel_sheets = None
        try:
            if ext == '.sas7bdat':
                try:
                    df = pd.read_sas(tmp_path, format='sas7bdat', encoding='utf-8')
                    checks.append({"label": "SAS7BDAT format valid", "status": "pass"})
                except:
                    try:
                        df = pd.read_sas(tmp_path, format='sas7bdat')
                        checks.append({"label": "SAS7BDAT format valid", "status": "pass"})
                        checks.append({"label": "Encoding: fallback", "status": "warn"})
                    except Exception as e:
                        error_msg = str(e); checks.append({"label": "SAS7BDAT format", "status": "fail"})
            elif ext == '.xpt':
                try:
                    df = pd.read_sas(tmp_path, format='xport')
                    checks.append({"label": "XPT format valid", "status": "pass"})
                except Exception as e:
                    error_msg = str(e); checks.append({"label": "XPT format", "status": "fail"})
            elif ext == '.sas7bcat':
                with open(tmp_path, 'rb') as f: header = f.read(64)
                if len(header) >= 64:
                    checks.append({"label": "SAS7BCAT detected", "status": "pass"})
                    return jsonify({"valid": True, "file_type": "sas7bcat", "row_count": 0, "col_count": 0, "checks": checks, "preview": []})
                return jsonify({"valid": False, "error": "Invalid catalog file"})
            elif ext == '.csv':
                try:
                    df = pd.read_csv(tmp_path, nrows=1000)
                    checks.append({"label": "CSV format valid", "status": "pass"})
                except Exception as e:
                    error_msg = str(e); checks.append({"label": "CSV format", "status": "fail"})
            elif ext in EXCEL_EXTENSIONS:
                try:
                    if os.path.getsize(tmp_path) > EXCEL_MAX_BYTES:
                        return jsonify({"valid": False, "error": f"Excel file exceeds the {EXCEL_MAX_BYTES // (1024 * 1024)} MB limit (workbooks are fully loaded into memory when landing)."}), 400
                    # nrows=1000: counts/preview from a sample, like the CSV branch
                    df, excel_sheets = _read_excel_workbook(tmp_path, ext, nrows=1000)
                    shown = ', '.join(excel_sheets[:5]) + (f" (+{len(excel_sheets) - 5} more)" if len(excel_sheets) > 5 else "")
                    checks.append({"label": f"{ext.lstrip('.').upper()} workbook readable", "status": "pass"})
                    checks.append({"label": f"Sheets: {len(excel_sheets)} ({shown})", "status": "pass"})
                except Exception as e:
                    error_msg = str(e); checks.append({"label": "Excel workbook", "status": "fail"})
            else:
                return jsonify({"valid": False, "error": f"Unsupported: {ext}"}), 400

            if df is None:
                return jsonify({"valid": False, "error": error_msg or "Cannot read", "checks": checks})

            rc, cc = len(df), len(df.columns)
            checks.append({"label": f"{rc} rows", "status": "pass" if rc > 0 else "fail"})
            checks.append({"label": f"{cc} columns", "status": "pass" if cc > 0 else "fail"})
            empty_cols = [c for c in df.columns if df[c].isna().all()]
            checks.append({"label": f"{len(empty_cols)} empty cols", "status": "warn" if empty_cols else "pass"})
            nc = len(df.select_dtypes(include='number').columns)
            checks.append({"label": f"types: {nc} numeric, {cc-nc} text", "status": "pass"})

            preview = {"columns": list(df.head(5).columns), "rows": json.loads(df.head(5).to_json(orient='records', date_format='iso'))}
            payload = {"valid": rc > 0 and cc > 0, "file_type": ext.replace('.', ''), "row_count": rc, "col_count": cc, "checks": checks, "preview": preview}
            if ext in EXCEL_EXTENSIONS:
                payload["sheets"] = excel_sheets  # sheet-picker data for the frontend
            payload["pii"] = _run_pii_analysis(df, file.filename)
            return jsonify(payload)
        finally:
            try:
                os.unlink(tmp_path)
            except:
                pass
    except Exception as e:
        traceback.print_exc()
        return jsonify({"valid": False, "error": str(e)}), 500

# ============================================================
# UPLOAD
# ============================================================
@app.route('/upload-sas', methods=['POST'])
def upload_sas():
    global upload_records
    try:
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "No file"}), 400
        file = request.files['file']
        metadata = json.loads(request.form.get('metadata', '{}'))
        pii_summary = _sanitize_pii_summary(metadata.get('pii_summary'))
        file_size = int(request.form.get('file_size_bytes', 0))
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in SAS_CSV_EXTENSIONS + EXCEL_EXTENSIONS:
            return jsonify({"success": False, "error": f"Invalid: {ext}"}), 400
        if ext in EXCEL_EXTENSIONS and file_size > EXCEL_MAX_BYTES:
            return jsonify({"success": False, "error": f"Excel file exceeds the {EXCEL_MAX_BYTES // (1024 * 1024)} MB limit (workbooks are fully loaded into memory when landing)."}), 400

        # --- ServiceNow request number is mandatory for every petition ---
        snow_request, snow_err = validate_snow_request(metadata.get('snow_request'))
        if snow_err:
            return jsonify({"success": False, "error": snow_err}), 400

        uploaded_by = metadata.get('uploaded_by', 'unknown')
        user = USERS.get(uploaded_by, {})
        if user.get('deactivated', False):
            return jsonify({"success": False, "error": "Your account is deactivated. Contact Approver."}), 403

        used_bytes, _ = get_folder_usage()
        used_gb = used_bytes / (1024**3)
        file_gb = file_size / (1024**3)
        if used_gb + file_gb > STORAGE_THRESHOLD_GB:
            return jsonify({"success": False, "error": f"Exceeds {STORAGE_THRESHOLD_GB} GB storage threshold."}), 400

        user_role = user.get('role', 'user')
        status = 'uploaded' if DRY_RUN else ('approved' if user_role == 'Approver' else 'pending_approval')

        dataset_name = metadata.get('dataset_name', 'unnamed')
        safe_name = "".join(c for c in dataset_name if c.isalnum() or c in ('_', '-')) or 'dataset'
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        stored_path = f"{uploaded_by}/{safe_name}/{ts}/{file.filename}"

        upload_ok = False
        if FOLDER_AVAILABLE:
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                file.save(tmp.name); tmp_path = tmp.name
            try:
                # Authoritative size check: file_size_bytes is client-declared,
                # and landing fully materializes Excel sheets in memory.
                if ext in EXCEL_EXTENSIONS and os.path.getsize(tmp_path) > EXCEL_MAX_BYTES:
                    return jsonify({"success": False, "error": f"Excel file exceeds the {EXCEL_MAX_BYTES // (1024 * 1024)} MB limit (workbooks are fully loaded into memory when landing)."}), 400
                with open(tmp_path, 'rb') as fs:
                    output_folder.upload_stream(stored_path, fs)
                upload_ok = True
            finally:
                try:
                    os.unlink(tmp_path)
                except:
                    pass
        else:
            upload_ok = True

        if not upload_ok:
            return jsonify({"success": False, "error": "Save failed"}), 500

        record = {
            "id": f"UP-{len(upload_records)+1:04d}",
            "filename": file.filename,
            "dataset_name": dataset_name,
            "category": metadata.get('category', 'other'),
            "description": metadata.get('description', ''),
            "priority": metadata.get('priority', 'normal'),
            "target_schema": metadata.get('target_schema', 'STAGING'),
            "target_project": (metadata.get('target_project') or '').strip() or None,
            "snow_request": snow_request,
            "pii": pii_summary,
            "snow_validated": None,
            "snow_validated_by": None,
            "snow_validated_date": None,
            "file_size_bytes": file_size,
            "uploaded_by": uploaded_by,
            "upload_date": datetime.now().isoformat(),
            "status": status,
            "stored_path": stored_path,
            "landing": None,
            "sheet": (metadata.get('sheet') or None) if ext in EXCEL_EXTENSIONS else None,
            "approver_notes": "DRY RUN" if DRY_RUN else None,
            "approved_by": "SYSTEM" if DRY_RUN else (uploaded_by if user_role == 'Approver' else None),
            "approval_date": datetime.now().isoformat() if (DRY_RUN or user_role == 'Approver') else None
        }

        with _RECORDS_LOCK:
            upload_records.append(record)
        save_records()

        # Auto-approved uploads (DRY RUN / Approver uploader) land in the target
        # project's Landing Zone immediately; standard petitions land on approve.
        # Landing runs on a background worker -> the response is fast and the
        # frontend polls /landing-status for live progress.
        landing_started = False
        if status in ('uploaded', 'approved') and record['target_project']:
            landing_started = _start_landing_async(record)

        msg = "Uploaded (DRY RUN)." if DRY_RUN else ("Auto-approved (Approver)." if user_role == 'Approver' else "Submitted for Approver approval.")
        if landing_started:
            msg += " Landing to the target project started - watch the progress below."
        elif record.get('landing'):
            l = record['landing']
            if l['status'] == 'ok':
                msg += f" Landed as {l['project']}/{l['dataset']} ({LANDING_ZONE_NAME})."
            elif l['status'] == 'error':
                msg += f" Landing failed: {l['detail']}"
        return jsonify({"success": True, "message": msg, "record": record,
                        "landing_started": landing_started, "upload_id": record['id']})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================
# APPROVALS
# ============================================================
@app.route('/pending-approvals', methods=['GET'])
def pending_approvals():
    p = [r for r in upload_records if r['status'] == 'pending_approval']
    return jsonify({"success": True, "uploads": p, "count": len(p)})

@app.route('/review-upload', methods=['POST'])
def review_upload():
    global upload_records
    try:
        data = request.get_json()
        uid = data.get('upload_id'); action = data.get('action')
        notes = data.get('notes', ''); reviewer = data.get('reviewer', 'Approver')
        rec = next((r for r in upload_records if r['id'] == uid), None)
        if not rec: return jsonify({"success": False, "error": "Not found"}), 404
        if rec['status'] != 'pending_approval': return jsonify({"success": False, "error": "Already reviewed"}), 400

        if action == 'approve':
            # The Approver must attest that the SNOW request was validated in ServiceNow.
            if data.get('snow_validated') is not True:
                return jsonify({"success": False, "error": "ServiceNow validation attestation required before approving."}), 400
            rec['status'] = 'approved'
            rec['Approver_notes'] = notes
            rec['approved_by'] = reviewer
            rec['approval_date'] = datetime.now().isoformat()
            rec['snow_validated'] = True
            rec['snow_validated_by'] = reviewer
            rec['snow_validated_date'] = datetime.now().isoformat()
            save_records()  # the approval is durable BEFORE any landing attempt
            # Landing runs on a background worker so the Approver can watch progress
            # via /landing-status instead of waiting on a multi-minute request.
            landing_started = False
            if rec.get('target_project'):
                landing_started = _start_landing_async(rec)
            msg = f"{uid} approved ({rec.get('snow_request', 'SNOW')} validated)."
            if landing_started:
                msg += " Landing is in progress - watch the live status."
            elif rec.get('landing'):
                l = rec['landing']
                if l['status'] == 'ok':
                    msg += f" Landed as {l['project']}/{l['dataset']} ({LANDING_ZONE_NAME})."
                elif l['status'] == 'error':
                    msg += f" Landing failed: {l['detail']}"
            return jsonify({"success": True, "message": msg, "record": rec,
                            "landing_started": landing_started, "upload_id": rec['id']})
        elif action == 'reject':
            rec['status'] = 'rejected'; rec['Approver_notes'] = notes; rec['approved_by'] = reviewer; rec['approval_date'] = datetime.now().isoformat()
            if FOLDER_AVAILABLE and rec.get('stored_path'):
                try: output_folder.delete_path(rec['stored_path'])
                except: pass
            save_records()
            return jsonify({"success": True, "message": f"{uid} rejected."})
        return jsonify({"success": False, "error": "Invalid action"}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================
# DATA RETRIEVAL
# ============================================================
@app.route('/my-uploads', methods=['GET'])
def my_uploads():
    user = request.args.get('user', '')
    ups = sorted([r for r in upload_records if r['uploaded_by'] == user], key=lambda x: x['upload_date'], reverse=True)
    return jsonify({"success": True, "uploads": ups})

@app.route('/upload-history', methods=['GET'])
def upload_history():
    return jsonify({"success": True, "history": sorted(upload_records, key=lambda x: x['upload_date'], reverse=True), "total": len(upload_records)})

@app.route('/landing-status', methods=['GET'])
def landing_status():
    """Live landing progress for the frontend poller. Always HTTP 200.

    Primary source: the in-memory job a background _landing_worker keeps fresh.
    If the job is gone (webapp restarted / pruned / multi-process runner), fall
    back to the persisted record['landing'] and synthesize a terminal payload -
    a persisted mid-flight status ('created'/...) means the process died
    mid-landing, which is reported as an interrupted error, not stuck progress.
    """
    try:
        uid = request.args.get('upload_id', '')
        with _JOBS_LOCK:
            job = _LANDING_JOBS.get(uid)
        if job is not None:
            return jsonify({"success": True, "upload_id": uid, "phase": job.get('phase'),
                            "pct": job.get('pct'), "rows_written": job.get('rows_written'),
                            "bytes_downloaded": job.get('bytes_downloaded'),
                            "file_size_bytes": job.get('file_size_bytes'),
                            "detail": job.get('detail'), "landing_status": job.get('landing_status'),
                            "done": job.get('done', False), "error": job.get('error')})

        with _RECORDS_LOCK:
            rec = next((r for r in upload_records if r['id'] == uid), None)
        landing = rec.get('landing') if rec else None
        if landing:
            st = landing.get('status')
            interrupted = st not in ('ok', 'error')
            return jsonify({"success": True, "upload_id": uid,
                            "phase": 'done' if st == 'error' else 'error',
                            "pct": 100.0 if st == 'ok' else None,
                            "rows_written": landing.get('rows_written'), "bytes_downloaded": None,
                            "file_size_bytes": rec.get('file_size_bytes'),
                            "detail": landing.get('detail'), "landing_status": st, "done": True,
                            "error": "Landing interrupted (webapp restarted)" if interrupted else (landing.get('detail') if st == 'error' else None)})
        return jsonify({"success": False, "error": "Unknown upload id / no landing recorded"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)})

@app.route('/storage-info', methods=['GET'])
def storage_info():
    ub, fc = get_folder_usage()
    ug = ub / (1024**3)
    return jsonify({
        "success": True, "folder_id": OUTPUT_FOLDER_ID, "folder_available": FOLDER_AVAILABLE,
        "threshold_gb": STORAGE_THRESHOLD_GB, "used_gb": round(ug, 6), "available_gb": round(max(0, STORAGE_THRESHOLD_GB - ug), 6),
        "usage_percent": round(ug/STORAGE_THRESHOLD_GB * 100, 2) if STORAGE_THRESHOLD_GB > 0 else 0, "file_count": fc
    })

@app.route('/storage-files', methods=['GET'])
def storage_files():
    return jsonify({"success": True, "files": list_folder_files()})

# ============================================================
# ADMIN: USER MANAGEMENT
# ============================================================
@app.route('/admin/users', methods=['GET'])
def admin_users():
    users_list = [{"username": k, "display_name": v["display_name"], "role": v["role"], "email": v.get("email", ""), "deactivated": v.get("deactivated", False)} for k, v in USERS.items()]
    activity = sorted(upload_records, key=lambda x: x['upload_date'], reverse=True)
    return jsonify({"success": True, "users": users_list, "activity": activity})

@app.route('/admin/user-action', methods=['POST'])
def admin_user_action():
    try:
        data = request.get_json()
        username = data.get('username'); action = data.get('action')
        if username not in USERS:
            return jsonify({"success": False, "error": "User not found"}), 404
        if USERS[username]['role'] == 'Approver':
            return jsonify({"success": False, "error": "Cannot modify Approver accounts"}), 403

        if action == 'deactivate':
            USERS[username]['deactivated'] = True
            save_users()
            return jsonify({"success": True, "message": f"{username} deactivated."})
        elif action == 'activate':
            USERS[username]['deactivated'] = False
            save_users()
            return jsonify({"success": True, "message": f"{username} activated."})
        elif action == 'delete':
            del USERS[username]
            save_users()
            return jsonify({"success": True, "message": f"{username} deleted."})
        return jsonify({"success": False, "error": "Invalid action"}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/admin/create-user', methods=['POST'])
def admin_create_user():
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        display_name = data.get('display_name', '').strip()
        password = data.get('password', '')
        email = data.get('email', '').strip()
        role = data.get('role', 'user')

        if not username or not display_name or not password:
            return jsonify({"success": False, "error": "All fields required"}), 400
        if username in USERS:
            return jsonify({"success": False, "error": f"Username '{username}' already exists"}), 400

        pw_errors = validate_password_strength(password)
        if pw_errors:
            return jsonify({"success": False, "error": "Password too weak: " + ", ".join(pw_errors)}), 400

        USERS[username] = {
            "password_hash": hash_password(password),
            "display_name": display_name,
            "role": role,
            "email": email,
            "deactivated": False
        }
        save_users()
        return jsonify({"success": True, "message": f"User '{username}' created."})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================
# DSS PROJECTS / DATASETS (dataiku-api-client)
# ============================================================
@app.route('/dss-projects', methods=['GET'])
def dss_projects():
    """Project list for the upload form dropdown and the Approver explorer.
    Returns HTTP 200 with success=False when the DSS API is unreachable so the
    frontend can degrade gracefully instead of erroring."""
    try:
        return jsonify({"success": True, "projects": _list_projects_payload()})
    except Exception as e:
        print(f"[DataGate] /dss-projects unavailable: {e}")
        return jsonify({"success": False, "error": f"DSS API unavailable: {e}", "projects": []})

@app.route('/dss-project-datasets', methods=['GET'])
def dss_project_datasets():
    """Datasets of one project with their connections (Approver explorer)."""
    try:
        project_key = (request.args.get('project') or '').strip()
        if not project_key:
            return jsonify({"success": False, "error": "project parameter required", "datasets": []}), 400
        project = _dss_client().get_project(project_key)
        datasets = []
        for d in project.list_datasets():
            datasets.append({
                "name": d.get('name'),
                "type": d.get('type', ''),
                "connection": (d.get('params') or {}).get('connection') or '-'
            })
        datasets.sort(key=lambda x: (x["name"] or "").lower())
        return jsonify({"success": True, "project": project_key, "datasets": datasets})
    except Exception as e:
        print(f"[DataGate] /dss-project-datasets error: {e}")
        return jsonify({"success": False, "error": f"DSS API unavailable: {e}", "datasets": []})

# ============================================================
# STORAGE INCREASE REQUESTS
# ============================================================
@app.route('/storage-requests', methods=['POST'])
def storage_request_create():
    """User asks the admins for a higher storage cap. Every such request needs a
    ServiceNow REQ number, like any other admin-bound request."""
    global storage_requests
    try:
        data = request.get_json()
        requested_gb = data.get('requested_gb')
        try:
            requested_gb = float(requested_gb)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Requested limit must be a number of GB."}), 400
        if requested_gb <= STORAGE_THRESHOLD_GB:
            return jsonify({"success": False, "error": f"Requested limit must be above the current {STORAGE_THRESHOLD_GB} GB threshold."}), 400

        snow_request, snow_err = validate_snow_request(data.get('snow_request'))
        if snow_err:
            return jsonify({"success": False, "error": snow_err}), 400

        requested_by = (data.get('requested_by') or '').strip()
        if requested_by not in USERS:
            return jsonify({"success": False, "error": "Unknown requester."}), 400

        req = {
            "id": f"SR-{len(storage_requests)+1:04d}",
            "requested_gb": requested_gb,
            "current_gb": STORAGE_THRESHOLD_GB,
            "snow_request": snow_request,
            "requested_by": requested_by,
            "request_date": datetime.now().isoformat(),
            "status": "open"
        }
        storage_requests.append(req)
        save_storage_requests()
        return jsonify({"success": True, "message": f"Storage request {req['id']} sent to admins ({requested_gb} GB, {snow_request}).", "request": req})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================
# ADMIN: DELETE UPLOADED DATASETS (any status)
# ============================================================
@app.route('/admin/delete-upload', methods=['POST'])
def admin_delete_upload():
    """Approver delete: removes the DSS dataset landed in the target project (if any)
    and the stored file from the output folder. The record is kept with
    status 'deleted' for the audit trail."""
    global upload_records
    try:
        data = request.get_json()
        uid = data.get('upload_id')
        deleted_by = data.get('deleted_by', 'Approver')
        rec = next((r for r in upload_records if r['id'] == uid), None)
        if not rec:
            return jsonify({"success": False, "error": "Not found"}), 404
        if rec.get('status') == 'deleted':
            return jsonify({"success": False, "error": "Already deleted"}), 400

        details = []
        landing = rec.get('landing') or {}
        if landing.get('dataset') and landing.get('project'):
            try:
                client = _dss_client()
                client.get_project(landing['project']).get_dataset(landing['dataset']).delete(drop_data=True)
                details.append(f"DSS dataset {landing['project']}/{landing['dataset']} deleted")
            except Exception as ds_err:
                details.append(f"DSS dataset delete failed: {ds_err}")
        if FOLDER_AVAILABLE and rec.get('stored_path'):
            try:
                output_folder.delete_path(rec['stored_path'])
                details.append("stored file removed")
            except Exception as f_err:
                details.append(f"stored file removal failed: {f_err}")

        rec['status'] = 'deleted'
        rec['deleted_by'] = deleted_by
        rec['deleted_date'] = datetime.now().isoformat()
        save_records()
        msg = f"{uid} deleted."
        if details:
            msg += " " + "; ".join(details) + "."
        return jsonify({"success": True, "message": msg})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================
# LOCAL ASSETS (air-gapped Font Awesome / fonts)
# ============================================================
@app.route('/assets/<path:asset_path>')
def serve_asset(asset_path):
    """Serve a local web asset from ASSETS_DIR so the webapp renders its icons on
    an air-gapped DSS box. Mirrors the on-disk tree: a request for
    /assets/fontawesome/css/all.min.css reads ASSETS_DIR/fontawesome/css/all.min.css;
    the CSS's relative ../webfonts/*.woff2 fizz resolves back through this same route,
    so keep the downloaded Font Awesome directory structure intact. 404 (not 500)
    if ASSETS_DIR is missing -> the frontend falls back to the public CDN."""
    if not ASSETS_DIR:
        abort(404)
    return send_from_directory(ASSETS_DIR, asset_path, conditional=True)

# ============================================================
# HEALTH
# ============================================================
@app.route('/health', methods=['GET'])
def health():
    ub, fc = get_folder_usage()
    return jsonify({
        "status": "ok", "dry_run": DRY_RUN, "storage_threshold_gb": STORAGE_THRESHOLD_GB,
        "output_folder": OUTPUT_FOLDER_ID, "folder_available": FOLDER_AVAILABLE, "uploads_count": len(upload_records),
        "files_in_folder": fc, "used_gb": round(ub/(1024**3), 6)
    })

@app.route('/pii/health', methods=['GET'])
def pii_health():
    """Readiness probe for the PII engine, loaded models and rule catalogs."""
    if not PII_ENGINE_AVAILABLE:
        return jsonify({"status": "degraded", "engine_ready": False, "error": PII_IMPORT_ERROR}), 503
    try:
        info = _get_pii_adapter().health()
    except Exception as exc:
        traceback.print_exc()
        return jsonify({
            "status": "degraded", "engine_ready": False,
            "error": f"{type(exc).__name__}: {exc}"
        }), 503
    return jsonify(info), 200 if info.get("engine_ready") else 503
