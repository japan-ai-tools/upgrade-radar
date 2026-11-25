---
name: upgrade-radar
description: >-
  Before or after bumping a Python dependency, list the public API changes between the two versions
  (removed modules/functions/classes, renamed or removed parameters, new required parameters,
  reordered positional parameters) and the exact call sites in this repository that break, with
  file:line and the reason. Reads wheels/sdists with the ast module; nothing from the package is
  imported or executed. Use when the user asks to upgrade, bump or pin a Python package, when a
  dependency update PR (Dependabot/Renovate) needs review, or when tests fail after an upgrade.
  Do NOT use for non-Python dependencies, for judging behaviour changes that keep the same
  signature, or as a replacement for running the test suite.
license: MIT
---

# upgrade-radar

Answers "what in *my* code breaks if I move from version A to version B?" with evidence.
Python 3.9+ standard library only. The script is `scripts/upgrade_radar.py` next to this file
(below: `R`).

## When to use

- The user asks to upgrade a Python package, or a dependency-update PR is open.
- Tests started failing right after an upgrade and you need to know which API changed.

## When not to use

- npm, cargo, Go and other ecosystems.
- Behaviour changes with an unchanged signature (a function that now returns something else):
  upgrade-radar cannot see them. Read the changelog and run the tests.

## Workflow

1. Find the installed (old) and target (new) versions, e.g. from `requirements.txt`,
   `pyproject.toml` or `pip show NAME`.
2. Run:

   ```sh
   python3 R check pypi:NAME==OLD pypi:NAME==NEW --repo .
   ```

   `pypi:` downloads one wheel (or the sdist) per version from pypi.org and checks its sha256.
   Without network, pass local files or directories instead: `R check old.whl new.tar.gz --repo .`
3. Read the exit code:
   - `0`: no breaking API change reaches your code.
   - `1`: at least one call site **breaks** (listed with `file:line:col` and the reason).
   - `3`: nothing certain, but some call sites need a look (`review`, e.g. calls with `**kwargs`).
   - `2`: bad input (unknown file, no Python modules, download refused).
4. Fix each `breaks` line, then run the project's tests. Report the remaining `review` lines and
   the limits below to the user instead of calling the upgrade safe.

## Subcommands

| Command | What it does |
| --- | --- |
| `diff OLD NEW` | public API differences only |
| `check OLD NEW --repo DIR` | differences plus the affected call sites |

Options: `--json` (full report on stdout), `--out FILE`, `--show-added`, `--cache-dir DIR`.
`OLD`/`NEW` accept `.whl`, `.tar.gz`, `.zip`, a source directory, or `pypi:NAME==VERSION`.

## What counts as public

Modules and names that do not start with `_`, narrowed by `__all__` when the module defines it,
including names re-exported through `from .x import y` and `from .x import *`. A call that still
binds correctly is not reported: `connect(url, timeout=5)` is fine when `timeout` only moved
position; `connect(url, 5)` breaks because `5` now binds to a different parameter.

## Limits (say these out loud)

- Static analysis only. Methods called on instances (`client.get(...)`) are not traced to their
  class; class-level access (`Client.get`) and constructors (`Client(...)`) are.
- C extensions, `__getattr__`-based modules and runtime-generated APIs are only partly visible.
  Removed names in a module with `__getattr__` are reported as `review`, not `breaks`.
- A clean result is not proof that the upgrade is safe. Run the tests.

Report format: `references/report-format.md`.
