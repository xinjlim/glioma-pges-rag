# Glioma PGES discordance: evidence-grounded explanations with retrieval-augmented generation

Five published glioma prognostic gene expression signatures (PGESs) each separate survival in the same TCGA
cohort, yet often place the same patient in different risk groups. This pipeline measures that disagreement and
answers questions about it with a retrieval-augmented LLM that must cite an evidence record for every claim.

| Signature | Source | Genes |
| --- | --- | --- |
| Pyroptosis | Huang et al. (2022) | 7 |
| Diffuse | Zhang et al. (2021) | 5 |
| Stemness | Niu et al. (2024) | 5 |
| Glyco | Zou et al. (2024) | 8 |
| Hypoxia | Gao et al. (2021) | 7 |

Gene lists and published Cox coefficients are in `data/signatures.csv`.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate     # Python 3.10
pip install -r requirements.txt
cp .env.example .env                                   # add OPENROUTER_API_KEY for the chat
python pipeline.py check
python pipeline.py test                                # offline integrity and retrieval tests
python pipeline.py chat                                # http://127.0.0.1:5000  (--terminal for a terminal chat)
```

The evidence file, vector index, processed tables, figures and evaluation results are provided, so chat, tests,
figures and evaluation need no raw data.

## How it works

1. **Statistics** (`pges/tcga.py`, `pges/stats.py`). TCGA GBMLGG RNA-seq (RSEM-normalised) and clinical data from
   the Broad GDAC Firehose (2016-01-28) are matched by patient (674 patients). Each signature's risk score is the
   sum of coefficient x expression; patients at or above the median are high risk. Log-rank tests use the 672
   patients with survival data; the adjusted Rand index (ARI), Jaccard index of high-risk groups and Pearson
   correlation of scores measure agreement between signatures.
2. **Biological evidence** (`pges/genes.py`, `pges/annotations.py`, `pges/pubmed.py`). Genes are resolved to NCBI
   official symbols and aliases, then annotated with Gene Ontology, Reactome (top 50 pathways), STRING v12.0
   (score >= 0.4), MSigDB Hallmark + C2, and PubMed abstracts (glioma, 2016-2026, 20 per query).
3. **Evidence records** (`pges/documents.py`). Every result becomes a short text record with a stable ID such as
   `STAT_pair_Diffuse_Glyco` or `PMID_31771235_chunk1` (5,688 records). At indexing time, 15 `DERIVED` records add
   genes per signature and shared annotations per pair, for 5,703 vectors.
4. **Retrieval and generation** (`pges/rag.py`). Records are embedded using all-MiniLM-L6-v2 from ChromaDB, with cosine distance. A rule-based router picks   one of seven intents; statistics and overlaps are fetched by ID and other
   evidence by similarity search filtered by signature and source (distance <= 0.75). Gemini (`gemini-3.8-flash` through
   OpenRouter, temperature 0.1) answers under a prompt that requires cited IDs and labels interpretive claims "Supported",
   "Plausible hypothesis" or "Not established". Each answer's citations and decimal numbers are checked against the
   retrieved evidence.
5. **Evaluation** (`pges/evaluate.py`). An independent Qwen judge (temperature 0, seed 42, JSON-schema output)
   scores each answer on a five-dimension interpretability rubric (1-4) and RAGAS-style faithfulness, answer
   relevance and context relevance, using the retrieved records plus up to 30 reference records the generator did
   not see.

## Commands

| Command | What it does |
| --- | --- |
| `python pipeline.py check` | Packages, raw data, keys and index status |
| `python pipeline.py data [--from STEP] [--to STEP]` | Rebuild the evidence; steps: `download standardise scores survival agreement genes go reactome string msigdb pubmed documents` |
| `python pipeline.py index [--force]` | Rebuild the vector index when the evidence file has changed |
| `python pipeline.py figures` | Kaplan-Meier curves, ARI / Jaccard / Pearson heatmaps, r vs ARI -> `results/figures/` |
| `python pipeline.py test [--llm]` | Offline tests; `--llm` also checks a generated answer |
| `python pipeline.py chat [--terminal]` | Web or terminal chat |
| `python pipeline.py evaluate [OPTIONS]` | Generate and judge answers to `data/eval_questions.json` |

Rebuilding the evidence needs the raw files in [data/raw/README.md](data/raw/README.md) and `ENTREZ_EMAIL` for
the `genes` and `pubmed` steps. PubMed retrieval (about 1,400 queries) is the slowest step.

### Evaluation

The reported results in `results/evaluation/comparison/` score the four comparison questions (Q1-Q4), three runs
each, with `gemini-3.8-flash` through OpenRouter and the judge `qwen/qwen3.8-27b` through OpenRouter:

```bash
python pipeline.py evaluate --ids Q1,Q2,Q3,Q4 --generate-only --out results/evaluation/new
python pipeline.py evaluate --answers results/evaluation/new/answers.jsonl --out results/evaluation/new
```

The judge runs through OpenRouter by default and needs `OPENROUTER_API_KEY`. `JUDGE_BACKEND=ollama` runs
`qwen3:8b` through a local [Ollama](https://ollama.com) server instead (`ollama pull qwen3:8b`). `--dry-run`
prints the judge prompt without calling any model. Re-running a stopped command continues where it stopped.

## Repository layout

```
pipeline.py               command-line entry point
pges/                     package: config, tcga, stats, genes, annotations, pubmed, documents, rag, chat,
                          evaluate, figures, static/index.html
tests/test_pipeline.py    integrity, retrieval and generation tests
data/signatures.csv       the five signatures (input)
data/eval_questions.json  evaluation questions Q1-Q9
data/processed/           statistics and annotation tables
data/evidence/            rag_documents.jsonl (evidence records) and chroma_db/ (vector index)
results/figures/          report figures
results/evaluation/       evaluation of the comparison questions
```

## Reproducibility

- Re-running the statistics, GO, MSigDB and evidence-record steps on the same inputs reproduces the included files
  byte for byte, and the figures pixel for pixel. Log-rank p-values in `survival_results.csv` match a re-run to
  at least 12 significant digits.
- STRING is pinned to v12.0 and reproduces exactly. Reactome is queried live and its later releases return
  different pathways, so `reactome_results.csv` is the archived result behind the evidence.
- PubMed relevance ranking changes over time; `data/processed/pubmed_query_log.csv` records all 1,399 queries and
  `pubmed_articles.csv` the 987 articles used.
- Retrieval is deterministic: re-running it reproduces the evidence IDs stored with every evaluated answer.

## Data and licences

TCGA data are open access. Gene Ontology, Reactome, STRING, NCBI Gene and PubMed are public resources; MSigDB
files require free registration and are not redistributed.
