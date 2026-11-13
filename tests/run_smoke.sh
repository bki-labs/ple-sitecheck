#!/usr/bin/env bash
# Mock end-to-end smoke test: no real model, no API key, no Docker.
#   tests/run_smoke.sh            normal scenario + cancel scenario + checklist regression
#   tests/run_smoke.sh --real     also run the real-model smoke (needs MODEL_* in the environment)
set -uo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
WORK=tests/_work/app
MOCK_PORT=${MOCK_PORT:-18111}
SRV_PORT=${SRV_PORT:-18765}
VENV_BIN="$PWD/.venv/bin"
status=0
pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT

# wait until something listens on the port (without connecting: a bare TCP
# probe would make serve.py log a failed WebSocket handshake)
wait_port() { for _ in $(seq 1 60); do $PY -c "import socket,sys; s=socket.socket(); s.settimeout(0.2)
try: s.connect(('127.0.0.1',$1)); s.send(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'); s.close(); sys.exit(0)
except OSError: sys.exit(1)" 2>/dev/null && return 0; sleep 0.5; done; return 1; }

echo "### 0. unit regressions (thought tags, read_file on documents, checklist phrasing)"
$PY tests/test_units.py || status=1

echo; echo "### 1. checklist regression over the 29 dev-task instructions (needs the public template clone; SKIP_CHECKLIST=1 to skip)"
[ -n "${SKIP_CHECKLIST:-}" ] || $PY tests/test_checklist.py || status=1

echo; echo "### 2. normal scenario (mock model): triage -> work -> verify finds gaps -> repair -> review -> done"
rm -rf "$WORK"; mkdir -p "$WORK"
MOCK_WORKDIR="$PWD/$WORK" $PY tests/mock_model.py --port $MOCK_PORT & pids+=($!)
wait_port $MOCK_PORT || { echo "mock model did not start"; exit 1; }
MODEL_BASE_URL=http://127.0.0.1:$MOCK_PORT/v1 MODEL_API_KEY=mock MODEL_NAME=mock-flash PORT=$SRV_PORT \
  MAX_TURNS=20 $PY serve.py & pids+=($!)
wait_port $SRV_PORT || { echo "serve.py did not listen"; exit 1; }
$PY tests/fake_evaluator.py --port $SRV_PORT --workdir "$WORK" --setup --agent-timeout 3600 \
  --path-prepend "$VENV_BIN" --expect-usage --forbid-final "<thought>" \
  --expect output/report.md output/schedule.xlsx output/memo.docx \
  --expect-log "checklist: 3 deliverables" "nudge: continue 1" "verify: 3 ok, 0 issues" || status=1
kill "${pids[@]}" 2>/dev/null; pids=(); sleep 0.5

echo; echo "### 2b. planner scenario: two tool-free plan turns with <thought> blocks must be nudged, not accepted as done"
rm -rf "$WORK"; mkdir -p "$WORK"
MOCK_WORKDIR="$PWD/$WORK" $PY tests/mock_model.py --port $((MOCK_PORT+3)) --scenario planner & pids+=($!)
wait_port $((MOCK_PORT+3)) || { echo "mock model did not start"; exit 1; }
MODEL_BASE_URL=http://127.0.0.1:$((MOCK_PORT+3))/v1 MODEL_API_KEY=mock MODEL_NAME=mock-gemma PORT=$((SRV_PORT+4)) \
  MAX_TURNS=20 ENG_REVIEW=0 $PY serve.py & pids+=($!)
wait_port $((SRV_PORT+4)) || { echo "serve.py did not listen"; exit 1; }
$PY tests/fake_evaluator.py --port $((SRV_PORT+4)) --workdir "$WORK" --setup --agent-timeout 3600 \
  --path-prepend "$VENV_BIN" --forbid-final "<thought>" \
  --expect output/report.md output/schedule.xlsx output/memo.docx \
  --expect-log "nudge: continue 2" "nudge: continue 3" "read_file" "verify: 3 ok, 0 issues" || status=1
kill "${pids[@]}" 2>/dev/null; pids=(); sleep 0.5

echo; echo "### 3. cancel scenario: model call stalls, evaluator cancels after 6 s, done must arrive within 10 s"
MOCK_WORKDIR="$PWD/$WORK" $PY tests/mock_model.py --port $((MOCK_PORT+1)) --slow-sec 60 & pids+=($!)
wait_port $((MOCK_PORT+1)) || { echo "mock model did not start"; exit 1; }
MODEL_BASE_URL=http://127.0.0.1:$((MOCK_PORT+1))/v1 MODEL_API_KEY=mock MODEL_NAME=mock-flash PORT=$((SRV_PORT+1)) \
  CHECKLIST_LLM=0 $PY serve.py & pids+=($!)
wait_port $((SRV_PORT+1)) || { echo "serve.py did not listen"; exit 1; }
$PY tests/fake_evaluator.py --port $((SRV_PORT+1)) --workdir "$WORK" --setup --agent-timeout 900 \
  --path-prepend "$VENV_BIN" --cancel-after 6 --max-wait 30 || status=1
kill "${pids[@]}" 2>/dev/null; pids=()

echo; echo "### 4. budget scenario: MAX_TURNS=2 runs out before the memo exists; grace turns must get it written"
rm -rf "$WORK"; mkdir -p "$WORK"
MOCK_WORKDIR="$PWD/$WORK" $PY tests/mock_model.py --port $((MOCK_PORT+2)) & pids+=($!)
wait_port $((MOCK_PORT+2)) || { echo "mock model did not start"; exit 1; }
MODEL_BASE_URL=http://127.0.0.1:$((MOCK_PORT+2))/v1 MODEL_API_KEY=mock MODEL_NAME=mock-flash PORT=$((SRV_PORT+3)) \
  MAX_TURNS=2 CHECKLIST_LLM=0 ENG_REVIEW=0 $PY serve.py & pids+=($!)
wait_port $((SRV_PORT+3)) || { echo "serve.py did not listen"; exit 1; }
$PY tests/fake_evaluator.py --port $((SRV_PORT+3)) --workdir "$WORK" --setup --agent-timeout 3600 \
  --path-prepend "$VENV_BIN" --expect output/report.md output/schedule.xlsx output/memo.docx \
  --expect-log "granting" || status=1
kill "${pids[@]}" 2>/dev/null; pids=()

echo; echo "### 5. rate-limit scenario: the first 6 chat calls answer HTTP 429 (Retry-After 1); the run must wait and still finish with every file"
rm -rf "$WORK"; mkdir -p "$WORK"
MOCK_WORKDIR="$PWD/$WORK" $PY tests/mock_model.py --port $((MOCK_PORT+4)) --fail-count 6 --retry-after 1 & pids+=($!)
wait_port $((MOCK_PORT+4)) || { echo "mock model did not start"; exit 1; }
MODEL_BASE_URL=http://127.0.0.1:$((MOCK_PORT+4))/v1 MODEL_API_KEY=mock MODEL_NAME=mock-flash PORT=$((SRV_PORT+5)) \
  MAX_TURNS=20 ENG_REVIEW=0 RETRY_WAITS=1,1 RETRY_CAP_SEC=1 $PY serve.py & pids+=($!)
wait_port $((SRV_PORT+5)) || { echo "serve.py did not listen"; exit 1; }
$PY tests/fake_evaluator.py --port $((SRV_PORT+5)) --workdir "$WORK" --setup --agent-timeout 3600 \
  --path-prepend "$VENV_BIN" --max-elapsed 120 --forbid-final "PLACEHOLDER" \
  --expect output/report.md output/schedule.xlsx output/memo.docx \
  --expect-log "rate limited (HTTP 429)" "attempt 1, again in 2s" "succeeded after" "verify: 3 ok, 0 issues" || status=1
kill "${pids[@]}" 2>/dev/null; pids=(); sleep 0.5

echo; echo "### 6. dead-endpoint scenario: every call 429s for the whole 40 s budget; no early finish, then honest placeholders at the 3 paths and a summary"
rm -rf "$WORK"; mkdir -p "$WORK"
MOCK_WORKDIR="$PWD/$WORK" $PY tests/mock_model.py --port $((MOCK_PORT+5)) --fail-count -1 --retry-after 1 & pids+=($!)
wait_port $((MOCK_PORT+5)) || { echo "mock model did not start"; exit 1; }
MODEL_BASE_URL=http://127.0.0.1:$((MOCK_PORT+5))/v1 MODEL_API_KEY=mock MODEL_NAME=mock-flash PORT=$((SRV_PORT+6)) \
  CHECKLIST_LLM=0 RETRY_WAITS=1 RETRY_CAP_SEC=1 $PY serve.py & pids+=($!)
wait_port $((SRV_PORT+6)) || { echo "serve.py did not listen"; exit 1; }
$PY tests/fake_evaluator.py --port $((SRV_PORT+6)) --workdir "$WORK" --setup --agent-timeout 40 --max-wait 90 \
  --path-prepend "$VENV_BIN" --max-elapsed 45 \
  --expect output/report.md output/schedule.xlsx output/memo.docx \
  --expect-final "PLACEHOLDER: " "kept failing until time ran out" \
  --expect-log "no time left to wait" "placeholder deliverables written" "attempt 8" || status=1
kill "${pids[@]}" 2>/dev/null; pids=(); sleep 0.5

echo; echo "### 7. budget-capped one-shot: RETRY_BUDGET_SEC=3 with a dead endpoint must raise within the budget (unit, no server)"
$PY - <<'PY' || status=1
import asyncio, sys, time
sys.path.insert(0, ".")
import engine, openai
try:
  import httpx2 as httpx
except ImportError:
  import httpx
class S:
  workdir = "/app"; limits = {}; mcp_servers = []; instruction = ""; final_message = ""
  def time_left(self): return 3600
  async def log(self, t): print("   [log]", t[:120])
  async def usage(self, *a): pass
async def main():
  a = engine.Agent(S(), "sys", [])
  req = httpx.Request("POST", "http://x/v1/chat/completions")
  async def dead(*args, **kw):
    raise openai.APIStatusError("429", response=httpx.Response(429, request=req, headers={"Retry-After": "1"}), body=None)
  a.stream = dead
  t0 = time.monotonic()
  try:
    await a.complete(retry_budget=3)
  except openai.APIStatusError:
    el = time.monotonic() - t0
    ok = 1.5 <= el <= 6 and a.retries >= 1
    print(("PASS" if ok else "FAIL") + f": retry budget honoured ({el:.1f}s, {a.retries} retries)")
    return 0 if ok else 1
  print("FAIL: complete() returned"); return 1
sys.exit(asyncio.run(main()))
PY

echo; echo "### 8. small-model switches on (NOTES_PHASE, VERIFY_TEMPLATE, READ_REPEAT_CHARS, TEMPERATURE, COMPACT_KEEP_OPENING): the normal scenario must still finish with every file, the memo compared with its template"
rm -rf "$WORK"; mkdir -p "$WORK"
MOCK_WORKDIR="$PWD/$WORK" $PY tests/mock_model.py --port $((MOCK_PORT+6)) & pids+=($!)
wait_port $((MOCK_PORT+6)) || { echo "mock model did not start"; exit 1; }
MODEL_BASE_URL=http://127.0.0.1:$((MOCK_PORT+6))/v1 MODEL_API_KEY=mock MODEL_NAME=mock-gemma PORT=$((SRV_PORT+7)) \
  MAX_TURNS=20 NOTES_PHASE=1 READ_SWEEP_TURNS=2 VERIFY_TEMPLATE=1 READ_REPEAT_CHARS=3000 TEMPERATURE=0.3 COMPACT_KEEP_OPENING=1 \
  $PY serve.py & pids+=($!)
wait_port $((SRV_PORT+7)) || { echo "serve.py did not listen"; exit 1; }
$PY tests/fake_evaluator.py --port $((SRV_PORT+7)) --workdir "$WORK" --setup --agent-timeout 3600 \
  --path-prepend "$VENV_BIN" --expect-usage --forbid-final "<thought>" \
  --expect output/report.md output/schedule.xlsx output/memo.docx \
  --expect-log "templates: 1 deliverable(s) paired" "memo.docx <- memo_template.docx" "verify: 4 ok, 0 issues" || status=1
kill "${pids[@]}" 2>/dev/null; pids=(); sleep 0.5

echo; echo "### 9. variant C switches on (PROVENANCE_CHECK, FINISH_GATE, IO_HELPERS): helper installed, corpus built, second pass asked, every file written"
rm -rf "$WORK"; mkdir -p "$WORK"
MOCK_WORKDIR="$PWD/$WORK" $PY tests/mock_model.py --port $((MOCK_PORT+7)) & pids+=($!)
wait_port $((MOCK_PORT+7)) || { echo "mock model did not start"; exit 1; }
MODEL_BASE_URL=http://127.0.0.1:$((MOCK_PORT+7))/v1 MODEL_API_KEY=mock MODEL_NAME=mock-gemma PORT=$((SRV_PORT+8)) \
  MAX_TURNS=40 PROVENANCE_CHECK=1 FINISH_GATE=1 IO_HELPERS=1 HELPER_PATH="${HELPER_PATH:-/tmp/ple_io.py}" \
  $PY serve.py & pids+=($!)
wait_port $((SRV_PORT+8)) || { echo "serve.py did not listen"; exit 1; }
$PY tests/fake_evaluator.py --port $((SRV_PORT+8)) --workdir "$WORK" --setup --agent-timeout 3600 \
  --path-prepend "$VENV_BIN" --expect-usage --forbid-final "<thought>" "UNSOURCED" \
  --expect-final "appears in a project document" \
  --expect output/report.md output/schedule.xlsx output/memo.docx \
  --expect-log "helper: ${HELPER_PATH:-/tmp/ple_io.py} written" "provenance: corpus of" "unsourced designation(s): UNSOURCED: \`DRAFT-0\`" \
  "finish gate: pass 1/2" "verify: 3 ok, 0 issues" || status=1
kill "${pids[@]}" 2>/dev/null; pids=(); sleep 0.5

echo; echo "### 10. variant D switches on top of C (IO_WRITERS, DXF_LAYER_CHECK, VALUE_CHECK, READBACK): same flow, writer commands offered, no VALUES finding on clean tables"
rm -rf "$WORK"; mkdir -p "$WORK"
MOCK_WORKDIR="$PWD/$WORK" $PY tests/mock_model.py --port $((MOCK_PORT+8)) & pids+=($!)
wait_port $((MOCK_PORT+8)) || { echo "mock model did not start"; exit 1; }
MODEL_BASE_URL=http://127.0.0.1:$((MOCK_PORT+8))/v1 MODEL_API_KEY=mock MODEL_NAME=mock-gemma PORT=$((SRV_PORT+9))   MAX_TURNS=40 PROVENANCE_CHECK=1 FINISH_GATE=1 IO_HELPERS=1 IO_WRITERS=1 DXF_LAYER_CHECK=1 VALUE_CHECK=1 READBACK=1 HELPER_PATH="${HELPER_PATH:-/tmp/ple_io.py}"   $PY serve.py & pids+=($!)
wait_port $((SRV_PORT+9)) || { echo "serve.py did not listen"; exit 1; }
$PY tests/fake_evaluator.py --port $((SRV_PORT+9)) --workdir "$WORK" --setup --agent-timeout 3600   --path-prepend "$VENV_BIN" --expect-usage --forbid-final "<thought>" "UNSOURCED" "VALUES"   --expect-final "appears in a project document"   --expect output/report.md output/schedule.xlsx output/memo.docx   --expect-log "helper: ${HELPER_PATH:-/tmp/ple_io.py} written" "provenance: corpus of" "finish gate: pass 1/2" "verify: 3 ok, 0 issues" || status=1
kill "${pids[@]}" 2>/dev/null; pids=(); sleep 0.5

if [ "${1:-}" = "--real" ]; then
  echo; echo "### 4. real model smoke (MODEL_NAME=$MODEL_NAME): tiny task, few turns"
  rm -rf "$WORK"; mkdir -p "$WORK"
  PORT=$((SRV_PORT+2)) MAX_TURNS=${REAL_MAX_TURNS:-8} CHECKLIST_LLM=${CHECKLIST_LLM:-0} ENG_REVIEW=${ENG_REVIEW:-0} \
    TRIAGE_SUMMARY_FILES=4 $PY serve.py & pids+=($!)
  wait_port $((SRV_PORT+2)) || { echo "serve.py did not listen"; exit 1; }
  $PY tests/fake_evaluator.py --port $((SRV_PORT+2)) --workdir "$WORK" --setup --agent-timeout 900 \
    --path-prepend "$VENV_BIN" --expect-usage --max-wait 900 \
    --expect output/report.md output/schedule.xlsx output/memo.docx || status=1
  kill "${pids[@]}" 2>/dev/null; pids=()
fi

echo; echo "### overall: $([ $status -eq 0 ] && echo PASS || echo FAIL)"
exit $status
