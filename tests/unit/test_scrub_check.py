"""G9: the history scrub check (report only)."""

import subprocess
from pathlib import Path

from stutters_mcp_core.scrub_check import main, scan_history, scan_text


def test_scan_text_rules() -> None:
    rules = {r for r, _ in scan_text('ISE_URL=https://198.18.134.35 password = "Hq7#lab-pass"', [])}
    assert {"lab-address", "secret-literal"} <= rules
    assert scan_text('password = "<your password>"', []) == []
    assert scan_text("token = ctx.lifespan_context['x']", []) == []
    assert [r for r, _ in scan_text("ISE_PASSWORD=Sup3rS3cret", [])] == ["secret-env-line"]
    assert scan_text("host 198.18.1.1", [__import__("re").compile(r"198\.18\.1\.1")]) == []


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_history_finds_deleted_secrets(tmp_path: Path, capsys: object) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / ".env").write_text("FMC_PASSWORD=RealLabPass1\n")
    (repo / "notes.md").write_text("FMC at fmc.demo.dcloud.cisco.com\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "oops")
    git(repo, "rm", "-q", ".env")
    git(repo, "commit", "-qm", "remove")
    rules = {f.rule for f in scan_history(str(repo), [])}
    assert {"forbidden-file", "secret-env-line", "lab-hostname"} <= rules
    assert {f.rule for f in scan_history(str(repo), [], head_only=True)} == {"lab-hostname"}
    assert main([str(repo)]) == 0  # report only
    assert main([str(repo), "--strict"]) == 1
