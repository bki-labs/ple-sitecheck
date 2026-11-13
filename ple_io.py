# Copyright 2026 ple-sitecheck authors
# Licensed under the Apache License, Version 2.0; see LICENSE.

"""Generic file readers for engineering project files, usable from bash or Python.

The harness copies this file into the task container (IO_HELPERS=1) so the
model does not have to rediscover library APIs by trial and error. It knows
nothing about any task: it only reads formats (DXF, IFC, XLSX, PDF, DOCX, CSV,
text) and prints what is in them.

    python3 /tmp/ple_io.py dxf  PATH [--layer L] [--type T] [--limit N]
    python3 /tmp/ple_io.py xlsx PATH [--sheet S] [--rows N] [--find TEXT]
    python3 /tmp/ple_io.py pdf  PATH [--pages a-b] [--tables]
    python3 /tmp/ple_io.py ifc  PATH [--type IfcWall] [--limit N] [--find TEXT]
    python3 /tmp/ple_io.py docx PATH
    python3 /tmp/ple_io.py csv  PATH [--rows N]
    python3 /tmp/ple_io.py info PATH
    python3 /tmp/ple_io.py grep PATH_OR_DIR REGEX     (any format, recursive)
    python3 /tmp/ple_io.py write-dxf  SPEC.json OUT.dxf   (layers created; see dxf_write)
    python3 /tmp/ple_io.py write-xlsx SPEC.json OUT.xlsx  ({"Sheet": [[header...], [row...]]})
    python3 /tmp/ple_io.py write-zip  OUT.zip FILE...     (files at the zip root)

    import sys; sys.path.insert(0, "/tmp"); import ple_io
    ple_io.dxf_write(path, entities, units=None); ple_io.xlsx_write(path, sheets); ple_io.zip_write(path, files)
    ple_io.dxf_entities(path, layer=None) -> list[dict]
    ple_io.xlsx_rows(path, sheet=None) -> dict[sheet, list[list]]
    ple_io.pdf_text(path, pages=None) -> list[str]; ple_io.pdf_tables(path, pages=None)
    ple_io.ifc_entities(path, ifc_type=None) -> list[dict]
    ple_io.docx_text(path) -> str; ple_io.csv_rows(path); ple_io.text_of(path) -> str
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from typing import Any


def _r(v: Any, nd: int = 4) -> Any:
  """Rounds a number or a vector-like for printing."""
  try:
    if hasattr(v, "__iter__") and not isinstance(v, str):
      return tuple(_r(x, nd) for x in v)
    return round(float(v), nd)
  except (TypeError, ValueError):
    return v


# ---------------------------------------------------------------- DXF
def dxf_doc(path: str):
  import ezdxf  # pylint: disable=g-import-not-at-top
  try:
    return ezdxf.readfile(path)
  except (UnicodeDecodeError, ezdxf.DXFStructureError):
    import io  # pylint: disable=g-import-not-at-top
    with open(path, "rb") as fh:
      return ezdxf.read(io.StringIO(fh.read().decode("utf-8", "replace"), newline=None))


def dxf_entity(e) -> dict[str, Any]:
  """One modelspace entity as a plain dict (type, layer, handle, geometry, text)."""
  t = e.dxftype()
  d: dict[str, Any] = {"type": t, "layer": e.dxf.layer, "handle": e.dxf.handle}
  try:
    if t == "LINE":
      d["start"], d["end"] = _r(e.dxf.start), _r(e.dxf.end)
    elif t == "LWPOLYLINE":
      d["points"] = [_r(p) for p in e.get_points(format="xy")]
      d["closed"] = bool(e.closed)
    elif t == "POLYLINE":
      d["points"] = [_r(v.dxf.location) for v in e.vertices]
      d["closed"] = bool(e.is_closed)
    elif t in ("CIRCLE", "ARC"):
      d["center"], d["radius"] = _r(e.dxf.center), _r(e.dxf.radius)
      if t == "ARC":
        d["start_angle"], d["end_angle"] = _r(e.dxf.start_angle), _r(e.dxf.end_angle)
    elif t == "TEXT":
      d["text"], d["insert"] = e.dxf.text, _r(e.dxf.insert)
      d["height"] = _r(e.dxf.height)
    elif t == "MTEXT":
      d["text"], d["insert"] = e.plain_text(), _r(e.dxf.insert)
    elif t == "INSERT":
      d["block"], d["insert"], d["rotation"] = e.dxf.name, _r(e.dxf.insert), _r(e.dxf.rotation)
      d["attribs"] = {a.dxf.tag: a.dxf.text for a in e.attribs}
    elif t == "POINT":
      d["location"] = _r(e.dxf.location)
    elif t == "HATCH":
      d["paths"] = len(e.paths)
      d["pattern"] = e.dxf.pattern_name
    elif t == "DIMENSION":
      d["text"] = e.dxf.text
      d["measurement"] = _r(e.get_measurement()) if hasattr(e, "get_measurement") else None
    elif t in ("SPLINE", "ELLIPSE", "3DFACE", "SOLID"):
      try:
        from ezdxf import bbox  # pylint: disable=g-import-not-at-top
        ext = bbox.extents([e])
        d["bbox"] = (_r(ext.extmin), _r(ext.extmax))
      except Exception:  # pylint: disable=broad-exception-caught
        pass
  except Exception as exc:  # pylint: disable=broad-exception-caught
    d["error"] = f"{type(exc).__name__}: {exc}"
  return d


def dxf_entities(path: str, layer: str | None = None, etype: str | None = None) -> list[dict[str, Any]]:
  doc = dxf_doc(path)
  out = []
  for e in doc.modelspace():
    if layer and e.dxf.layer != layer:
      continue
    if etype and e.dxftype() != etype.upper():
      continue
    out.append(dxf_entity(e))
  return out


def dxf_summary(path: str) -> dict[str, Any]:
  doc = dxf_doc(path)
  msp = doc.modelspace()
  layers: dict[str, dict[str, int]] = {}
  for e in msp:
    layers.setdefault(e.dxf.layer, {})
    layers[e.dxf.layer][e.dxftype()] = layers[e.dxf.layer].get(e.dxftype(), 0) + 1
  blocks = [b.name for b in doc.blocks if not b.name.startswith("*")]
  return {
      "version": doc.dxfversion, "insunits": doc.header.get("$INSUNITS", None),
      "extmin": _r(doc.header.get("$EXTMIN", None)), "extmax": _r(doc.header.get("$EXTMAX", None)),
      "layers_defined": sorted(l.dxf.name for l in doc.layers),
      "entities_by_layer": layers, "blocks": blocks[:50],
  }


# ---------------------------------------------------------------- XLSX
def xlsx_rows(path: str, sheet: str | None = None, max_rows: int = 100000) -> dict[str, list[list[Any]]]:
  """Cached cell values per sheet (formulas come back as their last computed value)."""
  import openpyxl  # pylint: disable=g-import-not-at-top
  wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
  out: dict[str, list[list[Any]]] = {}
  for ws in wb.worksheets:
    if sheet and ws.title != sheet:
      continue
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
      if i >= max_rows:
        break
      if any(c is not None and str(c).strip() != "" for c in row):
        rows.append(list(row))
    out[ws.title] = rows
  return out


# ---------------------------------------------------------------- PDF
def _page_range(pages: str | None, n: int) -> list[int]:
  if not pages:
    return list(range(n))
  a, _, b = pages.partition("-")
  a = max(1, int(a or 1))
  b = min(n, int(b or a))
  return list(range(a - 1, b))


def pdf_text(path: str, pages: str | None = None) -> list[str]:
  """Text of each page (1-based page range "a-b")."""
  import pymupdf  # pylint: disable=g-import-not-at-top
  doc = pymupdf.open(path)
  return [doc[i].get_text("text") or "" for i in _page_range(pages, doc.page_count)]


def pdf_tables(path: str, pages: str | None = None) -> list[dict[str, Any]]:
  """Tables found on the pages, as lists of rows (pdfplumber, else pymupdf)."""
  out = []
  try:
    import pdfplumber  # pylint: disable=g-import-not-at-top
    with pdfplumber.open(path) as pdf:
      for i in _page_range(pages, len(pdf.pages)):
        for k, t in enumerate(pdf.pages[i].extract_tables() or []):
          out.append({"page": i + 1, "table": k + 1, "rows": t})
    return out
  except ImportError:
    pass
  import pymupdf  # pylint: disable=g-import-not-at-top
  doc = pymupdf.open(path)
  for i in _page_range(pages, doc.page_count):
    for k, t in enumerate(doc[i].find_tables().tables):
      out.append({"page": i + 1, "table": k + 1, "rows": t.extract()})
  return out


# ---------------------------------------------------------------- IFC
def ifc_entities(path: str, ifc_type: str | None = None, limit: int = 100000) -> list[dict[str, Any]]:
  """Products (or one type) with name, tag, placement origin and property sets."""
  import ifcopenshell  # pylint: disable=g-import-not-at-top
  try:
    import ifcopenshell.util.element as uel  # pylint: disable=g-import-not-at-top
    import ifcopenshell.util.placement as upl  # pylint: disable=g-import-not-at-top
  except ImportError:
    uel = upl = None
  f = ifcopenshell.open(path)
  out = []
  for e in f.by_type(ifc_type or "IfcProduct"):
    if len(out) >= limit:
      break
    d: dict[str, Any] = {"type": e.is_a(), "id": e.id(), "GlobalId": getattr(e, "GlobalId", None),
                         "Name": getattr(e, "Name", None), "Tag": getattr(e, "Tag", None),
                         "Description": getattr(e, "Description", None)}
    try:
      if upl is not None and getattr(e, "ObjectPlacement", None) is not None:
        m = upl.get_local_placement(e.ObjectPlacement)
        d["origin"] = _r((m[0][3], m[1][3], m[2][3]))
    except Exception:  # pylint: disable=broad-exception-caught
      pass
    try:
      if uel is not None:
        psets = uel.get_psets(e)
        d["psets"] = {k: {kk: vv for kk, vv in v.items() if kk != "id"} for k, v in psets.items()}
    except Exception:  # pylint: disable=broad-exception-caught
      pass
    out.append(d)
  return out


def ifc_summary(path: str) -> dict[str, Any]:
  import ifcopenshell  # pylint: disable=g-import-not-at-top
  f = ifcopenshell.open(path)
  counts: dict[str, int] = {}
  for e in f:
    counts[e.is_a()] = counts.get(e.is_a(), 0) + 1
  units = []
  try:
    for u in f.by_type("IfcProject")[0].UnitsInContext.Units:
      units.append(" ".join(str(x) for x in (getattr(u, "Prefix", None), getattr(u, "Name", None) or getattr(u, "UnitType", "")) if x))
  except Exception:  # pylint: disable=broad-exception-caught
    pass
  return {"schema": f.schema, "units": units, "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1]))}


# ---------------------------------------------------------------- DOCX / CSV / text
def docx_text(path: str) -> str:
  import docx  # pylint: disable=g-import-not-at-top
  d = docx.Document(path)
  parts = [p.text for p in d.paragraphs if p.text.strip()]
  for k, t in enumerate(d.tables):
    parts.append(f"--- table {k + 1} ---")
    for r in t.rows:
      parts.append(" | ".join(c.text.strip() for c in r.cells))
  return "\n".join(parts)


def csv_rows(path: str) -> list[list[str]]:
  with open(path, newline="", encoding="utf-8", errors="replace") as fh:
    sample = fh.read(4096)
    fh.seek(0)
    try:
      dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
      dialect = csv.excel
    return list(csv.reader(fh, dialect))


def text_of(path: str) -> str:
  """Extracted text of any supported file (for grep)."""
  ext = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
  if ext == "pdf":
    return "\n".join(f"--- page {i + 1} ---\n{t}" for i, t in enumerate(pdf_text(path)))
  if ext == "docx":
    return docx_text(path)
  if ext in ("xlsx", "xlsm"):
    return "\n".join(
        f"=== {s} ===\n" + "\n".join("\t".join("" if c is None else str(c) for c in r) for r in rows)
        for s, rows in xlsx_rows(path).items()
    )
  if ext == "dxf":
    return "\n".join(json.dumps(e, default=str) for e in dxf_entities(path))
  if ext == "ifc":
    return "\n".join(json.dumps(e, default=str) for e in ifc_entities(path))
  with open(path, "rb") as fh:
    return fh.read().decode("utf-8", "replace")


def info(path: str) -> str:
  ext = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
  size = os.path.getsize(path)
  if ext == "dxf":
    s = dxf_summary(path)
    return f"DXF {s['version']} $INSUNITS={s['insunits']} extents {s['extmin']}..{s['extmax']}; entities by layer: {json.dumps(s['entities_by_layer'])}; blocks: {s['blocks'][:20]}"
  if ext in ("xlsx", "xlsm"):
    return "; ".join(f"sheet '{s}': {len(r)} rows, header {r[0][:12] if r else []}" for s, r in xlsx_rows(path, max_rows=5000).items())
  if ext == "pdf":
    t = pdf_text(path)
    return f"PDF {len(t)} pages, {sum(len(x) for x in t)} text chars"
  if ext == "ifc":
    s = ifc_summary(path)
    return f"IFC {s['schema']} units {s['units']} counts {json.dumps(s['counts'])[:1500]}"
  if ext == "docx":
    t = docx_text(path)
    return f"DOCX {len(t)} chars, {t.count('--- table')} tables"
  if ext == "csv":
    r = csv_rows(path)
    return f"CSV {len(r)} rows, header {r[0][:12] if r else []}"
  return f"{ext or 'file'} {size} bytes"


# ---------------------------------------------------------------- writers
def dxf_write(path: str, entities: list[dict[str, Any]], units: int | None = None, version: str = "R2010") -> dict[str, int]:
  """Writes a DXF from plain dicts; every layer named is created in the layer table.

  Entity dicts (all take "layer", optional "color" 1-255):
    {"type": "LINE", "start": [x, y], "end": [x, y]}
    {"type": "LWPOLYLINE", "points": [[x, y], ...], "closed": true}
    {"type": "CIRCLE", "center": [x, y], "radius": r}
    {"type": "ARC", "center": [x, y], "radius": r, "start_angle": a0, "end_angle": a1}
    {"type": "TEXT", "text": "...", "insert": [x, y], "height": h}
    {"type": "MTEXT", "text": "...", "insert": [x, y], "height": h, "width": w}
    {"type": "POINT", "location": [x, y]}
  `units`: $INSUNITS (1=in, 2=ft, 4=mm, 5=cm, 6=m). Returns entities written per layer.
  """
  import ezdxf  # pylint: disable=g-import-not-at-top
  doc = ezdxf.new(version)
  if units is not None:
    doc.header["$INSUNITS"] = int(units)
  msp = doc.modelspace()
  written: dict[str, int] = {}
  for i, e in enumerate(entities):
    t = str(e.get("type", "")).upper()
    layer = str(e.get("layer") or "0")
    if layer != "0" and layer not in doc.layers:
      doc.layers.add(layer)
    attribs: dict[str, Any] = {"layer": layer}
    if e.get("color") is not None:
      attribs["color"] = int(e["color"])
    if t == "LINE":
      msp.add_line(tuple(e["start"]), tuple(e["end"]), dxfattribs=attribs)
    elif t in ("LWPOLYLINE", "POLYLINE"):
      msp.add_lwpolyline([tuple(p) for p in e["points"]], close=bool(e.get("closed", False)), dxfattribs=attribs)
    elif t == "CIRCLE":
      msp.add_circle(tuple(e["center"]), float(e["radius"]), dxfattribs=attribs)
    elif t == "ARC":
      msp.add_arc(tuple(e["center"]), float(e["radius"]), float(e.get("start_angle", 0)), float(e.get("end_angle", 360)), dxfattribs=attribs)
    elif t == "TEXT":
      attribs["height"] = float(e.get("height", 1.0))
      msp.add_text(str(e.get("text", "")), dxfattribs=attribs).set_placement(tuple(e.get("insert", (0, 0))))
    elif t == "MTEXT":
      attribs["char_height"] = float(e.get("height", 1.0))
      if e.get("width") is not None:
        attribs["width"] = float(e["width"])
      msp.add_mtext(str(e.get("text", "")), dxfattribs=attribs).set_location(tuple(e.get("insert", (0, 0))))
    elif t == "POINT":
      msp.add_point(tuple(e["location"]), dxfattribs=attribs)
    else:
      raise ValueError(f"entity {i}: unknown type {t!r} (LINE, LWPOLYLINE, CIRCLE, ARC, TEXT, MTEXT, POINT)")
    written[layer] = written.get(layer, 0) + 1
  doc.saveas(path)
  return written


def xlsx_write(path: str, sheets: dict[str, list[list[Any]]]) -> dict[str, int]:
  """Writes a workbook of plain values (header row first per sheet). Returns rows written per sheet."""
  import openpyxl  # pylint: disable=g-import-not-at-top
  wb = openpyxl.Workbook()
  wb.remove(wb.active)
  written: dict[str, int] = {}
  for name, rows in sheets.items():
    ws = wb.create_sheet(str(name)[:31])
    for r in rows:
      ws.append(list(r))
    written[str(name)[:31]] = len(rows)
  wb.save(path)
  return written


def zip_write(path: str, files: list[str] | dict[str, str]) -> list[str]:
  """Zips files at the archive root (list: by basename; dict: {arcname: source path}). Returns the member names."""
  import zipfile  # pylint: disable=g-import-not-at-top
  items = files.items() if isinstance(files, dict) else [(os.path.basename(f), f) for f in files]
  names = []
  with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
    for arc, src in items:
      zf.write(src, arc)
      names.append(arc)
  return names


# ---------------------------------------------------------------- CLI
def _print_rows(rows: list[list[Any]], limit: int) -> None:
  for r in rows[:limit]:
    print("\t".join("" if c is None else str(c) for c in r))
  if len(rows) > limit:
    print(f"[... {len(rows) - limit} more rows; raise --rows or filter with --find]")


def main(argv: list[str] | None = None) -> int:
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("cmd", choices=["dxf", "xlsx", "pdf", "ifc", "docx", "csv", "info", "grep", "write-dxf", "write-xlsx", "write-zip"])
  ap.add_argument("path", help="file to read; write-dxf/write-xlsx: the JSON spec; write-zip: the zip to create")
  ap.add_argument("pattern", nargs="*", help="grep: a regular expression (case-insensitive); write-*: the output path / the files to zip")
  ap.add_argument("--layer")
  ap.add_argument("--type", dest="etype")
  ap.add_argument("--sheet")
  ap.add_argument("--pages", help="PDF page range, 1-based, e.g. 3-5")
  ap.add_argument("--tables", action="store_true")
  ap.add_argument("--find", help="only rows/entities whose text contains this (case-insensitive)")
  ap.add_argument("--rows", type=int, default=200)
  ap.add_argument("--limit", type=int, default=300)
  a = ap.parse_args(argv)
  try:
    if a.cmd == "info":
      print(info(a.path))
    elif a.cmd == "dxf":
      s = dxf_summary(a.path)
      print(f"DXF {s['version']}; $INSUNITS={s['insunits']} (1=in 2=ft 4=mm 6=m, 0=unitless); extents {s['extmin']} .. {s['extmax']}")
      print("entities by layer:", json.dumps(s["entities_by_layer"]))
      ents = dxf_entities(a.path, a.layer, a.etype)
      if a.find:
        ents = [e for e in ents if a.find.lower() in json.dumps(e, default=str).lower()]
      print(f"{len(ents)} entities" + (f" on layer {a.layer}" if a.layer else "") + (f" of type {a.etype}" if a.etype else "") + ":")
      for e in ents[:a.limit]:
        print(json.dumps(e, default=str))
      if len(ents) > a.limit:
        print(f"[... {len(ents) - a.limit} more; use --layer/--type/--find or raise --limit]")
    elif a.cmd == "xlsx":
      for s, rows in xlsx_rows(a.path, a.sheet).items():
        print(f"=== sheet '{s}' ({len(rows)} non-empty rows) ===")
        if a.find:
          rows = [rows[0]] + [r for r in rows[1:] if a.find.lower() in "\t".join("" if c is None else str(c) for c in r).lower()] if rows else rows
        _print_rows(rows, a.rows)
    elif a.cmd == "pdf":
      for i, t in zip(_page_range(a.pages, 10**6), pdf_text(a.path, a.pages)):
        print(f"--- page {i + 1} ---")
        print(t)
      if a.tables:
        for t in pdf_tables(a.path, a.pages):
          print(f"--- table {t['table']} on page {t['page']} ---")
          _print_rows(t["rows"], a.rows)
    elif a.cmd == "ifc":
      s = ifc_summary(a.path)
      print(f"IFC {s['schema']}; units {s['units']}; entity counts: {json.dumps(s['counts'])[:2000]}")
      ents = ifc_entities(a.path, a.etype, a.limit if not a.find else 10**6)
      if a.find:
        ents = [e for e in ents if a.find.lower() in json.dumps(e, default=str).lower()][:a.limit]
      print(f"{len(ents)} entities" + (f" of type {a.etype}" if a.etype else " (IfcProduct)") + ":")
      for e in ents:
        print(json.dumps(e, default=str))
    elif a.cmd == "docx":
      print(docx_text(a.path))
    elif a.cmd == "csv":
      _print_rows(csv_rows(a.path), a.rows)
    elif a.cmd == "write-dxf":
      if len(a.pattern) != 1:
        ap.error("write-dxf SPEC.json OUT.dxf")
      with open(a.path, encoding="utf-8") as fh:
        spec = json.load(fh)
      ents = spec["entities"] if isinstance(spec, dict) else spec
      w = dxf_write(a.pattern[0], ents, units=spec.get("units") if isinstance(spec, dict) else None)
      print(f"wrote {a.pattern[0]}: entities per layer {json.dumps(w)}")
    elif a.cmd == "write-xlsx":
      if len(a.pattern) != 1:
        ap.error("write-xlsx SPEC.json OUT.xlsx")
      with open(a.path, encoding="utf-8") as fh:
        spec = json.load(fh)
      w = xlsx_write(a.pattern[0], spec)
      print(f"wrote {a.pattern[0]}: rows per sheet {json.dumps(w)}")
    elif a.cmd == "write-zip":
      if not a.pattern:
        ap.error("write-zip OUT.zip FILE...")
      names = zip_write(a.path, list(a.pattern))
      print(f"wrote {a.path}: {names}")
    elif a.cmd == "grep":
      if len(a.pattern) != 1:
        ap.error("grep needs one pattern")
      rx = re.compile(a.pattern[0], re.I)
      paths = [a.path]
      if os.path.isdir(a.path):
        paths = [os.path.join(r, f) for r, _, fs in os.walk(a.path) for f in sorted(fs)]
      hits = 0
      for p in paths:
        if os.path.getsize(p) > 50_000_000:
          continue
        try:
          lines = text_of(p).splitlines()
        except Exception as exc:  # pylint: disable=broad-exception-caught
          print(f"{p}: (unreadable: {type(exc).__name__}: {exc})")
          continue
        for n, line in enumerate(lines, 1):
          if rx.search(line):
            print(f"{p}:{n}: {line.strip()[:300]}")
            hits += 1
            if hits >= a.limit:
              print(f"[... stopped at {a.limit} hits; narrow the pattern or raise --limit]")
              return 0
      print(f"{hits} hit(s)")
  except Exception as exc:  # pylint: disable=broad-exception-caught
    print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
