# -*- coding: utf-8 -*-
"""
Python recipe (Dataiku): entrenamiento del modelo MULTICLASE de ENTIDAD (por nombre de columna).

  Entrada : dataset con columnas  dataset, name, pii, entity
  Salida  : managed folder (en la raíz, únicamente la versión más reciente) con:
              model.joblib -> {"model","classes","temperature","min_confidence",...}
              report.html  -> reporte único autocontenido con métricas OOF (global,
                               nombres no vistos, vistos y por fold), métricas por entidad,
                               matrices de confusión, calibración, cobertura vs. exactitud,
                               análisis de errores, importancia de n-gramas, distribución
                               de entidades, conflictos e información de entrenamiento.

Diseño:
  * Solo se usan filas con pii = TRUE y entity informada (los FALSE no tienen entidad).
  * Se modelan TODAS las entidades del dataset (incluida la clase 'otro_pii'). Opcionalmente, con
    MIN_EXAMPLES puedes mandar las entidades con muy pocos nombres únicos a 'otro_pii'
    (desactivado por defecto).
  * Las probabilidades se calibran con temperature scaling sobre las predicciones fuera de muestra.
  * Aumento de datos (variantes sintéticas del nombre) SOLO en entrenamiento; la evaluación es
    siempre sobre nombres reales.

Requiere pii_utils.py (versión con apply_temperature / predict_entity) en Libraries > python.
"""
import base64
import html
import io
import json
import platform
import re
import time

import dataiku
import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
)
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import FunctionTransformer

from pii_utils import apply_temperature, normalize_name, normalize_names

# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------
INPUT_DATASET = "dataset_validated_stacked"
OUTPUT_FOLDER = "pii_entity_model"

NAME_COL = "name"
LABEL_COL = "pii"
ENTITY_COL = "entity"
GROUP_COL = "dataset"

MODEL_TYPE = "logreg"        # "logreg" | "lightgbm"
N_SPLITS = 5
RANDOM_STATE = 42

OTHER_LABEL = "otro_pii"     # clase "PII de otro tipo" (ya existe en tu dataset)
MIN_EXAMPLES = None          # None = modelar todas las entidades; p. ej. 15 = las que tengan menos
                             # nombres únicos pasan a OTHER_LABEL
MAP_TO_OTHER = set()         # entidades que quieras forzar a OTHER_LABEL
DROP_ENTITIES = set()         # entidades a eliminar por completo
DROP_CONFLICTS = False        # True: elimina nombres con más de una entidad asignada

AUGMENT = True               # variantes sintéticas (solo train)
AUG_TARGET = 120             # nombres únicos objetivo por entidad (las que tengan menos se aumentan)
AUG_WEIGHT = 0.5             # peso de las filas sintéticas

CALIBRATE_ON_UNSEEN = True   # ajusta la temperatura con los nombres que NO estaban en el train del fold
                             # (más representativo de tablas nuevas; si hay < 300, usa todos)
TARGET_PRECISION = 0.90      # precisión mínima en lo que el modelo sí responde
MIN_CONFIDENCE = None        # None -> menor umbral que alcanza TARGET_PRECISION en OOF

# ----------------------------------------------------------------------------
# Carga y limpieza
# ----------------------------------------------------------------------------
df = dataiku.Dataset(INPUT_DATASET).get_dataframe()
n_raw = len(df)


def to_bool(v):
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "si", "sí"):
            return True
        if s in ("false", "0", "no"):
            return False
    if isinstance(v, (int, float)) and not pd.isna(v):
        return bool(v)
    return None


df[LABEL_COL] = df[LABEL_COL].map(to_bool)
df = df[(df[LABEL_COL] == True) & df[NAME_COL].notna() & df[ENTITY_COL].notna()].copy()  # noqa: E712
df["entity"] = df[ENTITY_COL].astype(str).str.strip().str.lower()
df = df[(df["entity"] != "") & ~df["entity"].isin(DROP_ENTITIES)].copy()
df[GROUP_COL] = df[GROUP_COL].fillna("__unknown__").astype(str)
df["name_norm"] = [normalize_name(n) for n in df[NAME_COL]]
df = df[df["name_norm"] != ""].reset_index(drop=True)

# Un mismo nombre con más de una entidad
ent_per_name = df.groupby("name_norm")["entity"].agg(
    n_entities="nunique", entities=lambda s: " | ".join(sorted(set(s))), n="size")
conflicts = (ent_per_name[ent_per_name["n_entities"] > 1]
             .reset_index().sort_values("n", ascending=False))
if DROP_CONFLICTS and len(conflicts):
    df = df[~df["name_norm"].isin(set(conflicts["name_norm"]))].reset_index(drop=True)

# Qué entidades entran al modelo y cuáles pasan a 'otra'
counts = (df.groupby("entity")
            .agg(n_rows=("name_norm", "size"),
                 n_unique_names=("name_norm", "nunique"),
                 n_groups=(GROUP_COL, "nunique"))
            .reset_index().sort_values("n_unique_names", ascending=False))
rare = []
if MIN_EXAMPLES:
    rare = counts[(counts["n_unique_names"] < MIN_EXAMPLES)
                  & (counts["entity"] != OTHER_LABEL)
                  & ~counts["entity"].isin(MAP_TO_OTHER)]["entity"].tolist()
to_other = (set(rare) | set(MAP_TO_OTHER)) - {OTHER_LABEL}
df["label"] = np.where(df["entity"].isin(to_other), OTHER_LABEL, df["entity"])
counts["in_model"] = ~counts["entity"].isin(to_other)
counts["destination"] = np.where(counts["in_model"], counts["entity"], OTHER_LABEL)
few = counts[(counts["n_unique_names"] < 10) & counts["in_model"]]
if len(few):
    print("AVISO: entidades con menos de 10 nombres únicos (métricas poco fiables, el aumento "
          "de datos ayuda al entrenar pero no a evaluar):")
    print(few[["entity", "n_unique_names", "n_groups"]].to_string(index=False))

names = df[NAME_COL].astype(str).to_numpy(dtype=object)
norm = df["name_norm"].to_numpy()
labels = df["label"].to_numpy()
groups = df[GROUP_COL].to_numpy()
classes = np.array(sorted(df["label"].unique()))
y_idx = np.searchsorted(classes, labels)
K = len(classes)

print(f"Filas PII con entidad: {len(df)} (de {n_raw}) | clases en el modelo: {K} | "
      f"entidades enviadas a '{OTHER_LABEL}' por MIN_EXAMPLES: {len(rare)} | nombres en conflicto: {len(conflicts)}")
print(counts.to_string(index=False))


# ----------------------------------------------------------------------------
# Modelo y aumento de datos
# ----------------------------------------------------------------------------
def build_pipeline():
    feats = FeatureUnion([
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5),
                                 sublinear_tf=True, min_df=2)),
        ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2),
                                 sublinear_tf=True, token_pattern=r"[a-z0-9]+")),
    ])
    if MODEL_TYPE == "logreg":
        clf = LogisticRegression(C=10, max_iter=3000, class_weight="balanced")
    elif MODEL_TYPE == "lightgbm":
        from lightgbm import LGBMClassifier
        clf = LGBMClassifier(n_estimators=400, learning_rate=0.05, num_leaves=31,
                             min_child_samples=5, subsample=0.8, subsample_freq=1,
                             colsample_bytree=0.5, class_weight="balanced",
                             random_state=RANDOM_STATE, verbose=-1)
    else:
        raise ValueError(f"MODEL_TYPE desconocido: {MODEL_TYPE}")
    return Pipeline([
        ("norm", FunctionTransformer(normalize_names, validate=False)),
        ("tfidf", feats),
        ("clf", clf),
    ])


_PREFIXES = ["", "num", "cve", "id", "cod", "no"]


def make_variant(s, rng):
    """Variante plausible de un nombre de columna ya normalizado."""
    toks = s.split()
    for op in rng.choice(["join", "novowel", "trunc", "prefix", "suffix"],
                         size=int(rng.integers(1, 3)), replace=False):
        if op == "join":
            toks = ["".join(toks)]
        elif op == "novowel":
            toks = [t[0] + re.sub(r"[aeiou]", "", t[1:]) if len(t) > 4 else t for t in toks]
        elif op == "trunc":
            toks = [t[:int(rng.integers(4, 7))] if len(t) > 6 else t for t in toks]
        elif op == "prefix":
            toks = [_PREFIXES[int(rng.integers(len(_PREFIXES)))]] + toks
        elif op == "suffix":
            toks = toks + [str(int(rng.integers(1, 4)))]
    return " ".join(t for t in toks if t)


def augment_train(names_tr, norm_tr, y_tr, exclude, seed):
    """Añade variantes sintéticas a las entidades con pocos nombres únicos.
    'exclude' = nombres que no deben generarse (los de validación del fold)."""
    rng = np.random.default_rng(seed)
    X, Y, W = list(names_tr), list(y_tr), [1.0] * len(names_tr)
    existing = set(norm_tr) | set(exclude)
    for lab in np.unique(y_tr):
        if lab == OTHER_LABEL:
            continue
        base = sorted(set(norm_tr[y_tr == lab]))
        need, made, tries = AUG_TARGET - len(base), 0, 0
        while made < need and tries < need * 20:
            tries += 1
            v = make_variant(base[int(rng.integers(len(base)))], rng)
            if v and v not in existing:
                existing.add(v)
                X.append(v); Y.append(lab); W.append(AUG_WEIGHT)
                made += 1
    return np.array(X, dtype=object), np.array(Y), np.array(W)


def fit_model(names_tr, norm_tr, y_tr, exclude, seed):
    if AUGMENT:
        Xtr, Ytr, Wtr = augment_train(names_tr, norm_tr, y_tr, exclude, seed)
    else:
        Xtr, Ytr, Wtr = names_tr, y_tr, np.ones(len(y_tr))
    m = build_pipeline()
    m.fit(Xtr, Ytr, clf__sample_weight=Wtr)
    return m


def proba_aligned(m, X):
    """predict_proba alineado con 'classes' (una clase puede faltar en el train de un fold)."""
    out = np.zeros((len(X), K))
    out[:, np.searchsorted(classes, m.classes_)] = m.predict_proba(X)
    return out


# ----------------------------------------------------------------------------
# Validación cruzada agrupada (predicciones fuera de muestra)
# ----------------------------------------------------------------------------
oof_raw = np.zeros((len(df), K))
fold_id = np.full(len(df), -1)
unseen = np.zeros(len(df), dtype=bool)

sgkf = StratifiedGroupKFold(n_splits=N_SPLITS, shuffle=True, random_state=RANDOM_STATE)
for k, (tr, va) in enumerate(sgkf.split(names, labels, groups)):
    m = fit_model(names[tr], norm[tr], labels[tr], exclude=set(norm[va]), seed=RANDOM_STATE + k)
    oof_raw[va] = proba_aligned(m, names[va])
    fold_id[va] = k
    unseen[va] = ~pd.Series(norm[va]).isin(set(norm[tr])).to_numpy()
    acc = accuracy_score(y_idx[va], oof_raw[va].argmax(1))
    print(f"fold {k}: n={len(va)} acc={acc:.4f}")


# ----------------------------------------------------------------------------
# Calibración (temperature scaling) y umbral de abstención
# ----------------------------------------------------------------------------
def nll(P, y):
    """Log-loss multiclase (P: n x K, y: índices de clase)."""
    return float(-np.mean(np.log(np.clip(P[np.arange(len(y)), y], 1e-9, 1))))


_cal_mask = unseen if (CALIBRATE_ON_UNSEEN and unseen.sum() >= 300) else np.ones(len(df), dtype=bool)


def nll_at(T):
    return nll(apply_temperature(oof_raw[_cal_mask], T), y_idx[_cal_mask])


temperature = float(min(np.exp(np.linspace(np.log(0.2), np.log(10), 80)), key=nll_at))
oof = apply_temperature(oof_raw, temperature)
pred = oof.argmax(1)
pmax = oof.max(1)


def ece_top_label(correct, conf, n_bins=10):
    bins = np.linspace(0, 1, n_bins + 1)
    e = 0.0
    for lo, hi in zip(bins[:-1], bins[1:]):
        mk = (conf > lo) & (conf <= hi)
        if mk.any():
            e += mk.mean() * abs(correct[mk].mean() - conf[mk].mean())
    return float(e)


cov_rows = []
for t in np.round(np.arange(0.0, 0.96, 0.05), 2):
    mk = (pmax >= t) & _cal_mask
    cov_rows.append({"min_confidence": float(t), "coverage": float(mk.sum() / _cal_mask.sum()),
                     "accuracy_covered": float((pred[mk] == y_idx[mk]).mean()) if mk.any() else None})
cov_df = pd.DataFrame(cov_rows)

if MIN_CONFIDENCE is None:
    ok = cov_df[cov_df["accuracy_covered"].fillna(0) >= TARGET_PRECISION]
    min_conf = float(ok["min_confidence"].iloc[0]) if len(ok) else 0.5
    if not len(ok):
        print(f"AVISO: ningún umbral alcanza precisión {TARGET_PRECISION}; usando 0.5")
else:
    min_conf = float(MIN_CONFIDENCE)


# ----------------------------------------------------------------------------
# Métricas
# ----------------------------------------------------------------------------
def top_k_acc(P, y, k=3):
    top = np.argsort(-P, axis=1)[:, :k]
    return float(np.mean([y[i] in top[i] for i in range(len(y))]))


def summary(mask):
    if mask.sum() == 0:
        return {"n": 0}
    yt, pp, P = y_idx[mask], pred[mask], oof[mask]
    return {
        "n": int(mask.sum()),
        "accuracy": float(accuracy_score(yt, pp)),
        "macro_f1": float(f1_score(yt, pp, average="macro", labels=range(K), zero_division=0)),
        "weighted_f1": float(f1_score(yt, pp, average="weighted", labels=range(K), zero_division=0)),
        "top3_accuracy": top_k_acc(P, yt, 3),
        "log_loss": nll(P, yt),
    }


answered = pmax >= min_conf
metrics = {
    "classes": classes.tolist(),
    "temperature": temperature,
    "temperature_fit_rows": int(_cal_mask.sum()),
    "min_confidence": min_conf,
    "log_loss_before_calibration": nll(oof_raw, y_idx),
    "ece_top_label": ece_top_label((pred == y_idx).astype(float), pmax),
    "overall_oof": summary(np.ones(len(df), dtype=bool)),
    "unseen_names_oof": summary(unseen),
    "seen_names_oof": summary(~unseen),
    "share_unseen_names": float(unseen.mean()),
    "at_min_confidence": {
        "coverage": float(answered.mean()),
        "accuracy_covered": float((pred[answered] == y_idx[answered]).mean()) if answered.any() else None,
    },
    "per_fold": [dict(fold=k, **summary(fold_id == k)) for k in range(N_SPLITS)],
}

report = classification_report(y_idx, pred, labels=range(K), target_names=classes,
                               output_dict=True, zero_division=0)
per_class = (pd.DataFrame(report).T.loc[list(classes)]
             .rename(columns={"f1-score": "f1"}).reset_index().rename(columns={"index": "entity"}))
per_class["support"] = per_class["support"].astype(int)

# ----------------------------------------------------------------------------
# Gráficas (PNG en Base64 para incrustar en el reporte HTML)
# ----------------------------------------------------------------------------
def fig_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def plot_confusion(mask, title):
    if not mask.any():
        return None
    cm_part = confusion_matrix(y_idx[mask], pred[mask], labels=range(K))
    cmn = cm_part / cm_part.sum(axis=1, keepdims=True).clip(min=1)
    side = max(6, 0.45 * K + 3)
    fig, ax = plt.subplots(figsize=(side, side))
    ax.imshow(cmn, cmap="Blues", vmin=0, vmax=1)
    ax.set_xticks(range(K)); ax.set_xticklabels(classes, rotation=90, fontsize=7)
    ax.set_yticks(range(K)); ax.set_yticklabels(classes, fontsize=7)
    for i in range(K):
        for j in range(K):
            if cm_part[i, j] > 0:
                ax.text(j, i, str(cm_part[i, j]), ha="center", va="center", fontsize=6,
                        color="white" if cmn[i, j] > 0.5 else "black")
    ax.set_xlabel("Predicho"); ax.set_ylabel("Real")
    ax.set_title(f"{title}\nColor = % por fila | n = {int(mask.sum()):,}")
    return fig_b64(fig)


def plot_reliability():
    correct = (pred == y_idx).astype(float)
    edges = np.unique(np.quantile(pmax, np.linspace(0, 1, 11)))
    idx = np.clip(np.digitize(pmax, edges[1:-1]), 0, len(edges) - 2)
    xs = [pmax[idx == b].mean() for b in range(len(edges) - 1) if (idx == b).any()]
    ys = [correct[idx == b].mean() for b in range(len(edges) - 1) if (idx == b).any()]
    fig, ax = plt.subplots(figsize=(5.8, 4.2))
    ax.plot([0, 1], [0, 1], "--", color="gray", label="Perfecta")
    ax.plot(xs, ys, marker="o", label="Modelo (OOF)")
    ax.set_xlabel("Confianza (prob. de la clase predicha)"); ax.set_ylabel("Accuracy real")
    ax.set_title(f"Calibración top-label (T={temperature:.2f})")
    ax.grid(alpha=0.25); ax.legend()
    return fig_b64(fig)


def plot_coverage():
    fig, ax = plt.subplots(figsize=(5.8, 4.2))
    ax.plot(cov_df["min_confidence"], cov_df["coverage"], marker="o",
            color="#2563eb", label="Cobertura")
    ax.plot(cov_df["min_confidence"], cov_df["accuracy_covered"], marker="o",
            color="#16a34a", label="Accuracy en respondidas")
    ax.axvline(min_conf, color="#dc2626", linestyle="--",
               label=f"Umbral elegido = {min_conf:.2f}")
    ax.axhline(TARGET_PRECISION, color="#9333ea", linestyle=":",
               label=f"Objetivo = {TARGET_PRECISION:.2f}")
    ax.set_xlabel("Confianza mínima"); ax.set_ylabel("Proporción")
    ax.set_ylim(0, 1.02); ax.set_title("Cobertura y exactitud vs. abstención")
    ax.grid(alpha=0.25); ax.legend(fontsize=8)
    return fig_b64(fig)


cm_b64 = plot_confusion(np.ones(len(df), dtype=bool), "Matriz de confusión (OOF Global)")
cm_unseen_b64 = plot_confusion(unseen, "Matriz de confusión (Nombres no vistos)")
cal_b64 = plot_reliability()
coverage_b64 = plot_coverage()

# ----------------------------------------------------------------------------
# Modelo final con todos los datos
# ----------------------------------------------------------------------------
final = fit_model(names, norm, labels, exclude=set(), seed=RANDOM_STATE)

clf = final.named_steps["clf"]
feat_names = final.named_steps["tfidf"].get_feature_names_out()
if MODEL_TYPE == "logreg":
    rows = []
    for ci, c in enumerate(clf.classes_):
        top = np.argsort(-clf.coef_[ci])[:15]
        rows += [{"entity": c, "feature": feat_names[j], "weight": float(clf.coef_[ci][j])} for j in top]
    imp = pd.DataFrame(rows)
else:
    imp = (pd.DataFrame({"feature": feat_names, "weight": clf.feature_importances_})
           .sort_values("weight", ascending=False).head(300))

bundle = {
    "model": final,              # usar pii_utils.predict_entity(bundle, nombres)
    "classes": classes,
    "temperature": temperature,
    "min_confidence": min_conf,
    "other_label": OTHER_LABEL,
    "rare_entities": sorted(rare),
    "model_type": MODEL_TYPE,
    "name_col": NAME_COL,
}

training_info = {
    "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "input_dataset": INPUT_DATASET,
    "rows_raw": int(n_raw),
    "rows_used": int(len(df)),
    "n_classes": int(K),
    "n_groups": int(pd.Series(groups).nunique()),
    "n_conflicting_names": int(len(conflicts)),
    "dropped_conflicts": bool(DROP_CONFLICTS),
    "config": {
        "model_type": MODEL_TYPE, "n_splits": N_SPLITS, "min_examples": MIN_EXAMPLES,
        "map_to_other": sorted(MAP_TO_OTHER), "augment": AUGMENT, "aug_target": AUG_TARGET,
        "aug_weight": AUG_WEIGHT, "target_precision": TARGET_PRECISION,
        "min_confidence_fixed": MIN_CONFIDENCE, "group_col": GROUP_COL, "random_state": RANDOM_STATE,
    },
    "versions": {"python": platform.python_version(), "sklearn": sklearn.__version__,
                 "pandas": pd.__version__, "numpy": np.__version__, "joblib": joblib.__version__},
}

top2 = np.argsort(-oof, axis=1)[:, :2]
oof_df = pd.DataFrame({
    "dataset": df[GROUP_COL], "name": df[NAME_COL], "name_norm": df["name_norm"],
    "entity_real": df["entity"], "label": labels,
    "pred_1": classes[top2[:, 0]], "prob_1": oof[np.arange(len(oof)), top2[:, 0]],
    "pred_2": classes[top2[:, 1]] if K > 1 else None,
    "prob_2": oof[np.arange(len(oof)), top2[:, 1]] if K > 1 else None,
    "fold": fold_id, "unseen_name": unseen,
})

# ----------------------------------------------------------------------------
# Construcción del reporte único HTML
# ----------------------------------------------------------------------------
def _fmt(v, fmt=".3f"):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    if isinstance(v, (int, np.integer)):
        return f"{int(v):,}"
    if isinstance(v, (float, np.floating)):
        return format(float(v), fmt)
    return html.escape(str(v))


def _img_card(title, b64_str, subtitle=""):
    if not b64_str:
        return f'<div class="card plot-card"><h3>{html.escape(title)}</h3><p class="muted">Sin datos suficientes</p></div>'
    sub_html = f'<p class="muted">{html.escape(subtitle)}</p>' if subtitle else ""
    return (
        f'<div class="card plot-card">'
        f'<h3>{html.escape(title)}</h3>{sub_html}'
        f'<img src="data:image/png;base64,{b64_str}" alt="{html.escape(title)}">'
        f'</div>'
    )


def build_html_report():
    o = metrics["overall_oof"]
    u = metrics["unseen_names_oof"]
    s = metrics["seen_names_oof"]
    a = metrics["at_min_confidence"]

    split_rows = [
        ("OOF Global (todas las filas)", o),
        ("Nombres NO vistos en train (OOF)", u),
        ("Nombres vistos en train (OOF)", s),
    ]

    def _metric_row(label_row, m_row, first_is_fold=False):
        first = f"Fold {label_row}" if first_is_fold else label_row
        return (
            f"<tr><td><strong>{html.escape(str(first))}</strong></td>"
            f"<td class='num'>{_fmt(m_row.get('n'))}</td>"
            f"<td class='num'><strong>{_fmt(m_row.get('macro_f1'))}</strong></td>"
            f"<td class='num'>{_fmt(m_row.get('weighted_f1'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('accuracy'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('top3_accuracy'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('log_loss'), '.4f')}</td></tr>"
        )

    comp_html = "".join(_metric_row(label_row, m_row) for label_row, m_row in split_rows)
    folds_html = "".join(
        _metric_row(f_row["fold"], f_row, first_is_fold=True) for f_row in metrics["per_fold"]
    )

    per_class_html = "".join(
        f"<tr><td><code>{html.escape(str(r['entity']))}</code></td>"
        f"<td class='num'>{_fmt(r['support'])}</td>"
        f"<td class='num'>{_fmt(r['precision'])}</td>"
        f"<td class='num'>{_fmt(r['recall'])}</td>"
        f"<td class='num'><strong>{_fmt(r['f1'])}</strong></td></tr>"
        for _, r in per_class.iterrows()
    )

    errors_df = oof_df[oof_df["label"] != oof_df["pred_1"]].sort_values(
        "prob_1", ascending=False).head(50)
    if len(errors_df):
        errors_html = "".join(
            f"<tr><td>{html.escape(str(r['dataset']))}</td>"
            f"<td><code>{html.escape(str(r['name']))}</code></td>"
            f"<td><code>{html.escape(str(r['label']))}</code></td>"
            f"<td><code>{html.escape(str(r['pred_1']))}</code></td>"
            f"<td class='num'><strong>{r['prob_1']:.3f}</strong></td>"
            f"<td><code>{html.escape(str(r['pred_2']))}</code></td>"
            f"<td class='num'>{_fmt(r['prob_2'])}</td>"
            f"<td class='num'>{int(r['fold'])}</td>"
            f"<td>{'Sí' if r['unseen_name'] else 'No'}</td></tr>"
            for _, r in errors_df.iterrows()
        )
    else:
        errors_html = "<tr><td colspan='9' class='muted'>Sin errores en las predicciones OOF</td></tr>"

    if len(imp):
        imp_html = "".join(
            f"<tr><td><code>{html.escape(str(r.get('entity', '')))}</code></td>"
            f"<td><code>{html.escape(str(r['feature']))}</code></td>"
            f"<td class='num'>{r['weight']:+.4f}</td></tr>"
            for _, r in imp.iterrows()
        )
    else:
        imp_html = "<tr><td colspan='3' class='muted'>Sin importancias disponibles</td></tr>"

    counts_html = "".join(
        f"<tr><td><code>{html.escape(str(r['entity']))}</code></td>"
        f"<td class='num'>{_fmt(r['n_rows'])}</td>"
        f"<td class='num'>{_fmt(r['n_unique_names'])}</td>"
        f"<td class='num'>{_fmt(r['n_groups'])}</td>"
        f"<td><code>{html.escape(str(r['destination']))}</code></td></tr>"
        for _, r in counts.iterrows()
    )

    if len(conflicts):
        conflicts_html = "".join(
            f"<tr><td><code>{html.escape(str(r['name_norm']))}</code></td>"
            f"<td>{html.escape(str(r['entities']))}</td>"
            f"<td class='num'>{_fmt(r['n_entities'])}</td>"
            f"<td class='num'>{_fmt(r['n'])}</td></tr>"
            for _, r in conflicts.head(200).iterrows()
        )
    else:
        conflicts_html = "<tr><td colspan='4' class='muted'>No se detectaron nombres con entidades contradictorias</td></tr>"

    rare_html = ", ".join(f"<code>{html.escape(e)}</code>" for e in sorted(to_other)) or "Ninguna"
    raw_json = html.escape(json.dumps(
        {"training_info": training_info, "metrics": metrics, "rare_entities": sorted(rare)},
        indent=2, ensure_ascii=False))

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reporte Modelo de Entidad PII — {html.escape(training_info['trained_at'])}</title>
<style>
  :root {{
    --bg: #f8fafc; --card: #ffffff; --border: #e2e8f0;
    --text: #0f172a; --muted: #64748b; --primary: #1d4ed8; --accent: #eff6ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.45; font-size: 14px;
  }}
  .container {{ max-width: 1360px; margin: 0 auto; }}
  header {{
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 20px 24px; margin-bottom: 20px; display: flex; flex-wrap: wrap;
    justify-content: space-between; align-items: center; gap: 12px;
  }}
  header h1 {{ margin: 0 0 6px 0; font-size: 20px; }}
  .badges {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .badge {{
    background: var(--accent); color: var(--primary); border: 1px solid #bfdbfe;
    padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
  }}
  .kpi-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 12px; margin-bottom: 20px;
  }}
  .kpi {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
  .kpi .label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}
  .kpi .val {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
  .kpi .sub {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; margin-bottom: 20px; }}
  .card h2 {{ margin: 0 0 12px 0; font-size: 16px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
  .card h3 {{ margin: 0 0 8px 0; font-size: 14px; }}
  .muted {{ color: var(--muted); font-size: 12.5px; margin: 0 0 10px 0; }}
  .plots-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(430px, 1fr)); gap: 16px; margin-bottom: 20px; }}
  .plot-card {{ text-align: center; margin-bottom: 0; }}
  .plot-card img {{ max-width: 100%; height: auto; border-radius: 6px; }}
  .table-wrap {{ overflow: auto; max-height: 480px; border: 1px solid var(--border); border-radius: 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left; white-space: nowrap; }}
  th {{ background: #f1f5f9; font-weight: 600; position: sticky; top: 0; z-index: 1; }}
  tr:hover td {{ background: #f8fafc; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  code {{ background: #f1f5f9; padding: 2px 5px; border-radius: 4px; font-size: 12px; }}
  details summary {{ cursor: pointer; font-weight: 600; color: var(--primary); padding: 6px 0; }}
  pre {{ background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 8px; overflow-x: auto; font-size: 12px; max-height: 480px; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>Reporte de Entrenamiento — Modelo Multiclase de Entidad PII</h1>
      <p class="muted" style="margin:0;">
        Dataset: <code>{html.escape(INPUT_DATASET)}</code> &rarr; Managed Folder: <code>{html.escape(OUTPUT_FOLDER)}</code>
        &nbsp;|&nbsp; Entrenado: <strong>{html.escape(training_info['trained_at'])}</strong>
      </p>
    </div>
    <div class="badges">
      <span class="badge">Modelo: {html.escape(MODEL_TYPE)}</span>
      <span class="badge">Entidades: {K}</span>
      <span class="badge">StratifiedGroupKFold: {N_SPLITS} folds</span>
      <span class="badge">Temperatura: {temperature:.3f}</span>
      <span class="badge">Confianza mínima: {min_conf:.3f}</span>
    </div>
  </header>

  <div class="kpi-grid">
    <div class="kpi"><div class="label">Filas utilizadas</div><div class="val">{len(df):,}</div>
      <div class="sub">de {n_raw:,} crudas | {training_info['n_groups']:,} tablas</div></div>
    <div class="kpi"><div class="label">Macro-F1 (OOF Global)</div><div class="val">{_fmt(o.get('macro_f1'))}</div>
      <div class="sub">Accuracy: {_fmt(o.get('accuracy'))} | Weighted F1: {_fmt(o.get('weighted_f1'))}</div></div>
    <div class="kpi"><div class="label">Macro-F1 Nombres No Vistos</div><div class="val">{_fmt(u.get('macro_f1'))}</div>
      <div class="sub">n = {_fmt(u.get('n'))} ({metrics['share_unseen_names']:.1%} del total)</div></div>
    <div class="kpi"><div class="label">Top-3 Accuracy</div><div class="val">{_fmt(o.get('top3_accuracy'))}</div>
      <div class="sub">No vistos: {_fmt(u.get('top3_accuracy'))}</div></div>
    <div class="kpi"><div class="label">Cobertura / Accuracy</div><div class="val">{_fmt(a.get('coverage'), '.1%')} / {_fmt(a.get('accuracy_covered'))}</div>
      <div class="sub">Con confianza &ge; {min_conf:.3f}</div></div>
    <div class="kpi"><div class="label">Calibración (OOF)</div><div class="val">{_fmt(metrics.get('ece_top_label'), '.4f')}</div>
      <div class="sub">ECE | LogLoss: {_fmt(o.get('log_loss'), '.4f')}</div></div>
  </div>

  <div class="card">
    <h2>1. Resumen Comparativo de Métricas (Out-Of-Fold)</h2>
    <p class="muted">Evaluación fuera de muestra agrupada por <code>{html.escape(GROUP_COL)}</code>. El corte de nombres no vistos estima la generalización a nuevos nombres de columna.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Subconjunto OOF</th><th class="num">N</th><th class="num">Macro-F1</th><th class="num">Weighted F1</th><th class="num">Accuracy</th><th class="num">Top-3</th><th class="num">Log Loss</th></tr></thead>
      <tbody>{comp_html}</tbody>
    </table></div>
  </div>

  <div class="card">
    <h2>2. Desglose por Fold ({N_SPLITS})</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Fold</th><th class="num">N</th><th class="num">Macro-F1</th><th class="num">Weighted F1</th><th class="num">Accuracy</th><th class="num">Top-3</th><th class="num">Log Loss</th></tr></thead>
      <tbody>{folds_html}</tbody>
    </table></div>
  </div>

  <div class="plots-grid">
    {_img_card("Matriz de Confusión (OOF Global)", cm_b64, "Todas las filas de validación fuera de muestra")}
    {_img_card("Matriz de Confusión (Nombres No Vistos)", cm_unseen_b64, "Nombres normalizados ausentes en el train del fold")}
    {_img_card("Curva de Calibración", cal_b64, f"Temperature scaling: T = {temperature:.3f} | filas de ajuste = {metrics['temperature_fit_rows']:,}")}
    {_img_card("Cobertura y Exactitud vs. Umbral", coverage_b64, f"Umbral seleccionado para accuracy objetivo = {TARGET_PRECISION:.2f}")}
  </div>

  <div class="card">
    <h2>3. Métricas por Entidad (OOF Global)</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Entidad</th><th class="num">Soporte</th><th class="num">Precision</th><th class="num">Recall</th><th class="num">F1</th></tr></thead>
      <tbody>{per_class_html}</tbody>
    </table></div>
  </div>

  <div class="card">
    <h2>4. Errores OOF Más Confiados (Top 50)</h2>
    <p class="muted">Predicciones incorrectas ordenadas por probabilidad de la clase predicha. Útiles para detectar confusiones y posibles errores de etiquetado.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Dataset</th><th>Nombre</th><th>Entidad real</th><th>Predicción 1</th><th class="num">Prob. 1</th><th>Predicción 2</th><th class="num">Prob. 2</th><th class="num">Fold</th><th>No visto</th></tr></thead>
      <tbody>{errors_html}</tbody>
    </table></div>
  </div>

  <div class="card">
    <h2>5. Importancia de Características (Modelo Final)</h2>
    <p class="muted">N-gramas TF-IDF con mayor peso por entidad; en LightGBM se muestran importancias globales.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Entidad</th><th>Feature (char / word n-gram)</th><th class="num">Peso</th></tr></thead>
      <tbody>{imp_html}</tbody>
    </table></div>
  </div>

  <div class="card">
    <h2>6. Distribución de Entidades</h2>
    <p class="muted">Entidades enviadas a <code>{html.escape(OTHER_LABEL)}</code> por rareza o configuración: {rare_html}.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Entidad original</th><th class="num">Filas</th><th class="num">Nombres únicos</th><th class="num">Tablas</th><th>Etiqueta modelada</th></tr></thead>
      <tbody>{counts_html}</tbody>
    </table></div>
  </div>

  <div class="card">
    <h2>7. Nombres Normalizados con Entidades en Conflicto ({len(conflicts):,})</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Nombre normalizado</th><th>Entidades</th><th class="num">N.º entidades</th><th class="num">Apariciones</th></tr></thead>
      <tbody>{conflicts_html}</tbody>
    </table></div>
  </div>

  <div class="card">
    <h2>8. Configuración, Versiones y Métricas en JSON</h2>
    <details><summary>Ver JSON completo (training_info + metrics + rare_entities)</summary><pre>{raw_json}</pre></details>
  </div>
</div>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# Escritura al managed folder (en la raíz: solo la versión más reciente)
# ----------------------------------------------------------------------------
folder = dataiku.Folder(OUTPUT_FOLDER)


def save(filename, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    folder.upload_data(filename, data)


buf = io.BytesIO()
joblib.dump(bundle, buf, compress=3)
save("model.joblib", buf.getvalue())
save("report.html", build_html_report())

o, u, a = metrics["overall_oof"], metrics["unseen_names_oof"], metrics["at_min_confidence"]
print(f"Listo ({training_info['trained_at']}) | T={temperature:.2f} | min_conf={min_conf:.2f} | "
      f"OOF: acc={o['accuracy']:.3f} macroF1={o['macro_f1']:.3f} top3={o['top3_accuracy']:.3f} | "
      f"nombres no vistos: macroF1={u.get('macro_f1', float('nan')):.3f} (n={u['n']}) | "
      f"cobertura={a['coverage']:.2f} precisión={a['accuracy_covered']}")
