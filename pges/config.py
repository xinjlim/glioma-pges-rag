"""Paths, signature names and environment settings shared by every stage."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
EVIDENCE = DATA / "evidence"
RESULTS = ROOT / "results"

SIGNATURES_CSV = DATA / "signatures.csv"
QUESTIONS_JSON = DATA / "eval_questions.json"
GENE_METADATA = PROCESSED / "gene_metadata.json"
DOCUMENTS = EVIDENCE / "rag_documents.jsonl"
INDEX_DIR = EVIDENCE / "chroma_db"
FIGURES = RESULTS / "figures"
EVALUATION = RESULTS / "evaluation"

TCGA_RAW = RAW / "TCGA_GBMLGG"
GO_RAW = RAW / "GO"
MSIGDB_RAW = RAW / "MSigDB"
GO_FILES = [GO_RAW / "HUMAN-uniprot.gaf.gz", GO_RAW / "go-basic.obo"]
MSIGDB_FILES = [MSIGDB_RAW / "h.all.v2026.1.Hs.symbols.gmt", MSIGDB_RAW / "c2.all.v2026.1.Hs.symbols.gmt"]

# Order used for the survival table and the figures.
SIGNATURE_NAMES = ["Pyroptosis", "Diffuse", "Stemness", "Glyco", "Hypoxia"]


def load_env(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from .env; variables already set in the shell take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.split(" #")[0].strip().strip("\"'"))


def entrez_email() -> str:
    email = os.environ.get("ENTREZ_EMAIL", "")
    if "@" not in email or email.endswith("example.com"):
        raise SystemExit("NCBI requires a contact email: set ENTREZ_EMAIL in .env or the environment.")
    return email
