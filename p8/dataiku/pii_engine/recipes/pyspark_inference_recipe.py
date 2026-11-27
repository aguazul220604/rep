# -*- coding: utf-8 -*-
"""Dataiku PySpark recipe: infiere PII por columna usando `pii_engine`.

Pegar este script en una receta PySpark de Dataiku. Requiere `pii_engine` en
Project Libraries y los managed folders `model_binary_v1`, `model_entity_v1`
y `model_content_v1`. Spark solo se usa para leer y muestrear; el análisis se
ejecuta en el driver sobre una muestra acotada.
"""
from __future__ import annotations

from typing import Any, List, Optional

import pandas as pd

import dataiku
from dataiku import spark as dkuspark
from pyspark.sql import SparkSession

from pii_engine.adapters.dataiku_adapter import DataikuAdapter
from pii_engine.core.row_sampling import resolve_row_sample_size
from pii_engine.core.schema import ENGINE_VERSION

INPUT_DATASET = "data_test_final"
OUTPUT_DATASET = "pii_results"

MODE = "content"

BINARY_FOLDER = "model_binary_v1"
ENTITY_FOLDER = "model_entity_v1"
CONTENT_FOLDER = "model_content_v1"
MODEL_FILENAME = "model.pkl"
STRICT_MODELS = True

ROW_RATIO = 0.02
ROW_MIN_ROWS: Optional[int] = None
ROW_MAX_ROWS: Optional[int] = 50_000
SAMPLING_MODE = "random"
SAMPLING_SEED = 42
COLUMN_SAMPLE_SIZE = 1_000
USE_EXACT_ROW_COUNT = True

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


def _build_adapter() -> DataikuAdapter:
    return DataikuAdapter(
        binary_folder=BINARY_FOLDER,
        entity_folder=ENTITY_FOLDER,
        content_folder=CONTENT_FOLDER,
        model_filename=MODEL_FILENAME,
        sampling_mode=SAMPLING_MODE,
        sampling_seed=SAMPLING_SEED,
        row_ratio=ROW_RATIO,
        row_min_rows=ROW_MIN_ROWS,
        row_max_rows=ROW_MAX_ROWS,
        strict_models=STRICT_MODELS,
    )


def _results_to_dataframe(results: List[Any]) -> pd.DataFrame:
    rows = [result.to_dict() for result in results]
    if not rows:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    return pd.DataFrame(rows)


def _metadata_to_dataframe(spark_df: Any, analyzer: Any) -> pd.DataFrame:
    selected = [NAME_COLUMN]
    if TABLE_COLUMN and TABLE_COLUMN in spark_df.columns:
        selected.insert(0, TABLE_COLUMN)

    limit = int(ROW_MAX_ROWS or 50_000)
    frame = (
        spark_df.select(*selected)
        .dropna()
        .distinct()
        .limit(limit)
        .toPandas()
    )
    if frame.empty:
        return pd.DataFrame(columns=METADATA_COLUMNS)

    names = [str(value).strip() for value in frame[NAME_COLUMN].tolist()]
    predictions = analyzer.predict(names)
    rows = []
    for _, record in frame.iterrows():
        name = str(record[NAME_COLUMN]).strip()
        prediction = predictions.get(name)
        if prediction is None:
            continue
        item = prediction.to_dict()
        if TABLE_COLUMN and TABLE_COLUMN in frame.columns:
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

    spark = SparkSession.builder.getOrCreate()
    input_dataset = dataiku.Dataset(INPUT_DATASET)
    spark_df = dkuspark.get_dataframe(spark, input_dataset)
    adapter = _build_adapter()

    if mode == "metadata":
        analyzer = adapter.build_metadata_analyzer()
        output_df = _metadata_to_dataframe(spark_df, analyzer)
    else:
        sample_df = _sample_spark_dataframe(spark_df)
        results = adapter.analyze_dataframe(
            sample_df,
            table_name=INPUT_DATASET,
            sample_size=COLUMN_SAMPLE_SIZE,
            seed=SAMPLING_SEED,
            apply_row_sampling=False,
        )
        output_df = _results_to_dataframe(results)

    dataiku.Dataset(OUTPUT_DATASET).write_with_schema(output_df)
    print(
        "pii_engine pyspark recipe mode=%s rows=%d input=%s output=%s"
        % (mode, len(output_df), INPUT_DATASET, OUTPUT_DATASET)
    )


main()
