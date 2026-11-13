# ple-sitecheck

A verifier-in-the-loop agent harness for the
[Physical-Engineering's Last Exam (PLE)](https://ple-bench.com) benchmark,
built for the Google "Agent Harness on PLE" competition.

PLE tasks ask an agent to produce professional engineering deliverables
(workbooks, memos, drawings, models, archives) from a brief and a folder of
project documents. ple-sitecheck wraps a plain OpenAI-compatible tool-calling
loop with the habits of a checking engineer on site: inventory the documents
first, parse what the brief asks for, write early, then open every
deliverable again and check it before signing. The checks cost CPU, not
tokens, and any sponsored model drops in through one line of
`submission.yaml`.

## What it does

Six stages per task; three of them never call the model:

1. **Triage** (no tokens): lists the workspace, probes the task container's
   tooling, parses workbooks, documents, PDFs, drawings and models in the
   serve container and hands the model a compact inventory.
2. **Checklist**: deliverable paths, archive members, sheet/column names and
   drawing layers parsed from the brief by regex, refined by one cheap model
   call.
3. **Work loop**: the kit's streaming loop with a stable, cacheable prefix,
   retries that wait out rate limits within the task's time budget, context
   compaction and turn/time nudges.
4. **Verify and repair** (no tokens): when the model answers without a tool
   call, every deliverable is opened: exists, parses, has the asked shape, no
   formula cells without cached values, no template placeholders. Optional
   switches add provenance (designations must occur in the input documents),
   substance, value-sanity and layer-by-entities checks. Findings go back as a
   to-do list for bounded repair rounds.
5. **Review round**: one prompted self-check of the governing numbers, units
   and the clause each came from.
6. **Final message**: the model's structured summary plus the harness's own
   list of files it opened and parsed.

Every behaviour beyond the base loop sits behind an environment switch that
defaults to off, so variants can be A/B tested. `DESIGN.md` documents the
switches, the failure modes each targets and the ablation plan.

## Repository layout

| Path | Purpose |
|---|---|
| `submission.yaml` | Model, install and serve commands, harness switches under `env` |
| `serve.py` | WebSocket entry point (serve protocol v1) |
| `harness.py` | Orchestration: triage, checklist, work loop, verification, review, summary |
| `engine.py` | Model loop (adapted from the kit's `custom-openai` sample) |
| `triage.py`, `checklist.py`, `verify.py`, `parsers.py`, `ple_io.py` | The token-free layers and the file parsers/writers |
| `taskbridge.py` | The competition protocol (unchanged copy from the kit) |
| `tests/` | Unit regressions, mock model, fake evaluator, smoke script |
| `scripts/`, `.github/workflows/` | Dev-run automation (GitHub Actions, Codespaces) |
| `writeup/` | Stage-1 write-up draft and figures |
| `DESIGN.md` | Architecture, failure model, switches, ablation plan, run commands |

## Running it

### Unit regressions (no model, no key, no Docker)

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt   # Windows: .venv/Scripts/python.exe
.venv/bin/python tests/test_units.py
tests/run_smoke.sh            # Linux/macOS: mock end-to-end scenarios; SKIP_CHECKLIST=1 without a template clone
```

### With the official kit

The harness runs under the competition kit,
[google-research/ple](https://github.com/google-research/ple) (Apache-2.0),
which needs Docker on an x86-64 machine, `uv`, Harbor, access to the gated
PLE dataset on Hugging Face (`HF_TOKEN`) and a model endpoint.

```bash
git clone https://github.com/google-research/ple kit-src
git clone https://github.com/bki-labs/ple-sitecheck harness
cd kit-src
# local.env: HF_TOKEN=... plus the model/judge variables described in the kit's docs
uv run python -m kit check ../harness --env-file local.env
uv run python eval/run.py ../harness --env-file local.env          # dev-set run, writes ../harness/artifacts/
```

For a local run on an OpenAI-compatible endpoint that is not a sponsored
model, set `model_api: openai-compatible/<name>` and `model_base_url` in
`submission.yaml` (revert before committing; see `DESIGN.md` section 5 and
`scripts/cs_run.sh`).

## Results

| Model | Dev-set score | $/task | Notes |
|---|---|---|---|
| `google-cloud/gemini-3.8-flash` (submitted) | TBD | TBD | full harness |
| second model | TBD | TBD | cross-model check |

Scores come from the kit's `eval/run.py` on the public dev set; the
organisers re-score with the official judge. The committed `artifacts/`
folder (added at submission time) holds the eval-generated trajectories and
results only.

## Data terms

The PLE dataset is gated and its terms forbid redistribution and any training
on task data. This repository contains no task briefs, inputs, templates,
rubrics, expected answers or task-derived names; test fixtures are synthetic
and dev tasks are referred to only by their public task IDs. See
`CONTRIBUTING.md`.

## Citation

```bibtex
@software{bkilabs2026plesitecheck,
  author  = {{BKI Labs}},
  title   = {ple-sitecheck: a verifier-in-the-loop agent harness for engineering deliverables},
  year    = {2026},
  url     = {https://github.com/bki-labs/ple-sitecheck}
}
```

A machine-readable version is in `CITATION.cff`.

## Licence

Apache License 2.0 (`LICENSE`). `engine.py` and `taskbridge.py` are adapted
from or copied unchanged from the PLE kit, Copyright 2026 Google LLC,
Apache-2.0; see `NOTICE`.
