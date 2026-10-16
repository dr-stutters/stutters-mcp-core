"""C21: the `check` preflight helper (reachability, TLS, auth, role vs tiers, version) and its CLI."""

import json
import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from stutters_mcp_core.cli import run_cli
from stutters_mcp_core.preflight import Outcome, Preflight, exit_code, render_text, run_preflight

from .demo import BASE, SPEC, DemoApp, DemoSettings, create_server, make_settings

PROD = "https://prod.test"


async def demo_preflight(settings: DemoSettings, pf: Preflight) -> None:
    """What a server writes: probe, then compare the account's role and the version."""
    app = DemoApp(settings)
    try:
        info = await pf.reach("api", app.api.get("/api/version"), detail=lambda i: f"version {i['version']}")
        if info is None:
            return
        me = await pf.reach("users", app.api.get("/api/me"), check="role")
        if me is not None:
            pf.results.pop()  # replaced by the verdict below
            pf.access(write=me.get("write"), role=me.get("role"))
        pf.version(info["version"], tested=["3.5", "3.4"], minimum="3.1")
    finally:
        await app.aclose()


def _mock(version: str = "3.5.0.527", me: dict[str, Any] | None = None, base: str = BASE) -> None:
    respx.post(f"{base}/api/login").mock(return_value=httpx.Response(200, json={"token": "t"}))
    respx.get(f"{base}/api/version").mock(return_value=httpx.Response(200, json={"version": version}))
    respx.get(f"{base}/api/me").mock(
        return_value=httpx.Response(200, json=me if me is not None else {"role": "ro", "write": False})
    )


async def _run(settings: DemoSettings, **kw: Any) -> dict[str, Any]:
    return await run_preflight(
        settings, demo_preflight, server=SPEC.name, product=SPEC.product, env_prefix=SPEC.env_prefix, **kw
    )


def _by_check(report: dict[str, Any], instance: int = 0) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for c in report["instances"][instance]["checks"]:
        out.setdefault(c["check"], []).append(c)
    return out


def _outcome(report: dict[str, Any], check: str) -> str:
    return str(_by_check(report)[check][0]["outcome"])


@respx.mock
async def test_all_ok_read_only() -> None:
    _mock()
    report = await _run(make_settings())
    assert report["ok"] and exit_code(report) == 0
    checks = _by_check(report)
    assert [c["check"] for c in report["instances"][0]["checks"]] == [
        "config",
        "reachability",
        "tls",
        "auth",
        "role",
        "version",
    ]
    assert "read-only" in checks["config"][0]["detail"]
    assert checks["reachability"][0]["surface"] == "api"
    assert "version 3.5.0.527" in checks["reachability"][0]["detail"]
    assert "system trust store" in checks["tls"][0]["detail"]
    assert checks["role"][0]["outcome"] == "ok" and checks["version"][0]["outcome"] == "ok"
    assert "password" not in json.dumps(report).lower()


@respx.mock
async def test_unreachable_skips_the_rest() -> None:
    respx.post(f"{BASE}/api/login").mock(side_effect=httpx.ConnectError("connection refused"))
    report = await _run(make_settings())
    assert not report["ok"] and exit_code(report) == 1
    reach = _by_check(report)["reachability"][0]
    assert reach["outcome"] == "fail" and "DEMO_URL" in reach["hint"]
    for check in ("tls", "auth", "role", "version"):
        assert _outcome(report, check) == "skip"


@respx.mock
async def test_tls_failure_names_ca_bundle() -> None:
    respx.post(f"{BASE}/api/login").mock(
        side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate")
    )
    report = await _run(make_settings())
    tls = _by_check(report)["tls"][0]
    assert tls["outcome"] == "fail" and "DEMO_CA_BUNDLE" in tls["hint"]
    assert "reachability" not in _by_check(report)


@respx.mock
async def test_bad_credentials() -> None:
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json={"token": "t"}))
    respx.get(f"{BASE}/api/version").mock(return_value=httpx.Response(401))
    report = await _run(make_settings())
    auth = _by_check(report)["auth"][0]
    assert auth["outcome"] == "fail" and "DEMO_USERNAME" in auth["hint"]
    assert _outcome(report, "role") == "skip"


@respx.mock
async def test_role_forbidden_403() -> None:
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json={"token": "t"}))
    respx.get(f"{BASE}/api/version").mock(return_value=httpx.Response(200, json={"version": "3.5"}))
    respx.get(f"{BASE}/api/me").mock(return_value=httpx.Response(403))
    report = await _run(make_settings())
    role = _by_check(report)["role"][0]
    assert role["outcome"] == "fail" and "least-privilege" in role["hint"]


@respx.mock
async def test_role_missing_write_tier_fails() -> None:
    _mock(me={"role": "ro", "write": False})
    report = await _run(make_settings(mcp_enable_writes=True))
    role = _by_check(report)["role"][0]
    assert role["outcome"] == "fail" and "write" in role["detail"]
    assert "MCP_ENABLE_WRITES=false" in role["hint"]


@respx.mock
async def test_role_unknown_warns() -> None:
    _mock(me={"role": "custom"})  # write: None -> unknown
    report = await _run(make_settings(mcp_enable_writes=True))
    assert _outcome(report, "role") == "warn" and report["ok"]
    assert exit_code(report, strict=True) == 1


@respx.mock
async def test_role_more_than_needed_warns_least_privilege() -> None:
    _mock(me={"role": "admin", "write": True})
    report = await _run(make_settings())
    role = _by_check(report)["role"][0]
    assert role["outcome"] == "warn" and "read-only account" in role["hint"]


@pytest.mark.parametrize(
    ("version", "outcome"),
    [("3.4.0.608", "ok"), ("3.6.0", "warn"), ("2.7.0", "fail"), ("unknown", "warn")],
)
@respx.mock
async def test_version_matrix(version: str, outcome: str) -> None:
    _mock(version=version)
    report = await _run(make_settings())
    assert _outcome(report, "version") == outcome


@respx.mock
async def test_plain_http_and_unverified_tls_warn() -> None:
    _mock(base="http://demo.test")
    report = await _run(make_settings(url="http://demo.test"))
    config = _by_check(report)["config"]
    assert [c["outcome"] for c in config] == ["ok", "warn"]
    assert _outcome(report, "tls") == "warn"
    respx.reset()
    _mock()
    report = await _run(make_settings(verify_ssl=False))
    assert "NOT verified" in _by_check(report)["tls"][0]["detail"]


@respx.mock
async def test_server_exception_is_reported_not_raised() -> None:
    async def broken(settings: Any, pf: Preflight) -> None:
        raise RuntimeError("bug")

    report = await run_preflight(make_settings(), broken, server="s", product="Demo", env_prefix="DEMO_")
    assert _outcome(report, "internal") == "fail"


async def test_unwrapped_ssl_error_is_classified() -> None:
    async def raw(settings: Any, pf: Preflight) -> None:
        raise ssl.SSLCertVerificationError("certificate verify failed")

    report = await run_preflight(make_settings(), raw, server="s", product="Demo", env_prefix="DEMO_")
    assert _outcome(report, "tls") == "fail"


@respx.mock
async def test_multi_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_PROD_URL", PROD)
    monkeypatch.setenv("DEMO_PROD_USERNAME", "produser")
    monkeypatch.setenv("DEMO_PROD_PASSWORD", "prodpass")
    _mock()
    respx.post(f"{PROD}/api/login").mock(side_effect=httpx.ConnectError("no route"))
    settings = make_settings(instances="prod")
    report = await _run(settings)
    assert [i["instance"] for i in report["instances"]] == ["default", "prod"]
    assert report["instances"][0]["ok"] and not report["instances"][1]["ok"] and not report["ok"]
    text = render_text(report)
    assert "instance prod: https://prod.test" in text and "FAILED: 1 failed" in text
    only = await _run(settings, instances=["default"])
    assert only["ok"] and len(only["instances"]) == 1
    with pytest.raises(ValueError, match="unknown instance"):
        await _run(settings, instances=["nope"])


def test_access_dangerous_follows_write_by_default() -> None:
    pf = Preflight(
        make_settings(mcp_enable_writes=True, mcp_enable_dangerous=True), product="Demo", env_prefix="DEMO_"
    )
    assert pf.access(write=True).outcome is Outcome.OK
    assert pf.access(write=True, dangerous=False).outcome is Outcome.FAIL


# --- CLI -----------------------------------------------------------------------------------


def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_URL", BASE)
    monkeypatch.setenv("DEMO_USERNAME", "u")
    monkeypatch.setenv("DEMO_PASSWORD", "s3cret-value")


@respx.mock
def test_cli_check_text_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(tmp_path, monkeypatch)
    _mock(me={"role": "admin", "write": True})
    with pytest.raises(SystemExit) as e:
        run_cli(SPEC, DemoSettings, create_server, argv=["check"], preflight=demo_preflight)
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "OK    reachability[api]" in out and "WARN  role" in out and "passed: 0 failed" in out
    assert "s3cret-value" not in out

    with pytest.raises(SystemExit) as e:
        run_cli(
            SPEC, DemoSettings, create_server, argv=["check", "--json", "--strict"], preflight=demo_preflight
        )
    assert e.value.code == 1  # the warning fails under --strict
    data = json.loads(capsys.readouterr().out)
    assert data["server"] == "demo-mcp-server" and data["ok"] is True


@respx.mock
def test_cli_check_failure_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(tmp_path, monkeypatch)
    respx.post(f"{BASE}/api/login").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SystemExit) as e:
        run_cli(SPEC, DemoSettings, create_server, argv=["check"], preflight=demo_preflight)
    assert e.value.code == 1
    assert "FAIL  reachability[api]" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        run_cli(
            SPEC, DemoSettings, create_server, argv=["check", "--instance", "x"], preflight=demo_preflight
        )
    assert e.value.code == 2


def test_cli_serve_http_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cli_env(tmp_path, monkeypatch)
    monkeypatch.delenv("MCP_AUTH_TOKENS", raising=False)
    with pytest.raises(SystemExit) as e:  # HTTP fails closed without tokens: proves --http selected HTTP
        run_cli(SPEC, DemoSettings, create_server, argv=["serve", "--http"])
    assert "requires MCP_AUTH_TOKENS" in str(e.value.code)
