# -*- coding: utf-8 -*-
"""
Muestreo aleatorio de filas agnóstico de la fuente.

El motor debe analizar una muestra uniforme de filas y nunca las primeras N
(sesgo de orden). Este módulo implementa el muestreo de filas sin depender de
Dataiku ni de APIs de muestreo del origen:

  - `sample_dataframe`: muestreo sobre una tabla ya materializada.
  - `reservoir_sample`: muestreo uniforme sobre un iterable de chunks, con
    memoria O(k) (k = tamaño de muestra), apto para tablas de millones de filas.

El muestreo de valores únicos por columna vive en `pii_engine.core.sampling`.
"""
import math
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

try:
    import polars as pl
    HAS_POLARS = True
except ImportError:
    pl = None  # type: ignore
    HAS_POLARS = False

DEFAULT_SAMPLING_MODE = "random"
DEFAULT_ROW_SEED = 42
DEFAULT_ROW_RATIO = 0.02
VALID_SAMPLING_MODES = ("random", "head")


def normalize_sampling_mode(mode: Optional[str]) -> str:
    value = str(mode or DEFAULT_SAMPLING_MODE).strip().lower()
    if value not in VALID_SAMPLING_MODES:
        raise ValueError(
            f"sampling_mode inválido: {mode!r}. "
            f"Use uno de: {', '.join(VALID_SAMPLING_MODES)}"
        )
    return value


def _validate_max_rows(max_rows: int) -> int:
    value = int(max_rows)
    if value <= 0:
        raise ValueError("max_rows debe ser mayor que cero")
    return value


def _validate_row_ratio(ratio: float) -> float:
    value = float(ratio)
    if not 0 < value <= 1:
        raise ValueError("row_ratio debe estar en el rango (0, 1]")
    return value


def _validate_optional_rows(value: Optional[int], name: str) -> Optional[int]:
    if value is None:
        return None
    result = int(value)
    if result < 0:
        raise ValueError(f"{name} no puede ser negativo")
    return result


def resolve_row_sample_size(
    total_rows: int,
    ratio: float = DEFAULT_ROW_RATIO,
    min_rows: Optional[int] = None,
    max_rows: Optional[int] = None,
) -> int:
    """
    Calcula el número de filas a muestrear como `ceil(total_rows * ratio)`.

    `min_rows` y `max_rows` son límites opcionales; si son `None` no se aplican.
    El resultado nunca supera `total_rows`.
    """
    total = int(total_rows)
    if total < 0:
        raise ValueError("total_rows no puede ser negativo")
    ratio_value = _validate_row_ratio(ratio)
    min_value = _validate_optional_rows(min_rows, "min_rows")
    max_value = _validate_optional_rows(max_rows, "max_rows")
    if min_value is not None and max_value is not None and min_value > max_value:
        raise ValueError("min_rows no puede ser mayor que max_rows")
    if total == 0:
        return 0

    target = int(math.ceil(total * ratio_value))
    target = max(target, 1)
    if min_value is not None:
        target = max(target, min_value)
    if max_value is not None:
        target = min(target, max_value)
    return min(target, total)


def _is_polars_frame(obj: Any) -> bool:
    return HAS_POLARS and isinstance(obj, (pl.DataFrame, pl.LazyFrame))


def _to_pandas(obj: Any) -> pd.DataFrame:
    if _is_polars_frame(obj):
        if isinstance(obj, pl.LazyFrame):
            obj = obj.collect()
        return obj.to_pandas()
    if isinstance(obj, pd.DataFrame):
        return obj
    return pd.DataFrame(obj)


def _frame_len(obj: Any) -> int:
    if _is_polars_frame(obj):
        if isinstance(obj, pl.LazyFrame):
            obj = obj.collect()
        return int(obj.height)
    if isinstance(obj, pd.DataFrame):
        return int(len(obj))
    if isinstance(obj, dict):
        return int(len(pd.DataFrame(obj)))
    if obj is None:
        return 0
    return int(len(obj))


def count_chunks(chunks: Iterable[Any]) -> int:
    """Cuenta filas de un iterable de chunks sin materializar la tabla completa."""
    if isinstance(chunks, pd.DataFrame) or _is_polars_frame(chunks):
        chunks = [chunks]

    total = 0
    for chunk in chunks:
        total += _frame_len(chunk)
    return total


def sample_dataframe(
    df: Any,
    max_rows: Optional[int] = None,
    mode: str = DEFAULT_SAMPLING_MODE,
    seed: int = DEFAULT_ROW_SEED,
    ratio: Optional[float] = None,
    min_rows: Optional[int] = None,
) -> Any:
    """
    Devuelve una muestra de filas de una tabla ya materializada.

    Con `ratio`, el tamaño se calcula como `ceil(len(df) * ratio)`, aplicando
    `min_rows` y usando `max_rows` como tope opcional. Sin `ratio`, `max_rows`
    conserva el comportamiento anterior de tamaño fijo.
    """
    mode = normalize_sampling_mode(mode)
    if df is None:
        return None

    total_rows = _frame_len(df)
    if ratio is not None:
        target_rows = resolve_row_sample_size(
            total_rows,
            ratio,
            min_rows=min_rows,
            max_rows=max_rows,
        )
    else:
        if max_rows is None:
            raise ValueError("Debe indicar max_rows o ratio")
        target_rows = _validate_max_rows(max_rows)

    if target_rows <= 0:
        if HAS_POLARS and isinstance(df, (pl.DataFrame, pl.LazyFrame)):
            if isinstance(df, pl.LazyFrame):
                df = df.collect()
            return df.head(0)
        if isinstance(df, dict):
            df = pd.DataFrame(df)
        if isinstance(df, pd.DataFrame):
            return df.head(0)
        return df

    if HAS_POLARS and isinstance(df, pl.LazyFrame):
        df = df.collect()

    if HAS_POLARS and isinstance(df, pl.DataFrame):
        if df.height <= target_rows:
            return df
        if mode == "head":
            return df.head(target_rows)
        return df.sample(n=target_rows, with_replacement=False, seed=int(seed))

    if isinstance(df, dict):
        df = pd.DataFrame(df)

    if isinstance(df, pd.DataFrame):
        if len(df) <= target_rows:
            return df
        if mode == "head":
            return df.head(target_rows)
        return df.sample(n=target_rows, replace=False, random_state=int(seed))

    raise TypeError(f"Tipo de tabla no soportado: {type(df)!r}")


def reservoir_sample(
    chunks: Iterable[Any],
    max_rows: int,
    seed: int = DEFAULT_ROW_SEED,
) -> pd.DataFrame:
    """
    Muestreo uniforme (reservoir) sobre un iterable de chunks.

    Cada chunk puede ser un pandas DataFrame, un polars DataFrame/LazyFrame o
    cualquier objeto convertible a DataFrame. Se conservan las `max_rows` filas
    con las claves aleatorias más pequeñas, equivalente a un reservoir sampling
    uniforme sin reemplazo. Memoria O(k); recorre la fuente una sola vez.
    """
    max_rows = _validate_max_rows(max_rows)
    rng = np.random.default_rng(int(seed))

    if isinstance(chunks, pd.DataFrame) or _is_polars_frame(chunks):
        chunks = [chunks]

    kept: Optional[pd.DataFrame] = None
    kept_keys: Optional[np.ndarray] = None

    for chunk in chunks:
        if chunk is None:
            continue
        frame = _to_pandas(chunk)
        if len(frame) == 0:
            continue

        keys = rng.random(len(frame))
        if kept is None:
            kept = frame.reset_index(drop=True)
            kept_keys = keys
        else:
            kept = pd.concat(
                [kept, frame.reset_index(drop=True)], ignore_index=True
            )
            kept_keys = np.concatenate([kept_keys, keys])

        if len(kept) > max_rows:
            idx = np.argpartition(kept_keys, max_rows - 1)[:max_rows]
            kept = kept.iloc[idx].reset_index(drop=True)
            kept_keys = kept_keys[idx]

    if kept is None:
        return pd.DataFrame()
    return kept
