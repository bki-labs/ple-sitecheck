# ple-sitecheck: design, failure model, ablation plan

Competition: Google - Agent Harness on Physical-Engineering's Last Exam (PLE).
Goal: a cheap harness with a measurably better score per dollar than the
sample harnesses, written up for Stage 1. Primary model
`google-cloud/gemini-3.8-flash`; `gemini-4-argon` is the upgrade path and
`gemma-4-26b` the second model for the cross-model criterion. The model is
swapped in `submission.yaml` only.

## 1. Base choice

Started from the kit's `sample_submissions/custom-openai` (Apache-2.0, kept in
`engine.py` with its header and a change list), not from the Opus/Pi template:

- It is the only sample that is model-agnostic on the OpenAI Chat Completions
  endpoint every sponsored model exposes (Gemini, Gemma, GPT-OSS, GLM, Llama,
  Claude). `pi-opus`/`hermes-opus` wrap Node agents tuned for Claude's native
  API, and `agy-gemini` is a closed CLI. Swapping models in the template would
  mean re-validating a third-party agent; here it is one YAML line.
- It already has the expensive-to-get-right plumbing: streaming (the egress
  proxy drops 30-minute idle connections), retry/backoff on 429/5xx/dropped
  streams without poisoning the history, context compaction, Gemini thought
  signatures and Claude cache marks passed through, deadline-aware wrap-up,
  MCP tool discovery with a catalog fallback for 500 KB tool lists.
- Its cost profile is the cheapest: no agent framework overhead, one system
  prompt, tool outputs clipped, old tool results trimmed. `plan-verify` was the
  other candidate; its planner/worker/verifier idea is folded in as a single
  verification + review round rather than three separate conversations (each
  extra conversation re-reads the brief and the files).

What `pi-opus`'s dev artifacts told us (public template, 29 tasks, Opus 5.5):
4 tasks scored 0 (two with no deliverable file at all), 8 more under 0.35. The
failures cluster as: deliverable never written or written at the wrong
path/format, spreadsheets without the asked sheets, stopping after a partial
answer, numbers that do not trace to the governing document. Those are
harness problems before they are model problems, and they are what the layers
below target.

## 2. Architecture

```
 evaluator  <== WebSocket, serve protocol v1 ==>  serve.py -> taskbridge.py (kit, unchanged)
                                                      |
                                                harness.solve()
                                                      |
   +------------------+   exec/read_file    +---------v----------+
   | task container   | <------------------ | 1 triage.py        |  find, tooling probe, read the brief
   | (files, python,  | ------------------> |   parsers.py       |  xlsx/docx/pdf/ifc/dxf parsed HERE
   |  MCP apps)       |                     +---------+----------+  -> compact inventory (~10-15k tokens)
   |                  |                               |
   |                  |                     +---------v----------+
   |                  |                     | 2 checklist.py     |  regex over the brief + task.md
   |                  |                     |   (+1 model call)  |  -> paths, zip members, sheets/cols, layers
   |                  |                     +---------+----------+
   |                  |   bash/read/write/  +---------v----------+      Chat Completions (stream)
   |                  | <-----------------> | 3 engine.Agent     | <--------------------------> model
   |                  |   edit, MCP tools   |   (custom-openai)  |   system + brief + inventory + checklist
   |                  |                     |   hooks.after_tools|   turn/time nudges: first draft by turn 35,
   |                  |                     +---------+----------+   wrap-up under 25 min
   |                  |                               | model answers without a tool call
   |                  |   read_file         +---------v----------+
   |                  | ------------------> | 4 verify.py        |  exists, non-empty, parses, sheets/columns/
   |                  |                     |   (parsers.py)     |  layers/members present, no uncached formulas,
   |                  |                     +---------+----------+  no template placeholders, text not trivial
   |                  |                               | issues -> back to 3 (<= MAX_REPAIRS rounds)
   |                  |                     +---------v----------+
   |                  |                     | 5 review round     |  one prompted self-check: re-derive governing
   |                  |                     |   (hooks.on_final) |  numbers, units, clauses, assumptions section
   |                  |                     +---------+----------+
   |                  |                     +---------v----------+
   |                  |                     | 6 final message    |  structured summary + harness-verified file list
   +------------------+                     +--------------------+  (fallback text always set: never a run without one)
```

Everything in 1, 2, 4 runs in the serve container on bytes fetched over the
protocol: it costs CPU, not tokens. The model never sees graders or our
artifacts; the harness never reads them.

## 3. What each component is meant to fix

| Failure type seen in agent runs | Component | Mechanism |
|---|---|---|
| Deliverable missing, at the wrong path, wrong extension, inside a folder instead of the zip root | checklist + verify | Paths parsed from the brief and task.md (27/33 dev-set file deliverables by regex alone; the model refinement call fills the rest). After the model says "done", every path is read back; missing/empty/misplaced items go back as a to-do list, up to `MAX_REPAIRS` rounds. |
| Sheet or column names drift (`Schedule` vs `schedule`, a missing `Summary` sheet) | checklist + verify | Sheet/column specs parsed per workbook, including members inside a zip (`member.xlsx::Sheet`). Case-sensitive comparison with the hint "found `zones`". |
| Workbook holds formulas; the grader reads values with openpyxl `data_only=True` and sees `None` | verify | Counts formula cells without cached values and tells the model to write computed numbers. |
| Word memo built on a template with `[Project]`, `[Date]`, `TBD` left in | verify | Placeholder scan of the docx text. |
| Stopping early with a partial answer or a plan instead of files | engine hooks | A tool-free answer is not accepted until verification passes; turn-35 nudge to have a first complete version on disk (the briefs ask for it); wrap-up nudge under 25 minutes. |
| Running out of time or turns without `done` | engine + taskbridge | `MAX_TURNS` cap, `FINISH_MARGIN`, deadline-clamped exec timeouts, `cancel` handled by taskbridge with the harness's running fallback message (tested: `done` 2 s after cancel during a stalled model call). |
| Misread callouts: titles read, text entities and schedules not | triage + system prompt | DXF summaries list layers, entity counts and the first text entities; xlsx summaries give sheet headers and sample rows; PDFs give outline and first-page text, and flag scanned PDFs without a text layer. The prompt says callouts and notes carry the numbers. |
| Wrong code clause / superseded revision used | triage + review | Archive/superseded folders are listed (deprioritised in summaries); the review round asks for the clause each governing value comes from and that no superseded revision was used; the final-message structure has a "Basis" section the rubric judge reads. |
| Unit and order-of-magnitude errors | system prompt + review | Compute with code, carry units, sanity-check magnitudes against values stated in project documents; review re-derives the 3-5 governing numbers. |
| Wasted turns discovering tooling (`ModuleNotFoundError: pdfplumber` x10 in the public trajectories) | triage | One exec reports available Python modules and CLIs before the first model turn. |
| Weak closing summary (rubric judges read it) | final message | Structured prompt (deliverables, key results with units, basis, assumptions, checks, open items) plus a harness-written list of files it opened and parsed. |
| Cost: re-reading the brief and files every turn | engine | Stable prefix (system, brief, inventory, checklist) so Gemini's implicit cache hits; Claude cache marks kept; tool results clipped to 24k chars, older ones trimmed, notes-compaction past `COMPACT_AT_TOKENS`. |
| Endpoint rate limit or outage ends the run with nothing (seen: 429 on every call, 5 retries, `done` with no files, score 0) | engine | 408/409/429/5xx and cut streams are retried for as long as the task has time: `RETRY_WAITS`, then `RETRY_CAP_SEC` with jitter, `Retry-After` honoured; `MIN_CALL_INTERVAL_SEC` paces calls on per-minute quotas; `RETRY_BUDGET_SEC` bounds optional calls only. A refused request ends the loop with the fallback summary, never with a crash. |
| Opening context too big for small quotas and cheap caching (13.5k tokens on a 37-file task) | triage + harness | Every part of the inventory is capped (`TRIAGE_*_CHARS`), each cut annotated with how to read more; `fit_context` trims brief and inventory to `CONTEXT_BUDGET_TOKENS` (default 10k) and logs the estimate. |
| Failed run leaves no deliverable at all | harness + verify | After a failed loop the harness writes an honest "not produced" placeholder at each missing path (text/docx/xlsx/pdf/zip only), re-verifies, and reports it as a PLACEHOLDER gap in the final message. Rules: `docs/rules.md` ("deliverables already written before the failure are still graded", "do not hard-code task answers": the file holds none) and `docs/artifacts.md` ("nothing to grade" scores 0, the same as a placeholder). It changes no score; it leaves a record. `PLACEHOLDER_DELIVERABLES=0` disables it. |

## 4. Generality

The harness carries no task-specific content. The checklist is regex over
generic brief phrasing ("save it as", "### 1. `file.xlsx`", "sheet `X`: `a`,
`b`") and the model's own reading of the brief. Verification rules are format
rules (a workbook has the named sheets; a docx has no template placeholders;
an IFC has products). The engineering guidance in the prompts is method
(governing documents, units, magnitudes, basis), not values. The mock smoke
task in `tests/` is synthetic. The checklist regression (`tests/test_checklist.py`)
reads the public template's trajectories from a sibling clone by path and
copies nothing into this repository; dev-task data lives in `~/ple/data`,
outside the entry, and is never committed. No training or fine-tuning on task
data.

## 5. Model access and local development

- Submission: `model_api: google-cloud/<id>`; the evaluator injects
  `MODEL_BASE_URL`/`MODEL_API_KEY`/`MODEL_NAME`; `engine.py` reads them.
- Local dev with an AI Studio key: set `model_api: gemini/gemini-3.8-flash`
  (or `gemini/gemma-4-26b-a4b-it`) in the working tree and put `GEMINI_API_KEY`
  in `local.env`; the kit hands the harness the Gemini OpenAI-compatible URL.
  Outside the kit, `engine._endpoint()` also accepts `GOOGLE_GEMINI_BASE_URL`
  + `GEMINI_API_KEY` alone. Revert `model_api` before committing: a `gemini/`
  entry is not accepted for submission and `kit check` never says "ready".
- Judge for local dev runs: `eval/run.py` needs an OpenAI-compatible judge.
  Options, in the order the kit tries them: `JUDGE_BASE_URL` + `JUDGE_API_KEY`
  (an AI Studio key works: `JUDGE_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`,
  `JUDGE_MODEL=gemini-3.8-flash`; scores then approximate the leaderboard's
  Opus 5.5 judge), or `MODEL_PROXY_URL` (kit/model_proxy on Vertex, billed).
  For a smoke test, pick `checks`-graded tasks (no judge needed), e.g.
  `wind-diaphragm-shear`, `roof-deck-design`, `lv-cable-sizing` (checks-judge),
  `data-center-site-model`.
- Free-tier observations (AI Studio, 2026-11-10): see section 9.

## 6. Cost model and switches

Per task on Flash: ~1 checklist call (~15k in, ~1k out), N work turns with a
cached prefix of ~15-20k tokens, 1-3 verification/review rounds. The sample
`minimal`-style loop on Flash cost about $2.4/task on the dev leaderboard
(claude-code harness); we expect $1-3 with `MAX_TURNS=160`. Switches
(`submission.yaml` `env`): `TRIAGE`, `CHECKLIST_LLM`, `ENG_REVIEW`,
`MAX_REPAIRS`, `MAX_TURNS`, `FIRST_DRAFT_TURN`, `TOOL_RESULT_CHARS`,
`COMPACT_AT_TOKENS`, `REASONING_EFFORT`, `MAX_OUTPUT_TOKENS`,
`CONTEXT_BUDGET_TOKENS` (10000), `TRIAGE_INVENTORY_CHARS` (18000),
`TRIAGE_BRIEF_CHARS` (9000), `TRIAGE_SUMMARY_CHARS` (11000), `TRIAGE_FILE_CHARS`
(650), `TRIAGE_LIST_CHARS` (5000), `RETRY_WAITS` (5,15,30,60,120),
`RETRY_CAP_SEC` (120), `RETRY_BUDGET_SEC` (0 = the task's time),
`MIN_CALL_INTERVAL_SEC` (0), `PLACEHOLDER_DELIVERABLES` (1). For
`gemma-4-26b` set `COMPACT_AT_TOKENS: "90000"` and `MAX_OUTPUT_TOKENS: "8000"`;
if it rejects `reasoning_effort` the engine drops it after the first 400.
On a free AI Studio key with a per-minute token quota (Gemma: 16k input
tokens/min), add `MIN_CALL_INTERVAL_SEC: "60"` and `CONTEXT_BUDGET_TOKENS:
"8000"`: one call per quota window, each under the window.

## 7. Ablation plan (about $170 on Flash 3.8 through Vertex; nothing on the free tier)

The free AI Studio tier is not usable for real runs (section 9: Flash 503s
and stalls, Gemma's 16k-tokens/min quota 429s every turn, Argon has no free
quota). Every step below needs paid Vertex or organiser credits; the mock
suite (`tests/run_smoke.sh`) is what runs for free.

Cost basis (state it in the write-up, replace with measured `results.json`
token counts as soon as A0 runs): the kit's dev leaderboard puts the
claude-code sample on Flash at $2.4/task and the hidden-test Flash baseline at
about $2.6/task. ple-sitecheck's opening prefix is now capped at ~10k tokens
and cached, tool results clipped, turns capped at 160, so the estimate is
$1.0-2.5/task on Flash; the table uses $1.8. Gemma 4 26B on Vertex is roughly
a tenth of that per token: $0.3/task. The six S6 tasks are smaller than the
dev-set mean, so S6 steps are costed at 0.85 x the per-task figure.

Subset S6 (cheap, mixed graders, every deliverable type): `wind-diaphragm-shear`
(zip: xlsx+dxf+pdf+md, checks), `roof-deck-design` (zip, checks),
`lv-cable-sizing` (zip, checks-judge), `data-center-site-model` (IFC, checks),
`fill-compaction-acceptance` (docx memo, rubric, MCP world),
`fire-test-validation-memo` (docx under output/, rubric). Add
`data-hall-mep-routing` (solution.py, checks) if budget remains.

| Step | Config | Tasks | Est. cost (Flash via Vertex) | Measures |
|---|---|---|---|---|
| S0 | `kit check` on `google-cloud/gemini-3.8-flash` (build, model probe, smoke, MCP) | smoke | $0.10 | the real endpoint works through `kit/model_proxy` |
| A0 | full harness (all switches on) | S6 | 6 x $1.5 = $9 | reference point; $/task from token counts |
| A1 | `ENG_REVIEW=0 MAX_REPAIRS=0` (no verify loop, no review) | S6 | $8 | value of verify + review (A0 - A1) |
| A2 | `TRIAGE=0 CHECKLIST_LLM=0` (bare loop + prompt) | S6 | $9 (more tool calls, no cheap prefix) | value of triage + checklist (A0 - A2) |
| A3 | `REASONING_EFFORT=medium` | S6 | $6 | cost/score trade of reasoning effort |
| A5 | `MAX_TURNS=40` and `80` (two runs) | S6 | $4 + $7 | the score-vs-compute curve for Figure 2 |
| A4 | `gemma-4-26b`, full harness (cross-model point) | S6 | $2 | second model for criterion 3 |
| F1 | full pass, best config, `google-cloud/gemini-3.8-flash` | 29 tasks | 29 x $1.8 = $52 | the artifacts committed in the entry |
| F2 | second full pass (variance) if F1 is within budget; else the 12 lowest tasks | 29 or 12 | $52 or $22 | run-to-run variance for the failure breakdown |
| F3 | `gemma-4-26b` full pass (29 tasks) | 29 tasks | $9 | the cross-model table on the whole dev set |
| J | LLM judge for rubric and checks-judge tasks in local runs (Flash as judge, `JUDGE_MODEL=gemini-3.8-flash`) | all judge tasks x runs | $5-10 | approximate dev scores (the organisers re-score with Opus 5.5) |

Order: S0, A0, A4 first ($11: one real point per model, enough for a
minimal write-up), then A1-A3 and A5 ($34), then F1/F3 ($61), then F2 and J
with what is left. Keep A0-A5 under $40 so that two full passes (the
artifacts you commit and a variance check) fit in about $170. Report mean
score, $/task from `results.json` token counts, and the per-component delta
(A0-A1 = verify+review, A0-A2 = triage + checklist, A0-A5 = turn budget). The
Stage-1 write-up (`writeup/draft.md`) needs the architecture diagram
(`writeup/fig1_architecture.png`, done), the table in section 3 with measured
deltas, and the score-vs-cost point of each config next to the dev
leaderboard points (Opus 0.551 @ $17, Argon-pi 0.471 @ $5.3, Flash-cc 0.303 @
$2.4, Gemma ~0.04 @ $0.2-0.5).

## 8. Commands

Prerequisites on an x86 machine (or the aarch64 VM with Podman + qemu
emulation, section 9): Docker (or Podman with `DOCKER_HOST` on its local unix
socket), `uv`, Harbor 0.20.0 (`uv tool install harbor==0.20.0 --with websockets`),
HF access to the gated dataset (`HF_TOKEN`), and either an AI Studio key or a
Vertex project for `kit/model_proxy`.

```bash
# one-time
git clone https://github.com/google-research/ple kit-src
cat > kit-src/local.env <<'EOF'
GEMINI_API_KEY=...                 # AI Studio, for gemini/<model> dev runs
JUDGE_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
JUDGE_API_KEY=...                  # same key
JUDGE_MODEL=gemini-3.8-flash
HF_TOKEN=...
# or, for google-cloud/<id> through Vertex (billed):
# MODEL_PROXY_URL=http://localhost:4000
# MODEL_PROXY_KEY=...
EOF

# mock smoke test (no model, no key, no Docker): unit checks, checklist
# regression, normal / planner / cancel / budget / rate-limit / dead-endpoint /
# retry-budget scenarios
cd harness && tests/run_smoke.sh            # SKIP_CHECKLIST=1 without the template clone

# figure 1 for the write-up (matplotlib in a separate venv; not a harness dependency)
uv venv --python 3.12 ../wvenv && uv pip install --python ../wvenv/bin/python matplotlib
../wvenv/bin/python writeup/fig1_architecture.py

# kit check (builds the bundle for linux/amd64, model probe, smoke task, MCP task)
cd kit-src
sed -i 's#^model_api: .*#model_api: gemini/gemini-3.8-flash#' ../harness/submission.yaml
uv run python -m kit check ../harness --env-file local.env [--cancel-test]

# free-tier dev run (Gemma, 16k input tokens/min): pace the calls and shrink the prefix;
# expect ~1 turn/min, so use few turns and a checks-graded task
MIN_CALL_INTERVAL_SEC=60 CONTEXT_BUDGET_TOKENS=8000 MAX_TURNS=40 CHECKLIST_LLM=0 \
  uv run python eval/run.py ../harness --env-file local.env --no-export --trials 1 --tasks wind-diaphragm-shear

# ablations (paid, section 7): one subset, one config per run; --no-export keeps artifacts/ clean.
# The switches are env vars read by the harness: put them in submission.yaml `env`
# (what the evaluator will use) or export them for the run.
S6=wind-diaphragm-shear,roof-deck-design,lv-cable-sizing,data-center-site-model,fill-compaction-acceptance,fire-test-validation-memo
run() { uv run python eval/run.py ../harness --env-file local.env --no-export --trials 1 --tasks $S6 --out runs/$1; }
run A0-flash
ENG_REVIEW=0 MAX_REPAIRS=0        run A1-flash
TRIAGE=0 CHECKLIST_LLM=0          run A2-flash
REASONING_EFFORT=medium           run A3-flash
MAX_TURNS=40                      run A5-40-flash
MAX_TURNS=80                      run A5-80-flash
sed -i 's#^model_api: .*#model_api: google-cloud/gemma-4-26b#' ../harness/submission.yaml
COMPACT_AT_TOKENS=90000 MAX_OUTPUT_TOKENS=8000 run A4-gemma
# $/task: sum input/output/cached tokens per task from runs/<cfg>/results.json x Vertex list prices

# the final full pass (writes ../harness/artifacts/); model_api must be google-cloud/<id>
sed -i 's#^model_api: .*#model_api: google-cloud/gemini-3.8-flash#' ../harness/submission.yaml
uv run python eval/run.py ../harness --env-file local.env            # 29 tasks
uv run python -m kit artifacts ../harness && uv run python eval/run.py --artifacts ../harness --env-file local.env
git -C ../harness add -A && git -C ../harness commit -m "dev artifacts (gemini-3.8-flash)"
uv run python -m kit check ../harness --env-file local.env          # must end with "ready to submit"
```

Note: `eval/run.py` sizes a slot at 6 CPUs / 16.25 GB for the full dev set;
the 2-core VM can only run one small task at a time (`--trials 1`) and the
12 MCP "world" tasks build ~17 GB of images. Use the x86 machine for the full
pass.

## 9. Smoke-test record

- Mock end-to-end (`tests/run_smoke.sh`, no model): PASS. Normal scenario:
  triage (2 exec, 4 read_file) -> checklist (1 call) -> 2 work turns -> model
  claims done -> verify finds 2 issues (memo missing, 1 uncached formula) ->
  repair turn -> verify clean -> done; 6 model calls, usage frames reported.
  Cancel scenario: evaluator cancels at 6 s during a stalled 60 s model call,
  `done` with the fallback message arrives 2 s later.
- Checklist regression over the 29 public dev instructions: 27/33 file
  deliverables by regex (the 6 misses are relative paths the test cannot
  resolve without each task's real workdir), zip members and sheet/column
  specs recovered for the structural and electrical zip tasks.
- Triage dry-run on a real dev task's files (`tests/triage_dryrun.py`,
  37 files, 1.1 s): brief 9 k chars, inventory 35 k chars, whole context
  message ~13.5 k tokens; IFC/DXF/xlsx/docx/pdf all parsed.
- Budget scenario (`MAX_TURNS=2`): budget ends with the memo missing ->
  `on_budget` verifies, grants 12 grace turns with the missing list -> memo
  written -> verify clean -> done.
- Real model via AI Studio free tier (2026-11-10 02:14-02:27 CET, mock
  evaluator, synthetic task, `CHECKLIST_LLM=0 ENG_REVIEW=0 MAX_TURNS=6`):
  - `gemini-3.8-flash`: first call OK (tool call `read_file`, usage
    1746 in / 20 out), then HTTP 503 "This model is currently experiencing
    high demand" twice in a row (backoff 5 s, 15 s), then the third attempt
    stalled until the evaluator's 480 s wait ran out. No 429 seen, so the
    quota (RPM/RPD) was not the limit; capacity was. For dev runs, treat 503s
    as normal and keep `RETRY_WAITS`; or use Vertex through `kit/model_proxy`.
  - `gemma-4-26b-a4b-it`: 7 calls in 85 s, no 429, one HTTP 500 INTERNAL
    retried successfully; `prompt_tokens_details.cached_tokens` reported
    (4,899 of 21,823 prompt tokens cached: implicit caching works through
    the OpenAI endpoint); `reasoning_effort=high` accepted; tool calls and
    streaming parsed correctly. The model itself imported `pandas` after the
    probe said it was missing, burned the 6-turn budget and wrote nothing;
    the harness's final message listed the gaps honestly. This is the case
    the grace-turn rule now covers.
- Podman on the aarch64 VM (Ubuntu 24.04, podman 4.9.3, podman-docker,
  qemu-user-static 8.2.2, rootless `podman.socket`,
  `DOCKER_HOST=unix:///run/user/1000/podman/podman.sock`): the kit accepts
  the unix socket as a local daemon (`build.remote_daemon` only rejects
  tcp/ssh/http); `docker-compose` is provided by the package.
  `podman run --platform linux/amd64 debian:bookworm` works (3.6 s incl. pull).
  `kit build` built the builder image (803 MB, 4.5 min) but our `install`
  failed with exit 139: the x86-64 `uv` binary segfaults under qemu 8.2.2
  (`QEMU internal SIGSEGV {code=MAPERR, addr=0x20}`), reproducible with
  `podman run --platform linux/amd64 plebench-builder /opt/harness/bin/uv --version`,
  while Debian `python3` and Node 22 run fine. So the kit's prescribed
  `uv venv` install, `kit check` and `eval/run.py` cannot run on this VM with
  the apt qemu. Options (not done; outside the approved scope): a newer
  qemu-user-static (>= 9.x, e.g. the `tonistiigi/binfmt` privileged
  container or a trixie .deb), or an x86 machine. Set `BUILDAH_FORMAT=docker`
  when running the kit under Podman (the builder Dockerfile's `SHELL` line is
  ignored in OCI format).

### First real dev run (laptop, WSL docker-ce, x86, gemma-4-26b-a4b-it, 2026-11-10)

`kit check` passed every stage (build 293 MiB, model, listen, smoke 320 s,
mcp). `eval/run.py --tasks wind-diaphragm-shear` scored 0: 40 steps, 477 s,
46k prompt tokens, `done` with no file written. Three harness bugs, now fixed
with regressions in `tests/test_units.py` and `tests/run_smoke.sh` (2b):

1. A tool-free "plan" turn was accepted as the answer after the repair rounds
   were used up by earlier plan turns. Now a tool-free answer while required
   deliverables are MISSING/EMPTY/CORRUPT gets a bounded "continue, use your
   tools, these files do not exist" nudge (`MAX_CONTINUES`, default 4) that
   does not consume repair rounds; repairs apply only to shape issues of
   existing files; grace turns still apply at the turn budget; verify runs
   before any `done`.
2. `read_file` returned raw bytes for PDFs (no NUL in the first 8 KB) and
   other documents. Now pdf/docx/xlsx/pptx come back as extracted text
   (pages, paragraphs, TSV rows) and ifc/dxf/zip as structured summaries,
   all cut at `TOOL_RESULT_CHARS` with a note on how to read more via python
   in bash. The harness logs the sizes of the brief, inventory and checklist
   it put in front of the model.
3. Gemma's `<thought>...</thought>` leaked into content and the final
   message. `visible()` now strips `<think>`, `<thought>` and `<thinking>`
   blocks (also unterminated ones); stored assistant turns and the final
   message carry only the visible text.

### Free-tier run on a real dev task (laptop, gemma-4-26b-a4b-it, 2026-11-10 05:10)

`eval/run.py --tasks wind-diaphragm-shear` scored 0 for a harness reason: the
free tier allows 16k input tokens per minute for Gemma 26B, the opening
context was ~13k tokens and grew, so every call got HTTP 429; the five-step
retry schedule ran out and the harness sent `done` with no files. Fixed in
the commit after 7a83ab5, with mock regressions (`tests/run_smoke.sh` 5-7,
`tests/test_units.py` 4-6):

1. Retries never run out while the task has time (`retry_wait`: schedule,
   then `RETRY_CAP_SEC` with jitter; `Retry-After` honoured;
   `MIN_CALL_INTERVAL_SEC` to pace calls on a per-minute quota). Scenario 5:
   six 429s in a row, the run waits and finishes with every file. Scenario 6:
   429 on every call for a 40 s budget: 15 attempts, no early finish, then
   the harness verifies, writes placeholders and sends the summary 7 s
   before the deadline. Scenario 7: a `retry_budget` (used by the optional
   checklist call) gives up on time.
2. The opening context is budgeted: inventory capped at 18k chars (listing
   5k, summaries 11k, 650 chars per file), brief at 9k, and `fit_context`
   trims to `CONTEXT_BUDGET_TOKENS` (10k) with a logged estimate. Dry runs
   on the three local dev tasks (34-51 files): inventory 15.6k / 17.5k /
   17.7k chars (was 33-35k), brief file 9.1k chars where one exists; with
   the ~8k-char instruction and the 3k-char system prompt the opening turn
   estimates at 10-10.5k tokens and `fit_context` trims it to the 10k
   budget (set 8000 on a 16k-tokens/min quota).
3. A failed run leaves an honest placeholder at each missing deliverable path
   and says so in the final message (section 3, last row).

Still unverified without a paid run: that the Gemini OpenAI endpoint sends
`Retry-After` on 429 (the code works without it), the real token estimate of
the trimmed prefix on Flash, and whether a 60 s pacing makes the free Gemma
tier usable end to end (40 turns would take 40 minutes).

## 10. Open risks

- `ifcopenshell==0.9.0.post1` and `pymupdf==2.1` are large manylinux wheels.
  The pinned set installs on aarch64 in under a minute; the amd64 install in
  the real builder image is still unverified (section 9: `uv` segfaults under
  this VM's qemu). First thing to run on an x86 machine: `kit build`.
- The task containers' Python differs per task (some lack pdfplumber, pypdf,
  pandas); the harness parses in the serve container so the model needs only
  what the probe reports.
- Rubric tasks with `/filesystem/` or `/app/output/` folder deliverables:
  verification can only check that new files exist there; the model refinement
  call usually names the exact file.
- Gemini thought signatures must round-trip through the OpenAI endpoint; the
  engine keeps every extra field of a tool call (inherited from the sample).
- Free-tier rate limits make dev runs slow (the engine waits them out and can
  pace calls); for the full pass use Vertex through `kit/model_proxy`.
- Waiting out an outage for the whole 6-hour budget costs wall time, not
  money; `RETRY_BUDGET_SEC` bounds it if the evaluator's own retry policy
  ("sponsored-endpoint rate limits or outages retried without penalty") turns
  out to prefer an early `error` frame.
- Placeholders change no score and are flagged as gaps; if a grader ever read
  a placeholder docx as a submitted memo, the file's only sentence says it is
  not one. Set `PLACEHOLDER_DELIVERABLES=0` to switch them off.

## 11. Gemma 4 26B: what a weak model does with the loop, and the switches for it

Measured on the first Gemma 26B dev-task run (self-hosted Q8 llama.cpp, one
checks-graded zip task, 751 s, 47 turns / 73 tool calls, 2.65 M prompt tokens
of which 94% cache hits, 55 k output tokens, score 0.0; every content gate of
the grader failed although the zip parsed and held every member). The
trajectory, read generically:

| Failure mode | Count | Share |
|---|---|---|
| Blind read sweep: `read_file` turns with no visible text, no notes, no write | 42 of 73 calls; longest streak 24 reads in a row | 58% of calls |
| Reads that hit the 24 k-char clip (the model saw a head and never came back for the rest with python) | 15 of 42 | 36% of reads |
| Repeat reads of a file already read in full (forgotten or cut from context) | 12 of 42 | 29% of reads |
| Reads of files the inventory marked superseded | 3 of 42 | 7% |
| First write of anything: turn 30 (brief asks for a complete first draft early) | turn 30 of 47 | 64% of the run before any output |
| Build loop with no inspection: write script -> run -> `ls`/`unzip -l` -> rewrite, never opening a produced value | 8 script rewrites, 3 zip listings, 0 value checks | last 20 turns |
| Hard-coded constants typed from memory in the analysis script (no source), register template filled with 1 row | n/a | all 15 gates + register voided |
| Tool errors (missing module the probe had listed as missing; a NameError) | 2 of 23 execs | 9% |
| Shape verification passed (members, sheets, parse) while every content gate failed | 6 ok / 0 issues | verify was blind to template rows and keys |
| Visible model text (plans, governing-document list the rules asked for) | 0 of 74 turns | everything stayed in stripped thought blocks |

Three earlier free-tier runs (40 turns each) show the same shape: 34-35 of
40 calls were reads, no file written.

So the model is not short of turns or context (131 k window, 160 turns): it
reads without recording, forgets, re-reads, then guesses. The sample-harness
loop assumes a model that plans in its visible text; Gemma plans in thought
blocks that are stripped and never persisted. The switches below are the
harness's answer, all off unless set so they can be A/B tested:

| Switch (env) | Default | What it does | Targets |
|---|---|---|---|
| `NOTES_PHASE=1` (+ `READ_SWEEP_TURNS`, default 6) | off | Working method in the opening: a notes file (governing documents, every input value with unit and source, each deliverable's structure from the brief and templates, the checks). The hooks watch `write_file`/`edit_file`/`bash` for the notes path; `READ_SWEEP_TURNS` turns without touching it append a nudge to the tool result. The review round reads the notes back and asks the model to check every value against them (or to write them, if it never did). | blind sweep, forgotten reads, constants from memory, invisible plans |
| `VERIFY_TEMPLATE=1` | off | `checklist.match_templates` pairs each deliverable / zip member with a template file of the same format in the workspace (name or folder says template/example/sample/blank/form; same stem, else the only one of that format, else difflib >= 0.6). `verify` then compares: template sheets and columns present, the template's row keys (column A) present, drawing layers, document headings and tables, CSV columns. Format rules only; no values. | shape check passing while the register/schedule is empty |
| `READ_REPEAT_CHARS=N` | 0 (off) | A second `read_file` of the same path returns the first N characters and a note to extract the needed part with python and record it in the notes. | repeat reads (29% of reads) |
| `COMPACT_KEEP_OPENING=1` | off | Notes-compaction keeps the whole opening (instruction, brief, checklist, inventory) in front of the notes, not only the system prompt and instruction. | requirements lost at compaction on long runs |
| `TEMPERATURE=x` | unset | Sends `temperature` with every call (dropped if the endpoint refuses it). | tool-call and number precision on a sampled small model |

Regressions: `tests/test_units.py` 7-11 (template pairing and comparison for
xlsx/docx, repeat-read cut, temperature, compaction keeping the opening, the
notes nudge) and `tests/run_smoke.sh` scenario 8 (the normal mock scenario with
every switch on: all files written, the memo compared with its template).

A/B plan for the next Colab session (Gemma 26B, 2 slots): 8 checks-graded or
checks-judge dev tasks with templates and zip/xlsx deliverables, each run
twice: A = current defaults, B = `NOTES_PHASE=1 VERIFY_TEMPLATE=1
READ_REPEAT_CHARS=4000 COMPACT_KEEP_OPENING=1 TEMPERATURE=0.3
TOOL_RESULT_CHARS=12000`. Compare score, reads per run, repeat reads, turn of
first write, and tokens per task from `results.json`; then one ablation of B
without `NOTES_PHASE` to split the notes effect from the verify effect.

## 12. Variant C: where the first real runs lost their points, and the switches for it

Four graded dev runs (two checks-graded structural tasks, Gemma 4 26B Q4 and
a 30B-A3B hosted model, variant A, 2026-11-11/12) read generically:

| Loss class | Runs | Evidence |
|---|---|---|
| A designation (product code, type, pattern, count per unit) written into a table that the project's catalogue does not tabulate; a checks grader then cannot analyse the design at all | 4 of 4 | about half of the hard rubric families void on one wrong string; design-basis constants typed into scripts from memory and re-derived differently by the grader |
| Finish with the budget unused: a tool-free answer at 60-77 of 160 turns, 12-16% of the 6 h, right after the shape verify said "0 issues"; the one review round changed 3-8 turns of work | 3 of 4 | verify counts sheets and rows, not content |
| Library API guessed by trial and error (DXF/IFC entity access): 12-34 consecutive failing bash calls per run, 50-84% of all bash calls in the small-model runs; the geometry they were after never came out | 4 of 4 | layout and geometry checks failed for want of coordinates the drawings and the model held |
| Thin deliverables that pass the shape check: 2-4 data rows per sheet, a narrative under 1000 characters, a 1-page calculation PDF, a drawing with an empty required layer | 3 of 4 | "6 ok, 0 issues" on all of them |

The switches, all off unless set (A stays unchanged):

| Switch (env) | Default | What it does | Targets |
|---|---|---|---|
| `PROVENANCE_CHECK=1` | off | At verify time the harness collects the designation-like cell strings of every tabular deliverable (xlsx, csv, docx tables, DXF text; row-key and identifier columns skipped), builds a token corpus from the project's input files once (same parsers, plain and separator-squashed tokens), and reports the strings found in no document as `UNSOURCED` issues, which start a repair round (bounded by `MAX_REPAIRS`; each token flagged once). One working rule in the opening announces the check. Knows no values, only whether a string the model wrote exists in the inputs. | invented catalogue combinations, constants from memory |
| `FINISH_GATE=1` (+ `FINISH_GATE_PASSES` 2, `FINISH_GATE_TURN_FRACTION` 0.5, `FINISH_GATE_MINUTES` 45) | off | A tool-free answer with under half the turns used and over 45 minutes left gets a second-pass prompt with the substance report (data rows per sheet, characters per text, pages per PDF, entities per DXF layer, thinnest first) and the budget left; up to 2 passes, each only if the model did tool work since the previous one. Comes after the repair rounds and the review round. | early finish, thin deliverables |
| `IO_HELPERS=1` (+ `ERROR_STREAK` 3, `HELPER_PATH`) | off | `ple_io.py` (DXF entities with coordinates by layer, IFC entities with placement and property sets, workbook rows, PDF text and tables by page, DOCX, CSV, grep across formats; CLI and importable) is written into the task container before the first turn and its commands listed in the opening. Three consecutive failing bash calls append a hint to look the API up or use the helper. | API trial and error, geometry never extracted |

Regressions: `tests/test_units.py` 12-14 (designation filter, corpus lookup,
token collection from workbooks and zip members, identifier columns skipped,
substance report order, gate passes and bounds, streak hint, the hooks end to
end) and `tests/run_smoke.sh` scenario 9 (the normal mock scenario with the
three switches on: helper written, corpus built, one second pass, every file
written, no UNSOURCED finding on sourced values).

A/C plan: `scripts/cs_run.sh` with `VARIANT=C` applies `CENV`
(default `PROVENANCE_CHECK=1 FINISH_GATE=1 IO_HELPERS=1`); run the same tasks
as A on the same model and compare score, turns used, failing bash calls,
UNSOURCED findings per run (from the log) and tokens per task.

## 13. Variant D: one variant-A run read step by step, and the switches for it

One checks-graded run on the hosted 30B-A3B model (variant A, 2026-11-12:
117 of 160 turns, 62 of 360 minutes, 147 tool calls, 34 failing) read step by
step, next to the four runs of section 12:

| Where the turns went | Turns | What it cost |
|---|---|---|
| Writing a drawing with named layers: guessed `set_layer`, `set_location`, `set_text`, `TextEntityAlignment`, `layers.new(color=)`, `Modelspace.layers`, then a hand-written layer table that broke the file | 50 of 147, 17 tracebacks | the only repair rounds of the run; the drawing was rebuilt five times |
| Reading drawings: `ezdxf.read(path)` and five more guesses before splitting the DXF text by hand | 12, 10 tracebacks | the coordinates it was after never came out (as in the four earlier runs) |
| Re-reading 12 input files already read, after the first context cut | 12 | tokens only |
| The harness itself: the verifier read every ezdxf-written drawing as EMPTY (`ezdxf.read` on a plain `StringIO` of a CRLF file returns an R12 document with no layers and no entities and no error), so it reported "lacks layers" on correct files and kept the model in the loop above | - | fixed in `parsers.parse_dxf` and `ple_io.dxf_doc` for every variant (universal newlines; regression in `tests/test_units.py` 15) |

What the grader then penalised that a format check could have caught: a
template column with a header and no values in any row; a key whose name ends
in a unit holding a text such as `12 x 8`; the same row key twice in a
table. And what only the model can catch when its own numbers are shown back
to it in one place: two keyed values swapped between neighbouring keys, and a
demand column three orders of magnitude below its capacity column.

The switches, all off unless set (A, B and C stay unchanged):

| Switch (env) | Default | What it does | Targets |
|---|---|---|---|
| `IO_WRITERS=1` (needs `IO_HELPERS=1`) | off | `ple_io.py` gains tested writers: `dxf_write` (entities as plain dicts, every named layer created in the layer table, `$INSUNITS` set), `xlsx_write` (values, header row first), `zip_write` (files at the root), and the CLI commands `write-dxf SPEC.json OUT.dxf`, `write-xlsx SPEC.json OUT.xlsx`, `write-zip OUT.zip FILE...`; the opening lists them with a one-line example. | 50 turns of guessed write calls |
| `DXF_LAYER_CHECK=1` | off | A required drawing layer passes verification when entities sit on it, not when its name is in the table. The finding separates layers that exist and hold nothing from layers that do not exist, lists entities per layer, and gives the two ezdxf calls that fix it; entities on a layer missing from the table get their own line. | the layer-table loop; a required layer with nothing on it |
| `VALUE_CHECK=1` | off | At verify time (after the files parse, bounded by `MAX_REPAIRS`, each finding once): columns with a header and no values; cells holding text where the column header or the row key names a unit (`_plf`, `(mm)`, `K_FT`); duplicate row keys in an identifier column. Reported as `VALUES` issues that start a repair round; they stay out of the final message. Knows no values, only whether a cell holds one. | empty template columns, text under a unit key, duplicated rows |
| `READBACK=1` | off | The review prompt (and the second-pass prompt, with `FINISH_GATE`) opens with what the harness read back from the model's tables: key/value sheets in full, min..max of every unit-bearing column; the model is asked to check unit, order of magnitude and that each key holds the quantity its name says. | swapped keyed values, a column in the wrong unit, capacities typed from memory |

Regressions: `tests/test_units.py` 15 (writers round-trip through the readers
and the verifier's parser; CRLF and LF drawings parse the same; the layer
finding with the switch on and the unchanged check with it off; empty column,
unit-column text, unit-key text, duplicate keys, the clean case; the readback
text; the hooks: one repair round per finding, readback in front of the
review prompt, writer rules appended) and `tests/run_smoke.sh` scenario 10
(the mock scenario with the C and D switches on).

A/C/D plan: `scripts/cs_run.sh` with `VARIANT=D` applies `DENV` (default:
variant C's three switches plus `IO_WRITERS=1 DXF_LAYER_CHECK=1 VALUE_CHECK=1
READBACK=1`), so C against D isolates the D switches. Compare score, turns
spent on writes versus guesses (tracebacks per run), `values:` and `readback:`
log lines, and the grader's format findings.
