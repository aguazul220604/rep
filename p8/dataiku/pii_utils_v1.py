# -*- coding: utf-8 -*-
"""
Libraries > python > pii_utils.py  (librería del proyecto en Dataiku)

Librería compartida por los modelos binario de PII y multiclase de entidad.

Va en la librería del proyecto (no dentro de las recipes) para que los modelos
guardados con joblib puedan volver a cargarse en otras recipes y en la webapp:
los pipelines referencian estas funciones por nombre de módulo.

Contenido:
  * normalize_name / normalize_names: normalización común de ambos modelos.
  * apply_temperature: calibración de probabilidades del modelo de entidad.
  * predict_entity: inferencia del bundle del modelo multiclase de entidad.
"""
import re
import unicodedata

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name):
    """'Num_Cliente' / 'num-cliente' / 'NumCliente' -> 'num cliente'."""
    if name is None:
        return ""
    s = str(name)
    s = _CAMEL.sub(r"\1 \2", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _NON_ALNUM.sub(" ", s)
    return s.strip()


def normalize_names(names):
    """Versión vectorizada para usar en un FunctionTransformer."""
    return [normalize_name(n) for n in names]


# ---------------------------------------------------------------------------
# Utilidades del modelo de ENTIDAD (multiclase)
# ---------------------------------------------------------------------------
def apply_temperature(proba, temperature):
    """Reescala probabilidades con temperature scaling (T=1 no cambia nada)."""
    import numpy as np
    logp = np.log(np.clip(np.asarray(proba, dtype=float), 1e-9, 1.0)) / float(temperature)
    logp -= logp.max(axis=1, keepdims=True)
    q = np.exp(logp)
    return q / q.sum(axis=1, keepdims=True)


def predict_entity(bundle, names, top_k=3):
    """
    Aplica el bundle del modelo de entidad (model.joblib de pii_entity_model).

    Devuelve un DataFrame con:
      entity       -> entidad final, o None si el modelo se abstiene
                      (confianza < min_confidence o predicción == other_label)
      prob_entity  -> probabilidad calibrada de la clase más probable
      entity_top1  -> clase más probable (aunque haya abstención)
      top{k}       -> lista [(clase, prob), ...]
    Las entidades raras (bundle['rare_entities']) no las predice el modelo:
    corresponden a la capa de reglas / contenido.
    """
    import numpy as np
    import pandas as pd

    model = bundle["model"]
    classes = np.asarray(bundle["classes"])
    names = np.asarray(list(names), dtype=object)

    raw = model.predict_proba(names)
    P = np.zeros((len(names), len(classes)))
    P[:, np.searchsorted(classes, model.classes_)] = raw
    P = apply_temperature(P, bundle.get("temperature", 1.0))

    order = np.argsort(-P, axis=1)[:, :top_k]
    top1 = classes[order[:, 0]]
    p1 = P[np.arange(len(P)), order[:, 0]]
    abstain = (p1 < bundle["min_confidence"]) | (top1 == bundle["other_label"])
    entity = np.where(abstain, None, top1)
    top = [[(str(classes[j]), float(P[i, j])) for j in order[i]] for i in range(len(P))]
    return pd.DataFrame({"entity": entity, "prob_entity": p1,
                         "entity_top1": top1, f"top{top_k}": top})
