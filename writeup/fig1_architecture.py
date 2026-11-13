# Copyright 2026 ple-sitecheck authors
# Licensed under the Apache License, Version 2.0; see LICENSE.

"""Figure 1 of the Stage-1 writeup: the ple-sitecheck architecture.

    python writeup/fig1_architecture.py            # writes writeup/fig1_architecture.png

Needs matplotlib only (not a harness dependency: use a separate venv).
"""

from __future__ import annotations

import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # pylint: disable=g-import-not-at-top,wrong-import-position
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # pylint: disable=g-import-not-at-top,wrong-import-position

OUT = pathlib.Path(__file__).with_name("fig1_architecture.png")

INK = "#1f2933"
MUTED = "#52606d"
LINE = "#9aa5b1"
STAGE = "#e3f2fd"      # harness stages
STAGE_EDGE = "#1565c0"
EXT = "#f5f5f5"        # things outside the harness
EXT_EDGE = "#616161"
CHECK = "#fff8e1"      # verification / safety nets
CHECK_EDGE = "#ef6c00"


def box(ax, x, y, w, h, title, body="", fill=STAGE, edge=STAGE_EDGE, title_size=10.5, body_size=8.1):
  ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.12",
                              linewidth=1.4, edgecolor=edge, facecolor=fill))
  ty = y + h - 0.16 if body else y + h / 2
  ax.text(x + w / 2, ty, title, ha="center", va="top" if body else "center", fontsize=title_size,
          fontweight="bold", color=INK)
  if body:
    ax.text(x + w / 2, y + h - 0.42, body, ha="center", va="top", fontsize=body_size, color=MUTED, linespacing=1.25)


def arrow(ax, p, q, text="", color=LINE, style="-|>", lw=1.4, rad=0.0, tpos=0.5, dx=0.0, dy=0.12, fs=8.2):
  a = FancyArrowPatch(p, q, arrowstyle=style, mutation_scale=14, linewidth=lw, color=color,
                      connectionstyle=f"arc3,rad={rad}", shrinkA=2, shrinkB=2)
  ax.add_patch(a)
  if text:
    mx, my = p[0] + (q[0] - p[0]) * tpos + dx, p[1] + (q[1] - p[1]) * tpos + dy
    ax.text(mx, my, text, ha="center", va="bottom", fontsize=fs, color=MUTED,
            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.9))


def main() -> None:
  fig, ax = plt.subplots(figsize=(12.5, 7.2), dpi=160)
  ax.set_xlim(0, 12.5)
  ax.set_ylim(0, 7.2)
  ax.axis("off")

  # outside the harness: evaluator / task container (left), model endpoint (right)
  box(ax, 0.25, 0.9, 2.3, 5.5, "Task container", "(evaluator, WebSocket)\n\nproject files:\nPDF, DXF, IFC, xlsx, docx\n\nMCP apps\n\nbash / read / write\n\ndeliverables",
      fill=EXT, edge=EXT_EDGE)
  box(ax, 10.15, 4.25, 2.1, 2.2, "Model endpoint", "google-cloud/<id>\nChat Completions,\nstreamed\n\nswapped in\nsubmission.yaml only",
      fill=EXT, edge=EXT_EDGE, body_size=8.0)

  # harness stages, top to bottom
  x, w, h = 3.25, 4.05, 0.98
  ys = {"triage": 5.45, "checklist": 4.25, "loop": 3.05, "verify": 1.85, "final": 0.65}
  box(ax, x, ys["triage"], w, h, "1  Triage (no tokens)",
      "list files, probe tooling, parse xlsx/docx/pdf/ifc/dxf\nin the serve container -> compact inventory")
  box(ax, x, ys["checklist"], w, h, "2  Deliverable checklist",
      "paths, zip members, sheets/columns, layers\nparsed from the brief (regex + 1 cheap model call)")
  box(ax, x, ys["loop"], w, h, "3  Work loop (engine)",
      "stable prefix: system + brief + inventory + checklist\nturn/time nudges, compaction, tool outputs clipped")
  box(ax, x, ys["verify"], w, h, "4  Verify + repair (no tokens)",
      "open every deliverable: exists, parses, sheets/\ncolumns/layers present, values cached, no templates",
      fill=CHECK, edge=CHECK_EDGE)
  box(ax, x, ys["final"], w, h, "6  Final message",
      "model summary + harness-verified file list;\nfallback text: a run never ends without one",
      fill=CHECK, edge=CHECK_EDGE)

  # review on the side, between loop and final
  sx, sw = 7.7, 2.2
  box(ax, sx, 1.85, sw, 0.98, "5  Review round",
      "re-derive governing\nnumbers, units, clauses,\nbasis stated in the file",
      fill=CHECK, edge=CHECK_EDGE, title_size=9.8, body_size=7.8)

  # safety nets column
  box(ax, sx, 3.05, sw, 0.98, "Rate-limit wait",
      "429/503: back off within\nthe task's time budget;\nnever finish early",
      fill=CHECK, edge=CHECK_EDGE, title_size=9.8, body_size=7.8)
  box(ax, sx, 0.65, sw, 0.98, "Placeholder on failure",
      "honest 'not produced' file\nat each missing path,\nflagged in the message",
      fill=CHECK, edge=CHECK_EDGE, title_size=9.8, body_size=7.8)

  # flow arrows down the stages
  cx = x + w / 2
  for a, b in (("triage", "checklist"), ("checklist", "loop"), ("loop", "verify"), ("verify", "final")):
    arrow(ax, (cx, ys[a]), (cx, ys[b] + h), color=STAGE_EDGE)
  ax.text(cx + 0.08, ys["loop"] - 0.14, "model answers without a tool call", ha="left", va="center", fontsize=7.8, color=MUTED)
  # repair loop back from verify to loop
  arrow(ax, (x, ys["verify"] + 0.5), (x, ys["loop"] + 0.5), text="issues -> back to work\n(bounded repair rounds)",
        color=CHECK_EDGE, rad=0.6, dx=-0.85, dy=-0.15)
  # review <-> verify
  arrow(ax, (sx, ys["verify"] + 0.5), (x + w, ys["verify"] + 0.5), color=CHECK_EDGE, style="<|-|>")
  # loop <-> model: up and over the safety-net column
  arrow(ax, (x + w, ys["loop"] + 0.85), (10.15, 5.2), color=EXT_EDGE, style="<|-|>", rad=-0.25,
        text="every call goes through\nthe rate-limit wait", tpos=0.45, dx=0.0, dy=0.55, fs=7.4)
  arrow(ax, (sx, ys["loop"] + 0.5), (x + w, ys["loop"] + 0.5), color=CHECK_EDGE, style="-|>")
  # final <- placeholder
  arrow(ax, (sx, ys["final"] + 0.5), (x + w, ys["final"] + 0.5), color=CHECK_EDGE, style="-|>")
  # task container <-> stages
  for key, label in (("triage", "find, read_file"), ("loop", "bash, read, write,\nedit, MCP"), ("verify", "read_file"), ("final", "write placeholder")):
    arrow(ax, (2.55, ys[key] + 0.5), (x, ys[key] + 0.5), color=EXT_EDGE, style="<|-|>", text=label, dy=0.08, fs=7.4)

  ax.text(0.25, 6.85, "ple-sitecheck: a site engineer's checklist around a plain tool-calling loop",
          fontsize=13, fontweight="bold", color=INK, va="center")
  ax.text(0.25, 6.55, "Blue: stages (1-3 build the context once, cached across turns). Orange: checks and safety nets that cost CPU, not tokens. Grey: outside the harness.",
          fontsize=8.6, color=MUTED, va="center")
  fig.savefig(OUT, bbox_inches="tight", facecolor="white")
  print(f"wrote {OUT}")


if __name__ == "__main__":
  main()
