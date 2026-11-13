# Copyright 2026 ple-sitecheck authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ple-sitecheck: an engineering-task harness built around site-engineer habits.

Per task:
  1. triage      - list the files, probe the container's tooling, read the
                   brief (task.md), parse spreadsheets/PDFs/IFC/DXF in the
                   serve container and hand the model a compact inventory.
  2. checklist   - parse the deliverable paths and shapes from the brief
                   (regex), optionally refined by one cheap model call.
  3. work        - the tool-calling loop (engine.py) with a stable system
                   prompt, the brief, the inventory and the checklist in
                   front; progress nudges at fixed turns.
  4. verify      - when the model says it is done, open every deliverable,
                   parse it and compare it with the checklist; send findings
                   back (bounded repair rounds).
  5. review      - one engineering self-review round (units, magnitudes,
                   code values, assumptions written into the deliverable).
  6. summary     - a structured closing message for the rubric judge, with a
                   harness-written fallback so the run never ends without one.

Switches (submission.yaml `env`): CHECKLIST_LLM, ENG_REVIEW, MAX_REPAIRS,
TRIAGE, FIRST_DRAFT_TURN, MAX_TURNS, REASONING_EFFORT, COMPACT_AT_TOKENS,
CONTEXT_BUDGET_TOKENS, PLACEHOLDER_DELIVERABLES, and the engine's RETRY_*
and MIN_CALL_INTERVAL_SEC.

A run never ends early because the endpoint is rate limited or down: the
engine waits within the task's time budget. If the loop still ends without
the files, the harness verifies what exists, writes an honest placeholder at
each missing deliverable path (verify.placeholder_bytes) and says so in the
final message.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

import checklist as checklist_mod
import engine
import taskbridge
import triage
import verify

CHECKLIST_LLM = os.environ.get("CHECKLIST_LLM", "1") == "1"
ENG_REVIEW = os.environ.get("ENG_REVIEW", "1") == "1"
TRIAGE = os.environ.get("TRIAGE", "1") == "1"
MAX_REPAIRS = int(os.environ.get("MAX_REPAIRS", "2"))
GRACE_TURNS = int(os.environ.get("GRACE_TURNS", "12"))
# tool-free answers while required deliverables are still missing get this
# many "continue" nudges before the harness gives up on the model
MAX_CONTINUES = int(os.environ.get("MAX_CONTINUES", "4"))
FIRST_DRAFT_TURN = int(os.environ.get("FIRST_DRAFT_TURN", "35"))
WRAP_UP_MINUTES = int(os.environ.get("WRAP_UP_MINUTES", "25"))
# the opening turn (system + instruction + brief + inventory + checklist) is
# trimmed to about this many tokens (4 chars per token), inventory first
CONTEXT_BUDGET_TOKENS = int(os.environ.get("CONTEXT_BUDGET_TOKENS", "10000"))
# at the end of a failed run, write an honest "not produced" file at each
# missing deliverable path (see verify.placeholder_bytes for the rules)
PLACEHOLDER_DELIVERABLES = os.environ.get("PLACEHOLDER_DELIVERABLES", "1") == "1"
# A/B switches for small models (off unless set; see DESIGN.md section 11):
# NOTES_PHASE: the model keeps a notes file (governing documents, every input
#   value with unit and source, the structure of each deliverable, the checks)
#   and is nudged whenever READ_SWEEP_TURNS turns pass without updating it;
#   the review round reads the notes back.
# VERIFY_TEMPLATE: deliverables are compared with the template files they are
#   modelled on (sheets, columns, row keys, layers, headings) at verify time.
NOTES_PHASE = os.environ.get("NOTES_PHASE", "0") == "1"
READ_SWEEP_TURNS = int(os.environ.get("READ_SWEEP_TURNS", "6"))
VERIFY_TEMPLATE = os.environ.get("VERIFY_TEMPLATE", "0") == "1"
NOTES_NAME = "harness_notes.md"
# Variant C switches (off unless set; DESIGN.md section 12):
# PROVENANCE_CHECK: at verify time every designation-like string in a tabular
#   deliverable is looked up in the project's input documents; the ones found
#   nowhere come back as UNSOURCED issues (one repair round each, bounded by
#   MAX_REPAIRS; each token flagged once).
# FINISH_GATE: a tool-free answer with most of the turn and time budget unused
#   gets a second-pass prompt with the harness's substance report (rows,
#   chars, pages, entities per deliverable), up to FINISH_GATE_PASSES times,
#   each only if the model did tool work since the previous pass.
# IO_HELPERS: ple_io.py (generic DXF/IFC/XLSX/PDF/DOCX/CSV readers) is copied
#   into the task container and advertised; ERROR_STREAK consecutive failing
#   bash calls append a hint to look the API up instead of guessing.
PROVENANCE_CHECK = os.environ.get("PROVENANCE_CHECK", "0") == "1"
FINISH_GATE = os.environ.get("FINISH_GATE", "0") == "1"
FINISH_GATE_PASSES = int(os.environ.get("FINISH_GATE_PASSES", "2"))
FINISH_GATE_TURN_FRACTION = float(os.environ.get("FINISH_GATE_TURN_FRACTION", "0.5"))
FINISH_GATE_MINUTES = int(os.environ.get("FINISH_GATE_MINUTES", "45"))
IO_HELPERS = os.environ.get("IO_HELPERS", "0") == "1"
HELPER_PATH = os.environ.get("HELPER_PATH", "/tmp/ple_io.py")
ERROR_STREAK = int(os.environ.get("ERROR_STREAK", "3"))
# Variant D switches (off unless set; DESIGN.md section 13):
# IO_WRITERS: the helper's tested writers (DXF with layers, workbook values,
#   zip) are listed in the opening next to the readers (needs IO_HELPERS).
# DXF_LAYER_CHECK: a required drawing layer passes verification when entities
#   sit on it; the finding names the layers that hold nothing, the ones the
#   table lacks, and the two ezdxf calls that fix it.
# VALUE_CHECK: at verify time, columns with a header and no values, text in
#   cells whose header or key names a unit, and duplicate row keys come back
#   as VALUES issues (one repair round each, bounded by MAX_REPAIRS).
# READBACK: the review prompt and the second-pass prompt carry the model's
#   own numbers (key/value sheets in full, min..max per unit-bearing column).
IO_WRITERS = os.environ.get("IO_WRITERS", "0") == "1"
DXF_LAYER_CHECK = os.environ.get("DXF_LAYER_CHECK", "0") == "1"
VALUE_CHECK = os.environ.get("VALUE_CHECK", "0") == "1"
READBACK = os.environ.get("READBACK", "0") == "1"
verify.LAYER_ENTITIES = DXF_LAYER_CHECK

SYSTEM = """You are a senior engineer (civil/structural/mechanical/electrical as the task needs) with site experience, \
working on one engineering task inside a Linux container. Your tools act inside that container: bash (starts in \
{workdir}), read_file, write_file and edit_file. There is no internet access.

How you work:
- Read the whole brief and the harness's file inventory first. Open the governing documents before computing; when \
documents conflict, decide which governs (latest revision, transmittal, hierarchy stated in the brief) and record why.
- Callouts, schedules, revision clouds and notes carry the numbers: read drawings' text entities and tables, not only \
titles. Check every number you take from a document against its units and the clause it comes from.
- Compute with code, never by hand. Carry units explicitly. After each result, sanity-check its order of magnitude \
against engineering experience (loads, stresses, flows, voltages, costs) and against values stated in the project \
documents; investigate anything surprising.
- Write every deliverable exactly at the path, name and format the brief asks for, using real file formats produced \
by code (openpyxl, python-docx, ezdxf, ifcopenshell, ...). Write a first complete version early, then improve it in \
place. Spreadsheets: write computed VALUES into cells (a grader reading values sees None for uncached formulas); keep \
sheet and column names exactly as specified.
- Deliverables must state their basis: the governing documents and clauses, the assumptions you made, the inputs you \
used, and any limitation. A reviewer should be able to trace every number.
- Use the task's own tools (MCP functions or helper scripts under the workspace) when the brief points to project \
applications (documents, spreadsheets, email, CAD) that live there.
- Keep tool outputs small: print summaries, head/tail, and counts rather than whole files.
- Budget: the time and the number of model turns left are shown after each tool result. Leave margin to verify and \
to write the summary.

When the work is complete, answer WITHOUT calling a tool. The harness then opens every deliverable and reports what it \
finds; fix anything it reports. Your final answer is read by the grader together with the files."""

CONTEXT = """{brief}## Deliverable checklist (parsed from the brief{how}; the harness verifies these paths before you finish)
{checklist}

{inventory}

## Working rules for this run
- Decide the governing documents first and list them in your first message with one line each.
- Write a first complete version of every deliverable by turn {first_draft}; iterate after that.
- Before you answer without a tool call, re-open each deliverable and confirm its content and format."""

NOTES_RULES = """
## Working method (required): the notes file `{notes}`
1. Before you compute anything, create `{notes}` with write_file and keep it current. It holds: (a) the governing \
documents you chose, with revision and why the others are set aside; (b) every input value you will use, with its unit \
and its source (file and page / sheet and cell / entity or clause); (c) for each deliverable, its exact structure as the \
brief and the templates give it: sheets, columns, the rows or keys to fill, layers, sections; (d) the checks you will \
run on the result.
2. Read for a purpose: before each read_file, know which value or structure you are after, and append it to the notes \
right after. Do not go more than {sweep} turns without updating the notes. A long document is shown cut: extract the \
part you need with python3 in bash (a page range, one sheet, a search for a term) instead of reading it whole again.
3. Never type a number into code from memory. Every constant in your scripts comes from the notes, with its source in \
a comment next to it. When a value is not in the documents, write the assumption in the notes and in the deliverable.
4. Build the deliverables one at a time: write the script, run it, open the result, compare it with the structure in \
the notes, then move to the next one. Only then answer without a tool call."""

NOTES_NUDGE = (
    "[harness: {n} turns without updating `{notes}`. Record now what you have extracted so far (values with units"
    " and sources; the structure of each deliverable; open questions), then continue.]"
)

REVIEW_NOTES = """Your notes file `{notes}` reads:
{notes_text}

Check every value the deliverables use against these notes and the source documents; a value that is not in the notes \
needs its source added, or an assumption stated in the deliverable.

"""

REVIEW_NO_NOTES = """You never wrote the notes file `{notes}`. Write it now from the source documents (governing \
documents; every input value with unit and source; the structure of each deliverable), then check the deliverables \
against it.

"""

VERIFY_FEEDBACK = """The harness opened your deliverables. {ok}

It found these problems, which the grader would also see:
{issues}

Fix them now (write or correct the files at exactly those paths), re-open them to confirm, and then answer again \
with your final summary. Do not stop before every item above is resolved or explained."""

REVIEW_PROMPT = """The deliverables exist and parse. {ok}

Before you finish, do one engineering review pass as the checking engineer, using your tools:
1. Re-derive the 3 to 5 numbers that drive the result (loads, capacities, flows, ratings, quantities, costs) and \
compare them with what the deliverables state. Check units and order of magnitude on each.
2. Cross-check against the governing documents: the clause, table or schedule each value comes from, and that no \
superseded revision was used.
3. Make sure each deliverable states its basis: governing documents, assumptions, inputs, limitations. Add an \
assumptions/basis section (or sheet, or notes) where the format allows and the brief does not forbid it.
4. Confirm names, sheet names, column headers, layers, dates, signatories and file paths match the brief exactly.
Fix what you find, then answer with your final summary in this structure:
- Deliverables: each file with its full path and one line on what it contains.
- Key results: the governing numbers with units (and the demand/capacity or pass/fail they imply).
- Basis: governing documents and clauses used; documents set aside and why.
- Assumptions and judgement calls: each one with its effect on the result.
- Checks performed: what you verified and how.
- Open items / limitations: anything the reader must know before relying on the result."""

FINAL_PROMPT = """{why} Stop working now and answer without calling a tool. Give the final summary in this structure: \
Deliverables (each path and what it holds); Key results (numbers with units); Basis (governing documents and clauses); \
Assumptions and judgement calls; Checks performed; Open items. Be specific; this answer is graded with the files."""

CONTINUE_PROMPT = """You answered without calling a tool, but the task is not done: these required deliverables do not \
exist yet (or are empty/corrupt):
{missing}

Do not describe a plan; carry it out with your tools now (bash, write_file, edit_file; python3 with the libraries the \
tooling probe listed). Write each file at exactly its path, confirm it exists, then continue improving it. Answer \
without a tool call only when every deliverable is written. (continue nudge {n} of {max})"""

GRACE_PROMPT = """The turn budget is used up, but these required deliverables are still missing or broken:
{issues}

You have {extra} more turns, for this only: write the simplest complete version of each missing file at exactly its \
path (plain values, no optional refinements), confirm it exists, then answer without a tool call with your summary."""

PROVENANCE_RULE = """
- Designations you take from a catalogue, schedule, drawing or template (product codes, types, patterns, grades, \
sizes, options) must be copied exactly as the document writes them, as a combination the document tabulates. When \
you finish, the harness looks every designation in your tables up in the project documents and reports those it \
cannot find."""

HELPER_RULES = """
## Reading project files: a tested helper is in the container (use it before writing your own parser)
- `python3 {helper} dxf PATH [--layer L] [--type LINE|LWPOLYLINE|TEXT|INSERT] [--find TEXT] [--limit N]`: entities \
with layer, type, coordinates, text; the summary line gives entities per layer and the drawing units.
- `python3 {helper} xlsx PATH [--sheet S] [--rows N] [--find TEXT]`: cached cell values as TSV, every sheet.
- `python3 {helper} pdf PATH [--pages a-b] [--tables]`: text per page; `--tables` extracts tables as rows.
- `python3 {helper} ifc PATH [--type IfcWall] [--find TEXT] [--limit N]`: entities with name, tag, placement origin, \
property sets; the summary line gives units and entity counts.
- `python3 {helper} docx|csv|info PATH`; `python3 {helper} grep PATH_OR_DIR REGEX` searches any format, recursively.
- In your own scripts: `import sys; sys.path.insert(0, "{helper_dir}"); import ple_io` then `ple_io.dxf_entities(path, \
layer=None)`, `ple_io.xlsx_rows(path)`, `ple_io.pdf_text(path, "3-5")`, `ple_io.pdf_tables(path)`, \
`ple_io.ifc_entities(path, "IfcWall")`, `ple_io.docx_text(path)`, `ple_io.csv_rows(path)`, `ple_io.text_of(path)`.
- When a library call fails, look the API up (`python3 -c "import ezdxf, inspect; help(ezdxf.readfile)"`, \
`print(dir(obj))`) instead of guessing another attribute name."""

WRITER_RULES = """
- Writing files (tested; use these instead of guessing library calls): `python3 {helper} write-dxf SPEC.json OUT.dxf` \
where SPEC is {{"units": 2, "entities": [{{"type": "LWPOLYLINE", "layer": "L1", "points": [[0,0],[10,0],[10,5]], \
"closed": true}}, {{"type": "LINE", "layer": "L2", "start": [0,0], "end": [5,5]}}, {{"type": "CIRCLE", "layer": "L2", \
"center": [1,1], "radius": 0.5}}, {{"type": "TEXT", "layer": "L3", "text": "A1", "insert": [2,2], "height": 0.5}}]}} \
(layers are created in the layer table; units 1=in 2=ft 4=mm 6=m); `python3 {helper} write-xlsx SPEC.json OUT.xlsx` \
where SPEC is {{"SheetName": [["header1", "header2"], [1, 2.5]]}} (values, header row first); \
`python3 {helper} write-zip OUT.zip FILE...` (files at the zip root). In Python: `ple_io.dxf_write(path, entities, \
units=2)`, `ple_io.xlsx_write(path, sheets)`, `ple_io.zip_write(path, files)`; read a file back with the readers \
above after writing it."""

READBACK_BLOCK = """What the harness read back from your tables (your own numbers; check each one's unit and order of \
magnitude against the source documents and the catalogue or schedule values they come from, and that each key holds \
the quantity its name says):
{readback}

"""

ERROR_STREAK_HINT = (
    "[harness: {n} failing commands in a row. Do not guess another attribute or function name: read the error, then"
    " run `help(...)`/`print(dir(obj))` on the object, or use the tested helper `python3 {helper} ...` from the"
    " opening message to read the file.]"
)

SECOND_PASS_PROMPT = """The deliverables exist and parse, but you have used only {turn} of {max_turns} turns and about \
{minutes} minutes remain, and the grader scores engineering content, not file shape. What the harness measured in each \
deliverable (thinnest first):
{substance}

Make a second pass now (pass {n} of {max}), with your tools:
1. Start with the thinnest item above. A schedule, register or zone table with a handful of rows rarely covers every \
zone, line, member, scenario, load case, requirement or clause the brief and the project documents list: enumerate \
them from the documents with code and fill a row for each; where the brief lists scenarios or cases, every one needs \
its row.
2. For every designation (product, type, pattern, grade, size) and every input constant in your scripts, open the \
document it comes from and copy the value exactly as written there, as a tabulated combination; replace anything \
typed from memory, and record the source next to it in the deliverable.
3. Re-derive the governing numbers from the source data with code and compare them with what the files state; fix \
every disagreement. Check geometry against the latest revision and the measured (as-built, survey, scan, model) data \
where the documents disagree.
4. Re-open each deliverable after writing it. Then answer again with your final summary."""

CHECKLIST_SYSTEM = "You extract a precise deliverable checklist from an engineering brief and answer only with JSON."


class SiteHooks(engine.Hooks):
  """Verification, review and wrap-up behaviour around the engine loop."""

  def __init__(self, session: taskbridge.Session, cl: checklist_mod.Checklist, notes_path: str = "",
               files: list[tuple[int, str]] | None = None):
    self.session = session
    self.cl = cl
    self.repairs = 0
    self.reviewed = False
    self.last_report: verify.Report | None = None
    self.nudged_draft = False
    self.nudged_wrap = False
    self.graced = False
    self.continues = 0
    # NOTES_PHASE: the notes file, turns since the model last touched it, and
    # whether a tool call of the current turn touched it
    self.notes_path = notes_path
    self.since_notes = 0
    self.touched_notes = False
    self.notes_nudges = 0
    # PROVENANCE_CHECK: the input listing (from triage), the corpus built from
    # it on first use, and the tokens already reported
    self.files = files or []
    self.corpus: verify.Corpus | None = None
    self.flagged: set[str] = set()
    # VALUE_CHECK: the findings already reported
    self.flagged_values: set[str] = set()
    # FINISH_GATE: passes sent, and tool calls since the last one
    self.gate_passes = 0
    self.tools_since_gate = 1
    # IO_HELPERS: consecutive failing bash calls, and the turn of the last hint
    self.err_streak = 0
    self.last_err_hint = -10**6

  def on_tool(self, name: str, args: dict[str, Any], result: str) -> None:
    self.tools_since_gate += 1
    if name == "bash":
      head = (result or "")[:40]
      failed = (head.startswith("exit code") and not head.startswith("exit code 0")) or "Traceback (most recent call last)" in (result or "")
      self.err_streak = self.err_streak + 1 if failed else 0
    if not self.notes_path:
      return
    target = str(args.get("path") or "") if name in ("write_file", "edit_file") else ""
    command = str(args.get("command") or "") if name == "bash" else ""
    notes = self.notes_path.rsplit("/", 1)[-1]
    if (target and (target == self.notes_path or target.endswith("/" + notes) or target == notes)) or (
        command and notes in command and (">" in command or "open(" in command or "tee" in command)
    ):
      self.touched_notes = True

  async def on_final(self, text: str, turn: int) -> str | None:
    report = await verify.verify(self.session, self.cl)
    self.last_report = report
    self.session.final_message = compose_final(text, report)
    await self.session.log(
        f"verify: {len(report.ok)} ok, {len(report.issues)} issues after turn {turn}"
    )
    ok = ("Checked: " + "; ".join(report.ok[:8])) if report.ok else ""
    blocking = [i for i in report.issues if i.startswith(("MISSING", "EMPTY", "CORRUPT"))]
    if blocking and self.continues < MAX_CONTINUES:
      # the model stopped (a plan, a question, a partial answer) before the
      # files exist: that is not a finish, and it does not use a repair round
      self.continues += 1
      await self.session.log(f"nudge: continue {self.continues}/{MAX_CONTINUES}, {len(blocking)} deliverables missing")
      return CONTINUE_PROMPT.format(
          missing="\n".join(f"- {i}" for i in blocking[:10]), n=self.continues, max=MAX_CONTINUES
      )
    if PROVENANCE_CHECK and not blocking and self.repairs < MAX_REPAIRS and report.tokens:
      await self._provenance(report)
    if VALUE_CHECK and not blocking and self.repairs < MAX_REPAIRS and report.tables:
      found = [i for i in verify.value_issues(report) if i not in self.flagged_values]
      if len(found) > 1:  # the last line is the explanation
        self.flagged_values.update(found[:-1])
        await self.session.log(f"values: {len(found) - 1} finding(s): " + "; ".join(found[:-1])[:600])
        report.issues.extend(found)
    if report.issues and self.repairs < MAX_REPAIRS:
      self.repairs += 1
      return VERIFY_FEEDBACK.format(
          ok=ok, issues="\n".join(f"- {i}" for i in report.issues[:12])
      )
    if ENG_REVIEW and not self.reviewed and self._time_for_review():
      self.reviewed = True
      readback = self._readback(report)
      if readback:
        await self.session.log(f"readback: {readback.count(chr(10))} line(s) in front of the review prompt")
      return (await self._notes_preface()) + readback + REVIEW_PROMPT.format(ok=ok)
    if FINISH_GATE and self.gate_passes < FINISH_GATE_PASSES and self.tools_since_gate > 0 and self._gate_open(turn):
      self.gate_passes += 1
      self.tools_since_gate = 0
      left = self.session.time_left()
      minutes = int(left // 60) if left is not None else 0
      await self.session.log(f"finish gate: pass {self.gate_passes}/{FINISH_GATE_PASSES} at turn {turn}, {minutes} min left")
      return SECOND_PASS_PROMPT.format(
          turn=turn, max_turns=engine.MAX_TURNS, minutes=minutes, substance=verify.substance_lines(report),
          n=self.gate_passes, max=FINISH_GATE_PASSES,
      ) + ("\n\n" + self._readback(report).rstrip() if READBACK else "")
    return None

  def _readback(self, report: verify.Report) -> str:
    """READBACK: the model's own numbers in front of the review and second-pass prompts."""
    if not READBACK or not report.tables:
      return ""
    lines = verify.readback_lines(report)
    return READBACK_BLOCK.format(readback=lines) if lines else ""

  async def _provenance(self, report: verify.Report) -> None:
    """Looks the deliverables' designations up in the input documents (corpus built once)."""
    if self.corpus is None:
      exclude = [d.path for d in self.cl.deliverables] + [self.notes_path]
      try:
        self.corpus = await verify.build_corpus(self.session, self.files, exclude)
      except (taskbridge.TaskError, taskbridge.Cancelled):
        return
      await self.session.log(f"provenance: corpus of {self.corpus.files} input files, {self.corpus.chars} chars, {len(self.corpus.tokens)} tokens")
    found = verify.provenance_issues(report, self.corpus, self.flagged)
    if found:
      await self.session.log(f"provenance: {len(found) - 1} unsourced designation(s): " + "; ".join(found[:-1])[:600])
      report.issues.extend(found)
    else:
      report.ok.append(f"every designation in the tables ({len(report.tokens)} distinct) appears in a project document")

  def _gate_open(self, turn: int) -> bool:
    left = self.session.time_left()
    if left is not None and left < FINISH_GATE_MINUTES * 60:
      return False
    return turn < FINISH_GATE_TURN_FRACTION * engine.MAX_TURNS

  async def _notes_preface(self) -> str:
    """The model's own notes (or the fact that there are none) in front of the review prompt."""
    if not self.notes_path:
      return ""
    try:
      res = await self.session.read_result(self.notes_path)
    except taskbridge.TaskError:
      return ""
    if not res.get("ok") or int(res.get("size") or 0) == 0:
      return REVIEW_NO_NOTES.format(notes=self.notes_path)
    import base64  # pylint: disable=g-import-not-at-top
    text = base64.b64decode(res.get("content_b64") or "").decode("utf-8", "replace").strip()
    if len(text) > 6000:
      text = text[:6000] + "\n[... cut; the file holds the rest]"
    return REVIEW_NOTES.format(notes=self.notes_path, notes_text=text)

  async def on_budget(self, turn: int) -> tuple[int, str | None]:
    """Grace turns when the budget ends with deliverables missing (once)."""
    if self.graced or not self.cl.deliverables or GRACE_TURNS <= 0:
      return 0, None
    left = self.session.time_left()
    if left is not None and left < 10 * 60:
      return 0, None
    report = await verify.verify(self.session, self.cl)
    self.last_report = report
    self.session.final_message = compose_final(self.session.final_message, report)
    blocking = [i for i in report.issues if i.startswith(("MISSING", "EMPTY", "CORRUPT"))]
    if not blocking:
      return 0, None
    self.graced = True
    await self.session.log(f"budget: {len(blocking)} deliverables missing at turn {turn}; granting {GRACE_TURNS} turns")
    return GRACE_TURNS, GRACE_PROMPT.format(issues="\n".join(f"- {i}" for i in blocking[:10]), extra=GRACE_TURNS)

  def _time_for_review(self) -> bool:
    left = self.session.time_left()
    return left is None or left > 20 * 60

  def final_prompt(self, why: str) -> str:
    return FINAL_PROMPT.format(why=why)

  def after_tools(self, turn: int, left: float | None) -> str:
    notes = []
    if IO_HELPERS and ERROR_STREAK > 0 and self.err_streak >= ERROR_STREAK and turn - self.last_err_hint >= ERROR_STREAK:
      self.last_err_hint = turn
      notes.append(ERROR_STREAK_HINT.format(n=self.err_streak, helper=HELPER_PATH))
    if self.notes_path:
      self.since_notes = 0 if self.touched_notes else self.since_notes + 1
      self.touched_notes = False
      if self.since_notes >= READ_SWEEP_TURNS:
        self.since_notes = 0
        self.notes_nudges += 1
        notes.append(NOTES_NUDGE.format(n=READ_SWEEP_TURNS, notes=self.notes_path))
    if self.cl.deliverables and not self.nudged_draft and turn >= FIRST_DRAFT_TURN:
      self.nudged_draft = True
      notes.append(
          "[harness: by now a first complete version of every deliverable should exist on disk at: "
          + ", ".join(d.path for d in self.cl.deliverables[:6])
          + ". If not, write it next, then keep improving it.]"
      )
    if not self.nudged_wrap and left is not None and left < WRAP_UP_MINUTES * 60:
      self.nudged_wrap = True
      notes.append(
          f"[harness: under {WRAP_UP_MINUTES} minutes left. Make sure every deliverable is written and complete,"
          " then answer without a tool call with your final summary.]"
      )
    return ("\n" + "\n".join(notes)) if notes else ""


def compose_final(text: str, report: verify.Report | None, failure: str = "") -> str:
  """Model text plus the harness's verified file list (the judge reads both)."""
  text = (text or "").strip()
  block = verify.summary_lines(report) if report else ""
  if not text:
    text = "The run ended before the model wrote its summary."
  if failure:
    block = (f"Harness note: the model loop ended without a result: {failure}\n" + block).strip()
  return text + ("\n\n---\n" + block if block else "")


def helper_rules() -> str:
  """The helper's readers in the opening (IO_HELPERS), plus its writers (IO_WRITERS)."""
  text = HELPER_RULES.format(helper=HELPER_PATH, helper_dir=HELPER_PATH.rsplit("/", 1)[0] or "/")
  if IO_WRITERS:
    text += WRITER_RULES.format(helper=HELPER_PATH)
  return text


async def install_helper(session: taskbridge.Session) -> bool:
  """Copies ple_io.py into the task container at HELPER_PATH (IO_HELPERS)."""
  src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ple_io.py")
  try:
    with open(src, "rb") as fh:
      data = fh.read()
    await session.write_file(HELPER_PATH, data)
  except (OSError, taskbridge.TaskError) as exc:
    await session.log(f"helper: could not write {HELPER_PATH}: {exc}")
    return False
  await session.log(f"helper: {HELPER_PATH} written ({len(data)} bytes)")
  return True


def notes_path_for(cl: checklist_mod.Checklist) -> str:
  """Where the model keeps its notes: next to the brief, never inside a deliverable folder."""
  import posixpath  # pylint: disable=g-import-not-at-top

  base = (cl.base or cl.workdir).rstrip("/") or "/"
  path = posixpath.join(base, NOTES_NAME)
  dirs = [d.path.rstrip("/") for d in cl.deliverables if d.kind == "dir" or d.path.endswith("/")]
  if any(path.startswith(d + "/") for d in dirs):
    path = posixpath.join("/tmp", NOTES_NAME)
  return path


def fit_context(system: str, instruction: str, brief: str, inventory: str, checklist: str,
                budget_tokens: int = CONTEXT_BUDGET_TOKENS) -> tuple[str, str, int]:
  """Trims brief and inventory so the opening turn fits the token budget.

  Returns (brief, inventory, estimated_tokens). Estimate: 4 chars per token
  plus ~700 tokens for the tool schemas and the working rules. The inventory
  gives way first (its tail: summaries, then listing), the brief file second;
  the instruction is never cut. Each cut leaves a note on how to read the rest.
  """
  fixed = len(system) + len(instruction) + len(checklist) + 700 * 4
  room = budget_tokens * 4 - fixed
  if room < 2000:
    room = 2000
  if len(brief) + len(inventory) > room:
    keep_inv = max(1500, room - len(brief))
    if len(inventory) > keep_inv:
      inventory = inventory[:keep_inv] + "\n[... inventory cut to fit the context budget; list and read files with your tools]"
    keep_brief = max(1500, room - len(inventory))
    if len(brief) > keep_brief:
      brief = brief[:keep_brief] + "\n[... brief file cut to fit the context budget: read the file itself for the rest]\n\n"
  tokens = (fixed + len(brief) + len(inventory)) // 4
  return brief, inventory, tokens


async def solve(session: taskbridge.Session) -> str:
  """Entry point called by taskbridge for each task."""
  t0 = time.monotonic()
  print(
      f"task: workdir {session.workdir}, model {engine.MODEL}, {len(session.mcp_servers)} MCP server(s),"
      f" time {session.time_left()}",
      file=sys.stderr, flush=True,
  )
  session.final_message = "The run ended before any summary was written."
  cl = checklist_mod.extract(session.instruction, session.workdir)

  brief, inventory = "", ""
  files: list[str] = []
  listing: list[tuple[int, str]] = []  # (size, path) of every workspace file, for the provenance corpus
  if TRIAGE:
    try:
      inv = await triage.inventory(session, cl)
      brief, inventory = inv["brief"], inv["inventory"]
      listing = list(inv.get("files") or [])
      files = [p for _, p in listing]
      if brief:
        # the brief file (task.md) often holds the real deliverable spec
        cl = checklist_mod.extract(session.instruction, session.workdir, brief)
        brief = "## The brief file(s) in the workspace\n" + brief + "\n\n"
    except (taskbridge.TaskError, RuntimeError, ValueError) as exc:
      await session.log(f"triage failed: {exc}")

  how = ""
  if CHECKLIST_LLM:
    text = await engine.one_shot(
        session,
        CHECKLIST_SYSTEM,
        checklist_mod.MODEL_PROMPT
        + "\n\nBRIEF:\n" + session.instruction
        + (("\n\n" + brief) if brief else "")
        + "\n\nREGEX DRAFT (correct it):\n" + cl.to_json(),
        max_tokens=3000,
    )
    added = checklist_mod.merge_model(cl, text) if text else 0
    how = f", refined by the model (+{added})" if text else ""
  await session.log(
      f"checklist: {len(cl.deliverables)} deliverables: " + ", ".join(d.path for d in cl.deliverables)[:1500]
  )
  if VERIFY_TEMPLATE and files:
    paired = checklist_mod.match_templates(cl, files)
    await session.log(
        f"templates: {paired} deliverable(s) paired with a template: "
        + "; ".join(f"{k or d.path.rsplit('/', 1)[-1]} <- {t.rsplit('/', 1)[-1]}" for d in cl.deliverables for k, t in d.templates.items())[:800]
    )
  notes_path = notes_path_for(cl) if NOTES_PHASE else ""

  left = session.time_left()
  system = SYSTEM.format(workdir=session.workdir)
  checklist_text = cl.to_prompt()
  brief, inventory, est_tokens = fit_context(system, session.instruction, brief, inventory, checklist_text)
  context = CONTEXT.format(
      brief=brief,
      how=how,
      checklist=checklist_text,
      inventory=inventory or "(no inventory: the harness could not list the workspace)",
      first_draft=FIRST_DRAFT_TURN,
  )
  if left is not None:
    context += f"\n- Total time for this task: about {int(left // 60)} minutes."
  if PROVENANCE_CHECK:
    context += PROVENANCE_RULE
  if notes_path:
    context += "\n" + NOTES_RULES.format(notes=notes_path, sweep=READ_SWEEP_TURNS)
  if IO_HELPERS and await install_helper(session):
    context += "\n" + helper_rules()
  opening = [
      {"role": "user", "content": session.instruction},
      {"role": "user", "content": context},
  ]
  hooks = SiteHooks(session, cl, notes_path, listing)
  agent = engine.Agent(session, system, opening, hooks)
  await session.log(
      f"context: instruction {len(session.instruction)} chars, brief file {len(brief)} chars, inventory"
      f" {len(inventory)} chars, checklist {len(cl.deliverables)} deliverables, about {est_tokens} tokens"
      f" (budget {CONTEXT_BUDGET_TOKENS}); triage+checklist took {time.monotonic() - t0:.0f}s; starting the loop"
  )
  text, failure = "", ""
  try:
    text = await agent.run()
    failure = agent.failure
  except (taskbridge.Cancelled, asyncio.CancelledError):
    raise
  except taskbridge.TaskError as exc:
    # the task container stopped answering: nothing more can be written
    failure = f"task container error: {exc}"
    await session.log(f"model loop aborted: {failure}")
  except Exception as exc:  # pylint: disable=broad-exception-caught
    # a harness bug must not turn a run with files on disk into an `error`
    # frame without a summary; the message records it for the log and judge
    failure = f"harness error {type(exc).__name__}: {str(exc)[:300]}"
    await session.log(f"model loop crashed: {failure}")

  report = hooks.last_report
  left = session.time_left()
  if (report is None or report.issues) and (left is None or left > 4) and not failure.startswith("task container"):
    try:
      report = await verify.verify(session, cl)
      if PLACEHOLDER_DELIVERABLES and report.issues and (left is None or left > 3):
        why = failure or "the model answered without producing it"
        if await verify.write_placeholders(session, cl, report, why):
          report = await verify.verify(session, cl)
    except (taskbridge.TaskError, taskbridge.Cancelled):
      pass
  final = compose_final(text, report, failure)
  session.final_message = final
  await session.log(
      f"done after {time.monotonic() - t0:.0f}s, {agent.turn} turns, {agent.retries} retried calls"
      + (f", loop failure: {failure[:120]}" if failure else "")
  )
  return final
