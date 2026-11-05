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


def sample_dataframe(
    df: Any,
    max_rows: int,
    mode: str = DEFAULT_SAMPLING_MODE,
    seed: int = DEFAULT_ROW_SEED,
) -> Any:
    """
    Devuelve hasta `max_rows` filas de una tabla ya materializada.

    `mode="random"` (default) toma una muestra aleatoria sin reemplazo con
    semilla reproducible; `mode="head"` conserva el comportamiento anterior
    (primeras N filas) para compatibilidad.
    """
    max_rows = _validate_max_rows(max_rows)
    mode = normalize_sampling_mode(mode)
    if df is None:
        return None

    if HAS_POLARS and isinstance(df, pl.LazyFrame):
        df = df.collect()

    if HAS_POLARS and isinstance(df, pl.DataFrame):
        if df.height <= max_rows:
            return df
        if mode == "head":
            return df.head(max_rows)
        return df.sample(n=max_rows, with_replacement=False, seed=int(seed))

    if isinstance(df, dict):
        df = pd.DataFrame(df)

    if isinstance(df, pd.DataFrame):
        if len(df) <= max_rows:
            return df
        if mode == "head":
            return df.head(max_rows)
        return df.sample(n=max_rows, replace=False, random_state=int(seed))

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
