# -*- coding: utf-8 -*-
"""
Python recipe (Dataiku): entrena el modelo de análisis de contenido.

Entrada : dataset de entrenamiento (value, type) con las clases
          nombre, apellido, direccion, empresa y otro
Salida  : managed folder con
            content_model.joblib      pipeline completo (TF-IDF + logreg)
            config.json               etiquetas, temperatura, tau, theta, versiones
            pii_content_features.py   copia del módulo compartido
            metrics.json              métricas completas
            classification_report.csv / confusion_matrix.csv / .png
            column_level_grid.csv     simulación a nivel columna (tau x theta)
            top_features.csv          features con más peso por clase
            oof_errors.csv            errores más confiados (revisar etiquetas)

Requiere el módulo pii_content_features.py en Project > Libraries > python.
"""
import inspect
import io
import json
import platform
import re
import time

import joblib
import numpy as np
import pandas as pd
import scipy
import sklearn
import dataiku
from scipy.optimize import minimize_scalar
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, classification_report,
                             confusion_matrix, f1_score, log_loss)
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import MaxAbsScaler

import pii_content_features as pcf
from pii_content_features import (ValueShapeFeatures, aggregate_column,
                                  apply_temperature, canonical, normalize_text)

# ======================= CONFIG =======================
INPUT_DATASET = "training_dataset_with_otro"   # <-- cambia al nombre de tu dataset
OUTPUT_FOLDER = "model_content_v1"             # <-- cambia al nombre de tu managed folder
MODEL_VERSION = "v1"
VALUE_COL, TYPE_COL = "value", "type"
OTRO_LABEL = "otro"
SEED = 42

N_SPLITS = 5
MAX_ROWS_PER_CLASS = None      # p. ej. 200_000 para acelerar; None = todo
MAX_VALUE_LEN = 120

# Modelo
CHAR_NGRAM = (2, 5)
CHAR_MAX_FEATURES = 400_000
WORD_MAX_FEATURES = 100_000
MIN_DF = 3
C = 4.0
CLASS_WEIGHT = "balanced"
MAX_ITER = 300

# Robustez: fracción de valores de validación a los que se les mete ruido
STRESS_NOISE_P = 0.5

# Simulación de columnas (decide tau, theta y mide el desempeño real del motor)
COL_SAMPLE_SIZE = 200          # valores por columna (tamaño de muestra del motor)
COLS_PER_CLASS = 300
PURITY_LEVELS = [1.0, 0.8, 0.6]   # fracción de valores de la clase real en la columna
PURITY_FOR_SELECTION = 0.8
TAU_GRID = [0.5, 0.6, 0.7, 0.8, 0.9]
THETA_GRID = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
MAX_OTRO_FPR = 0.01            # máx. % de columnas "otro" marcadas como PII difícil
# ======================================================

t0 = time.time()
folder = dataiku.Folder(OUTPUT_FOLDER)


def put_bytes(path, data):
    folder.upload_stream(path, io.BytesIO(data))


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def put_json(path, obj):
    put_bytes(path, json.dumps(obj, default=_json_default, ensure_ascii=False, indent=2).encode("utf-8"))


def put_df(path, df, index=False):
    put_bytes(path, df.to_csv(index=index).encode("utf-8"))


# ----------------------- datos -----------------------
df = dataiku.Dataset(INPUT_DATASET).get_dataframe()[[VALUE_COL, TYPE_COL]].dropna()
df[VALUE_COL] = df[VALUE_COL].astype(str).str.strip()
df[TYPE_COL] = df[TYPE_COL].astype(str).str.strip()
df = df[(df[VALUE_COL] != "") & (df[VALUE_COL].str.len() <= MAX_VALUE_LEN)].copy()
n_raw = len(df)

df["_canon"] = df[VALUE_COL].map(canonical)
df = df.drop_duplicates(subset=["_canon", TYPE_COL])
n_dedup = len(df)

# valores canónicos presentes en más de una clase (ambigüedad inherente: "Guadalupe", "Martinez"...)
n_conflicts = int((df.groupby("_canon")[TYPE_COL].nunique() > 1).sum())

if MAX_ROWS_PER_CLASS:
    df = (df.groupby(TYPE_COL, group_keys=False)
            .apply(lambda g: g.sample(min(len(g), MAX_ROWS_PER_CLASS), random_state=SEED)))

df = df.sample(frac=1, random_state=SEED).reset_index(drop=True)

labels = sorted(df[TYPE_COL].unique().tolist())
if OTRO_LABEL not in labels:
    raise ValueError("No hay filas con type='%s' en el dataset de entrada." % OTRO_LABEL)
K = len(labels)
label_to_idx = {l: i for i, l in enumerate(labels)}
otro_idx = label_to_idx[OTRO_LABEL]
hard_idx = [i for i in range(K) if i != otro_idx]

X = df[VALUE_COL].values.astype(object)
y = df[TYPE_COL].map(label_to_idx).values
n = len(df)
class_counts = df[TYPE_COL].value_counts().to_dict()
print("Filas: %d (crudas %d) | clases: %s" % (n, n_raw, class_counts))
print("Valores canónicos en >1 clase: %d" % n_conflicts)

# ----------------------- grupos para el CV -----------------------
# Los valores que comparten su token alfabético más largo caen siempre en el mismo
# fold: así el test contiene nombres/calles/empresas NO vistos en train. Con un split
# aleatorio, Faker repite léxico y el F1 sale inflado.
def group_key(c):
    toks = re.findall(r"[a-z]{3,}", c)
    return max(toks, key=lambda t: (len(t), t)) if toks else c


keys = df["_canon"].map(group_key)
cap = max(20, int(0.002 * n))   # grupos enormes (palabras estructurales) se parten
cnt = keys.groupby(keys).transform("size")
cc = keys.groupby(keys).cumcount()
split_mask = cnt > cap
groups = keys.copy()
groups[split_mask] = (keys[split_mask] + "#" + (cc[split_mask] % np.ceil(cnt[split_mask] / cap)).astype(int).astype(str))
groups = groups.values
n_groups = int(len(set(groups)))
print("Grupos: %d | filas en grupos partidos: %.1f%%" % (n_groups, 100.0 * split_mask.mean()))


# ----------------------- pipeline -----------------------
def build_pipeline():
    feats = FeatureUnion([
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=CHAR_NGRAM,
                                 preprocessor=normalize_text, min_df=MIN_DF,
                                 max_features=CHAR_MAX_FEATURES, sublinear_tf=True)),
        ("word", TfidfVectorizer(analyzer="word", token_pattern=r"(?u)\b\w+\b",
                                 ngram_range=(1, 2), preprocessor=normalize_text,
                                 min_df=MIN_DF, max_features=WORD_MAX_FEATURES,
                                 sublinear_tf=True)),
        ("shape", Pipeline([("f", ValueShapeFeatures()), ("s", MaxAbsScaler())])),
    ])
    clf = LogisticRegression(C=C, class_weight=CLASS_WEIGHT, max_iter=MAX_ITER)
    return Pipeline([("features", feats), ("clf", clf)])


# ruido para la prueba de robustez (typos, truncado, letra duplicada)
def perturb(s, rng):
    if len(s) < 4 or rng.random() > STRESS_NOISE_P:
        return s
    k = int(rng.integers(0, 4))
    i = int(rng.integers(1, len(s) - 1))
    if k == 0:
        return s[:i] + s[i + 1:]
    if k == 1:
        return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    if k == 2:
        return s[:max(3, int(len(s) * rng.uniform(0.6, 0.9)))]
    return s[:i] + s[i] + s[i:]


rng_noise = np.random.default_rng(SEED)
X_noisy = np.array([perturb(s, rng_noise) for s in X], dtype=object)

# ----------------------- CV fuera de muestra -----------------------
oof = np.zeros((n, K))
stress_pred = np.zeros(n, dtype=int)
sgkf = StratifiedGroupKFold(n_splits=N_SPLITS, shuffle=True, random_state=SEED)
for fold, (tr, va) in enumerate(sgkf.split(X, y, groups)):
    tf = time.time()
    pipe = build_pipeline().fit(X[tr], y[tr])
    if not np.array_equal(pipe.classes_, np.arange(K)):
        raise RuntimeError("El fold %d no vio todas las clases en train." % fold)
    oof[va] = pipe.predict_proba(X[va])
    stress_pred[va] = pipe.predict(X_noisy[va])
    print("Fold %d/%d: %d train, %d valid, F1 macro %.3f (%.0fs)" % (
        fold + 1, N_SPLITS, len(tr), len(va),
        f1_score(y[va], oof[va].argmax(1), average="macro"), time.time() - tf))

oof_pred = oof.argmax(1)


# ----------------------- calibración (temperatura) -----------------------
def ece_top_label(p, yt, bins=15):
    conf = p.max(1)
    correct = (p.argmax(1) == yt).astype(float)
    edges = np.linspace(0, 1, bins + 1)
    e = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi)
        if m.any():
            e += m.mean() * abs(correct[m].mean() - conf[m].mean())
    return float(e)


def fit_temperature(p, yt):
    def nll(T):
        return log_loss(yt, apply_temperature(p, T), labels=list(range(p.shape[1])))
    return float(minimize_scalar(nll, bounds=(0.3, 3.0), method="bounded").x)


T = fit_temperature(oof, y)
oof_cal = apply_temperature(oof, T)
calib = {
    "temperature": T,
    "logloss_before": float(log_loss(y, oof, labels=list(range(K)))),
    "logloss_after": float(log_loss(y, oof_cal, labels=list(range(K)))),
    "ece_before": ece_top_label(oof, y),
    "ece_after": ece_top_label(oof_cal, y),
}
print("Calibración:", calib)

# ----------------------- métricas a nivel valor -----------------------
report = classification_report(y, oof_pred, target_names=labels, output_dict=True, zero_division=0)
cm = confusion_matrix(y, oof_pred, labels=list(range(K)))
cm_df = pd.DataFrame(cm, index=["real_" + l for l in labels], columns=["pred_" + l for l in labels])
value_level = {
    "accuracy": float(accuracy_score(y, oof_pred)),
    "macro_f1": float(f1_score(y, oof_pred, average="macro")),
    "weighted_f1": float(f1_score(y, oof_pred, average="weighted")),
    "macro_f1_with_noise": float(f1_score(y, stress_pred, average="macro")),
    "per_class": {l: report[l] for l in labels},
}
print("Valor: macro-F1 %.3f | con ruido %.3f" % (value_level["macro_f1"], value_level["macro_f1_with_noise"]))

# ----------------------- simulación a nivel columna -----------------------
# Reproduce lo que hará el motor: muestrear N valores de una columna, votar y decidir.
# Se usan las probabilidades fuera de muestra, así que es una estimación honesta.
rng = np.random.default_rng(SEED + 1)
idx_by_class = {j: np.where(y == j)[0] for j in range(K)}


def sample_idx(j, size):
    pool = idx_by_class[j]
    return rng.choice(pool, size=size, replace=len(pool) < size)


sim = {}
for purity in PURITY_LEVELS:
    rows, y_col = [], []
    for j in range(K):
        for _ in range(COLS_PER_CLASS):
            if j == otro_idx:
                rows.append(sample_idx(otro_idx, COL_SAMPLE_SIZE))
            else:
                n_main = int(round(purity * COL_SAMPLE_SIZE))
                rows.append(np.concatenate([sample_idx(j, n_main),
                                            sample_idx(otro_idx, COL_SAMPLE_SIZE - n_main)]))
            y_col.append(j)
    sim[purity] = (np.vstack(rows), np.array(y_col))

top = oof_cal.argmax(1)
conf = oof_cal.max(1)


def col_predict(idx_mat, tau, theta):
    cov = np.stack([((top[idx_mat] == j) & (conf[idx_mat] >= tau)).mean(axis=1) for j in hard_idx], axis=1)
    best = cov.argmax(axis=1)
    ok = cov.max(axis=1) >= theta
    return np.where(ok, np.array(hard_idx)[best], otro_idx)


grid_rows = []
for tau in TAU_GRID:
    for theta in THETA_GRID:
        for purity, (im, yc) in sim.items():
            pred = col_predict(im, tau, theta)
            row = {
                "tau": tau, "theta": theta, "purity": purity,
                "macro_f1": float(f1_score(yc, pred, average="macro", labels=list(range(K)), zero_division=0)),
                "otro_fpr": float(np.mean(pred[yc == otro_idx] != otro_idx)),
            }
            for j in hard_idx:
                row["recall_" + labels[j]] = float(np.mean(pred[yc == j] == j))
            grid_rows.append(row)
grid = pd.DataFrame(grid_rows)

cand = grid[(grid["purity"] == PURITY_FOR_SELECTION) & (grid["otro_fpr"] <= MAX_OTRO_FPR)]
if cand.empty:
    print("AVISO: ninguna combinación cumple otro_fpr <= %.3f; se elige la de menor FPR." % MAX_OTRO_FPR)
    cand = grid[grid["purity"] == PURITY_FOR_SELECTION].sort_values(["otro_fpr", "macro_f1"], ascending=[True, False])
    best = cand.iloc[0]
else:
    best = cand.sort_values("macro_f1", ascending=False).iloc[0]
tau_star, theta_star = float(best["tau"]), float(best["theta"])
print("Umbrales elegidos: tau=%.2f theta=%.2f" % (tau_star, theta_star))

# chequeo: la versión vectorizada coincide con aggregate_column (la que usará el motor)
im, yc = sim[PURITY_FOR_SELECTION]
pred_chk = col_predict(im, tau_star, theta_star)
for r in rng.choice(len(im), size=min(40, len(im)), replace=False):
    lab, _ = aggregate_column(oof_cal[im[r]], labels, tau_star, theta_star, OTRO_LABEL)
    assert lab == labels[pred_chk[r]], "aggregate_column difiere de la simulación"

chosen_rows = grid[(grid["tau"] == tau_star) & (grid["theta"] == theta_star)]
column_level = {
    "col_sample_size": COL_SAMPLE_SIZE, "cols_per_class": COLS_PER_CLASS,
    "tau": tau_star, "theta": theta_star,
    "selection": {"purity": PURITY_FOR_SELECTION, "max_otro_fpr": MAX_OTRO_FPR},
    "by_purity": {str(r["purity"]): {k: float(v) for k, v in r.items() if k not in ("tau", "theta", "purity")}
                  for _, r in chosen_rows.iterrows()},
}

# ----------------------- modelo final -----------------------
final = build_pipeline().fit(X, y)
assert np.array_equal(final.classes_, np.arange(K))

# top features por clase
top_feat_rows = []
try:
    names = final.named_steps["features"].get_feature_names_out()
except Exception:
    names = np.array(["f%d" % i for i in range(final.named_steps["clf"].coef_.shape[1])], dtype=object)
coef = final.named_steps["clf"].coef_
for j, lab in enumerate(labels):
    for i in np.argsort(coef[j])[::-1][:20]:
        top_feat_rows.append({"class": lab, "feature": names[i], "weight": float(coef[j, i])})
top_feat = pd.DataFrame(top_feat_rows)

# errores más confiados (candidatos a etiqueta mala; útiles para cleanlab)
wrong = np.where(oof_pred != y)[0]
err = pd.DataFrame({
    "value": X[wrong],
    "true": [labels[i] for i in y[wrong]],
    "pred": [labels[i] for i in oof_pred[wrong]],
    "p_pred": oof_cal[wrong, oof_pred[wrong]],
    "p_true": oof_cal[wrong, y[wrong]],
}).sort_values("p_pred", ascending=False).head(5000)

# ----------------------- config y metadatos -----------------------
config = {
    "model_version": MODEL_VERSION,
    "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "labels": labels,                  # orden de las columnas de predict_proba
    "other_label": OTRO_LABEL,
    "temperature": T,                  # aplicar apply_temperature(proba, T)
    "tau": tau_star,
    "theta": theta_star,
    "column_sample_size": COL_SAMPLE_SIZE,
    "feature_module": "pii_content_features",
    "model_file": "content_model.joblib",
    "libs": {
        "python": platform.python_version(), "sklearn": sklearn.__version__,
        "numpy": np.__version__, "scipy": scipy.__version__,
        "pandas": pd.__version__, "joblib": joblib.__version__,
    },
    "hyperparameters": {
        "char_ngram": list(CHAR_NGRAM), "char_max_features": CHAR_MAX_FEATURES,
        "word_max_features": WORD_MAX_FEATURES, "min_df": MIN_DF, "C": C,
        "class_weight": CLASS_WEIGHT, "max_iter": MAX_ITER,
    },
}

metrics = {
    "dataset": {
        "input": INPUT_DATASET, "rows_raw": n_raw, "rows_after_dedup": n_dedup,
        "rows_used": n, "class_counts": class_counts,
        "canonical_values_in_multiple_classes": n_conflicts,
    },
    "cv": {
        "scheme": "StratifiedGroupKFold por token alfabético más largo (léxico no visto en test)",
        "n_splits": N_SPLITS, "n_groups": n_groups,
        "rows_in_split_groups_pct": float(100.0 * split_mask.mean()),
    },
    "value_level": value_level,
    "calibration": calib,
    "column_level": column_level,
    "train_seconds": round(time.time() - t0, 1),
}

# ----------------------- guardar en el managed folder -----------------------
buf = io.BytesIO()
joblib.dump(final, buf, compress=3)
put_bytes("content_model.joblib", buf.getvalue())
put_bytes("pii_content_features.py", inspect.getsource(pcf).encode("utf-8"))
put_json("config.json", config)
put_json("metrics.json", metrics)
put_df("classification_report.csv", pd.DataFrame(report).T, index=True)
put_df("confusion_matrix.csv", cm_df, index=True)
put_df("column_level_grid.csv", grid)
put_df("top_features.csv", top_feat)
put_df("oof_errors.csv", err)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    cm_norm = cm / cm.sum(axis=1, keepdims=True).clip(min=1)
    fig, ax = plt.subplots(figsize=(1.6 * K + 2, 1.4 * K + 2))
    ax.imshow(cm_norm, cmap="Blues", vmin=0, vmax=1)
    ax.set_xticks(range(K)); ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_yticks(range(K)); ax.set_yticklabels(labels)
    ax.set_xlabel("predicho"); ax.set_ylabel("real")
    for i in range(K):
        for j in range(K):
            ax.text(j, i, "%.2f\n(%d)" % (cm_norm[i, j], cm[i, j]), ha="center", va="center",
                    color="white" if cm_norm[i, j] > 0.5 else "black", fontsize=8)
    ax.set_title("Matriz de confusión (valor, fuera de muestra)")
    fig.tight_layout()
    png = io.BytesIO()
    fig.savefig(png, format="png", dpi=130)
    plt.close(fig)
    put_bytes("confusion_matrix.png", png.getvalue())
except Exception as e:
    print("No se generó el PNG de la matriz de confusión: %s" % e)

print("Listo en %.0fs. Artefactos guardados en el folder '%s'." % (time.time() - t0, OUTPUT_FOLDER))
