"""Gene identifiers: NCBI official symbols and aliases, and matching of database symbols to signature genes."""
import json
import os
import time

import pandas as pd
import requests

from .config import GENE_METADATA, SIGNATURES_CSV, entrez_email

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


def fetch_metadata() -> None:
    """Official symbol, Entrez ID, description and aliases for every signature gene (NCBI Gene)."""
    email, api_key = entrez_email(), os.environ.get("NCBI_API_KEY", "")

    def get(endpoint, **params):
        params.update(email=email, retmode="json", **({"api_key": api_key} if api_key else {}))
        response = requests.get(EUTILS + endpoint, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    records = []
    for gene in sorted(pd.read_csv(SIGNATURES_CSV)["gene"].unique()):
        try:
            ids = get("esearch.fcgi", db="gene", term=f"{gene}[sym] AND human[orgn]", retmax=1)["esearchresult"]["idlist"]
            if not ids:
                records.append({"gene": gene, "found": False})
            else:
                doc = get("esummary.fcgi", db="gene", id=ids[0])["result"][ids[0]]
                records.append({"gene": gene, "found": True, "ncbi_gene_id": ids[0],
                                "official_symbol": doc.get("name"), "description": doc.get("description"),
                                "aliases": doc.get("otheraliases"), "summary": doc.get("summary")})
        except requests.RequestException as exc:
            raise SystemExit(f"NCBI request for {gene} failed ({exc}); no output was written.")
        time.sleep(0.11 if api_key else 0.4)
    GENE_METADATA.write_text(json.dumps(records, indent=2))


def load_records() -> dict:
    """Signature gene -> {official_symbol, ncbi_gene_id, aliases (list)}."""
    records = {}
    for r in json.loads(GENE_METADATA.read_text(encoding="utf-8")):
        aliases = r.get("aliases") or ""
        records[r["gene"]] = {
            "official_symbol": r.get("official_symbol"),
            "ncbi_gene_id": str(r["ncbi_gene_id"]) if r.get("ncbi_gene_id") is not None else None,
            "aliases": [a.strip() for a in aliases.split(",") if a.strip()],
        }
    return records


def official_symbol(records: dict, gene: str) -> str:
    return records[gene]["official_symbol"] or gene


class IdentifierIndex:
    """Maps any symbol, alias or NCBI ID (case-insensitive) to the signature gene(s) it names."""

    def __init__(self, records: dict):
        self._index = {}
        for gene, r in records.items():
            for identifier, kind in [(gene, "input_symbol"), (r["official_symbol"], "official_symbol"),
                                     (r["ncbi_gene_id"], "ncbi_gene_id")] + [(a, "alias") for a in r["aliases"]]:
                key = str(identifier or "").strip().upper()
                if key:
                    entry = self._index.setdefault(key, {"genes": set(), "types": set()})
                    entry["genes"].add(gene)
                    entry["types"].add(kind)

    def match(self, identifier) -> tuple:
        """(gene, match_type); gene is None when the identifier is unknown or names several genes."""
        entry = self._index.get(str(identifier or "").strip().upper())
        if entry is None:
            return None, "not_found"
        if len(entry["genes"]) != 1:
            return None, "ambiguous"
        kind = sorted(entry["types"])[0]
        return next(iter(entry["genes"])), "stable_id" if kind == "ncbi_gene_id" else kind
