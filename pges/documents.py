"""Turn every evidence table into short, labelled text records with stable IDs (data/evidence/rag_documents.jsonl).

IDs are built from names, never row positions (STAT_pair_Diffuse_Glyco, GO_GO:0005515_PSMD12,
REACTOME_Diffuse_R-HSA-2299718, MSIGDB_<gene set>_<signature>, PMID_<pmid>_chunk<n>).
"""
import json
import re

import pandas as pd

from .config import DOCUMENTS, PROCESSED

CHUNK_WORDS = 250


def _normalise(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def clean(text: str) -> str:
    """Collapse whitespace and drop repeated lines (case-insensitive), keeping their order."""
    lines, seen = [], set()
    for line in text.splitlines():
        line = _normalise(line)
        if line and line.casefold() not in seen:
            seen.add(line.casefold())
            lines.append(line)
    return "\n".join(lines).strip()


def _doc(text: str, **metadata) -> dict:
    return {"text": clean(text), "metadata": metadata}


def _read(name: str) -> pd.DataFrame:
    return pd.read_csv(PROCESSED / name)


def statistical() -> list:
    docs = [_doc(f"SOURCE TYPE: STATISTICAL\nSIGNATURE: {r['signature']}\n"
                 f"Kaplan-Meier log-rank p-value: {r['p_value']:.4g}\n"
                 f"n High-risk: {r['n_high']}, n Low-risk: {r['n_low']}\n",
                 source="statistical", signature=r["signature"], id=f"STAT_survival_{r['signature']}")
            for _, r in _read("survival_results.csv").iterrows()]
    ari, jaccard, corr = (_read(n).set_index(_read(n).columns[0])
                          for n in ("ARI.csv", "Jaccard.csv", "risk_score_correlation.csv"))
    for a in ari.index:
        for b in ari.columns:
            if a < b:
                docs.append(_doc(
                    f"SOURCE TYPE: STATISTICAL\nCOMPARISON: {a} vs {b}\n"
                    f"Adjusted Rand Index: {ari.loc[a, b]:.4f}\n"
                    f"Jaccard similarity (High-risk groups): {jaccard.loc[a, b]:.4f}\n"
                    f"Pearson correlation of risk scores (r): {corr.loc[a, b]:.4f}\n",
                    source="statistical", signature_pair=f"{a}_{b}", id=f"STAT_pair_{a}_{b}"))
    return docs


def gene_ontology() -> list:
    return [_doc(f"SOURCE TYPE: GO\nSIGNATURE: {r.get('signature')}\nGENE: {r.get('gene')}\n"
                 f"GO TERM: {r.get('GO_term')} ({r.get('GO_ID')})\nASPECT: {r.get('aspect')}\n",
                 source="GO", signature=r.get("signature"), gene=r.get("gene"), id=f"GO_{r.get('GO_ID')}_{r.get('gene')}")
            for _, r in _read("go_annotations.csv").iterrows()]


def reactome() -> list:
    docs = []
    for _, r in _read("reactome_results.csv").iterrows():
        signature, pid, name = (str(r.get(k, "")).strip() for k in ("signature", "pathway_id", "pathway_name"))
        docs.append(_doc(f"SOURCE TYPE: REACTOME\nSIGNATURE: {signature}\nPATHWAY: {name} ({pid})\n"
                         f"p-value: {r.get('p_value')}, FDR: {r.get('fdr')}\n"
                         f"Genes found: {r.get('genes_found')}/{r.get('genes_total')}\n",
                         source="Reactome", signature=signature, pathway_id=pid, pathway_name=name,
                         id=f"REACTOME_{signature}_{pid}"))
    return docs


def string_network() -> list:
    docs = []
    for signature, group in _read("string_interactions.csv").groupby("signature"):
        pairs, seen = [], set()
        for r in group.itertuples():
            a, b = str(r.gene_A).strip(), str(r.gene_B).strip()
            key = tuple(sorted([a.upper(), b.upper()]))
            if key not in seen:
                seen.add(key)
                pairs.append(f"{a}-{b} ({float(r.score):.2f})")
        docs.append(_doc(f"SOURCE TYPE: STRING\nSIGNATURE: {signature}\nFUNCTIONAL ASSOCIATIONS: {'; '.join(pairs)}\n",
                         source="STRING", signature=signature, id=f"STRING_{signature}"))
    return docs


def msigdb() -> list:
    return [_doc(f"SOURCE TYPE: MSIGDB\nSIGNATURE: {r['signature']}\nGENE SET: {r['gene_set']} ({r['msigdb_collection']})\n"
                 f"OVERLAP: {r['overlap_genes']} ({r['overlap_count']}/{r['signature_size']} signature genes)\n",
                 source="MSigDB", signature=r["signature"], id=f"MSIGDB_{r['gene_set']}_{r['signature']}")
            for _, r in _read("msigdb_overlap.csv").iterrows()]


def pubmed() -> list:
    docs = []
    for _, r in _read("pubmed_articles.csv").iterrows():
        words = _normalise(clean(str(r.get("abstract") or ""))).split()
        chunks = [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)]
        for i, chunk in enumerate(chunks, 1):
            docs.append(_doc(f"SOURCE TYPE: PUBMED\nPMID: {r['pmid']}\nTITLE: {r['title']}\nYEAR: {r.get('year')}\n"
                             f"SIGNATURE: {r.get('signature')}\nGENE: {r.get('gene')}\nABSTRACT CHUNK {i}: {chunk}\n",
                             source="PubMed", signature=r.get("signature"), gene=r.get("gene"), pmid=str(r["pmid"]),
                             year=r.get("year"), id=f"PMID_{r['pmid']}_chunk{i}"))
    return docs


def build() -> None:
    lines = serialised()
    DOCUMENTS.parent.mkdir(parents=True, exist_ok=True)
    DOCUMENTS.write_text("".join(lines), encoding="utf-8")
    print(f"  {len(lines)} records")


def serialised() -> list:
    """One JSON line per record; identical texts are kept once and any remaining ID clash gets a __vN suffix."""
    docs, seen_text, id_counts, unique = [], set(), {}, []
    for part in (statistical, gene_ontology, reactome, string_network, msigdb, pubmed):
        docs += part()
    for doc in docs:
        key = _normalise(doc["text"]).casefold()
        if not doc["text"] or key in seen_text:
            continue
        seen_text.add(key)
        base = str(doc["metadata"].get("id", "document"))
        id_counts[base] = id_counts.get(base, 0) + 1
        doc["metadata"]["id"] = base if id_counts[base] == 1 else f"{base}__v{id_counts[base]}"
        unique.append(doc)
    return [json.dumps(doc, ensure_ascii=False) + "\n" for doc in unique]
