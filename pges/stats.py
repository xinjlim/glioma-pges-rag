"""Risk scores, median-split risk groups, log-rank tests and agreement between signatures."""
import pandas as pd
from lifelines.statistics import logrank_test
from sklearn.metrics import adjusted_rand_score

from .config import PROCESSED, SIGNATURE_NAMES, SIGNATURES_CSV


def high_risk(scores: pd.Series) -> pd.Series:
    """High risk = score at or above the signature's median."""
    return scores >= scores.median()


def risk_scores() -> None:
    """Score = sum(coefficient x expression) per signature; write scores, groups and their correlation."""
    expression = pd.read_csv(PROCESSED / "expression_common.csv", index_col=0)
    signatures = pd.read_csv(SIGNATURES_CSV)
    scores = pd.DataFrame(index=expression.columns)
    for name, group in signatures.groupby("signature"):
        score = pd.Series(0.0, index=expression.columns)
        for gene, coefficient in zip(group["gene"], group["coefficient"]):
            score += expression.loc[gene] * coefficient
        scores[name] = score
    groups = scores.apply(lambda s: high_risk(s).map({True: "High", False: "Low"}))
    scores.to_csv(PROCESSED / "signature_scores.csv")
    groups.to_csv(PROCESSED / "risk_groups.csv")
    scores.corr().to_csv(PROCESSED / "risk_score_correlation.csv")


def survival_cohort(signature: str) -> pd.DataFrame:
    """Patients with a score and complete survival data, with the median split recomputed on them."""
    clinical = pd.read_csv(PROCESSED / "clinical_common.csv")
    scores = pd.read_csv(PROCESSED / "signature_scores.csv", index_col=0)
    df = clinical.merge(scores[[signature]], left_on="patient_id", right_index=True)
    df = df.dropna(subset=[signature, "OS.time", "OS"])
    df["high"] = high_risk(df[signature])
    return df


def survival_table() -> pd.DataFrame:
    rows = []
    for signature in SIGNATURE_NAMES:
        df = survival_cohort(signature)
        high, low = df[df["high"]], df[~df["high"]]
        test = logrank_test(high["OS.time"], low["OS.time"], event_observed_A=high["OS"], event_observed_B=low["OS"])
        rows.append({"signature": signature, "n_high": len(high), "n_low": len(low),
                     "p_value": test.p_value, "test_statistic": test.test_statistic})
    return pd.DataFrame(rows)


def survival() -> None:
    """Log-rank test of high- vs low-risk overall survival for each signature."""
    result = survival_table()
    result.to_csv(PROCESSED / "survival_results.csv", index=False)
    print(result.to_string(index=False))


def _pairwise(names, fn) -> pd.DataFrame:
    out = pd.DataFrame(index=names, columns=names, dtype=float)
    for a in names:
        for b in names:
            out.loc[a, b] = fn(a, b)
    return out


def agreement_matrices(groups: pd.DataFrame) -> tuple:
    """(ARI of the risk groups, Jaccard index of the high-risk groups) for every pair of signatures."""
    high = {s: groups[s] == "High" for s in groups.columns}

    def jaccard(a, b):
        union = (high[a] | high[b]).sum()
        return (high[a] & high[b]).sum() / union if union else 0.0

    return (_pairwise(groups.columns, lambda a, b: adjusted_rand_score(groups[a], groups[b])),
            _pairwise(groups.columns, jaccard))


def agreement() -> None:
    ari, jaccard = agreement_matrices(pd.read_csv(PROCESSED / "risk_groups.csv", index_col=0))
    ari.to_csv(PROCESSED / "ARI.csv")
    jaccard.to_csv(PROCESSED / "Jaccard.csv")
    print(ari.round(3).to_string())
