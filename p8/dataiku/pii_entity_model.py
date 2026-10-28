# -*- coding: utf-8 -*-
"""
Clasificador de entidades por nombre de columna.

Este módulo debe publicarse como ``pii_entity_model.py`` en Project Libraries
para que los bundles ``model_entity_v1/model.joblib`` puedan deserializarse
fuera de la recipe de entrenamiento.
"""
import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import FeatureUnion
from sklearn.preprocessing import Normalizer
from sklearn.svm import LinearSVC

ENS_WEIGHTS = (0.38, 0.22, 0.22, 0.18)
MEMORY_PRIOR_STRENGTH = 0.50
KNN_TOP_K = 15
KNN_POWER = 4.0
SVD_COMPONENTS = 128
MLP_SAMPLES_PER_CLASS = 250
RANDOM_STATE = 42


def _softmax_rows(logits):
    z = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(np.clip(z, -50.0, 50.0))
    return e / e.sum(axis=1, keepdims=True).clip(min=1e-12)


class HybridEntityClassifier(BaseEstimator, ClassifierMixin):
    """
    Ensamble Híbrido Multivista para clasificación de nombres de columna PII:
      - Vectorizador TF-IDF en 3 niveles (char_wb 2-5, char 3-5, word 1-2).
      - Rama 1: LogisticRegression L2 regularizada y balanceada.
      - Rama 2: LinearSVC de margen máximo con calibración de temperatura sobre márgenes.
      - Rama 3: TruncatedSVD + MLPClassifier (ReLU) entrenado con muestreo balanceado.
      - Rama 4: k-NN ponderado por similitud coseno (sim^p) sobre el vocabulario de train.
      - Memoria Léxica Bayesiana para nombres ya vistos en entrenamiento.
    """

    def __init__(
        self,
        ens_weights=ENS_WEIGHTS,
        memory_prior_strength=MEMORY_PRIOR_STRENGTH,
        knn_top_k=KNN_TOP_K,
        knn_power=KNN_POWER,
        svd_components=SVD_COMPONENTS,
        mlp_samples_per_class=MLP_SAMPLES_PER_CLASS,
        random_state=RANDOM_STATE,
    ):
        self.ens_weights = ens_weights
        self.memory_prior_strength = memory_prior_strength
        self.knn_top_k = knn_top_k
        self.knn_power = knn_power
        self.svd_components = svd_components
        self.mlp_samples_per_class = mlp_samples_per_class
        self.random_state = random_state

    def _build_vectorizer(self):
        return FeatureUnion([
            ("char_wb", TfidfVectorizer(
                analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True, min_df=2
            )),
            ("char", TfidfVectorizer(
                analyzer="char", ngram_range=(2, 5), sublinear_tf=True, min_df=2
            )),
            ("word", TfidfVectorizer(
                analyzer="word", ngram_range=(1, 2), sublinear_tf=True, token_pattern=r"[a-z0-9]+"
            )),
        ])

    def fit(self, X, y, sample_weight=None, exact_memory=None):
        X_arr = np.asarray(X, dtype=object)
        y_arr = np.asarray(y)
        self.classes_ = np.unique(y_arr)
        n_classes = len(self.classes_)
        y_idx_tr = np.searchsorted(self.classes_, y_arr)

        if sample_weight is None:
            w_tr = np.ones(len(y_arr), dtype=float)
        else:
            w_tr = np.asarray(sample_weight, dtype=float)

        self.exact_memory_ = {}
        if exact_memory is not None:
            for nm, dist in exact_memory.items():
                vec = np.zeros(n_classes, dtype=float)
                for lb, cnt in dist.items():
                    ci = int(np.searchsorted(self.classes_, lb))
                    if 0 <= ci < n_classes and self.classes_[ci] == lb:
                        vec[ci] += float(cnt)
                if vec.sum() > 0:
                    self.exact_memory_[nm] = vec
        else:
            for nm, ci, w in zip(X_arr, y_idx_tr, w_tr):
                if w >= 1.0 and nm:
                    vec = self.exact_memory_.get(nm)
                    if vec is None:
                        vec = np.zeros(n_classes, dtype=float)
                        self.exact_memory_[nm] = vec
                    vec[ci] += float(w)

        self.tfidf_ = self._build_vectorizer()
        X_vec = self.tfidf_.fit_transform(X_arr)

        class_totals = np.bincount(y_idx_tr, weights=w_tr, minlength=n_classes).clip(min=1e-6)
        mean_total = float(class_totals.mean())
        class_factors = (mean_total / class_totals) ** 0.85
        w_balanced = w_tr * class_factors[y_idx_tr]

        self.clf_lr_ = LogisticRegression(
            C=2.5, max_iter=3000, random_state=self.random_state
        )
        self.clf_lr_.fit(X_vec, y_idx_tr, sample_weight=w_balanced)
        self.coef_ = self.clf_lr_.coef_

        self.clf_svm_ = LinearSVC(
            C=0.6, max_iter=4000, random_state=self.random_state
        )
        self.clf_svm_.fit(X_vec, y_idx_tr, sample_weight=w_balanced)
        svm_margins = self.clf_svm_.decision_function(X_vec)
        if svm_margins.ndim == 1:
            svm_margins = np.column_stack([-svm_margins, svm_margins])

        def _svm_loss(scale):
            P = _softmax_rows(svm_margins * scale)
            p_true = np.clip(P[np.arange(len(y_idx_tr)), y_idx_tr], 1e-9, 1.0)
            return - float(np.average(np.log(p_true), weights=w_balanced))

        scales = np.linspace(0.8, 5.0, 18)
        self.svm_scale_ = float(min(scales, key=_svm_loss))

        max_svd = min(self.svd_components, X_vec.shape[1] - 1, X_vec.shape[0] - 1)
        if max_svd >= 16:
            self.svd_ = TruncatedSVD(n_components=max_svd, random_state=self.random_state)
            self.svd_norm_ = Normalizer(copy=False)
            X_svd = self.svd_norm_.fit_transform(self.svd_.fit_transform(X_vec))

            rng = np.random.default_rng(self.random_state)
            sampled_indices = []
            for ci in range(n_classes):
                idx_c = np.where(y_idx_tr == ci)[0]
                if len(idx_c) == 0:
                    continue
                p_c = w_tr[idx_c] / w_tr[idx_c].sum()
                n_draw = int(max(len(idx_c), self.mlp_samples_per_class))
                replace = len(idx_c) < n_draw
                drawn = rng.choice(idx_c, size=n_draw, replace=replace, p=p_c)
                sampled_indices.append(drawn)
            mlp_idx = np.concatenate(sampled_indices)
            rng.shuffle(mlp_idx)

            self.clf_mlp_ = MLPClassifier(
                hidden_layer_sizes=(128, 64),
                activation="relu",
                alpha=1e-2,
                batch_size=min(128, max(32, len(mlp_idx) // 20)),
                learning_rate_init=1.5e-3,
                max_iter=200,
                early_stopping=True,
                validation_fraction=0.1,
                n_iter_no_change=12,
                random_state=self.random_state,
            )
            self.clf_mlp_.fit(X_svd[mlp_idx], y_idx_tr[mlp_idx])
        else:
            self.svd_ = None
            self.clf_mlp_ = None

        self.knn_norm_ = Normalizer(copy=True)
        self.X_knn_ = self.knn_norm_.fit_transform(X_vec)
        self.y_knn_ = y_idx_tr
        knn_class_factors = (mean_total / class_totals) ** 0.50
        self.w_knn_ = w_tr * knn_class_factors[y_idx_tr]

        return self

    def _predict_knn_proba(self, X_vec):
        n_samples = X_vec.shape[0]
        n_classes = len(self.classes_)
        Q = self.knn_norm_.transform(X_vec)
        out = np.zeros((n_samples, n_classes), dtype=float)
        batch_size = 2048
        k = min(self.knn_top_k, self.X_knn_.shape[0])

        for start in range(0, n_samples, batch_size):
            end = min(start + batch_size, n_samples)
            sim = (Q[start:end] @ self.X_knn_.T).toarray()
            if k < sim.shape[1]:
                top_idx = np.argpartition(-sim, kth=k - 1, axis=1)[:, :k]
                rows = np.arange(end - start)[:, None]
                top_sim = np.clip(sim[rows, top_idx], 0.0, 1.0)
                top_y = self.y_knn_[top_idx]
                top_w = self.w_knn_[top_idx]
            else:
                top_sim = np.clip(sim, 0.0, 1.0)
                top_y = np.broadcast_to(self.y_knn_, top_sim.shape)
                top_w = np.broadcast_to(self.w_knn_, top_sim.shape)

            weights = (top_sim ** self.knn_power) * top_w
            for r in range(end - start):
                np.add.at(out[start + r], top_y[r], weights[r])

        row_sums = out.sum(axis=1, keepdims=True)
        uniform = np.full((n_samples, n_classes), 1.0 / n_classes, dtype=float)
        return np.where(row_sums > 1e-9, out / np.clip(row_sums, 1e-9, None), uniform)

    def predict_proba(self, X):
        X_arr = np.asarray(X, dtype=str)
        if len(X_arr) == 0:
            return np.zeros((0, len(self.classes_)), dtype=float)

        uniq_X, inv_idx = np.unique(X_arr, return_inverse=True)
        X_vec = self.tfidf_.transform(uniq_X)
        n_classes = len(self.classes_)

        w_lr, w_svm, w_mlp, w_knn = self.ens_weights

        p_lr = self.clf_lr_.predict_proba(X_vec)

        svm_margins = self.clf_svm_.decision_function(X_vec)
        if svm_margins.ndim == 1:
            svm_margins = np.column_stack([-svm_margins, svm_margins])
        p_svm = _softmax_rows(svm_margins * self.svm_scale_)

        if self.clf_mlp_ is not None and self.svd_ is not None:
            X_svd = self.svd_norm_.transform(self.svd_.transform(X_vec))
            p_mlp_raw = self.clf_mlp_.predict_proba(X_svd)
            if p_mlp_raw.shape[1] == n_classes:
                p_mlp = p_mlp_raw
            else:
                p_mlp = np.zeros((len(uniq_X), n_classes), dtype=float)
                p_mlp[:, self.clf_mlp_.classes_] = p_mlp_raw
        else:
            p_mlp = p_lr

        p_knn = self._predict_knn_proba(X_vec)

        p_ens = (w_lr * p_lr) + (w_svm * p_svm) + (w_mlp * p_mlp) + (w_knn * p_knn)
        p_ens = p_ens / p_ens.sum(axis=1, keepdims=True).clip(min=1e-12)

        if self.exact_memory_:
            kappa = float(self.memory_prior_strength)
            for i, nm in enumerate(uniq_X):
                mem_counts = self.exact_memory_.get(nm)
                if mem_counts is not None:
                    n_obs = float(mem_counts.sum())
                    p_mem = mem_counts / n_obs
                    lam = n_obs / (n_obs + kappa)
                    p_ens[i] = (lam * p_mem) + ((1.0 - lam) * p_ens[i])

        return p_ens[inv_idx]

    def predict(self, X):
        P = self.predict_proba(X)
        return self.classes_[P.argmax(axis=1)]


__all__ = [
    "HybridEntityClassifier",
    "ENS_WEIGHTS",
    "MEMORY_PRIOR_STRENGTH",
    "KNN_TOP_K",
    "KNN_POWER",
    "SVD_COMPONENTS",
    "MLP_SAMPLES_PER_CLASS",
    "RANDOM_STATE",
]
