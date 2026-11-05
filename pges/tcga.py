"""TCGA GBMLGG data: download from the Broad GDAC Firehose and match expression with clinical records."""
import hashlib
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

from .config import PROCESSED, SIGNATURES_CSV, TCGA_RAW

GDAC_URL = "https://gdac.broadinstitute.org/runs/stddata__2016_01_28/data/GBMLGG/20160128/"
GDAC_FILES = [
    "gdac.broadinstitute.org_GBMLGG.Merge_rnaseqv2__illuminahiseq_rnaseqv2__unc_edu__Level_3__RSEM_genes_normalized"
    "__data.Level_3.2016012800.0.0.tar.gz",
    "gdac.broadinstitute.org_GBMLGG.Clinical_Pick_Tier1.Level_4.2016012800.0.0.tar.gz",
]
EXPRESSION_GLOB = "*Merge_rnaseqv2*/*.rnaseqv2*Level_3__RSEM_genes_normalized__data.data.txt"
CLINICAL_GLOB = "*Clinical_Pick_Tier1*/*.clin.merged.picked.txt"


def _fetch(url: str, dest: Path) -> None:
    urllib.request.urlretrieve(url, dest)
    head = dest.read_bytes()[:256]
    if head.lstrip().startswith((b"<", b"{", b"Error")):
        dest.unlink()
        raise RuntimeError(f"{url} returned an error page instead of data.")


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        root = dest.resolve()
        for member in tar.getmembers():
            if not (root / member.name).resolve().is_relative_to(root):
                raise RuntimeError(f"Unsafe path in {archive.name}: {member.name}")
        tar.extractall(dest)


def download() -> None:
    """Download both archives, verify their MD5 checksums and extract them into data/raw/TCGA_GBMLGG."""
    TCGA_RAW.mkdir(parents=True, exist_ok=True)
    for name in GDAC_FILES:
        if any(TCGA_RAW.glob(name.removesuffix(".tar.gz") + "*/")):
            print(f"  already extracted: {name}")
            continue
        archive, checksum = TCGA_RAW / name, TCGA_RAW / (name + ".md5")
        try:
            print(f"  downloading {name}")
            _fetch(GDAC_URL + name, archive)
            _fetch(GDAC_URL + name + ".md5", checksum)
        except (urllib.error.URLError, RuntimeError) as exc:
            raise SystemExit(f"Download failed ({exc}). Check {GDAC_URL} in a browser.")
        expected = checksum.read_text().split()[0]
        if _md5(archive) != expected:
            raise SystemExit(f"MD5 mismatch for {name}; delete it and try again.")
        _extract(archive, TCGA_RAW)
        archive.unlink()
        checksum.unlink()


def _find(pattern: str) -> Path:
    matches = sorted(TCGA_RAW.glob(pattern))
    if not matches:
        raise SystemExit(f"No file matches {pattern} in {TCGA_RAW}; run the download step first.")
    return matches[0]


def patient_id(barcode: str) -> str:
    """TCGA-02-0001-01 -> TCGA-02-0001"""
    return "-".join(str(barcode).upper().split("-")[:3])


def load_expression() -> pd.DataFrame:
    # The second header row ("gene_id / normalized_count ...") is skipped; "SYMBOL|EntrezID" keeps the symbol.
    expr = pd.read_csv(_find(EXPRESSION_GLOB), sep="\t", index_col=0, skiprows=[1])
    expr = expr[~expr.index.str.startswith("?")]
    expr.index = expr.index.str.split("|").str[0]
    expr = expr[~expr.index.duplicated(keep="first")]
    expr.columns = [patient_id(c) for c in expr.columns]
    return expr


def load_clinical() -> pd.DataFrame:
    # Clinical_Pick_Tier1 has one row per variable and one column per patient.
    clinical = pd.read_csv(_find(CLINICAL_GLOB), sep="\t", index_col=0).transpose()
    clinical.index.name = "barcode"
    clinical = clinical.reset_index()
    clinical["patient_id"] = clinical["barcode"].apply(patient_id)
    dead = clinical["vital_status"].astype(str).str.lower().isin(["1", "dead", "deceased"])
    clinical["OS"] = dead.astype(int)
    clinical["OS.time"] = pd.to_numeric(clinical["days_to_death"], errors="coerce").where(
        dead, pd.to_numeric(clinical["days_to_last_followup"], errors="coerce"))
    return clinical


def standardise() -> None:
    """Keep patients with both expression and clinical data; derive overall survival (OS, OS.time)."""
    expr, clinical = load_expression(), load_clinical()
    common = set(expr.columns) & set(clinical["patient_id"])
    if not common:
        raise SystemExit("No patient IDs are shared by the expression and clinical files.")
    genes = pd.read_csv(SIGNATURES_CSV)["gene"].unique()
    missing = [g for g in genes if g not in expr.index]
    if missing:
        raise SystemExit(f"Signature genes missing from the expression data: {missing}")
    print(f"  {len(common)} patients with expression and clinical data; all {len(genes)} signature genes present")
    # Patients with a second sample (recurrent tumour or normal tissue) keep both expression columns.
    expr[sorted(common)].to_csv(PROCESSED / "expression_common.csv")
    clinical[clinical["patient_id"].isin(common)].to_csv(PROCESSED / "clinical_common.csv", index=False)
