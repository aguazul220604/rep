# -*- coding: utf-8 -*-
"""Dataiku PySpark recipe: prueba directa de `pii_engine` sin DataikuAdapter.

La receta lee y muestrea con Spark, resuelve los modelos con rutas explícitas
y llama directamente a `PIIAnalyzer` o `MetadataNameAnalyzer`. No usa
`DataikuAdapter` ni resuelve managed folders automáticamente.

Antes de ejecutar, configurar las rutas de los tres `model.pkl`. Se pueden
obtener desde un notebook o Python recipe con:

    import dataiku
    for name in ["model_binary_v1", "model_entity_v1", "model_content_v1"]:
        print(name, dataiku.Folder(name).get_path() + "/model.pkl")

Si el dataset es `FilesInFolder`, Spark puede no leerlo de forma distribuida;
en ese caso usar `PREFER_SPARK = False`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional

import pandas as pd

import dataiku
from dataiku import spark as dkuspark
from pyspark.sql import SparkSession

from pii_engine.core.analyzer import PIIAnalyzer
from pii_engine.core.metadata import MetadataNameAnalyzer
from pii_engine.core.name_models import NameModelPredictor
from pii_engine.core.row_sampling import (
    reservoir_sample,
    resolve_row_sample_size,
    sample_dataframe,
)
from pii_engine.core.schema import ENGINE_VERSION

INPUT_DATASET = "data_test_final"
OUTPUT_DATASET = "pii_results_direct"

MODE = "content"

BINARY_MODEL_PATH = "/ruta/a/model_binary_v1/model.pkl"
ENTITY_MODEL_PATH = "/ruta/a/model_entity_v1/model.pkl"
CONTENT_MODEL_PATH = "/ruta/a/model_content_v1/model.pkl"

ROW_RATIO = 0.02
ROW_MIN_ROWS: Optional[int] = None
ROW_MAX_ROWS: Optional[int] = 50_000
SAMPLING_MODE = "random"
SAMPLING_SEED = 42
COLUMN_SAMPLE_SIZE = 1_000
USE_EXACT_ROW_COUNT = False
PREFER_SPARK = True
CHUNK_SIZE = 50_000

NAME_COLUMN = "name"
TABLE_COLUMN = "dataset"

RESULT_COLUMNS = [
    "table",
    "column",
    "pii",
    "prob_pii",
    "entity",
    "prob_entity",
    "evidence",
    "match_rate",
    "sample_size",
    "needs_review",
    "classification",
    "engine_version",
    "model_version",
    "config_version",
    "schema_version",
]

METADATA_COLUMNS = [
    "table",
    "column",
    "pii",
    "prob_pii",
    "entity",
    "prob_entity",
    "needs_review",
    "source",
    "score_binary",
    "regex_entity",
    "entity_top1",
    "abstained",
    "classification",
    "evaluable",
    "normalized_name",
    "engine_version",
    "model_version",
]


def _validate_model_paths(include_content: bool = True) -> None:
    required = [
        ("binary", BINARY_MODEL_PATH),
        ("entity", ENTITY_MODEL_PATH),
    ]
    if include_content:
        required.append(("content", CONTENT_MODEL_PATH))

    missing = []
    for label, raw_path in required:
        if not raw_path:
            missing.append("%s=<vacío>" % label)
            continue
        path = Path(str(raw_path)).expanduser()
        if not path.is_file():
            missing.append("%s=%r" % (label, str(path)))
    if missing:
        raise FileNotFoundError(
            "Faltan archivos de modelo para la receta directa: %s"
            % "; ".join(missing)
        )


def _build_analyzer() -> PIIAnalyzer:
    _validate_model_paths(include_content=True)
    return PIIAnalyzer(
        binary_model_bundle=BINARY_MODEL_PATH,
        entity_model_bundle=ENTITY_MODEL_PATH,
        content_model_path=CONTENT_MODEL_PATH,
    )


def _build_metadata_analyzer() -> MetadataNameAnalyzer:
    _validate_model_paths(include_content=False)
    predictor = NameModelPredictor(
        binary_bundle=BINARY_MODEL_PATH,
        entity_bundle=ENTITY_MODEL_PATH,
    )
    return MetadataNameAnalyzer(predictor)


def _target_rows(total_rows: int) -> int:
    return resolve_row_sample_size(
        total_rows,
        ROW_RATIO,
        min_rows=ROW_MIN_ROWS,
        max_rows=ROW_MAX_ROWS,
    )


def _sample_spark_dataframe(spark_df: Any) -> pd.DataFrame:
    if SAMPLING_MODE == "head":
        limit = int(ROW_MAX_ROWS or 50_000)
        return spark_df.limit(limit).toPandas()

    if USE_EXACT_ROW_COUNT:
        total_rows = int(spark_df.count())
        if total_rows <= 0:
            return spark_df.limit(0).toPandas()
        target_rows = _target_rows(total_rows)
        if target_rows >= total_rows:
            return spark_df.toPandas()
        fraction = min(1.0, (float(target_rows) / float(total_rows)) * 1.10)
        sampled = spark_df.sample(
            withReplacement=False,
            fraction=fraction,
            seed=int(SAMPLING_SEED),
        )
        return sampled.limit(int(target_rows)).toPandas()

    target_rows = int(ROW_MAX_ROWS or 50_000)
    fraction = min(1.0, max(float(ROW_RATIO), 0.0))
    sampled = spark_df.sample(
        withReplacement=False,
        fraction=fraction,
        seed=int(SAMPLING_SEED),
    )
    return sampled.limit(target_rows).toPandas()


def _extract_record_count(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for key in ("recordCount", "record_count", "recordsCount", "nbRecords", "rows"):
        value = payload.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def _dataset_total_rows(dataset: Any) -> Optional[int]:
    for method_name in ("get_info", "get_metadata"):
        method = getattr(dataset, method_name, None)
        if not callable(method):
            continue
        try:
            count = _extract_record_count(method())
        except Exception:
            count = None
        if count is not None:
            return count
    return None


def _read_dataiku_sample(dataset: Any) -> pd.DataFrame:
    total_rows = _dataset_total_rows(dataset)
    if total_rows is not None and total_rows >= 0:
        target_rows = _target_rows(total_rows)
    else:
        target_rows = int(ROW_MAX_ROWS or 50_000)

    if target_rows <= 0:
        return pd.DataFrame()

    chunks = None
    iterator = getattr(dataset, "iter_dataframes", None)
    if callable(iterator):
        try:
            chunks = iterator(chunksize=int(CHUNK_SIZE))
        except TypeError:
            try:
                chunks = iterator()
            except TypeError:
                chunks = None

    if chunks is not None:
        return reservoir_sample(chunks, target_rows, seed=int(SAMPLING_SEED))

    frame = dataset.get_dataframe()
    return sample_dataframe(
        frame,
        max_rows=target_rows,
        mode="random",
        seed=int(SAMPLING_SEED),
    )


def _results_to_dataframe(results: List[Any]) -> pd.DataFrame:
    rows = [result.to_dict() for result in results]
    if not rows:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    return pd.DataFrame(rows)


def _metadata_to_dataframe(frame: pd.DataFrame, analyzer: Any) -> pd.DataFrame:
    if frame is None or frame.empty or NAME_COLUMN not in frame.columns:
        return pd.DataFrame(columns=METADATA_COLUMNS)

    if TABLE_COLUMN and TABLE_COLUMN in frame.columns:
        source = frame[[TABLE_COLUMN, NAME_COLUMN]].dropna().drop_duplicates()
    else:
        source = frame[[NAME_COLUMN]].dropna().drop_duplicates()
        source[TABLE_COLUMN or "table"] = INPUT_DATASET

    names = [str(value).strip() for value in source[NAME_COLUMN].tolist()]
    predictions = analyzer.predict(names)
    rows = []
    for _, record in source.iterrows():
        name = str(record[NAME_COLUMN]).strip()
        prediction = predictions.get(name)
        if prediction is None:
            continue
        item = prediction.to_dict()
        if TABLE_COLUMN and TABLE_COLUMN in source.columns:
            item["table"] = str(record[TABLE_COLUMN])
        else:
            item["table"] = INPUT_DATASET
        item["column"] = item.pop("element")
        item["pii"] = item.pop("is_pii")
        item["engine_version"] = ENGINE_VERSION
        item["model_version"] = analyzer.model_version_tag
        rows.append(item)

    if not rows:
        return pd.DataFrame(columns=METADATA_COLUMNS)
    return pd.DataFrame(rows)


def main() -> None:
    mode = str(MODE).strip().lower()
    if mode not in {"content", "metadata"}:
        raise ValueError("MODE debe ser 'content' o 'metadata'")
    if str(SAMPLING_MODE).strip().lower() not in {"random", "head"}:
        raise ValueError("SAMPLING_MODE debe ser 'random' o 'head'")
    if not 0 < float(ROW_RATIO) <= 1:
        raise ValueError("ROW_RATIO debe estar en (0, 1]")

    input_dataset = dataiku.Dataset(INPUT_DATASET)

    if mode == "metadata":
        analyzer = _build_metadata_analyzer()
        output_df = _metadata_to_dataframe(input_dataset.get_dataframe(), analyzer)
    else:
        sample_df = None
        if PREFER_SPARK:
            try:
                spark = SparkSession.builder.getOrCreate()
                spark_df = dkuspark.get_dataframe(spark, input_dataset)
                sample_df = _sample_spark_dataframe(spark_df)
            except Exception as exc:
                print(
                    "Spark no disponible para este dataset (%s: %s)."
                    % (type(exc).__name__, exc),
                    file=sys.stderr,
                )

        if sample_df is None or sample_df.empty:
            sample_df = _read_dataiku_sample(input_dataset)

        analyzer = _build_analyzer()
        results = analyzer.analyze(
            data=sample_df,
            table_meta=INPUT_DATASET,
            sample_size=COLUMN_SAMPLE_SIZE,
            seed=SAMPLING_SEED,
        )
        output_df = _results_to_dataframe(results)

    dataiku.Dataset(OUTPUT_DATASET).write_with_schema(output_df)
    print(
        "pii_engine direct pyspark recipe mode=%s rows=%d input=%s output=%s"
        % (mode, len(output_df), INPUT_DATASET, OUTPUT_DATASET)
    )


main()
