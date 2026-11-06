# -*- coding: utf-8 -*-
from pii_engine.recognizers.base import Recognizer
from pii_engine.recognizers.context import ContextEnhancer, normalize_column_name
from pii_engine.recognizers.dictionary_recognizer import DictionaryRecognizer
from pii_engine.recognizers.model_recognizer import ModelRecognizer
from pii_engine.recognizers.regex_recognizer import RegexRecognizer
from pii_engine.recognizers.registry import RecognizerRegistry

__all__ = [
    "Recognizer",
    "ContextEnhancer",
    "normalize_column_name",
    "DictionaryRecognizer",
    "ContentModelLoader",
    "ModelRecognizer",
    "RegexRecognizer",
    "RecognizerRegistry",
]


def __getattr__(name):
    # Reexport perezoso: evita que un `model_recognizer.py` desactualizado
    # (sin ContentModelLoader) rompa el import de todo el paquete.
    if name == "ContentModelLoader":
        from pii_engine.recognizers.model_recognizer import ContentModelLoader

        return ContentModelLoader
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
