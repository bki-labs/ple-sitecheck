# Copyright 2026 ple-sitecheck authors
# Licensed under the Apache License, Version 2.0; see LICENSE.

"""Unit regressions that need no server: thought-tag stripping, read_file on
documents, checklist paths from generic brief phrasing, retry schedule,
context budget, placeholder deliverables.

    python tests/test_units.py
"""

from __future__ import annotations

import asyncio
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import checklist  # pylint: disable=g-import-not-at-top,wrong-import-position
import engine  # pylint: disable=g-import-not-at-top,wrong-import-position
import harness  # pylint: disable=g-import-not-at-top,wrong-import-position
import parsers  # pylint: disable=g-import-not-at-top,wrong-import-position
import triage  # pylint: disable=g-import-not-at-top,wrong-import-position
import verify  # pylint: disable=g-import-not-at-top,wrong-import-position

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
  print(("PASS: " if cond else "FAIL: ") + what)
  if not cond:
    FAILS.append(what)


class StubSession:
  """Just enough of taskbridge.Session for Agent.read_file."""

  def __init__(self, files: dict[str, bytes]):
    self.files = files
    self.workdir = "/app"
    self.limits = {}
    self.mcp_servers = []
    self.instruction = ""
    self.final_message = ""

  def time_left(self):
    return None

  async def log(self, text):
    pass

  async def read_file(self, path):
    return self.files[path]


def make_pdf() -> bytes:
  import pymupdf  # pylint: disable=g-import-not-at-top

  doc = pymupdf.open()
  page = doc.new_page()
  page.insert_text((72, 72), "Design criteria: basic wind speed 115 mph, exposure C.")
  page = doc.new_page()
  page.insert_text((72, 72), "Second page: roof dead load 20 psf.")
  return doc.tobytes()


def make_xlsx() -> bytes:
  import openpyxl  # pylint: disable=g-import-not-at-top

  wb = openpyxl.Workbook()
  ws = wb.active
  ws.title = "Loads"
  ws.append(["case", "kN"])
  ws.append(["G1", 20.0])
  buf = io.BytesIO()
  wb.save(buf)
  return buf.getvalue()


def make_docx() -> bytes:
  import docx  # pylint: disable=g-import-not-at-top

  d = docx.Document()
  d.add_paragraph("Memo body: the footing bears on stiff clay.")
  buf = io.BytesIO()
  d.save(buf)
  return buf.getvalue()


async def main() -> int:
  # 1. thought blocks never reach the answer or the stored history
  check(engine.visible("<thought>plan plan</thought>I'll read the criteria.") == "I'll read the criteria.", "visible() strips <thought>")
  check(engine.visible("<thinking>x</thinking>") == "", "visible() strips <thinking> to empty")
  check(engine.visible("<think>a</think>\n<thought>b</thought>answer") == "answer", "visible() strips mixed tags")
  check(engine.visible("<thought>unterminated...") == "", "visible() strips an unterminated thought")
  r = engine.Reply()
  r.text = "<thought>t</thought>done"
  check(r.message()["content"] == "done", "Reply.message() stores content without thoughts")

  # 2. read_file on documents returns extracted text, never raw bytes
  files = {
      "/app/spec.pdf": make_pdf(),
      "/app/big.pdf": make_pdf() + b"\n" * 3_000_000,
      "/app/loads.xlsx": make_xlsx(),
      "/app/memo.docx": make_docx(),
      "/app/notes.txt": b"plain text",
  }
  agent = engine.Agent(StubSession(files), "sys", [])
  out = await agent.read_file("spec.pdf")
  check("basic wind speed 115 mph" in out and "page 2 of 2" in out, "read_file(pdf) extracts page text")
  check("%PDF" not in out and "obj" not in out[:200], "read_file(pdf) returns no raw PDF bytes")
  out = await agent.read_file("big.pdf")
  check(len(out) <= engine.TOOL_RESULT_CHARS + 400 and "%PDF" not in out, "read_file(large pdf) is bounded")
  out = await agent.read_file("loads.xlsx")
  check("sheet 'Loads'" in out and "G1\t20" in out, "read_file(xlsx) returns rows as TSV")
  out = await agent.read_file("memo.docx")
  check("stiff clay" in out, "read_file(docx) returns paragraph text")
  out = await agent.read_file("notes.txt")
  check(out == "plain text", "read_file(txt) unchanged")

  # 3. checklist: output dir and file from generic brief phrasing
  brief = (
      "You are working in `/filesystem/some_project__blind`. The project documents are under"
      " `/filesystem/some_project__blind/` (read `task.md` there first); write your deliverable to"
      " `/filesystem/some_project__blind/output/submission.zip`.\n\nWrite one file, `output/submission.zip`,"
      " containing at its root:\n\n### 1. `schedule.xlsx`\n\n* **Items**: `item_id`, `qty`, `unit`.\n\n### 2. `basis.md`\n\n"
      "## How the deliverable is judged\n"
  )
  cl = checklist.extract(brief, "/filesystem")
  paths = [d.path for d in cl.deliverables]
  check(paths == ["/filesystem/some_project__blind/output/submission.zip"], f"zip deliverable path parsed: {paths}")
  z = cl.deliverables[0]
  check(z.members == ["schedule.xlsx", "basis.md"], f"zip members parsed: {z.members}")
  check(z.sheets.get("schedule.xlsx::Items") == ["item_id", "qty", "unit"], f"sheet columns parsed: {z.sheets}")
  brief2 = "Draft the memo as a Word document and save it as /filesystem/Reply_Memo_2026-11-10.docx. Two files are attached: Records.xlsx and TEMPLATE.docx."
  cl2 = checklist.extract(brief2, "/app")
  check([d.path for d in cl2.deliverables] == ["/filesystem/Reply_Memo_2026-11-10.docx"], "absolute docx path with digits parsed")
  check("/app/Records.xlsx" in cl2.inputs, "attached workbook is an input")
  brief3 = "Save the files that make up your answer under `output/` in this workspace."
  cl3 = checklist.extract(brief3, "/app/files")
  check([d.path for d in cl3.deliverables] == ["/app/files/output/"], f"output folder deliverable parsed: {[d.path for d in cl3.deliverables]}")
  smoke = "Create a file named `hello.txt` in the current working directory (`/app`) whose entire content is the single line:\n\nHELLO\n"
  cl4 = checklist.extract(smoke, "/app")
  check([d.path for d in cl4.deliverables] == ["/app/hello.txt"], "kit smoke task deliverable parsed")

  # 4. retry schedule: listed waits, then the cap for ever; Retry-After wins
  check(engine.retry_wait(1) == float(engine.RETRY_WAITS[0]), "retry_wait(1) is the first scheduled wait")
  late = engine.retry_wait(len(engine.RETRY_WAITS) + 50)
  check(0.75 * engine.RETRY_CAP_SEC <= late <= 1.25 * engine.RETRY_CAP_SEC, f"retry_wait past the schedule stays near the cap ({late:.0f}s)")
  check(engine.retry_wait(3, retry_after=7) == 8.0, "Retry-After replaces the schedule (+1 s slack)")
  import openai  # pylint: disable=g-import-not-at-top
  try:
    import httpx2 as httpx  # pylint: disable=g-import-not-at-top  # openai 3.x vendors httpx as httpx2
  except ImportError:
    import httpx  # pylint: disable=g-import-not-at-top

  req = httpx.Request("POST", "http://x/v1/chat/completions")
  e429 = openai.APIStatusError("429", response=httpx.Response(429, request=req, headers={"Retry-After": "12"}), body=None)
  e401 = openai.APIStatusError("401", response=httpx.Response(401, request=req), body=None)
  e503 = openai.APIStatusError("503", response=httpx.Response(503, request=req), body=None)
  check(engine.retry_after_of(e429) == 12.0, "retry_after_of reads the header")
  check(engine.retry_after_of(e503) is None, "retry_after_of is None without the header")
  check(engine.retryable(e429) and engine.retryable(e503) and not engine.retryable(e401), "429/503 retryable, 401 not")
  check(engine.retryable(engine.StreamCut("cut")), "a cut stream is retryable")

  # 5. context budget: a big workspace still fits the inventory cap
  files = [(1000 * i, f"/app/project/folder{i % 7}/file_{i:03d}.pdf") for i in range(400)]
  summaries = [f"- `/app/project/folder{i % 7}/file_{i:03d}.pdf` [pdf]: " + "lorem ipsum " * 60 for i in range(40)]
  inv = triage.compose(files, ["/app"], summaries, "python 3.12\nmodules available: openpyxl", max_chars=12000)
  check(len(inv) <= 12000, f"compose() fits the cap ({len(inv)} chars)")
  check("more files" in inv and "more files not summarised" in inv, "compose() says what it cut and how to read more")
  check("Tooling in the task container" in inv and "openpyxl" in inv, "compose() keeps the tooling probe")
  inv_small = triage.compose(files[:5], ["/app"], summaries[:2], "python 3.12")
  check("more files" not in inv_small and "lorem" in inv_small, "compose() leaves a small workspace whole")
  brief, inventory, tokens = harness.fit_context("s" * 3000, "i" * 8000, "b" * 20000, "v" * 40000, "c" * 1000, budget_tokens=10000)
  check(tokens <= 10500 and len(brief) + len(inventory) < 60000, f"fit_context trims to the budget ({tokens} tokens)")
  check(inventory.endswith("]") and "cut to fit" in inventory, "fit_context cuts the inventory with a note")
  check(len(brief) >= 1500, "fit_context keeps at least the head of the brief")
  b2, v2, t2 = harness.fit_context("s", "i" * 2000, "b" * 4000, "v" * 8000, "c", budget_tokens=10000)
  check(b2 == "b" * 4000 and v2 == "v" * 8000 and t2 < 10000, "fit_context leaves a small context alone")

  # 6. placeholders: every supported format parses, is detected, holds the marker and no shape from the brief
  d = checklist.Deliverable(path="/app/out/x.xlsx", kind="xlsx", sheets={"Schedule": ["item", "qty"]})
  for name in ("report.md", "memo.docx", "book.xlsx", "calc.pdf", "pack.zip", "data.json", "notes.txt"):
    data = verify.placeholder_bytes("/app/out/" + name, "the model never answered (HTTP 429 until the deadline)")
    info = parsers.parse(name, data, detail=400)
    check(data and info.get("ok") and verify.is_placeholder(info), f"placeholder {name} parses and is detected")
    issues, oks = [], []
    verify._check_bytes("/app/out/" + name, data, d, issues, oks)  # pylint: disable=protected-access
    check(any(i.startswith("PLACEHOLDER: ") for i in issues) and not oks, f"verify flags the {name} placeholder as a gap")
  check(verify.placeholder_bytes("/app/out/model.ifc", "x") is None and verify.placeholder_bytes("/app/out/plan.dxf", "x") is None, "no placeholder for CAD/BIM formats")
  x = parsers.parse("book.xlsx", verify.placeholder_bytes("/app/out/book.xlsx", "x"), detail=400)
  check("Schedule" not in x["sheets"], "placeholder workbook carries no sheet from the checklist")
  real = make_xlsx()
  issues, oks = [], []
  verify._check_bytes("/app/out/loads.xlsx", real, checklist.Deliverable(path="/app/out/loads.xlsx", kind="xlsx"), issues, oks)  # pylint: disable=protected-access
  check(not issues and oks, "a real workbook is not mistaken for a placeholder")

  # 7. VERIFY_TEMPLATE: deliverables paired with template files, then compared with them
  import openpyxl  # pylint: disable=g-import-not-at-top
  import docx as docx_mod  # pylint: disable=g-import-not-at-top

  cl7 = checklist.extract(
      "Write `output/submission.zip` containing at its root:\n\n### 1. `lateral_schedule.xlsx`\n\n### 2. `plan.dxf`\n\n### 3. `basis.md`\n",
      "/app",
  )
  files7 = [
      "/app/templates/lateral_schedule_template.xlsx", "/app/templates/plan_template.dxf",
      "/app/project/schedule_2024.xlsx", "/app/templates/memo_template.docx", "/app/library/example_register.xlsx",
  ]
  n7 = checklist.match_templates(cl7, files7)
  z7 = cl7.deliverables[0]
  check(
      z7.templates.get("lateral_schedule.xlsx") == files7[0] and z7.templates.get("plan.dxf") == files7[1]
      and "basis.md" not in z7.templates and n7 == 2,
      f"match_templates pairs zip members with templates by stem: {z7.templates}",
  )
  cl7b = checklist.extract("Save the memo as `/app/output/Reply_Memo.docx`.", "/app")
  checklist.match_templates(cl7b, files7)
  check(cl7b.deliverables[0].templates.get("") == files7[3], "the only template of a format is paired with the deliverable")
  check("templates" not in cl7.to_json(), "to_json leaves the template pairing out of the model's checklist draft")

  def book(rows, title="Register"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for r in rows:
      ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

  tpl_path = "/app/templates/register_template.xlsx"
  tinfo = parsers.parse(tpl_path, book([["id", "requirement", "status"]] + [[f"R-{i:02d}", "", ""] for i in range(1, 9)]))
  check(tinfo["sheets"]["Register"]["first_col"][1:3] == ["R-01", "R-02"], "parse_xlsx records the first column (row keys)")
  d7 = checklist.Deliverable(path="/app/out/register.xlsx", kind="xlsx")
  one = book([["id", "requirement", "status"], ["X1", "done", "ok"]])
  issues, oks = [], []
  verify._check_bytes(d7.path, one, d7, issues, oks, templates={"": (tpl_path, tinfo)})  # pylint: disable=protected-access
  check(any("lists 8 rows by key" in i and "R-01" in i for i in issues), f"verify flags template row keys missing from the deliverable: {issues}")
  full = book([["id", "requirement", "status"]] + [[f"R-{i:02d}", "met", "ok"] for i in range(1, 9)])
  issues, oks = [], []
  verify._check_bytes(d7.path, full, d7, issues, oks, templates={"": (tpl_path, tinfo)})  # pylint: disable=protected-access
  check(not issues and any("matches template" in o for o in oks), f"a register with every template key passes: {issues}")
  wrong = book([["key", "text"], ["R-01", "x"], ["R-02", "y"]], title="Sheet1")
  issues, oks = [], []
  verify._check_bytes(d7.path, wrong, d7, issues, oks, templates={"": (tpl_path, tinfo)})  # pylint: disable=protected-access
  check(any("sheet `Register` of the template is missing" in i for i in issues), f"verify flags a template sheet that is missing: {issues}")
  issues, oks = [], []
  verify._check_bytes(d7.path, full, d7, issues, oks)  # pylint: disable=protected-access
  check(not issues and not any("template" in o for o in oks), "without a template nothing is compared (flag off)")

  def memo(headings, body):
    doc = docx_mod.Document()
    for level, h in headings:
      doc.add_heading(h, level)
    doc.add_paragraph(body)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

  tdoc = parsers.parse("memo_template.docx", memo([(0, "Memo"), (1, "Determination"), (1, "Basis")], "[Project]"))
  dd = checklist.Deliverable(path="/app/out/memo.docx", kind="docx")
  issues, oks = [], []
  verify._check_bytes(dd.path, memo([], "The footing bears on stiff clay. " * 20), dd, issues, oks, templates={"": ("/app/templates/memo_template.docx", tdoc)})  # pylint: disable=protected-access
  check(any("section headings" in i and "Determination" in i for i in issues), f"verify flags template headings missing from a memo: {issues}")
  issues, oks = [], []
  verify._check_bytes(dd.path, memo([(0, "Memo"), (1, "Determination"), (1, "Basis")], "The footing bears on stiff clay. " * 20), dd, issues, oks, templates={"": ("/app/templates/memo_template.docx", tdoc)})  # pylint: disable=protected-access
  check(not issues, f"a memo with the template's headings passes: {issues}")

  # 8. READ_REPEAT_CHARS: a second read of the same file comes back short, with a pointer
  files8 = {"/app/long.txt": b"line of text\n" * 500}
  agent8 = engine.Agent(StubSession(files8), "sys", [])
  engine.READ_REPEAT_CHARS = 2000
  try:
    agent8.turn = 1
    first = await agent8.read_file("long.txt")
    agent8.turn = 2
    again = await agent8.read_file("long.txt")
    agent8.turn = 3
    other = await agent8.read_file("/app/long.txt")
  finally:
    engine.READ_REPEAT_CHARS = 0
  check(len(first) == 6500 and "already read" not in first, "first read_file returns the whole file")
  check(len(again) < 2600 and "already read long.txt in full at turn 1" in again, f"repeat read_file is cut with a note ({len(again)} chars)")
  check("already read" in other, "the repeat check resolves relative and absolute paths alike")
  agent8b = engine.Agent(StubSession(files8), "sys", [])
  agent8b.turn = 1
  await agent8b.read_file("long.txt")
  agent8b.turn = 2
  check(len(await agent8b.read_file("long.txt")) == 6500, "with READ_REPEAT_CHARS=0 (default) a repeat read is unchanged")

  # 9. TEMPERATURE: sent when set, dropped when the endpoint refuses it
  import os  # pylint: disable=g-import-not-at-top
  os.environ["TEMPERATURE"] = "0.2"
  try:
    a9 = engine.Agent(StubSession({}), "sys", [])
  finally:
    del os.environ["TEMPERATURE"]
  check(a9.sampling == {"temperature": 0.2}, f"TEMPERATURE=0.2 is sent as the sampling temperature: {a9.sampling}")
  check(a9.drop_sampling("Unknown parameter: 'temperature'") and not a9.sampling, "a 400 naming temperature drops it")
  check(not a9.drop_sampling("max_completion_tokens unsupported"), "an unrelated 400 leaves sampling alone")
  check(engine.Agent(StubSession({}), "sys", []).sampling == {}, "no TEMPERATURE: nothing extra is sent")

  # 10. COMPACT_KEEP_OPENING: notes-compaction keeps the brief/checklist/inventory message
  async def compacted(flag: bool) -> list[dict]:
    opening = [{"role": "user", "content": "INSTRUCTION"}, {"role": "user", "content": "CONTEXT with checklist"}]
    a = engine.Agent(StubSession({}), "sys", opening)
    a.messages = [{"role": "system", "content": "sys"}, *opening]
    for i in range(12):
      a.messages.append({"role": "assistant", "content": f"step {i}", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]})
      a.messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"})

    async def notes(*args, **kwargs):
      r = engine.Reply()
      r.text, r.finish = "NOTES SO FAR", "stop"
      return r

    a.stream = notes
    engine.COMPACT_KEEP_OPENING = flag
    try:
      done = await a.compact()
    finally:
      engine.COMPACT_KEEP_OPENING = False
    check(done, f"compact(flag={flag}) replaced turns with notes")
    return a.messages

  m_on = await compacted(True)
  check(m_on[2]["content"] == "CONTEXT with checklist" and m_on[3]["content"].startswith("Your notes"), "COMPACT_KEEP_OPENING=1 keeps the context message in front of the notes")
  m_off = await compacted(False)
  check(m_off[2]["content"].startswith("Your notes"), "default compaction keeps only the system prompt and instruction (unchanged behaviour)")

  # 11. NOTES_PHASE: the notes path and the read-sweep nudge
  cl11 = checklist.extract("Save the files that make up your answer under `output/` in this workspace.", "/app/files")
  check(harness.notes_path_for(cl11) == "/app/files/harness_notes.md", f"notes live next to the brief: {harness.notes_path_for(cl11)}")
  cl11b = checklist.Checklist(workdir="/app", base="/app/output", deliverables=[checklist.Deliverable(path="/app/output/", kind="dir")])
  check(harness.notes_path_for(cl11b) == "/tmp/harness_notes.md", "notes never land inside a deliverable folder")
  hooks = harness.SiteHooks(StubSession({}), cl7, "/app/harness_notes.md")
  nudges = []
  for t in range(1, 7):
    hooks.on_tool("read_file", {"path": f"/app/doc{t}.pdf"}, "text")
    nudges.append(hooks.after_tools(t, None))
  check(not any(nudges[:5]) and "without updating `/app/harness_notes.md`" in nudges[5], f"six read-only turns trigger one notes nudge: {[bool(n) for n in nudges]}")
  hooks.on_tool("write_file", {"path": "harness_notes.md", "content": "# notes"}, "ok")
  check(hooks.after_tools(7, None) == "" and hooks.since_notes == 0, "writing the notes resets the sweep counter")
  hooks.on_tool("bash", {"command": "cat >> /app/harness_notes.md <<'EOF'\n- value\nEOF"}, "exit code 0")
  hooks.after_tools(8, None)
  check(hooks.since_notes == 0, "appending to the notes with bash counts as an update")
  quiet = harness.SiteHooks(StubSession({}), cl7)
  for t in range(1, 10):
    quiet.on_tool("read_file", {"path": "/app/x.pdf"}, "text")
    check_nudge = quiet.after_tools(t, None)
  check(check_nudge == "" and quiet.notes_nudges == 0, "without NOTES_PHASE there is no notes nudge")

  # 12. PROVENANCE_CHECK: designations in tabular deliverables looked up in the input corpus
  check(verify.designation_like("40/7") and verify.designation_like("BOLT-12") and verify.designation_like("KINDK") and verify.designation_like("Kind K") and verify.designation_like("W36x150") and verify.designation_like("ASD"), "code-like strings count as designations")
  check(not verify.designation_like("12.5") and not verify.designation_like("Z1") and not verify.designation_like("yes") and not verify.designation_like("steel plate") and not verify.designation_like("N/A") and not verify.designation_like("2026-07-31"), "numbers, row keys, words, dates and n/a do not")
  corpus = verify.Corpus()
  corpus.add("ACME P-40 catalogue: size 18, 20, 22; pattern 40/7 and 40/4; joint Kind K bolts BOLT-12 at 12 in.")
  check(corpus.has("40/7") and corpus.has("KINDK") and corpus.has("kind-k") and corpus.has("Bolt-12") and not corpus.has("KINDL") and not corpus.has("40/9"), "corpus lookup is case-, separator- and space-insensitive")
  zones = book(
      [["item_id", "size", "pattern", "joint", "fastener", "note"],
       ["Z1", 18, "40/7", "KINDK", "FIX-X9", "edge item"],
       ["Z2", 20, "40/9", "KINDK", "FIX-X9", "field"],
       ["Z3", 20, "40/7", "Kind K", "FIX-X9", "field"]],
      title="Items",
  )
  d12 = checklist.Deliverable(path="/app/out/parts_plan.xlsx", kind="xlsx")
  rep = verify.Report([], [])
  verify._check_bytes(d12.path, zones, d12, rep.issues, rep.ok, report=rep)  # pylint: disable=protected-access
  check(set(rep.tokens) == {"40/7", "40/9", "KINDK", "Kind K", "FIX-X9"} and rep.tokens["KINDK"]["n"] == 2, f"tokens come from data columns, not row keys or free text: {rep.tokens}")
  check(rep.infos and rep.infos[0][1]["kind"] == "xlsx", "verify keeps the parsed info for the substance report")
  flagged: set[str] = set()
  found = verify.provenance_issues(rep, corpus, flagged)
  check(len(found) == 3 and found[0].startswith("UNSOURCED: `FIX-X9`") and any("`40/9`" in f for f in found) and not any("KINDK" in f for f in found), f"unsourced designations are reported, most frequent first, sourced ones not: {found}")
  check(verify.provenance_issues(rep, corpus, flagged) == [], "a token is flagged once per run")
  rep.issues += found
  check("UNSOURCED" not in verify.summary_lines(rep), "provenance findings stay out of the final message")
  ids = book([["pier", "panel", "line"], ["S01-L12", "S01", "S"], ["S02-L12", "S02", "S"], ["S03-L12", "S03", "S"], ["S04-L12", "S04", "N"], ["S05-L12", "S05", "N"]], title="Piers")
  rep2 = verify.Report([], [])
  verify._check_bytes("/app/out/piers.xlsx", ids, checklist.Deliverable(path="/app/out/piers.xlsx", kind="xlsx"), rep2.issues, rep2.ok, report=rep2)  # pylint: disable=protected-access
  check(not rep2.tokens, f"identifier columns (unique per row) and simple ids are skipped: {rep2.tokens}")
  z = io.BytesIO()
  import zipfile as _zipfile  # pylint: disable=g-import-not-at-top
  with _zipfile.ZipFile(z, "w") as zf:
    zf.writestr("parts_plan.xlsx", zones)
    zf.writestr("basis.md", "# Basis\nshort\n")
  rep3 = verify.Report([], [])
  verify._check_bytes("/app/out/submission.zip", z.getvalue(), checklist.Deliverable(path="/app/out/submission.zip", kind="zip"), rep3.issues, rep3.ok, report=rep3)  # pylint: disable=protected-access
  check("KINDK" in rep3.tokens and "parts_plan.xlsx" in rep3.tokens["KINDK"]["where"], "zip members are scanned too")
  sub = verify.substance_lines(rep3)
  check("basis.md: 14 characters" in sub and "(thin)" in sub and "sheet `Items`: 3 data rows" in sub and sub.index("basis.md") < sub.index("Items"), f"substance report lists rows/chars, thinnest first: {sub}")

  # 13. FINISH_GATE + error-streak hint in the hooks
  import os as _os  # pylint: disable=g-import-not-at-top

  class GateSession(StubSession):
    def __init__(self, files, left):
      super().__init__(files)
      self.left = left
      self.logs = []

    def time_left(self):
      return self.left

    async def log(self, text):
      self.logs.append(text)

    async def read_result(self, path):
      data = self.files.get(path)
      if data is None:
        return {"ok": False}
      import base64  # pylint: disable=g-import-not-at-top
      return {"ok": True, "size": len(data), "content_b64": base64.b64encode(data).decode()}

  cl13 = checklist.Checklist(workdir="/app", base="/app", deliverables=[checklist.Deliverable(path="/app/out/parts_plan.xlsx", kind="xlsx")])
  harness.FINISH_GATE, harness.ENG_REVIEW, engine.MAX_TURNS = True, False, 100
  try:
    g = harness.SiteHooks(GateSession({"/app/out/parts_plan.xlsx": zones}, 3 * 3600), cl13)
    first = await g.on_final("done", 30)
    check(first is not None and "pass 1 of 2" in first and "sheet `Items`: 3 data rows" in first and "30 of 100 turns" in first, "a tool-free answer at turn 30/100 with 3 h left gets the second-pass prompt")
    check(await g.on_final("done", 31) is None, "no second pass without tool work since the first")
    g.on_tool("bash", {"command": "python3 x.py"}, "exit code 0\nok")
    second = await g.on_final("done", 40)
    check(second is not None and "pass 2 of 2" in second, "tool work since the last pass opens the next one")
    g.on_tool("bash", {"command": "python3 x.py"}, "exit code 0\nok")
    check(await g.on_final("done", 45) is None, "FINISH_GATE_PASSES bounds the passes")
    late = harness.SiteHooks(GateSession({"/app/out/parts_plan.xlsx": zones}, 3 * 3600), cl13)
    check(await late.on_final("done", 60) is None, "past half the turn budget the answer is accepted")
    short = harness.SiteHooks(GateSession({"/app/out/parts_plan.xlsx": zones}, 30 * 60), cl13)
    check(await short.on_final("done", 10) is None, "with under FINISH_GATE_MINUTES left the answer is accepted")
  finally:
    harness.FINISH_GATE, harness.ENG_REVIEW, engine.MAX_TURNS = False, True, int(_os.environ.get("MAX_TURNS", "160"))
  off = harness.SiteHooks(GateSession({"/app/out/parts_plan.xlsx": zones}, 3 * 3600), cl13)
  harness.ENG_REVIEW = False
  try:
    check(await off.on_final("done", 30) is None, "without FINISH_GATE the answer is accepted as before")
  finally:
    harness.ENG_REVIEW = True
  harness.IO_HELPERS = True
  try:
    h = harness.SiteHooks(StubSession({}), cl13)
    hints = []
    for t in range(1, 5):
      h.on_tool("bash", {"command": "python3 -c 'import ezdxf; ezdxf.read(\"x\")'"}, "exit code 1\nTraceback (most recent call last):\n  TypeError")
      hints.append(h.after_tools(t, None))
    check(not hints[0] and not hints[1] and "3 failing commands in a row" in hints[2] and "/tmp/ple_io.py" in hints[2] and not hints[3], f"three failing bash calls in a row trigger one hint: {[bool(x) for x in hints]}")
    h.on_tool("bash", {"command": "ls"}, "exit code 0\nfile")
    check(h.err_streak == 0, "a successful call resets the streak")
  finally:
    harness.IO_HELPERS = False
  h2 = harness.SiteHooks(StubSession({}), cl13)
  for t in range(1, 6):
    h2.on_tool("bash", {"command": "x"}, "exit code 1\nTraceback (most recent call last):")
    hint_off = h2.after_tools(t, None)
  check(hint_off == "", "without IO_HELPERS there is no streak hint")

  # 14. PROVENANCE_CHECK end to end in the hooks: corpus from the listing, UNSOURCED into the repair round
  harness.PROVENANCE_CHECK, harness.ENG_REVIEW = True, False
  try:
    gs = GateSession({"/app/out/parts_plan.xlsx": zones, "/app/project/catalogue.txt": b"ACME P-40: pattern 40/7, 40/4; joint Kind K; fastener FIX-X9"}, 3 * 3600)
    hp = harness.SiteHooks(gs, cl13, files=[(60, "/app/project/catalogue.txt"), (100, "/app/out/parts_plan.xlsx")])
    fb = await hp.on_final("done", 30)
    check(fb is not None and "opened your deliverables" in fb and "UNSOURCED: `40/9`" in fb and "FIX-X9" not in fb and "KINDK" not in fb, f"an unsourced designation starts a repair round; sourced ones pass: {fb}")
    check(hp.corpus is not None and hp.corpus.files == 1 and any("provenance: corpus of 1 input files" in l for l in gs.logs), f"the corpus is built from the inputs only (deliverables excluded): {gs.logs}")
    fb2 = await hp.on_final("done", 32)
    check(fb2 is None and any("every designation" in o for o in hp.last_report.ok), f"the same token is not flagged twice; the clean verdict goes into the ok lines: {fb2}")
  finally:
    harness.PROVENANCE_CHECK, harness.ENG_REVIEW = False, True

  # 15. Variant D: ple_io writers round-trip through the readers and the verifier's parsers
  import json as _json  # pylint: disable=g-import-not-at-top
  import tempfile  # pylint: disable=g-import-not-at-top
  import zipfile as _zf  # pylint: disable=g-import-not-at-top
  import ple_io  # pylint: disable=g-import-not-at-top

  with tempfile.TemporaryDirectory() as tmp:
    pth = lambda n: _os.path.join(tmp, n)  # pylint: disable=g-long-lambda
    ents = [
        {"type": "LWPOLYLINE", "layer": "L-AREAS", "points": [[0, 0], [40, 0], [40, 20], [0, 20]], "closed": True},
        {"type": "LINE", "layer": "L-GRID", "start": [0, 10], "end": [40, 10], "color": 3},
        {"type": "CIRCLE", "layer": "L-HOLES", "center": [5, 5], "radius": 1.5},
        {"type": "TEXT", "layer": "L-LABELS", "text": "A1", "insert": [2, 2], "height": 0.8},
        {"type": "MTEXT", "layer": "L-LABELS", "text": "note", "insert": [3, 3], "height": 0.5},
    ]
    w = ple_io.dxf_write(pth("plan.dxf"), ents, units=2)
    check(w == {"L-AREAS": 1, "L-GRID": 1, "L-HOLES": 1, "L-LABELS": 2}, f"dxf_write counts entities per layer: {w}")
    raw_dxf = open(pth("plan.dxf"), "rb").read()
    lf_dxf = raw_dxf.replace(b"\r\n", b"\n")
    crlf_dxf = lf_dxf.replace(b"\n", b"\r\n")  # what CAD (and ezdxf on Windows) writes; used to parse as an empty drawing
    pinfo = parsers.parse("plan.dxf", crlf_dxf, detail=400)
    pinfo_lf = parsers.parse("plan.dxf", lf_dxf, detail=400)
    check(pinfo_lf["by_layer"] == pinfo["by_layer"] and pinfo_lf["layers"] == pinfo["layers"] and pinfo["by_layer"], "CRLF and LF drawings parse the same")
    check(pinfo["ok"] and all(l in pinfo["layers"] for l in w) and pinfo["by_layer"] == w and pinfo["insunits"] == 2, f"the verifier's parser sees the layers in the table and on the entities: {pinfo.get('layers')} {pinfo.get('by_layer')}")
    back = ple_io.dxf_entities(pth("plan.dxf"), layer="L-AREAS")
    check(len(back) == 1 and back[0]["closed"] and back[0]["points"][2] == (40.0, 20.0), f"dxf_entities reads the polyline back with its coordinates: {back}")
    try:
      ple_io.dxf_write(pth("bad.dxf"), [{"type": "SPLINE", "layer": "X"}])
      check(False, "an unknown entity type raises")
    except ValueError as exc:
      check("SPLINE" in str(exc), "an unknown entity type raises with the type named")
    wx = ple_io.xlsx_write(pth("book.xlsx"), {"Params": [["key", "value"], ["K_FT", 12.5]], "Items": [["item_id", "span_ft", "load_psf"], ["I1", 30, 45.0]]})
    rows = ple_io.xlsx_rows(pth("book.xlsx"))
    check(wx == {"Params": 2, "Items": 2} and rows["Items"][1] == ["I1", 30, 45.0] and list(rows) == ["Params", "Items"], f"xlsx_write writes values the reader gets back: {rows}")
    names = ple_io.zip_write(pth("pack.zip"), [pth("plan.dxf"), pth("book.xlsx")])
    check(names == ["plan.dxf", "book.xlsx"] and _zf.ZipFile(pth("pack.zip")).namelist() == names, "zip_write puts the files at the root")
    with open(pth("spec.json"), "w", encoding="utf-8") as fh:
      _json.dump({"units": 4, "entities": ents[:2]}, fh)
    with open(pth("sheets.json"), "w", encoding="utf-8") as fh:
      _json.dump({"S": [["a", "b"], [1, 2]]}, fh)
    rc = ple_io.main(["write-dxf", pth("spec.json"), pth("cli.dxf")])
    rc2 = ple_io.main(["write-xlsx", pth("sheets.json"), pth("cli.xlsx")])
    rc3 = ple_io.main(["write-zip", pth("cli.zip"), pth("cli.dxf"), pth("cli.xlsx")])
    check(rc == 0 and rc2 == 0 and rc3 == 0 and parsers.parse("cli.dxf", open(pth("cli.dxf"), "rb").read(), detail=100)["insunits"] == 4 and _zf.ZipFile(pth("cli.zip")).namelist() == ["cli.dxf", "cli.xlsx"], "the write-dxf / write-xlsx / write-zip commands work from the CLI")
    with open(pth("g.txt"), "w", encoding="utf-8") as fh:
      fh.write("alpha\nbeta 12\n")
    check(ple_io.main(["grep", pth("g.txt"), "beta"]) == 0, "grep still takes one pattern after the CLI change")

    # DXF_LAYER_CHECK: layers judged by their entities, with the fix in the message
    d15 = checklist.Deliverable(path="/app/out/plan.dxf", kind="dxf", layers=["L-AREAS", "L-HOLES", "L-NOTES"])
    import ezdxf as _ezdxf  # pylint: disable=g-import-not-at-top
    doc = _ezdxf.new("R2010")
    doc.layers.add("L-NOTES")  # defined, nothing on it
    msp = doc.modelspace()
    msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "L-AREAS"})  # used, not in the table
    doc.saveas(pth("ent.dxf"))
    ent_dxf = open(pth("ent.dxf"), "rb").read()
    verify.LAYER_ENTITIES = True
    try:
      issues, oks = [], []
      verify._check_bytes(d15.path, ent_dxf, d15, issues, oks)  # pylint: disable=protected-access
      check(any("no entities on required layer(s) ['L-HOLES', 'L-NOTES']" in i and "['L-NOTES'] exist in the layer table but hold nothing" in i and "['L-HOLES'] do not exist at all" in i and "doc.layers.add" in i for i in issues), f"the layer finding separates empty from absent layers and names the fix: {issues}")
      check(any("entities use layer(s) ['L-AREAS'] that are not in the layer table" in i for i in issues), f"entities on an undeclared layer are reported with the fix: {issues}")
      issues, oks = [], []
      verify._check_bytes(d15.path, open(pth("plan.dxf"), "rb").read(), checklist.Deliverable(path="/app/out/plan.dxf", kind="dxf", layers=["L-AREAS", "L-HOLES"]), issues, oks)  # pylint: disable=protected-access
      check(not issues and any("per layer {'L-AREAS': 1" in o for o in oks), f"a drawing with entities on every required layer passes and reports entities per layer: {issues} {oks}")
    finally:
      verify.LAYER_ENTITIES = False
    issues, oks = [], []
    verify._check_bytes(d15.path, ent_dxf, d15, issues, oks)  # pylint: disable=protected-access
    check(len(issues) == 1 and "DXF lacks layers ['L-AREAS', 'L-HOLES']" in issues[0], f"with the switch off the table-only check is unchanged: {issues}")

  # VALUE_CHECK: empty columns, text under unit headers or unit keys, duplicate keys; READBACK echo
  check(verify.unit_of("load_plf") == "plf" and verify.unit_of("Width (mm)") == "mm" and verify.unit_of("K_FT") == "ft" and verify.unit_of("qty") == "" and verify.unit_of("item_id") == "" and verify.unit_of("ft") == "", "unit_of reads the unit a header ends with, and nothing else")
  check(verify.as_number("1,250.5") == 1250.5 and verify.as_number(" 12 ") == 12 and verify.as_number("12 x 8") is None and verify.as_number(True) is None and verify.as_number("") is None, "as_number parses numbers with separators and rejects text")
  wb15 = openpyxl.Workbook()
  ws = wb15.active
  ws.title = "Items"
  for r in [["item_id", "span_ft", "load_psf", "count", "note"], ["I1", 30, 45.0, None, "edge"], ["I2", "30 x 12", 50.0, None, ""], ["I1", 28, "see note", None, ""], ["I3", 26, 40.0, None, ""]]:
    ws.append(r)
  ws2 = wb15.create_sheet("Params")
  for r in [["key", "value"], ["K_FT", "12 x 8"], ["PRESSURE_PSF", "7.25"], ["FACTOR", "n/a"]]:
    ws2.append(r)
  buf15 = io.BytesIO()
  wb15.save(buf15)
  d15x = checklist.Deliverable(path="/app/out/book.xlsx", kind="xlsx")
  rep15 = verify.Report([], [])
  verify._check_bytes(d15x.path, buf15.getvalue(), d15x, rep15.issues, rep15.ok, report=rep15)  # pylint: disable=protected-access
  check([t[1] for t in rep15.tables] == ["Items", "Params"] and len(rep15.tables[0][2]) == 5, f"verify keeps the rows of every sheet for the value checks: {[(t[1], len(t[2])) for t in rep15.tables]}")
  vi = verify.value_issues(rep15)
  check(any("column(s) ['count'] have a header and no values in 4 rows" in i for i in vi), f"an empty column is reported: {vi}")
  check(any("column `span_ft` names a unit but holds text in 1 of 4 cells" in i and "30 x 12" in i for i in vi), f"text in a unit column is reported with examples: {vi}")
  check(any("column `load_psf`" in i for i in vi), f"text in a second unit column is reported: {vi}")
  check(any("row key(s) ['I1'] appear more than once" in i for i in vi), f"duplicate row keys are reported: {vi}")
  check(any("keys that name a unit hold text instead of one number" in i and "K_FT='12 x 8'" in i and "PRESSURE_PSF" not in i and "FACTOR" not in i for i in vi), f"a key/value sheet: text under a unit key is reported, numbers and unitless keys are not: {vi}")
  check(vi[-1].startswith("VALUES means") and all(i.startswith("VALUES") for i in vi), "every finding carries the VALUES prefix and the explanation comes last")
  clean = book([["item_id", "span_ft", "load_psf"], ["I1", 30, 45.0], ["I2", 28, "50"], ["I3", 26, 40.0]], title="Items")
  repc = verify.Report([], [])
  verify._check_bytes(d15x.path, clean, d15x, repc.issues, repc.ok, report=repc)  # pylint: disable=protected-access
  check(verify.value_issues(repc) == [], "a clean table has no VALUES finding (numbers stored as text count as numbers)")
  rb = verify.readback_lines(rep15)
  check("sheet `Params`: K_FT=12 x 8, PRESSURE_PSF=7.25, FACTOR=n/a" in rb and "sheet `Items`: span_ft 26..30 (3); load_psf 40..50 (3)" in rb, f"readback echoes key/value sheets in full and min..max of unit columns: {rb}")
  rep15.issues += vi
  check("VALUES" not in verify.summary_lines(rep15), "value findings stay out of the final message")

  # the hooks: VALUE_CHECK opens a repair round once per finding; READBACK precedes the review prompt; IO_WRITERS extends the helper rules
  cl15 = checklist.Checklist(workdir="/app", base="/app", deliverables=[checklist.Deliverable(path="/app/out/book.xlsx", kind="xlsx")])
  harness.VALUE_CHECK, harness.READBACK, harness.ENG_REVIEW = True, True, True
  try:
    hv = harness.SiteHooks(GateSession({"/app/out/book.xlsx": buf15.getvalue()}, 3 * 3600), cl15)
    fb = await hv.on_final("done", 30)
    check(fb is not None and "opened your deliverables" in fb and "VALUES: " in fb and "['count']" in fb, f"VALUES findings start a repair round: {fb}")
    fb2 = await hv.on_final("done", 32)
    check(fb2 is not None and "VALUES: " not in fb2 and "engineering review pass" in fb2 and "What the harness read back" in fb2 and "K_FT=12 x 8" in fb2 and fb2.index("read back") < fb2.index("engineering review pass"), f"the same findings are not repeated; the review prompt carries the readback first: {fb2}")
    harness.READBACK = False
    hr = harness.SiteHooks(GateSession({"/app/out/book.xlsx": clean}, 3 * 3600), cl15)
    fb3 = await hr.on_final("done", 30)
    check(fb3 is not None and "read back" not in fb3 and "engineering review pass" in fb3, f"without READBACK the review prompt is unchanged: {fb3}")
  finally:
    harness.VALUE_CHECK, harness.READBACK, harness.ENG_REVIEW = False, False, True
  rules_off = harness.helper_rules()
  harness.IO_WRITERS = True
  try:
    rules_on = harness.helper_rules()
  finally:
    harness.IO_WRITERS = False
  check("write-dxf" not in rules_off and rules_on.startswith(rules_off) and "write-dxf SPEC.json OUT.dxf" in rules_on and "ple_io.dxf_write" in rules_on and "/tmp/ple_io.py" in rules_on, "IO_WRITERS appends the writer commands to the helper rules")

  print(f"\n{len(FAILS)} failures")
  return 1 if FAILS else 0


if __name__ == "__main__":
  sys.exit(asyncio.run(main()))
