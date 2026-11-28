# -*- coding: utf-8 -*-
"""Dataiku Python recipe: infiere PII por columna sobre una muestra.

Lee el dataset intermedio generado por `pyspark_sample_recipe.py`, resuelve los
modelos desde managed folders con `DataikuAdapter` y escribe los resultados.
Debe ejecutarse en un code env compatible con los `model.pkl` (scikit-learn
1.6.1). No usa Spark.
"""
from __future__ import annotations

import os
import sys
import traceback
from typing import Any, List

import pandas as pd

import dataiku

from pii_engine.adapters.dataiku_adapter import DataikuAdapter

INPUT_SAMPLE_DATASET = "pii_sample_data_test_final_2_copy"
ORIGINAL_DATASET = "data_test_final_2_copy"
OUTPUT_DATASET = "pii_results_test"

INPUT_SAMPLE_DATASET = os.environ.get(
    "DKU_INPUT_0_DATASET_ID", INPUT_SAMPLE_DATASET
)
OUTPUT_DATASET = os.environ.get("DKU_OUTPUT_0_DATASET_ID", OUTPUT_DATASET)

MODEL_PROJECT_KEY = ""
BINARY_FOLDER = "model_binary_v1"
ENTITY_FOLDER = "model_entity_v1"
CONTENT_FOLDER = "model_content_v1"
MODEL_FILENAME = "model.pkl"
STRICT_MODELS = True

COLUMN_SAMPLE_SIZE = 500
SAMPLING_SEED = 42

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


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _folder_ref(folder_name: str) -> str:
    if MODEL_PROJECT_KEY:
        return "%s.%s" % (MODEL_PROJECT_KEY, folder_name)
    return folder_name


def _build_adapter() -> DataikuAdapter:
    return DataikuAdapter(
        binary_folder=_folder_ref(BINARY_FOLDER),
        entity_folder=_folder_ref(ENTITY_FOLDER),
        content_folder=_folder_ref(CONTENT_FOLDER),
        model_filename=MODEL_FILENAME,
        strict_models=STRICT_MODELS,
    )


def _results_to_dataframe(results: List[Any]) -> pd.DataFrame:
    rows = [result.to_dict() for result in results]
    if not rows:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    return pd.DataFrame(rows).reindex(columns=RESULT_COLUMNS)


def main() -> None:
    _log("pii_engine python inference recipe: inicio")
    _log("python: %s" % sys.version.replace("\n", " "))

    import sklearn

    _log("sklearn: %s" % sklearn.__version__)

    sample_df = dataiku.Dataset(INPUT_SAMPLE_DATASET).get_dataframe()
    _log("sample: %s" % (sample_df.shape,))

    if sample_df.empty:
        _log("muestra vacia; se escribira salida vacia")
        output_df = pd.DataFrame(columns=RESULT_COLUMNS)
    else:
        adapter = _build_adapter()
        results = adapter.analyze_dataframe(
            sample_df,
            table_name=ORIGINAL_DATASET,
            sample_size=COLUMN_SAMPLE_SIZE,
            seed=SAMPLING_SEED,
            apply_row_sampling=False,
        )
        _log("results: %d" % len(results))
        output_df = _results_to_dataframe(results)

    dataiku.Dataset(OUTPUT_DATASET).write_with_schema(output_df)
    _log("written: %d" % len(output_df))


try:
    main()
except Exception:
    print(traceback.format_exc(), file=sys.stderr, flush=True)
    raise
