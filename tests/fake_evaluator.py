# Copyright 2026 ple-sitecheck authors
# Licensed under the Apache License, Version 2.0; see LICENSE.

"""A stand-in for the PLE evaluator: speaks serve protocol v1 to serve.py.

Sends the `task` frame, executes `exec` / `read_file` / `write_file` requests
in a local directory (the pretend task container), records `log` and `usage`
frames, and checks the `done` frame and the expected deliverables. With
`--cancel-after N` it sends `cancel` after N seconds and requires `done`
within 10 s, as the real evaluator does.

    python tests/fake_evaluator.py --port 18765 --workdir tests/_work/app --setup \
        --expect output/report.md output/schedule.xlsx output/memo.docx
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import pathlib
import sys
import time

import websockets

MAX_OUTPUT = 100_000

TASK_MD = """# Mock task: load schedule and memo

You are working in `{wd}`. The input loads are in `inputs/loads.csv` (columns case, kN). Our house memo format is
`templates/memo_template.docx`; read it before writing.

## Deliverable

Write a first COMPLETE version of the deliverables early, then iterate.

1. `output/report.md` - a short narrative of the basis and the result.
2. `output/schedule.xlsx` - sheet `Schedule` with columns `item`, `qty`, `unit`: one row per load case and a total row.
3. The memo for the client, saved as `{wd}/output/memo.docx`, built on the template, dated 2026-11-10.
"""

INSTRUCTION = (
    "You are working in `{wd}`. Read `task.md` there first; write your deliverables to `{wd}/output/`"
    " exactly as it says. Save the memo as `{wd}/output/memo.docx`."
)


def setup(workdir: pathlib.Path) -> None:
  import docx  # pylint: disable=g-import-not-at-top

  for sub in ("inputs", "templates", "output"):
    (workdir / sub).mkdir(parents=True, exist_ok=True)
  (workdir / "task.md").write_text(TASK_MD.format(wd=workdir))
  (workdir / "inputs" / "loads.csv").write_text("case,kN\nG1,20.0\nQ1,12.5\nW1,25.0\nS1,4.0\n")
  for stale in ("output/report.md", "output/schedule.xlsx", "output/memo.docx"):
    p = workdir / stale
    if p.exists():
      p.unlink()
  try:
    import pymupdf  # pylint: disable=g-import-not-at-top

    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Mock specification: all loads are characteristic values in kN.")
    (workdir / "inputs" / "spec.pdf").write_bytes(pdf.tobytes())
  except ImportError:
    pass
  d = docx.Document()
  d.add_heading("Memo", 0)
  d.add_paragraph("Project: [Project]")
  d.add_paragraph("Date: [Date]")
  d.add_paragraph("To: Client")
  d.save(workdir / "templates" / "memo_template.docx")


class Evaluator:
  """One task session against the harness."""

  def __init__(self, args):
    self.args = args
    self.workdir = pathlib.Path(args.workdir).resolve()
    self.done = None
    self.error = None
    self.usage = []
    self.logs = []
    self.requests = 0
    self.env = dict(os.environ)
    if args.path_prepend:
      self.env["PATH"] = args.path_prepend + os.pathsep + self.env.get("PATH", "")
    self.sem = asyncio.Semaphore(8)
    self.cancelled = False

  def resolve(self, path: str) -> pathlib.Path:
    p = pathlib.Path(path)
    return p if p.is_absolute() else self.workdir / p

  async def handle(self, ws, msg: dict) -> None:
    kind = msg.get("type")
    rid = msg.get("id")
    if kind == "log":
      self.logs.append(msg.get("text", ""))
      print(f"  [log] {str(msg.get('text', ''))[:300]}", flush=True)
      return
    if kind == "usage":
      self.usage.append(msg)
      return
    if kind == "done":
      self.done = msg
      return
    if kind == "error":
      self.error = msg
      return
    if self.cancelled:
      await ws.send(json.dumps({"type": "result", "id": rid, "ok": False, "error": "cancelled: the time is up"}))
      return
    self.requests += 1
    async with self.sem:
      if kind == "exec":
        result = await self.exec(msg)
      elif kind == "read_file":
        result = self.read_file(msg)
      elif kind == "write_file":
        result = self.write_file(msg)
      elif kind == "http":
        result = {"ok": False, "error": "no MCP servers in this task"}
      else:
        result = {"ok": False, "error": f"unknown request type {kind}"}
    await ws.send(json.dumps({"type": "result", "id": rid, **result}))

  async def exec(self, msg: dict) -> dict:
    timeout = min(int(msg.get("timeout_sec") or 120), 600)
    cmd = msg["command"]
    print(f"  [exec] {cmd[:160]!r}", flush=True)
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", cmd, cwd=self.workdir, env=self.env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    timed_out = False
    try:
      out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
      proc.kill()
      out, _ = await proc.communicate()
      timed_out = True
    text = out.decode("utf-8", "replace")
    truncated = len(text.encode()) > MAX_OUTPUT
    if truncated:
      text = text[: MAX_OUTPUT // 2] + "\n[...truncated...]\n" + text[-MAX_OUTPUT // 2:]
    return {
        "ok": True, "exit_code": 124 if timed_out else proc.returncode, "output": text,
        "truncated": truncated, "timed_out": timed_out,
    }

  def read_file(self, msg: dict) -> dict:
    p = self.resolve(msg["path"])
    if not p.is_file():
      return {"ok": False, "error": f"{msg['path']}: no such regular file"}
    data = p.read_bytes()
    truncated = len(data) > 10_000_000
    return {
        "ok": True, "content_b64": base64.b64encode(data[:10_000_000]).decode(),
        "size": len(data), "truncated": truncated,
    }

  def write_file(self, msg: dict) -> dict:
    p = self.resolve(msg["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(msg.get("content_b64") or "")
    with open(p, "ab" if msg.get("append") else "wb") as f:
      f.write(data)
    return {"ok": True, "size": p.stat().st_size}

  async def run(self) -> int:
    url = f"ws://127.0.0.1:{self.args.port}"
    t0 = time.monotonic()
    async with websockets.connect(url, max_size=16 << 20, ping_interval=None) as ws:
      task = {
          "type": "task", "protocol": 1, "session": "mock0001",
          "instruction": INSTRUCTION.format(wd=self.workdir),
          "workdir": str(self.workdir),
          "limits": {
              "agent_timeout_sec": self.args.agent_timeout, "exec_timeout_sec": 600,
              "max_output_bytes": MAX_OUTPUT, "max_write_bytes": 10_000_000,
          },
          "mcp_servers": [],
      }
      await ws.send(json.dumps(task))
      pending = set()
      cancel_at = t0 + self.args.cancel_after if self.args.cancel_after else None
      deadline = t0 + self.args.max_wait
      close_at = None
      while True:
        now = time.monotonic()
        if cancel_at and not self.cancelled and now >= cancel_at:
          print("  [evaluator] sending cancel", flush=True)
          self.cancelled = True
          await ws.send(json.dumps({"type": "cancel", "reason": "timeout"}))
          close_at = now + 10
        wait = min(deadline, close_at or deadline) - now
        if wait <= 0:
          print("  [evaluator] giving up waiting", flush=True)
          break
        try:
          frame = await asyncio.wait_for(ws.recv(), min(wait, 1.0))
        except asyncio.TimeoutError:
          continue
        except websockets.ConnectionClosed:
          print("  [evaluator] harness closed the socket", flush=True)
          break
        msg = json.loads(frame)
        if msg.get("type") in ("done", "error", "log", "usage"):
          await self.handle(ws, msg)  # one-way frames: record synchronously
          if msg.get("type") in ("done", "error"):
            break
          continue
        t = asyncio.ensure_future(self.handle(ws, msg))
        pending.add(t)
        t.add_done_callback(pending.discard)
      for t in pending:
        t.cancel()
    return self.report(time.monotonic() - t0)

  def report(self, elapsed: float) -> int:
    ok = True
    print(f"\n=== fake evaluator report ({elapsed:.1f}s, {self.requests} requests, {len(self.usage)} usage frames) ===")
    if self.done is None:
      print("FAIL: no `done` frame received", "(error frame: %s)" % self.error if self.error else "")
      ok = False
    else:
      fm = self.done.get("final_message") or ""
      print(f"done.final_message ({len(fm)} chars):\n" + "\n".join("  | " + l for l in fm.splitlines()[:40]))
      if not fm.strip():
        print("FAIL: empty final_message")
        ok = False
      for bad in self.args.forbid_final:
        if bad in fm:
          print(f"FAIL: final_message contains {bad!r}")
          ok = False
        else:
          print(f"PASS: final_message free of {bad!r}")
      for want in self.args.expect_final:
        if want in fm:
          print(f"PASS: final_message mentions {want!r}")
        else:
          print(f"FAIL: final_message lacks {want!r}")
          ok = False
    if self.args.max_elapsed and elapsed > self.args.max_elapsed:
      print(f"FAIL: took {elapsed:.0f}s, more than {self.args.max_elapsed}s")
      ok = False
    if self.args.cancel_after and self.done is not None:
      print("PASS: done arrived after cancel")
    tokens_in = sum(int(u.get("input_tokens") or 0) for u in self.usage)
    tokens_out = sum(int(u.get("output_tokens") or 0) for u in self.usage)
    cached = sum(int(u.get("cache_read_tokens") or 0) for u in self.usage)
    print(f"usage: {len(self.usage)} calls, in={tokens_in} (cached {cached}) out={tokens_out}")
    if self.args.expect_usage and not self.usage:
      print("FAIL: no usage frames")
      ok = False
    for rel in self.args.expect:
      p = self.resolve(rel)
      if p.is_file() and p.stat().st_size > 0:
        print(f"PASS: {rel} exists ({p.stat().st_size} bytes)")
      else:
        print(f"FAIL: {rel} missing or empty")
        ok = False
    for needle in self.args.expect_log:
      if any(needle in l for l in self.logs):
        print(f"PASS: log mentions {needle!r}")
      else:
        print(f"FAIL: no log line mentions {needle!r}")
        ok = False
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--port", type=int, required=True)
  ap.add_argument("--workdir", required=True)
  ap.add_argument("--setup", action="store_true", help="create the mock task files in workdir")
  ap.add_argument("--agent-timeout", type=int, default=900)
  ap.add_argument("--cancel-after", type=float, default=0)
  ap.add_argument("--max-wait", type=float, default=600)
  ap.add_argument("--expect", nargs="*", default=[])
  ap.add_argument("--expect-log", nargs="*", default=[])
  ap.add_argument("--forbid-final", nargs="*", default=[], help="strings that must not appear in final_message")
  ap.add_argument("--expect-final", nargs="*", default=[], help="strings that must appear in final_message")
  ap.add_argument("--max-elapsed", type=float, default=0, help="fail if the session takes longer than this")
  ap.add_argument("--expect-usage", action="store_true")
  ap.add_argument("--path-prepend", default="")
  args = ap.parse_args()
  wd = pathlib.Path(args.workdir).resolve()
  if args.setup:
    setup(wd)
  sys.exit(asyncio.run(Evaluator(args).run()))


if __name__ == "__main__":
  main()
