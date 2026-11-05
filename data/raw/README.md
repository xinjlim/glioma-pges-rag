# Raw data (not in the repository)

These files are needed only to rebuild the evidence (`python pipeline.py data`). The processed tables, the
evidence file and the vector index are included, so the chat, tests, figures and evaluation run without them.

| Folder | Files | Source | Version |
| --- | --- | --- | --- |
| `TCGA_GBMLGG/` | RNA-seq V2 RSEM normalised genes (Level 3) and Clinical Pick Tier 1 (Level 4) | Downloaded and MD5-checked by the `download` step from the Broad GDAC Firehose | Standard data run 2016-01-28 |
| `GO/` | `HUMAN-uniprot.gaf.gz` | Human annotations from UniProt-GOA, [GO downloads](https://current.geneontology.org/products/pages/downloads.html) | Generated 2026-07-28 (GO release 2026-07-26) |
| `GO/` | `go-basic.obo` | `http://purl.obolibrary.org/obo/go/releases/2026-07-26/go-basic.obo` | GO release 2026-07-26 |
| `MSigDB/` | `h.all.v2026.1.Hs.symbols.gmt`, `c2.all.v2026.1.Hs.symbols.gmt` | [MSigDB](https://www.gsea-msigdb.org/gsea/msigdb) (free registration; not redistributable) | v2026.1 |

Later GO or MSigDB releases change the annotation and overlap records.
