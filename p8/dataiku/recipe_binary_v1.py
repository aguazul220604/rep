# -*- coding: utf-8 -*-
"""
Python recipe (Dataiku): entrenamiento del modelo BINARIO de PII (por nombre de columna).

  Entrada : dataset de entrenamiento con columnas  dataset, name, pii
  Salida  : managed folder (en la raíz, únicamente la versión más reciente) con:
              model.joblib  -> {"model", "threshold", "model_type", "name_col"}
              report.html   -> reporte único autocontenido con métricas OOF (global,
                               nombres no vistos, vistos y por fold), matrices de
                               confusión, curvas ROC/PR/umbral/calibración, análisis
                               de errores (FP/FN), importancia de n-gramas, conflictos
                               de etiquetado e información de entrenamiento.

Requiere pii_utils.py en Libraries > python del proyecto.
"""
import base64
import html
import io
import json
import platform
import time

import dataiku
import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sklearn
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    log_loss,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
    roc_curve,
)
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import FunctionTransformer

from pii_utils import normalize_name, normalize_names

# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------
INPUT_DATASET = "dataset_validated_stacked"   # nombre del dataset de entrada
OUTPUT_FOLDER = "model_pii"            # nombre del managed folder de salida

NAME_COL = "name"
LABEL_COL = "pii"
GROUP_COL = "dataset"      # los folds se separan por tabla para evitar fuga

MODEL_TYPE = "logreg"      # "logreg" | "lightgbm"
N_SPLITS = 5
CALIBRATION = "isotonic"   # "isotonic" (muchos datos) | "sigmoid"
THRESHOLD = None           # None -> umbral que maximiza F1 en las predicciones OOF
DROP_CONFLICTS = False     # True: elimina nombres con etiquetas contradictorias
RANDOM_STATE = 42

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
df = df[df[LABEL_COL].notna() & df[NAME_COL].notna()].copy()
df[GROUP_COL] = df[GROUP_COL].fillna("__unknown__").astype(str)
df["name_norm"] = [normalize_name(n) for n in df[NAME_COL]]
df = df[df["name_norm"] != ""].reset_index(drop=True)

# Etiquetas contradictorias para el mismo nombre normalizado
g = df.groupby("name_norm")[LABEL_COL].agg(n_true="sum", n="count")
g["n_false"] = g["n"] - g["n_true"]
conflicts = g[(g["n_true"] > 0) & (g["n_false"] > 0)].reset_index().sort_values("n", ascending=False)
if DROP_CONFLICTS and len(conflicts):
    df = df[~df["name_norm"].isin(set(conflicts["name_norm"]))].reset_index(drop=True)

y = df[LABEL_COL].astype(int).to_numpy()
names = df[NAME_COL].astype(str).to_numpy(dtype=object)
norm = df["name_norm"].to_numpy()
groups = df[GROUP_COL].to_numpy()
print(f"Filas: {n_raw} -> {len(df)} | %PII: {y.mean():.3f} | nombres en conflicto: {len(conflicts)}")


# ----------------------------------------------------------------------------
# Modelo
# ----------------------------------------------------------------------------
def build_pipeline():
    feats = FeatureUnion([
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5),
                                 sublinear_tf=True, min_df=2)),
        ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2),
                                 sublinear_tf=True, token_pattern=r"[a-z0-9]+")),
    ])
    if MODEL_TYPE == "logreg":
        clf = LogisticRegression(C=10, max_iter=2000)
    elif MODEL_TYPE == "lightgbm":
        from lightgbm import LGBMClassifier
        clf = LGBMClassifier(n_estimators=400, learning_rate=0.05, num_leaves=63,
                             subsample=0.8, subsample_freq=1, colsample_bytree=0.5,
                             random_state=RANDOM_STATE, verbose=-1)
    else:
        raise ValueError(f"MODEL_TYPE desconocido: {MODEL_TYPE}")
    return Pipeline([
        ("norm", FunctionTransformer(normalize_names, validate=False)),
        ("tfidf", feats),
        ("clf", clf),
    ])


def fit_calibrated(X, y_, g_):
    """Pipeline + calibración con CV agrupada interna (sin fuga entre tablas)."""
    inner = list(GroupKFold(n_splits=3).split(X, y_, g_))
    model = CalibratedClassifierCV(build_pipeline(), method=CALIBRATION, cv=inner, ensemble=False)
    model.fit(X, y_)
    return model


# ----------------------------------------------------------------------------
# Validación cruzada agrupada (predicciones fuera de muestra)
# ----------------------------------------------------------------------------
oof = np.zeros(len(df))
fold_id = np.full(len(df), -1)
unseen = np.zeros(len(df), dtype=bool)   # nombre (normalizado) que no aparece en el train del fold

for k, (tr, va) in enumerate(GroupKFold(n_splits=N_SPLITS).split(names, y, groups)):
    m = fit_calibrated(names[tr], y[tr], groups[tr])
    oof[va] = m.predict_proba(names[va])[:, 1]
    fold_id[va] = k
    unseen[va] = ~pd.Series(norm[va]).isin(set(norm[tr])).to_numpy()
    print(f"fold {k}: roc_auc={roc_auc_score(y[va], oof[va]):.4f}")

# Umbral
if THRESHOLD is None:
    prec, rec, thr = precision_recall_curve(y, oof)
    f1s = 2 * prec * rec / (prec + rec + 1e-12)
    threshold = float(thr[int(np.argmax(f1s[:-1]))])
else:
    threshold = float(THRESHOLD)


def compute_metrics(y_true, p, thr):
    if len(y_true) == 0 or len(np.unique(y_true)) < 2:
        return {"n": int(len(y_true))}
    pred = (p >= thr).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, pred, labels=[0, 1]).ravel()
    return {
        "n": int(len(y_true)),
        "positive_rate": float(np.mean(y_true)),
        "roc_auc": float(roc_auc_score(y_true, p)),
        "pr_auc": float(average_precision_score(y_true, p)),
        "brier": float(brier_score_loss(y_true, p)),
        "log_loss": float(log_loss(y_true, np.clip(p, 1e-6, 1 - 1e-6))),
        "accuracy": float(accuracy_score(y_true, pred)),
        "precision": float(precision_score(y_true, pred, zero_division=0)),
        "recall": float(recall_score(y_true, pred, zero_division=0)),
        "f1": float(f1_score(y_true, pred, zero_division=0)),
        "tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp),
    }


metrics = {
    "threshold": threshold,
    "overall_oof": compute_metrics(y, oof, threshold),
    # Rendimiento en nombres que el modelo no vio en entrenamiento (estimación más honesta)
    "unseen_names_oof": compute_metrics(y[unseen], oof[unseen], threshold),
    "seen_names_oof": compute_metrics(y[~unseen], oof[~unseen], threshold),
    "share_unseen_names": float(unseen.mean()),
    "per_fold": [dict(fold=k, **compute_metrics(y[fold_id == k], oof[fold_id == k], threshold))
                 for k in range(N_SPLITS)],
}

# ----------------------------------------------------------------------------
# Gráficas (PNG en Base64 para incrustar en el reporte HTML)
# ----------------------------------------------------------------------------
def fig_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def plot_confusion(y_true, p, thr, title):
    if len(y_true) == 0:
        return None
    cm = confusion_matrix(y_true, (p >= thr).astype(int), labels=[0, 1])
    cm_norm = cm / cm.sum(axis=1, keepdims=True).clip(min=1)
    fig, ax = plt.subplots(figsize=(4.8, 4.2))
    ax.imshow(cm_norm, cmap="Blues", vmin=0, vmax=1)
    for i in range(2):
        for j in range(2):
            ax.text(j, i, f"{cm[i, j]:,}\n({cm_norm[i, j]:.1%})", ha="center", va="center",
                    color="white" if cm_norm[i, j] > 0.5 else "black", fontsize=10)
    ax.set_xticks([0, 1]); ax.set_xticklabels(["No PII (0)", "PII (1)"])
    ax.set_yticks([0, 1]); ax.set_yticklabels(["No PII (0)", "PII (1)"])
    ax.set_xlabel("Predicho"); ax.set_ylabel("Real")
    ax.set_title(f"{title}\numbral = {thr:.3f} (n = {len(y_true):,})")
    return fig_b64(fig)


def plot_roc(y_true, p, unseen_mask, thr):
    if len(np.unique(y_true)) < 2:
        return None
    fig, ax = plt.subplots(figsize=(4.8, 4.2))
    fpr, tpr, _ = roc_curve(y_true, p)
    auc_all = roc_auc_score(y_true, p)
    ax.plot(fpr, tpr, label=f"OOF Global (AUC = {auc_all:.3f})", lw=2, color="#1d4ed8")

    if unseen_mask.sum() > 0 and len(np.unique(y_true[unseen_mask])) >= 2:
        fpr_u, tpr_u, _ = roc_curve(y_true[unseen_mask], p[unseen_mask])
        auc_u = roc_auc_score(y_true[unseen_mask], p[unseen_mask])
        ax.plot(fpr_u, tpr_u, label=f"No vistos (AUC = {auc_u:.3f})", lw=1.8, linestyle="--", color="#ea580c")

    pred = (p >= thr).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, pred, labels=[0, 1]).ravel()
    fpr_pt = fp / max(fp + tn, 1)
    tpr_pt = tp / max(tp + fn, 1)
    ax.scatter([fpr_pt], [tpr_pt], color="#dc2626", zorder=5, s=45, label=f"Umbral ({thr:.3f})")

    ax.plot([0, 1], [0, 1], "--", color="gray", lw=1)
    ax.set_xlabel("False Positive Rate (FPR)")
    ax.set_ylabel("True Positive Rate (Recall)")
    ax.set_title("Curva ROC (OOF)")
    ax.grid(alpha=0.25)
    ax.legend(loc="lower right", fontsize=8.5)
    return fig_b64(fig)


def plot_pr(y_true, p, unseen_mask, thr):
    if len(np.unique(y_true)) < 2:
        return None
    fig, ax = plt.subplots(figsize=(4.8, 4.2))
    prec, rec, _ = precision_recall_curve(y_true, p)
    ap_all = average_precision_score(y_true, p)
    ax.plot(rec, prec, label=f"OOF Global (PR-AUC = {ap_all:.3f})", lw=2, color="#1d4ed8")

    if unseen_mask.sum() > 0 and len(np.unique(y_true[unseen_mask])) >= 2:
        prec_u, rec_u, _ = precision_recall_curve(y_true[unseen_mask], p[unseen_mask])
        ap_u = average_precision_score(y_true[unseen_mask], p[unseen_mask])
        ax.plot(rec_u, prec_u, label=f"No vistos (PR-AUC = {ap_u:.3f})", lw=1.8, linestyle="--", color="#ea580c")

    pred = (p >= thr).astype(int)
    p_pt = precision_score(y_true, pred, zero_division=0)
    r_pt = recall_score(y_true, pred, zero_division=0)
    ax.scatter([r_pt], [p_pt], color="#dc2626", zorder=5, s=45, label=f"Umbral ({thr:.3f})")
    ax.axhline(y_true.mean(), linestyle=":", color="gray", lw=1, label=f"Base (%PII = {y_true.mean():.1%})")

    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_title("Curva Precision-Recall (OOF)")
    ax.grid(alpha=0.25)
    ax.legend(loc="lower left", fontsize=8.5)
    return fig_b64(fig)


def plot_threshold_curves(y_true, p, thr):
    if len(np.unique(y_true)) < 2:
        return None
    prec, rec, thrs = precision_recall_curve(y_true, p)
    f1s = 2 * prec[:-1] * rec[:-1] / (prec[:-1] + rec[:-1] + 1e-12)
    fig, ax = plt.subplots(figsize=(4.8, 4.2))
    ax.plot(thrs, prec[:-1], label="Precision", color="#2563eb", lw=1.6)
    ax.plot(thrs, rec[:-1], label="Recall", color="#16a34a", lw=1.6)
    ax.plot(thrs, f1s, label="F1-score", color="#9333ea", lw=2)
    ax.axvline(thr, color="#dc2626", linestyle="--", lw=1.5, label=f"Umbral óptimo = {thr:.3f}")
    ax.set_xlabel("Umbral de probabilidad")
    ax.set_ylabel("Métrica")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1.02)
    ax.set_title("Precision, Recall y F1 vs. Umbral")
    ax.grid(alpha=0.25)
    ax.legend(loc="lower center", fontsize=8.5)
    return fig_b64(fig)


def plot_calibration(y_true, p, thr):
    if len(np.unique(y_true)) < 2:
        return None
    frac_pos, mean_pred = calibration_curve(y_true, p, n_bins=15, strategy="quantile")
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(4.8, 4.4), gridspec_kw={"height_ratios": [3, 1.2]}, sharex=True)
    ax1.plot([0, 1], [0, 1], "--", color="gray", label="Perfecta")
    ax1.plot(mean_pred, frac_pos, marker="o", color="#1d4ed8", label="Modelo (OOF)")
    ax1.axvline(thr, color="#dc2626", linestyle=":", lw=1.2, label=f"Umbral = {thr:.3f}")
    ax1.set_ylabel("Fracción real de PII")
    ax1.set_title("Curva de calibración y distribución OOF")
    ax1.grid(alpha=0.25)
    ax1.legend(loc="upper left", fontsize=8.5)

    bins = np.linspace(0, 1, 26)
    ax2.hist(p[y_true == 0], bins=bins, alpha=0.6, color="#64748b", label="No PII", log=True)
    ax2.hist(p[y_true == 1], bins=bins, alpha=0.6, color="#2563eb", label="PII", log=True)
    ax2.axvline(thr, color="#dc2626", linestyle=":", lw=1.2)
    ax2.set_xlabel("Probabilidad predicha")
    ax2.set_ylabel("Conteo (log)")
    ax2.legend(loc="upper center", fontsize=7.5, ncol=2)
    ax2.grid(alpha=0.25)
    return fig_b64(fig)


cm_overall_b64 = plot_confusion(y, oof, threshold, "Matriz de confusión (OOF Global)")
cm_unseen_b64 = plot_confusion(y[unseen], oof[unseen], threshold, "Matriz de confusión (Nombres no vistos)")
roc_b64 = plot_roc(y, oof, unseen, threshold)
pr_b64 = plot_pr(y, oof, unseen, threshold)
thr_b64 = plot_threshold_curves(y, oof, threshold)
cal_b64 = plot_calibration(y, oof, threshold)

# ----------------------------------------------------------------------------
# Modelo final con todos los datos
# ----------------------------------------------------------------------------
final = fit_calibrated(names, y, groups)

cc = final.calibrated_classifiers_[0]
base = getattr(cc, "estimator", None) or getattr(cc, "base_estimator")
feat_names = base.named_steps["tfidf"].get_feature_names_out()
clf = base.named_steps["clf"]
if MODEL_TYPE == "logreg":
    imp_all = pd.DataFrame({"feature": feat_names, "weight": clf.coef_[0]})
    imp_pos = imp_all[imp_all["weight"] > 0].sort_values("weight", ascending=False).head(150)
    imp_neg = imp_all[imp_all["weight"] < 0].sort_values("weight", ascending=True).head(150)
    imp_all["abs_weight"] = imp_all["weight"].abs()
    imp = imp_all.sort_values("abs_weight", ascending=False).head(300).drop(columns="abs_weight")
else:
    imp = pd.DataFrame({"feature": feat_names, "weight": clf.feature_importances_})
    imp["abs_weight"] = imp["weight"].abs()
    imp = imp.sort_values("abs_weight", ascending=False).head(300).drop(columns="abs_weight")
    imp_pos, imp_neg = imp.head(150), pd.DataFrame(columns=["feature", "weight"])

bundle = {
    "model": final,                 # predict_proba(array_de_nombres)[:, 1] -> prob_pii
    "threshold": threshold,
    "model_type": MODEL_TYPE,
    "name_col": NAME_COL,
}

training_info = {
    "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "input_dataset": INPUT_DATASET,
    "rows_raw": int(n_raw),
    "rows_used": int(len(df)),
    "positive_rate": float(y.mean()),
    "n_groups": int(pd.Series(groups).nunique()),
    "n_conflicting_names": int(len(conflicts)),
    "dropped_conflicts": bool(DROP_CONFLICTS),
    "config": {
        "model_type": MODEL_TYPE, "n_splits": N_SPLITS, "calibration": CALIBRATION,
        "threshold_fixed": THRESHOLD, "group_col": GROUP_COL, "random_state": RANDOM_STATE,
    },
    "versions": {"python": platform.python_version(), "sklearn": sklearn.__version__,
                 "pandas": pd.__version__, "numpy": np.__version__, "joblib": joblib.__version__},
}

oof_df = pd.DataFrame({
    "dataset": df[GROUP_COL], "name": df[NAME_COL], "name_norm": df["name_norm"],
    "pii": y, "prob_pii_oof": oof, "pred": (oof >= threshold).astype(int),
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

    # Tabla comparativa de cortes OOF
    split_rows = [
        ("OOF Global (todas las filas)", o),
        ("Nombres NO vistos en train (OOF)", u),
        ("Nombres vistos en train (OOF)", s),
    ]
    comp_html = ""
    for label_row, m_row in split_rows:
        comp_html += (
            f"<tr>"
            f"<td><strong>{html.escape(label_row)}</strong></td>"
            f"<td class='num'>{_fmt(m_row.get('n'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('positive_rate'), '.1%')}</td>"
            f"<td class='num'><strong>{_fmt(m_row.get('f1'))}</strong></td>"
            f"<td class='num'>{_fmt(m_row.get('precision'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('recall'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('roc_auc'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('pr_auc'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('accuracy'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('brier'), '.4f')}</td>"
            f"<td class='num'>{_fmt(m_row.get('log_loss'), '.4f')}</td>"
            f"<td class='num'>{_fmt(m_row.get('tp'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('fp'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('tn'))}</td>"
            f"<td class='num'>{_fmt(m_row.get('fn'))}</td>"
            f"</tr>"
        )

    # Tabla por fold
    folds_html = ""
    for f_row in metrics["per_fold"]:
        folds_html += (
            f"<tr>"
            f"<td class='num'>Fold {f_row['fold']}</td>"
            f"<td class='num'>{_fmt(f_row.get('n'))}</td>"
            f"<td class='num'>{_fmt(f_row.get('positive_rate'), '.1%')}</td>"
            f"<td class='num'><strong>{_fmt(f_row.get('f1'))}</strong></td>"
            f"<td class='num'>{_fmt(f_row.get('precision'))}</td>"
            f"<td class='num'>{_fmt(f_row.get('recall'))}</td>"
            f"<td class='num'>{_fmt(f_row.get('roc_auc'))}</td>"
            f"<td class='num'>{_fmt(f_row.get('pr_auc'))}</td>"
            f"<td class='num'>{_fmt(f_row.get('accuracy'))}</td>"
            f"<td class='num'>{_fmt(f_row.get('brier'), '.4f')}</td>"
            f"<td class='num'>{_fmt(f_row.get('log_loss'), '.4f')}</td>"
            f"<td class='num'>{_fmt(f_row.get('tp'))}</td>"
            f"<td class='num'>{_fmt(f_row.get('fp'))}</td>"
            f"<td class='num'>{_fmt(f_row.get('tn'))}</td>"
            f"<td class='num'>{_fmt(f_row.get('fn'))}</td>"
            f"</tr>"
        )

    # Top errores OOF (Falsos Positivos y Falsos Negativos)
    fp_df = oof_df[(oof_df["pii"] == 0) & (oof_df["pred"] == 1)].sort_values("prob_pii_oof", ascending=False).head(30)
    fn_df = oof_df[(oof_df["pii"] == 1) & (oof_df["pred"] == 0)].sort_values("prob_pii_oof", ascending=True).head(30)

    def _err_rows(sub_df):
        if len(sub_df) == 0:
            return "<tr><td colspan='6' class='muted'>Sin errores en este corte</td></tr>"
        rows = []
        for _, r in sub_df.iterrows():
            rows.append(
                f"<tr>"
                f"<td>{html.escape(str(r['dataset']))}</td>"
                f"<td><code>{html.escape(str(r['name']))}</code></td>"
                f"<td><code>{html.escape(str(r['name_norm']))}</code></td>"
                f"<td class='num'><strong>{r['prob_pii_oof']:.3f}</strong></td>"
                f"<td class='num'>{int(r['fold'])}</td>"
                f"<td>{'Sí' if r['unseen_name'] else 'No'}</td>"
                f"</tr>"
            )
        return "".join(rows)

    # Tablas de importancia de features
    def _imp_rows(sub_df):
        if len(sub_df) == 0:
            return "<tr><td colspan='2' class='muted'>Sin datos</td></tr>"
        return "".join(
            f"<tr><td><code>{html.escape(str(r['feature']))}</code></td>"
            f"<td class='num'>{r['weight']:+.4f}</td></tr>"
            for _, r in sub_df.iterrows()
        )

    # Tabla de conflictos
    if len(conflicts) == 0:
        conf_rows = "<tr><td colspan='5' class='muted'>No se detectaron nombres con etiquetas contradictorias</td></tr>"
    else:
        conf_rows = "".join(
            f"<tr>"
            f"<td><code>{html.escape(str(r['name_norm']))}</code></td>"
            f"<td class='num'>{int(r['n_true']):,}</td>"
            f"<td class='num'>{int(r['n_false']):,}</td>"
            f"<td class='num'><strong>{int(r['n']):,}</strong></td>"
            f"<td class='num'>{r['n_true'] / max(r['n'], 1):.1%}</td>"
            f"</tr>"
            for _, r in conflicts.head(200).iterrows()
        )

    raw_json = html.escape(json.dumps({"training_info": training_info, "metrics": metrics}, indent=2, ensure_ascii=False))

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reporte Modelo Binario PII — {html.escape(training_info['trained_at'])}</title>
<style>
  :root {{
    --bg: #f8fafc;
    --card: #ffffff;
    --border: #e2e8f0;
    --text: #0f172a;
    --muted: #64748b;
    --primary: #1d4ed8;
    --accent: #eff6ff;
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
    padding: 20px 24px; margin-bottom: 20px;
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px;
  }}
  header h1 {{ margin: 0 0 6px 0; font-size: 20px; color: var(--text); }}
  .badges {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .badge {{
    background: var(--accent); color: var(--primary); border: 1px solid #bfdbfe;
    padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
  }}
  .kpi-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 12px; margin-bottom: 20px;
  }}
  .kpi {{
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px;
  }}
  .kpi .label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}
  .kpi .val {{ font-size: 22px; font-weight: 700; margin-top: 4px; color: var(--text); }}
  .kpi .sub {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  .card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 18px 20px; margin-bottom: 20px;
  }}
  .card h2 {{ margin: 0 0 12px 0; font-size: 16px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
  .card h3 {{ margin: 0 0 8px 0; font-size: 14px; }}
  .muted {{ color: var(--muted); font-size: 12.5px; margin: 0 0 10px 0; }}
  .plots-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
    gap: 16px; margin-bottom: 20px;
  }}
  .plot-card {{ text-align: center; margin-bottom: 0; }}
  .plot-card img {{ max-width: 100%; height: auto; border-radius: 6px; }}
  .two-col {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr));
    gap: 16px;
  }}
  .table-wrap {{ overflow-x: auto; max-height: 420px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left; white-space: nowrap; }}
  th {{ background: #f1f5f9; font-weight: 600; position: sticky; top: 0; z-index: 1; }}
  tr:hover td {{ background: #f8fafc; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  code {{ background: #f1f5f9; padding: 2px 5px; border-radius: 4px; font-size: 12px; }}
  details summary {{ cursor: pointer; font-weight: 600; color: var(--primary); padding: 6px 0; }}
  pre {{
    background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 8px;
    overflow-x: auto; font-size: 12px; max-height: 480px;
  }}
</style>
</head>
<body>
<div class="container">

  <header>
    <div>
      <h1>Reporte de Entrenamiento — Modelo Binario PII (por nombre de columna)</h1>
      <p class="muted" style="margin:0;">
        Dataset: <code>{html.escape(INPUT_DATASET)}</code> &rarr; Managed Folder: <code>{html.escape(OUTPUT_FOLDER)}</code>
        &nbsp;|&nbsp; Entrenado: <strong>{html.escape(training_info['trained_at'])}</strong>
      </p>
    </div>
    <div class="badges">
      <span class="badge">Modelo: {html.escape(MODEL_TYPE)}</span>
      <span class="badge">Calibración: {html.escape(CALIBRATION)}</span>
      <span class="badge">GroupKFold: {N_SPLITS} folds</span>
      <span class="badge">Umbral óptimo: {threshold:.3f}</span>
    </div>
  </header>

  <div class="kpi-grid">
    <div class="kpi">
      <div class="label">Filas utilizadas</div>
      <div class="val">{len(df):,}</div>
      <div class="sub">de {n_raw:,} crudas ({y.mean():.1%} PII)</div>
    </div>
    <div class="kpi">
      <div class="label">F1-Score (OOF Global)</div>
      <div class="val">{_fmt(o.get('f1'))}</div>
      <div class="sub">P: {_fmt(o.get('precision'))} | R: {_fmt(o.get('recall'))}</div>
    </div>
    <div class="kpi">
      <div class="label">F1 Nombres No Vistos</div>
      <div class="val">{_fmt(u.get('f1'))}</div>
      <div class="sub">n = {_fmt(u.get('n'))} ({metrics['share_unseen_names']:.1%} del total)</div>
    </div>
    <div class="kpi">
      <div class="label">ROC AUC / PR AUC (OOF)</div>
      <div class="val">{_fmt(o.get('roc_auc'))} / {_fmt(o.get('pr_auc'))}</div>
      <div class="sub">No vistos: {_fmt(u.get('roc_auc'))} / {_fmt(u.get('pr_auc'))}</div>
    </div>
    <div class="kpi">
      <div class="label">Calibración (OOF)</div>
      <div class="val">{_fmt(o.get('brier'), '.4f')}</div>
      <div class="sub">Brier | LogLoss: {_fmt(o.get('log_loss'), '.4f')}</div>
    </div>
    <div class="kpi">
      <div class="label">Tablas / Conflictos</div>
      <div class="val">{training_info['n_groups']:,} / {len(conflicts):,}</div>
      <div class="sub">Drop conflicts: {'Sí' if DROP_CONFLICTS else 'No'}</div>
    </div>
  </div>

  <div class="card">
    <h2>1. Resumen Comparativo de Métricas (Out-Of-Fold)</h2>
    <p class="muted">
      Evaluación fuera de muestra agrupada por <code>{html.escape(GROUP_COL)}</code> con umbral de decisión
      <strong>{threshold:.3f}</strong>. El corte en <em>nombres no vistos</em> mide la capacidad de generalización
      a nombres de columna que no aparecieron en el conjunto de entrenamiento del fold.
    </p>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Subconjunto OOF</th>
            <th class="num">N</th>
            <th class="num">% PII</th>
            <th class="num">F1</th>
            <th class="num">Precision</th>
            <th class="num">Recall</th>
            <th class="num">ROC AUC</th>
            <th class="num">PR AUC</th>
            <th class="num">Accuracy</th>
            <th class="num">Brier</th>
            <th class="num">Log Loss</th>
            <th class="num">TP</th>
            <th class="num">FP</th>
            <th class="num">TN</th>
            <th class="num">FN</th>
          </tr>
        </thead>
        <tbody>{comp_html}</tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2>2. Desglose por Fold (GroupKFold = {N_SPLITS})</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="num">Fold</th>
            <th class="num">N</th>
            <th class="num">% PII</th>
            <th class="num">F1</th>
            <th class="num">Precision</th>
            <th class="num">Recall</th>
            <th class="num">ROC AUC</th>
            <th class="num">PR AUC</th>
            <th class="num">Accuracy</th>
            <th class="num">Brier</th>
            <th class="num">Log Loss</th>
            <th class="num">TP</th>
            <th class="num">FP</th>
            <th class="num">TN</th>
            <th class="num">FN</th>
          </tr>
        </thead>
        <tbody>{folds_html}</tbody>
      </table>
    </div>
  </div>

  <div class="plots-grid">
    {_img_card("Matriz de Confusión (OOF Global)", cm_overall_b64, "Todas las filas de validación fuera de muestra")}
    {_img_card("Matriz de Confusión (Nombres No Vistos)", cm_unseen_b64, "Filas cuyo nombre normalizado no existía en train del fold")}
    {_img_card("Curva ROC (OOF)", roc_b64, "Comparativa Global vs. Nombres no vistos")}
    {_img_card("Curva Precision-Recall (OOF)", pr_b64, "Comparativa Global vs. Nombres no vistos")}
    {_img_card("Métricas vs. Umbral de Decisión", thr_b64, "Selección de umbral que maximiza F1 en OOF")}
    {_img_card("Curva de Calibración y Distribución", cal_b64, f"Método de calibración: {CALIBRATION}")}
  </div>

  <div class="card">
    <h2>3. Análisis de Errores Fuera de Muestra (Top 30 FP y Top 30 FN)</h2>
    <p class="muted">
      Errores más confiados del modelo en OOF (total FP = {_fmt(o.get('fp'))}, total FN = {_fmt(o.get('fn'))}).
      Útil para detectar posibles errores de etiquetado en el dataset de entrenamiento.
    </p>
    <div class="two-col">
      <div>
        <h3>Falsos Positivos (Real = No PII, Predicho = PII)</h3>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Dataset</th><th>Nombre</th><th>Normalizado</th>
                <th class="num">Prob PII</th><th class="num">Fold</th><th>No visto</th>
              </tr>
            </thead>
            <tbody>{_err_rows(fp_df)}</tbody>
          </table>
        </div>
      </div>
      <div>
        <h3>Falsos Negativos (Real = PII, Predicho = No PII)</h3>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Dataset</th><th>Nombre</th><th>Normalizado</th>
                <th class="num">Prob PII</th><th class="num">Fold</th><th>No visto</th>
              </tr>
            </thead>
            <tbody>{_err_rows(fn_df)}</tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>4. Importancia de Características (N-gramas TF-IDF del Modelo Final)</h2>
    <div class="two-col">
      <div>
        <h3>{"Top N-gramas que impulsan hacia PII (peso > 0)" if MODEL_TYPE == "logreg" else "Top N-gramas más importantes"}</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Feature (char / word n-gram)</th><th class="num">Peso</th></tr></thead>
            <tbody>{_imp_rows(imp_pos)}</tbody>
          </table>
        </div>
      </div>
      <div>
        <h3>{"Top N-gramas que impulsan hacia No PII (peso < 0)" if MODEL_TYPE == "logreg" else "Sin desglose negativo (LightGBM)"}</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Feature (char / word n-gram)</th><th class="num">Peso</th></tr></thead>
            <tbody>{_imp_rows(imp_neg)}</tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>5. Nombres Normalizados con Etiquetas en Conflicto ({len(conflicts):,})</h2>
    <p class="muted">
      Nombres de columna normalizados que aparecen etiquetados como PII en algunas tablas y como No PII en otras.
    </p>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Nombre normalizado</th>
            <th class="num">Veces PII (True)</th>
            <th class="num">Veces No PII (False)</th>
            <th class="num">Total apariciones</th>
            <th class="num">% PII</th>
          </tr>
        </thead>
        <tbody>{conf_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2>6. Configuración, Versiones y Métricas en JSON</h2>
    <details>
      <summary>Ver JSON completo (training_info + metrics)</summary>
      <pre>{raw_json}</pre>
    </details>
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

o = metrics["overall_oof"]; u = metrics["unseen_names_oof"]
print(f"Listo ({training_info['trained_at']}) | umbral={threshold:.3f} | OOF: F1={o['f1']:.3f} P={o['precision']:.3f} "
      f"R={o['recall']:.3f} AUC={o['roc_auc']:.3f} | nombres no vistos: "
      f"F1={u.get('f1', float('nan')):.3f} (n={u['n']})")
