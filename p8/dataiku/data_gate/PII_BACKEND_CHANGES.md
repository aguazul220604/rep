# Cambios PII en `backend.py` — v2 optimizada

Referencia: `pii_engine` actual (`adapters/dataiku_adapter.py`, `core/sampling.py`,
`core/fusion.py`, `recognizers/validators.py`, `config/entities.yaml`) y las fotos
de `data_gate/changes/`.

> **Principio v2.** El motor es la única fuente de detección. El backend solo
> resuelve la entrada, acota filas, llama a `analyze_dataframe()` una vez y
> persiste metadatos. No se reimplementan regex, validadores, umbrales ni reglas
> de nombre en la webapp.

---

## 0. Resumen

La v1 documentaba la integración base del backend. Esta v2 la reemplaza y conserva
lo esencial (import protegido, adapter por managed folder, `/pii/health`,
sanitización y persistencia). La v2 elimina además la **capa extra de reglas** que
se añadió después (fotos `changes/*.jpeg`, aproximadamente líneas 604–961 del
archivo del compañero) porque duplica capacidades que ya existen en `pii_engine`
y, en un caso, contradice su diseño validado.

Beneficios esperados:

- ~400–500 líneas menos en el backend.
- Una sola pasada de detección (sin la segunda pasada de hasta 30 s).
- Sin falsos positivos por heurísticas paralelas.
- Resultados consistentes con la calibración y los tests del motor.
- No requiere cambios en `app.js` ni `index.html`: el frontend no consume `rule`,
  `skipped_columns` ni `sheet`.

---

## 1. Código del compañero a ELIMINAR

Eliminar por completo (constantes, funciones y su uso):

| Bloque | Motivo |
|---|---|
| `_PII_EMAIL_RE`, `_PII_NUMBER_RE`, `_PII_TOKEN_RE`, `_pii_mask_number`, `_pii_mask_evidence` | `evidence` es un enum del motor (`name`/`content`/`both`, ver `core/fusion.py`); nunca contiene valores. Enmascararlo es no-op y cuesta O(filas × columnas). |
| `_pii_csv_frame` | Sustituido por `_pii_source_frame` (una sola lectura con `sep=None` + `dtype=str`). |
| `_pii_rules_frame` | El motor ya lee CSV con `dtype=str` y preserva ceros iniciales (`adapters/dataiku_adapter.py`, `read_csv_sample`). |
| `_PII_RULE_MAX_ROWS`, `_PII_RULE_MAX_CHARS`, `_PII_RULE_MAX_SECONDS`, `_PII_RULE_MAX_CELLS`, `_PII_RULE_MIN_RATE`, `_PII_ID_COLUMN_SHARE`, `_PII_ID_COLUMN_PASS`, `_PII_ID_COLUMN_MIN`, `_PII_NEAR_MISS_MIN` | Umbrales paralelos a `config/thresholds.yaml`; el motor ya muestrea y decide. |
| `_PII_RULE_EMAIL_RE`, `_PII_RULE_NUMBER_RE`, `_PII_RULE_CURP_RE`, `_PII_RFC_DATE`, `_PII_RFC_PERSON_RE`, `_PII_RFC_COMPANY_RE` | CURP/RFC/correo ya están en `config/entities.yaml` (grupo A) con validadores en `recognizers/validators.py`. |
| `_PII_NAME_TOKENS`, `_PII_NAME_PHRASES`, `_PII_NAME_EXCLUDE`, `_pii_name_hint` | Cubierto por los modelos de nombre (`core/name_models.py`) y `regex.txt`. |
| `_pii_luhn_ok`, `_pii_card_shape_ok`, `_pii_clabe_ok`, `_pii_checksum_verdict`, `_pii_format_verdict`, `_pii_scan_values` | Duplican `tarjeta`, `clabe`, `curp`, `rfc`, `correo` y sus validadores. |
| `_pii_apply_rules` | Segunda pasada sobre filas/celdas; el motor ya hace muestreo, contexto y fusión calibrada. |
| `_run_pii_engine` (wrapper) | Se colapsa en `_run_pii_analysis`. |
| `_pii_float` con `math.isinf` | Bug: devuelve `default` para valores finitos. Se reemplaza abajo. |
| `skipped_columns`, `rule`, `rule_columns`, `rule_skipped_columns` en el summary | Campos que solo existían por la capa de reglas. |
| `Response` en el import de Flask | Solo conservarlo si el archivo lo usa en otro endpoint. |

> **Caso crítico:** `_pii_clabe_ok` rellenaba con ceros un CLABE de 16–17 dígitos.
> `pii_engine/tests/test_engine.py` tiene una prueba explícita
> (`test_clabe_without_leading_zero_is_not_validated`) que exige que un CLABE de
> 17 dígitos **no** se valide, para no convertir identificadores numéricos en
> falsos positivos. Esa heurística debe desaparecer.

---

## 2. Código del compañero a CONSERVAR

- `_pii_decode` — los SAS llegan como `bytes` y el motor los convertiría en `"b'...'"`.
- `_pii_hide_columns` / `_records_for_viewer` — ocultan nombres de columna y
  `evidence` a usuarios que no son Approver (privacidad; no es lógica del motor).
- `sheet` en el summary y `sheet_mismatch` en `/upload-sas`.
- `_pii_int` con `OverflowError`.

---

## 3. Imports y adapter (sin cambios de fondo)

Añadir `import math` junto a los imports estándar si no existe.

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

```python
_PII_ADAPTER = None
_PII_ADAPTER_LOCK = threading.Lock()
_PII_MAX_ROWS = 50000
_PII_MAX_RECORD_COLUMNS = 200
_PII_EVIDENCE_ALLOWED = {"name", "content", "both"}


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
                model_filename=os.environ.get("PII_MODEL_FILENAME", "model.pkl"),
                strict_models=True,
            )
        return _PII_ADAPTER
```

---

## 4. Helpers optimizados (reemplazan el bloque del compañero)

Insertar después de `validate_snow_request()` y antes del bloque
`# DSS API (dataiku-api-client)`.

```python
def _pii_decode(value):
    """SAS text arrives as bytes; legacy files are usually cp1252, not UTF-8."""
    if not isinstance(value, (bytes, bytearray)):
        return value
    try:
        return bytes(value).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(value).decode("cp1252", "replace")


def _pii_ready_dataframe(df, max_rows=_PII_MAX_ROWS):
    """Bound the frame, decode SAS bytes and normalize column names. Value
    sampling (unique values, NULL tokens, etc.) is the engine's job."""
    if df is None or getattr(df, "shape", (0, 0))[1] == 0:
        return df
    work = df
    try:
        from pii_engine.core.row_sampling import sample_dataframe

        work = sample_dataframe(work, max_rows, mode="random", seed=42)
    except Exception:
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
            work[col] = series.map(_pii_decode)
    return work


def _pii_source_frame(tmp_path, ext, df, sheet=None):
    """One bounded, text-preserving read for the engine.

    CSV is re-read with delimiter auto-detection and dtype=str so codes keep
    their leading zeros. Excel re-reads the selected sheet up to _PII_MAX_ROWS.
    SAS/XPT reuse the already parsed frame (bytes are decoded later)."""
    if ext == ".csv":
        try:
            import pandas as pd

            return pd.read_csv(
                tmp_path,
                sep=None,
                engine="python",
                dtype=str,
                nrows=_PII_MAX_ROWS,
            )
        except Exception as exc:
            print(f"[DataGate] PII CSV read failed, using validation frame: {exc}")
            return df
    if ext in EXCEL_EXTENSIONS and sheet:
        try:
            return _read_excel_workbook(
                tmp_path, ext, sheet=sheet, nrows=_PII_MAX_ROWS
            )[0]
        except Exception as exc:
            print(f"[DataGate] PII Excel read failed, using validation frame: {exc}")
            return df
    return df


def _pii_float(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(0.0, min(1.0, number))


def _pii_int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _pii_summary_from_results(results):
    from pii_engine.core.schema import ENGINE_VERSION

    rows = []
    for result in results:
        d = result.to_dict()
        rows.append({
            "column": str(d.get("column") or ""),
            "pii": bool(d.get("pii")),
            "prob_pii": _pii_float(d.get("prob_pii")),
            "entity": str(d.get("entity") or ""),
            "prob_entity": _pii_float(d.get("prob_entity")),
            "evidence": str(d.get("evidence") or ""),
            "match_rate": _pii_float(d.get("match_rate")),
            "sample_size": _pii_int(d.get("sample_size")),
            "needs_review": bool(d.get("needs_review")),
            "classification": _pii_int(d.get("classification"), 1),
        })
    first = results[0].to_dict() if results else {}
    return {
        "status": "ok",
        "engine_version": str(first.get("engine_version") or ENGINE_VERSION),
        "model_version": str(first.get("model_version") or "rules_fallback"),
        "config_version": str(first.get("config_version") or "1.0"),
        "analyzed_columns": len(rows),
        "pii_columns": sum(1 for r in rows if r["pii"]),
        "needs_review": sum(1 for r in rows if r["needs_review"]),
        "columns": rows,
    }


def _run_pii_analysis(df, table_name):
    """Run the engine once over a bounded frame. Never raises: validation must
    still succeed when the engine is unavailable."""
    if not PII_ENGINE_AVAILABLE:
        return {"status": "unavailable", "error": PII_IMPORT_ERROR}
    try:
        frame = _pii_ready_dataframe(df)
        if frame is None or getattr(frame, "shape", (0, 0))[1] == 0:
            return {"status": "unavailable", "error": "No tabular data to analyze."}
        results = _get_pii_adapter().analyze_dataframe(
            frame,
            table_name=str(table_name or "upload"),
            sample_size=2000,
            apply_row_sampling=False,
        )
        if not results:
            return {"status": "unavailable", "error": "The PII engine returned no results."}
        return _pii_summary_from_results(results)
    except Exception as exc:
        traceback.print_exc()
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _sanitize_pii_summary(raw):
    """Whitelist the client-supplied summary before persisting it. Only engine
    metadata is accepted; unknown fields and sample values are dropped."""
    if not isinstance(raw, dict):
        return {"status": "unavailable"}
    status = str(raw.get("status") or "unavailable").lower()
    raw_columns = raw.get("columns")
    if not isinstance(raw_columns, list):
        raw_columns = []
    valid_columns = [item for item in raw_columns if isinstance(item, dict)]

    stored_columns = []
    for item in valid_columns:
        evidence = str(item.get("evidence") or "")
        if evidence not in _PII_EVIDENCE_ALLOWED:
            evidence = ""
        stored_columns.append({
            "column": str(item.get("column") or "")[:300],
            "pii": bool(item.get("pii")),
            "prob_pii": _pii_float(item.get("prob_pii")),
            "entity": str(item.get("entity") or "")[:100],
            "prob_entity": _pii_float(item.get("prob_entity")),
            "evidence": evidence[:40],
            "match_rate": _pii_float(item.get("match_rate")),
            "sample_size": _pii_int(item.get("sample_size")),
            "needs_review": bool(item.get("needs_review")),
            "classification": _pii_int(item.get("classification"), 1),
        })
        if len(stored_columns) >= _PII_MAX_RECORD_COLUMNS:
            break

    if status != "ok":
        clean_status = status if status in ("error", "unavailable") else "unavailable"
        partial = {"status": clean_status, "error": str(raw.get("error") or "")[:300]}
        if stored_columns:
            partial.update({
                "columns": stored_columns,
                "pii_columns": sum(1 for c in stored_columns if c["pii"]),
                "needs_review": sum(1 for c in stored_columns if c["needs_review"]),
            })
        return partial

    analyzed_columns = (
        len(valid_columns) if valid_columns
        else _pii_int(raw.get("analyzed_columns"), 0)
    )
    pii_columns = (
        sum(1 for c in valid_columns if c.get("pii")) if valid_columns
        else _pii_int(raw.get("pii_columns"), 0)
    )
    needs_review = (
        sum(1 for c in valid_columns if c.get("needs_review")) if valid_columns
        else _pii_int(raw.get("needs_review"), 0)
    )

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
        "sheet": str(raw.get("sheet") or "")[:100],
    }
```

> **Por qué `apply_row_sampling=False`:** el backend ya acota a `_PII_MAX_ROWS`
> con muestreo aleatorio en `_pii_ready_dataframe`. Volver a muestrear el 2% en el
> adapter duplicaría el trabajo y reduciría la cobertura a ~1,000 filas. El
> muestreo de **valores únicos por columna** sigue haciéndolo el motor en
> `core/sampling.py`.

---

## 5. `/validate-file`

Reemplazar la línea que asignaba `payload["pii"]` por:

```python
            pii_sheet = excel_sheets[0] if excel_sheets else None
            pii_frame = _pii_source_frame(tmp_path, ext, df, pii_sheet)
            payload["pii"] = _run_pii_analysis(pii_frame, file.filename)
            if pii_sheet and payload["pii"].get("status") == "ok":
                payload["pii"]["sheet"] = pii_sheet
```

Con esto:

- CSV se lee **una sola vez para PII** con `sep=None` (detecta `,` `;` `|` `\t`)
  y `dtype=str` (conserva ceros iniciales).
- Excel analiza hasta 50,000 filas de la hoja seleccionada.
- SAS/XPT reutiliza el DataFrame ya leído y `_pii_ready_dataframe` decodifica bytes.
- Se elimina la triple lectura (`df` de validación + `_pii_csv_frame` +
  `_pii_rules_frame`).

> **Nota sobre CSV enormes:** `nrows=_PII_MAX_ROWS` acota la lectura a las primeras
> 50,000 filas. Si se requiere muestreo uniforme sobre todo el archivo, se puede
> sustituir esa lectura por `read_csv_sample` (exportado por
> `pii_engine.adapters.dataiku_adapter`) con `mode="random"`, `max_rows=_PII_MAX_ROWS`,
> `dtype=str` y el separador detectado con `csv.Sniffer`. No es obligatorio para la
> optimización: el motor sigue deduplicando y muestreando valores por columna.

---

## 6. `/upload-sas`

Después de `metadata = json.loads(...)`:

```python
        pii_summary = _sanitize_pii_summary(metadata.get('pii_summary'))
        if (
            pii_summary.get("sheet")
            and metadata.get('sheet')
            and pii_summary["sheet"] != metadata.get('sheet')
        ):
            pii_summary["sheet_mismatch"] = True
```

Dentro del diccionario `record`, después de `"snow_request": snow_request,`:

```python
            "pii": pii_summary,
```

---

## 7. `/pii/health` (sin cambios)

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

---

## 8. Privacidad por rol (conservar del compañero)

Mantener `_viewer_is_approver()`, `_pii_hide_columns()` y `_records_for_viewer()`.
El motor produce `columns` con nombres y `evidence`; los conteos
(`pii_columns`, `needs_review`) deben seguir visibles para todos, pero los
detalles de columna solo para Approver.

```python
def _pii_hide_columns(records):
    # Counts stay visible; column names and evidence are Approver-only
    hidden = []
    for rec in records:
        if isinstance(rec.get("pii"), dict) and "columns" in rec["pii"]:
            pii = {k: v for k, v in rec["pii"].items() if k != "columns"}
            pii["columns_hidden"] = True
            rec = dict(rec, pii=pii)
        hidden.append(rec)
    return hidden


def _records_for_viewer(records):
    return records if _viewer_is_approver() else _pii_hide_columns(records)
```

Aplicarlo en los endpoints que devuelven `upload_records` a usuarios normales
(`/my-uploads`, `/upload-history`). No aplicarlo en `/pending-approvals`
(solo Approver).

---

## 9. `webapp_v2_1/backend.py` — wiring por managed folder

En `get_adapter()` mantener:

```python
_adapter = DataikuAdapter(
    binary_folder=os.getenv("PII_BINARY_FOLDER", "model_binary_v1"),
    entity_folder=os.getenv("PII_ENTITY_FOLDER", "model_entity_v1"),
    content_folder=os.getenv("PII_CONTENT_FOLDER", "model_content_v1"),
    model_filename=os.getenv("PII_MODEL_FILENAME", "model.pkl"),
    max_read_rows=_MAX_READ_ROWS,
    strict_models=True,
)
```

Sin cambios en `process_table()`, `_result_to_row()` ni `health()`.

---

## 10. Variables de entorno

| Variable | Default | Uso |
|---|---|---|
| `PII_BINARY_FOLDER` | `model_binary_v1` | Managed folder del modelo binario |
| `PII_ENTITY_FOLDER` | `model_entity_v1` | Managed folder del modelo de entidad |
| `PII_CONTENT_FOLDER` | `model_content_v1` | Managed folder del modelo de contenido |
| `PII_MODEL_FILENAME` | `model.pkl` | Nombre del artefacto en cada folder |
| `PII_MAX_READ_ROWS` | `50000` | Límite de lectura del adapter |
| `PII_MAX_UPLOAD_MB` | `0` | Límite de subida (`0`/`none` = sin límite) |

Variables obsoletas: `PII_MODELS_FOLDER`, `PII_BINARY_MODEL_PATH`,
`PII_ENTITY_MODEL_PATH`, `PII_CONTENT_MODEL_PATH`.

No hay variables nuevas. Las constantes `_PII_RULE_*` desaparecen.

El code env del backend debe incluir `cloudpickle` y `lightgbm`, además de
`joblib`, `scikit-learn`, `scipy`, `pandas`, `PyYAML` y `dataiku`.

---

## 11. Verificación

1. `GET /pii/health` → `engine_ready=true` y `model_version` con los tres modelos.
2. CSV con separador `;` o `|` → el motor detecta las columnas (no una sola).
3. CSV con CLABE de 18 dígitos y ceros iniciales → `entity="clabe"`.
4. CLABE de 17 dígitos (sin cero inicial) → **no** PII (alineado a
   `test_clabe_without_leading_zero_is_not_validated`).
5. Excel multi-hoja → `pii.sheet` coincide con la hoja validada; si en el upload
   se cambia de hoja, `sheet_mismatch=true`.
6. SAS7BDAT/XPT con columnas de texto → sin valores `b'...'` en el análisis.
7. `/my-uploads` como usuario normal → `pii.columns` ausente y
   `columns_hidden=true`; como Approver → columnas presentes.
8. `/upload-sas` → `record["pii"]` solo contiene campos del motor + `sheet`.
9. Confirmar en DSS los managed folders:
   - `model_binary_v1/model.pkl`
   - `model_entity_v1/model.pkl`
   - `model_content_v1/model.pkl`

---

## 12. Trazabilidad foto → decisión

| Foto (aprox.) | Contenido | Decisión |
|---|---|---|
| 1 | Import con `Response` | Conservar solo si se usa; si no, quitar |
| 2–3 | `_pii_csv_frame`, `_get_pii_adapter` | Reemplazar por `_pii_source_frame`; adapter igual |
| 3–4 | `_pii_ready_dataframe`, `_pii_decode` | Conservar `_pii_decode`; simplificar el resto |
| 4 | `_pii_summary_from_results`, `_run_pii_engine` | Reemplazar; eliminar wrapper |
| 5 | `_PII_EMAIL/NUMBER/TOKEN_RE`, `_pii_mask_*` | Eliminar |
| 6 | Reglas CURP/RFC/nombre y Luhn/card/CLABE | Eliminar |
| 7–9 | `_pii_scan_values`, `_pii_name_hint`, `_pii_rules_frame`, `_pii_apply_rules` | Eliminar |
| 9 | `_run_pii_analysis` con `rules_df` | Reemplazar por versión de una pasada |
| 9–10 | `_pii_float`, `_pii_int`, `_sanitize_pii_summary` | Corregir `_pii_float`; sanear sin `rule*` |
| 10 | `_viewer_is_approver`, `_pii_hide_columns`, `_records_for_viewer` | Conservar |
| 11 | `/validate-file` con `pii_df`/`_pii_rules_frame` | Reemplazar por `_pii_source_frame` |
| 11–12 | `/upload-sas` con `sheet_mismatch`, `record["pii"]` | Conservar |
| 12 | `/pii/health` | Conservar |
