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

"""Input triage: inventory the task's files and summarise them for the model.

Runs before the first model turn. One `exec` lists the files, one probes the
task container's tooling, and the interesting files are read over the protocol
and parsed in the serve container (`parsers.py`), so the model gets sheet
names, headers, PDF outlines, IFC entity counts and DXF layers in a few
thousand tokens instead of discovering them over twenty tool calls.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from typing import Any

import parsers
import taskbridge

# Size caps, all in characters (about 4 characters per token). The defaults
# keep brief + inventory near 6k tokens so the opening turn (system prompt,
# instruction, brief, inventory, checklist, tool schemas) stays under about
# 10k tokens: small per-minute token quotas and cheap cached prefixes both
# want it that way. Everything cut is cut with a note on how to read the rest.
MAX_FILES_LISTED = int(os.environ.get("TRIAGE_LIST_FILES", "350"))
MAX_LIST_CHARS = int(os.environ.get("TRIAGE_LIST_CHARS", "5000"))
MAX_FILES_SUMMARISED = int(os.environ.get("TRIAGE_SUMMARY_FILES", "30"))
MAX_SUMMARY_CHARS = int(os.environ.get("TRIAGE_SUMMARY_CHARS", "11000"))
MAX_BRIEF_CHARS = int(os.environ.get("TRIAGE_BRIEF_CHARS", "9000"))
MAX_INVENTORY_CHARS = int(os.environ.get("TRIAGE_INVENTORY_CHARS", "18000"))
MAX_TOOLING_CHARS = 900
MAX_FILE_BYTES = 9_500_000  # a read_file returns at most 10 MB
PER_FILE_CHARS = int(os.environ.get("TRIAGE_FILE_CHARS", "650"))
BRIEF_NAMES = ("task.md", "readme.md", "brief.md", "instructions.md", "task.txt", "readme.txt", "agent_view.json")
PRIORITY = {
    "md": 0, "txt": 0, "json": 1, "csv": 1, "xlsx": 2, "xlsm": 2, "docx": 2,
    "dxf": 3, "ifc": 3, "pdf": 4, "eml": 2, "yaml": 1, "yml": 1, "py": 3,
    "xml": 5, "html": 5, "zip": 5,
}
LIBRARY_DIR_RE = re.compile(r"(^|/)(library|standards?|codes?|references?|archive|superseded)(/|$)", re.I)
PY_MODULES = (
    "openpyxl", "docx", "ezdxf", "ifcopenshell", "fitz", "pymupdf", "pdfplumber",
    "pypdf", "numpy", "scipy", "pandas", "shapely", "pulp", "ortools",
    "matplotlib", "reportlab", "PIL", "lxml", "xlsxwriter", "networkx",
    "sympy", "cv2", "pytesseract",
)
CLIS = ("python3", "pdftotext", "pdfinfo", "pdftoppm", "libreoffice", "soffice", "pandoc", "zip", "unzip", "tesseract", "node", "jq", "sqlite3")


def roots_for(cl: Any) -> list[str]:
  """Directories worth listing: the base dir, the workdir, input parents."""
  roots = []
  for p in [cl.base, cl.workdir] + [posixpath.dirname(i.rstrip("/")) for i in cl.inputs]:
    p = p.rstrip("/") or "/"
    if p in ("/", "") or any(p == r or p.startswith(r + "/") for r in roots):
      continue
    roots = [r for r in roots if not r.startswith(p + "/")] + [p]
  return roots or [cl.workdir]


async def list_files(session: taskbridge.Session, roots: list[str]) -> list[tuple[int, str]]:
  """Returns (size, path) for regular files under `roots`, largest trees first."""
  cmd = (
      "for r in " + " ".join(shlex.quote(r) for r in roots) + "; do "
      "find \"$r\" -maxdepth 8 -type f ! -path '*/.git/*' ! -name '*.pyc' "
      "! -path '*/node_modules/*' ! -path '*/__pycache__/*' -printf '%s\\t%p\\n' 2>/dev/null; "
      f"done | sort -u | head -n {MAX_FILES_LISTED + 1}"
  )
  try:
    result = await session.exec(cmd, timeout_sec=90)
  except taskbridge.TaskError as exc:
    await session.log(f"triage: find failed: {exc}")
    return []
  files = []
  for line in (result.get("output") or "").splitlines():
    size, _, path = line.partition("\t")
    if path and size.isdigit():
      files.append((int(size), path))
  return files


async def probe_tooling(session: taskbridge.Session) -> str:
  """One exec that reports which Python modules and CLIs the container has."""
  py = (
      "python3 - <<'PY'\nimport importlib, sys\nprint('python', sys.version.split()[0])\n"
      "ok, no = [], []\nfor m in " + repr(list(PY_MODULES)) + ":\n"
      "    try:\n        importlib.import_module(m); ok.append(m)\n"
      "    except Exception:\n        no.append(m)\n"
      "print('modules available:', ' '.join(ok))\nprint('modules missing:', ' '.join(no))\nPY\n"
  )
  cli = "for c in " + " ".join(CLIS) + "; do command -v $c >/dev/null 2>&1 && printf '%s ' $c; done; echo"
  cmd = py + "echo -n 'cli available: '; " + cli + "; echo -n 'user: '; id -un; echo -n 'cwd: '; pwd; echo -n 'pip: '; (python3 -m pip --version 2>/dev/null | head -c 60 || echo none)"
  try:
    result = await session.exec(cmd, timeout_sec=120)
  except taskbridge.TaskError as exc:
    return f"(tooling probe failed: {exc})"
  out = (result.get("output") or "").strip()
  return out[:1500] or "(tooling probe gave no output)"


def choose(files: list[tuple[int, str]], cl: Any) -> tuple[list[str], list[str]]:
  """Picks the brief file(s) and the files to summarise, by priority."""
  briefs, candidates = [], []
  for size, path in files:
    name = path.rsplit("/", 1)[-1].lower()
    ext = parsers.ext_of(path)
    if name in BRIEF_NAMES and size <= 2_000_000:
      briefs.append(path)
      continue
    if ext not in PRIORITY or size == 0 or size > MAX_FILE_BYTES:
      continue
    if name in (".keep", ".gitkeep", "sha256sums"):
      continue
    pri = PRIORITY[ext]
    if LIBRARY_DIR_RE.search(path):
      pri += 4  # standards and archives: title and outline only, later
    if any(path == i or path.startswith(i) for i in cl.inputs):
      pri -= 2  # the brief names it: read it
    if any(path == d.path for d in cl.deliverables):
      pri += 10  # a pre-existing deliverable (template to build upon)
    candidates.append((pri, size, path))
  candidates.sort(key=lambda c: (c[0], c[1]))
  # the task brief first, then shallow READMEs, then deeper ones
  briefs.sort(key=lambda p: (not p.rsplit("/", 1)[-1].lower().startswith(("task", "brief", "instruction")), p.count("/")))
  return briefs, [p for _, _, p in candidates[:MAX_FILES_SUMMARISED]]


def tree(files: list[tuple[int, str]], roots: list[str], max_chars: int = MAX_LIST_CHARS) -> str:
  """The file listing, cut at `max_chars` with a note on how to see the rest."""
  lines, used = [], 0
  for size, path in files[:MAX_FILES_LISTED]:
    short = path
    for r in roots:
      if path.startswith(r + "/"):
        short = path[len(r) + 1:]
        break
    line = f"{_human(size):>8}  {short}"
    if used + len(line) + 1 > max_chars:
      break
    lines.append(line)
    used += len(line) + 1
  more = len(files) - len(lines)
  if more > 0:
    lines.append(f"... and {more} more files (run `find <root> -type f` in bash for the full list)")
  return "\n".join(lines)


def compose(
    files: list[tuple[int, str]],
    roots: list[str],
    summaries: list[str],
    tooling: str,
    max_chars: int = MAX_INVENTORY_CHARS,
) -> str:
  """The inventory section the model sees, fitted into `max_chars`.

  Summaries are cut first (whole entries from the end, the least important),
  then the listing; the tooling probe is kept.
  """
  tooling = tooling[:MAX_TOOLING_CHARS]
  head = f"## Workspace inventory ({len(files)} files; roots: {', '.join(roots)})\n"
  note = "## File summaries (parsed by the harness; read a file yourself before relying on details)\n"
  tail = f"\n\n## Tooling in the task container\n```\n{tooling}\n```"
  fixed = len(head) + len(note) + len(tail) + 16
  listing = tree(files, roots)
  body = list(summaries)
  cut = 0
  while body and fixed + len(listing) + sum(len(s) + 1 for s in body) > max_chars:
    body.pop()
    cut += 1
  if cut:
    body.append(f"- ({cut} more files not summarised here: read them with read_file when needed)")
  while fixed + len(listing) + sum(len(s) + 1 for s in body) > max_chars and len(listing) > 200:
    listing = tree(files, roots, max_chars=max(200, len(listing) // 2))
  return (
      head + f"```\n{listing}\n```\n\n" + note
      + ("\n".join(body) if body else "(no parseable files found)")
      + tail
  )


def _human(n: int) -> str:
  for unit in ("B", "K", "M", "G"):
    if n < 1024 or unit == "G":
      return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
    n /= 1024
  return str(n)


async def inventory(session: taskbridge.Session, cl: Any) -> dict[str, Any]:
  """Runs the whole triage. Returns brief text, inventory markdown, tooling."""
  roots = roots_for(cl)
  files = await list_files(session, roots)
  tooling = await probe_tooling(session)
  briefs, picks = choose(files, cl)

  brief_text = ""
  for path in briefs[:3]:
    try:
      data = await session.read_file(path)
    except taskbridge.TaskError:
      continue
    text = data.decode("utf-8", "replace")
    if len(text) > MAX_BRIEF_CHARS:
      text = text[:MAX_BRIEF_CHARS] + f"\n[... {len(text) - MAX_BRIEF_CHARS} more chars: read the file for the rest]"
    brief_text += f"\n\n### {path}\n{text}"

  summaries, used, skipped = [], 0, 0
  for path in picks:
    if used >= MAX_SUMMARY_CHARS:
      skipped += 1
      continue
    try:
      data = await session.read_file(path)
    except taskbridge.TaskError as exc:
      summaries.append(f"- `{path}`: (unreadable: {exc})")
      continue
    detail = PER_FILE_CHARS // 2 if LIBRARY_DIR_RE.search(path) else PER_FILE_CHARS
    info = parsers.parse(path, data, detail=detail)
    text = info.get("summary", "")
    summaries.append(f"- `{path}` [{info.get('kind')}]: {text}")
    used += len(text)
  if skipped:
    summaries.append(f"- ({skipped} more candidate files not summarised: summary budget used; read_file them when needed)")

  inv = compose(files, roots, summaries, tooling)
  return {"brief": brief_text.strip(), "inventory": inv, "files": files, "tooling": tooling, "roots": roots}
