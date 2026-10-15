"""C17: `<server> config` shows the effective configuration without secrets."""

import json
from pathlib import Path
from typing import Any

import pytest

from stutters_mcp_core.cli import run_cli
from stutters_mcp_core.config_report import config_report, render_text

from .demo import SPEC, DemoSettings, create_server

PASSWORD = "S3cret-config-pass-77"


def test_report_sources_and_masking(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD)
    monkeypatch.setenv("DEMO_URL", "https://admin:urlpw@demo.example")
    monkeypatch.setenv("DEMO_USERNAME", "svc")
    monkeypatch.setenv("DEMO_PASSWORD_FILE", str(pw))
    monkeypatch.setenv("MCP_ENABLE_WRITES", "true")
    monkeypatch.setenv("MCP_DRY_RUN", "true")
    monkeypatch.setenv("MCP_ROLES", "alice:admin,bob:rw")
    monkeypatch.setenv("MCP_AUTH_TOKENS", "alice:" + "t" * 30)
    monkeypatch.setenv("DEMO_INSTANCES", "prod")
    monkeypatch.setenv("DEMO_PROD_URL", "https://prod.example")
    monkeypatch.setenv("DEMO_PROD_READ_ONLY", "true")
    report = config_report(DemoSettings(_env_file=None))  # type: ignore[call-arg]
    dumped = json.dumps(report, default=str)
    assert PASSWORD not in dumped and "urlpw" not in dumped and "t" * 30 not in dumped
    s = report["settings"]
    assert s["DEMO_PASSWORD"] == {"source": "env DEMO_PASSWORD_FILE", "set": True}
    assert s["DEMO_USERNAME"] == {"source": "env", "value": "svc"}
    assert s["MCP_ROLES"]["value"] == "2 entries"
    assert report["safety"] == {"dry_run": True}
    assert report["roles"] == {"entries": 2, "default_role": "ro"}
    assert report["instances"]["prod"]["rules"] == {"read_only": True}
    assert report["instances"]["prod"]["credentials"] == "missing"
    assert report["http"]["bearer_tokens"] == ["alice"]
    text = render_text(report)
    assert "DEMO_PASSWORD = set  (env DEMO_PASSWORD_FILE)" in text and PASSWORD not in text


def test_cli_config_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DEMO_URL", "https://demo.example")
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    monkeypatch.chdir(Path(__file__).parent)  # no .env here

    def create(settings: Any, **kw: Any) -> Any:
        return create_server(settings, **kw)

    run_cli(SPEC, DemoSettings, create, argv=["config", "--json"])
    out = capsys.readouterr().out
    assert json.loads(out)["settings"]["DEMO_PASSWORD"]["set"] is True and PASSWORD not in out
    monkeypatch.setenv("MCP_ROLES", "bad entry")
    with pytest.raises(SystemExit, match="invalid configuration"):
        run_cli(SPEC, DemoSettings, create, argv=["config"])


def test_plain_str_field_with_a_secret_name_is_masked() -> None:
    from stutters_mcp_core import server_config

    class S(DemoSettings):
        model_config = server_config("DEMO_")
        shared_secret: str | None = None

    report = config_report(S(_env_file=None, url="https://x.example", shared_secret="plain-secret-123"))  # type: ignore[call-arg]
    assert report["settings"]["DEMO_SHARED_SECRET"] == {"source": "default", "set": True}
    assert "plain-secret-123" not in json.dumps(report, default=str)
