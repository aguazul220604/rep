# -*- coding: utf-8 -*-
"""
Adaptador de despliegue para Dataiku DSS.

Resuelve los artefactos del managed folder `pii_models`:
`binary/model.pkl`, `entity/model.pkl` y `content/model.pkl`. Construye un
`PIIAnalyzer` y ofrece operaciones de alto nivel para la webapp:

  - `analyze_dataset(...)`: lee una muestra de un dataset del flow.
  - `analyze_dataframe(...)`: analiza un DataFrame ya cargado.
  - `analyze_file(...)`: analiza CSV/parquet subido por el usuario.
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

import pandas as pd

from pii_engine.core.analyzer import PIIAnalyzer
from pii_engine.core.schema import ENGINE_VERSION, ColumnResult

DEFAULT_MODELS_FOLDER = "pii_models"
DEFAULT_BINARY_MODEL_PATH = "binary/model.pkl"
DEFAULT_ENTITY_MODEL_PATH = "entity/model.pkl"
DEFAULT_CONTENT_MODEL_PATH = "content/model.pkl"
DEFAULT_MAX_READ_ROWS = 50_000


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
        models_folder: str = DEFAULT_MODELS_FOLDER,
        binary_model_path: str = DEFAULT_BINARY_MODEL_PATH,
        entity_model_path: str = DEFAULT_ENTITY_MODEL_PATH,
        content_model_path: str = DEFAULT_CONTENT_MODEL_PATH,
        max_read_rows: int = DEFAULT_MAX_READ_ROWS,
        cache_dir: Optional[Union[str, Path]] = None,
        analyzer_factory: Optional[Any] = None,
        strict_models: bool = True,
    ):
        if max_read_rows <= 0:
            raise ValueError("max_read_rows debe ser mayor que cero")
        self.models_folder = str(models_folder)
        self.binary_model_path = str(binary_model_path)
        self.entity_model_path = str(entity_model_path)
        self.content_model_path = str(content_model_path)
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

    def _download_model(self, folder_name: str, artifact_path: str, folder: Any) -> Path:
        artifact = str(artifact_path).replace("\\", "/").lstrip("/")
        cache_key = f"{folder_name}::{artifact}"
        dest_dir = self._cache_dir / self._cache_key(cache_key)
        dest = dest_dir / Path(artifact).name
        with self._lock:
            if dest.exists():
                return dest
            dest_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")

            stream = None
            last_error: Optional[Exception] = None
            for remote_path in (f"/{artifact}", artifact):
                try:
                    stream = folder.get_download_stream(remote_path)
                    break
                except Exception as exc:  # API varía entre versiones de DSS
                    last_error = exc

            if stream is None:
                raise ModelArtifactError(
                    f"No se pudo descargar {artifact!r} del folder {folder_name!r}"
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

    def resolve_model_path(self, artifact_path: str) -> Path:
        """
        Devuelve una ruta local al artefacto indicado dentro de `models_folder`.
        Acepta una ruta local directa, una carpeta local o un managed folder.
        """
        artifact = str(artifact_path).replace("\\", "/").lstrip("/")
        key = f"{self.models_folder}::{artifact}"
        with self._lock:
            cached = self._resolved.get(key)
            if cached is not None and cached.exists():
                return cached

        candidate = Path(artifact).expanduser()
        if candidate.is_file():
            resolved = candidate
        else:
            root = Path(self.models_folder).expanduser()
            if root.is_dir():
                model_file = root / artifact
                if not model_file.exists():
                    raise ModelArtifactError(
                        f"El directorio {self.models_folder!r} no contiene {artifact!r}"
                    )
                resolved = model_file
            else:
                dataiku = self._import_dataiku()
                try:
                    folder = dataiku.Folder(self.models_folder)
                except Exception as exc:
                    raise ModelArtifactError(
                        f"No se pudo abrir el managed folder {self.models_folder!r}"
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
                        elif (root / artifact).exists():
                            resolved = root / artifact

                if resolved is None:
                    resolved = self._download_model(self.models_folder, artifact, folder)

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
            for name, artifact in (
                ("binary", self.binary_model_path),
                ("entity", self.entity_model_path),
                ("content", self.content_model_path),
            ):
                try:
                    paths[name] = self.resolve_model_path(artifact)
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
        return self.build_analyzer().analyze(
            data=df,
            table_meta=str(table_name),
            columns=list(columns) if columns is not None else None,
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
        if not dataset_name or not str(dataset_name).strip():
            raise ValueError("El nombre del dataset es obligatorio")

        dataiku = self._import_dataiku()
        dataset = dataiku.Dataset(str(dataset_name))
        cols = list(columns) if columns is not None else None

        try:
            df = dataset.get_dataframe(
                columns=cols,
                limit=self.max_read_rows,
            )
        except TypeError:
            df = dataset.get_dataframe()
            if cols:
                df = df[[c for c in cols if c in df.columns]]
            df = df.head(self.max_read_rows)

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
    ) -> List[ColumnResult]:
        """
        Analiza un CSV o parquet. `file_obj` puede ser una ruta o un stream
        (por ejemplo `request.files["file"].stream`).
        """
        name = str(filename or "")
        suffix = Path(name).suffix.lower()
        if suffix not in {".csv", ".parquet", ".pq"}:
            raise ValueError("Formato no soportado: use CSV o parquet")

        cols = list(columns) if columns is not None else None
        close_stream = False
        if isinstance(file_obj, (str, Path)):
            stream = open(file_obj, "rb")
            close_stream = True
        else:
            stream = file_obj

        try:
            if suffix == ".csv":
                df = pd.read_csv(
                    stream,
                    nrows=self.max_read_rows,
                    usecols=cols,
                    low_memory=False,
                )
            else:
                df = pd.read_parquet(stream, columns=cols)
                df = df.head(self.max_read_rows)
        finally:
            if close_stream:
                stream.close()

        return self.analyze_dataframe(
            df,
            table_name=Path(name).name or "archivo",
            columns=cols,
            sample_size=sample_size,
            seed=seed,
        )

    def health(self) -> Dict[str, Any]:
        """Estado del motor sin exponer valores de datos."""
        info: Dict[str, Any] = {
            "status": "ok",
            "engine_version": ENGINE_VERSION,
            "engine_ready": False,
            "models_folder": self.models_folder,
            "model_files": {
                "binary": self.binary_model_path,
                "entity": self.entity_model_path,
                "content": self.content_model_path,
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
