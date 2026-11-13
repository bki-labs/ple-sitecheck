# Copyright 2026 ple-sitecheck authors
# Licensed under the Apache License, Version 2.0; see LICENSE.

"""A scripted OpenAI-compatible Chat Completions server for smoke tests.

No real model: replies follow a fixed scenario that exercises the harness's
checklist call, tool calls, verification feedback, repair, review and final
summary. Streams SSE chunks (tool-call deltas + usage) like a real endpoint,
or answers non-streamed when `stream` is false.

    python tests/mock_model.py --port 18111 [--slow-sec 40] [--scenario planner]

Scenarios: `normal` (look, write two of three files with a draft marker in
the schedule's basis column, claim done, get nudged, write the third, fix the
formula and the basis on repair feedback, review, finish) and
`planner` (two tool-free "plan" turns with <thought> blocks first, as Gemma
does, then the normal flow). The mock looks at the workdir to decide whether
a repair prompt concerns the missing memo or the formula.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SLOW_SEC = 0.0
WORKDIR = "/app"
SCENARIO = "normal"
# --fail-count N: the first N chat calls get HTTP --fail-status (429 by
# default, with a Retry-After header and a Gemini-style error body); -1 = all
FAIL_COUNT = 0
FAIL_STATUS = 429
RETRY_AFTER = 1
CALLS = [0]

CHECKLIST_JSON = """{"deliverables": [
 {"path": "output/report.md", "format": "md", "members": [], "sheets": {}, "layers": [], "notes": ["narrative with basis"]},
 {"path": "output/schedule.xlsx", "format": "xlsx", "members": [], "sheets": {"Schedule": ["item", "qty", "unit"]}, "layers": [], "notes": []},
 {"path": "__WD__/output/memo.docx", "format": "docx", "members": [], "sheets": {}, "layers": [], "notes": ["use the house template"]}
], "inputs": ["inputs/loads.csv", "templates/memo_template.docx"],
 "key_requirements": ["Schedule sheet lists every load case from loads.csv", "memo dated 2026-11-10"],
 "graded_by": "both"}"""

PLAN_1 = "<thought>Okay, I've seen the files. I need the loads and the template.</thought>I'll start by reading the inputs, then compute the schedule, then write the memo."
PLAN_2 = "Plan:\n1. Read inputs/loads.csv.\n2. Build the schedule workbook.\n3. Fill the memo template.\n<thought>I should just do it.</thought>"

WRITE_FIRST = r"""mkdir -p output && python3 - <<'PY'
import csv, openpyxl
rows = list(csv.DictReader(open('inputs/loads.csv')))
wb = openpyxl.Workbook(); ws = wb.active; ws.title = 'Schedule'
ws.append(['item', 'qty', 'unit', 'basis'])
for r in rows:
    ws.append([r['case'], float(r['kN']), 'kN', 'DRAFT-0'])
ws.append(['total', '=SUM(B2:B%d)' % (len(rows) + 1), 'kN', 'DRAFT-0'])
wb.save('output/schedule.xlsx')
open('output/report.md', 'w').write('# Load schedule report\n\n' + 'Basis: loads.csv, Eurocode 1 characteristic values. ' * 8 + '\n\nTotal is in the workbook.\n')
print('wrote schedule.xlsx and report.md', len(rows), 'rows')
PY"""

WRITE_MEMO = r"""python3 - <<'PY'
import csv, docx
rows = list(csv.DictReader(open('inputs/loads.csv')))
total = sum(float(r['kN']) for r in rows)
d = docx.Document('templates/memo_template.docx')
for p in d.paragraphs:
    if '[Project]' in p.text: p.text = p.text.replace('[Project]', 'Mock Project')
    if '[Date]' in p.text: p.text = p.text.replace('[Date]', '2026-11-10')
d.add_paragraph('Determination: the schedule totals %.1f kN over %d load cases; basis loads.csv and EN 1991-1-1. Assumptions: characteristic values, no dynamic factor. ' % (total, len(rows)) * 3)
d.save('__WD__/output/memo.docx')
print('wrote memo.docx')
PY"""

WRITE_FIX = r"""python3 - <<'PY'
import csv, openpyxl
rows = list(csv.DictReader(open('inputs/loads.csv')))
total = sum(float(r['kN']) for r in rows)
wb = openpyxl.load_workbook('output/schedule.xlsx'); ws = wb['Schedule']
ws.cell(row=ws.max_row, column=2).value = total
for r in range(2, ws.max_row + 1):
    ws.cell(row=r, column=4).value = 'loads.csv'  # the basis named in the brief, not a draft marker
wb.save('output/schedule.xlsx')
print('wrote the computed total and the basis column')
PY"""

SUMMARY_1 = (
    "<thought>Two files done; the memo can wait.</thought>I have written output/schedule.xlsx (sheet Schedule with item, qty, unit) and output/report.md.\n"
    "Key result: total load 61.5 kN over 4 cases."
)
SUMMARY_2 = (
    "Done: output/report.md, output/schedule.xlsx and __WD__/output/memo.docx are written.\n"
    "Key result: total load 61.5 kN."
)
SUMMARY_FINAL = """- Deliverables: output/report.md (narrative and basis); output/schedule.xlsx (sheet Schedule: item, qty, unit; 4 cases + total); __WD__/output/memo.docx (memo from the house template, dated 2026-11-10).
- Key results: total characteristic load 61.5 kN; largest case 25.0 kN.
- Basis: inputs/loads.csv; EN 1991-1-1 characteristic values.
- Assumptions and judgement calls: no dynamic amplification; values taken as characteristic.
- Checks performed: re-summed the CSV in Python and compared with the workbook total; re-opened all three files.
- Open items: none."""


def tool_call(name: str, args: dict) -> dict:
  return {"name": name, "arguments": json.dumps(args)}


def wd(text: str) -> str:
  return text.replace("__WD__", WORKDIR)


def scenario(messages: list[dict]) -> tuple[str, list[dict], str]:
  """Returns (text, tool_calls, finish_reason) for the request's messages."""
  system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
  if "checklist" in system.lower() and "JSON" in system:
    return wd(CHECKLIST_JSON), [], "stop"
  last = messages[-1]
  last_text = (last.get("content") or "") if isinstance(last.get("content"), str) else json.dumps(last.get("content"))
  n_assistant = sum(1 for m in messages if m.get("role") == "assistant")
  memo_exists = (pathlib.Path(WORKDIR) / "output" / "memo.docx").is_file()
  report_exists = (pathlib.Path(WORKDIR) / "output" / "report.md").is_file()
  if last.get("role") == "user":
    if "engineering review pass" in last_text:
      return wd(SUMMARY_FINAL), [], "stop"
    if "Stop working now" in last_text:
      return "Cut short. " + wd(SUMMARY_FINAL), [], "stop"
    if "Make a second pass now" in last_text:
      return "", [tool_call("bash", {"command": "ls -la output; python3 /tmp/ple_io.py info output/schedule.xlsx", "timeout_sec": 60})], "tool_calls"
    if "opened your deliverables" in last_text:
      return "", [tool_call("bash", {"command": wd(WRITE_FIX), "timeout_sec": 60})], "tool_calls"
    if ("continue nudge" in last_text or "turn budget is used up" in last_text) and report_exists and not memo_exists:
      return "", [tool_call("bash", {"command": wd(WRITE_MEMO), "timeout_sec": 60})], "tool_calls"
  k = n_assistant - (2 if SCENARIO == "planner" else 0)
  if k == -2:
    return PLAN_1, [], "stop"
  if k == -1:
    return PLAN_2, [], "stop"
  if k == 0:
    calls = [tool_call("bash", {"command": "ls -la; echo ---; cat task.md; echo ---; head -5 inputs/loads.csv"})]
    if SCENARIO == "planner":
      calls.append(tool_call("read_file", {"path": "inputs/spec.pdf"}))
    return "Let me look at the workspace first.", calls, "tool_calls"
  if k == 1:
    return "", [tool_call("bash", {"command": WRITE_FIRST, "timeout_sec": 60})], "tool_calls"
  if k == 2:
    return SUMMARY_1, [], "stop"
  if last.get("role") == "tool":
    return wd(SUMMARY_2), [], "stop"
  return wd(SUMMARY_FINAL), [], "stop"


def usage_for(messages: list[dict], text: str) -> dict:
  prompt = sum(len(json.dumps(m)) for m in messages) // 4
  return {
      "prompt_tokens": prompt,
      "completion_tokens": max(1, len(text) // 4),
      "total_tokens": prompt + max(1, len(text) // 4),
      "prompt_tokens_details": {"cached_tokens": prompt // 2},
  }


class Handler(BaseHTTPRequestHandler):
  """Handles POST /v1/chat/completions (and the bare path)."""

  def log_message(self, fmt, *args):  # quieter
    print(f"mock-model: {fmt % args}", flush=True)

  def do_POST(self):  # pylint: disable=invalid-name
    length = int(self.headers.get("Content-Length") or 0)
    body = json.loads(self.rfile.read(length) or b"{}")
    if not self.path.endswith("/chat/completions"):
      self.send_response(404)
      self.end_headers()
      return
    messages = body.get("messages") or []
    CALLS[0] += 1
    if FAIL_COUNT == -1 or CALLS[0] <= FAIL_COUNT:
      err = {"error": {"code": FAIL_STATUS, "status": "RESOURCE_EXHAUSTED" if FAIL_STATUS == 429 else "UNAVAILABLE",
                       "message": f"mock: call {CALLS[0]} refused with {FAIL_STATUS} (quota exceeded for metric"
                                  " input_tokens_per_minute)" if FAIL_STATUS == 429 else "mock: high demand"}}
      data = json.dumps(err).encode()
      self.send_response(FAIL_STATUS)
      self.send_header("Content-Type", "application/json")
      self.send_header("Retry-After", str(RETRY_AFTER))
      self.send_header("Content-Length", str(len(data)))
      self.end_headers()
      self.wfile.write(data)
      return
    if SLOW_SEC and not any("checklist" in (m.get("content") or "").lower() for m in messages if m.get("role") == "system"):
      time.sleep(SLOW_SEC)
    text, calls, finish = scenario(messages)
    model = body.get("model", "mock")
    usage = usage_for(messages, text + json.dumps(calls))
    now = int(time.time())
    if body.get("stream"):
      self.send_response(200)
      self.send_header("Content-Type", "text/event-stream")
      self.send_header("Cache-Control", "no-cache")
      self.end_headers()

      def chunk(delta, finish_reason=None, usage_obj=None):
        obj = {
            "id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": now, "model": model,
            "choices": [] if usage_obj else [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage_obj:
          obj["usage"] = usage_obj
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
        self.wfile.flush()

      chunk({"role": "assistant", "content": ""})
      for i in range(0, len(text), 60):
        chunk({"content": text[i:i + 60]})
      for idx, call in enumerate(calls):
        chunk({"tool_calls": [{"index": idx, "id": f"call_{now}_{idx}", "type": "function",
                               "function": {"name": call["name"], "arguments": ""}}]})
        args = call["arguments"]
        for i in range(0, len(args), 80):
          chunk({"tool_calls": [{"index": idx, "function": {"arguments": args[i:i + 80]}}]})
      chunk({}, finish_reason=finish)
      chunk({}, usage_obj=usage)
      self.wfile.write(b"data: [DONE]\n\n")
      self.wfile.flush()
      return
    message = {"role": "assistant", "content": text or None}
    if calls:
      message["tool_calls"] = [
          {"id": f"call_{now}_{i}", "type": "function", "function": c} for i, c in enumerate(calls)
      ]
    resp = {
        "id": "chatcmpl-mock", "object": "chat.completion", "created": now, "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage,
    }
    data = json.dumps(resp).encode()
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)


def main():
  global SLOW_SEC, WORKDIR, SCENARIO, FAIL_COUNT, FAIL_STATUS, RETRY_AFTER  # pylint: disable=global-statement
  ap = argparse.ArgumentParser()
  ap.add_argument("--port", type=int, default=18111)
  ap.add_argument("--slow-sec", type=float, default=float(os.environ.get("MOCK_SLOW_SEC", "0")))
  ap.add_argument("--workdir", default=os.environ.get("MOCK_WORKDIR", "/app"))
  ap.add_argument("--scenario", default=os.environ.get("MOCK_SCENARIO", "normal"), choices=["normal", "planner"])
  ap.add_argument("--fail-count", type=int, default=0, help="first N chat calls fail (-1: every call)")
  ap.add_argument("--fail-status", type=int, default=429)
  ap.add_argument("--retry-after", type=int, default=1, help="Retry-After header on failed calls")
  args = ap.parse_args()
  SLOW_SEC, WORKDIR, SCENARIO = args.slow_sec, args.workdir, args.scenario
  FAIL_COUNT, FAIL_STATUS, RETRY_AFTER = args.fail_count, args.fail_status, args.retry_after
  server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
  print(f"mock model listening on {args.port} (slow {SLOW_SEC}s, scenario {SCENARIO}, fail {FAIL_COUNT}x{FAIL_STATUS})", flush=True)
  server.serve_forever()


if __name__ == "__main__":
  main()
