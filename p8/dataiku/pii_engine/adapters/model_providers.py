# -*- coding: utf-8 -*-
"""Proveedores de artefactos de modelos para los adaptadores PII."""
from __future__ import annotations

import atexit
import hashlib
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Union

from pii_engine.adapters.contracts import (
    ModelArtifactError,
    ModelArtifacts,
    NotSupportedError,
)

DEFAULT_BINARY_FOLDER = "model_binary_v1"
DEFAULT_ENTITY_FOLDER = "model_entity_v1"
DEFAULT_CONTENT_FOLDER = "model_content_v1"
DEFAULT_MODEL_FILENAME = "model.pkl"


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
