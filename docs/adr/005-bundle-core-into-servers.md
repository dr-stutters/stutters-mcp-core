# ADR-005: Bundle core into each server package (`<pkg>._core`), generated at build time

- Status: accepted
- Date: 2026-10-16
- Roadmap: C20 (#63); servers: ise I35, fmc F35, catc D31, cml L35
- Supersedes: the "git tag dependency" part of [ADR-001](001-shared-core-package.md)

## Context

Every server depended on core through a git source (`[tool.uv.sources] stutters-mcp-core = { git = ..., tag = ... }`),
and the Docker build and CI needed `CORE_READ_TOKEN` while core was private. Someone outside the suite who
wants one server should not need a second repository, a token, or a core release on PyPI. The owner's rule
for Wave 4: **each server is standalone, with no external requirements**.

Two servers in one environment (a shared host, one venv for several stdio servers) must also not clash: with
a shared `stutters-mcp-core` distribution, ise pinned to one core and fmc to another can't both be satisfied.

Options considered:

1. **Publish core to PyPI** and depend on a version range. Still an external requirement, and two servers
   still share one installed core, so the version clash remains.
2. **Vendor core, committed** into each server repo (`src/<pkg>/_core/`). Self-contained, but every server
   diff on a core bump is ~8k lines of generated code, reviews get noisy, and a hand edit to the copy is easy
   to make and hard to see.
3. **Vendor core, generated at build time** from a pinned tag. The repo holds only the pin; the wheel, sdist
   and image carry the copy. The source stays readable.

For option 3 there's a second choice: keep server code importing `stutters_mcp_core` and rewrite it at build
time (the wheel then differs from the source, and tests would need the same rewrite to run against it), or
have server code import `<pkg>._core` directly. The second keeps one import path in the editor, the tests and
the wheel.

## Decision

Option 3, with server code importing `<pkg>._core` directly.

- **Pin.** `[tool.bundle-core]` in the server's `pyproject.toml`: `tag`, the tag's full `commit` (a moved
  tag is detected), and the core `extras` whose dependencies the server needs (cml and catc: `tasks`).
- **Script.** `scripts/bundle_core.py` (stdlib only; canonical copy in core, each server carries a copy):
  - `vendor` checks out the pinned tag (a shallow clone, or `--source` a local clone), verifies the commit,
    copies `src/stutters_mcp_core` to `src/<pkg>/_core`, rewrites its imports to `<pkg>._core`, sets
    `__version__` to core's version (there is no `stutters-mcp-core` distribution to ask), adds core's
    `LICENSE` and a `BUNDLE.json` (tag, commit, version, dependencies, sha256 of the files);
  - it writes core's runtime dependencies (plus the extras') into a marked block of `[project].dependencies`,
    which is committed, so `uv.lock` and the wheel metadata list them;
  - `check` fails on drift: pin vs `BUNDLE.json` (a bumped tag that wasn't vendored), files vs digest (hand
    edits), the tag now pointing elsewhere, a fresh vendor of the tag producing different files, a stale
    dependency block, a leftover git dependency, and (from the first core tag that carries the script) a
    server copy of the script that differs from the tag's; `--dist` checks the built wheel and sdist too.
- **Not committed.** `src/<pkg>/_core/` is in `.gitignore`. After a clone: `python scripts/bundle_core.py vendor`,
  then `uv sync`. Ruff skips it (it honours `.gitignore`); mypy is told to ignore errors in it (core's own CI
  type-checks core).
- **CI.** `bundle-core.yml` (reusable) vendors, runs the drift check and `uv lock --check`, builds the wheel and
  sdist, installs the wheel in a clean venv with `pip install --no-index` from a predownloaded wheelhouse, and
  runs the unit + contract suite against the installed wheel with `src/` moved away. `python-ci.yml` and
  `python-release.yml` take `bundle-core: true` and vendor (and, on release, drift-check) before building.
- **Image.** The Dockerfile vendors only if the context isn't vendored yet (the release's is), runs the offline
  check, builds the wheel and installs it into the image venv next to the locked dependencies. No token.
- **Kept stable:** logger names (`stutters_mcp_core.audit`), environment variables and the `core_version` that
  `<prefix>_status` and manifests report (now the bundled version).

## Consequences

- `pip install <server>.whl` or `uvx <server>` needs nothing but PyPI; two servers in one venv each carry
  their own core. `CORE_READ_TOKEN` is gone from every Dockerfile, compose file and workflow.
- A core fix reaches a server only through a new server release (bump the pin with
  `bundle_core.py update --tag vX`, `uv lock`, release), which was already true with git pins.
- A fresh clone needs one extra step (`vendor`) before tests run; the README and CONTRIBUTING say so, and an
  import error in `<pkg>._core` points at it.
- Server diffs on a core bump are the pin, the dependency block and `uv.lock`; the generated copy never shows up.
- Core itself stays an installable package (its own tests, and any server not yet bundled, still use it).
  Publishing core to PyPI becomes optional.
- Each server carries a copy of `scripts/bundle_core.py`; `check` keeps it in step with the pinned tag.
