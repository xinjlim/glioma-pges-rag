"""PubMed abstracts on each signature gene and theme in glioma (2016-2026, top 20 per query by relevance)."""
import os
import time
from urllib.error import HTTPError, URLError

import pandas as pd
from Bio import Entrez

from .config import PROCESSED, SIGNATURES_CSV, entrez_email
from .genes import load_records

RETMAX = 20
MIN_DATE, MAX_DATE, MIN_YEAR = "2016/01/01", "2026", 2016

GLIOMA = "glioma[Title/Abstract]"
TOPICS = {
    "general": "",
    "prognosis": "prognosis[Title/Abstract]",
    "survival": '(survival[Title/Abstract] OR "overall survival"[Title/Abstract])',
    "mechanism": "(mechanism[Title/Abstract] OR pathway[Title/Abstract] OR signaling[Title/Abstract] OR "
                 "signalling[Title/Abstract])",
    "expression": '(expression[Title/Abstract] OR "differential expression"[Title/Abstract] OR '
                  "signature[Title/Abstract])",
    "proliferation": "(proliferation[Title/Abstract] OR growth[Title/Abstract])",
    "invasion": "(invasion[Title/Abstract] OR migration[Title/Abstract])",
    "immune": '(immune[Title/Abstract] OR "tumor microenvironment"[Title/Abstract] OR '
              '"tumour microenvironment"[Title/Abstract] OR immunity[Title/Abstract])',
    "therapy": '(therapy[Title/Abstract] OR treatment[Title/Abstract] OR "drug resistance"[Title/Abstract])',
}
THEME_TERMS = {
    "Pyroptosis": ["pyroptosis", "inflammation", '"cell death"'],
    "Diffuse": ['"diffuse glioma"', "invasion", "migration"],
    "Stemness": ["stemness", '"cancer stem cell"', '"glioma stem cell"'],
    "Glyco": ["glycosylation", '"glycosylation modification"'],
    "Hypoxia": ["hypoxia", "hypoxic"],
}


def gene_query(identifier: str, topic: str) -> str:
    parts = [f'("{identifier.replace(chr(34), "")}"[Title/Abstract])', GLIOMA] + ([TOPICS[topic]] if TOPICS[topic] else [])
    return " AND ".join(parts)


def theme_query(term: str) -> str:
    return f'"{term}"[Title/Abstract] AND {GLIOMA} AND prognosis[Title/Abstract]'


def search_identifiers(record: dict) -> list:
    """The official symbol, then each distinct NCBI alias."""
    official = str(record.get("official_symbol") or "").strip()
    found = [(official, "official_symbol")] if official else []
    found += [(a, "alias") for a in record["aliases"]]
    unique, seen = [], set()
    for identifier, kind in found:
        if identifier.upper() not in seen:
            seen.add(identifier.upper())
            unique.append((identifier, kind))
    return unique


def _retry(call, attempts=5):
    for i in range(attempts):
        try:
            return call()
        except (HTTPError, URLError, RuntimeError) as exc:
            if i == attempts - 1:
                raise
            print(f"    retry in {2 ** i}s ({exc})")
            time.sleep(2 ** i)


class PubMed:
    def __init__(self):
        Entrez.email = entrez_email()
        Entrez.api_key = os.environ.get("NCBI_API_KEY") or None
        self.delay = 0.12 if Entrez.api_key else 0.35
        self.cache = {}

    def _read(self, handle_fn):
        def call():
            handle = handle_fn()
            try:
                return Entrez.read(handle)
            finally:
                handle.close()
        result = _retry(call)
        time.sleep(self.delay)
        return result

    def search(self, term: str) -> list:
        record = self._read(lambda: Entrez.esearch(db="pubmed", term=term, retmax=RETMAX, sort="relevance",
                                                   datetype="pdat", mindate=MIN_DATE, maxdate=MAX_DATE))
        return [str(x) for x in record["IdList"]]

    def articles(self, pmids: list) -> list:
        new = [p for p in pmids if p not in self.cache]
        for start in range(0, len(new), 100):
            batch = new[start:start + 100]
            try:
                records = self._read(lambda: Entrez.efetch(db="pubmed", id=",".join(batch), rettype="abstract",
                                                           retmode="xml"))
            except Exception as exc:
                print(f"    fetch failed for {len(batch)} articles: {exc}")
                continue
            for article in records.get("PubmedArticle", []):
                parsed = _parse(article)
                self.cache[parsed["pmid"]] = parsed
        return [dict(self.cache[p]) for p in pmids if p in self.cache]


def _parse(article) -> dict:
    medline = article["MedlineCitation"]
    data = medline["Article"]

    def attempt(fn, default=""):
        try:
            return fn()
        except (KeyError, TypeError, IndexError, AttributeError):
            return default

    def year():
        date = data["Journal"]["JournalIssue"]["PubDate"]
        if date.get("Year"):
            return str(date["Year"])
        if date.get("MedlineDate"):
            return str(date["MedlineDate"])[:4]
        raise KeyError

    def abstract():
        parts = []
        for section in data["Abstract"]["AbstractText"]:
            label = getattr(section, "attributes", {}).get("Label")
            parts.append(f"{label}: {section}" if label else str(section))
        return " ".join(parts)

    def authors():
        names = []
        for a in data["AuthorList"]:
            name = str(a["CollectiveName"]) if a.get("CollectiveName") else " ".join(
                x for x in (str(a.get("LastName", "")), str(a.get("Initials", ""))) if x)
            if name:
                names.append(name)
        return "; ".join(names)

    ids = {}
    for article_id in attempt(lambda: article["PubmedData"]["ArticleIdList"], []):
        ids[getattr(article_id, "attributes", {}).get("IdType", "")] = str(article_id)
    return {
        "pmid": str(medline["PMID"]),
        "title": str(data.get("ArticleTitle", "")),
        "abstract": attempt(abstract) if "Abstract" in data else "",
        "year": attempt(year, None) or attempt(lambda: str(data["ArticleDate"][0].get("Year", ""))),
        "journal": str(data.get("Journal", {}).get("Title", "")),
        "authors": attempt(authors),
        "publication_types": attempt(lambda: "; ".join(str(x) for x in data["PublicationTypeList"])),
        "mesh_terms": "; ".join(dict.fromkeys(str(h["DescriptorName"]) for h in medline.get("MeshHeadingList", []))),
        "doi": ids.get("doi", ""),
        "pmc_id": ids.get("pmc", ""),
    }


def retrieve() -> None:
    """Search every gene identifier x topic, then each signature theme; keep one row per article."""
    pubmed, records = PubMed(), load_records()
    signatures = pd.read_csv(SIGNATURES_CSV)
    articles, log = [], []

    def run(signature, gene, identifier, kind, topic, term):
        try:
            pmids, status = pubmed.search(term), "OK"
        except Exception as exc:
            pmids, status = [], f"ERROR: {exc}"
        r = records.get(gene, {})
        context = {"signature": signature, "gene": gene, "official_symbol": r.get("official_symbol"),
                   "ncbi_gene_id": r.get("ncbi_gene_id"), "matched_identifier": identifier, "match_type": kind,
                   "query_type": topic, "query_term": term}
        log.append({**context, "pmid_count": len(pmids), "status": status})
        articles.extend({**a, **context, "source": "PubMed"} for a in pubmed.articles(pmids))

    for signature, group in signatures.groupby("signature"):
        for gene in group["gene"].dropna().astype(str).str.strip().unique():
            print(f"  {signature} / {gene}")
            for identifier, kind in search_identifiers(records[gene]):
                for topic in TOPICS:
                    run(signature, gene, identifier, kind, topic, gene_query(identifier, topic))
    for signature in signatures["signature"].dropna().unique():
        for term in THEME_TERMS.get(signature, [signature]):
            run(signature, None, term, "signature_term", "signature_mechanism", theme_query(term))

    df = pd.DataFrame(articles).drop_duplicates(subset=["pmid"])
    df = df[pd.to_numeric(df["year"].astype(str).str[:4], errors="coerce") >= MIN_YEAR]
    df.to_csv(PROCESSED / "pubmed_articles.csv", index=False)
    pd.DataFrame(log).to_csv(PROCESSED / "pubmed_query_log.csv", index=False)
    print(f"  {len(log)} queries, {len(df)} articles")
