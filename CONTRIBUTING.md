# Contributing

Thank you for looking at ple-sitecheck. Small, focused changes are welcome.

## Before you open a pull request

1. Run the unit regressions: `python tests/test_units.py` (no model, no key,
   no Docker). The mock end-to-end suite is `tests/run_smoke.sh` (Linux/macOS,
   needs the venv from the README).
2. Keep every new behaviour behind an environment switch that defaults to
   off, documented in `DESIGN.md`, so variants can be A/B tested.
3. Add a regression for the bug or switch in `tests/test_units.py`.

## What must never be committed

The PLE dataset is gated and its terms forbid redistribution. Do not commit,
quote or paraphrase in this repository:

- task briefs, input files, templates, rubrics, grader values or expected
  answers;
- input file names, sheet names, layer names, product designations or any
  number that comes from a task's documents;
- anything that would expose task content to the agent while it is evaluated
  (hard-coded answers, task-specific prompts or few-shots).

Test fixtures are synthetic. Dev tasks may be referred to only by their public
task IDs. Keys and tokens are passed through the environment, never written to
files in this tree (`.gitignore` covers `*.env`).

## Licence

By contributing you agree that your contribution is licensed under the
Apache License 2.0 (see `LICENSE`). Files adapted from the PLE kit keep their
Google LLC copyright header (see `NOTICE`).
