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

"""Deliverable checklist: what the task wants on disk, parsed from its text.

`extract(instruction, workdir, extra_text)` finds deliverable paths (files and
folders), zip members, spreadsheet sheet/column names and DXF layer names with
regular expressions over generic brief phrasing ("save it as X", "write
`output/...`", "### 1. `file.xlsx`", "sheet `Name`: `col`, `col`").
`merge_model(checklist, json_text)` folds in the model's own reading of the
brief (one cheap call made by `harness.py`). The checklist drives the
verification pass in `verify.py` and the fallback final message.

Only the task text is read here: never a grader, never our own artifacts, and
nothing here is specific to any task.
"""

from __future__ import annotations

import dataclasses
import json
import posixpath
import re

EXTS = (
    "xlsx|xlsm|csv|tsv|docx|pdf|md|txt|json|yaml|yml|ifc|dxf|dwg|py|zip|pptx|"
    "html|xml|eml|step|stp|svg|png|ics|sql|db"
)
# a file name with one of the extensions, optionally with folders in front,
# possibly absolute; not preceded by a word character, a dot or a hyphen (so
# "2026-10-31.docx" is not cut out of "Memo_2026-10-31.docx")
FILE_RE = re.compile(
    rf"(?<![\w.\-])(/?(?:[\w.\-]+/)*[\w.\-]+\.(?:{EXTS}))(?![\w/])", re.I
)
DIR_TOKEN_RE = re.compile(
    r"\b(?:under|in|into|to|at)\s+`((?:/?[\w.\-]+/)+)`", re.I
)
WORKING_IN_RE = re.compile(r"working (?:directory is|in) `?(/[\w./\-]+?)`?[\s.,;:]")
OUTPUT_VERBS = re.compile(
    r"\b(save|saved|saving|write|written|writing|deliver|deliverables?|output|"
    r"produce|producing|submit|submission|export|create|generate|place|put|"
    r"issue|store|record|named?|file it|hand (?:in|over)|return)\b",
    re.I,
)
INPUT_WORDS = re.compile(
    r"\b(attached|provided|given|read|inputs?|library|templates?|reference|"
    r"example|standards?|records?|are in|is in|located|contains?|cites?|"
    r"from the|listed in|in the (?:folder|directory))\b",
    re.I,
)
OUTPUT_DIR_RE = re.compile(r"(^|/)(output|outputs|submission|deliverables?|out|results?)(/|$)", re.I)
TEMPLATE_RE = re.compile(r"template|example|sample|\boracle", re.I)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*$")
TOP_HEADING_RE = re.compile(r"^\s{0,3}#{1,2}\s+(?!\d)")
ITEM_FILE_RE = re.compile(
    rf"^\s*(?:\*\*)?(?:[-*]|\d+[.)])\s*(?:\*\*)?\s*`?([\w.\-]+\.(?:{EXTS}))`?", re.I
)
BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")
SHEET_BOLD_RE = re.compile(r"^\s*\*\*([^*`]{1,60})\*\*\s*(?:\([^)]*\))?\s*[:—–-]")
SHEET_TICK_RE = re.compile(r"^\s*`([^`]{1,60})`\s*(?:\([^)]*\))?\s*[:—–-]")
SHEET_NAMED_RE = re.compile(
    r"\b(?:work)?sheets?\s+(?:named\s+|called\s+)?`([^`]+)`", re.I
)
COLUMNS_HINT_RE = re.compile(r"\b(columns?|headers?|fields?|header row)\b", re.I)
TICKS_RE = re.compile(r"`([^`]+)`")
FENCE_RE = re.compile(r"```[a-z]*\n(.*?)```", re.S)
LAYER_RE = re.compile(r"\blayers?\s+`([^`]+)`(?:\s*(?:,|and|or)\s*`([^`]+)`)*", re.I)
IDENT_RE = re.compile(r"^[A-Za-z_][\w .\-/%()]*$")


@dataclasses.dataclass
class Deliverable:
  """One file (or folder) the task wants, with what we know about its shape."""

  path: str
  kind: str = ""
  members: list[str] = dataclasses.field(default_factory=list)
  # sheet -> columns; for zips `member.xlsx::Sheet`; the sheet "*" means "some
  # sheet must carry these columns"
  sheets: dict[str, list[str]] = dataclasses.field(default_factory=dict)
  layers: list[str] = dataclasses.field(default_factory=list)
  notes: list[str] = dataclasses.field(default_factory=list)
  source: str = "regex"
  token: str = ""
  # template files in the workspace the deliverable is modelled on: "" -> the
  # file itself, "member.ext" -> a zip member (filled by match_templates)
  templates: dict[str, str] = dataclasses.field(default_factory=dict)

  def merge(self, other: "Deliverable") -> None:
    for m in other.members:
      if m not in self.members:
        self.members.append(m)
    for s, cols in other.sheets.items():
      have = self.sheets.setdefault(s, [])
      for c in cols:
        if c not in have:
          have.append(c)
    for l in other.layers:
      if l not in self.layers:
        self.layers.append(l)
    for n in other.notes:
      if n not in self.notes:
        self.notes.append(n)
    if other.source == "model" and self.source == "regex":
      self.source = "regex+model"


@dataclasses.dataclass
class Checklist:
  """Everything `verify.py` checks before the harness lets the model finish."""

  workdir: str
  base: str
  deliverables: list[Deliverable] = dataclasses.field(default_factory=list)
  inputs: list[str] = dataclasses.field(default_factory=list)
  requirements: list[str] = dataclasses.field(default_factory=list)
  early_draft: bool = False

  def find(self, path: str) -> Deliverable | None:
    for d in self.deliverables:
      if d.path == path:
        return d
    return None

  def add(self, d: Deliverable) -> Deliverable:
    have = self.find(d.path)
    if have:
      have.merge(d)
      return have
    self.deliverables.append(d)
    return d

  def to_prompt(self) -> str:
    """Markdown the model sees: paths, members, sheets/columns, layers."""
    if not self.deliverables:
      return (
          "No deliverable path could be parsed from the brief. Decide the exact"
          " path(s) and format(s) from the brief yourself, state them in your"
          " first message, and write them early."
      )
    lines = []
    for d in self.deliverables:
      lines.append(f"- `{d.path}`" + (f" ({d.kind})" if d.kind else ""))
      if d.members:
        lines.append("  - zip must contain at its root: " + ", ".join(f"`{m}`" for m in d.members))
      for s, cols in d.sheets.items():
        label = "a sheet" if s.endswith("*") else f"sheet `{s}`"
        lines.append(f"  - {label}" + (": columns " + ", ".join(f"`{c}`" for c in cols) if cols else ""))
      if d.layers:
        lines.append("  - layers: " + ", ".join(f"`{l}`" for l in d.layers))
      for n in d.notes[:4]:
        lines.append(f"  - {n}")
    if self.requirements:
      lines.append("")
      lines.append("Key requirements to design to (from the brief):")
      lines += [f"- {r}" for r in self.requirements[:25]]
    return "\n".join(lines)

  def to_json(self) -> str:
    return json.dumps(
        {
            "base": self.base,
            "deliverables": [
                {k: v for k, v in dataclasses.asdict(d).items() if k not in ("token", "templates")}
                for d in self.deliverables
            ],
            "inputs": self.inputs,
        },
        indent=1,
    )


def resolve(path: str, base: str) -> str:
  path = path.strip().strip("`'\"")
  trailing = path.endswith("/")
  if not path.startswith("/"):
    path = posixpath.join(base, path)
  out = posixpath.normpath(path)
  return out + ("/" if trailing and not out.endswith("/") else "")


def _kind(path: str) -> str:
  if path.endswith("/"):
    return "dir"
  name = path.rsplit("/", 1)[-1]
  return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _sentences(text: str) -> list[str]:
  return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z`/#*-])|\n+", text) if s.strip()]


def _score(sentence: str, token: str, pos: int) -> int:
  before = sentence[:pos]
  score = 0
  if OUTPUT_VERBS.search(sentence):
    score += 2
  if OUTPUT_DIR_RE.search(token) or token.lower().startswith("submission"):
    score += 2
  if INPUT_WORDS.search(before) and not OUTPUT_VERBS.search(before):
    score -= 3
  elif INPUT_WORDS.search(sentence) and not OUTPUT_VERBS.search(sentence):
    score -= 2
  if re.search(r"\b(as|to|into|at)\s+`?\**" + re.escape(token), sentence):
    score += 1
  if re.search(r"\b(read|open|see|refer|from|run)\s+`?" + re.escape(token), sentence, re.I):
    score -= 2
  if re.search(r"^\s*[-*]\s*`" + re.escape(token) + r"`\s*:", sentence):
    score -= 2  # "- `file`: what it is" lists an input
  return score


def extract(instruction: str, workdir: str, extra_text: str = "") -> Checklist:
  """Builds the checklist from the instruction (and e.g. task.md's text)."""
  text = instruction + ("\n\n" + extra_text if extra_text else "")
  m = WORKING_IN_RE.search(text + " ")
  base = m.group(1).rstrip("/") if m else workdir
  cl = Checklist(workdir=workdir, base=base)
  cl.early_draft = bool(
      re.search(r"first (?:complete|COMPLETE) version|write .{0,40} early", text)
  )

  # 1. file and folder tokens, sentence by sentence
  for sentence in _sentences(text):
    for mt in FILE_RE.finditer(sentence):
      token = mt.group(1)
      path = resolve(token, base)
      if TEMPLATE_RE.search(token):
        if path not in cl.inputs:
          cl.inputs.append(path)
        continue
      score = _score(sentence, token, mt.start())
      if score >= 2:
        cl.add(Deliverable(path=path, kind=_kind(path), token=token))
      elif score <= -1 and path not in cl.inputs:
        cl.inputs.append(path)
    if OUTPUT_VERBS.search(sentence):
      for dm in DIR_TOKEN_RE.finditer(sentence):
        token = dm.group(1)
        if TEMPLATE_RE.search(token) or INPUT_WORDS.search(sentence[:dm.start()]) and not OUTPUT_VERBS.search(sentence[:dm.start()]):
          continue
        if OUTPUT_DIR_RE.search(token) or re.search(r"\b(save|write|deliver|put|place)\b", sentence, re.I):
          cl.add(Deliverable(path=resolve(token, base), kind="dir", token=token))

  # 2. component files listed as "### 1. `file.ext`" or "**1. `file.ext`**"
  # anywhere in the brief: members of the zip if there is one, else
  # deliverables in the base folder
  members = _members(text)
  zips = [d for d in cl.deliverables if d.kind == "zip"]
  if zips:
    for name in members:
      if name not in zips[0].members:
        zips[0].members.append(name)
    for name, sub in members.items():
      for sheet, cols in sub.sheets.items():
        zips[0].sheets[f"{name}::{sheet}"] = cols
      for layer in sub.layers:
        zips[0].layers.append(f"{name}::{layer}")
    # a member mentioned on its own is not a separate deliverable
    cl.deliverables = [
        d for d in cl.deliverables
        if not (d.kind != "zip" and d.token and "/" not in d.token and d.token in zips[0].members)
    ]
  else:
    for name, sub in members.items():
      d = cl.add(Deliverable(path=resolve(name, base), kind=_kind(name), token=name))
      d.merge(sub)

  # 3. sheets and layers named for a single workbook / drawing outside a zip
  xlsx = [d for d in cl.deliverables if d.kind in ("xlsx", "xlsm")]
  if len(xlsx) == 1 and not xlsx[0].sheets:
    _sheets_from_text(text, xlsx[0])
  dxfs = [d for d in cl.deliverables if d.kind == "dxf"]
  if len(dxfs) == 1 and not dxfs[0].layers:
    for lm in LAYER_RE.finditer(text):
      for name in TICKS_RE.findall(lm.group(0)):
        if name not in dxfs[0].layers:
          dxfs[0].layers.append(name)

  # a folder deliverable that holds a file deliverable is redundant
  files = [d.path for d in cl.deliverables if d.kind != "dir"]
  cl.deliverables = [
      d for d in cl.deliverables
      if d.kind != "dir" or not any(f.startswith(d.path) for f in files)
  ]
  cl.inputs = [p for p in cl.inputs if not cl.find(p)]
  return cl


def _blocks(lines: list[str], start: int) -> list[str]:
  """Bullets (with their indented continuation lines) after `start`, until the
  next heading or blank-line-separated paragraph that is not a bullet."""
  out: list[str] = []
  for line in lines[start:]:
    if HEADING_RE.match(line):
      break
    if BULLET_RE.match(line):
      out.append(line.strip())
    elif out and line.startswith((" ", "\t")) and line.strip():
      out[-1] += " " + line.strip()
    elif line.strip() and not out:
      out.append(line.strip())
    elif line.strip() and out and not BULLET_RE.match(line):
      out.append(line.strip())
  return out


def _members(text: str) -> dict[str, Deliverable]:
  """Component files named as numbered headings or bold numbered items."""
  lines = text.splitlines()
  found: dict[str, Deliverable] = {}
  for i, line in enumerate(lines):
    hm = HEADING_RE.match(line)
    name = None
    if hm and re.match(r"^\s*\d+[.)]", hm.group(1)):
      fm = FILE_RE.search(hm.group(1))
      name = fm.group(1) if fm else None
    elif re.match(r"^\s*\*\*\s*\d+[.)]", line):
      im = ITEM_FILE_RE.match(line)
      name = im.group(1) if im else None
    if not name or "/" in name or TEMPLATE_RE.search(name):
      continue
    d = found.setdefault(name, Deliverable(path=name, kind=_kind(name), token=name))
    body = []
    for nxt in lines[i + 1:i + 80]:
      if TOP_HEADING_RE.match(nxt) or re.match(r"^\s{0,3}#{1,6}\s+\d+[.)]", nxt) or re.match(r"^\s*\*\*\s*\d+[.)]", nxt):
        break  # the next section or the next component file
      body.append(nxt)
    section = "\n".join(body)
    if d.kind in ("xlsx", "xlsm"):
      _sheets_from_section(section, d)
    if d.kind == "dxf":
      for lm in LAYER_RE.finditer(section):
        for lname in TICKS_RE.findall(lm.group(0)):
          if lname not in d.layers:
            d.layers.append(lname)
  return found


def _column_groups(rest: str) -> list[str]:
  """Backticked groups that form one list: consecutive, separated only by
  commas/"and"/"or"/"/" (so "`tiedown` `yes` or `no`" stops after the list)."""
  groups: list[str] = []
  last_end = None
  for m in TICKS_RE.finditer(rest):
    if last_end is not None:
      sep = rest[last_end:m.start()].strip().lower()
      if sep not in ("", ",", ", and", "and", "or", ", or", ";", "/"):
        break
      if sep in ("or", ", or") and groups and "," not in groups[-1]:
        break  # "`a` or `b`" is a value choice, not a column list
    groups.append(m.group(1))
    last_end = m.end()
  return groups


def _split_cols(groups: list[str]) -> list[str]:
  cols: list[str] = []
  for g in groups:
    parts = [p.strip() for p in g.split(",")] if "," in g else [g.strip()]
    for p in parts:
      if p and IDENT_RE.match(p) and " " not in p.strip() and p not in cols:
        cols.append(p)
  return cols


def _sheets_from_section(section: str, d: Deliverable) -> None:
  """Reads "**Sheet**: `a`, `b`" and "`Sheet`: ... `a, b, c`" bullets."""
  blocks = _blocks(section.splitlines(), 0)
  for block in blocks:
    block = BULLET_RE.sub("", block, count=1)
    sm = SHEET_BOLD_RE.match(block) or SHEET_TICK_RE.match(block)
    if sm:
      sheet = sm.group(1).strip()
      rest = block[sm.end():]
      groups = _column_groups(rest)
      if len(groups) == 1 and "," not in groups[0]:
        # "`Sheet`: one row per X — `a, b, c`": the list comes later
        groups = [g for g in TICKS_RE.findall(rest) if "," in g][:1] or groups
      cols = _split_cols(groups)
      if (" " in sheet and len(sheet.split()) > 3) or not cols:
        # a prose bullet, not a sheet spec
        if not cols:
          continue
      d.sheets.setdefault(sheet, [])
      for c in cols:
        if c not in d.sheets[sheet] and c != sheet:
          d.sheets[sheet].append(c)
      continue
    for sn in SHEET_NAMED_RE.finditer(block):
      d.sheets.setdefault(sn.group(1), [])
      tail = block[sn.end():sn.end() + 300]
      if COLUMNS_HINT_RE.search(tail):
        for c in _split_cols(TICKS_RE.findall(tail)):
          if c not in d.sheets[sn.group(1)]:
            d.sheets[sn.group(1)].append(c)
  # "header row carries exactly these columns:" followed by a code fence
  if not d.sheets:
    for hm in re.finditer(r"(columns?|headers?)[^\n]{0,80}:\s*\n+```[a-z]*\n(.*?)```", section, re.S | re.I):
      cols = _split_cols([hm.group(2).replace("\n", " ")])
      if len(cols) >= 2:
        d.sheets["*"] = cols
        break


def _sheets_from_text(text: str, d: Deliverable) -> None:
  for sn in SHEET_NAMED_RE.finditer(text):
    d.sheets.setdefault(sn.group(1), [])
    tail = text[sn.end():sn.end() + 300].split("\n\n")[0]
    if COLUMNS_HINT_RE.search(tail):
      for c in _split_cols(TICKS_RE.findall(tail)):
        if c not in d.sheets[sn.group(1)]:
          d.sheets[sn.group(1)].append(c)


TEMPLATE_WORDS_RE = re.compile(r"(template|example|sample|blank|form|empty|skeleton)s?", re.I)
TEMPLATE_DIR_RE = re.compile(r"(^|/)(templates?|forms?|examples?|samples?)(/|$)", re.I)
TEMPLATE_EXTS = ("xlsx", "xlsm", "docx", "dxf", "csv", "md", "txt", "json")


def _stem(name: str) -> str:
  """A comparable stem: lower-case, no extension, no template words, alnum only."""
  base = name.rsplit("/", 1)[-1]
  if "." in base:
    base = base.rsplit(".", 1)[0]
  base = TEMPLATE_WORDS_RE.sub("", base)
  return re.sub(r"[^a-z0-9]+", "", base.lower())


def match_templates(cl: Checklist, files: list[str]) -> int:
  """Pairs each deliverable (or zip member) with a template of the same format
  found in the workspace: a file whose name or folder says template/example/
  sample/blank/form. Same stem wins; else the only template of that format;
  else the closest stem (difflib, at least 0.6). Returns how many were paired.
  """
  import difflib  # pylint: disable=g-import-not-at-top

  taken = {d.path for d in cl.deliverables}
  candidates = [
      p for p in files
      if p not in taken and _kind(p) in TEMPLATE_EXTS
      and (TEMPLATE_WORDS_RE.search(p.rsplit("/", 1)[-1]) or TEMPLATE_DIR_RE.search(p))
  ]
  if not candidates:
    return 0
  paired = 0
  for d in cl.deliverables:
    targets = [(d.path, "")] if d.kind != "zip" else [(m, m) for m in d.members]
    for name, key in targets:
      ext = _kind(name)
      same = [p for p in candidates if _kind(p) == ext]
      if not same or ext not in TEMPLATE_EXTS:
        continue
      stem = _stem(name)
      score, best = max(
          (
              (1.0 if _stem(p) == stem else difflib.SequenceMatcher(None, stem, _stem(p)).ratio(), p)
              for p in same
          ),
          key=lambda t: t[0],
      )
      if score >= 0.6 or len(same) == 1:
        d.templates[key] = best
        paired += 1
  return paired


MODEL_PROMPT = """You are the checklist extraction step of an engineering-task harness. Read the brief below and \
return ONLY a JSON object (no prose, no code fence) of this shape:
{"deliverables": [{"path": "<exact path as the brief gives it>", "format": "<xlsx|docx|ifc|dxf|csv|json|md|zip|pdf|py|dir|...>", \
"members": ["<file names required inside a zip, at its root>"], "sheets": {"<sheet name>": ["<column header>", ...]}, \
"layers": ["<DXF layer names>"], "notes": ["<short must-have facts: sections, rows, naming, units, date, signatory>"]}],
 "inputs": ["<paths of files the brief gives you to read>"],
 "key_requirements": ["<each hard requirement, limit or rule the result is checked against, with its number and unit>", ...],
 "graded_by": "<programmatic checks|rubric judge|both|unknown>"}
Rules: copy paths and names exactly as written (case, underscores, extension). A path relative to the working directory \
stays relative. A folder the brief says to save files under is a deliverable with format "dir" and a trailing slash. \
Templates, examples, libraries and records you must READ are inputs, not deliverables. Files inside a zip are members \
of the zip, not separate deliverables. Keep key_requirements to the 25 most consequential items."""


def merge_model(cl: Checklist, json_text: str) -> int:
  """Folds the model's JSON into `cl`. Returns how many deliverables it added."""
  text = json_text.strip()
  text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
  start, end = text.find("{"), text.rfind("}")
  if start < 0 or end < 0:
    return 0
  try:
    data = json.loads(text[start:end + 1])
  except ValueError:
    return 0
  added = 0
  model_paths = set()
  for item in data.get("deliverables") or []:
    if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].strip():
      continue
    raw = item["path"].strip()
    fmt = str(item.get("format") or "").lower()
    if fmt == "dir" and not raw.endswith("/"):
      raw += "/"
    path = resolve(raw, cl.base)
    model_paths.add(path)
    d = Deliverable(
        path=path,
        kind=_kind(path) or fmt,
        members=[m for m in item.get("members") or [] if isinstance(m, str)],
        sheets={
            str(k): [str(c) for c in v if isinstance(c, (str, int, float))]
            for k, v in (item.get("sheets") or {}).items()
            if isinstance(v, list)
        } if isinstance(item.get("sheets"), dict) else {},
        layers=[l for l in item.get("layers") or [] if isinstance(l, str)],
        notes=[n for n in item.get("notes") or [] if isinstance(n, str)][:6],
        source="model",
    )
    if cl.find(path) is None:
      added += 1
    cl.add(d)
  inputs = {resolve(p, cl.base) for p in data.get("inputs") or [] if isinstance(p, str) and p.strip()}
  cl.deliverables = [
      d for d in cl.deliverables
      if not (d.source == "regex" and d.path in inputs and d.path not in model_paths)
  ]
  for p in inputs:
    if p not in cl.inputs and cl.find(p) is None:
      cl.inputs.append(p)
  for r in data.get("key_requirements") or []:
    if isinstance(r, str) and r not in cl.requirements:
      cl.requirements.append(r.strip()[:300])
  graded = str(data.get("graded_by") or "")
  if graded and graded != "unknown":
    cl.requirements.insert(0, f"Graded by: {graded}")
  return added
