# -*- coding: utf-8 -*-
"""
Adaptador de despliegue para Dataiku DSS.

Resuelve los managed folders de modelos (`model_binary_v1`, `model_entity_v1`,
`model_content_v1`), cada uno con su `model.pkl`. Construye un `PIIAnalyzer` y
ofrece operaciones de alto nivel para la webapp:

  - `analyze_dataset(...)`: lee una muestra aleatoria (reservoir) de un dataset
    del flow, sin depender del muestreo nativo de DSS.
  - `analyze_dataframe(...)`: analiza un DataFrame ya cargado.
  - `analyze_file(...)`: analiza CSV/parquet/Excel subido por el usuario.
  - `health()`: estado del motor y versi��n de modelos.

El adaptador no registra ni devuelve valores de las muestras. El managed
folder puede exponer una ruta local (`get_path`) o requerir descarga
(`get_download_stream`); en este ǧltimo caso el artefacto se copia a un
directorio temporal.
"""
from __future__ import annotations

import atexit
import hashlib
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, Union

from pii_engine.adapters.local_adapter import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_READ_ROWS,
    LocalAdapter,
)
from pii_engine.core.analyzer import PIIAnalyzer
from pii_engine.core.row_sampling import (
    DEFAULT_ROW_SEED,
    DEFAULT_SAMPLING_MODE,
    reservoir_sample,
    sample_dataframe,
)
from pii_engine.core.schema import ENGINE_VERSION, ColumnResult

DEFAULT_BINARY_FOLDER = "model_binary_v1"
DEFAULT_ENTITY_FOLDER = "model_entity_v1"
DEFAULT_CONTENT_FOLDER = "model_content_v1"
DEFAULT_MODEL_FILENAME = "model.pkl"


class ModelArtifactError(RuntimeError):
    """El bundle de un modelo no está disponible o no se pudo descargar."""


class DataikuAdapter:
    """
    Fachada entre Dataiku y el motor PII.

    `analyzer_factory` permite inyectar una fábrica distinta en pruebas; debe
    aceptar los argumentos `binary_model_bundle`, `entity_model_bundle` y
    `content_model_path`.
    """

    def __init__(
        self,
        binary_folder: str = DEFAULT_BINARY_FOLDER,
        entity_folder: str = DEFAULT_ENTITY_FOLDER,
        content_folder: str = DEFAULT_CONTENT_FOLDER,
        model_filename: str = DEFAULT_MODEL_FILENAME,
        max_read_rows: int = DEFAULT_MAX_READ_ROWS,
        sampling_mode: str = DEFAULT_SAMPLING_MODE,
        sampling_seed: int = DEFAULT_ROW_SEED,
        cache_dir: Optional[Union[str, Path]] = None,
        analyzer_factory: Optional[Any] = None,
        strict_models: bool = True,
    ):
        if max_read_rows <= 0:
            raise ValueError("max_read_rows debe ser mayor que cero")
        self.binary_folder = str(binary_folder)
        self.entity_folder = str(entity_folder)
        self.content_folder = str(content_folder)
        self.model_filename = str(model_filename)
        self.max_read_rows = int(max_read_rows)
        self.strict_models = bool(strict_models)
        self.analyzer_factory = analyzer_factory or PIIAnalyzer

        if cache_dir is None:
            self._cache_dir = Path(tempfile.mkdtemp(prefix="pii_engine_models_"))
            atexit.register(shutil.rmtree, str(self._cache_dir), ignore_errors=True)
        else:
            self._cache_dir = Path(cache_dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._analyzer: Optional[Any] = None
        self._resolved: Dict[str, Path] = {}
        self._local_adapter = LocalAdapter(
            max_read_rows=self.max_read_rows,
            sampling_mode=sampling_mode,
            sampling_seed=sampling_seed,
            chunk_size=DEFAULT_CHUNK_SIZE,
            analyzer_getter=self.build_analyzer,
        )

    # ------------------------------------------------------------------
    # Managed folders
    # ------------------------------------------------------------------
    @staticmethod
    def _import_dataiku():
        try:
            import dataiku  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "El paquete dataiku no está disponible; ejecute el adaptador dentro de DSS"
            ) from exc
        return dataiku

    def _cache_key(self, folder_name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", folder_name).strip("._") or "folder"
        digest = hashlib.sha1(folder_name.encode("utf-8")).hexdigest()[:8]
        return f"{safe}_{digest}"

    def _download_model(self, folder_name: str, folder: Any) -> Path:
        dest_dir = self._cache_dir / self._cache_key(folder_name)
        dest = dest_dir / self.model_filename
        with self._lock:
            if dest.exists():
                return dest
            dest_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")

            stream = None
            last_error: Optional[Exception] = None
            for remote_path in (f"/{self.model_filename}", self.model_filename):
                try:
                    stream = folder.get_download_stream(remote_path)
                    break
                except Exception as exc:  # API varía entre versiones de DSS
                    last_error = exc

            if stream is None:
                raise ModelArtifactError(
                    f"No se pudo descargar {self.model_filename} del folder {folder_name!r}"
                ) from last_error

            try:
                with stream as src:
                    with open(tmp, "wb") as out:
                        shutil.copyfileobj(src, out)
                tmp.replace(dest)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

            return dest

    def resolve_model_path(self, folder_name: str) -> Path:
        """
        Devuelve una ruta local al `model.pkl` del managed folder indicado.
        Acepta también una ruta local directa (útil para pruebas y desarrollo).
        """
        key = str(folder_name)
        with self._lock:
            cached = self._resolved.get(key)
            if cached is not None and cached.exists():
                return cached

        candidate = Path(key).expanduser()
        if candidate.is_file():
            resolved = candidate
        elif candidate.is_dir():
            model_file = candidate / self.model_filename
            if not model_file.exists() and self.model_filename == "model.pkl":
                legacy = candidate / "model.joblib"
                if legacy.exists():
                    model_file = legacy
            if not model_file.exists():
                raise ModelArtifactError(
                    f"El directorio {key!r} no contiene {self.model_filename!r}"
                )
            resolved = model_file
        else:
            dataiku = self._import_dataiku()
            try:
                folder = dataiku.Folder(key)
            except Exception as exc:
                raise ModelArtifactError(f"No se pudo abrir el managed folder {key!r}") from exc

            resolved = None
            get_path = getattr(folder, "get_path", None)
            if callable(get_path):
                try:
                    root = Path(get_path())
                except Exception:
                    root = None
                if root is not None:
                    if root.is_file():
                        resolved = root
                    elif (root / self.model_filename).exists():
                        resolved = root / self.model_filename
                    elif self.model_filename == "model.pkl" and (root / "model.joblib").exists():
                        resolved = root / "model.joblib"

            if resolved is None:
                resolved = self._download_model(key, folder)

        with self._lock:
            self._resolved[key] = resolved
        return resolved

    # ------------------------------------------------------------------
    # Motor
    # ------------------------------------------------------------------
    def build_analyzer(self, refresh: bool = False) -> Any:
        """Construye (y cachea) el `PIIAnalyzer` con los tres artefactos resueltos."""
        with self._lock:
            if self._analyzer is not None and not refresh:
                return self._analyzer

            paths: Dict[str, Optional[Path]] = {}
            errors: List[str] = []
            for name, folder in (
                ("binary", self.binary_folder),
                ("entity", self.entity_folder),
                ("content", self.content_folder),
            ):
                try:
                    paths[name] = self.resolve_model_path(folder)
                except ModelArtifactError as exc:
                    paths[name] = None
                    errors.append(str(exc))

            if self.strict_models and errors:
                raise ModelArtifactError("; ".join(errors))

            analyzer = self.analyzer_factory(
                binary_model_bundle=paths.get("binary"),
                entity_model_bundle=paths.get("entity"),
                content_model_path=paths.get("content"),
            )
            self._analyzer = analyzer
            return analyzer

    def reset(self) -> None:
        """Invalida el analizador y las rutas resueltas (útil en pruebas o redeploy)."""
        with self._lock:
            self._analyzer = None
            self._resolved.clear()
            self._local_adapter.reset()

    # ------------------------------------------------------------------
    # Operaciones
    # ------------------------------------------------------------------
    def analyze_dataframe(
        self,
        df: Any,
        table_name: str = "dataframe",
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> List[ColumnResult]:
        if df is None:
            raise ValueError("El DataFrame de entrada es obligatorio")
        return self._local_adapter.analyze_dataframe(
            df,
            table_name=table_name,
            columns=columns,
            sample_size=sample_size,
            seed=seed,
        )

    def _read_dataset_sample(self, dataset: Any, cols: Optional[List[str]]) -> Any:
        """Muestrea filas del dataset sin depender del muestreo nativo de DSS."""
        if self._local_adapter.sampling_mode == "head":
            try:
                return dataset.get_dataframe(
                    columns=cols,
                    limit=self.max_read_rows,
                )
            except TypeError:
                df = dataset.get_dataframe()
                if cols:
                    df = df[[c for c in cols if c in df.columns]]
                return df.head(self.max_read_rows)

        iterator = getattr(dataset, "iter_dataframes", None)
        if callable(iterator):
            try:
                chunks = iterator(
                    columns=cols,
                    chunksize=DEFAULT_CHUNK_SIZE,
                )
                return reservoir_sample(
                    chunks,
                    self.max_read_rows,
                    seed=self._local_adapter.sampling_seed,
                )
            except TypeError:
                # Versiones de DSS con otra firma: iterar con defaults.
                try:
                    chunks = iterator()
                    return reservoir_sample(
                        chunks,
                        self.max_read_rows,
                        seed=self._local_adapter.sampling_seed,
                    )
                except TypeError:
                    pass

        try:
            df = dataset.get_dataframe(columns=cols)
        except TypeError:
            df = dataset.get_dataframe()
            if cols:
                df = df[[c for c in cols if c in df.columns]]
        return sample_dataframe(
            df,
            self.max_read_rows,
            mode="random",
            seed=self._local_adapter.sampling_seed,
        )

    def analyze_dataset(
        self,
        dataset_name: str,
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> List[ColumnResult]:
        if not dataset_name or not str(dataset_name).strip():
            raise ValueError("El nombre del dataset es obligatorio")

        dataiku = self._import_dataiku()
        dataset = dataiku.Dataset(str(dataset_name))
        cols = list(columns) if columns is not None else None

        df = self._read_dataset_sample(dataset, cols)

        return self.analyze_dataframe(
            df,
            table_name=str(dataset_name),
            columns=cols,
            sample_size=sample_size,
            seed=seed,
        )

    def analyze_file(
        self,
        file_obj: Union[str, Path, BinaryIO, Any],
        filename: str,
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
        sheet: Optional[Union[str, int]] = None,
    ) -> List[ColumnResult]:
        """
        Analiza un CSV, parquet o Excel. `file_obj` puede ser una ruta o un
        stream (por ejemplo `request.files["file"].stream`).
        """
        return self._local_adapter.analyze_file(
            file_obj,
            filename,
            columns=columns,
            sample_size=sample_size,
            seed=seed,
            sheet=sheet,
        )

    def health(self) -> Dict[str, Any]:
        """Estado del motor sin exponer valores de datos."""
        info: Dict[str, Any] = {
            "status": "ok",
            "engine_version": ENGINE_VERSION,
            "engine_ready": False,
            "folders": {
                "binary": self.binary_folder,
                "entity": self.entity_folder,
                "content": self.content_folder,
            },
            "sampling": {
                "mode": self._local_adapter.sampling_mode,
                "seed": self._local_adapter.sampling_seed,
                "max_read_rows": self.max_read_rows,
            },
        }
        try:
            analyzer = self.build_analyzer()
            info["engine_ready"] = True
            version_getter = getattr(analyzer, "_model_version", None)
            info["model_version"] = version_getter() if callable(version_getter) else "unknown"
        except Exception as exc:
            info["status"] = "degraded"
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info
