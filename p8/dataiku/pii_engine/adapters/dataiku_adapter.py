# -*- coding: utf-8 -*-
"""
Adaptadores de despliegue del motor PII (local y Dataiku DSS).

Este módulo único concentra los contratos compartidos, los lectores de
CSV/parquet/Excel y datasets, los proveedores de artefactos de modelos y las
fachadas `PIIAdapter`, `LocalAdapter` y `DataikuAdapter`.
"""
from __future__ import annotations

import atexit
import hashlib
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Protocol,
    Union,
)

import pandas as pd

from pii_engine.core.analyzer import PIIAnalyzer
from pii_engine.core.row_sampling import (
    DEFAULT_ROW_SEED,
    DEFAULT_SAMPLING_MODE,
    count_chunks,
    normalize_sampling_mode,
    resolve_row_sample_size,
    reservoir_sample,
    sample_dataframe,
)
from pii_engine.core.schema import ENGINE_VERSION, ColumnResult

DEFAULT_MAX_READ_ROWS = 50_000
DEFAULT_CHUNK_SIZE = 50_000

DEFAULT_BINARY_FOLDER = "model_binary_v1"
DEFAULT_ENTITY_FOLDER = "model_entity_v1"
DEFAULT_CONTENT_FOLDER = "model_content_v1"
DEFAULT_MODEL_FILENAME = "model.pkl"

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"


def _load_sampling_defaults() -> dict:
    try:
        import yaml

        payload = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return payload.get("sampling", {}) or {}
    except Exception:
        return {}


_SAMPLING_DEFAULTS = _load_sampling_defaults()
DEFAULT_ROW_RATIO = float(_SAMPLING_DEFAULTS.get("row_ratio") or 0.02)
DEFAULT_ROW_MIN_ROWS = _SAMPLING_DEFAULTS.get("row_min_rows")
DEFAULT_ROW_MAX_ROWS = _SAMPLING_DEFAULTS.get("row_max_rows")

CSV_SUFFIXES = {".csv"}
PARQUET_SUFFIXES = {".parquet", ".pq"}
EXCEL_SUFFIXES = {".xls", ".xlsx", ".xlsm"}
SUPPORTED_FILE_SUFFIXES = CSV_SUFFIXES | PARQUET_SUFFIXES | EXCEL_SUFFIXES

_Source = Union[str, Path, BinaryIO, Any]


class ModelArtifactError(RuntimeError):
    """El bundle de un modelo no está disponible o no se pudo descargar."""


class NotSupportedError(RuntimeError):
    """La fuente o el proveedor no soporta la operación solicitada."""


@dataclass(frozen=True)
class ModelArtifacts:
    binary_bundle: Optional[Union[str, Any]] = None
    entity_bundle: Optional[Union[str, Any]] = None
    content_model_path: Optional[Union[str, Any]] = None


@dataclass
class SamplingConfig:
    row_ratio: Optional[float] = None
    row_min_rows: Optional[int] = None
    row_max_rows: Optional[int] = 50_000
    column_sample_size: Optional[int] = 2_000
    seed: int = 42
    mode: str = "random"
    chunk_size: int = 50_000


class ModelProvider(Protocol):
    def resolve_artifacts(self) -> ModelArtifacts:
        ...

    def resolve_model_path(self, folder_name: str) -> Any:
        ...

    def reset(self) -> None:
        ...


class SourceReader(Protocol):
    def load_dataframe(
        self,
        df: Any,
        columns: Optional[List[str]] = None,
    ) -> Any:
        ...

    def load_file(
        self,
        file_obj: Any,
        filename: str,
        columns: Optional[List[str]] = None,
        sheet: Optional[Union[str, int]] = None,
    ) -> Any:
        ...

    def load_dataset(
        self,
        dataset_name: str,
        columns: Optional[List[str]] = None,
    ) -> Any:
        ...

    def health_info(self) -> Dict[str, Any]:
        ...

    def reset(self) -> None:
        ...


def _validate_max_rows(max_rows: int) -> int:
    value = int(max_rows)
    if value <= 0:
        raise ValueError("max_read_rows debe ser mayor que cero")
    return value


def _rewind(source: _Source) -> bool:
    if isinstance(source, (str, Path)):
        return True
    seek = getattr(source, "seek", None)
    if not callable(seek):
        return False
    try:
        seek(0)
        return True
    except Exception:
        return False


def _count_csv_rows(
    source: _Source,
    chunksize: int,
    options: dict,
) -> int:
    chunks = pd.read_csv(source, chunksize=int(chunksize), **options)
    return count_chunks(chunks)


def read_csv_sample(
    source: _Source,
    max_rows: Optional[int] = None,
    usecols: Optional[List[str]] = None,
    dtype: Optional[Any] = str,
    seed: int = DEFAULT_ROW_SEED,
    mode: str = DEFAULT_SAMPLING_MODE,
    chunksize: int = DEFAULT_CHUNK_SIZE,
    ratio: Optional[float] = None,
    min_rows: Optional[int] = None,
    **read_kwargs: Any,
) -> pd.DataFrame:
    """
    Lee un CSV conservando una muestra de filas.

    Con `ratio`, cuenta las filas en una primera pasada y muestrea exactamente
    `ceil(total * ratio)` en la segunda. Sin `ratio`, `max_rows` es el tamaño
    fijo legacy. `max_rows` actúa como tope opcional cuando hay `ratio`.
    """
    mode = normalize_sampling_mode(mode)
    options = dict(usecols=usecols, dtype=dtype, **read_kwargs)

    if ratio is None:
        if max_rows is None:
            max_rows = DEFAULT_MAX_READ_ROWS
        max_rows = _validate_max_rows(max_rows)
        if mode == "head":
            return pd.read_csv(source, nrows=max_rows, low_memory=False, **options)
        chunks = pd.read_csv(source, chunksize=int(chunksize), **options)
        return reservoir_sample(chunks, max_rows, seed=seed)

    total_rows = _count_csv_rows(source, chunksize, options)
    target_rows = resolve_row_sample_size(
        total_rows, ratio, min_rows=min_rows, max_rows=max_rows
    )
    if target_rows <= 0:
        return pd.DataFrame(columns=usecols)
    if not _rewind(source):
        raise ValueError(
            "La fuente CSV no permite volver a leerla para aplicar el muestreo por ratio"
        )
    if mode == "head":
        return pd.read_csv(
            source, nrows=target_rows, low_memory=False, **options
        )
    chunks = pd.read_csv(source, chunksize=int(chunksize), **options)
    return reservoir_sample(chunks, target_rows, seed=seed)


def _iter_parquet_frames(
    source: _Source,
    columns: Optional[List[str]],
    batch_size: int,
) -> Iterator[pd.DataFrame]:
    """Itera un parquet por batches con pyarrow; fallback a lectura completa."""
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        yield pd.read_parquet(source, columns=columns)
        return

    try:
        parquet_file = pq.ParquetFile(source)
    except Exception:
        if hasattr(source, "seek"):
            try:
                source.seek(0)
            except Exception:
                pass
        yield pd.read_parquet(source, columns=columns)
        return

    for batch in parquet_file.iter_batches(
        batch_size=int(batch_size), columns=columns
    ):
        yield batch.to_pandas()


def _parquet_num_rows(source: _Source) -> Optional[int]:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        return None
    try:
        parquet_file = pq.ParquetFile(source)
    except Exception:
        return None
    metadata = getattr(parquet_file, "metadata", None)
    num_rows = getattr(metadata, "num_rows", None)
    if num_rows is None:
        return None
    return int(num_rows)


def read_parquet_sample(
    source: _Source,
    max_rows: Optional[int] = None,
    columns: Optional[List[str]] = None,
    seed: int = DEFAULT_ROW_SEED,
    mode: str = DEFAULT_SAMPLING_MODE,
    chunksize: int = DEFAULT_CHUNK_SIZE,
    ratio: Optional[float] = None,
    min_rows: Optional[int] = None,
) -> pd.DataFrame:
    """Lee un parquet con muestreo aleatorio uniforme de filas (o head legacy)."""
    mode = normalize_sampling_mode(mode)

    if ratio is None:
        if max_rows is None:
            max_rows = DEFAULT_MAX_READ_ROWS
        max_rows = _validate_max_rows(max_rows)
        target_rows = max_rows
    else:
        total_rows = _parquet_num_rows(source)
        if total_rows is None:
            frames = _iter_parquet_frames(source, columns, int(chunksize))
            total_rows = count_chunks(frames)
            if not _rewind(source):
                raise ValueError(
                    "La fuente parquet no permite volver a leerla para aplicar el muestreo por ratio"
                )
        target_rows = resolve_row_sample_size(
            total_rows, ratio, min_rows=min_rows, max_rows=max_rows
        )
        if target_rows <= 0:
            return pd.DataFrame(columns=columns)
        if not _rewind(source):
            raise ValueError(
                "La fuente parquet no permite volver a leerla para aplicar el muestreo por ratio"
            )

    frames = _iter_parquet_frames(source, columns, int(chunksize))
    if mode == "random":
        return reservoir_sample(frames, target_rows, seed=seed)

    parts: List[pd.DataFrame] = []
    remaining = target_rows
    for frame in frames:
        if remaining <= 0:
            break
        take = frame.head(remaining)
        if len(take):
            parts.append(take)
            remaining -= len(take)
    if not parts:
        return pd.DataFrame(columns=columns)
    return pd.concat(parts, ignore_index=True)


def read_excel_sample(
    source: _Source,
    max_rows: Optional[int] = None,
    sheet: Optional[Union[str, int]] = None,
    seed: int = DEFAULT_ROW_SEED,
    mode: str = DEFAULT_SAMPLING_MODE,
    ratio: Optional[float] = None,
    min_rows: Optional[int] = None,
) -> pd.DataFrame:
    """
    Lee una hoja de Excel y aplica muestreo de filas.

    pandas no tiene lector Excel por chunks: la hoja se materializa y se
    muestrea con `sample_dataframe`.
    """
    mode = normalize_sampling_mode(mode)
    sheet_name: Any = 0 if sheet is None else sheet

    if ratio is None:
        if max_rows is None:
            max_rows = DEFAULT_MAX_READ_ROWS
        max_rows = _validate_max_rows(max_rows)
        if mode == "head":
            return pd.read_excel(source, sheet_name=sheet_name, nrows=max_rows)
        df = pd.read_excel(source, sheet_name=sheet_name)
        return sample_dataframe(df, max_rows, mode="random", seed=seed)

    df = pd.read_excel(source, sheet_name=sheet_name)
    return sample_dataframe(
        df,
        max_rows=max_rows,
        mode=mode,
        seed=seed,
        ratio=ratio,
        min_rows=min_rows,
    )


class LocalSourceReader:
    """Lector local de DataFrames y archivos CSV/parquet/Excel."""

    def __init__(
        self,
        max_read_rows: Optional[int] = None,
        sampling_mode: str = DEFAULT_SAMPLING_MODE,
        sampling_seed: int = DEFAULT_ROW_SEED,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        row_ratio: Optional[float] = DEFAULT_ROW_RATIO,
        row_min_rows: Optional[int] = DEFAULT_ROW_MIN_ROWS,
        row_max_rows: Optional[int] = DEFAULT_ROW_MAX_ROWS,
    ):
        self.sampling_mode = normalize_sampling_mode(sampling_mode)
        self.sampling_seed = int(sampling_seed)
        self.chunk_size = max(1, int(chunk_size))
        self.row_ratio = None if row_ratio is None else float(row_ratio)
        self.row_min_rows = None if row_min_rows is None else int(row_min_rows)
        if row_max_rows is None and max_read_rows is not None:
            row_max_rows = max_read_rows
        self.row_max_rows = None if row_max_rows is None else int(row_max_rows)
        if self.row_ratio is None and self.row_max_rows is None:
            self.row_max_rows = DEFAULT_MAX_READ_ROWS
        self.max_read_rows = self.row_max_rows

    def load_dataframe(
        self,
        df: Any,
        columns: Optional[List[str]] = None,
    ) -> Any:
        if df is None:
            raise ValueError("El DataFrame de entrada es obligatorio")
        return sample_dataframe(
            df,
            max_rows=self.row_max_rows,
            mode=self.sampling_mode,
            seed=self.sampling_seed,
            ratio=self.row_ratio,
            min_rows=self.row_min_rows,
        )

    def load_file(
        self,
        file_obj: _Source,
        filename: str,
        columns: Optional[List[str]] = None,
        sheet: Optional[Union[str, int]] = None,
    ) -> pd.DataFrame:
        name = str(filename or "")
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_FILE_SUFFIXES:
            raise ValueError("Formato no soportado: use CSV, parquet o Excel")

        cols = list(columns) if columns is not None else None
        close_stream = False
        if isinstance(file_obj, (str, Path)):
            stream: Any = open(file_obj, "rb")
            close_stream = True
        else:
            stream = file_obj

        try:
            if suffix in CSV_SUFFIXES:
                return read_csv_sample(
                    stream,
                    self.row_max_rows,
                    usecols=cols,
                    dtype=str,
                    seed=self.sampling_seed,
                    mode=self.sampling_mode,
                    chunksize=self.chunk_size,
                    ratio=self.row_ratio,
                    min_rows=self.row_min_rows,
                )
            if suffix in PARQUET_SUFFIXES:
                return read_parquet_sample(
                    stream,
                    self.row_max_rows,
                    columns=cols,
                    seed=self.sampling_seed,
                    mode=self.sampling_mode,
                    chunksize=self.chunk_size,
                    ratio=self.row_ratio,
                    min_rows=self.row_min_rows,
                )
            return read_excel_sample(
                stream,
                self.row_max_rows,
                sheet=sheet,
                seed=self.sampling_seed,
                mode=self.sampling_mode,
                ratio=self.row_ratio,
                min_rows=self.row_min_rows,
            )
        finally:
            if close_stream:
                stream.close()

    def load_dataset(
        self,
        dataset_name: str,
        columns: Optional[List[str]] = None,
    ) -> Any:
        raise NotSupportedError(
            "LocalSourceReader no lee datasets de Dataiku; use DataikuSourceReader"
        )

    def health_info(self) -> dict:
        return {
            "max_read_rows": self.max_read_rows,
            "sampling_mode": self.sampling_mode,
            "sampling_seed": self.sampling_seed,
            "row_ratio": self.row_ratio,
            "row_min_rows": self.row_min_rows,
            "row_max_rows": self.row_max_rows,
        }

    def reset(self) -> None:
        pass


class LocalModelProvider:
    """Provider que usa bundles o rutas locales ya resueltas."""

    def __init__(
        self,
        binary_bundle: Optional[Any] = None,
        entity_bundle: Optional[Any] = None,
        content_model_path: Optional[Any] = None,
    ):
        self.binary_bundle = binary_bundle
        self.entity_bundle = entity_bundle
        self.content_model_path = content_model_path
        self._lock = threading.RLock()

    def resolve_artifacts(self) -> ModelArtifacts:
        with self._lock:
            return ModelArtifacts(
                binary_bundle=self.binary_bundle,
                entity_bundle=self.entity_bundle,
                content_model_path=self.content_model_path,
            )

    def resolve_model_path(self, folder_name: str) -> Any:
        raise NotSupportedError(
            "LocalModelProvider no resuelve managed folders de Dataiku"
        )

    def reset(self) -> None:
        with self._lock:
            pass


class PIIAdapter:
    """
    Orquesta muestreo, lectura de fuentes y ciclo de vida del analizador.

    `model_provider` resuelve los artefactos del modelo y `source_reader` la
    entrada de datos. Las implementaciones concretas de Dataiku se inyectan
    desde las fachadas, sin importar `dataiku` aquí.
    """

    def __init__(
        self,
        model_provider: Optional[ModelProvider] = None,
        source_reader: Optional[SourceReader] = None,
        analyzer: Optional[Any] = None,
        analyzer_factory: Optional[Any] = None,
        analyzer_getter: Optional[Any] = None,
    ):
        self.model_provider = model_provider or LocalModelProvider()
        self.source_reader = source_reader or LocalSourceReader()
        self._analyzer = analyzer
        self._analyzer_factory = analyzer_factory or PIIAnalyzer
        self._analyzer_getter = analyzer_getter
        self._lock = threading.RLock()

    def build_analyzer(self, refresh: bool = False) -> Any:
        with self._lock:
            if self._analyzer is not None and not refresh:
                return self._analyzer

            if self._analyzer_getter is not None:
                analyzer = self._analyzer_getter()
            else:
                artifacts = self.model_provider.resolve_artifacts()
                analyzer = self._analyzer_factory(
                    binary_model_bundle=artifacts.binary_bundle,
                    entity_model_bundle=artifacts.entity_bundle,
                    content_model_path=artifacts.content_model_path,
                )
            self._analyzer = analyzer
            return analyzer

    def reset(self) -> None:
        with self._lock:
            self._analyzer = None
            self.model_provider.reset()
            self.source_reader.reset()

    def resolve_model_path(self, folder_name: str) -> Any:
        resolver = getattr(self.model_provider, "resolve_model_path", None)
        if not callable(resolver):
            raise NotSupportedError(
                "El proveedor de modelos no resuelve rutas de managed folders"
            )
        return resolver(folder_name)

    def _analyze_frame(
        self,
        frame: Any,
        table_name: str,
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> List[ColumnResult]:
        return self.build_analyzer().analyze(
            data=frame,
            table_meta=str(table_name),
            columns=list(columns) if columns is not None else None,
            sample_size=sample_size,
            seed=seed,
        )

    def analyze_dataframe(
        self,
        df: Any,
        table_name: str = "dataframe",
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
        apply_row_sampling: bool = True,
    ) -> List[ColumnResult]:
        if df is None:
            raise ValueError("El DataFrame de entrada es obligatorio")
        cols = list(columns) if columns is not None else None
        if apply_row_sampling:
            frame = self.source_reader.load_dataframe(df, columns=cols)
        else:
            frame = df
        return self._analyze_frame(
            frame,
            table_name=table_name,
            columns=cols,
            sample_size=sample_size,
            seed=seed,
        )

    def analyze_file(
        self,
        file_obj: Any,
        filename: str,
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
        sheet: Optional[Any] = None,
    ) -> List[ColumnResult]:
        cols = list(columns) if columns is not None else None
        frame = self.source_reader.load_file(
            file_obj,
            filename,
            columns=cols,
            sheet=sheet,
        )
        return self._analyze_frame(
            frame,
            table_name=Path(str(filename)).name or "archivo",
            columns=cols,
            sample_size=sample_size,
            seed=seed,
        )

    def analyze_dataset(
        self,
        dataset_name: str,
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> List[ColumnResult]:
        cols = list(columns) if columns is not None else None
        frame = self.source_reader.load_dataset(dataset_name, columns=cols)
        return self._analyze_frame(
            frame,
            table_name=str(dataset_name),
            columns=cols,
            sample_size=sample_size,
            seed=seed,
        )

    def health(self) -> Dict[str, Any]:
        source_info = self.source_reader.health_info()
        info: Dict[str, Any] = {
            "status": "ok",
            "engine_version": ENGINE_VERSION,
            "engine_ready": False,
        }
        info.update(source_info)
        info["sampling"] = {
            "mode": source_info.get("sampling_mode"),
            "seed": source_info.get("sampling_seed"),
            "max_read_rows": source_info.get("max_read_rows"),
            "row_ratio": source_info.get("row_ratio"),
            "row_min_rows": source_info.get("row_min_rows"),
            "row_max_rows": source_info.get("row_max_rows"),
        }

        folders_info = getattr(self.model_provider, "folders_info", None)
        if callable(folders_info):
            info["folders"] = folders_info()

        try:
            analyzer = self.build_analyzer()
            info["engine_ready"] = True
            version_getter = getattr(analyzer, "_model_version", None)
            info["model_version"] = (
                version_getter() if callable(version_getter) else "unknown"
            )
        except Exception as exc:
            info["status"] = "degraded"
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info


class LocalAdapter(PIIAdapter):
    """Fachada local entre un canal sin Dataiku y el motor PII."""

    def __init__(
        self,
        max_read_rows: Optional[int] = None,
        sampling_mode: str = "random",
        sampling_seed: int = 42,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        row_ratio: Optional[float] = DEFAULT_ROW_RATIO,
        row_min_rows: Optional[int] = DEFAULT_ROW_MIN_ROWS,
        row_max_rows: Optional[int] = DEFAULT_ROW_MAX_ROWS,
        analyzer: Optional[Any] = None,
        analyzer_factory: Optional[Any] = None,
        analyzer_getter: Optional[Any] = None,
    ):
        self._source_reader = LocalSourceReader(
            max_read_rows=max_read_rows,
            sampling_mode=sampling_mode,
            sampling_seed=sampling_seed,
            chunk_size=chunk_size,
            row_ratio=row_ratio,
            row_min_rows=row_min_rows,
            row_max_rows=row_max_rows,
        )
        super().__init__(
            model_provider=LocalModelProvider(),
            source_reader=self._source_reader,
            analyzer=analyzer,
            analyzer_factory=analyzer_factory,
            analyzer_getter=analyzer_getter,
        )

    @property
    def max_read_rows(self) -> Optional[int]:
        return self._source_reader.max_read_rows

    @property
    def row_ratio(self) -> Optional[float]:
        return self._source_reader.row_ratio

    @property
    def row_min_rows(self) -> Optional[int]:
        return self._source_reader.row_min_rows

    @property
    def row_max_rows(self) -> Optional[int]:
        return self._source_reader.row_max_rows

    @property
    def sampling_mode(self) -> str:
        return self._source_reader.sampling_mode

    @property
    def sampling_seed(self) -> int:
        return self._source_reader.sampling_seed

    @property
    def chunk_size(self) -> int:
        return self._source_reader.chunk_size

    def build_analyzer(self, refresh: bool = False) -> Any:
        return super().build_analyzer(refresh=refresh)


class DataikuManagedFolderProvider:
    """Provider que resuelve modelos desde managed folders de Dataiku DSS."""

    def __init__(
        self,
        binary_folder: str = DEFAULT_BINARY_FOLDER,
        entity_folder: str = DEFAULT_ENTITY_FOLDER,
        content_folder: str = DEFAULT_CONTENT_FOLDER,
        model_filename: str = DEFAULT_MODEL_FILENAME,
        cache_dir: Optional[Union[str, Path]] = None,
        strict_models: bool = True,
    ):
        self.binary_folder = str(binary_folder)
        self.entity_folder = str(entity_folder)
        self.content_folder = str(content_folder)
        self.model_filename = str(model_filename)
        self.strict_models = bool(strict_models)

        if cache_dir is None:
            self._cache_dir = Path(tempfile.mkdtemp(prefix="pii_engine_models_"))
            atexit.register(shutil.rmtree, str(self._cache_dir), ignore_errors=True)
        else:
            self._cache_dir = Path(cache_dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._resolved: Dict[str, Path] = {}

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
        Acepta también una ruta local directa.
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
                raise ModelArtifactError(
                    f"No se pudo abrir el managed folder {key!r}"
                ) from exc

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
                    elif (
                        self.model_filename == "model.pkl"
                        and (root / "model.joblib").exists()
                    ):
                        resolved = root / "model.joblib"

            if resolved is None:
                resolved = self._download_model(key, folder)

        with self._lock:
            self._resolved[key] = resolved
        return resolved

    def resolve_artifacts(self) -> ModelArtifacts:
        paths: Dict[str, Optional[Path]] = {}
        errors = []
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

        return ModelArtifacts(
            binary_bundle=paths.get("binary"),
            entity_bundle=paths.get("entity"),
            content_model_path=paths.get("content"),
        )

    def folders_info(self) -> Dict[str, str]:
        return {
            "binary": self.binary_folder,
            "entity": self.entity_folder,
            "content": self.content_folder,
        }

    def reset(self) -> None:
        with self._lock:
            self._resolved.clear()


class DataikuSourceReader(LocalSourceReader):
    """Lector de datasets de Dataiku DSS, además de archivos y DataFrames."""

    @staticmethod
    def _import_dataiku():
        try:
            import dataiku  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "El paquete dataiku no está disponible; ejecute el adaptador dentro de DSS"
            ) from exc
        return dataiku

    @staticmethod
    def _extract_record_count(payload: Any) -> Optional[int]:
        if not isinstance(payload, dict):
            return None
        for key in (
            "recordCount",
            "record_count",
            "recordsCount",
            "nbRecords",
            "rows",
        ):
            value = payload.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
        return None

    def _iter_dataset_chunks(self, dataset: Any, cols: Optional[List[str]]) -> Any:
        iterator = getattr(dataset, "iter_dataframes", None)
        if not callable(iterator):
            return None
        try:
            return iterator(columns=cols, chunksize=self.chunk_size)
        except TypeError:
            try:
                return iterator()
            except TypeError:
                return None

    def _get_dataset_row_count(
        self, dataset: Any, cols: Optional[List[str]]
    ) -> Optional[int]:
        for method_name in ("get_info", "get_metadata"):
            method = getattr(dataset, method_name, None)
            if not callable(method):
                continue
            try:
                count = self._extract_record_count(method())
            except Exception:
                count = None
            if count is not None:
                return count

        get_metrics = getattr(dataset, "get_last_metric_values", None)
        if callable(get_metrics):
            try:
                collector = get_metrics()
                metric = collector.get_metric_by_id("records:COUNT_RECORDS")
                if isinstance(metric, dict):
                    value = metric.get("value")
                else:
                    value = getattr(metric, "value", None)
                if value is None and hasattr(metric, "get_value"):
                    value = metric.get_value()
                if value is not None:
                    return int(value)
            except Exception:
                pass

        chunks = self._iter_dataset_chunks(dataset, cols)
        if chunks is not None:
            try:
                return count_chunks(chunks)
            except Exception:
                pass

        try:
            df = dataset.get_dataframe(columns=cols)
        except TypeError:
            df = dataset.get_dataframe()
            if cols:
                df = df[[c for c in cols if c in df.columns]]
        return int(len(df))

    def _target_rows(self, dataset: Any, cols: Optional[List[str]]) -> int:
        if self.row_ratio is None:
            if self.row_max_rows is None:
                return DEFAULT_MAX_READ_ROWS
            return self.row_max_rows
        total_rows = self._get_dataset_row_count(dataset, cols)
        if total_rows is None:
            raise ValueError(
                "No se pudo determinar el total de filas del dataset para aplicar el ratio"
            )
        return resolve_row_sample_size(
            total_rows,
            self.row_ratio,
            min_rows=self.row_min_rows,
            max_rows=self.row_max_rows,
        )

    def _read_dataset_sample(self, dataset: Any, cols: Optional[List[str]]) -> Any:
        """Muestrea filas del dataset sin depender del muestreo nativo de DSS."""
        target_rows = self._target_rows(dataset, cols)
        if target_rows <= 0:
            return pd.DataFrame(columns=cols)

        if self.sampling_mode == "head":
            try:
                return dataset.get_dataframe(
                    columns=cols,
                    limit=target_rows,
                )
            except TypeError:
                df = dataset.get_dataframe()
                if cols:
                    df = df[[c for c in cols if c in df.columns]]
                return df.head(target_rows)

        chunks = self._iter_dataset_chunks(dataset, cols)
        if chunks is not None:
            return reservoir_sample(
                chunks,
                target_rows,
                seed=self.sampling_seed,
            )

        try:
            df = dataset.get_dataframe(columns=cols)
        except TypeError:
            df = dataset.get_dataframe()
            if cols:
                df = df[[c for c in cols if c in df.columns]]
        return sample_dataframe(
            df,
            max_rows=target_rows,
            mode="random",
            seed=self.sampling_seed,
        )

    def load_dataset(
        self,
        dataset_name: str,
        columns: Optional[List[str]] = None,
    ) -> Any:
        if not dataset_name or not str(dataset_name).strip():
            raise ValueError("El nombre del dataset es obligatorio")
        dataiku = self._import_dataiku()
        dataset = dataiku.Dataset(str(dataset_name))
        cols = list(columns) if columns is not None else None
        return self._read_dataset_sample(dataset, cols)


class DataikuAdapter(PIIAdapter):
    """Fachada entre Dataiku y el motor PII."""

    def __init__(
        self,
        binary_folder: str = DEFAULT_BINARY_FOLDER,
        entity_folder: str = DEFAULT_ENTITY_FOLDER,
        content_folder: str = DEFAULT_CONTENT_FOLDER,
        model_filename: str = DEFAULT_MODEL_FILENAME,
        max_read_rows: Optional[int] = None,
        sampling_mode: str = "random",
        sampling_seed: int = 42,
        row_ratio: Optional[float] = DEFAULT_ROW_RATIO,
        row_min_rows: Optional[int] = DEFAULT_ROW_MIN_ROWS,
        row_max_rows: Optional[int] = DEFAULT_ROW_MAX_ROWS,
        cache_dir: Optional[Union[str, Path]] = None,
        analyzer_factory: Optional[Any] = None,
        strict_models: bool = True,
    ):
        self.binary_folder = str(binary_folder)
        self.entity_folder = str(entity_folder)
        self.content_folder = str(content_folder)
        self.model_filename = str(model_filename)
        self.strict_models = bool(strict_models)

        self._model_provider = DataikuManagedFolderProvider(
            binary_folder=binary_folder,
            entity_folder=entity_folder,
            content_folder=content_folder,
            model_filename=model_filename,
            cache_dir=cache_dir,
            strict_models=strict_models,
        )
        self._source_reader = DataikuSourceReader(
            max_read_rows=max_read_rows,
            sampling_mode=sampling_mode,
            sampling_seed=sampling_seed,
            chunk_size=DEFAULT_CHUNK_SIZE,
            row_ratio=row_ratio,
            row_min_rows=row_min_rows,
            row_max_rows=row_max_rows,
        )
        super().__init__(
            model_provider=self._model_provider,
            source_reader=self._source_reader,
            analyzer_factory=analyzer_factory,
        )

    @property
    def max_read_rows(self) -> Optional[int]:
        return self._source_reader.max_read_rows

    @property
    def row_ratio(self) -> Optional[float]:
        return self._source_reader.row_ratio

    @property
    def row_min_rows(self) -> Optional[int]:
        return self._source_reader.row_min_rows

    @property
    def row_max_rows(self) -> Optional[int]:
        return self._source_reader.row_max_rows

    def _read_dataset_sample(self, dataset: Any, cols: Optional[list]) -> Any:
        return self._source_reader._read_dataset_sample(dataset, cols)


__all__ = [
    "PIIAdapter",
    "LocalAdapter",
    "DataikuAdapter",
    "LocalSourceReader",
    "DataikuSourceReader",
    "LocalModelProvider",
    "DataikuManagedFolderProvider",
    "ModelArtifactError",
    "ModelArtifacts",
    "NotSupportedError",
    "SamplingConfig",
    "ModelProvider",
    "SourceReader",
    "DEFAULT_MAX_READ_ROWS",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_ROW_RATIO",
    "DEFAULT_ROW_MIN_ROWS",
    "DEFAULT_ROW_MAX_ROWS",
    "DEFAULT_BINARY_FOLDER",
    "DEFAULT_ENTITY_FOLDER",
    "DEFAULT_CONTENT_FOLDER",
    "DEFAULT_MODEL_FILENAME",
    "CSV_SUFFIXES",
    "PARQUET_SUFFIXES",
    "EXCEL_SUFFIXES",
    "SUPPORTED_FILE_SUFFIXES",
    "read_csv_sample",
    "read_parquet_sample",
    "read_excel_sample",
]
