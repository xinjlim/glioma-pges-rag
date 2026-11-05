"""Biological annotations per signature gene: Gene Ontology, Reactome, STRING and MSigDB."""
import gzip
import time
from io import StringIO

import pandas as pd
import requests

from .config import GO_FILES, MSIGDB_FILES, PROCESSED, SIGNATURES_CSV
from .genes import IdentifierIndex, load_records, official_symbol

REACTOME = "https://reactome.org/AnalysisService/"
STRING = "https://version-12-0.string-db.org/api/tsv/network"  # pinned: later releases return different edges
GAF_COLUMNS = ["DB", "DB_Object_ID", "DB_Object_Symbol", "Qualifier", "GO_ID", "DB_Reference", "Evidence_Code",
               "With_From", "Aspect", "DB_Object_Name", "DB_Object_Synonym", "DB_Object_Type", "Taxon", "Date",
               "Assigned_By", "Annotation_Extension", "Gene_Product_Form_ID"]
MATCH_PRIORITY = {"stable_id": 0, "official_symbol": 1, "input_symbol": 2, "alias": 3}


def _with_retries(call, label, attempts=5):
    for i in range(attempts):
        try:
            return call()
        except requests.RequestException as exc:
            if i == attempts - 1:
                raise SystemExit(f"{label} failed after {attempts} attempts ({exc}); no output was written.")
            print(f"    retry in {2 ** i}s ({exc})")
            time.sleep(2 ** i)


def _submitted_symbols(records, genes) -> list:
    """Current official symbols (e.g. HIST1H2BK is submitted as H2BC12), without duplicates."""
    return list(dict.fromkeys(official_symbol(records, g) for g in genes))


def gene_ontology() -> None:
    """GO annotations whose GAF symbol or synonym names a signature gene (one row per distinct annotation)."""
    from goatools.obo_parser import GODag

    gaf, obo = GO_FILES
    index = IdentifierIndex(load_records())
    dag = GODag(str(obo))
    rows = []
    with gzip.open(gaf, "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("!"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < len(GAF_COLUMNS):
                continue
            row = dict(zip(GAF_COLUMNS, fields))
            candidates = ([(row["DB_Object_Symbol"], "DB_Object_Symbol")] if row["DB_Object_Symbol"] else []) + [
                (s.strip(), "DB_Object_Synonym") for s in row["DB_Object_Synonym"].split("|") if s.strip()]
            matches = []
            for identifier, field in candidates:
                gene, kind = index.match(identifier)
                if gene:
                    matches.append((MATCH_PRIORITY.get(kind, 50), gene, identifier, kind, field))
            if not matches:
                continue
            _, gene, identifier, kind, field = min(matches, key=lambda m: m[0])
            term = dag.get(row["GO_ID"])
            rows.append({"gene": gene, "matched_identifier": identifier, "match_type": kind,
                         "match_source_field": field, "GO_ID": row["GO_ID"], "GO_term": term.name if term else None,
                         "aspect": row["Aspect"], "evidence_code": row["Evidence_Code"],
                         "DB_Object_ID": row["DB_Object_ID"], "DB_Object_Symbol": row["DB_Object_Symbol"],
                         "DB_Object_Synonym": row["DB_Object_Synonym"], "Assigned_By": row["Assigned_By"]})
    annotated = pd.DataFrame(rows).drop_duplicates()
    signature_of = pd.read_csv(SIGNATURES_CSV).drop_duplicates("gene").set_index("gene")["signature"]
    annotated["signature"] = annotated["gene"].map(signature_of)
    annotated.to_csv(PROCESSED / "go_annotations.csv", index=False)
    print(f"  {len(annotated)} annotations for {annotated['gene'].nunique()} genes")


def reactome() -> None:
    """Top 50 enriched Reactome pathways per signature, sorted so re-runs give the same file."""
    records, rows = load_records(), []
    for signature, group in pd.read_csv(SIGNATURES_CSV).groupby("signature"):
        symbols = _submitted_symbols(records, group["gene"])

        def submit():
            r = requests.post(REACTOME + "identifiers/projection", data="\n".join(symbols),
                              headers={"Content-Type": "text/plain"}, params={"pageSize": 0, "page": 1}, timeout=60)
            r.raise_for_status()
            return r.json()["summary"]["token"]

        token = _with_retries(submit, f"Reactome submission for {signature}")
        time.sleep(1)

        def results():
            r = requests.get(REACTOME + f"token/{token}", params={"pageSize": 50, "page": 1}, timeout=60)
            r.raise_for_status()
            return r.json().get("pathways", [])

        for p in _with_retries(results, f"Reactome results for {signature}"):
            e = p["entities"]
            if e["found"] >= 1:
                rows.append({"signature": signature, "pathway_id": p["stId"], "pathway_name": p["name"],
                             "p_value": e["pValue"], "fdr": e["fdr"], "genes_found": e["found"],
                             "genes_total": e["total"]})
    out = pd.DataFrame(rows).sort_values(["signature", "p_value", "pathway_id"], kind="mergesort")
    out.reset_index(drop=True).to_csv(PROCESSED / "reactome_results.csv", index=False)


def string_network() -> None:
    """STRING v12.0 functional associations (default score >= 0.4) among each signature's genes."""
    records, frames = load_records(), []
    for signature, group in pd.read_csv(SIGNATURES_CSV).groupby("signature"):
        symbols = _submitted_symbols(records, group["gene"])

        def query():
            r = requests.get(STRING, params={"identifiers": "%0d".join(symbols), "species": 9606,
                                             "caller_identity": "glioma_pges_rag"}, timeout=60)
            r.raise_for_status()
            return r.text.strip()

        text = _with_retries(query, f"STRING query for {signature}")
        time.sleep(1)
        if not text or "\n" not in text:
            print(f"  {signature}: no interactions")
            continue
        edges = pd.read_csv(StringIO(text), sep="\t", dtype=str)
        edges.insert(0, "signature", signature)
        frames.append(edges[["signature", "preferredName_A", "preferredName_B", "score"]]
                      .rename(columns={"preferredName_A": "gene_A", "preferredName_B": "gene_B"}))
        print(f"  {signature}: {len(edges)} interactions")
    columns = ["signature", "gene_A", "gene_B", "score"]
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)
    out.to_csv(PROCESSED / "string_interactions.csv", index=False)


def _read_gmt(path) -> dict:
    sets = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            fields = line.rstrip("\n").split("\t")
            if len(fields) >= 3:
                sets[fields[0]] = {g.strip() for g in fields[2:] if g.strip()}
    return sets


def msigdb() -> None:
    """Hallmark and C2 gene sets that contain at least one gene of each signature (alias-aware)."""
    records = load_records()
    index = IdentifierIndex(records)
    signatures = pd.read_csv(SIGNATURES_CSV)
    rows = []
    for gmt in sorted(MSIGDB_FILES):
        gene_sets = {name: {g for g in (index.match(x)[0] for x in members) if g}
                     for name, members in _read_gmt(gmt).items()}
        for signature, group in signatures.groupby("signature"):
            genes = list(dict.fromkeys(group["gene"]))
            canonical = {index.match(official_symbol(records, g))[0] or g for g in genes}
            for name, members in gene_sets.items():
                overlap = canonical & members
                if overlap:
                    rows.append({"signature": signature, "msigdb_collection": gmt.stem, "gene_set": name,
                                 "overlap_genes": ";".join(sorted(overlap)), "overlap_count": len(overlap),
                                 "signature_size": len(genes)})
    out = pd.DataFrame(rows).sort_values(["signature", "overlap_count"], ascending=[True, False])
    out.to_csv(PROCESSED / "msigdb_overlap.csv", index=False)
    print(f"  {len(out)} overlapping gene sets")
