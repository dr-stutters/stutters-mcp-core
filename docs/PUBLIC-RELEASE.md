# Public release checklist (roadmap G9)

The repositories are **private**. This is the groundwork for opening them; nothing here has been done to
history or visibility. Work through it per repository (core first, then cml, fmc, catc, ise), and flip
visibility last.

## 1. History scrub: report (done by tooling) and decide (owner)

```bash
uv run python -m stutters_mcp_core.scrub_check --json scrub.json ~/MCP-Suite/<repo>
```

It lists, per commit and file, lab addresses (lab 198.18.0.0/15), lab host names (`*.mitchcloud.lab`),
quoted secrets and `*_PASSWORD=` lines, private keys, tokens and forbidden files (.env, keys, captures).
RFC 1918 addresses are listed as informational. Report as of 2026-10-15 (to review / informational):

| Repo | To review | Notable |
|---|---|---|
| stutters-mcp-core | test fixtures only | fake secrets in unit tests (`S3cr3t-...`, `hunter22`) |
| cml-mcp-server | ~470 | lab addresses throughout recipes, labs, docs; a lab device password in `src/cml_mcp/recipes/ftd-ha.yaml` history |
| fmc-mcp-server | ~125 | lab addresses in prompts/snapshots/tests; `fmcv...mitchcloud` host name in ROADMAP history |
| catc-mcp-server | ~100 | lab addresses, an ISE lab host name in a live test |
| ise-mcp-server | ~40 | a test private key (`tests/unit/test_certs.py`, generated for the test), a lab password in `scripts/lab_policy.py` history |

Decide per finding:
- **Lab passwords / real keys:** rotate them first (labs are throwaway, but rotate anything reused),
  then remove them from history.
- **Lab addresses and host names** (lab is public demo infrastructure, but the addresses tie the repo to
  a specific session): replace with documentation ranges in the current tree (`192.0.2.0/24`,
  `198.51.100.0/24`, `example.com`), then decide whether to rewrite history or start the public repo from a
  squashed snapshot. A fresh repo from the current tree is far simpler than rewriting five histories.
- **Test-only fake secrets and generated test keys:** fine to keep; add `--allow` patterns so the check is clean.

To rewrite (only after deciding; coordinate with every clone and CI):
```bash
pip install git-filter-repo
git filter-repo --replace-text replacements.txt      # lines like: 198.18.==>192.0.2.  or  literal:Hq7#...==>REDACTED
git filter-repo --invert-paths --path .env           # drop files
```
Then force-push, re-tag, and re-run `scrub_check --strict` until it exits 0.

## 2. Repository hygiene

- [ ] LICENSE (MIT) present in every repo; `pyproject.toml` `license` / `license-files` set.
- [ ] SECURITY.md and CONTRIBUTING.md in every repo (copy core's, adjust product scope).
- [ ] README: "not affiliated with Cisco" note, status/support statement, no internal hostnames (cortex, ports).
- [ ] Remove or generalise internal docs (`docs/ci-runner.md`, lab rebuild notes) or move them to a private repo.
- [ ] Enable private vulnerability reporting, Dependabot alerts, secret scanning + push protection.
- [ ] Branch protection on `main` (CI required, no force-push).

## 3. PyPI (trusted publishing)

Servers bundle core into their own package (ADR-005), so they don't need core on PyPI and no
`CORE_READ_TOKEN` anywhere. Publishing the servers (and, optionally, core for people building new servers):

1. Check the names are free: `stutters-mcp-core`, `cml-mcp-server`, `fmc-mcp-server`, `catc-mcp-server`,
   `ise-mcp-server` (pypi.org/project/<name>). Rename now if any is taken.
2. On pypi.org (owner account, 2FA on): *Your account → Publishing → Add a new pending publisher*, per package:
   - PyPI project name: `stutters-mcp-core`; owner `dr-stutters`; repository `stutters-mcp-core`;
   - workflow name `release.yml`; environment `pypi`.
3. In each GitHub repo: *Settings → Environments → New environment* `pypi` (optionally require a reviewer).
4. In `release.yml`: add `environment: pypi` to the job that publishes and pass `with: { publish-pypi: true }`
   (the reusable workflow already has the `pypa/gh-action-pypi-publish` step and `id-token: write`).
   Test first against TestPyPI (`repository-url: https://test.pypi.org/legacy/`, pending publisher on test.pypi.org).
5. ~~Switch servers from the git source to a version range~~: superseded by ADR-005 (core is bundled; the
   git source, `CORE_READ_TOKEN` and the Dockerfile's token secret are gone).
6. Container images: make the GHCR packages public (*Package settings → Change visibility*), decide on cosign
   signing (docs/SUPPLY-CHAIN.md).

## 4. Docs site (optional)

MkDocs Material from `docs/` (README, BUILDING-A-SERVER, PRODUCTION, SUPPLY-CHAIN, ADRs) published with GitHub
Pages from a `docs.yml` workflow. Each server: README, tool table, least-privilege guide.

## 5. Flip visibility (last)

Per repo: *Settings → General → Danger zone → Change visibility → Public*, after `scrub_check --strict` exits 0
and the checklist above is ticked. Announce with the first PyPI release.
