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

"""Deliverable verification: exists, non-empty, parses, has the asked shape.

Runs in the serve container on bytes read over the protocol, so it costs no
model tokens. Findings go back to the model as plain text; `ok` lines go into
the final message so the judge sees what was delivered and checked.
"""

from __future__ import annotations

import dataclasses
import io
import posixpath
import re
import zipfile
from typing import Any

import parsers
import taskbridge

MIN_TEXT_CHARS = 300
MIN_DOC_CHARS = 400

# A deliverable the model never produced is replaced, at the very end of a
# failed run, by a file that says so. It carries no engineering content (no
# values, no sheet names, no layers from the brief), so it answers nothing
# and cannot be mistaken for a result: it only makes the failure visible in
# the deliverable the grader opens. Rules: kit docs/rules.md "Deliverables
# already written in the task container before the failure are still graded"
# and "Do not hard-code task answers"; docs/artifacts.md lists "Nothing to
# grade (no deliverable file and no non-empty final_message.md)" as a task
# scored 0, the same as a placeholder, so this changes no score; it leaves a
# traceable record in the deliverable and the final message instead of
# silence. Formats the placeholder is written in: text-like files, docx,
# xlsx, pdf, zip. CAD/BIM/code formats get none (a fake model is noise).
MARKER = "PLE-SITECHECK PLACEHOLDER"
PLACEHOLDER_NOTE = (
    f"{MARKER}: this deliverable was NOT produced. The harness wrote this file at the end of the run because the"
    " model never created it ({reason}). It contains no engineering content, no results and no assumptions; treat"
    " the deliverable as missing."
)
TEXT_LIKE = ("md", "txt", "csv", "tsv", "json", "yaml", "yml", "html", "xml", "rst", "")


@dataclasses.dataclass
class Report:
  issues: list[str]
  ok: list[str]
  # (label, parsed info) of every deliverable (and zip member) that parsed,
  # for the substance report (FINISH_GATE)
  infos: list[tuple[str, dict[str, Any]]] = dataclasses.field(default_factory=list)
  # designation-like strings found in tabular deliverables: token -> where
  # (PROVENANCE_CHECK)
  tokens: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
  # (label, sheet, rows) of every tabular deliverable, header row first
  # (VALUE_CHECK, READBACK)
  tables: list[tuple[str, str, list[list[Any]]]] = dataclasses.field(default_factory=list)

  @property
  def passed(self) -> bool:
    return not self.issues


# DXF_LAYER_CHECK (variant D): a required drawing layer counts when entities
# sit on it, not when its name is in the layer table. Set by the harness.
LAYER_ENTITIES = False


def placeholder_bytes(path: str, reason: str) -> bytes | None:
  """The honest placeholder for `path`'s format, or None for formats without one."""
  note = PLACEHOLDER_NOTE.format(reason=reason[:300])
  ext = parsers.ext_of(path)
  if ext in TEXT_LIKE:
    if ext == "json":
      import json  # pylint: disable=g-import-not-at-top
      return json.dumps({"placeholder": True, "note": note}, indent=1).encode()
    return (note + "\n").encode()
  if ext == "docx":
    import docx  # pylint: disable=g-import-not-at-top
    d = docx.Document()
    d.add_paragraph(note)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
  if ext in ("xlsx", "xlsm"):
    import openpyxl  # pylint: disable=g-import-not-at-top
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PLACEHOLDER"
    ws.append([note])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
  if ext == "pdf":
    import pymupdf  # pylint: disable=g-import-not-at-top
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in _wrap(note, 90):
      page.insert_text((72, y), line, fontsize=10)
      y += 14
    return doc.tobytes()
  if ext == "zip":
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
      zf.writestr("PLACEHOLDER_README.txt", note + "\n")
    return buf.getvalue()
  return None


def _wrap(text: str, width: int) -> list[str]:
  words, lines, cur = text.split(), [], ""
  for w in words:
    if len(cur) + len(w) + 1 > width:
      lines.append(cur)
      cur = w
    else:
      cur = (cur + " " + w).strip()
  if cur:
    lines.append(cur)
  return lines


async def write_placeholders(session: taskbridge.Session, cl: Any, report: Report, reason: str) -> list[str]:
  """Writes a placeholder for every MISSING/EMPTY file deliverable; returns the paths written."""
  written = []
  for d in cl.deliverables:
    if d.kind == "dir" or d.path.endswith("/"):
      continue
    if not any(i.startswith(("MISSING: " + d.path, "EMPTY: " + d.path)) for i in report.issues):
      continue
    data = placeholder_bytes(d.path, reason)
    if data is None:
      continue
    try:
      # never over an existing non-empty file: re-check right before writing
      res = await session.read_result(d.path)
      if res.get("ok") and int(res.get("size") or 0) > 0:
        continue
      await session.write_file(d.path, data)
      written.append(d.path)
    except (taskbridge.TaskError, taskbridge.Cancelled):
      break
  if written:
    await session.log(f"placeholder deliverables written (no content): {written}")
  return written


def is_placeholder(info: dict[str, Any]) -> bool:
  """Whether a parsed deliverable is one of our placeholders."""
  kind = info.get("kind")
  if kind == "xlsx":
    return list(info.get("sheets") or {}) == ["PLACEHOLDER"]
  if kind == "zip":
    return "PLACEHOLDER_README.txt" in (info.get("root") or [])
  text = info.get("text") or info.get("head") or ""
  if kind == "json":
    text = str(info.get("data") or "")
  return MARKER in text


async def verify(session: taskbridge.Session, cl: Any) -> Report:
  """Checks every deliverable of the checklist."""
  issues, ok = [], []
  report = Report(issues, ok)
  for d in cl.deliverables:
    if d.kind == "dir" or d.path.endswith("/"):
      await _check_dir(session, d.path, issues, ok)
      continue
    templates = await _load_templates(session, getattr(d, "templates", None) or {})
    try:
      result = await session.read_result(d.path)
    except taskbridge.TaskError as exc:
      issues.append(f"{d.path}: could not read ({exc})")
      continue
    if not result.get("ok"):
      issues.append(f"MISSING: {d.path} does not exist (the task requires it at exactly this path)")
      continue
    size = int(result.get("size") or 0)
    if size == 0:
      issues.append(f"EMPTY: {d.path} is 0 bytes")
      continue
    if result.get("truncated"):
      ok.append(f"{d.path}: exists, {size} bytes (too large to parse here)")
      continue
    import base64  # pylint: disable=g-import-not-at-top
    data = base64.b64decode(result.get("content_b64") or "")
    _check_bytes(d.path, data, d, issues, ok, templates=templates, report=report)
  report.issues, report.ok = issues, ok
  return report


async def _load_templates(session: taskbridge.Session, paths: dict[str, str]) -> dict[str, tuple[str, dict[str, Any]]]:
  """Parses the template files a deliverable (or its zip members) is modelled on."""
  out: dict[str, tuple[str, dict[str, Any]]] = {}
  for key, path in paths.items():
    try:
      data = await session.read_file(path)
    except taskbridge.TaskError:
      continue
    info = parsers.parse(path, data, detail=200)
    if info.get("ok"):
      out[key] = (path, info)
  return out


def _looks_like_id(value: str) -> bool:
  """A short token with a digit or a separator, as row keys tend to be (W02, G1_FOOTPRINT, P-12)."""
  v = value.strip()
  return 1 < len(v) <= 24 and " " not in v and bool(re.search(r"[0-9_\-./]", v))


def _check_template(label: str, info: dict[str, Any], tpl: tuple[str, dict[str, Any]], issues: list[str], ok: list[str]) -> None:
  """Compares a parsed deliverable with the parsed template it was modelled on.

  Format rules only: the sheets, columns and row keys a workbook template
  lays out, the layers of a drawing template, the headings of a document
  template. Nothing here knows what the values should be.
  """
  tpath, tinfo = tpl
  tname = tpath.rsplit("/", 1)[-1]
  found = []
  kind = info.get("kind")
  if kind == "xlsx" and tinfo.get("kind") == "xlsx":
    sheets, tsheets = info["sheets"], tinfo["sheets"]
    for name, ts in tsheets.items():
      if name not in sheets:
        if ts["rows"] > 0:
          found.append(f"sheet `{name}` of the template is missing (sheets present: {list(sheets)[:8]})")
        continue
      s = sheets[name]
      header = [str(h).strip() for h in s["header"]]
      theader = [str(h).strip() for h in ts["header"]]
      lacking = [h for h in theader if h not in header]
      if lacking and len(lacking) * 2 > len(theader):
        found.append(f"sheet `{name}`: the template's header is {theader[:12]}; yours is {header[:12]}")
      elif lacking:
        found.append(f"sheet `{name}`: template columns missing from your header: {lacking[:8]}")
      tkeys = [k for k in ts.get("first_col", [])[1:] if _looks_like_id(k)]
      if len(tkeys) >= 3 and len(tkeys) * 2 >= max(1, ts["rows"] - 1):
        have = set(s.get("first_col", []))
        missing = [k for k in tkeys if k not in have]
        if len(missing) * 3 > len(tkeys):
          found.append(
              f"sheet `{name}`: the template lists {len(tkeys)} rows by key in column A and {len(missing)} of them"
              f" are not in your sheet (e.g. {missing[:6]}); if the template lists the rows to fill, every one must be"
              " present with its key unchanged"
          )
      elif ts["rows"] >= 4 and s["rows"] * 2 < ts["rows"]:
        found.append(
            f"sheet `{name}`: the template has {ts['rows']} non-empty rows, yours {s['rows']}; if the template"
            " lists the rows to fill, fill every one"
        )
  elif kind == "dxf" and tinfo.get("kind") == "dxf":
    missing = [l for l in tinfo.get("layers", []) if l not in info.get("layers", []) and l != "0"]
    if missing:
      found.append(f"layers of the template drawing missing from yours: {missing[:10]}")
  elif kind == "docx" and tinfo.get("kind") == "docx":
    theads = tinfo.get("headings") or []
    text = (info.get("text") or "").lower()
    missing = [h for h in theads if h.lower() not in text]
    if len(theads) >= 2 and len(missing) * 2 > len(theads):
      found.append(f"the template's section headings are not in your document: {missing[:8]}")
    if theads and tinfo.get("tables", 0) and not info.get("tables"):
      found.append(f"the template has {tinfo['tables']} table(s); yours has none")
  elif kind == "csv" and tinfo.get("kind") == "csv":
    theader = [h.strip() for h in tinfo.get("header", [])]
    header = [h.strip() for h in info.get("header", [])]
    lacking = [h for h in theader if h not in header]
    if lacking:
      found.append(f"template columns missing from your CSV header: {lacking[:10]}")
  for f in found:
    issues.append(f"{label} vs template `{tname}`: {f}")
  if not found:
    ok.append(f"{label}: structure matches template {tname}")


async def _check_dir(session: taskbridge.Session, path: str, issues: list[str], ok: list[str]) -> None:
  try:
    res = await session.exec(
        f"find {_q(path)} -type f -newer /proc/1 2>/dev/null | head -n 50; echo ---; find {_q(path)} -type f 2>/dev/null | wc -l",
        timeout_sec=60,
    )
  except taskbridge.TaskError as exc:
    issues.append(f"{path}: could not list ({exc})")
    return
  out = res.get("output") or ""
  new, _, total = out.partition("---")
  new_files = [l for l in new.splitlines() if l.strip()]
  if new_files:
    ok.append(f"{path}: {len(new_files)} file(s) written during this run, e.g. {new_files[:5]}")
  elif total.strip() == "0":
    issues.append(f"{path}: the deliverable folder is empty")
  else:
    ok.append(f"{path}: folder holds {total.strip()} files (none detectably new)")


def _q(s: str) -> str:
  return "'" + s.replace("'", "'\\''") + "'"


def _check_bytes(
    path: str, data: bytes, d: Any, issues: list[str], ok: list[str], inside: str = "",
    templates: dict[str, tuple[str, dict[str, Any]]] | None = None, report: Report | None = None,
) -> None:
  """Parses `data` as `path`'s kind and checks it against `d`'s expectations."""
  label = f"{inside}{path}"
  info = parsers.parse(path, data, detail=400)
  kind = info.get("kind")
  if not info.get("ok"):
    issues.append(f"CORRUPT: {label} does not parse as {kind}: {info.get('error')}")
    return
  if is_placeholder(info):
    issues.append(f"PLACEHOLDER: {label} holds only the harness's placeholder note (no engineering content)")
    return
  templates = templates or {}
  if report is not None and kind != "zip":
    report.infos.append((label, info))
    try:
      collect_tokens(label, kind, data, info, report.tokens)
    except Exception:  # pylint: disable=broad-exception-caught
      pass
    try:
      for sheet, rows in table_rows(kind, data):
        report.tables.append((label, sheet, rows))
    except Exception:  # pylint: disable=broad-exception-caught
      pass
  if kind == "zip":
    root = info["root"]
    for m in d.members:
      if m in root:
        continue
      nested = [n for n in info["nested"] if n.endswith("/" + m)]
      if nested:
        issues.append(f"{label}: `{m}` is at `{nested[0]}` but the brief wants it at the zip ROOT")
      else:
        issues.append(f"{label}: zip lacks required member `{m}`")
    zf = info["zip"]
    for name in root:
      sub = _member_spec(d, name)
      try:
        _check_bytes(
            name, zf.read(name), sub, issues, ok, inside=f"{path}::",
            templates={"": templates[name]} if name in templates else None, report=report,
        )
      except Exception as exc:  # pylint: disable=broad-exception-caught
        issues.append(f"{label}::{name}: unreadable member ({exc})")
    ok.append(f"{label}: zip with {len(info['members'])} files, root: {root[:12]}")
    return
  if "" in templates:
    _check_template(label, info, templates[""], issues, ok)
  if kind == "xlsx":
    sheets = info["sheets"]
    for want, cols in d.sheets.items():
      if "::" in want:
        continue
      if want not in sheets:
        close = [s for s in sheets if s.lower().strip() == want.lower().strip()]
        issues.append(
            f"{label}: sheet `{want}` missing"
            + (f" (found `{close[0]}`: names are case/space sensitive)" if close else f" (sheets present: {list(sheets)[:10]})")
        )
        continue
      header = [str(h).strip() for h in sheets[want]["header"]]
      missing = [c for c in cols if c not in header]
      if missing:
        issues.append(f"{label} sheet `{want}`: header lacks columns {missing} (header row 1 is {header[:16]})")
      if sheets[want]["rows"] < 2:
        issues.append(f"{label} sheet `{want}`: has no data rows under the header")
    uncached = sum(s["uncached"] for s in sheets.values())
    if uncached:
      issues.append(
          f"{label}: {uncached} formula cells have no cached value. A grader that opens the workbook with"
          " openpyxl(data_only=True) reads None for them. Write computed numbers into the cells (keep"
          " formulas only in a separate calc sheet, or write both)."
      )
    empty = [s for s, v in sheets.items() if v["rows"] == 0]
    if empty and len(empty) == len(sheets):
      issues.append(f"{label}: every sheet is empty")
    ok.append(f"{label}: workbook, sheets " + ", ".join(f"{s} ({v['rows']} rows)" for s, v in sheets.items()))
    return
  if kind == "docx":
    if info["chars"] < MIN_DOC_CHARS:
      issues.append(f"{label}: Word document has only {info['chars']} characters of text")
    if info["placeholders"]:
      issues.append(f"{label}: template placeholders left unfilled: {info['placeholders'][:6]}")
    ok.append(f"{label}: Word document, {info['paragraphs']} paragraphs, {info['tables']} tables, {info['chars']} chars")
    return
  if kind == "pdf":
    if info["pages"] == 0:
      issues.append(f"{label}: PDF has no pages")
    elif info["chars"] < 20:
      issues.append(f"{label}: PDF has no text layer (a judge extracts text; render text, not only images)")
    ok.append(f"{label}: PDF, {info['pages']} pages")
    return
  if kind == "ifc":
    if info["products"] == 0:
      issues.append(f"{label}: IFC has no IfcProduct entities (nothing placed)")
    if not str(info["schema"]).upper().startswith("IFC4"):
      issues.append(f"{label}: IFC schema is {info['schema']}; the brief likely wants IFC4")
    if info["projects"] != 1 or info["sites"] == 0:
      issues.append(f"{label}: IFC needs exactly one IfcProject and at least one IfcSite (found {info['projects']} / {info['sites']})")
    ok.append(f"{label}: IFC {info['schema']}, {info['products']} products")
    return
  if kind == "dxf":
    want = [l for l in d.layers if "::" not in l]
    if LAYER_ENTITIES:
      _check_layers(label, info, want, issues, ok)
      return
    missing = [l for l in want if l not in info["layers"]]
    if missing:
      issues.append(f"{label}: DXF lacks layers {missing} (has {info['layers'][:20]})")
    if sum(info["counts"].values()) == 0:
      issues.append(f"{label}: DXF modelspace is empty")
    ok.append(f"{label}: DXF, {sum(info['counts'].values())} entities, layers {info['layers'][:10]}")
    return
  if kind == "json":
    ok.append(f"{label}: valid JSON")
    return
  if kind == "csv":
    if info["rows"] < 2:
      issues.append(f"{label}: CSV has no data rows")
    ok.append(f"{label}: CSV, {info['rows']} rows, header {info['header'][:10]}")
    return
  if kind == "py":
    ok.append(f"{label}: Python source compiles")
    return
  if kind == "text":
    if info["chars"] < MIN_TEXT_CHARS and parsers.ext_of(path) in ("md", "txt"):
      issues.append(f"{label}: only {info['chars']} characters; a narrative deliverable this short will not satisfy a judge")
    ok.append(f"{label}: text, {info['lines']} lines")
    return
  ok.append(f"{label}: {kind}, {len(data)} bytes")


def _member_spec(zipd: Any, name: str) -> Any:
  """Expectations for a zip member, from `member::sheet` / `member::layer` keys."""
  import checklist  # pylint: disable=g-import-not-at-top

  sub = checklist.Deliverable(path=name, kind=parsers.ext_of(name))
  for key, cols in zipd.sheets.items():
    member, sep, sheet = key.partition("::")
    if sep and member == name:
      sub.sheets[sheet] = cols
  for key in zipd.layers:
    member, sep, layer = key.partition("::")
    if sep and member == name:
      sub.layers.append(layer)
  return sub


def summary_lines(report: Report) -> str:
  """A compact block for the final message."""
  lines = []
  if report.ok:
    lines.append("Files delivered (opened and parsed by the harness after the run):")
    lines += [f"- {line}" for line in report.ok[:20]]
  # provenance findings are advice to the model, not a verdict on the files
  gaps = [i for i in report.issues if not i.startswith((UNSOURCED, VALUES))]
  if gaps:
    lines.append("Known gaps at the end of the run:")
    lines += [f"- {line}" for line in gaps[:10]]
  return "\n".join(lines)


# ---------------------------------------------------------------------------
# PROVENANCE_CHECK: designations in tabular deliverables (product codes, types,
# patterns, grades, sizes) looked up in the project's own documents. Generic:
# it knows no values, only whether a string the model wrote appears anywhere
# in the inputs. Row keys / ID columns are skipped (they are the model's own).
UNSOURCED = "UNSOURCED"
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-./+]*")
_NUMBER_RE = re.compile(r"[-+]?\d+(\.\d+)?([eE][-+]?\d+)?%?")
_SIMPLE_ID_RE = re.compile(r"[A-Za-z]{1,3}-?\d{1,4}[A-Za-z]?")
_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}")
_PATTERN_RE = re.compile(r"\d+[/x]\d+([/x]\d+)?")  # 36/7, 2x4, 12x12x1
_CLASS_RE = re.compile(r"[A-Za-z]{2,12} ?[A-Z0-9]")  # Type B, Grade 5, Class C
_STOP = {"n/a", "na", "none", "tbd", "tbc", "yes", "no", "ok", "pass", "fail", "true", "false", "null", "nan", "n.a.", "see", "ref", "ref.", "-", "--"}
_FREE_TEXT_COLS = ("id", "name", "description", "notes", "note", "comment", "comments", "remark", "remarks", "title")


def _squash(s: str) -> str:
  return re.sub(r"[\s_\-./+]", "", s.lower())


def designation_like(value: str) -> bool:
  """A short code-like string: letters with a digit or separator, a pattern like 36/7, an all-caps word, 'Type B'."""
  v = value.strip().strip(".,;:()[]")
  if not v or len(v) > 24 or "\n" in v or v.count(" ") > 1:
    return False
  if _NUMBER_RE.fullmatch(v) or _DATE_RE.fullmatch(v) or v.lower() in _STOP:
    return False
  if _SIMPLE_ID_RE.fullmatch(v):
    return False  # Z1, S01, P-12, W02: row keys and scenario ids, not designations
  if _PATTERN_RE.fullmatch(v) or _CLASS_RE.fullmatch(v):
    return True
  if " " in v or not re.search(r"[A-Za-z]", v):
    return False
  return bool(re.search(r"\d", v) or re.search(r"[-_/.]", v) or (v.isupper() and v.isalpha() and len(v) >= 3))


def _skip_column(header: str, values: list[str]) -> bool:
  h = header.strip().lower()
  if h.endswith("_id") or h in _FREE_TEXT_COLS or h.split()[-1:] == ["id"]:
    return True
  if len(values) >= 5 and len(set(values)) > 0.8 * len(values):
    return True  # unique per row: an identifier column
  return False


def _table_tokens(label: str, sheet: str, rows: list[list[Any]], tokens: dict[str, dict[str, Any]]) -> None:
  rows = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
  if len(rows) < 2:
    return
  header = ["" if c is None else str(c) for c in rows[0]]
  width = max(len(r) for r in rows)
  for j in range(1, width):  # column 0 = row keys
    vals = [str(r[j]).strip() for r in rows[1:] if j < len(r) and r[j] is not None and str(r[j]).strip()]
    if not vals or _skip_column(header[j] if j < len(header) else "", vals):
      continue
    for v in vals:
      if designation_like(v):
        key = v.strip().strip(".,;:()[]")
        t = tokens.setdefault(key, {"n": 0, "where": f"{label}" + (f" sheet `{sheet}`" if sheet else "") + f" column `{header[j] if j < len(header) else j + 1}`"})
        t["n"] += 1


def collect_tokens(label: str, kind: str, data: bytes, info: dict[str, Any], tokens: dict[str, dict[str, Any]]) -> None:
  """Adds the designation-like cell strings of a tabular deliverable to `tokens`."""
  if kind == "xlsx":
    import openpyxl  # pylint: disable=g-import-not-at-top
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    for ws in wb.worksheets:
      rows = []
      for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i > 5000:
          break
        rows.append(list(row))
      _table_tokens(label, ws.title, rows, tokens)
  elif kind == "csv":
    import csv  # pylint: disable=g-import-not-at-top
    rows = list(csv.reader(io.StringIO(data.decode("utf-8", "replace"))))[:5000]
    _table_tokens(label, "", rows, tokens)
  elif kind == "docx":
    import docx  # pylint: disable=g-import-not-at-top
    d = docx.Document(io.BytesIO(data))
    for k, t in enumerate(d.tables):
      _table_tokens(label, f"table {k + 1}", [[c.text for c in r.cells] for r in t.rows], tokens)
  elif kind == "dxf":
    for text in info.get("texts") or []:
      for word in str(text).split():
        if designation_like(word):
          key = word.strip(".,;:()[]")
          tokens.setdefault(key, {"n": 0, "where": f"{label} text entities"})["n"] += 1


class Corpus:
  """Every token of the project's input documents, plain and squashed (Type B == TYPEB == type-b)."""

  def __init__(self):
    self.tokens: set[str] = set()
    self.squashed: set[str] = set()
    self.texts: list[str] = []  # lower-case text per file, for the substring fallback
    self.squashed_texts: list[str] = []
    self.files = 0
    self.chars = 0

  def add(self, text: str) -> None:
    lower = text.lower()
    words = _TOKEN_RE.findall(lower)
    self.tokens.update(words)
    self.squashed.update(_squash(w) for w in words)
    self.squashed.update(_squash(a + b) for a, b in zip(words, words[1:]))
    self.texts.append(lower)
    self.squashed_texts.append(_squash(lower))
    self.files += 1
    self.chars += len(text)

  def has(self, token: str) -> bool:
    t = token.lower().strip(".,;:()[]")
    if t in self.tokens or _squash(t) in self.squashed:
      return True
    # a part of a longer token (`loads.csv` in `inputs/loads.csv`), or spaced
    # differently (`Type B` in `type-B`): substring search, misses only
    st = _squash(t)
    return any(t in x for x in self.texts) or (len(st) >= 3 and any(st in x for x in self.squashed_texts))


CORPUS_SKIP_EXTS = {"zip", "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "pyc", "so", "bin", "gz", "tar", "7z", "db", "sqlite"}


async def build_corpus(session: taskbridge.Session, files: list[tuple[int, str]], exclude: list[str],
                       max_total: int = 120_000_000) -> Corpus:
  """Reads the input files over the protocol and indexes their text (once per run)."""
  corpus = Corpus()
  total = 0
  for size, path in sorted(files, key=lambda f: f[0]):
    ext = parsers.ext_of(path)
    if size == 0 or size > 9_500_000 or ext in CORPUS_SKIP_EXTS or total > max_total:
      continue
    if any(path == e or path.startswith(e.rstrip("/") + "/") for e in exclude if e):
      continue
    try:
      data = await session.read_file(path)
    except (taskbridge.TaskError, taskbridge.Cancelled):
      continue
    total += len(data)
    if ext in parsers.EXTRACT_EXTS:
      text = parsers.extract_text(path, data, 4_000_000)
    elif b"\0" in data[:4096]:
      continue
    else:
      text = data.decode("utf-8", "replace")
    corpus.add(text)
  return corpus


def provenance_issues(report: Report, corpus: Corpus, flagged: set[str], limit: int = 8) -> list[str]:
  """UNSOURCED lines for designations that appear in no project document (each flagged once per run)."""
  found = []
  for token, t in sorted(report.tokens.items(), key=lambda kv: -kv[1]["n"]):
    if token in flagged or corpus.has(token):
      continue
    flagged.add(token)
    found.append(f"{UNSOURCED}: `{token}` ({t['where']}, {t['n']} cell(s)) appears in no project document")
    if len(found) >= limit:
      break
  if found:
    found.append(
        f"{UNSOURCED} means: a designation you wrote (product, type, pattern, grade, size, option) is not in any"
        " catalogue, schedule, drawing, template or specification of this project. Copy it exactly as the"
        " source document writes it (open the document and check the exact tabulated combination), or, if it"
        " is your own choice with no source, say so in the deliverable's basis."
    )
  return found


# ---------------------------------------------------------------------------
# FINISH_GATE: what the harness measured in each deliverable, thinnest first.
def substance_lines(report: Report, limit: int = 24) -> str:
  rows: list[tuple[int, str]] = []  # (thinness rank, line)
  for label, info in report.infos:
    kind = info.get("kind")
    name = label.rsplit("/", 1)[-1]
    if kind == "xlsx":
      for s, v in info["sheets"].items():
        n = max(0, v["rows"] - 1)
        rows.append((n, f"{name} sheet `{s}`: {n} data rows, {len(v['header'])} columns" + (" (thin)" if n <= 3 else "")))
    elif kind == "csv":
      n = max(0, info["rows"] - 1)
      rows.append((n, f"{name}: {n} data rows" + (" (thin)" if n <= 3 else "")))
    elif kind == "text":
      rows.append((info["chars"] // 400, f"{name}: {info['chars']} characters, {info['lines']} lines" + (" (thin)" if info["chars"] < 1500 else "")))
    elif kind == "docx":
      rows.append((info["chars"] // 400, f"{name}: {info['chars']} characters, {info['paragraphs']} paragraphs, {info['tables']} tables" + (" (thin)" if info["chars"] < 1500 else "")))
    elif kind == "pdf":
      rows.append((info["pages"], f"{name}: {info['pages']} page(s), {info['chars']} text characters" + (" (thin)" if info["pages"] <= 1 or info["chars"] < 1500 else "")))
    elif kind == "dxf":
      by = info.get("by_layer") or {}
      empty = [l for l in info.get("layers", []) if l not in by and l != "0"]
      rows.append((sum(by.values()) // 10, f"{name}: {sum(by.values())} entities; per layer {dict(list(by.items())[:12])}" + (f"; layers with nothing on them: {empty[:8]}" if empty else "")))
    elif kind == "ifc":
      rows.append((info["products"], f"{name}: {info['products']} IfcProduct entities"))
  rows.sort(key=lambda r: r[0])
  return "\n".join(f"- {line}" for _, line in rows[:limit]) or "- (nothing parsed)"


# ---------------------------------------------------------------------------
# Variant D. DXF_LAYER_CHECK: layers judged by what sits on them. VALUE_CHECK:
# empty columns, non-numeric cells under unit-bearing headers, duplicate row
# keys. READBACK: the model's own numbers echoed back before the review. None
# of it knows what a value should be; it reads the model's files only.
VALUES = "VALUES"
_UNIT_TOKENS = {
    "psf", "plf", "pcf", "psi", "ksi", "kip", "kips", "klf", "lb", "lbs", "lbf", "kn", "kpa", "mpa", "gpa", "pa",
    "nm", "knm", "kipft", "kft", "ft", "in", "inch", "mm", "cm", "m", "m2", "m3", "sf", "sqft", "sqm", "cf", "cy",
    "ea", "pct", "%", "deg", "degc", "degf", "sec", "s", "min", "hr", "h", "hours", "days", "cfm", "gpm", "lps",
    "a", "amp", "amps", "v", "kv", "kw", "kva", "kwh", "mwh", "hz", "mph", "kmh", "ms", "usd", "eur", "gbp", "pln",
    "kg", "t", "ton", "tons", "tonne", "g", "mm2", "in2", "in3", "in4", "mm4", "ratio", "count", "qty", "no", "nos",
}
_UNIT_SPLIT = re.compile(r"[\s_\-/()\[\],]+")


def unit_of(header: str) -> str:
  """The unit a column or key name ends with (`load_plf` -> plf, `Width (mm)` -> mm, `K_FT` -> ft), else ''."""
  parts = [p for p in _UNIT_SPLIT.split(str(header).strip().lower()) if p]
  if len(parts) >= 2 and parts[-1] in _UNIT_TOKENS:
    return parts[-1]
  return ""


def as_number(value: Any) -> float | None:
  if isinstance(value, bool):
    return None
  if isinstance(value, (int, float)):
    return float(value)
  s = str(value).strip().replace(",", "").rstrip("%").strip()
  if not s:
    return None
  try:
    return float(s)
  except ValueError:
    return None


def table_rows(kind: str, data: bytes, limit: int = 5000) -> list[tuple[str, list[list[Any]]]]:
  """(sheet, rows) of a workbook or CSV: non-empty rows, header first, cached values."""
  out: list[tuple[str, list[list[Any]]]] = []
  if kind == "xlsx":
    import openpyxl  # pylint: disable=g-import-not-at-top
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    for ws in wb.worksheets:
      rows = []
      for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i > limit:
          break
        if any(c is not None and str(c).strip() for c in row):
          rows.append(list(row))
      out.append((ws.title, rows))
  elif kind == "csv":
    import csv  # pylint: disable=g-import-not-at-top
    rows = [r for r in list(csv.reader(io.StringIO(data.decode("utf-8", "replace"))))[:limit] if any(c.strip() for c in r)]
    out.append(("", rows))
  return out


def _is_key_value(header: list[str], rows: list[list[Any]]) -> bool:
  """A two-column sheet of parameter names and values."""
  cols = [h for h in header if h]
  return len(cols) == 2 and 2 <= len(rows) - 1 <= 60 and all(len([c for c in r if c is not None and str(c).strip()]) <= 2 for r in rows[1:])


def value_issues(report: Report, limit: int = 10) -> list[str]:
  """VALUES lines: empty columns, non-numeric cells in unit-bearing columns or keys, duplicate row keys."""
  found = []
  for label, sheet, rows in report.tables:
    if len(rows) < 2:
      continue
    name = label.rsplit("/", 1)[-1] + (f" sheet `{sheet}`" if sheet else "")
    header = ["" if c is None else str(c).strip() for c in rows[0]]
    width = max(len(r) for r in rows)
    body = rows[1:]
    cell = lambda r, j: r[j] if j < len(r) else None  # pylint: disable=g-long-lambda
    filled = lambda v: v is not None and str(v).strip() != ""  # pylint: disable=g-long-lambda
    empty_cols = [header[j] for j in range(width) if j < len(header) and header[j] and not any(filled(cell(r, j)) for r in body)]
    if empty_cols and len(body) >= 2:
      found.append(f"{VALUES}: {name}: column(s) {empty_cols[:6]} have a header and no values in {len(body)} rows")
    if _is_key_value(header, rows):
      bad = [f"{cell(r, 0)}={str(cell(r, 1))[:30]!r}" for r in body if unit_of(str(cell(r, 0) or "")) and filled(cell(r, 1)) and as_number(cell(r, 1)) is None]
      if bad:
        found.append(f"{VALUES}: {name}: keys that name a unit hold text instead of one number: {bad[:5]}")
    else:
      for j in range(min(width, len(header))):
        if not unit_of(header[j]):
          continue
        vals = [cell(r, j) for r in body if filled(cell(r, j))]
        bad = [str(v)[:30] for v in vals if as_number(v) is None]
        if bad and len(bad) * 4 >= len(vals):
          found.append(f"{VALUES}: {name}: column `{header[j]}` names a unit but holds text in {len(bad)} of {len(vals)} cells (e.g. {bad[:3]}); write one number per cell in that unit")
    keys = [str(cell(r, 0)).strip() for r in body if filled(cell(r, 0))]
    if len(keys) >= 3 and sum(_looks_like_id(k) for k in keys) * 2 >= len(keys):
      seen, dup = set(), []
      for k in keys:
        if k in seen and k not in dup:
          dup.append(k)
        seen.add(k)
      if dup and not _is_key_value(header, rows):
        found.append(f"{VALUES}: {name}: row key(s) {dup[:6]} appear more than once in column `{header[0] or 'A'}`; one row per key, or make the keys distinct")
    if len(found) >= limit:
      break
  if found:
    found.append(
        f"{VALUES} means: a cell the grader will read as a number, a count or an identifier does not hold one."
        " Fill every column the template or brief names, write numbers as numbers in the unit the header names"
        " (no `12 x 8`, no `see note`), and keep row keys unique."
    )
  return found[:limit + 1]


def readback_lines(report: Report, limit: int = 30) -> str:
  """The model's own numbers: key/value sheets in full, min..max of each unit-bearing column."""
  lines: list[str] = []
  for label, sheet, rows in report.tables:
    if len(rows) < 2 or len(lines) >= limit:
      continue
    name = label.rsplit("/", 1)[-1] + (f" sheet `{sheet}`" if sheet else "")
    header = ["" if c is None else str(c).strip() for c in rows[0]]
    body = rows[1:]
    cell = lambda r, j: r[j] if j < len(r) else None  # pylint: disable=g-long-lambda
    if _is_key_value(header, rows):
      pairs = [f"{cell(r, 0)}={str(cell(r, 1)).strip()[:24]}" for r in body if cell(r, 0) is not None]
      text = ", ".join(pairs)
      lines.append(f"- {name}: " + (text[:700] + " ..." if len(text) > 700 else text))
      continue
    stats = []
    for j in range(min(len(header), max(len(r) for r in rows))):
      u = unit_of(header[j])
      if not u:
        continue
      nums = [as_number(cell(r, j)) for r in body]
      nums = [n for n in nums if n is not None]
      if nums:
        lo, hi = min(nums), max(nums)
        stats.append(f"{header[j]} {lo:g}..{hi:g} ({len(nums)})" if lo != hi else f"{header[j]} {lo:g} ({len(nums)})")
    if stats:
      lines.append(f"- {name}: " + "; ".join(stats)[:700])
  return "\n".join(lines[:limit])


def _check_layers(label: str, info: dict[str, Any], want: list[str], issues: list[str], ok: list[str]) -> None:
  """DXF_LAYER_CHECK: a required layer passes when entities sit on it; the message says what is wrong and how to fix it."""
  by = info.get("by_layer") or {}
  table = info.get("layers") or []
  total = sum(info["counts"].values())
  if total == 0:
    issues.append(f"{label}: DXF modelspace is empty")
  nothing = [l for l in want if l not in by]
  if nothing:
    defined = [l for l in nothing if l in table]
    absent = [l for l in nothing if l not in table]
    msg = f"{label}: no entities on required layer(s) {nothing}"
    if defined:
      msg += f" ({defined} exist in the layer table but hold nothing)"
    if absent:
      msg += f" ({absent} do not exist at all)"
    msg += (f"; entities per layer now: {dict(list(by.items())[:12])}. Put the entities on the layer by name"
            " (ezdxf: `doc.layers.add('NAME')`, then `msp.add_lwpolyline(points, dxfattribs={'layer': 'NAME'})`)")
    issues.append(msg)
  undeclared = [l for l in by if l not in table and l != "0"]
  if undeclared:
    issues.append(f"{label}: entities use layer(s) {undeclared[:8]} that are not in the layer table; add each with `doc.layers.add(name)` so CAD readers list them")
  ok.append(f"{label}: DXF, {total} entities; per layer {dict(list(by.items())[:12])}")


def relpath(path: str, base: str) -> str:
  return posixpath.relpath(path, base) if path.startswith(base) else path
