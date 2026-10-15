# Supply chain: dependency audit, image scan, SBOMs, signing (roadmap G8)

All of this lives in the reusable workflows, so every server gets it by calling them:

```yaml
# .github/workflows/ci.yml                     # .github/workflows/release.yml
jobs:                                           jobs:
  ci:                                             release:
    uses: dr-stutters/stutters-mcp-core/            uses: dr-stutters/stutters-mcp-core/
      .github/workflows/python-ci.yml@main            .github/workflows/python-release.yml@main
    secrets: inherit                                with: { image: true }
                                                    secrets: inherit
```

## On every push / PR (`python-ci.yml`)

- **pip-audit** (`audit: true` by default) checks the locked runtime dependencies (`uv export --no-dev` from
  `uv.lock`, with hashes) against the PyPI/OSV advisory databases and fails on any known vulnerability.
  `stutters-mcp-core` itself is a git dependency without a hash, so it is left out of a server's export;
  its dependencies are still audited, and core's own CI audits core.
- A vulnerability that doesn't apply can be ignored per caller, with the reason next to it:
  `with: { audit-ignore: "GHSA-xxxx-xxxx-xxxx" }  # not reachable: we never call foo()`.

## On a `v*` tag (`python-release.yml`)

1. Wheel + sdist attached to the GitHub release (as before).
2. **Python SBOM** (CycloneDX JSON, from the locked runtime dependencies) attached to the release as
   `<repo>-<tag>.python.cdx.json`.
3. With `image: true`: the image is built and pushed to GHCR with BuildKit **provenance** (`mode=max`) and an
   **SBOM attestation** attached to the image, plus an **SPDX SBOM** of the image (syft) attached to the release.
4. **Trivy** scans the pushed image (by digest) for fixable `CRITICAL,HIGH` vulnerabilities (`scan-severity`)
   and fails the workflow when it finds any (`scan-fail: true`). The image is already pushed at that point
   (Trivy needs a pullable image), so a red release means: rebuild with a fixed base image
   (`python:3.12-slim-bookworm` gets patched often), bump the dependency, or document and set `scan-fail: false`.
5. **cosign** signing: ready, **off** (`sign: false`) until a key decision is made (below).

## Turning on image signing (decision for the repo owner)

Two options; both work with the existing workflow permissions (`id-token: write`, `packages: write`).

**A. Keyless (recommended, no key to manage).** Signs with a short-lived certificate tied to the GitHub
Actions workflow identity, logged in the public Sigstore transparency log (Rekor). Note: the log entry is
public and names the repository and workflow, even though the repo is private.
1. In each server's `release.yml`: `with: { image: true, sign: true }`.
2. Verify a pulled image:
   ```bash
   cosign verify ghcr.io/dr-stutters/ise-mcp-server:v0.5.0 \
     --certificate-identity-regexp '^https://github.com/dr-stutters/.+/.github/workflows/.+@refs/tags/v' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com
   ```

**B. Key pair (private, no public log upload).**
1. `cosign generate-key-pair` on a trusted machine; store `cosign.key` + its password as org secrets
   `COSIGN_PRIVATE_KEY` / `COSIGN_PASSWORD`; publish `cosign.pub`.
2. Change the signing step to `cosign sign --key env://COSIGN_PRIVATE_KEY --tlog-upload=false <image>@<digest>`
   with those two secrets in `env:` (and pass them via `secrets: inherit`).
3. Verify with `cosign verify --key cosign.pub --insecure-ignore-tlog <image>`.

Either way, deployments can then enforce signatures (e.g. a Kubernetes policy controller, or a
`cosign verify` before `docker compose pull`).

## Running the checks locally

```bash
uv export --no-dev --no-emit-project --no-emit-package stutters-mcp-core --frozen -q -o /tmp/req.txt
uvx pip-audit==2.9.0 -r /tmp/req.txt --disable-pip
docker build -t local/x . && trivy image --severity CRITICAL,HIGH --ignore-unfixed local/x   # needs trivy installed
```
