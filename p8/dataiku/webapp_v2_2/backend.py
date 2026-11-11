import importlib.util
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
import yaml
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
        DEFAULT_BINARY_FOLDER,
        DEFAULT_ENTITY_FOLDER,
        DEFAULT_MODEL_FILENAME,
        DataikuManagedFolderProvider,
    )
    from pii_engine.core.name_models import NameModelPredictor
    from pii_engine.core.schema import OTRO_PII
    from pii_engine.recognizers.registry import DEFAULT_CONFIG_DIR
    _ENGINE_IMPORT_ERROR = None
except Exception as _exc_engine:
    DEFAULT_BINARY_FOLDER = "model_binary_v1"
    DEFAULT_ENTITY_FOLDER = "model_entity_v1"
    DEFAULT_MODEL_FILENAME = "model.pkl"
    DataikuManagedFolderProvider = None
    NameModelPredictor = None
    OTRO_PII = "otro_pii"
    DEFAULT_CONFIG_DIR = None
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

app.config["MAX_CONTENT_LENGTH"] = (
    None if _MAX_UPLOAD_MB is None else _MAX_UPLOAD_MB * 1024 * 1024
)

MANUAL_DEFAULT_LABEL = "Nombres manuales"
_MANUAL_SPLIT = re.compile(r"[\r\n,;\t]+")

_predictor: Optional[Any] = None
_predictor_lock = threading.Lock()


def _dataiku_available() -> bool:
    try:
        return importlib.util.find_spec("dataiku") is not None
    except Exception:
        return False


def _load_thresholds() -> Dict[str, Any]:
    if DEFAULT_CONFIG_DIR is None:
        return {}
    path = DEFAULT_CONFIG_DIR / "thresholds.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("No se pudo leer thresholds.yaml: %s: %s", type(exc).__name__, exc)
        return {}


def _build_predictor() -> Any:
    if NameModelPredictor is None:
        raise RuntimeError(
            f"No se pudo importar pii_engine: {_ENGINE_IMPORT_ERROR}"
        )

    thresholds = _load_thresholds()
    name_cfg = thresholds.get("name_models", {}) or {}
    name_resolution_cfg = thresholds.get("name_resolution", {}) or {}

    binary_bundle = None
    entity_bundle = None

    if _dataiku_available() and DataikuManagedFolderProvider is not None:
        provider = DataikuManagedFolderProvider(
            binary_folder=os.getenv("PII_BINARY_FOLDER", DEFAULT_BINARY_FOLDER),
            entity_folder=os.getenv("PII_ENTITY_FOLDER", DEFAULT_ENTITY_FOLDER),
            model_filename=os.getenv("PII_MODEL_FILENAME", DEFAULT_MODEL_FILENAME),
        )
        try:
            binary_bundle = provider.resolve_model_path(provider.binary_folder)
        except Exception as exc:
            logger.warning(
                "No se pudo resolver el modelo binario: %s: %s",
                type(exc).__name__,
                exc,
            )
        try:
            entity_bundle = provider.resolve_model_path(provider.entity_folder)
        except Exception as exc:
            logger.warning(
                "No se pudo resolver el modelo de entidad: %s: %s",
                type(exc).__name__,
                exc,
            )

    return NameModelPredictor(
        binary_bundle=binary_bundle,
        entity_bundle=entity_bundle,
        binary_threshold=float(name_cfg.get("binary_threshold", 0.409)),
        entity_min_confidence=float(name_cfg.get("entity_min_confidence", 0.45)),
        name_resolution_cfg=name_resolution_cfg,
    )


def get_predictor() -> Any:
    global _predictor
    with _predictor_lock:
        if _predictor is None:
            _predictor = _build_predictor()
        return _predictor


def set_predictor(predictor: Optional[Any]) -> None:
    global _predictor
    with _predictor_lock:
        _predictor = predictor


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


def _read_column_names(file) -> List[Any]:
    """Lee únicamente los encabezados/columnas de un archivo."""
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()

    def _rewind():
        seek = getattr(file, "seek", None)
        if callable(seek):
            try:
                seek(0)
            except Exception:
                pass

    if suffix == ".csv":
        _rewind()
        try:
            df = pd.read_csv(file, nrows=0, dtype=str, keep_default_na=False)
        except pd.errors.EmptyDataError as exc:
            raise ValueError(f"'{filename}' está vacío.") from exc
        except Exception as exc:
            raise ValueError(
                f"No se pudo leer el encabezado de '{filename}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return list(df.columns)

    if suffix in {".xlsx", ".xlsm"}:
        _rewind()
        try:
            from openpyxl import load_workbook
        except Exception as exc:
            raise ValueError(
                "openpyxl no está disponible para leer archivos .xlsx/.xlsm."
            ) from exc
        workbook = None
        try:
            workbook = load_workbook(file, read_only=True, data_only=True)
            if not workbook.sheetnames:
                return []
            sheet = workbook[workbook.sheetnames[0]]
            row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
            return list(row)
        except Exception as exc:
            raise ValueError(
                f"No se pudo leer el encabezado de '{filename}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            if workbook is not None:
                try:
                    workbook.close()
                except Exception:
                    pass

    if suffix == ".xls":
        _rewind()
        try:
            df = pd.read_excel(file, nrows=0, sheet_name=0, dtype=str)
        except Exception as exc:
            raise ValueError(
                f"No se pudo leer el encabezado de '{filename}'. "
                "Para .xls se requiere xlrd: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return list(df.columns)

    raise ValueError(
        f"Formato no soportado para '{filename}'. Sube archivos .csv, .xlsx o .xls."
    )


def _normalize_column_names(raw_names: List[Any]) -> List[str]:
    names: List[str] = []
    used = set()
    for raw in raw_names:
        base = "" if raw is None else str(raw).strip()
        if not base or base.lower() in {"nan", "none"}:
            continue
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        names.append(name)
    return names


def _parse_manual_names(text: Any) -> List[str]:
    if text is None:
        return []
    parts = _MANUAL_SPLIT.split(str(text))
    return _normalize_column_names(parts)


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

_ORIGIN_MAP = {"rules": "rule", "model": "model", "both": "both"}


def _classify_information(
    is_pii: bool,
    entity: Optional[str],
    needs_review: bool,
) -> Dict[str, Any]:
    if not is_pii:
        return {
            "information_classification": None,
            "information_classification_group": None,
            "information_classification_label": "Sin clasificación",
            "classification_review": False,
            "classification_reason": "not_pii",
        }

    entity_key = str(entity or "").strip().lower()

    if needs_review or entity_key == OTRO_PII:
        key = "confidential_pii"
        reason = "needs_review"
    elif entity_key in ENTITY_TO_INFORMATION_CLASSIFICATION:
        key = ENTITY_TO_INFORMATION_CLASSIFICATION[entity_key]
        reason = "entity"
    else:
        key = "confidential_pii"
        reason = "unknown_entity"

    review = (
        needs_review
        or entity_key == OTRO_PII
        or reason == "unknown_entity"
        or entity_key in CLASSIFICATION_REVIEW_ENTITIES
    )
    return {
        "information_classification": key,
        "information_classification_group": INFORMATION_CLASSIFICATION_GROUPS.get(key),
        "information_classification_label": INFORMATION_CLASSIFICATION_LABELS.get(key, key),
        "classification_review": bool(review),
        "classification_reason": reason,
    }


def _prediction_to_row(
    prediction: Any,
    source_file: str,
    model_version: str,
) -> Dict[str, Any]:
    is_pii = bool(prediction.is_pii_name)
    entity = prediction.expected_entity if is_pii else None
    needs_review = bool(
        is_pii
        and (
            prediction.abstained
            or not entity
            or entity == OTRO_PII
        )
    )
    row = {
        "source_file": source_file,
        "dataset": source_file,
        "name": prediction.column,
        "is_pii": is_pii,
        "pii_probability": round(float(prediction.score_name), 4),
        "entity": entity,
        "entity_probability": (
            round(float(prediction.prob_entity_name), 4)
            if is_pii and entity
            else None
        ),
        "origin": _ORIGIN_MAP.get(str(prediction.source or ""), None),
        "needs_review": needs_review,
        "model_version": model_version,
        "classification_version": INFORMATION_CLASSIFICATION_VERSION,
    }
    row.update(_classify_information(is_pii, entity, needs_review))
    return row


def _analyze_names(
    names: List[str],
    source_file: str,
    predictor: Any,
    model_version: str,
) -> List[Dict[str, Any]]:
    if not names:
        return []
    predictions = predictor.predict(names)
    rows: List[Dict[str, Any]] = []
    for name in names:
        prediction = predictions.get(name)
        if prediction is None:
            continue
        rows.append(_prediction_to_row(prediction, source_file, model_version))
    return rows


@app.route("/health", methods=["GET"])
def health():
    if _ENGINE_IMPORT_ERROR:
        return jsonify({
            "status": "degraded",
            "engine_ready": False,
            "mode": "name_only",
            "error": _ENGINE_IMPORT_ERROR,
        }), 503

    try:
        predictor = get_predictor()
    except Exception as exc:
        logger.exception("Error al inicializar el predictor de nombres")
        return jsonify({
            "status": "degraded",
            "engine_ready": False,
            "mode": "name_only",
            "error": f"{type(exc).__name__}: {exc}",
        }), 503

    return jsonify({
        "status": "ok",
        "engine_ready": True,
        "mode": "name_only",
        "model_version": predictor.model_version_tag,
        "models_loaded": bool(
            predictor.binary_bundle is not None
            and predictor.entity_bundle is not None
        ),
        "max_upload_mb": _MAX_UPLOAD_MB,
    }), 200


@app.route("/process_table", methods=["POST"])
def process_table():
    if _ENGINE_IMPORT_ERROR:
        return _error(
            f"No se pudo importar pii_engine: {_ENGINE_IMPORT_ERROR}",
            503,
        )

    files = [f for f in request.files.getlist("file") if f and f.filename]
    manual_text = request.form.get("names", "")
    manual_label = (request.form.get("names_label") or "").strip()
    if not manual_label:
        manual_label = MANUAL_DEFAULT_LABEL

    manual_names = _parse_manual_names(manual_text)
    if not files and not manual_names:
        return _error(
            "Sube al menos un archivo o pega nombres de columna para analizar.",
            400,
        )

    try:
        predictor = get_predictor()
    except Exception as exc:
        logger.exception("No se pudo inicializar el predictor de nombres")
        return _error(
            f"No se pudo inicializar el predictor de nombres: "
            f"{type(exc).__name__}: {exc}",
            503,
        )

    model_version = predictor.model_version_tag
    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for file in files:
        try:
            raw_names = _read_column_names(file)
        except ValueError as exc:
            warnings.append(str(exc))
            continue

        names = _normalize_column_names(raw_names)
        if not names:
            warnings.append(
                f"'{file.filename}' no tiene nombres de columna válidos para analizar."
            )
            continue

        try:
            rows.extend(
                _analyze_names(names, file.filename, predictor, model_version)
            )
        except Exception as exc:
            logger.exception("Error al analizar nombres de '%s'", file.filename)
            warnings.append(
                f"No se pudo analizar '{file.filename}': "
                f"{type(exc).__name__}: {exc}"
            )

    if manual_names:
        try:
            rows.extend(
                _analyze_names(manual_names, manual_label, predictor, model_version)
            )
        except Exception as exc:
            logger.exception("Error al analizar nombres manuales")
            warnings.append(
                f"No se pudieron analizar los nombres manuales: "
                f"{type(exc).__name__}: {exc}"
            )

    if not rows:
        return _error(
            " | ".join(warnings) if warnings else "Ningún nombre pudo procesarse.",
            400,
        )

    response: Dict[str, Any] = {
        "status": "success",
        "mode": "name_only",
        "columns_analysis": rows,
        "classification_version": INFORMATION_CLASSIFICATION_VERSION,
        "model_version": model_version,
    }
    if warnings:
        response["warnings"] = warnings
    return jsonify(response)


EXPORT_XLSX_HEADER = [
    "Archivo",
    "Nombre de Columna",
    "Prediccion",
    "Probabilidad PII (%)",
    "Entidad",
    "Probabilidad Entidad (%)",
    "Origen",
    "Requiere revision",
    "Clasificación",
    "Grupo",
    "Revisión clasificación",
]
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
        ws.title = "PII_Nombres"
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
                if idx in (3, 5) and v not in (None, ""):
                    try:
                        out.append(float(v))
                        continue
                    except (TypeError, ValueError):
                        pass
                out.append("" if v is None else str(v))
            ws.append(out)
        for col in ws.columns:
            width = max((len(str(c.value or "")) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(width + 2, 48)
        for row in ws.iter_rows(min_row=2, min_col=4, max_col=6):
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
