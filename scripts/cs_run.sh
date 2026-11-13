#!/bin/bash
# One PLE dev run in the Codespace (mirrors .github/workflows/ple-run.yml). Usage: cs_run.sh RUN VARIANT TASK
# VARIANT A = defaults, B = BENV, C = CENV, D = DENV (variant C's switches plus the D ones; override DENV to change)
# Keys come from Codespaces secrets (env HF_TOKEN, MODEL_API_KEY) and are passed via a pipe, never written to disk.
set -u
. /workspaces/ple-harness/scripts/cs_env.sh
RUN=$1; V=$2; TASKS=$3
MODEL=${MODEL:-nvidia/nemotron-3.5-lightning-30b-a3b}
BASE=${BASE:-https://integrate.api.nvidia.com/v1}
COMMON=${COMMON:-"COMPACT_AT_TOKENS=90000 MAX_OUTPUT_TOKENS=16000"}
BENV=${BENV:-"NOTES_PHASE=1 VERIFY_TEMPLATE=1 READ_REPEAT_CHARS=4000 COMPACT_KEEP_OPENING=1 TEMPERATURE=0.3 TOOL_RESULT_CHARS=12000"}
CENV=${CENV:-"PROVENANCE_CHECK=1 FINISH_GATE=1 IO_HELPERS=1"}
DENV=${DENV:-"PROVENANCE_CHECK=1 FINISH_GATE=1 IO_HELPERS=1 IO_WRITERS=1 DXF_LAYER_CHECK=1 VALUE_CHECK=1 READBACK=1"}
export PATH=$HOME/.local/bin:$PATH
mkdir -p $HOME/ple/locks $HOME/ple/logs
mkdir "$HOME/ple/locks/$RUN-$V" 2>/dev/null || { echo "skip $RUN-$V (taken)"; exit 0; }
cd $HOME/ple
H="h-$RUN-$V"; rm -rf "$H" && cp -r /workspaces/ple-harness "$H" && rm -rf "$H/.git"
cd "$H"
sed -i "s#^model_api: .*#model_api: openai-compatible/${MODEL}\nmodel_base_url: https://llm.example.org/v1#" submission.yaml
V=$V COMMON="$COMMON" BENV="$BENV" CENV="$CENV" DENV="$DENV" python3 - <<'PY'
import os, re
p = "submission.yaml"; s = open(p).read()
pairs = os.environ.get("COMMON", "").split()
if os.environ["V"] == "B": pairs += os.environ.get("BENV", "").split()
if os.environ["V"] == "C": pairs += os.environ.get("CENV", "").split()
if os.environ["V"] == "D": pairs += os.environ.get("DENV", "").split()
for kv in pairs:
    k, v = kv.split("=", 1); line = f'  {k}: "{v}"'
    if re.search(rf"(?m)^  {re.escape(k)}: .*$", s): s = re.sub(rf"(?m)^  {re.escape(k)}: .*$", line, s)
    else: s = re.sub(r"(?m)^env:\s*$", "env:\n" + line, s, count=1)
open(p, "w").write(s)
PY
cd ../kit-src
python3 - <<'PY'
p = "eval/run.py"; s = open(p).read()
old = '    fresh_copy(src, target)\n    toml = target / "task.toml"\n'
new = old + ('    import os as _os, re as _re\n'
             '    toml.write_text(_re.sub(r"(?m)^(\s*cpus\s*=\s*)(\d+)", lambda m: m.group(1) + str(min(int(m.group(2)), _os.cpu_count() or 2)), toml.read_text(encoding="utf-8")), encoding="utf-8")\n')
if "_os.cpu_count()" not in s:
    assert s.count(old) == 1; open(p, "w").write(s.replace(old, new))
PY
uv run python eval/run.py "../$H" --env-file <(printf 'MODEL_API_KEY=%s\nHF_TOKEN=%s\n' "$MODEL_API_KEY" "$HF_TOKEN") \
  --model-base-url "$BASE" --no-export --trials 1 --tasks "$TASKS" --out "runs/$RUN-$V" 2>&1 \
  | sed -E 's/(hf_|nvapi-)[A-Za-z0-9_-]+/\1***/g' | grep -v 'Fetching .* files' > "$HOME/ple/logs/$RUN-$V.log"
docker images --filter reference="plebench-*" -q | xargs -r docker rmi -f >/dev/null 2>&1; docker image prune -f >/dev/null 2>&1; docker builder prune -af >/dev/null 2>&1
echo "$(date -u +%H:%M) $RUN-$V $(grep -m1 -oE '"score": [0-9.]+' runs/$RUN-$V/results.json 2>/dev/null)" >> $HOME/ple/logs/summary.txt
