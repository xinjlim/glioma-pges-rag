"""Judge the assistant's answers with an independent Qwen model (rubric + RAGAS-style scores).

For each answer the judge sees the question, the answer, automatic citation and number checks, the records the
generator retrieved, and up to 30 reference records from the same index that the generator did not see.

Scores
  Interpretability rubric, 1 (Poor) to 4 (Excellent): biological plausibility, literature support, consistency,
    clinical readability, and reproducibility (judged across the runs of a question).
  RAGAS-style, 0-1: faithfulness = (supported + 0.5 x partially supported claims) / claims;
    answer relevance = judge rating; context relevance = retrieved records judged relevant / retrieved records.
  Automatic: citation validity = cited IDs found among the retrieved records / all cited evidence IDs.

Judge backends (JUDGE_BACKEND): openrouter (default, qwen/qwen3.8-27b) or ollama (local, qwen3:8b).
Both run at temperature 0 with seed 42 and a JSON-schema output.

Outputs in --out: answers.jsonl (generation), raw.jsonl, scores.csv, justifications.csv, report.md.
Judged answers are also appended to progress.jsonl, so re-running the same command continues an interrupted run.
"""
import argparse
import csv
import json
import os
import re
import statistics
import time
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests

from . import rag as rc
from .config import EVALUATION, QUESTIONS_JSON

JUDGE_BACKEND = os.environ.get("JUDGE_BACKEND", "openrouter").lower()
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "qwen3:8b" if JUDGE_BACKEND == "ollama" else "qwen/qwen3.8-27b")
JUDGE_CTX = 24576
JUDGE_SEED = 42
JUDGE_MAX_TOKENS = 32768
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
REF_K = 15                  # unfiltered semantic hits added to the reference evidence
REF_MAX = 30                # reference records shown to the judge
EVIDENCE_CHARS = 1200       # characters per record in the judge prompt
GEN_SLEEP = 6               # seconds between generator calls (provider rate limits)

RUBRIC = [
    ("biological_plausibility", "Biological plausibility",
     "1 Poor: explanations are biologically incorrect or unsupported | 2 Satisfactory: partially aligns with known "
     "biology | 3 Good: mostly consistent with known biology, minor issues | 4 Excellent: strongly aligned with "
     "established biological evidence"),
    ("literature_support", "Literature support",
     "1 Poor: no supporting literature | 2 Satisfactory: some supporting evidence, incomplete justification | "
     "3 Good: well supported by relevant literature | 4 Excellent: strongly supported by high-quality biomedical sources"),
    ("consistency", "Consistency",
     "1 Poor: contradictory or illogical | 2 Satisfactory: moderately consistent reasoning | 3 Good: mostly coherent "
     "and logically structured | 4 Excellent: highly coherent, scientifically consistent reasoning"),
    ("clinical_readability", "Clinical readability",
     "1 Poor: difficult to understand | 2 Satisfactory: mostly understandable | 3 Good: clear and interpretable | "
     "4 Excellent: concise and clinically understandable"),
]
REPRO = ("reproducibility", "Reproducibility",
         "1 Poor: outputs vary greatly between runs | 2 Satisfactory: some variability in explanations | "
         "3 Good: mostly reproducible outputs | 4 Excellent: stable reasoning across repeated runs")

JUDGE_SYSTEM = """You are an expert reviewer in cancer genomics and in evaluating retrieval-augmented generation (RAG).
You assess answers produced by a RAG assistant that explains why five glioma prognostic gene expression signatures
(Pyroptosis, Diffuse, Stemness, Glyco, Hypoxia) disagree when stratifying the same TCGA GBMLGG patients.

The assistant's knowledge is a ChromaDB vector database of evidence records with IDs:
  STAT_*      statistics computed on the cohort (log-rank tests, ARI, Jaccard)
  DERIVED_*   set operations over the corpus (genes per signature, shared annotations per pair)
  STRING_*    protein-protein interactions; REACTOME_* pathway enrichment; MSIGDB_* gene-set overlaps;
  GO_*        Gene Ontology annotations; PMID_*  PubMed abstract chunks.
The assistant is instructed to base every claim on the RETRIEVED evidence, cite IDs in [brackets], avoid causal
language, and label interpretive claims Supported / Plausible hypothesis / Not established.

You receive the question, the answer, the RETRIEVED evidence (what the assistant saw), REFERENCE evidence (other
records from the same database that the assistant did not see), and automatic citation checks.

Judge against the database, not against your memory:
- A claim is "supported" only if the cited or retrieved evidence states or directly shows it; "partially_supported"
  if the evidence backs part of it or it is a reasonable, clearly labelled hypothesis; "unsupported" if no
  retrieved record backs it; "contradicted" if any retrieved or reference record conflicts with it.
- Split the answer into atomic factual claims. Ignore pure structure, hedges and questions.
- Mark each RETRIEVED record relevant if it bears on the question.
- Use your general biomedical knowledge only to judge biological plausibility, and say so in the justification.

Interpretability rubric (score 1-4):
""" + "\n".join(f"- {label}: {desc}" for _, label, desc in RUBRIC) + """
Answer relevance (one of 0, 0.25, 0.5, 0.75, 1): how directly and completely the answer addresses the question,
without padding. 1 = fully and directly answers it; 0 = does not answer it.

Every justification: 1-3 sentences, specific, citing evidence IDs or quoting the answer where useful."""

REPRO_SYSTEM = f"""You compare repeated answers to the same question from a RAG assistant on glioma prognostic
signatures. Score reproducibility on this scale and justify it in 1-3 sentences, naming what stayed stable
(conclusions, statistics, cited evidence) and what changed.
{REPRO[1]}: {REPRO[2]}"""

_JUST = {"type": "string"}
_SCORE4 = {"type": "object", "additionalProperties": False, "required": ["score", "justification"],
           "properties": {"score": {"type": "integer", "enum": [1, 2, 3, 4]}, "justification": _JUST}}
JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["claims", "retrieved_relevance", "answer_relevance", "rubric", "overall_comment"],
    "properties": {
        "claims": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["claim", "verdict", "evidence_ids", "note"],
            "properties": {"claim": _JUST,
                           "verdict": {"type": "string",
                                       "enum": ["supported", "partially_supported", "unsupported", "contradicted"]},
                           "evidence_ids": {"type": "array", "items": {"type": "string"}},
                           "note": _JUST}}},
        "retrieved_relevance": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "relevant"],
            "properties": {"id": {"type": "string"}, "relevant": {"type": "boolean"}}}},
        "answer_relevance": {"type": "object", "additionalProperties": False, "required": ["score", "justification"],
                             "properties": {"score": {"type": "number", "enum": [0, 0.25, 0.5, 0.75, 1]},
                                            "justification": _JUST}},
        "rubric": {"type": "object", "additionalProperties": False, "required": [k for k, _, _ in RUBRIC],
                   "properties": {k: _SCORE4 for k, _, _ in RUBRIC}},
        "overall_comment": _JUST,
    },
}
REPRO_SCHEMA = _SCORE4


# --------------------------------------------------------------------------- #
# Evidence and automatic checks
# --------------------------------------------------------------------------- #

def corpus_profile(col) -> str:
    counts = {}
    metas = col.get(include=["metadatas"])["metadatas"]
    for m in metas:
        counts[m.get("source", "?")] = counts.get(m.get("source", "?"), 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])) + f" (total {len(metas)})"


def reference_evidence(query: str, col, emb, retrieved_ids: set) -> list:
    """Records from the same index that the generator was not shown."""
    focus = rc.route(query, col)["focus"]
    ids = [f"{p}_{s}" for s in focus for p in ("STAT_survival", "DERIVED_genes", "STRING")]
    if len(focus) >= 2:
        ids += [f"{p}_{a}_{b}" for a, b in combinations(sorted(focus), 2) for p in ("STAT_pair", "DERIVED_overlap")]
    ref = rc.fetch(col, [i for i in ids if i not in retrieved_ids])
    seen = retrieved_ids | {e["id"] for e in ref}
    r = col.query(query_embeddings=[emb.embed_query(query)], n_results=REF_K + len(retrieved_ids))
    for i, t, m, d in zip(r["ids"][0], r["documents"][0], r["metadatas"][0], r["distances"][0]):
        if i not in seen:
            seen.add(i)
            ref.append({"id": i, "text": t, "metadata": m, "how": f"semantic (cos.dist {d:.3f})"})
    return ref[:REF_MAX]


def auto_checks(answer: str, evidence: list) -> dict:
    cit = rc.check_citations(answer, evidence)
    nums = rc.unsupported_numbers(answer, "\n".join(e["text"] for e in evidence))
    return {"n_retrieved": len(evidence), "n_cited": len(cit["cited"]), "cited_ids": cit["cited"],
            "unknown_ids": cit["unknown_ids"], "numbers_not_in_evidence": nums,
            "citation_validity": round(len(cit["cited"]) / max(1, len(cit["cited"]) + len(cit["unknown_ids"])), 3)}


# --------------------------------------------------------------------------- #
# Judge
# --------------------------------------------------------------------------- #

def _blocks(evs) -> str:
    return "\n\n".join(f"[{e['id']}] ({e['metadata'].get('source', '')})\n{e['text'][:EVIDENCE_CHARS]}"
                       for e in evs) or "(none)"


def judge_messages(q: dict, answer: str, evidence, reference, profile: str, auto: dict) -> list:
    user = (f"VECTOR DATABASE: {profile}\n\n"
            f"QUESTION ({q.get('type', 'unspecified')}): {q['query']}\n\n"
            f"ANSWER TO EVALUATE:\n{answer}\n\n"
            f"AUTOMATIC CHECKS: cited {auto['n_cited']} of {auto['n_retrieved']} retrieved records; "
            f"cited IDs not in retrieved evidence: {auto['unknown_ids'] or 'none'}; "
            f"decimal numbers that match no evidence value (rounding allowed): {auto['numbers_not_in_evidence'] or 'none'}\n\n"
            f"=== RETRIEVED EVIDENCE (given to the assistant) ===\n{_blocks(evidence)}\n\n"
            f"=== REFERENCE EVIDENCE (same database, not given to the assistant) ===\n{_blocks(reference)}")
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]


def check_judge_ready() -> None:
    if JUDGE_BACKEND == "ollama":
        try:
            tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10).json()
        except requests.RequestException:
            raise SystemExit(f"Ollama is not running at {OLLAMA_URL} (https://ollama.com).")
        if not {JUDGE_MODEL, f"{JUDGE_MODEL}:latest"} & {m["name"] for m in tags.get("models", [])}:
            raise SystemExit(f"Model {JUDGE_MODEL} is not pulled. Run: ollama pull {JUDGE_MODEL}")
    elif JUDGE_BACKEND == "openrouter":
        if not os.environ.get("OPENROUTER_API_KEY") or not JUDGE_MODEL:
            raise SystemExit("JUDGE_BACKEND=openrouter needs OPENROUTER_API_KEY and JUDGE_MODEL.")
    else:
        raise SystemExit("JUDGE_BACKEND must be 'ollama' or 'openrouter'.")


def _est_tokens(messages) -> int:
    return sum(len(m["content"]) for m in messages) // 3


def _parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


def _validate(obj, schema, path="$") -> None:
    """Minimal JSON-schema check for the shapes used here."""
    kind = schema.get("type")
    if "enum" in schema and obj not in schema["enum"]:
        raise ValueError(f"{path}: {obj!r} not in {schema['enum']}")
    checks = {"integer": lambda o: isinstance(o, int) and not isinstance(o, bool),
              "number": lambda o: isinstance(o, (int, float)), "boolean": lambda o: isinstance(o, bool),
              "string": lambda o: isinstance(o, str), "object": lambda o: isinstance(o, dict),
              "array": lambda o: isinstance(o, list)}
    if kind in checks and not checks[kind](obj):
        raise ValueError(f"{path}: expected {kind}")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in obj:
                raise ValueError(f"{path}: missing '{key}'")
            _validate(obj[key], schema["properties"][key], f"{path}.{key}")
    elif kind == "array":
        for i, item in enumerate(obj):
            _validate(item, schema["items"], f"{path}[{i}]")


def _post(messages, schema, name) -> str:
    if JUDGE_BACKEND == "ollama":
        num_ctx = max(JUDGE_CTX, -(-(_est_tokens(messages) + 6144) // 4096) * 4096)
        r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=7200, json={
            "model": JUDGE_MODEL, "messages": messages, "format": schema, "stream": False, "think": False,
            "options": {"temperature": 0, "seed": JUDGE_SEED, "num_ctx": num_ctx}})
        r.raise_for_status()
        return r.json()["message"]["content"]
    r = requests.post(rc.OPENROUTER_URL, timeout=600, headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
                      json={"model": JUDGE_MODEL, "messages": messages, "temperature": 0, "seed": JUDGE_SEED,
                            "max_tokens": JUDGE_MAX_TOKENS,
                            "response_format": {"type": "json_schema",
                                                "json_schema": {"name": name, "strict": True, "schema": schema}}})
    if r.status_code in (429, 500, 502, 503):
        raise ConnectionError(f"{r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    choice = r.json()["choices"][0]
    if not choice["message"].get("content"):
        raise ValueError(f"empty judge response (finish_reason={choice.get('finish_reason')})")
    return choice["message"]["content"]


def call_judge(messages, schema, name, tries=4) -> dict:
    """The schema is enforced by the backend, stated in the prompt and validated here."""
    messages = [dict(messages[0], content=messages[0]["content"] +
                     "\n\nReturn only a JSON object that matches this JSON schema:\n" + json.dumps(schema))] + messages[1:]
    last = None
    for attempt in range(tries):
        try:
            out = _parse_json(_post(messages, schema, name))
            _validate(out, schema)
            return out
        except (ConnectionError, requests.Timeout) as exc:
            last = exc
            time.sleep(20 * (attempt + 1))
        except ValueError as exc:  # includes JSONDecodeError: ask again, quoting the error
            last = exc
            messages = messages + [{"role": "user", "content": f"Your output was invalid ({exc}). "
                                                                "Return only the corrected JSON object."}]
    raise RuntimeError(f"Judge failed after {tries} attempts: {last}")


def ragas_scores(j: dict, evidence: list) -> dict:
    claims = j["claims"]
    weight = {"supported": 1.0, "partially_supported": 0.5, "unsupported": 0.0, "contradicted": 0.0}
    retrieved = {e["id"] for e in evidence}
    relevant = sum(x["relevant"] for x in {x["id"]: x for x in j["retrieved_relevance"] if x["id"] in retrieved}.values())

    def rnd(x):
        return None if x is None else round(x, 3)

    return {"faithfulness": rnd(sum(weight[c["verdict"]] for c in claims) / len(claims) if claims else None),
            "answer_relevance": rnd(min(1.0, max(0.0, j["answer_relevance"]["score"]))),
            "context_relevance": rnd(relevant / len(retrieved) if retrieved else None),
            "claims_total": len(claims),
            "claims_unsupported": sum(c["verdict"] in ("unsupported", "contradicted") for c in claims)}


def judge_reproducibility(query: str, answers: list) -> dict:
    body = "\n\n".join(f"--- RUN {i + 1} ---\n{a}" for i, a in enumerate(answers))
    return call_judge([{"role": "system", "content": REPRO_SYSTEM},
                       {"role": "user", "content": f"QUESTION: {query}\n\n{body}"}], REPRO_SCHEMA, "reproducibility")


def cited_jaccard(cited_sets: list):
    pairs = [len(a & b) / len(a | b) for a, b in combinations(cited_sets, 2) if a | b]
    return round(statistics.mean(pairs), 3) if pairs else None


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

SCORE_COLS = [("faithfulness", "Faithfulness"), ("answer_relevance", "Answer relevance"),
              ("context_relevance", "Context relevance"),
              ("biological_plausibility", "Biological plausibility"), ("literature_support", "Literature support"),
              ("consistency", "Consistency"), ("clinical_readability", "Clinical readability"),
              ("reproducibility", "Reproducibility"), ("citation_validity", "Citation validity")]


def _cell(s) -> str:
    return str(s if s is not None else "\u2013").replace("|", "\\|").replace("\n", " ")


def write_outputs(rows: list, out: Path) -> None:
    with open(out / "raw.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    flat, just = [], []
    for r in rows:
        s, j = r["scores"], r["judge"]
        unsupported = "; ".join(c["claim"] for c in j["claims"] if c["verdict"] in ("unsupported", "contradicted"))
        row = {"question_id": r["qid"], "question": r["query"], "run": r["run"], "mode": r["mode"], "model": r["model"],
               **{k: s.get(k) for k, _ in SCORE_COLS},
               "answer_relevance_justification": j["answer_relevance"]["justification"],
               **{f"{k}_justification": j["rubric"][k]["justification"] for k, _, _ in RUBRIC},
               "reproducibility_justification": r.get("repro", {}).get("justification", ""),
               "unsupported_claims": unsupported, "overall_comment": j["overall_comment"]}
        flat.append(row)
        base = {"question_id": r["qid"], "run": r["run"]}
        just += [{**base, "dimension": "Faithfulness", "score": s["faithfulness"],
                  "justification": f"{s['claims_total'] - s['claims_unsupported']}/{s['claims_total']} claims "
                                   f"supported or partially supported. Unsupported: {unsupported or 'none'}"},
                 {**base, "dimension": "Answer relevance", "score": s["answer_relevance"],
                  "justification": row["answer_relevance_justification"]},
                 {**base, "dimension": "Context relevance", "score": s["context_relevance"],
                  "justification": f"{sum(x['relevant'] for x in j['retrieved_relevance'])} of "
                                   f"{r['auto']['n_retrieved']} retrieved records judged relevant."}]
        just += [{**base, "dimension": label, "score": s[k], "justification": j["rubric"][k]["justification"]}
                 for k, label, _ in RUBRIC]
        if "repro" in r:
            just.append({**base, "dimension": "Reproducibility", "score": s["reproducibility"],
                         "justification": r["repro"]["justification"]})
    for name, data in (("scores.csv", flat), ("justifications.csv", just)):
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)

    md = ["# RAG answer evaluation", "",
          f"Generator: {', '.join(sorted({r['model'] for r in rows}))} \u00b7 Judge: {JUDGE_MODEL} ({JUDGE_BACKEND}, "
          f"temperature 0) \u00b7 {len({r['qid'] for r in rows})} questions \u00b7 {max(r['run'] for r in rows)} run(s) "
          f"each \u00b7 {datetime.now():%Y-%m-%d %H:%M}", "",
          "Rubric dimensions are scored 1 (Poor) to 4 (Excellent); RAGAS dimensions and citation validity 0\u20131.", "",
          "## Scores", "",
          "| Question | Run | Mode | " + " | ".join(label for _, label in SCORE_COLS) + " |",
          "| --- | --- | --- | " + " | ".join("---" for _ in SCORE_COLS) + " |"]
    md += [f"| {r['question_id']} | {r['run']} | {r['mode']} | " + " | ".join(_cell(r[k]) for k, _ in SCORE_COLS) + " |"
           for r in flat]
    md += ["", "## Mean per dimension", "", "| Dimension | Mean | n |", "| --- | --- | --- |"]
    for k, label in SCORE_COLS:
        vals = [r[k] for r in flat if isinstance(r[k], (int, float))]
        md.append(f"| {label} | {statistics.mean(vals):.2f} | {len(vals)} |" if vals else f"| {label} | \u2013 | 0 |")
    md += ["", "## Justifications", "", "| Question | Run | Dimension | Score | Justification |",
           "| --- | --- | --- | --- | --- |"]
    md += [f"| {j['question_id']} | {j['run']} | {j['dimension']} | {_cell(j['score'])} | {_cell(j['justification'])} |"
           for j in just]
    md += ["", "## Questions", ""] + [f"- **{qid}**: {q}" for qid, q in dict((r["qid"], r["query"]) for r in rows).items()]
    (out / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def _jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def generate_answers(jobs: list, out: Path, col, emb) -> None:
    """Generation only; each answer is appended to <out>/answers.jsonl, so a stopped run can continue."""
    path = out / "answers.jsonl"
    done = {(a["id"], a["run"]) for a in _jsonl(path)}
    for q, run, _ in [j for j in jobs if (j[0]["id"], j[1]) not in done]:
        print(f"{q['id']} run {run}: {q['query'][:70]}")
        try:
            r = rc.answer(q["query"], col, emb)
        except RuntimeError as exc:
            raise SystemExit(f"Stopped: {exc}\nRun the same command later to generate the rest.")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": q["id"], "type": q.get("type", ""), "query": q["query"], "run": run,
                                "answer": r["answer"], "model": r["model"], "mode": r["mode"],
                                "evidence_ids": [e["id"] for e in r["evidence"]]}, ensure_ascii=False) + "\n")
        time.sleep(GEN_SLEEP)
    print(f"Answers in {path}. Judge them with: python pipeline.py evaluate --answers {path} --out {out}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pipeline.py evaluate")
    ap.add_argument("--questions", default=str(QUESTIONS_JSON))
    ap.add_argument("--answers", help="JSONL of saved answers {id, query, answer, run, model}; skips generation")
    ap.add_argument("--runs", type=int, default=3, help="generator runs per question (reproducibility needs >= 2)")
    ap.add_argument("--ids", help="comma-separated question IDs, e.g. Q1,Q2,Q3,Q4 (default: all)")
    ap.add_argument("--out", default=str(EVALUATION / datetime.now().strftime("%Y%m%d_%H%M%S")))
    ap.add_argument("--dry-run", action="store_true", help="print the first judge prompt and exit (no API calls)")
    ap.add_argument("--generate-only", action="store_true", help="generate answers into <out>/answers.jsonl only")
    args = ap.parse_args(argv)

    out = Path(args.out)
    col, emb = rc.open_index()
    profile = corpus_profile(col)
    if args.answers:
        items = _jsonl(Path(args.answers))
        jobs = [({"id": it.get("id", f"A{n + 1}"), "query": it["query"], "type": it.get("type", "")},
                 it.get("run", 1), it) for n, it in enumerate(items)]
    else:
        jobs = [(q, run, None) for q in json.load(open(args.questions, encoding="utf-8"))
                for run in range(1, args.runs + 1)]
    if args.ids:
        wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
        unknown = sorted(set(wanted) - {q["id"] for q, _, _ in jobs})
        if unknown:
            raise SystemExit(f"Unknown question IDs {unknown}")
        jobs = [j for j in jobs if j[0]["id"] in wanted]

    if args.dry_run:
        q, _, saved = jobs[0]
        info, ev = rc.retrieve(q["query"], col, emb)
        ans = saved["answer"] if saved else "(generated answer would appear here)"
        msgs = judge_messages(q, ans, ev, reference_evidence(q["query"], col, emb, {e["id"] for e in ev}), profile,
                              auto_checks(ans, ev))
        for m in msgs:
            print(f"===== {m['role'].upper()} =====\n{m['content']}\n")
        print(f"[dry run] intent={info['intent']} retrieved={len(ev)} judge={JUDGE_BACKEND}:{JUDGE_MODEL} "
              f"~{_est_tokens(msgs)} prompt tokens")
        return
    out.mkdir(parents=True, exist_ok=True)
    if args.generate_only:
        generate_answers(jobs, out, col, emb)
        return

    check_judge_ready()
    progress = out / "progress.jsonl"
    rows = _jsonl(progress)
    done = {(r["qid"], r["run"]) for r in rows}
    for q, run, saved in jobs:
        if (q["id"], run) in done:
            continue
        print(f"{q['id']} run {run}: {q['query'][:70]}")
        if saved:
            info, ev = rc.retrieve(q["query"], col, emb)  # retrieval is deterministic: this is what the generator saw
            ans, mode, model = saved["answer"], info["intent"], saved.get("model", "saved answer")
        else:
            try:
                r = rc.answer(q["query"], col, emb)
            except RuntimeError as exc:
                raise SystemExit(f"Stopped: {exc}\nJudged answers are kept; run the same command later to continue.")
            ans, ev, mode, model = r["answer"], r["evidence"], r["mode"], r["model"]
            time.sleep(GEN_SLEEP)
        ref = reference_evidence(q["query"], col, emb, {e["id"] for e in ev})
        auto = auto_checks(ans, ev)
        j = call_judge(judge_messages(q, ans, ev, ref, profile, auto), JUDGE_SCHEMA, "rag_evaluation")
        scores = {**ragas_scores(j, ev), **{k: j["rubric"][k]["score"] for k, _, _ in RUBRIC},
                  "citation_validity": auto["citation_validity"]}
        rows.append({"qid": q["id"], "query": q["query"], "type": q.get("type", ""), "run": run, "mode": mode,
                     "model": model, "answer": ans, "retrieved_ids": [e["id"] for e in ev],
                     "reference_ids": [e["id"] for e in ref], "auto": auto, "judge": j, "scores": scores})
        with open(progress, "a", encoding="utf-8") as f:
            f.write(json.dumps(rows[-1], ensure_ascii=False) + "\n")
        print(f"   faithfulness={scores['faithfulness']} context={scores['context_relevance']} "
              f"rubric={[scores[k] for k, _, _ in RUBRIC]}")

    for qid in dict.fromkeys(r["qid"] for r in rows):
        group = sorted((r for r in rows if r["qid"] == qid), key=lambda r: r["run"])
        if len(group) < 2:
            continue
        rep = judge_reproducibility(group[0]["query"], [r["answer"] for r in group])
        rep["justification"] += (f" (Mean Jaccard of cited IDs across runs: "
                                 f"{cited_jaccard([set(r['auto']['cited_ids']) for r in group])}.)")
        for r in group:
            r["repro"], r["scores"]["reproducibility"] = rep, rep["score"]
    write_outputs(rows, out)
    print(f"Wrote {out / 'report.md'}")
