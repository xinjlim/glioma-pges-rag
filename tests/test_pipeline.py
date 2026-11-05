"""Integrity, retrieval and (optionally) generation tests.

    python -m unittest discover -s tests -v          # offline
    RUN_LLM_TESTS=1 python -m unittest discover -s tests -v   # also generates answers (needs an API key)
"""
import os
import re
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pges import documents, rag, stats  # noqa: E402
from pges.config import DOCUMENTS, PROCESSED, SIGNATURES_CSV, load_env  # noqa: E402

load_env()


class SavedOutputs(unittest.TestCase):
    """The saved tables and evidence file are exactly what the code computes from their inputs."""

    def test_agreement_matches_saved_tables(self):
        ari, jaccard = stats.agreement_matrices(pd.read_csv(PROCESSED / "risk_groups.csv", index_col=0))
        pd.testing.assert_frame_equal(ari, pd.read_csv(PROCESSED / "ARI.csv", index_col=0), check_names=False)
        pd.testing.assert_frame_equal(jaccard, pd.read_csv(PROCESSED / "Jaccard.csv", index_col=0), check_names=False)

    def test_survival_matches_saved_table(self):
        pd.testing.assert_frame_equal(stats.survival_table(), pd.read_csv(PROCESSED / "survival_results.csv"))

    def test_evidence_file_rebuilds_identically(self):
        self.assertEqual("".join(documents.serialised()), DOCUMENTS.read_text(encoding="utf-8"))


class DerivedEvidence(unittest.TestCase):
    def setUp(self):
        self.docs = rag.load_documents()
        self.derived = rag.build_derived_docs(self.docs)

    def test_genes_match_signature_definitions(self):
        definitions = pd.read_csv(SIGNATURES_CSV).groupby("signature")["gene"].apply(set).to_dict()
        genes = [d for d in self.derived if d["id"].startswith("DERIVED_genes_")]
        self.assertEqual(len(genes), 5)
        for d in genes:
            listed = re.search(r"corpus \(\d+\): ([^\n(]+)", d["text"]).group(1)
            counts = [int(x) for x in re.findall(r"(?:MSigDB gene sets|Reactome pathways|GO terms)=(\d+)", d["text"])]
            self.assertEqual({g.strip() for g in listed.split(",")}, definitions[d["metadata"]["signature"]])
            self.assertTrue(len(counts) == 3 and all(counts), d["id"])

    def test_pair_records(self):
        self.assertEqual(sum(d["id"].startswith("DERIVED_overlap_") for d in self.derived), 10)
        pairs = [d for d in self.docs if d["id"].startswith("STAT_pair_")]
        self.assertEqual(len(pairs), 10)
        for d in pairs:
            for field in ("Adjusted Rand Index", "Jaccard similarity", "Pearson correlation"):
                self.assertIn(field, d["text"])


class Retrieval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.col, cls.emb = rag.open_index()

    def test_index_is_current_and_complete(self):
        current, reason = rag.index_status()
        self.assertTrue(current, reason)
        docs = rag.load_documents()
        self.assertEqual(self.col.count(), len(docs) + len(rag.build_derived_docs(docs)))

    def test_pair_question(self):
        info, ev = rag.retrieve("Why do Pyroptosis and Hypoxia signatures have different prognostic results in glioma?",
                                self.col, self.emb)
        ids = {e["id"] for e in ev}
        self.assertEqual((info["intent"], info["focus"]), ("compare", ["Hypoxia", "Pyroptosis"]))
        self.assertLessEqual({"STAT_pair_Hypoxia_Pyroptosis", "STAT_survival_Hypoxia", "STAT_survival_Pyroptosis",
                              "DERIVED_overlap_Hypoxia_Pyroptosis"}, ids)
        self.assertTrue(all(e["metadata"]["signature"] in info["focus"] for e in ev if "signature" in e["metadata"]))
        self.assertLessEqual({"MSigDB", "Reactome", "GO", "PubMed", "STRING"}, {e["metadata"]["source"] for e in ev})

    def test_all_signatures_question(self):
        info, ev = rag.retrieve("Why do different glioma signatures show inconsistent prognostic outcomes?",
                                self.col, self.emb)
        ids = [e["id"] for e in ev]
        self.assertEqual((info["intent"], info["focus"]), ("compare", rag.SIGNATURES))
        self.assertEqual(sum(i.startswith("STAT_pair_") for i in ids), 10)
        self.assertEqual(sum(i.startswith("STAT_survival_") for i in ids), 5)

    def test_routing(self):
        cases = {"Tell me about the Hypoxia signature.": "single",
                 "Which genes do the Diffuse and Stemness signatures share?": "gene",
                 "Which pathways are enriched in the Pyroptosis signature?": "pathway",
                 "What do the log-rank p-values show about each signature's prognostic performance?": "stats",
                 "What signatures are included in the database?": "catalog",
                 "How is diffuse glioma treated in children?": "open"}
        for query, intent in cases.items():
            self.assertEqual(rag.route(query, self.col)["intent"], intent, query)

    def test_checks(self):
        evidence = [{"id": "STAT_pair_Diffuse_Glyco", "text": "Adjusted Rand Index: 0.4978"}]
        answer = "ARI was 0.50 [STAT_pair_Diffuse_Glyco] and 0.73 [STAT_pair_Glyco_Hypoxia]."
        self.assertEqual(rag.check_citations(answer, evidence),
                         {"cited": ["STAT_pair_Diffuse_Glyco"], "unknown_ids": ["STAT_pair_Glyco_Hypoxia"]})
        self.assertEqual(rag.unsupported_numbers(answer, evidence[0]["text"]), ["0.73"])


@unittest.skipUnless(os.environ.get("RUN_LLM_TESTS"), "set RUN_LLM_TESTS=1 (needs an API key)")
class Generation(unittest.TestCase):
    def test_answer_is_grounded(self):
        col, emb = rag.open_index()
        r = rag.answer("Why do Glyco and Stemness assign patients to similar risk groups?", col, emb)
        text = r["answer"]
        self.assertGreaterEqual(len(r["citations"]["cited"]), 3)
        self.assertEqual(r["citations"]["unknown_ids"], [])
        self.assertEqual(rag.unsupported_numbers(text, "\n".join(e["text"] for e in r["evidence"])), [])
        first = re.search(r"\[(STAT_[^\]]+)\]", text)
        self.assertTrue(first and first.start() < len(text) / 3, "statistics should come first")
        self.assertNotIn("$", text)


if __name__ == "__main__":
    unittest.main()
