"""Report lab data and secrets anywhere in a repository's git history (roadmap G9). Report only: it never
rewrites history. Run it in a server repo before making it public:

    uv run python -m stutters_mcp_core.scrub_check            # whole history, all refs
    uv run python -m stutters_mcp_core.scrub_check --head     # current tree only
    uv run python -m stutters_mcp_core.scrub_check --json report.json --strict   # exit 1 on findings

What it looks for (in every added line of every commit, plus file names):
- Lab / lab addresses (198.18.0.0/15) and lab host names (*.mitchcloud.lab), RFC 1918 addresses;
- private keys, GitHub/Slack/AWS tokens, JWTs, quoted `password="..."` values and `*_PASSWORD=...` lines;
- files that should never be committed: .env (not .env.example), *.pem, *.key, *.p12, *.pcap, *.pcapng.

Matches are shown masked (first 4 characters). Allow-list known-safe values with `--allow REGEX` (repeatable),
e.g. documentation examples. docs/PUBLIC-RELEASE.md says what to do with the findings.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("lab-address", re.compile(r"\b198\.1[89]\.\d{1,3}\.\d{1,3}\b")),
    ("lab-hostname", re.compile(r"\b[\w.-]+\.mitchcloud\.lab\b", re.IGNORECASE)),
    (
        "private-address",
        re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    ),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack-token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    (
        "secret-literal",
        re.compile(
            r"(?i)\b\w*(?:password|passwd|secret|api_?key|token|community)\w*[\"']?\s*[:=]\s*"
            r"[\"'](?P<value>[^\s\"']{6,})[\"']"
        ),
    ),
    (
        "secret-env-line",
        re.compile(
            r"^\s*(?:export\s+)?[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY)[A-Z0-9_]*=(?P<value>[^\s#]{6,})"
        ),
    ),
]
FORBIDDEN_FILES = re.compile(r"(^|/)(\.env(\.[\w-]+)?|[^/]+\.(pem|key|p12|pfx|pcap|pcapng))$")
ALLOWED_FILES = re.compile(r"(^|/)\.env\.example$")
# Values that are obviously placeholders, not secrets.
PLACEHOLDER = re.compile(
    r"(?i)^(\*+|<[^>]*>|\$\{?[\w]+\}?|x{3,}|changeme|example\w*|placeholder|redacted|none|null|true|false|"
    r"your[_-]?\w*|dummy\w*|test\w*|fake\w*|secret|password|token|\.\.\.|sha256:.*|"
    r"(settings|self|os|env|config|args|kwargs|value|data|cfg)\b.*)$"
)


@dataclass
class Finding:
    rule: str
    commit: str
    path: str
    line: int | None
    match: str


INFO_RULES = {"private-address"}  # usually documentation/test examples; listed, but not "to review"


def _mask(text: str) -> str:
    return text[:4] + "…" if len(text) > 4 else text


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True, errors="replace"
    ).stdout


def scan_text(text: str, allow: list[re.Pattern[str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for rule, pattern in RULES:
        for m in pattern.finditer(text):
            value = m.groupdict().get("value") or m.group(0)
            if rule.startswith("secret-") and PLACEHOLDER.match(value):
                continue
            if any(a.search(m.group(0)) for a in allow):
                continue
            out.append((rule, value))
    return out


def scan_history(repo: str, allow: list[re.Pattern[str]], head_only: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    if head_only:
        for path in _git(repo, "ls-files").splitlines():
            if FORBIDDEN_FILES.search(path) and not ALLOWED_FILES.search(path):
                findings.append(Finding("forbidden-file", "HEAD", path, None, path))
            try:
                content = _git(repo, "show", f"HEAD:{path}")
            except subprocess.CalledProcessError:
                continue
            for n, line in enumerate(content.splitlines(), start=1):
                for rule, value in scan_text(line, allow):
                    findings.append(Finding(rule, "HEAD", path, n, _mask(value)))
        return findings
    log = _git(repo, "log", "--all", "-p", "--no-color", "--unified=0", "--format=commit %H")
    commit, path, line_no = "", "", 0
    for raw in log.splitlines():
        if raw.startswith("commit "):
            commit = raw[7:19]
            continue
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else raw[4:]
            if FORBIDDEN_FILES.search(path) and not ALLOWED_FILES.search(path):
                findings.append(Finding("forbidden-file", commit, path, None, path))
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line_no = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            for rule, value in scan_text(raw[1:], allow):
                findings.append(Finding(rule, commit, path, line_no, _mask(value)))
            line_no += 1
    return findings


def summarize(findings: list[Finding]) -> str:
    if not findings:
        return "No findings."
    counts = Counter(f.rule for f in findings)
    review = sum(n for r, n in counts.items() if r not in INFO_RULES)
    lines = [
        f"To review: {review}. By rule: " + ", ".join(f"{r} {n}" for r, n in counts.most_common()),
        f"({', '.join(sorted(INFO_RULES))} is informational: usually example data)",
    ]
    seen: set[tuple[str, str, str]] = set()
    for f in sorted(findings, key=lambda x: x.rule in INFO_RULES):
        key = (f.rule, f.path, f.match)
        if key in seen:
            continue
        seen.add(key)
        where = f"{f.path}:{f.line}" if f.line else f.path
        lines.append(f"  {f.rule:<18} {f.commit}  {where}  {f.match}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report lab data and secrets in git history (report only).")
    parser.add_argument("repo", nargs="?", default=".", help="repository path (default: .)")
    parser.add_argument("--head", action="store_true", help="scan the current tree only, not history")
    parser.add_argument("--allow", action="append", default=[], help="regex of known-safe matches")
    parser.add_argument("--json", help="also write the findings to this JSON file")
    parser.add_argument("--strict", action="store_true", help="exit 1 when anything is found")
    args = parser.parse_args(argv)
    allow = [re.compile(a) for a in args.allow]
    findings = scan_history(args.repo, allow, head_only=args.head)
    print(summarize(findings))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump([asdict(f) for f in findings], fh, indent=2)
    return 1 if (args.strict and any(f.rule not in INFO_RULES for f in findings)) else 0


if __name__ == "__main__":
    sys.exit(main())
