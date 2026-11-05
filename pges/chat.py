"""Chat interfaces: a local web page (Flask) and a terminal loop."""
import os
from pathlib import Path

from . import rag


def serve() -> None:
    from flask import Flask, jsonify, request, send_from_directory

    app = Flask(__name__)
    col, emb = rag.open_index()

    @app.get("/")
    def index():
        return send_from_directory(Path(__file__).parent / "static", "index.html")

    @app.post("/api/chat")
    def chat():
        query = ((request.get_json(silent=True) or {}).get("query") or "").strip()
        if not query:
            return jsonify(error="Enter a question."), 400
        try:
            r = rag.answer(query, col, emb)
        except Exception as exc:  # quota and key problems are shown in the page
            return jsonify(error=str(exc)), 502
        r["evidence"] = [{"id": e["id"], "source": e["metadata"].get("source", ""), "how": e["how"], "text": e["text"]}
                         for e in r["evidence"]]
        return jsonify(r)

    port = int(os.environ.get("PORT", 5000))
    print(f"Open http://127.0.0.1:{port} (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False)


def terminal() -> None:
    col, emb = rag.open_index()
    print(f"{col.count()} evidence records | model: {', '.join(rag.GEMINI_MODELS)} | blank line to quit")
    while query := input("\nQuestion> ").strip():
        r = rag.answer(query, col, emb)
        print(f"\n[{r['mode']} | {', '.join(r['signatures'])} | {r['model']} | {len(r['evidence'])} records]\n")
        print(r["answer"])
        if r["citations"]["unknown_ids"]:
            print("\nCited IDs not in the retrieved evidence:", r["citations"]["unknown_ids"])
