import io
import logging
import os
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from flask import Response, jsonify, request

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    _XLSX_ERROR = None
except Exception as _exc_xlsx:
    Workbook = None
    _XLSX_ERROR = f"{type(_exc_xlsx).__name__}: {_exc_xlsx}"

try:
    _HERE = Path(__file__).resolve().parent
except NameError:
    _HERE = None

_PROJECT_ROOT = os.getenv("PII_PROJECT_ROOT")
_PATH_CANDIDATES = []
if _HERE is not None:
    _PATH_CANDIDATES.extend([_HERE.parent, _HERE])
if _PROJECT_ROOT:
    _PATH_CANDIDATES.append(Path(_PROJECT_ROOT))
for _candidate in _PATH_CANDIDATES:
    if _candidate.exists() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

try:
    from pii_engine.adapters.dataiku_adapter import (
        DataikuAdapter,
        ModelArtifactError,
    )
    _ENGINE_IMPORT_ERROR = None
except Exception as _exc_engine:
    DataikuAdapter = None

    class ModelArtifactError(RuntimeError):
        pass

    _ENGINE_IMPORT_ERROR = f"{type(_exc_engine).__name__}: {_exc_engine}"

logger = logging.getLogger(__name__)

if "app" not in globals():
    from flask import Flask

    app = Flask(__name__)

_UNLIMITED_UPLOAD_VALUES = {
    "",
    "0",
    "none",
    "unlimited",
    "sin_limite",
    "sin límite",
}


def _parse_max_upload_mb(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _UNLIMITED_UPLOAD_VALUES:
        return None
    try:
        max_mb = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "PII_MAX_UPLOAD_MB debe ser un entero positivo o "
            "'none'/'unlimited' para no limitar."
        ) from exc
    return max_mb if max_mb > 0 else None


try:
    _MAX_UPLOAD_MB = _parse_max_upload_mb(os.getenv("PII_MAX_UPLOAD_MB", "0"))
except ValueError as _exc_upload:
    logging.getLogger(__name__).warning("%s", _exc_upload)
    _MAX_UPLOAD_MB = None

_MAX_READ_ROWS = int(os.getenv("PII_MAX_READ_ROWS", "50000"))
app.config["MAX_CONTENT_LENGTH"] = (
    None if _MAX_UPLOAD_MB is None else _MAX_UPLOAD_MB * 1024 * 1024
)

_adapter: Optional[Any] = None
_adapter_lock = threading.Lock()


def get_adapter() -> Any:
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            if _ENGINE_IMPORT_ERROR:
                raise RuntimeError(
                    f"No se pudo importar pii_engine: {_ENGINE_IMPORT_ERROR}"
                )
            _adapter = DataikuAdapter(
                binary_folder=os.getenv("PII_BINARY_FOLDER", "model_binary_v1"),
                entity_folder=os.getenv("PII_ENTITY_FOLDER", "model_entity_v1"),
                content_folder=os.getenv("PII_CONTENT_FOLDER", "model_content_v1"),
                model_filename=os.getenv("PII_MODEL_FILENAME", "model.pkl"),
                max_read_rows=_MAX_READ_ROWS,
                strict_models=True,
            )
        return _adapter


def set_adapter(adapter: Optional[Any]) -> None:
    global _adapter
    with _adapter_lock:
        _adapter = adapter


def _error(message: str, status: int = 400):
    return jsonify({"status": "error", "message": message}), status


@app.errorhandler(413)
def _request_entity_too_large(_exc):
    if _MAX_UPLOAD_MB is None:
        message = (
            "El archivo es demasiado grande para el servidor o el reverse proxy. "
            "Contacta al administrador para aumentar el límite de carga "
            "(client_max_body_size)."
        )
    else:
        message = (
            f"El archivo supera el límite de {_MAX_UPLOAD_MB} MB. "
            "Reduce el tamaño o ajusta PII_MAX_UPLOAD_MB."
        )
    return _error(message, 413)


def _parse_sample_size(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        sample_size = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("sample_size debe ser un entero") from exc
    if sample_size < 1 or sample_size > 50000:
        raise ValueError("sample_size debe estar entre 1 y 50000")
    return sample_size


def _read_uploaded_file(file, max_rows: int) -> pd.DataFrame:
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()

    if suffix == ".csv":
        try:
            # `dtype=str` evita que pandas infiera identificadores numéricos
            # (CLABE, cuenta, NSS, etc.) y destruya sus ceros iniciales.
            return pd.read_csv(
                file,
                nrows=max_rows,
                low_memory=False,
                dtype=str,
            )
        except pd.errors.EmptyDataError as exc:
            raise ValueError(f"'{filename}' está vacío.") from exc

    if suffix in {".xls", ".xlsx"}:
        try:
            return pd.read_excel(file, nrows=max_rows)
        except TypeError:
            df = pd.read_excel(file)
            return df.head(max_rows)
        except pd.errors.EmptyDataError as exc:
            raise ValueError(f"'{filename}' está vacío.") from exc

    raise ValueError(
        f"Formato no soportado para '{filename}'. Sube archivos .csv o .xlsx."
    )


def _prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.shape[1] == 0:
        return df

    keep = []
    names = []
    used = set()
    for position, original in enumerate(df.columns):
        base = str(original).strip()
        if not base or base.lower() == "nan":
            continue
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        keep.append(position)
        names.append(name)

    if not keep:
        return df.iloc[:, 0:0].copy()

    result = df.iloc[:, keep].copy()
    result.columns = names
    return result


INFORMATION_CLASSIFICATION_VERSION = "1.0"

INFORMATION_CLASSIFICATION_LABELS = {
    "public": "Público",
    "internal_information": "Interno",
    "internal_pii": "PII Interno",
    "confidential_information": "Confidencial",
    "confidential_pii": "PII Confidencial",
    "sensitive_pii": "PII Sensible",
    "restricted": "Restringido",
}

INFORMATION_CLASSIFICATION_GROUPS = {
    "public": "public",
    "internal_information": "internal",
    "internal_pii": "internal",
    "confidential_information": "confidential_sensitive",
    "confidential_pii": "confidential_sensitive",
    "sensitive_pii": "confidential_sensitive",
    "restricted": "restricted",
}

ENTITY_TO_INFORMATION_CLASSIFICATION = {
    # Internal PII
    "nombre": "internal_pii",
    "apellido": "internal_pii",
    "direccion": "internal_pii",
    "telefono": "internal_pii",
    "correo": "internal_pii",
    "codigo_postal": "internal_pii",
    "ip": "internal_pii",
    "coordenadas": "internal_pii",
    "soeid": "internal_pii",
    "geid": "internal_pii",
    "cliente": "internal_pii",
    "lugar_nacimiento": "internal_pii",
    "estado_civil": "internal_pii",
    "puesto": "internal_pii",
    "nomina": "internal_pii",
    # Confidential PII
    "curp": "confidential_pii",
    "rfc": "confidential_pii",
    "clave_elector": "confidential_pii",
    "ssn": "confidential_pii",
    "nss": "confidential_pii",
    "pasaporte": "confidential_pii",
    "licencia_conducir": "confidential_pii",
    "cic": "confidential_pii",
    "ocr": "confidential_pii",
    "fecha_nacimiento": "confidential_pii",
    "fecha_vencimiento": "confidential_pii",
    "tarjeta": "confidential_pii",
    "cvv": "confidential_pii",
    "pin_nip": "confidential_pii",
    "clabe": "confidential_pii",
    "iban": "confidential_pii",
    "cuenta": "confidential_pii",
    "saldo": "confidential_pii",
    "credito": "confidential_pii",
    "otro_pii": "confidential_pii",
    # Sensitive PII
    "biometrico": "sensitive_pii",
    "nacionalidad": "sensitive_pii",
    "sexo": "sensitive_pii",
    # Business information
    "contrato": "confidential_information",
    "empresa": "internal_information",
}

CLASSIFICATION_REVIEW_ENTITIES = {"empresa", "contrato"}


def _classify_information(data: Dict[str, Any]) -> Dict[str, Any]:
    is_pii = bool(data.get("pii"))
    if not is_pii:
        return {
            "information_classification": None,
            "information_classification_group": None,
            "information_classification_label": "Sin clasificación",
            "classification_review": False,
            "classification_reason": "not_pii",
        }

    entity = str(data.get("entity") or "").strip().lower()
    needs_review = bool(data.get("needs_review"))

    if needs_review or entity == "otro_pii":
        key = "confidential_pii"
        reason = "needs_review"
    elif entity in ENTITY_TO_INFORMATION_CLASSIFICATION:
        key = ENTITY_TO_INFORMATION_CLASSIFICATION[entity]
        reason = "entity"
    else:
        key = "confidential_pii"
        reason = "unknown_entity"

    review = (
        needs_review
        or entity == "otro_pii"
        or reason == "unknown_entity"
        or entity in CLASSIFICATION_REVIEW_ENTITIES
    )
    return {
        "information_classification": key,
        "information_classification_group": INFORMATION_CLASSIFICATION_GROUPS.get(key),
        "information_classification_label": INFORMATION_CLASSIFICATION_LABELS.get(key, key),
        "classification_review": bool(review),
        "classification_reason": reason,
    }


def _result_to_row(result: Any, source_file: str) -> Dict[str, Any]:
    data = result.to_dict()
    is_pii = bool(data.get("pii"))

    row = {
        "source_file": source_file,
        "dataset": source_file,
        "name": data.get("column"),
        "is_pii": is_pii,
        "pii_probability": data.get("prob_pii"),
        "entity": data.get("entity") if is_pii else None,
        "entity_probability": data.get("prob_entity") if is_pii else None,
        "needs_review": data.get("needs_review"),
        "evidence": data.get("evidence"),
        "match_rate": data.get("match_rate"),
        "sample_size": data.get("sample_size"),
        "model_version": data.get("model_version"),
        "classification_version": INFORMATION_CLASSIFICATION_VERSION,
    }
    row.update(_classify_information(data))
    return row


@app.route("/health", methods=["GET"])
def health():
    if _ENGINE_IMPORT_ERROR:
        return jsonify({
            "status": "degraded",
            "engine_ready": False,
            "error": _ENGINE_IMPORT_ERROR,
        }), 503

    try:
        info = get_adapter().health()
    except Exception as exc:
        logger.exception("Error al verificar el motor")
        return jsonify({
            "status": "degraded",
            "engine_ready": False,
            "error": f"{type(exc).__name__}: {exc}",
        }), 503

    info = dict(info)
    info["max_upload_mb"] = _MAX_UPLOAD_MB
    info["max_read_rows"] = _MAX_READ_ROWS
    status = 200 if info.get("engine_ready") else 503
    return jsonify(info), status


@app.route("/process_table", methods=["POST"])
def process_table():
    if _ENGINE_IMPORT_ERROR:
        return _error(
            f"No se pudo importar pii_engine: {_ENGINE_IMPORT_ERROR}",
            503,
        )

    files = request.files.getlist("file")
    if not files:
        return _error("No se subió ningún archivo.", 400)

    try:
        sample_size = _parse_sample_size(request.form.get("sample_size"))
    except ValueError as exc:
        return _error(str(exc), 400)

    try:
        adapter = get_adapter()
    except Exception as exc:
        logger.exception("No se pudo inicializar el adaptador")
        return _error(
            f"No se pudo inicializar el motor PII: {type(exc).__name__}: {exc}",
            503,
        )

    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for file in files:
        if not file.filename:
            continue

        try:
            df = _read_uploaded_file(file, _MAX_READ_ROWS)
        except ValueError as exc:
            warnings.append(str(exc))
            continue

        if df.shape[1] == 0:
            warnings.append(f"'{file.filename}' no tiene columnas para analizar.")
            continue

        df = _prepare_dataframe(df)
        if df.shape[1] == 0:
            warnings.append(f"'{file.filename}' no tiene columnas válidas para analizar.")
            continue

        try:
            results = adapter.analyze_dataframe(
                df,
                table_name=file.filename,
                sample_size=sample_size,
            )
        except ModelArtifactError as exc:
            logger.exception("Error de artefacto al analizar '%s'", file.filename)
            return _error(str(exc), 503)
        except Exception as exc:
            logger.exception("Error al analizar '%s'", file.filename)
            warnings.append(
                f"No se pudo analizar '{file.filename}': "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        rows.extend(_result_to_row(result, file.filename) for result in results)

    if not rows:
        return _error(
            " | ".join(warnings) if warnings else "Ningún archivo pudo procesarse.",
            400,
        )

    response = {
        "status": "success",
        "columns_analysis": rows,
        "classification_version": INFORMATION_CLASSIFICATION_VERSION,
    }
    if warnings:
        response["warnings"] = warnings
    return jsonify(response)


EXPORT_XLSX_HEADER = ["Archivo", "Nombre de Columna", "Prediccion",
                      "Probabilidad PII (%)", "Evidencia", "Requiere revision",
                      "Entidad", "Probabilidad Entidad (%)", "Muestra",
                      "Clasificación", "Grupo",
                      "Revisión clasificación"]
_EXPORT_COL_COUNT = len(EXPORT_XLSX_HEADER)


def _safe_xlsx_filename(name):
    base = re.sub(r"[\\/:*?\"<>|]", "_", str(name or "reporte")).strip()
    base = re.sub(r"\.(csv|xls|xlsx)$", "", base, flags=re.IGNORECASE).strip()
    base = base[:80] or "reporte"
    return base + ".xlsx"


@app.route('/export_xlsx', methods=['POST'])
def export_xlsx():
    try:
        if Workbook is None:
            return jsonify({
                "status": "error",
                "message": f"openpyxl no disponible: {_XLSX_ERROR}. Usa CSV.",
            }), 500
        payload = request.get_json(force=True, silent=True) or {}
        rows = payload.get("rows") or []
        if not isinstance(rows, list) or not rows:
            return jsonify({"status": "error", "message": "Sin filas para exportar."}), 400

        wb = Workbook()
        ws = wb.active
        ws.title = "PII"
        ws.freeze_panes = "A2"
        head_fill = PatternFill("solid", fgColor="003746")
        head_font = Font(bold=True, color="FFFFFF")
        ws.append(EXPORT_XLSX_HEADER)
        for cell in ws[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(vertical="center")
        for r in rows:
            vals = list(r)[:_EXPORT_COL_COUNT]
            vals += [""] * max(0, _EXPORT_COL_COUNT - len(vals))
            out = []
            for idx, v in enumerate(vals):
                if idx in (3, 7) and v not in (None, ""):
                    try:
                        out.append(float(v))
                        continue
                    except (TypeError, ValueError):
                        pass
                if idx == 8 and v not in (None, ""):
                    try:
                        out.append(int(float(v)))
                        continue
                    except (TypeError, ValueError):
                        pass
                out.append("" if v is None else str(v))
            ws.append(out)
        for col in ws.columns:
            width = max((len(str(c.value or "")) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(width + 2, 48)
        for row in ws.iter_rows(min_row=2, min_col=4, max_col=8):
            for cell in row:
                if isinstance(cell.value, float):
                    cell.number_format = '0.00'
        ws.auto_filter.ref = ws.dimensions

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition":
                     f"attachment; filename=\"{_safe_xlsx_filename(payload.get('filename'))}\""},
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

