# Cambios PII en `backend.py`

Referencia: `data_gate/backend.py` local (1452 líneas). Los números de línea son los del archivo actual; al insertar bloques, replica de abajo hacia arriba para evitar desplazamientos.

## 1. Líneas 24–32 — Import protegido

Insertar después de `from flask import request, jsonify, send_from_directory, abort` y antes de `DRY_RUN = False`.

```python
try:
    from pii_engine.adapters.dataiku_adapter import DataikuAdapter, ModelArtifactError
    PII_ENGINE_AVAILABLE = True
    PII_IMPORT_ERROR = None
except Exception as _pii_exc:
    DataikuAdapter = None
    ModelArtifactError = RuntimeError
    PII_ENGINE_AVAILABLE = False
    PII_IMPORT_ERROR = f"{type(_pii_exc).__name__}: {_pii_exc}"
```

## 2. Líneas 230–392 — Helpers PII

Insertar después de `validate_snow_request()` y antes del bloque `# DSS API (dataiku-api-client)`.

```python
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
                binary_folder=os.environ.get("PII_BINARY_FOLDER", "model_binary_v1"),
                entity_folder=os.environ.get("PII_ENTITY_FOLDER", "model_entity_v1"),
                content_folder=os.environ.get("PII_CONTENT_FOLDER", "model_content_v1"),
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

    flagged = []
    for item in raw_columns:
        if not isinstance(item, dict):
            continue
        is_pii = bool(item.get("pii"))
        needs_review = bool(item.get("needs_review"))
        if not (is_pii or needs_review):
            continue
        flagged.append({
            "column": str(item.get("column") or "")[:300],
            "pii": is_pii,
            "prob_pii": _pii_float(item.get("prob_pii")),
            "entity": str(item.get("entity") or "")[:100],
            "prob_entity": _pii_float(item.get("prob_entity")),
            "evidence": str(item.get("evidence") or "")[:40],
            "match_rate": _pii_float(item.get("match_rate")),
            "sample_size": _pii_int(item.get("sample_size")),
            "needs_review": needs_review,
            "classification": _pii_int(item.get("classification"), 1),
        })
        if len(flagged) >= _PII_MAX_RECORD_COLUMNS:
            break

    return {
        "status": "ok",
        "engine_version": str(raw.get("engine_version") or "")[:40],
        "model_version": str(raw.get("model_version") or "")[:300],
        "config_version": str(raw.get("config_version") or "")[:40],
        "analyzed_columns": _pii_int(raw.get("analyzed_columns"), len(raw_columns)),
        "pii_columns": _pii_int(raw.get("pii_columns"),
                                sum(1 for c in raw_columns if isinstance(c, dict) and c.get("pii"))),
        "needs_review": _pii_int(raw.get("needs_review"),
                                 sum(1 for c in raw_columns if isinstance(c, dict) and c.get("needs_review"))),
        "columns": flagged,
        "truncated": len(flagged) >= _PII_MAX_RECORD_COLUMNS,
    }
```

## 3. Línea 964 — `/validate-file`

Insertar dentro de `/validate-file`, después de `payload["sheets"] = excel_sheets` y antes de `return jsonify(payload)`.

```python
            payload["pii"] = _run_pii_analysis(df, file.filename)
```

## 4. Línea 986 — `/upload-sas`

Insertar dentro de `/upload-sas`, justo después de `metadata = json.loads(request.form.get('metadata', '{}'))`.

```python
        pii_summary = _sanitize_pii_summary(metadata.get('pii_summary'))
```

## 5. Línea 1051 — Campo en `record`

Insertar dentro del diccionario `record`, después de `"snow_request": snow_request,`.

```python
            "pii": pii_summary,
```

## 6. Líneas 1439–1452 — `/pii/health`

Insertar después de la ruta `/health` existente.

```python
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
```
