# Copyright 2026 ple-sitecheck authors
# Licensed under the Apache License, Version 2.0; see LICENSE.

"""Runs triage + checklist against a local folder, without any model call.

Shows exactly what the model would receive as its second user message (file
inventory, summaries, tooling probe, checklist) and how many characters that
is. Use it on any workspace folder with a brief:

    python tests/triage_dryrun.py --workdir /path/to/workspace --instruction-file brief.md

Nothing is written. The folder is read in place through a local stand-in for
the task container (bash runs here, so the tooling probe reports this
machine's Python, not the task image's).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import checklist  # pylint: disable=g-import-not-at-top,wrong-import-position
import triage  # pylint: disable=g-import-not-at-top,wrong-import-position


class LocalSession:
  """The subset of taskbridge.Session that triage uses, on a local folder."""

  def __init__(self, workdir: str, instruction: str):
    self.workdir = workdir
    self.instruction = instruction
    self.limits = {}
    self.mcp_servers = []

  def time_left(self):
    return None

  async def log(self, text):
    print(f"[log] {text[:200]}", file=sys.stderr)

  async def exec(self, command, timeout_sec=120):
    proc = subprocess.run(["bash", "-c", command], cwd=self.workdir, capture_output=True, timeout=timeout_sec, check=False)
    return {"exit_code": proc.returncode, "output": (proc.stdout + proc.stderr).decode("utf-8", "replace"), "truncated": False, "timed_out": False}

  async def read_result(self, path):
    p = pathlib.Path(path)
    p = p if p.is_absolute() else pathlib.Path(self.workdir) / p
    if not p.is_file():
      return {"ok": False, "error": "no such file"}
    data = p.read_bytes()
    return {"ok": True, "content_b64": base64.b64encode(data).decode(), "size": len(data), "truncated": False}

  async def read_file(self, path):
    r = await self.read_result(path)
    if not r["ok"]:
      raise FileNotFoundError(path)
    return base64.b64decode(r["content_b64"])


async def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--workdir", required=True)
  ap.add_argument("--instruction-file", required=True)
  ap.add_argument("--workdir-alias", default="", help="path the brief uses for the workdir (e.g. /filesystem) to rewrite")
  args = ap.parse_args()
  instruction = pathlib.Path(args.instruction_file).read_text()
  workdir = str(pathlib.Path(args.workdir).resolve())
  if args.workdir_alias:
    instruction = instruction.replace(args.workdir_alias, workdir)
  session = LocalSession(workdir, instruction)
  t0 = time.monotonic()
  cl = checklist.extract(instruction, workdir)
  inv = await triage.inventory(session, cl)
  if inv["brief"]:
    cl = checklist.extract(instruction, workdir, inv["brief"])
  print("=" * 30, "CHECKLIST")
  print(cl.to_prompt())
  print("=" * 30, "BRIEF FILES", len(inv["brief"]), "chars")
  print(inv["brief"][:1500])
  print("=" * 30, "INVENTORY", len(inv["inventory"]), "chars")
  print(inv["inventory"])
  total = len(inv["brief"]) + len(inv["inventory"]) + len(cl.to_prompt()) + len(instruction)
  print("=" * 30, f"context message total ~{total} chars (~{total // 4} tokens), {time.monotonic() - t0:.1f}s")
  return 0


if __name__ == "__main__":
  sys.exit(asyncio.run(main()))
