# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.
- Interim: the `dev` extra in `pyproject.toml` is incomplete — it declares only pytest, so
  until that is fixed install `rich`, `textual`, and `pytest-asyncio` alongside `.[dev]`.
  Without them `tests/test_tui.py` fails collection or reports every async test as
  unsupported. Delete this note once `pyproject.toml` declares those three.
- This repo has no CI configured. `.no-mistakes.yaml` declares `no_ci: true` (the documented fix
  in no-mistakes' own SKILL.md), but the currently installed no-mistakes daemon does not honor it —
  confirmed across three real axion PRs, where the pipeline's `ci` step hung indefinitely instead of
  skipping, repeating `warning: could not check CI: gh pr checks: exit status 1` (one run sat stuck
  over 2 hours before being caught). Start the *first* `no-mistakes axi run` of any session with
  `--skip ci` explicitly (e.g. `no-mistakes axi run --intent "..." --skip ci`) rather than waiting
  for the ci step to hang. If a future no-mistakes version fixes this, drop the workaround.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
