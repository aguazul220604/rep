# -*- coding: utf-8 -*-
"""
pii_content_features.py

Código COMPARTIDO entre el entrenamiento y el motor de inferencia del modelo
de contenido. Va en Dataiku: Project > Libraries > python/pii_content_features.py

Por qué es un módulo y no código dentro de la recipe: joblib guarda las clases y
funciones del pipeline por referencia. Si estuvieran definidas en la recipe
(__main__), el .joblib no se podría cargar desde el motor. La recipe además
guarda una copia de este archivo junto al modelo.
"""
import re
import unicodedata

import numpy as np
import scipy.sparse as sp
from sklearn.base import BaseEstimator, TransformerMixin

_DIGIT = re.compile(r"\d")
_WS = re.compile(r"\s+")


def canonical(s):
    """minúsculas, sin acentos, espacios colapsados (para deduplicar y agrupar)."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _WS.sub(" ", s).strip().casefold()


def normalize_text(s):
    """Preprocesador de los TF-IDF: canonical + cada dígito -> '0'.

    Hace al modelo independiente de mayúsculas y acentos (los datos reales
    suelen venir en MAYÚSCULAS y sin acentos) y evita que cada número distinto
    sea un token nuevo.
    """
    return _DIGIT.sub("0", canonical(s))


class ValueShapeFeatures(BaseEstimator, TransformerMixin):
    """Features numéricas de forma, insensibles a mayúsculas/acentos."""

    NAMES = [
        "log_len", "n_tokens", "mean_token_len", "digit_ratio",
        "alpha_ratio", "punct_ratio", "n_digit_groups", "has_comma",
    ]

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        rows = []
        for s in X:
            s = str(s)
            n = len(s) or 1
            toks = s.split()
            nt = len(toks)
            letters = sum(c.isalpha() for c in s)
            digits = sum(c.isdigit() for c in s)
            punct = sum((not c.isalnum()) and (not c.isspace()) for c in s)
            rows.append([
                np.log1p(len(s)),
                min(nt, 15),
                float(np.mean([len(t) for t in toks])) if nt else 0.0,
                digits / n,
                letters / n,
                punct / n,
                min(len(re.findall(r"\d+", s)), 10),
                1.0 if "," in s else 0.0,
            ])
        return sp.csr_matrix(np.asarray(rows, dtype=np.float64))

    def get_feature_names_out(self, input_features=None):
        return np.asarray(self.NAMES, dtype=object)


def apply_temperature(proba, T):
    """Escalado por temperatura: softmax(log p / T). T=1 no cambia nada."""
    logp = np.log(np.clip(np.asarray(proba, dtype=np.float64), 1e-12, 1.0)) / T
    logp -= logp.max(axis=1, keepdims=True)
    e = np.exp(logp)
    return e / e.sum(axis=1, keepdims=True)


def aggregate_column(proba, labels, tau, theta, other_label="otro"):
    """Decide la entidad de UNA columna a partir de las probabilidades por valor.

    proba : array (n_valores, K) ya calibrado (apply_temperature).
    labels: lista de clases en el orden de las columnas de proba.
    tau   : probabilidad mínima para que un valor 'vote' por su clase.
    theta : cobertura mínima (fracción de valores votando) para aceptar la clase.

    Devuelve (etiqueta, {clase: cobertura}); si ninguna clase llega a theta
    devuelve other_label.
    """
    proba = np.asarray(proba)
    top = proba.argmax(axis=1)
    conf = proba.max(axis=1)
    cov = {}
    for j, lab in enumerate(labels):
        if lab == other_label:
            continue
        cov[lab] = float(np.mean((top == j) & (conf >= tau)))
    best = max(cov, key=cov.get)
    return (best if cov[best] >= theta else other_label), cov
