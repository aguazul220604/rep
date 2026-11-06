# Parche PII para el `backend_dg.py` del servidor (DataGate)

Este documento es el **anexo de aplicación en servidor** de
`PII_BACKEND_CHANGES.md`. Contiene únicamente los bloques que deben quedar en el
`backend_dg.py` de la webapp DataGate en Dataiku, con la acción exacta
(reemplazar, insertar o eliminar) y la verificación posterior.

> **Archivo destino:** `backend_dg.py` de la webapp DataGate en Dataiku DSS.
> **Referencia local:** `data_gate/backend.py` (ya alineado con la sección 13 de
> `PII_BACKEND_CHANGES.md`).
> **Los números de línea son aproximados** (se tomaron de las fotos de
> `data_gate/changes_2/`); guíate por el nombre de la función y su contenido.

---

## 0. Contexto: por qué `clabe` no se detectaba

Síntoma: en DataGate la columna `d` (CLABE) salía `entity="no aplica"` y
`PII=NO`, mientras que `webapp_v2_1` la detectaba como `clabe` con 99.9%.

Causa encontrada en el servidor:

1. Existían **dos** definiciones de `_pii_source_frame`:
   - La nueva (sección 13.4.4), que lee con `read_csv_sample(..., dtype=str)` y
     muestreo 2% / mínimo 2,000 / tope 50,000.
   - La vieja, que usaba `pd.read_csv(..., nrows=_PII_MAX_ROWS)`.
2. Python se queda con la **última** definición, así que ganaba la vieja.
3. `_PII_MAX_ROWS` ya se había eliminado, por lo que la vieja lanzaba
   `NameError`, caía en su `except` y devolvía el `df` de validación.
4. Ese `df` se lee en `/validate-file` con `pd.read_csv(tmp_path, nrows=1000)`
   **sin `dtype=str`**. Pandas interpreta el CLABE de 18 dígitos con cero
   inicial como `int64` y **pierde el cero**: quedan 17 dígitos.
5. `validate_clabe` exige 18 dígitos (`^\d{18}$` + checksum). Un CLABE de 17
   dígitos **no** debe validarse (ver
   `pii_engine/tests/test_engine.py::test_clabe_without_leading_zero_is_not_validated`).
   Resultado: `no aplica` / `PII=NO`.

`webapp_v2_1` sí conserva los ceros porque lee con
`read_csv_sample(..., dtype=str)`. De ahí la diferencia entre ambas webapps.

---

## 1. Resumen de cambios

| # | Bloque | Acción en el servidor |
|---|---|---|
| 1 | `import math` | Insertar si no existe |
| 2 | Constantes `_PII_*` | Quitar `_PII_MAX_ROWS`; agregar `_PII_EVIDENCE_ALLOWED` |
| 3 | `_pii_env_int`, `_pii_sampling_defaults`, `_pii_sample_rows` | Verificar que existan (sección 13.4.2) |
| 4 | `_pii_ready_dataframe` | Reemplazar el stub por la versión completa |
| 5 | `_pii_source_frame` | **Conservar la nueva y eliminar la vieja duplicada** |
| 6 | `_pii_summary_from_results` | Reemplazar para usar `_pii_float` / `_pii_int` |
| 7 | `_run_pii_analysis` | Usar `apply_row_sampling=False` |
| 8 | `_pii_float`, `_pii_int` | Reemplazar por las versiones corregidas |
| 9 | `_sanitize_pii_summary` | Reemplazar por la versión completa (evidencia/sheet) |
| 10 | `/validate-file` | Dejar una sola asignación de `pii_sheet`/`pii_frame` |
| 11 | `/upload-sas` | Dejar un solo bloque `_sanitize_pii_summary` + `sheet_mismatch` |

---

## 2. Bloques exactos

### 2.1 Imports

**Acción:** verificar que exista `import math` junto a los imports estándar.

```python
import dataiku
import json
import math
import os
import re
import hashlib
import importlib
```

### 2.2 Constantes PII

**Acción:** el bloque debe quedar así. **Eliminar `_PII_MAX_ROWS`** si todavía
existe y **agregar** `_PII_EVIDENCE_ALLOWED`.

```python
_PII_ADAPTER = None
_PII_ADAPTER_LOCK = threading.Lock()
_PII_MAX_RECORD_COLUMNS = 200
_PII_EVIDENCE_ALLOWED = {"name", "content", "both"}
```

### 2.3 Helpers de muestreo

**Acción:** deben existir tal cual (sección 13.4.2). Si ya están en el servidor,
solo verifica que coincidan.

```python
def _pii_env_int(name, default):
    """Read an optional integer env var, treating 0/none as 'no limit'."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"", "0", "none", "null", "unlimited", "sin_limite", "sin límite"}:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return default

def _pii_sampling_defaults():
    """Return (row_ratio, min_rows, max_rows) from the engine config, with the
    same env overrides supported by webapp_v2_1."""
    try:
        from pii_engine.adapters.dataiku_adapter import (
            DEFAULT_ROW_MAX_ROWS,
            DEFAULT_ROW_MIN_ROWS,
            DEFAULT_ROW_RATIO,
        )
    except Exception:
        DEFAULT_ROW_RATIO, DEFAULT_ROW_MIN_ROWS, DEFAULT_ROW_MAX_ROWS = 0.02, 2000, 50000

    ratio = DEFAULT_ROW_RATIO if DEFAULT_ROW_RATIO is not None else 0.02
    min_rows = DEFAULT_ROW_MIN_ROWS if DEFAULT_ROW_MIN_ROWS is not None else 2000
    max_rows = DEFAULT_ROW_MAX_ROWS if DEFAULT_ROW_MAX_ROWS is not None else 50000

    env_ratio = os.environ.get("PII_SAMPLE_RATIO")
    if env_ratio is not None:
        try:
            ratio = float(env_ratio)
        except (TypeError, ValueError):
            pass
    min_rows = _pii_env_int("PII_SAMPLE_MIN_ROWS", min_rows)
    max_rows = _pii_env_int("PII_MAX_READ_ROWS", max_rows)
    if min_rows is not None and max_rows is not None and min_rows > max_rows:
        min_rows = max_rows
    return float(ratio), min_rows, max_rows

def _pii_sample_rows(df):
    """Apply the engine's row sampling (2% with a 2,000-row floor and 50,000 cap
    by default) to an already materialized frame."""
    if df is None:
        return df
    ratio, min_rows, max_rows = _pii_sampling_defaults()
    try:
        from pii_engine.core.row_sampling import sample_dataframe

        return sample_dataframe(
            df,
            max_rows=max_rows,
            mode="random",
            seed=42,
            ratio=ratio,
            min_rows=min_rows,
        )
    except Exception as exc:
        print(f"[DataGate] PII row sampling failed, using source frame: {exc}")
        try:
            if max_rows is not None and len(df) > max_rows:
                return df.sample(n=max_rows, random_state=42)
        except Exception:
            pass
        return df
```

### 2.4 `_pii_ready_dataframe`

**Acción:** reemplazar la versión actual (la que solo hace `df.copy()`) por esta.
Es la que normaliza nombres de columna y decodifica bytes de SAS/XPT.

```python
def _pii_ready_dataframe(df):
    """Normalize the sampled frame for the PII engine: string column names,
    decoded SAS bytes and unique names. Row sampling is done by
    `_pii_source_frame` so CSV, Excel and SAS use the same policy."""
    if df is None or getattr(df, "shape", (0, 0))[1] == 0:
        return df
    work = df.copy()

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
```

### 2.5 `_pii_source_frame` (el bloque crítico)

**Acción:** debe existir **una sola** definición. Conserva esta:

```python
def _pii_source_frame(tmp_path, ext, df, sheet=None):
    """Build the PII input sample with the same row policy as webapp_v2_1:
    2% of the table, with a 2,000-row floor and a 50,000-row cap. CSV is
    re-read as text so codes keep their leading zeros; Excel is re-read from
    the selected sheet; SAS/XPT reuse the already parsed frame."""
    ratio, min_rows, max_rows = _pii_sampling_defaults()
    if ext == '.csv':
        try:
            from pii_engine.adapters.dataiku_adapter import read_csv_sample

            return read_csv_sample(
                tmp_path,
                max_rows=max_rows,
                dtype=str,
                seed=42,
                mode="random",
                ratio=ratio,
                min_rows=min_rows,
            )
        except Exception as exc:
            print(f"[DataGate] PII CSV read failed, using validation frame: {exc}")
            return _pii_sample_rows(df)
    if ext in EXCEL_EXTENSIONS:
        try:
            from pii_engine.adapters.dataiku_adapter import read_excel_sample

            return read_excel_sample(
                tmp_path,
                max_rows=max_rows,
                sheet=sheet,
                seed=42,
                mode="random",
                ratio=ratio,
                min_rows=min_rows,
            )
        except Exception as exc:
            print(f"[DataGate] PII Excel read failed, using validation frame: {exc}")
            return _pii_sample_rows(df)
    return _pii_sample_rows(df)
```

**Eliminar por completo** cualquier otra definición de `_pii_source_frame` que
aparezca después. En particular, la que contiene:

```python
import pandas as pd
...
nrows=_PII_MAX_ROWS
...
_read_excel_workbook(tmp_path, ext, sheet=sheet, nrows=_PII_MAX_ROWS)
```

Esa versión vieja es la que anula la lectura con `dtype=str` y provoca el fallo
de `clabe`.

### 2.6 `_pii_summary_from_results`

**Acción:** reemplazar para que normalice con `_pii_float` / `_pii_int`.

```python
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
```

### 2.7 `_run_pii_analysis`

**Acción:** evitar el doble muestreo. La llamada debe usar
`apply_row_sampling=False` y **no** pasar `sample_size`.

```python
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
            frame,
            table_name=str(table_name or "upload"),
            apply_row_sampling=False,
        )
        return _pii_summary_from_results(results)
    except Exception as exc:
        traceback.print_exc()
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
```

### 2.8 `_pii_float` y `_pii_int`

**Acción:** reemplazar por las versiones con `math.isfinite` y `OverflowError`.

```python
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
```

### 2.9 `_sanitize_pii_summary`

**Acción:** reemplazar por la versión completa (whitelist de `evidence`,
manejo parcial de `error`/`unavailable`, `truncated` y `sheet`).

```python
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

### 2.10 `/validate-file`

**Acción:** debe existir una sola asignación de `pii_sheet` / `pii_frame`. Si el
servidor las tiene duplicadas, deja este bloque:

```python
            pii_sheet = excel_sheets[0] if excel_sheets else None
            pii_frame = _pii_source_frame(tmp_path, ext, df, pii_sheet)
            payload["pii"] = _run_pii_analysis(pii_frame, file.filename)
            if pii_sheet and payload["pii"].get("status") == "ok":
                payload["pii"]["sheet"] = pii_sheet
            return jsonify(payload)
```

### 2.11 `/upload-sas`

**Acción:** debe existir un solo bloque de sanitización. Si el servidor lo tiene
duplicado, deja este:

```python
        metadata = json.loads(request.form.get('metadata', '{}'))
        pii_summary = _sanitize_pii_summary(metadata.get('pii_summary'))
        if (
            pii_summary.get("sheet")
            and metadata.get('sheet')
            and pii_summary["sheet"] != metadata.get('sheet')
        ):
            pii_summary["sheet_mismatch"] = True
        file_size = int(request.form.get('file_size_bytes', 0))
```

---

## 3. Checklist de búsquedas antes de reiniciar

En el `backend_dg.py` del servidor:

- [ ] `def _pii_source_frame` aparece **1 sola vez**.
- [ ] `_PII_MAX_ROWS` aparece **0 veces**.
- [ ] `import math` aparece **1 vez**.
- [ ] `_PII_EVIDENCE_ALLOWED` aparece (definición + uso).
- [ ] `apply_row_sampling=False` está en `_run_pii_analysis`.
- [ ] `dtype=str` está en la llamada a `read_csv_sample` dentro de
      `_pii_source_frame`.
- [ ] En `/validate-file` no hay `pii_sheet` repetido.
- [ ] En `/upload-sas` no hay `_sanitize_pii_summary` repetido.

Comandos útiles (dentro del servidor):

```bash
grep -n "def _pii_source_frame" backend_dg.py
grep -n "_PII_MAX_ROWS" backend_dg.py
grep -n "apply_row_sampling" backend_dg.py
grep -n "dtype=str" backend_dg.py
```

---

## 4. Reinicio

1. Respaldar el `backend_dg.py` actual.
2. Aplicar los bloques anteriores.
3. **Reiniciar la webapp / code env de DataGate.** No basta con refrescar el
   navegador: los defaults de muestreo de `pii_engine` se leen al importar.
4. Si se actualizó también `pii_engine`, reiniciar igualmente el code env.

---

## 5. Verificación

### 5.1 Health check

`GET /pii/health` debe reportar:

```json
{
  "sampling": {
    "row_ratio": 0.02,
    "row_min_rows": 2000,
    "row_max_rows": 50000,
    "max_read_rows": 50000
  }
}
```

### 5.2 Prueba del motor (sin webapp)

En el code env del servidor:

```python
from pii_engine.adapters.dataiku_adapter import read_csv_sample

df = read_csv_sample(
    "data_test_final (3).csv",
    max_rows=50000,
    dtype=str,
    seed=42,
    mode="random",
    ratio=0.02,
    min_rows=2000,
)
print(df["d"].head())
```

Deben verse valores de **18 caracteres**, conservando los ceros iniciales.

### 5.3 Prueba de punta a punta

Subir `data_test_final (3).csv` a DataGate y revisar la respuesta de
`/validate-file`:

- Columna `d`: `pii=true`.
- `entity="clabe"`.
- `sample_size` igual al total de filas si el archivo tiene menos de 2,000
  (para 40 filas debe ser 40).
- `match_rate` cercano a 100%.
- Resultado equivalente a `webapp_v2_1`.

---

## 6. Rollback

Si algo falla:

1. Restaurar el `backend_dg.py` respaldado.
2. Reiniciar la webapp / code env.
3. Confirmar con `GET /pii/health` que el motor sigue `engine_ready=true`.

---

## 7. Relación con `PII_BACKEND_CHANGES.md`

| Sección de `PII_BACKEND_CHANGES.md` | Bloque de este documento |
|---|---|
| 3. Imports y adapter | 2.1 |
| 4. Helpers optimizados | 2.4, 2.6, 2.7, 2.8, 2.9 |
| 5. `/validate-file` | 2.10 |
| 6. `/upload-sas` | 2.11 |
| 13.4.1 Eliminar `_PII_MAX_ROWS` | 2.2 |
| 13.4.2 Helpers de muestreo | 2.3 |
| 13.4.3 `_pii_ready_dataframe` sin muestreo | 2.4 |
| 13.4.4 `_pii_source_frame` | 2.5 |
| 13.4.5 Evitar doble muestreo | 2.7 |
| 13.4.6 `/validate-file` usa la muestra PII | 2.10 |
