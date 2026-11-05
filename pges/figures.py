"""Report figures: Kaplan-Meier curves, ARI / Jaccard / Pearson heatmaps and score correlation vs ARI per pair.

Every plotted log-rank p-value, group size, ARI, Jaccard index and Pearson r is checked against the
STAT_* evidence records first, so the figures and the LLM's evidence cannot disagree.
"""
import json
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from lifelines import KaplanMeierFitter  # noqa: E402
from lifelines.statistics import logrank_test  # noqa: E402

from .config import DOCUMENTS, FIGURES, PROCESSED, SIGNATURE_NAMES  # noqa: E402

HIGH, LOW = "#d62728", "#1f77b4"
R_COLOR, ARI_COLOR = "#2a78d6", "#eb6834"


def _matrix(name: str) -> pd.DataFrame:
    return pd.read_csv(PROCESSED / name, index_col=0).loc[SIGNATURE_NAMES, SIGNATURE_NAMES]


def _evidence_stats():
    """{signature: (p, n_high, n_low)} and {(a, b): (ari, jaccard, r)} parsed from the STAT_* records."""
    survival, pairs = {}, {}
    for line in DOCUMENTS.read_text(encoding="utf-8").split("\n"):
        if not line:
            continue
        record = json.loads(line)
        meta, text = record["metadata"], record["text"]
        if meta.get("source") != "statistical":
            continue
        if "signature" in meta:
            n = re.search(r"n High-risk: (\d+), n Low-risk: (\d+)", text)
            survival[meta["signature"]] = (float(re.search(r"p-value: (\S+)", text).group(1)),
                                           int(n.group(1)), int(n.group(2)))
        else:
            a, b = meta["signature_pair"].split("_")
            pairs[(a, b)] = tuple(float(re.search(p, text).group(1)) for p in (
                r"Rand Index: (\S+)", r"Jaccard similarity \(High-risk groups\): (\S+)",
                r"Pearson correlation of risk scores \(r\): (\S+)"))
    return survival, pairs


def _p_label(p: float) -> str:
    if p < 1e-4:
        mantissa, exponent = f"{p:.1e}".split("e")
        return rf"$p = {mantissa} \times 10^{{{int(exponent)}}}$"
    return f"p = {p:.4f}"


def heatmap(m: pd.DataFrame, title: str, label: str, path) -> None:
    fig, ax = plt.subplots(figsize=(7.2, 5.8))
    signed = m.values.min() < 0
    im = ax.imshow(m.values, cmap="RdBu_r" if signed else "Blues", vmin=-1 if signed else 0, vmax=1)
    names = [s.lower() for s in m.columns]
    ax.set_xticks(range(len(names)), names, rotation=45, ha="right")
    ax.set_yticks(range(len(names)), names)
    ax.set_xticks(np.arange(-.5, len(names)), minor=True)
    ax.set_yticks(np.arange(-.5, len(names)), minor=True)
    ax.grid(which="minor", color="white", linewidth=1.5)
    ax.tick_params(which="minor", length=0)
    for i in range(len(names)):
        for j in range(len(names)):
            v = m.values[i, j]
            ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=11, color="white" if v > 0.45 else "#1a1a1a")
    ax.set_xlabel("Gene expression signature")
    ax.set_ylabel("Gene expression signature")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label=label)
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


def correlation_vs_ari(corr: pd.DataFrame, ari: pd.DataFrame, path) -> None:
    pairs = sorted((("–".join(sorted((a, b))), corr.loc[a, b], ari.loc[a, b])
                    for i, a in enumerate(SIGNATURE_NAMES) for b in SIGNATURE_NAMES[i + 1:]), key=lambda t: t[1])
    fig, ax = plt.subplots(figsize=(8, 5.6))
    y = np.arange(len(pairs))
    for yi, (_, r, a) in zip(y, pairs):
        ax.plot([a, r], [yi, yi], color="0.78", linewidth=2, zorder=1)
    ax.scatter([p[1] for p in pairs], y, s=70, color=R_COLOR, marker="o", zorder=3, edgecolor="white", linewidth=1.5,
               label="Pearson r of risk scores")
    ax.scatter([p[2] for p in pairs], y, s=70, color=ARI_COLOR, marker="D", zorder=3, edgecolor="white", linewidth=1.5,
               label="ARI of high/low risk groups")
    for yi, (_, r, a) in zip(y, pairs):
        ax.text(r + 0.02, yi, f"{r:.2f}", va="center", ha="left", fontsize=9, color="#0b0b0b")
        ax.text(a - 0.02, yi, f"{a:.2f}", va="center", ha="right", fontsize=9, color="#52514e")
    ax.set_yticks(y, [p[0] for p in pairs])
    ax.set_xlim(0, 1)
    ax.set_xlabel("Value (0–1)")
    ax.xaxis.grid(True, color="0.9", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_title("Risk scores correlate more strongly than the risk groups agree", loc="left")
    ax.legend(loc="lower right", frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


def kaplan_meier(df: pd.DataFrame, path) -> pd.DataFrame:
    fig, axes = plt.subplots(2, 3, figsize=(16, 9.5))
    t_max = df["OS.time"].max() * 1.03
    rows = []
    for ax, sig in zip(axes.flat, SIGNATURE_NAMES):
        high_mask = df[sig] >= df[sig].median()
        hi, lo = df[high_mask], df[~high_mask]
        for sub, label, color in ((hi, "High", HIGH), (lo, "Low", LOW)):
            KaplanMeierFitter(label=f"{label} risk (n={len(sub)})").fit(sub["OS.time"], sub["OS"]) \
                .plot_survival_function(ax=ax, color=color, ci_alpha=0.2, show_censors=True,
                                        censor_styles={"ms": 4, "marker": "|"})
        res = logrank_test(hi["OS.time"], lo["OS.time"], event_observed_A=hi["OS"], event_observed_B=lo["OS"])
        rows.append({"signature": sig, "n_high": len(hi), "n_low": len(lo), "events_high": int(hi["OS"].sum()),
                     "events_low": int(lo["OS"].sum()), "logrank_chi2": res.test_statistic, "p_value": res.p_value})
        ax.text(0.97, 0.95, f"Log-rank {_p_label(res.p_value)}", transform=ax.transAxes, ha="right", va="top",
                bbox=dict(boxstyle="round", fc="white", ec="0.6"))
        ax.set_title(f"{sig} signature", fontsize=13)
        ax.set_xlabel("Time (days)")
        ax.set_ylabel("Overall survival probability")
        ax.set_xlim(-t_max * 0.02, t_max)
        ax.set_ylim(0, 1.03)
        ax.legend(loc="lower left")
    axes.flat[-1].axis("off")
    fig.suptitle(f"Kaplan–Meier overall survival by risk group (TCGA GBMLGG, n = {len(df)})", fontsize=15)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path, dpi=300)
    plt.close(fig)
    return pd.DataFrame(rows).set_index("signature")


def draw() -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    clinical = pd.read_csv(PROCESSED / "clinical_common.csv")
    scores = pd.read_csv(PROCESSED / "signature_scores.csv", index_col=0)
    cohort = clinical.merge(scores[SIGNATURE_NAMES], left_on="patient_id", right_index=True)
    cohort = cohort.dropna(subset=SIGNATURE_NAMES + ["OS.time", "OS"])
    ari, jac, corr = _matrix("ARI.csv"), _matrix("Jaccard.csv"), _matrix("risk_score_correlation.csv")

    heatmap(ari, "Adjusted Rand index between risk-group assignments", "ARI", FIGURES / "ari_heatmap.png")
    heatmap(jac, "Jaccard similarity of high-risk groups", "Jaccard index", FIGURES / "jaccard_heatmap.png")
    heatmap(corr, "Pearson correlation of risk scores", "Pearson r", FIGURES / "pearson_heatmap.png")
    correlation_vs_ari(corr, ari, FIGURES / "correlation_vs_ari.png")
    surv = kaplan_meier(cohort, FIGURES / "km_curves.png")

    problems = []
    ev_survival, ev_pairs = _evidence_stats()
    for sig, (p, n_high, n_low) in ev_survival.items():
        row = surv.loc[sig]
        if f"{row.p_value:.4g}" != f"{p:.4g}" or (row.n_high, row.n_low) != (n_high, n_low):
            problems.append(f"{sig}: figure p={row.p_value:.4g} n={row.n_high}/{row.n_low}, evidence p={p:.4g}")
    for (a, b), values in ev_pairs.items():
        if tuple(round(m.loc[a, b], 4) for m in (ari, jac, corr)) != values:
            problems.append(f"{a}-{b}: figure values differ from the evidence {values}")
    if problems:
        raise SystemExit("Figures disagree with rag_documents.jsonl:\n  " + "\n  ".join(problems))
    print(f"  {len(cohort)} patients; all plotted values match the evidence records")
