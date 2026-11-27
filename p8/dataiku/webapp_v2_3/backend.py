import io
import json
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
        DEFAULT_ROW_MAX_ROWS as _CFG_ROW_MAX_ROWS,
        DEFAULT_ROW_MIN_ROWS as _CFG_ROW_MIN_ROWS,
        DEFAULT_ROW_RATIO as _CFG_ROW_RATIO,
        DataikuAdapter,
        ModelArtifactError,
        read_csv_sample,
        read_excel_sample,
    )
    from pii_engine.core.schema import NO_ENTITY
    _ENGINE_IMPORT_ERROR = None
except Exception as _exc_engine:
    DataikuAdapter = None
    read_csv_sample = None
    read_excel_sample = None
    _CFG_ROW_RATIO = 0.02
    _CFG_ROW_MIN_ROWS = None
    _CFG_ROW_MAX_ROWS = None
    NO_ENTITY = "no aplica"

    class ModelArtifactError(RuntimeError):
        pass

    _ENGINE_IMPORT_ERROR = f"{type(_exc_engine).__name__}: {_exc_engine}"

try:
    from pii_deep_scan import DeepScanOptions, scan_column_chunks
    _DEEP_IMPORT_ERROR = None
except Exception as _exc_deep:
    DeepScanOptions = None
    scan_column_chunks = None
    _DEEP_IMPORT_ERROR = f"{type(_exc_deep).__name__}: {_exc_deep}"

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

_MAX_READ_ROWS: Optional[int] = None
_UNLIMITED_ROW_VALUES = {"", "0", "none", "null", "unlimited", "sin_limite", "sin límite"}


def _parse_optional_rows(value: Any, name: str) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _UNLIMITED_ROW_VALUES:
        return None
    try:
        rows = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} debe ser un entero positivo o vacío") from exc
    if rows <= 0:
        raise ValueError(f"{name} debe ser un entero positivo o vacío")
    return rows


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "si", "sí"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _parse_sample_ratio(value: Any) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("PII_SAMPLE_RATIO debe ser un número en (0, 1]") from exc
    if not 0 < ratio <= 1:
        raise ValueError("PII_SAMPLE_RATIO debe estar en (0, 1]")
    return ratio


_env_ratio = os.getenv("PII_SAMPLE_RATIO")
try:
    _SAMPLE_RATIO = (
        _parse_sample_ratio(_env_ratio) if _env_ratio is not None else _CFG_ROW_RATIO
    )
except ValueError as _exc_ratio:
    logger.warning("%s; se usa %s", _exc_ratio, _CFG_ROW_RATIO)
    _SAMPLE_RATIO = _CFG_ROW_RATIO

_env_min_rows = os.getenv("PII_SAMPLE_MIN_ROWS")
try:
    _ROW_MIN_ROWS = (
        _parse_optional_rows(_env_min_rows, "PII_SAMPLE_MIN_ROWS")
        if _env_min_rows is not None
        else _CFG_ROW_MIN_ROWS
    )
except ValueError as _exc_min:
    logger.warning("%s; sin mínimo", _exc_min)
    _ROW_MIN_ROWS = _CFG_ROW_MIN_ROWS

_env_max_rows = os.getenv("PII_MAX_READ_ROWS")
try:
    _MAX_READ_ROWS = (
        _parse_optional_rows(_env_max_rows, "PII_MAX_READ_ROWS")
        if _env_max_rows is not None
        else _CFG_ROW_MAX_ROWS
    )
except ValueError as _exc_max:
    logger.warning("%s; sin máximo", _exc_max)
    _MAX_READ_ROWS = _CFG_ROW_MAX_ROWS

_VALID_SAMPLING_MODES = {"random", "head"}
_SAMPLING_MODE = (os.getenv("PII_SAMPLING_MODE", "random") or "random").strip().lower()
if _SAMPLING_MODE not in _VALID_SAMPLING_MODES:
    logger.warning(
        "PII_SAMPLING_MODE=%r no válido; se usa 'random'", _SAMPLING_MODE
    )
    _SAMPLING_MODE = "random"

try:
    _SAMPLING_SEED = int(os.getenv("PII_SAMPLE_SEED", "42"))
except (TypeError, ValueError):
    logger.warning("PII_SAMPLE_SEED no es un entero; se usa 42")
    _SAMPLING_SEED = 42

_env_deep_max_rows = os.getenv("PII_DEEP_MAX_ROWS")
try:
    _DEEP_MAX_ROWS = (
        _parse_optional_rows(_env_deep_max_rows, "PII_DEEP_MAX_ROWS")
        if _env_deep_max_rows is not None
        else 500_000
    )
except ValueError as _exc_deep_rows:
    logger.warning("%s; se usa 500000", _exc_deep_rows)
    _DEEP_MAX_ROWS = 500_000

try:
    _DEEP_CHUNK_SIZE = int(os.getenv("PII_DEEP_CHUNK_SIZE", "50000"))
except (TypeError, ValueError):
    logger.warning("PII_DEEP_CHUNK_SIZE no es un entero; se usa 50000")
    _DEEP_CHUNK_SIZE = 50_000
if _DEEP_CHUNK_SIZE <= 0:
    logger.warning("PII_DEEP_CHUNK_SIZE debe ser positivo; se usa 50000")
    _DEEP_CHUNK_SIZE = 50_000

_DEEP_INCLUDE_MODEL = str(os.getenv("PII_DEEP_INCLUDE_MODEL", "1")).strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

_DEFAULT_CONTENT_ANALYSIS = _parse_bool(
    os.getenv("PII_DEFAULT_CONTENT_ANALYSIS"), False
)
_METADATA_ELEM_COLUMN = (os.getenv("PII_METADATA_ELEM_COLUMN") or "").strip()
_env_metadata_max_rows = os.getenv("PII_METADATA_MAX_ROWS")
try:
    _METADATA_MAX_ROWS = (
        _parse_optional_rows(_env_metadata_max_rows, "PII_METADATA_MAX_ROWS")
        if _env_metadata_max_rows is not None
        else 200_000
    )
except ValueError as _exc_metadata_rows:
    logger.warning("%s; se usa 200000", _exc_metadata_rows)
    _METADATA_MAX_ROWS = 200_000

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
                sampling_mode=_SAMPLING_MODE,
                sampling_seed=_SAMPLING_SEED,
                row_ratio=_SAMPLE_RATIO,
                row_min_rows=_ROW_MIN_ROWS,
                row_max_rows=_MAX_READ_ROWS,
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
            "El archivo es demasiado grande para el servidor o el proxy inverso. "
            "Se recomienda contactar al administrador para aumentar el límite "
            "de carga (client_max_body_size)."
        )
    else:
        message = (
            f"El archivo supera el límite de {_MAX_UPLOAD_MB} MB. "
            "Es necesario reducir el tamaño o ajustar PII_MAX_UPLOAD_MB."
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


def _read_uploaded_file(
    file,
    max_rows: Optional[int] = None,
    seed: Optional[int] = None,
    mode: Optional[str] = None,
    ratio: Optional[float] = None,
    min_rows: Optional[int] = None,
) -> pd.DataFrame:
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    seed = _SAMPLING_SEED if seed is None else seed
    mode = _SAMPLING_MODE if mode is None else mode

    if suffix == ".csv":
        try:
            return read_csv_sample(
                file,
                max_rows,
                dtype=str,
                seed=seed,
                mode=mode,
                ratio=ratio,
                min_rows=min_rows,
            )
        except pd.errors.EmptyDataError as exc:
            raise ValueError(f"'{filename}' está vacío.") from exc

    if suffix in {".xls", ".xlsx", ".xlsm"}:
        try:
            return read_excel_sample(
                file,
                max_rows,
                seed=seed,
                mode=mode,
                ratio=ratio,
                min_rows=min_rows,
            )
        except pd.errors.EmptyDataError as exc:
            raise ValueError(f"'{filename}' está vacío.") from exc

    raise ValueError(
        f"Formato no compatible con '{filename}'. "
        "Se admiten archivos .csv o .xlsx."
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


def _prepared_column_pairs(columns) -> List[Dict[str, Any]]:
    used = set()
    pairs: List[Dict[str, Any]] = []
    for position, original in enumerate(columns):
        base = str(original).strip()
        if not base or base.lower() == "nan":
            continue
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        pairs.append({"position": position, "original": original, "name": name})
    return pairs


_ELEM_COLUMN_ALIASES = (
    "dataelem",
    "dataelement",
    "dataelemento",
    "datoelem",
    "elementodato",
    "elementodedato",
    "elemento",
    "nombredato",
    "nombreelemento",
    "nombrecampo",
    "dataitem",
    "datafield",
    "campo",
    "field",
    "columna",
    "column",
    "atributo",
)


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _detect_elem_column(columns: List[str]) -> Optional[str]:
    if not columns:
        return None
    normalized = [(column, _normalize_key(column)) for column in columns]

    if _METADATA_ELEM_COLUMN:
        wanted = _normalize_key(_METADATA_ELEM_COLUMN)
        for original, key in normalized:
            if key == wanted:
                return original

    for alias in _ELEM_COLUMN_ALIASES:
        for original, key in normalized:
            if key == alias:
                return original

    for original, key in normalized:
        if "dataelem" in key or "elementodato" in key or "elementodedato" in key:
            return original
    return None


def _parse_json_mapping(value: Any) -> Dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _sheet_for_index(mapping: Dict[str, Any], index: int) -> Any:
    value = mapping.get(str(index), mapping.get(index))
    if value in (None, ""):
        return None
    return value


def _read_header_columns(file, sheet: Optional[Any] = None) -> List[str]:
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    _rewind_upload(file)
    try:
        if suffix == ".csv":
            header = pd.read_csv(getattr(file, "stream", file), nrows=0)
        elif suffix in {".xls", ".xlsx", ".xlsm"}:
            header = pd.read_excel(
                getattr(file, "stream", file),
                nrows=0,
                sheet_name=sheet if sheet not in (None, "") else 0,
            )
        else:
            raise ValueError(
                f"Formato no compatible con '{filename}'. "
                "Se admiten archivos .csv o Excel."
            )
    finally:
        _rewind_upload(file)
    return [str(column) for column in header.columns]


def _read_metadata_frame(
    file,
    sheet: Optional[Any] = None,
    max_rows: Optional[int] = None,
) -> pd.DataFrame:
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    _rewind_upload(file)
    try:
        if suffix == ".csv":
            kwargs: Dict[str, Any] = {
                "dtype": str,
                "keep_default_na": False,
                "na_values": [],
            }
            if max_rows:
                kwargs["nrows"] = int(max_rows) + 1
            return pd.read_csv(getattr(file, "stream", file), **kwargs)
        if suffix in {".xls", ".xlsx", ".xlsm"}:
            kwargs = {
                "dtype": str,
                "keep_default_na": False,
                "na_values": [],
                "sheet_name": sheet if sheet not in (None, "") else 0,
            }
            if max_rows:
                kwargs["nrows"] = int(max_rows) + 1
            return pd.read_excel(getattr(file, "stream", file), **kwargs)
    finally:
        _rewind_upload(file)
    raise ValueError(
        f"Formato no compatible con '{filename}'. "
        "Se admiten archivos .csv o Excel."
    )


def _clip_metadata_frame(
    df: pd.DataFrame,
    max_rows: Optional[int],
) -> tuple:
    if not max_rows or len(df) <= int(max_rows):
        return df, False
    return df.iloc[: int(max_rows)].copy(), True


def _rewind_upload(file) -> None:
    stream = getattr(file, "stream", file)
    seek = getattr(stream, "seek", None)
    if callable(seek):
        try:
            seek(0)
        except Exception:
            pass


def _resolve_source_column(file, prepared_name: str, suffix: str) -> Any:
    _rewind_upload(file)
    if suffix == ".csv":
        header = pd.read_csv(getattr(file, "stream", file), nrows=0)
    elif suffix in {".xls", ".xlsx", ".xlsm"}:
        header = pd.read_excel(getattr(file, "stream", file), nrows=0)
    else:
        raise ValueError(
            "Formato no compatible. El análisis profundo admite .csv o Excel."
        )
    _rewind_upload(file)

    for pair in _prepared_column_pairs(header.columns):
        if pair["name"] == prepared_name:
            return pair["original"]
    raise ValueError(
        f"La columna '{prepared_name}' no se encontró en el archivo cargado."
    )


def _iter_deep_chunks(file, suffix: str, original_column: Any):
    _rewind_upload(file)
    if suffix == ".csv":
        chunks = pd.read_csv(
            getattr(file, "stream", file),
            usecols=[original_column],
            dtype=str,
            chunksize=_DEEP_CHUNK_SIZE,
            low_memory=False,
        )
        for chunk in chunks:
            if original_column in chunk.columns:
                yield chunk[original_column]
            elif chunk.shape[1] == 1:
                yield chunk.iloc[:, 0]
        return

    nrows = (_DEEP_MAX_ROWS + 1) if _DEEP_MAX_ROWS else None
    frame = pd.read_excel(
        getattr(file, "stream", file),
        usecols=[original_column],
        nrows=nrows,
    )
    series = (
        frame[original_column]
        if original_column in frame.columns
        else frame.iloc[:, 0] if frame.shape[1] == 1 else None
    )
    if series is None:
        return
    for start in range(0, len(series), _DEEP_CHUNK_SIZE):
        yield series.iloc[start : start + _DEEP_CHUNK_SIZE]


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


def _classify_information(
    data: Dict[str, Any],
    *,
    escalate_on_review: bool = True,
) -> Dict[str, Any]:
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

    if (escalate_on_review and needs_review) or entity == "otro_pii":
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


def _result_to_row(
    result: Any, source_file: str, source_index: Optional[int] = None
) -> Dict[str, Any]:
    data = result.to_dict()
    is_pii = bool(data.get("pii"))

    row = {
        "source_file": source_file,
        "source_index": source_index,
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
        "content_analysis": True,
        "analysis_mode": "columns",
    }
    row.update(_classify_information(data))
    return row


def _metadata_column_row(
    prediction: Any,
    source_file: str,
    source_index: Optional[int],
    model_version: str,
) -> Dict[str, Any]:
    data = {
        "pii": bool(prediction.is_pii),
        "entity": prediction.entity,
        "needs_review": bool(prediction.needs_review),
    }
    row = {
        "source_file": source_file,
        "source_index": source_index,
        "dataset": source_file,
        "name": prediction.element,
        "is_pii": bool(prediction.is_pii),
        "pii_probability": round(float(prediction.prob_pii), 4),
        "entity": prediction.entity if prediction.is_pii else None,
        "entity_probability": (
            round(float(prediction.prob_entity), 4)
            if prediction.is_pii
            else None
        ),
        "needs_review": bool(prediction.needs_review),
        "evidence": "name",
        "match_rate": 0.0,
        "sample_size": 0,
        "model_version": model_version,
        "classification_version": INFORMATION_CLASSIFICATION_VERSION,
        "content_analysis": False,
        "analysis_mode": "columns",
        "source": prediction.source,
        "score_binary": round(float(prediction.score_binary), 4),
        "regex_entity": prediction.regex_entity,
        "entity_top1": prediction.entity_top1,
        "evaluable": bool(prediction.evaluable),
    }
    row.update(_classify_information(data))
    return row


def _metadata_row(
    prediction: Any,
    source_file: str,
    source_index: Optional[int],
    row_index: int,
    elem_column: str,
    element: str,
    metadata_values: Dict[str, Any],
    model_version: str,
) -> Dict[str, Any]:
    if prediction is None:
        data = {"pii": False, "entity": NO_ENTITY, "needs_review": True}
        pii = False
        entity = NO_ENTITY
        prob_pii = 0.0
        prob_entity = 0.0
        needs_review = True
        source = "none"
        score_binary = 0.0
        regex_entity = None
        entity_top1 = None
        evaluable = bool(element)
    else:
        data = {
            "pii": bool(prediction.is_pii),
            "entity": prediction.entity,
            "needs_review": bool(prediction.needs_review),
        }
        pii = bool(prediction.is_pii)
        entity = prediction.entity
        prob_pii = float(prediction.prob_pii)
        prob_entity = float(prediction.prob_entity)
        needs_review = bool(prediction.needs_review)
        source = prediction.source
        score_binary = float(prediction.score_binary)
        regex_entity = prediction.regex_entity
        entity_top1 = prediction.entity_top1
        evaluable = bool(prediction.evaluable)

    if not evaluable:
        needs_review = True

    row = dict(metadata_values)
    row.update(
        {
            "source_file": source_file,
            "source_index": source_index,
            "row_index": row_index,
            "elem_column": elem_column,
            "element": element,
            "evaluable": evaluable,
            "analysis_mode": "metadata",
            "content_analysis": False,
            "model_version": model_version,
            "classification_version": INFORMATION_CLASSIFICATION_VERSION,
            "is_pii": bool(pii),
            "pii_probability": round(prob_pii, 4),
            "entity": entity if pii else None,
            "entity_probability": round(prob_entity, 4) if pii else None,
            "needs_review": needs_review,
            "evidence": "name" if evaluable else "sin_elemento",
            "source": source,
            "score_binary": round(score_binary, 4),
            "regex_entity": regex_entity,
            "entity_top1": entity_top1,
        }
    )
    row.update(_classify_information(data, escalate_on_review=False))
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
    info["row_ratio"] = _SAMPLE_RATIO
    info["row_min_rows"] = _ROW_MIN_ROWS
    info["sampling_mode"] = _SAMPLING_MODE
    info["sampling_seed"] = _SAMPLING_SEED
    info["deep_max_rows"] = _DEEP_MAX_ROWS
    info["deep_chunk_size"] = _DEEP_CHUNK_SIZE
    info["deep_include_model"] = _DEEP_INCLUDE_MODEL
    info["default_content_analysis"] = _DEFAULT_CONTENT_ANALYSIS
    info["metadata_elem_column"] = _METADATA_ELEM_COLUMN or None
    info["metadata_max_rows"] = _METADATA_MAX_ROWS
    info["metadata_use_entity_model"] = _parse_bool(
        os.getenv("PII_METADATA_USE_ENTITY_MODEL"), True
    )
    info["metadata_allow_regex_fallback"] = _parse_bool(
        os.getenv("PII_METADATA_ALLOW_REGEX_FALLBACK"), True
    )
    info["analysis_modes"] = ["columns", "metadata"]
    status = (
        200
        if (info.get("engine_ready") or info.get("metadata_ready"))
        else 503
    )
    return jsonify(info), status


@app.route("/inspect_columns", methods=["POST"])
def inspect_columns():
    if _ENGINE_IMPORT_ERROR:
        return _error(
            f"No se pudo importar pii_engine: {_ENGINE_IMPORT_ERROR}",
            503,
        )

    files = request.files.getlist("file")
    if not files:
        return _error("No se recibió ningún archivo.", 400)

    sheets = _parse_json_mapping(request.form.get("sheets"))
    results: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for file_index, file in enumerate(files):
        if not file.filename:
            continue
        try:
            columns = _read_header_columns(
                file, sheet=_sheet_for_index(sheets, file_index)
            )
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        except Exception as exc:
            logger.exception("No se pudieron leer columnas de '%s'", file.filename)
            warnings.append(
                f"No se pudieron leer las columnas de '{file.filename}': "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        pairs = _prepared_column_pairs(columns)
        names = [pair["name"] for pair in pairs]
        results.append(
            {
                "source_file": file.filename,
                "source_index": file_index,
                "columns": names,
                "suggested_column": _detect_elem_column(names),
            }
        )

    if not results:
        return _error(
            " | ".join(warnings)
            if warnings
            else "Ningún archivo pudo inspeccionarse.",
            400,
        )

    response = {"status": "success", "files": results}
    if warnings:
        response["warnings"] = warnings
    return jsonify(response)


METADATA_RESULT_COLUMNS = [
    "elem_column",
    "element",
    "is_pii",
    "entity",
    "pii_probability",
    "entity_probability",
    "needs_review",
    "evidence",
    "source",
    "score_binary",
    "regex_entity",
    "entity_top1",
    "evaluable",
    "information_classification",
    "information_classification_label",
]


@app.route("/process_metadata", methods=["POST"])
def process_metadata():
    if _ENGINE_IMPORT_ERROR:
        return _error(
            f"No se pudo importar pii_engine: {_ENGINE_IMPORT_ERROR}",
            503,
        )

    files = request.files.getlist("file")
    if not files:
        return _error("No se recibió ningún archivo de metadata.", 400)

    try:
        adapter = get_adapter()
        analyzer = adapter.build_metadata_analyzer()
    except Exception as exc:
        logger.exception("No se pudo inicializar el analizador de metadata")
        return _error(
            "No se pudo inicializar el analizador de metadata: "
            f"{type(exc).__name__}: {exc}",
            503,
        )

    metadata_columns = _parse_json_mapping(request.form.get("metadata_columns"))
    sheets = _parse_json_mapping(request.form.get("sheets"))
    model_version = getattr(analyzer, "model_version_tag", "metadata")

    prepared: List[Dict[str, Any]] = []
    warnings: List[str] = []
    all_elements: List[str] = []
    metadata_order: Dict[str, None] = {}

    for file_index, file in enumerate(files):
        if not file.filename:
            continue

        try:
            df = _read_metadata_frame(
                file,
                sheet=_sheet_for_index(sheets, file_index),
                max_rows=_METADATA_MAX_ROWS,
            )
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        except Exception as exc:
            logger.exception("No se pudo leer '%s'", file.filename)
            warnings.append(
                f"No se pudo leer '{file.filename}': "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        df, truncated = _clip_metadata_frame(df, _METADATA_MAX_ROWS)
        df = _prepare_dataframe(df)
        if df.shape[1] == 0:
            warnings.append(
                f"'{file.filename}' no tiene columnas válidas para analizar."
            )
            continue

        columns = [str(column) for column in df.columns]
        requested = metadata_columns.get(str(file_index), metadata_columns.get(file_index))
        elem_column = str(requested) if requested not in (None, "") else None
        if elem_column not in columns:
            elem_column = _detect_elem_column(columns)

        if not elem_column:
            warnings.append(
                f"'{file.filename}' no contiene la columna de metadata indicada "
                "y no fue posible detectarla automáticamente."
            )
            continue

        for column in columns:
            metadata_order.setdefault(column, None)

        elements = [str(value).strip() for value in df[elem_column].tolist()]
        for element in elements:
            if element:
                all_elements.append(element)

        prepared.append(
            {
                "source_file": file.filename,
                "source_index": file_index,
                "df": df,
                "columns": columns,
                "elem_column": elem_column,
                "elements": elements,
                "truncated": truncated,
                "rows_total": int(len(df)),
            }
        )

    if not prepared:
        return _error(
            " | ".join(warnings)
            if warnings
            else "Ningún archivo de metadata pudo procesarse.",
            400,
        )

    unique_elements = list(dict.fromkeys(all_elements))
    try:
        predictions = analyzer.predict(unique_elements) if unique_elements else {}
    except Exception as exc:
        logger.exception("Error al predecir metadata")
        return _error(
            f"No se pudo completar la predicción de metadata: "
            f"{type(exc).__name__}: {exc}",
            500,
        )

    rows: List[Dict[str, Any]] = []
    file_summaries: List[Dict[str, Any]] = []
    for item in prepared:
        df = item["df"]
        elem_column = item["elem_column"]
        elements = item["elements"]
        if item["truncated"]:
            warnings.append(
                f"'{item['source_file']}' se truncó a {_METADATA_MAX_ROWS} filas "
                "según PII_METADATA_MAX_ROWS."
            )
        file_summaries.append(
            {
                "source_file": item["source_file"],
                "source_index": item["source_index"],
                "elem_column": elem_column,
                "columns": item["columns"],
                "rows_total": item["rows_total"],
                "truncated": item["truncated"],
            }
        )

        for row_index, (_, row) in enumerate(df.iterrows()):
            element = elements[row_index] if row_index < len(elements) else ""
            metadata_values = {
                column: ("" if row[column] is None else str(row[column]))
                for column in item["columns"]
            }
            rows.append(
                _metadata_row(
                    predictions.get(element) if element else None,
                    item["source_file"],
                    item["source_index"],
                    row_index,
                    elem_column,
                    element,
                    metadata_values,
                    model_version,
                )
            )

    response = {
        "status": "success",
        "analysis_mode": "metadata",
        "content_analysis": False,
        "files": file_summaries,
        "metadata_columns": list(metadata_order.keys()),
        "result_columns": METADATA_RESULT_COLUMNS,
        "results": rows,
        "classification_version": INFORMATION_CLASSIFICATION_VERSION,
    }
    if warnings:
        response["warnings"] = warnings
    return jsonify(response)


@app.route("/process_table", methods=["POST"])
def process_table():
    if _ENGINE_IMPORT_ERROR:
        return _error(
            f"No se pudo importar pii_engine: {_ENGINE_IMPORT_ERROR}",
            503,
        )

    files = request.files.getlist("file")
    if not files:
        return _error("No se recibió ningún archivo.", 400)

    try:
        sample_size = _parse_sample_size(request.form.get("sample_size"))
    except ValueError as exc:
        return _error(str(exc), 400)

    content_analysis = _parse_bool(
        request.form.get("content_analysis"), _DEFAULT_CONTENT_ANALYSIS
    )

    try:
        adapter = get_adapter()
    except Exception as exc:
        logger.exception("No se pudo inicializar el adaptador")
        return _error(
            f"No se pudo inicializar el motor PII: {type(exc).__name__}: {exc}",
            503,
        )

    metadata_analyzer = None
    metadata_model_version = "metadata"
    if not content_analysis:
        try:
            metadata_analyzer = adapter.build_metadata_analyzer()
            metadata_model_version = getattr(
                metadata_analyzer, "model_version_tag", "metadata"
            )
        except Exception as exc:
            logger.exception("No se pudo inicializar el analizador de nombres")
            return _error(
                "No se pudo inicializar el analizador de nombres: "
                f"{type(exc).__name__}: {exc}",
                503,
            )

    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for file_index, file in enumerate(files):
        if not file.filename:
            continue

        if not content_analysis:
            try:
                header_columns = _read_header_columns(file)
            except ValueError as exc:
                warnings.append(str(exc))
                continue

            pairs = _prepared_column_pairs(header_columns)
            names = [pair["name"] for pair in pairs]
            if not names:
                warnings.append(
                    f"'{file.filename}' no tiene columnas válidas para analizar."
                )
                continue

            try:
                predictions = metadata_analyzer.predict(names)
            except Exception as exc:
                logger.exception(
                    "Error al analizar nombres de '%s'", file.filename
                )
                warnings.append(
                    f"No se pudo analizar '{file.filename}': "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            for name in names:
                prediction = predictions.get(name)
                if prediction is None:
                    continue
                rows.append(
                    _metadata_column_row(
                        prediction,
                        file.filename,
                        file_index,
                        metadata_model_version,
                    )
                )
            continue

        try:
            df = _read_uploaded_file(
                file,
                _MAX_READ_ROWS,
                ratio=_SAMPLE_RATIO,
                min_rows=_ROW_MIN_ROWS,
            )
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
                apply_row_sampling=False,
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

        rows.extend(
            _result_to_row(result, file.filename, file_index) for result in results
        )

    if not rows:
        return _error(
            " | ".join(warnings) if warnings else "Ningún archivo pudo procesarse.",
            400,
        )

    response = {
        "status": "success",
        "analysis_mode": "columns",
        "content_analysis": bool(content_analysis),
        "columns_analysis": rows,
        "classification_version": INFORMATION_CLASSIFICATION_VERSION,
    }
    if warnings:
        response["warnings"] = warnings
    return jsonify(response)


@app.route("/analyze_column", methods=["POST"])
def analyze_column():
    if _ENGINE_IMPORT_ERROR:
        return _error(
            f"No se pudo importar pii_engine: {_ENGINE_IMPORT_ERROR}",
            503,
        )
    if _DEEP_IMPORT_ERROR:
        return _error(
            f"No se pudo importar el escáner profundo: {_DEEP_IMPORT_ERROR}",
            503,
        )

    file = request.files.get("file")
    if file is None or not file.filename:
        return _error("No se recibió el archivo para el análisis profundo.", 400)

    column = str(request.form.get("column") or "").strip()
    if not column:
        return _error("Debe indicar la columna a analizar.", 400)

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".csv", ".xls", ".xlsx", ".xlsm"}:
        return _error(
            "Formato no compatible con el análisis profundo. "
            "Se admiten archivos .csv o Excel.",
            400,
        )

    source_file = str(request.form.get("source_file") or file.filename)
    raw_source_index = request.form.get("source_index")
    try:
        source_index = int(raw_source_index) if raw_source_index not in (None, "") else None
    except (TypeError, ValueError):
        source_index = None

    try:
        original_column = _resolve_source_column(file, column, suffix)
    except ValueError as exc:
        return _error(str(exc), 400)

    try:
        adapter = get_adapter()
        analyzer = adapter.build_analyzer()
    except ModelArtifactError as exc:
        logger.exception("Error de artefacto al preparar el análisis profundo")
        return _error(str(exc), 503)
    except Exception as exc:
        logger.exception("No se pudo inicializar el análisis profundo")
        return _error(
            f"No se pudo inicializar el motor PII: {type(exc).__name__}: {exc}",
            503,
        )

    try:
        chunks = _iter_deep_chunks(file, suffix, original_column)
        result = scan_column_chunks(
            analyzer,
            chunks,
            column,
            DeepScanOptions(
                max_rows=_DEEP_MAX_ROWS,
                chunk_size=_DEEP_CHUNK_SIZE,
                include_model=_DEEP_INCLUDE_MODEL,
            ),
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    except ModelArtifactError as exc:
        logger.exception("Error de artefacto durante el análisis profundo")
        return _error(str(exc), 503)
    except Exception as exc:
        logger.exception("Error durante el análisis profundo de '%s'", column)
        return _error(
            f"No se pudo completar el análisis profundo: "
            f"{type(exc).__name__}: {exc}",
            500,
        )

    payload = result.to_dict()
    payload["source_file"] = source_file
    payload["source_index"] = source_index

    enriched_entities = []
    for entity in payload.get("entities", []):
        item = dict(entity)
        item.update(
            _classify_information(
                {
                    "pii": True,
                    "entity": item.get("entity"),
                    "needs_review": False,
                }
            )
        )
        enriched_entities.append(item)
    payload["entities"] = enriched_entities

    return jsonify({
        "status": "success",
        "column_analysis": payload,
        "classification_version": INFORMATION_CLASSIFICATION_VERSION,
    })


EXPORT_XLSX_HEADER = ["Archivo", "Nombre de Columna", "Prediccion",
                      "Probabilidad PII (%)", "Evidencia", "Requiere revision",
                      "Entidad", "Probabilidad Entidad (%)", "Muestra",
                      "Clasificación", "Grupo",
                      "Revisión clasificación"]


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
                "message": f"openpyxl no disponible: {_XLSX_ERROR}. "
                "Se sugiere utilizar el formato CSV.",
            }), 500
        payload = request.get_json(force=True, silent=True) or {}
        rows = payload.get("rows") or []
        if not isinstance(rows, list) or not rows:
            return jsonify(
                {"status": "error", "message": "No hay filas para exportar."}
            ), 400

        header = payload.get("header")
        if not isinstance(header, list) or not header:
            header = EXPORT_XLSX_HEADER
        header = [str(h) for h in header][:50]
        col_count = len(header)

        wb = Workbook()
        ws = wb.active
        ws.title = "PII"
        ws.freeze_panes = "A2"
        head_fill = PatternFill("solid", fgColor="003746")
        head_font = Font(bold=True, color="FFFFFF")
        ws.append(header)
        for cell in ws[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(vertical="center")
        for r in rows:
            vals = list(r)[:col_count]
            vals += [""] * max(0, col_count - len(vals))
            out = []
            for idx, v in enumerate(vals):
                label = header[idx]
                if label in ("Probabilidad PII (%)", "Probabilidad Entidad (%)") and v not in (None, ""):
                    try:
                        out.append(float(v))
                        continue
                    except (TypeError, ValueError):
                        pass
                if label == "Muestra" and v not in (None, ""):
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
        for idx, label in enumerate(header, start=1):
            if label in ("Probabilidad PII (%)", "Probabilidad Entidad (%)"):
                for row in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
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

