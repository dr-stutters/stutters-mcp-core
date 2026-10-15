"""G4: secrets never reach logs or status; <VAR>_REF secret loaders."""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from stutters_mcp_core.formatting import MASK
from stutters_mcp_core.secrets import (
    SecretValueFilter,
    install_secret_filter,
    scrub_text,
    secret_values,
)
from stutters_mcp_core.testing import call_tool

from .demo import BASE, DemoSettings, create_server, make_settings

PASSWORD = "S3cr3t-Pass-Phrase-42"
RAW_TOKEN = "raw-token-value-abcdefghijklmnop"


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("Could not connect to https://admin:hunter22@ise.lab:443/api", "hunter22"),
        ("GET /api/x?apikey=abc123&x=1 failed", "abc123"),
        ("redirected to /login?token=zzz999;", "zzz999"),
        ("header Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc", "eyJhbGciOiJIUzI1NiJ9"),
        ("Authorization: Basic YWRtaW46c2VjcmV0cGFzcw==", "YWRtaW46c2VjcmV0cGFzcw"),
        ("sent X-Auth-Token: abcdef123", "abcdef123"),
        ("Cookie: APIC-cookie=s3ss10n", "s3ss10n"),
        ('body {"password": "hunter22", "name": "r1"}', "hunter22"),
        ("login password=hunter22 rejected", "hunter22"),
        ("?client_secret=zz11", "zz11"),
        ("Cookie: a=b; sessionid=SECRETVAL", "SECRETVAL"),
        ("ValueError: {'password': 'hunter22'}", "hunter22"),
        ("clientSecret=abc123xyz", "abc123xyz"),
    ],
)
def test_scrub_text_masks_credential_shapes(text: str, leaked: str) -> None:
    out = scrub_text(text)
    assert leaked not in out and MASK in out


@pytest.mark.parametrize(
    "text",
    [
        "GET /api/items?name=r1&limit=5 returned 404 (token count: 3)",
        "Basic authentication failed for user admin",
        "Basic configuration is applied",
        "Token management-service unavailable",
        "?author=bob&sig_count=1&authority=x&authenticated=true&design=a&assignment=b&signal=c",
        'detail {"key": "name", "passwordPolicy": "", "authType": "radius"}',
        "Bearer token missing",
        "Authorization: required for this endpoint, retry",
    ],
)
def test_scrub_text_leaves_normal_text(text: str) -> None:
    assert scrub_text(text) == text


def test_weak_values_only_masked_as_whole_words() -> None:
    from stutters_mcp_core.secrets import mask_values

    assert mask_values("user admin is administrator", ["admin"]) == f"user {MASK} is administrator"


def test_secret_values_collects_secrets_not_hashes() -> None:
    s = make_settings(
        password=PASSWORD,
        token="tok-1234567890",
        mcp_auth_tokens=f"alice:{RAW_TOKEN},bob:sha256:{'a' * 64}",
    )
    values = secret_values(s)
    assert PASSWORD in values and "tok-1234567890" in values and RAW_TOKEN in values
    assert not any(v.startswith("sha256:") or v == "a" * 64 for v in values)


def test_record_factory_covers_fastmcp_and_uvicorn_loggers(capsys: pytest.CaptureFixture[str]) -> None:
    import sys

    install_secret_filter(make_settings(password=PASSWORD))
    for name in ("fastmcp.test", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.propagate = False  # like FastMCP's and uvicorn's own loggers
        handler = logging.StreamHandler(sys.stderr)  # added after the install
        lg.addHandler(handler)
        try:
            lg.warning('%s - "%s %s HTTP/1.1" %d', "1.2.3.4", "GET", f"/mcp?x={PASSWORD}", 200)
            try:
                raise RuntimeError(f"boom {PASSWORD}")
            except RuntimeError:
                lg.exception("failed")
        finally:
            lg.removeHandler(handler)
            lg.propagate = True
    err = capsys.readouterr().err
    assert "GET" in err and "boom" in err and "Traceback" in err
    assert PASSWORD not in err


async def test_tool_errors_returned_to_client_are_scrubbed() -> None:
    from fastmcp.exceptions import ToolError

    settings = make_settings(password=PASSWORD)
    with respx.mock(base_url=BASE, assert_all_called=False) as api:
        api.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        api.get("/api/items/1").mock(
            return_value=httpx.Response(400, json={"message": f"bad credential {PASSWORD}"})
        )
        with pytest.raises(ToolError) as exc:
            await call_tool(create_server(settings), "demo_get_item", {"item_id": "1"})
    assert "bad credential" in str(exc.value) and PASSWORD not in str(exc.value)


def test_fastmcp_rich_traceback_is_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    from fastmcp.utilities.logging import configure_logging

    install_secret_filter(make_settings(password=PASSWORD))
    configure_logging("INFO")
    lg = logging.getLogger("fastmcp.server.test")
    try:
        raise ValueError(f"bad password {PASSWORD} here")
    except ValueError:
        lg.exception("Error calling tool 'x'")
    err = capsys.readouterr().err
    assert "Error calling tool" in err and "bad password" in err
    assert PASSWORD not in err


def test_filter_masks_values_args_and_tracebacks() -> None:
    filt = SecretValueFilter([PASSWORD])
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "login with %s failed", (PASSWORD,), None)
    filt.filter(rec)
    assert PASSWORD not in rec.getMessage() and MASK in rec.getMessage()
    try:
        raise RuntimeError(f"boom {PASSWORD}")
    except RuntimeError:
        import sys

        rec2 = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    filt.filter(rec2)
    assert rec2.exc_text is not None and PASSWORD not in rec2.exc_text
    assert rec2.exc_info is not None  # kept for structured handlers


async def test_no_secret_in_logs_or_status(caplog: pytest.LogCaptureFixture) -> None:
    """End to end: an appliance that echoes the password in errors; nothing the server logs or reports
    in status may contain it."""
    settings = make_settings(password=PASSWORD, mcp_auth_tokens=f"alice:{RAW_TOKEN}")
    caplog.set_level(logging.DEBUG)
    install_secret_filter(settings, [logging.getLogger("stutters_mcp_core.audit")])
    # caplog's handler is attached to the root logger; make sure it filters too
    caplog.handler.addFilter(SecretValueFilter(secret_values(settings)))
    with respx.mock(base_url=BASE, assert_all_called=False) as api:
        api.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        api.get("/api/version").mock(
            return_value=httpx.Response(500, json={"message": f"db error for password={PASSWORD}"})
        )
        api.get("/api/items/1").mock(
            return_value=httpx.Response(400, json={"message": f"bad credential {PASSWORD}"})
        )
        server = create_server(settings)
        status = await call_tool(server, "demo_status")
        with pytest.raises(Exception):  # noqa: B017 - the error text is what matters
            await call_tool(server, "demo_get_item", {"item_id": "1"})
    dumped = json.dumps(status) + "\n".join(r.getMessage() for r in caplog.records)
    assert "db error" in dumped  # the error is reported...
    assert PASSWORD not in dumped and RAW_TOKEN not in dumped  # ...without the secret
    assert "bad credential" in dumped


def test_status_url_hides_userinfo() -> None:
    from stutters_mcp_core.secrets import scrub_url

    assert scrub_url("https://u:p@host/x") == f"https://{MASK}@host/x"


def test_validation_errors_hide_input() -> None:
    with pytest.raises(ValueError) as exc:
        DemoSettings(_env_file=None, url="ftp://user:pw-in-url@host")  # type: ignore[call-arg]
    assert "pw-in-url" not in str(exc.value)


# --- <VAR>_REF loaders ----------------------------------------------------------------------


def test_file_ref_json_dotenv_and_whole_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"demo_password": "from-json"}))
    monkeypatch.setenv("DEMO_PASSWORD_REF", f"file:{creds}#demo_password")
    assert DemoSettings(url=BASE, _env_file=None).password_value() == "from-json"  # type: ignore[call-arg]
    env = tmp_path / "creds.env"
    env.write_text("OTHER=1\nDEMO_PASSWORD=from-dotenv\n")
    monkeypatch.setenv("DEMO_PASSWORD_REF", f"file:{env}#DEMO_PASSWORD")
    assert DemoSettings(url=BASE, _env_file=None).password_value() == "from-dotenv"  # type: ignore[call-arg]
    whole = tmp_path / "pw"
    whole.write_text("whole-file\n")
    monkeypatch.setenv("DEMO_PASSWORD_REF", f"file:{whole}")
    assert DemoSettings(url=BASE, _env_file=None).password_value() == "whole-file"  # type: ignore[call-arg]
    monkeypatch.setenv("DEMO_PASSWORD_REF", f"file:{creds}#nope")
    with pytest.raises(ValueError, match="no string value for key 'nope'"):
        DemoSettings(url=BASE, _env_file=None)  # type: ignore[call-arg]


def test_custom_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_SECRET_LOADERS", "fake=tests.unit.loader_fixture:FakeLoader")
    monkeypatch.setenv("DEMO_PASSWORD_REF", "fake:ise/admin")
    assert DemoSettings(url=BASE, _env_file=None).password_value() == "from-fake-ise/admin"  # type: ignore[call-arg]
    monkeypatch.setenv("DEMO_PASSWORD_REF", "fake:missing")
    with pytest.raises(ValueError, match="DEMO_PASSWORD_REF: no such secret"):
        DemoSettings(url=BASE, _env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "loaders, ref, error",
    [
        (None, "vault:x", "unknown scheme 'vault'"),
        ("bad", "file:/x", "must look like"),
        ("v=tests.unit.loader_fixture:Nope", "file:/x", "cannot import"),
        ("v=tests.unit.loader_fixture:NotALoader", "file:/x", "no load"),
    ],
)
def test_loader_errors(monkeypatch: pytest.MonkeyPatch, loaders: Any, ref: str, error: str) -> None:
    if loaders:
        monkeypatch.setenv("MCP_SECRET_LOADERS", loaders)
    monkeypatch.setenv("DEMO_PASSWORD_REF", ref)
    with pytest.raises(ValueError, match=error):
        DemoSettings(url=BASE, _env_file=None)  # type: ignore[call-arg]


def test_ref_conflicts_with_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEMO_PASSWORD", "x")
    monkeypatch.setenv("DEMO_PASSWORD_REF", "file:/x")
    with pytest.raises(ValueError, match="set DEMO_PASSWORD or DEMO_PASSWORD_REF, not both"):
        DemoSettings(url=BASE, _env_file=None)  # type: ignore[call-arg]
