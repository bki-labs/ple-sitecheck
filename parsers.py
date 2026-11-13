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

"""File parsers that run in the serve container (never in the task container).

Each `parse_*` takes raw bytes and returns a dict with at least `ok` (bool),
`kind`, and `summary` (one short paragraph for the model), plus structured
fields the verifier uses (sheet names, headers, layers, entity counts, ...).
Parsers never raise: a failure comes back as `ok: False` with `error`.
"""

from __future__ import annotations

import ast
import collections
import csv
import io
import json
import re
import zipfile
from typing import Any

# read_file returns extracted text for these instead of raw bytes
EXTRACT_EXTS = {"pdf", "docx", "xlsx", "xlsm", "pptx", "ifc", "dxf", "zip"}
TEXT_EXTS = {
    "md", "txt", "csv", "tsv", "json", "yaml", "yml", "py", "xml", "html",
    "htm", "eml", "ini", "cfg", "toml", "log", "sh", "tex", "rst",
}
OFFICE_EXTS = {"xlsx", "xlsm", "docx", "pdf", "pptx"}
CAD_EXTS = {"ifc", "dxf"}

# Text a template leaves behind when a field was not filled in.
PLACEHOLDER_RE = re.compile(
    r"\[(?:insert|enter|type|your|name|date|project|client|engineer|title|"
    r"number|description|add|fill|xx+|tbd|todo)[^\]]{0,60}\]"
    r"|\{\{[^}]{0,80}\}\}|<<[^>]{0,80}>>|\b(?:TBD|TODO|XXX+|Lorem ipsum)\b"
    r"|_{6,}",
    re.I,
)


def ext_of(path: str) -> str:
  """Lower-case extension of `path` without the dot ("" when none)."""
  name = path.rsplit("/", 1)[-1]
  return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _clip(text: str, n: int) -> str:
  text = text.strip()
  return text if len(text) <= n else text[:n] + f" [... {len(text) - n} more chars]"


def parse(path: str, data: bytes, detail: int = 1200) -> dict[str, Any]:
  """Dispatches on the file extension. `detail` caps the summary length."""
  ext = ext_of(path)
  try:
    if ext in ("xlsx", "xlsm"):
      return parse_xlsx(data, detail)
    if ext == "docx":
      return parse_docx(data, detail)
    if ext == "pdf":
      return parse_pdf(data, detail)
    if ext == "ifc":
      return parse_ifc(data, detail)
    if ext == "dxf":
      return parse_dxf(data, detail)
    if ext == "zip":
      return parse_zip(data, detail)
    if ext == "json":
      return parse_json(data, detail)
    if ext in ("csv", "tsv"):
      return parse_csv(data, detail, "\t" if ext == "tsv" else ",")
    if ext == "py":
      return parse_py(data, detail)
    if ext in TEXT_EXTS or b"\0" not in data[:4096]:
      return parse_text(data, detail)
    return {
        "ok": True,
        "kind": ext or "binary",
        "summary": f"binary file, {len(data)} bytes",
        "size": len(data),
    }
  except Exception as exc:  # pylint: disable=broad-exception-caught
    return {
        "ok": False,
        "kind": ext,
        "error": f"{type(exc).__name__}: {exc}"[:300],
        "summary": f"could not parse ({type(exc).__name__}: {exc})"[:300],
    }


def extract_text(path: str, data: bytes, limit: int) -> str:
  """What `read_file` returns for a document, workbook, drawing or model.

  Full text where there is text (PDF pages, Word paragraphs and tables,
  workbook rows as TSV), a structured summary where there is not (IFC, DXF,
  zip), cut at `limit` characters with a note on how to get the rest.
  """
  ext = ext_of(path)
  head = f"[{path}: {len(data)} bytes, {ext}, text extracted by the harness]\n"
  try:
    if ext == "pdf":
      import pymupdf  # pylint: disable=g-import-not-at-top

      doc = pymupdf.open(stream=data, filetype="pdf")
      parts, used = [], 0
      for i, page in enumerate(doc):
        t = (page.get_text("text") or "").strip()
        parts.append(f"--- page {i + 1} of {doc.page_count} ---\n{t}")
        used += len(t)
        if used > limit:
          parts.append(f"[... {doc.page_count - i - 1} more pages not shown: read page ranges with python3 (pymupdf/pdfplumber/pdftotext) in bash]")
          break
      body = "\n".join(parts)
      if doc.page_count and used < 20:
        body += "\n[no text layer: the PDF is scanned or vector-only; render pages with pdftoppm/pymupdf and OCR if available]"
      return head + _cut(body, limit)
    if ext == "docx":
      info = parse_docx(data, limit)
      return head + _cut(info["text"], limit)
    if ext in ("xlsx", "xlsm"):
      import openpyxl  # pylint: disable=g-import-not-at-top

      wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
      parts, used = [], 0
      for ws in wb.worksheets:
        parts.append(f"=== sheet '{ws.title}' ===")
        for n, row in enumerate(ws.iter_rows(values_only=True)):
          line = "\t".join("" if c is None else str(c) for c in row)
          parts.append(line)
          used += len(line) + 1
          if used > limit:
            parts.append("[... more rows: read the workbook with openpyxl in bash]")
            break
        if used > limit:
          break
      return head + _cut("\n".join(parts), limit)
    if ext == "pptx":
      import zipfile as _zf  # pylint: disable=g-import-not-at-top
      import re as _re  # pylint: disable=g-import-not-at-top

      zf = _zf.ZipFile(io.BytesIO(data))
      parts = []
      for name in sorted(n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")):
        xml = zf.read(name).decode("utf-8", "replace")
        parts.append(f"--- {name} ---\n" + " ".join(_re.findall(r"<a:t>([^<]*)</a:t>", xml)))
      return head + _cut("\n".join(parts), limit)
    info = parse(path, data, detail=limit)
    return head + _cut(info.get("summary", ""), limit) + (
        "\n[structured file: use python3 (ifcopenshell / ezdxf / zipfile) in bash for entities and geometry]"
    )
  except Exception as exc:  # pylint: disable=broad-exception-caught
    return head + f"(could not extract: {type(exc).__name__}: {exc}; inspect it with python3 in bash)"


def _cut(text: str, limit: int) -> str:
  text = text.strip() or "(no text)"
  if len(text) <= limit:
    return text
  return text[:limit] + f"\n[... {len(text) - limit} more characters cut; read parts with python3 in bash]"


def parse_text(data: bytes, detail: int) -> dict[str, Any]:
  text = data.decode("utf-8", "replace")
  lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
  return {
      "ok": True,
      "kind": "text",
      "chars": len(text),
      "lines": lines,
      "text": text,
      "summary": f"{lines} lines, {len(text)} chars:\n{_clip(text, detail)}",
  }


def parse_json(data: bytes, detail: int) -> dict[str, Any]:
  text = data.decode("utf-8", "replace")
  try:
    obj = json.loads(text)
  except ValueError as exc:
    return {"ok": False, "kind": "json", "error": f"invalid JSON: {exc}",
            "summary": f"INVALID JSON ({exc}); head: {_clip(text, 300)}"}
  if isinstance(obj, dict):
    shape = "object with keys: " + ", ".join(list(obj)[:40])
  elif isinstance(obj, list):
    shape = f"array of {len(obj)} items"
    if obj and isinstance(obj[0], dict):
      shape += "; item keys: " + ", ".join(list(obj[0])[:30])
  else:
    shape = type(obj).__name__
  return {
      "ok": True, "kind": "json", "data": obj,
      "summary": f"{shape}\n{_clip(text, detail)}",
  }


def parse_csv(data: bytes, detail: int, delim: str = ",") -> dict[str, Any]:
  text = data.decode("utf-8", "replace")
  rows = list(csv.reader(io.StringIO(text), delimiter=delim))
  header = rows[0] if rows else []
  return {
      "ok": True, "kind": "csv", "rows": len(rows), "header": header,
      "summary": (
          f"{len(rows)} rows; header: {header}\n"
          + _clip("\n".join(delim.join(r) for r in rows[1:6]), detail)
      ),
  }


def parse_py(data: bytes, detail: int) -> dict[str, Any]:
  text = data.decode("utf-8", "replace")
  try:
    ast.parse(text)
    ok, err = True, ""
  except SyntaxError as exc:
    ok, err = False, f"SyntaxError line {exc.lineno}: {exc.msg}"
  return {
      "ok": ok, "kind": "py", "error": err, "chars": len(text),
      "summary": (err + "\n" if err else "") + _clip(text, detail),
  }


def parse_zip(data: bytes, detail: int) -> dict[str, Any]:
  try:
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    bad = zf.testzip()
  except zipfile.BadZipFile as exc:
    return {"ok": False, "kind": "zip", "error": f"not a zip: {exc}",
            "summary": f"NOT A VALID ZIP ({exc})"}
  members = {}
  for info in zf.infolist():
    if not info.is_dir():
      members[info.filename] = info.file_size
  root = sorted(n for n in members if "/" not in n)
  nested = sorted(n for n in members if "/" in n)
  return {
      "ok": bad is None, "kind": "zip", "members": members, "root": root,
      "nested": nested, "zip": zf,
      "error": f"corrupt member {bad}" if bad else "",
      "summary": (
          f"{len(members)} files; at root: {root[:30]}"
          + (f"; nested: {nested[:20]}" if nested else "")
      ),
  }


def parse_xlsx(data: bytes, detail: int) -> dict[str, Any]:
  import openpyxl  # pylint: disable=g-import-not-at-top

  wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
  try:
    wb_vals = openpyxl.load_workbook(
        io.BytesIO(data), read_only=True, data_only=True
    )
  except Exception:  # pylint: disable=broad-exception-caught
    wb_vals = None
  sheets: dict[str, dict[str, Any]] = {}
  parts = []
  for ws in wb.worksheets:
    rows = []
    formulas = uncached = 0
    vals_ws = wb_vals[ws.title] if wb_vals is not None else None
    vals_iter = vals_ws.iter_rows(values_only=True) if vals_ws is not None else None
    nonempty = 0
    first_col: list[str] = []  # the first cell of each non-empty row (row keys/IDs)
    for i, row in enumerate(ws.iter_rows(values_only=True)):
      vrow = next(vals_iter, None) if vals_iter is not None else None
      if any(c is not None and str(c).strip() != "" for c in row):
        nonempty += 1
        if row and row[0] is not None and str(row[0]).strip() and len(first_col) < 400:
          first_col.append(str(row[0]).strip()[:60])
      for j, c in enumerate(row):
        if isinstance(c, str) and c.startswith("="):
          formulas += 1
          if vrow is None or j >= len(vrow) or vrow[j] is None:
            uncached += 1
      if i < 4:
        rows.append([("" if c is None else str(c))[:40] for c in row[:16]])
      if i > 5000:
        break
    header = [h for h in (rows[0] if rows else []) if h]
    sheets[ws.title] = {
        "rows": nonempty, "header": header, "formulas": formulas,
        "uncached": uncached, "sample": rows[1:4], "first_col": first_col,
    }
    parts.append(
        f"- sheet '{ws.title}': {nonempty} non-empty rows; header: {header[:16]}"
        + (f"; {formulas} formulas ({uncached} without cached values)" if formulas else "")
        + (f"; rows: {rows[1:3]}" if rows[1:3] else "")
    )
  return {
      "ok": True, "kind": "xlsx", "sheets": sheets,
      "summary": _clip("\n".join(parts) or "(no sheets)", detail),
  }


def parse_docx(data: bytes, detail: int) -> dict[str, Any]:
  import docx  # pylint: disable=g-import-not-at-top

  doc = docx.Document(io.BytesIO(data))
  paras = [p.text for p in doc.paragraphs if p.text.strip()]
  headings = [
      p.text.strip() for p in doc.paragraphs
      if p.style is not None and p.style.name and p.style.name.lower().startswith(("heading", "title")) and p.text.strip()
  ]
  table_texts = []
  for t in doc.tables:
    for r in t.rows:
      table_texts.append(" | ".join(c.text.strip() for c in r.cells))
  text = "\n".join(paras + table_texts)
  placeholders = sorted(set(m.group(0) for m in PLACEHOLDER_RE.finditer(text)))
  return {
      "ok": True, "kind": "docx", "paragraphs": len(paras),
      "tables": len(doc.tables), "chars": len(text), "headings": headings,
      "placeholders": placeholders, "text": text,
      "summary": (
          f"{len(paras)} paragraphs, {len(doc.tables)} tables, {len(text)} chars"
          + (f"; headings: {headings[:12]}" if headings else "")
          + (f"; PLACEHOLDERS LEFT: {placeholders[:6]}" if placeholders else "")
          + "\n" + _clip(text, detail)
      ),
  }


def parse_pdf(data: bytes, detail: int) -> dict[str, Any]:
  import pymupdf  # pylint: disable=g-import-not-at-top

  doc = pymupdf.open(stream=data, filetype="pdf")
  pages = doc.page_count
  toc = doc.get_toc() or []
  head = ""
  text_chars = 0
  for i, page in enumerate(doc):
    t = page.get_text("text") or ""
    text_chars += len(t)
    if i < 2:
      head += t
    if i >= 40:
      break
  toc_lines = [f"{'  ' * (lvl - 1)}{title} (p{pg})" for lvl, title, pg in toc[:20]]
  return {
      "ok": True, "kind": "pdf", "pages": pages, "chars": text_chars,
      "toc": toc, "head": head,
      "summary": (
          f"{pages} pages, {text_chars} text chars in the first {min(pages, 41)}"
          + (" (NO TEXT LAYER: scanned? needs OCR or a visual read)" if pages and text_chars < 20 else "")
          + (f"\noutline: {'; '.join(toc_lines)}" if toc_lines else "")
          + "\n" + _clip(re.sub(r"\n{2,}", "\n", head), detail)
      ),
  }


def parse_ifc(data: bytes, detail: int) -> dict[str, Any]:
  import ifcopenshell  # pylint: disable=g-import-not-at-top

  text = data.decode("utf-8", "replace")
  f = ifcopenshell.file.from_string(text)
  schema = f.schema
  counts = collections.Counter(e.is_a() for e in f)
  products = len(f.by_type("IfcProduct"))
  project = f.by_type("IfcProject")
  site = f.by_type("IfcSite")
  storeys = f.by_type("IfcBuildingStorey")
  units = []
  try:
    for u in (project[0].UnitsInContext.Units if project and project[0].UnitsInContext else []):
      units.append(getattr(u, "Name", None) or getattr(u, "UnitType", ""))
  except Exception:  # pylint: disable=broad-exception-caught
    pass
  top = ", ".join(f"{t} x{n}" for t, n in counts.most_common(18))
  total = sum(counts.values())
  return {
      "ok": True, "kind": "ifc", "schema": schema, "products": products,
      "counts": dict(counts), "projects": len(project), "sites": len(site),
      "storeys": len(storeys),
      "summary": _clip(
          f"schema {schema}; {total} entities, {products} IfcProduct; "
          f"{len(project)} IfcProject, {len(site)} IfcSite, "
          f"{len(f.by_type('IfcBuilding'))} IfcBuilding, {len(storeys)} storeys; "
          f"units {units[:6]}; entity types: {top}", detail,
      ),
  }


def parse_dxf(data: bytes, detail: int) -> dict[str, Any]:
  import ezdxf  # pylint: disable=g-import-not-at-top

  text = data.decode("utf-8", "replace")
  # universal newlines: a DXF with CRLF line endings (what CAD and ezdxf
  # write by default) read through a plain StringIO comes back as an EMPTY
  # R12 document with no error, so every layer and entity went unseen
  doc = ezdxf.read(io.StringIO(text, newline=None))
  msp = doc.modelspace()
  layers = sorted(l.dxf.name for l in doc.layers)
  counts = collections.Counter(e.dxftype() for e in msp)
  by_layer = collections.Counter(e.dxf.layer for e in msp)
  texts = []
  for e in msp:
    if e.dxftype() in ("TEXT", "MTEXT"):
      try:
        texts.append(e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text)
      except Exception:  # pylint: disable=broad-exception-caught
        pass
    if len(texts) >= 40:
      break
  units = doc.header.get("$INSUNITS", None)
  return {
      "ok": True, "kind": "dxf", "layers": layers, "counts": dict(counts),
      "by_layer": dict(by_layer), "texts": texts, "insunits": units,
      "summary": _clip(
          f"DXF {doc.dxfversion}; $INSUNITS={units}; {sum(counts.values())} "
          f"modelspace entities: {dict(counts.most_common(10))}; layers "
          f"({len(layers)}): {layers[:40]}; entities per layer: "
          f"{dict(by_layer.most_common(15))}; text samples: {texts[:15]}",
          detail,
      ),
  }
