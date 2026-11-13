# Copyright 2026 ple-sitecheck authors
# Licensed under the Apache License, Version 2.0; see LICENSE.

"""Checklist regression: parse the 29 dev-task instructions, compare with
`eval/dev_set.yaml` deliverables. Reads only task text (the public template's
trajectory step 1), never graders or our artifacts.

    PLE_KIT=~/ple/kit-src PLE_TEMPLATE=~/ple/template-src python tests/test_checklist.py
"""

from __future__ import annotations

import glob
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import checklist  # pylint: disable=g-import-not-at-top,wrong-import-position

KIT = pathlib.Path(os.environ.get("PLE_KIT", "~/ple/kit-src")).expanduser()
TEMPLATE = pathlib.Path(os.environ.get("PLE_TEMPLATE", "~/ple/template-src")).expanduser()


def dev_set() -> dict[str, list[str]]:
  text = (KIT / "eval" / "dev_set.yaml").read_text()
  out, cur = {}, None
  for line in text.splitlines():
    m = re.match(r"- id: (\S+)", line)
    if m:
      cur = m.group(1)
      out[cur] = []
    m = re.match(r"\s+deliverables: \[(.*)\]", line)
    if m and cur:
      out[cur] = [p.strip() for p in m.group(1).split(",") if p.strip()]
  return out


def main() -> int:
  wanted = dev_set()
  if not wanted:
    print("dev_set.yaml not found; skipping")
    return 0
  hits = total = 0
  rows = []
  for traj in sorted(glob.glob(str(TEMPLATE / "artifacts" / "*" / "trajectory.json"))):
    task = pathlib.Path(traj).parent.name
    if task not in wanted:
      continue
    steps = json.load(open(traj))["steps"]
    instruction = steps[0]["message"]
    workdir = "/app"
    cl = checklist.extract(instruction, workdir)
    got = [d.path for d in cl.deliverables]
    covered = []
    for want in wanted[task]:
      if want.endswith("/"):
        ok = any(g.startswith(want) for g in got)
      else:
        # the test does not know each task's real workdir, so a path we resolved
        # against /app also counts when the wanted path ends with its tail
        ok = want in got or (want.replace("/filesystem/filesystem/", "/filesystem/") in got) or any(
            g.startswith("/app/") and want.endswith(g[len("/app"):]) for g in got
        )
      covered.append(ok)
    file_wants = [w for w in wanted[task] if not w.endswith("/")]
    # a task with only folder deliverables is scored on having found any path under them
    n = len(file_wants) or 1
    k = sum(c for c, w in zip(covered, wanted[task]) if not w.endswith("/")) if file_wants else int(any(covered))
    hits += k
    total += n
    detail = ""
    zips = [d for d in cl.deliverables if d.kind == "zip"]
    if zips:
      detail = f" zip members={zips[0].members} sheets={list(zips[0].sheets)[:6]} layers={zips[0].layers[:4]}"
    rows.append(f"{'OK ' if k == n else 'MISS'} {task:36s} got={got[:3]}{detail}")
  print("\n".join(rows))
  print(f"\nchecklist regex recall on dev-set file deliverables: {hits}/{total}")
  return 0 if hits >= total * 0.8 else 1


if __name__ == "__main__":
  sys.exit(main())
