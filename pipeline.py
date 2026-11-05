"""Glioma PGES discordance pipeline: evidence building, retrieval-augmented chat and evaluation.

    python pipeline.py check                       what is installed, configured and built
    python pipeline.py data [--from STEP] [--to STEP]   rebuild the evidence (steps listed below)
    python pipeline.py index [--force]             rebuild the vector index if the evidence changed
    python pipeline.py figures                     report figures -> results/figures/
    python pipeline.py test [--llm]                offline integrity and retrieval tests (--llm: also Gemini)
    python pipeline.py chat [--terminal]           web chat at http://127.0.0.1:5000, or a terminal chat
    python pipeline.py evaluate [OPTIONS]          judge answers with Qwen (python pipeline.py evaluate -h)

Settings are read from the environment or a .env file (see .env.example).
"""
import argparse
import importlib.util
import os
import subprocess
import sys
import time

from pges.config import GO_FILES, MSIGDB_FILES, ROOT, TCGA_RAW, load_env

STEPS = [  # (name, module, function, description); order respects dependencies
    ("download", "tcga", "download", "TCGA GBMLGG expression and clinical data (Broad GDAC Firehose)"),
    ("standardise", "tcga", "standardise", "match patients and derive overall survival"),
    ("scores", "stats", "risk_scores", "risk scores, median-split groups, score correlation"),
    ("survival", "stats", "survival", "log-rank tests"),
    ("agreement", "stats", "agreement", "adjusted Rand index and Jaccard index between signatures"),
    ("genes", "genes", "fetch_metadata", "NCBI official symbols and aliases"),
    ("go", "annotations", "gene_ontology", "Gene Ontology annotations"),
    ("reactome", "annotations", "reactome", "Reactome pathway enrichment"),
    ("string", "annotations", "string_network", "STRING v12.0 interactions"),
    ("msigdb", "annotations", "msigdb", "MSigDB Hallmark and C2 overlaps"),
    ("pubmed", "pubmed", "retrieve", "PubMed abstracts (about 1,400 queries; slowest step)"),
    ("documents", "documents", "build", "evidence records -> data/evidence/rag_documents.jsonl"),
]
PACKAGES = {"pandas": "pandas", "numpy": "numpy", "requests": "requests", "lifelines": "lifelines",
            "sklearn": "scikit-learn", "goatools": "goatools", "Bio": "biopython", "matplotlib": "matplotlib",
            "chromadb": "chromadb", "google.genai": "google-genai", "flask": "flask"}


def missing_packages() -> list:
    def found(module):
        try:
            return importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            return False
    return [pip for module, pip in PACKAGES.items() if not found(module)]


def has_key(*names) -> bool:
    return any(os.environ.get(n) for n in names)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def cmd_check(_args) -> None:
    from pges import rag
    missing = missing_packages()
    rows = [("Python packages", not missing, "installed" if not missing else "pip install " + " ".join(missing)),
            ("TCGA raw data", any(TCGA_RAW.glob("*Merge_rnaseqv2*")), "needed only to rebuild the evidence"),
            ("GO raw files", all(p.exists() for p in GO_FILES), "see data/raw/README.md"),
            ("MSigDB raw files", all(p.exists() for p in MSIGDB_FILES), "see data/raw/README.md"),
            ("ENTREZ_EMAIL", has_key("ENTREZ_EMAIL"), "needed for the genes and pubmed steps"),
            ("OPENROUTER_API_KEY or GEMINI_API_KEY", has_key("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"),
             "needed for chat, test --llm and generating answers")]
    if "chromadb" not in missing:
        rows.append(("Vector index", *rag.index_status()))
    width = max(len(r[0]) for r in rows)
    for name, ok, note in rows:
        print(f"  {'✓' if ok else '✗'} {name.ljust(width)}  {note}")


def cmd_data(args) -> None:
    import importlib
    names = [s[0] for s in STEPS]
    for step in (args.from_step, args.to_step):
        require(step is None or step in names, f"Unknown step {step}; steps: {' '.join(names)}")
    selected = STEPS[names.index(args.from_step or names[0]):names.index(args.to_step or names[-1]) + 1]
    require(not missing_packages(), "Missing packages: pip install " + " ".join(missing_packages()))
    for name, module, function, description in selected:
        print(f"\n▶ {name}: {description}")
        start = time.time()
        getattr(importlib.import_module(f"pges.{module}"), function)()
        print(f"  done in {time.time() - start:.0f}s")
    if any(s[0] == "documents" for s in selected):
        ensure_index()


def ensure_index(force: bool = False) -> None:
    from pges import rag
    current, reason = rag.index_status()
    if current and not force:
        print(f"Index is current ({reason}).")
        return
    print(f"Rebuilding the index ({'forced' if current else reason}).")
    print(f"  {rag.build_index()} vectors")


def cmd_index(args) -> None:
    ensure_index(args.force)


def cmd_figures(_args) -> None:
    from pges import figures
    figures.draw()


def cmd_test(args) -> None:
    ensure_index()
    env = dict(os.environ, RUN_LLM_TESTS="1" if args.llm else "")
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT, env=env)
    sys.exit(result.returncode)


def cmd_chat(args) -> None:
    from pges import chat
    ensure_index()
    chat.terminal() if args.terminal else chat.serve()


def cmd_evaluate(args) -> None:
    from pges import evaluate
    ensure_index()
    evaluate.main(args.rest)


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    p = sub.add_parser("data", help="rebuild the evidence; steps: " + " ".join(s[0] for s in STEPS))
    p.add_argument("--from", dest="from_step")
    p.add_argument("--to", dest="to_step")
    p.set_defaults(fn=cmd_data)
    p = sub.add_parser("index")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_index)
    sub.add_parser("figures").set_defaults(fn=cmd_figures)
    p = sub.add_parser("test")
    p.add_argument("--llm", action="store_true")
    p.set_defaults(fn=cmd_test)
    p = sub.add_parser("chat")
    p.add_argument("--terminal", action="store_true")
    p.set_defaults(fn=cmd_chat)
    p = sub.add_parser("evaluate", add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_evaluate)
    args, unknown = parser.parse_known_args()
    if args.command == "evaluate":
        args.rest = unknown + args.rest
    elif unknown:
        parser.error(f"unrecognised arguments: {' '.join(unknown)}")
    args.fn(args)


if __name__ == "__main__":
    main()
