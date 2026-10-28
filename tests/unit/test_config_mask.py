"""C28: secret-bearing arguments masked in the audit log and previews (key regex, per-argument redact=
hook, SecretStr parameters, the IOS / NX-OS config line masker in scrub_text)."""

import json
import logging
from typing import Annotated, Any

import httpx
import pytest
import respx
from fastmcp import Client, Context, FastMCP
from pydantic import Field, SecretStr

from stutters_mcp_core import Confirm, Plan, ToolRegistrar, build_server, preview_or_apply
from stutters_mcp_core.audit import arg_redactors, redact_args, register_arg_redactors
from stutters_mcp_core.config_mask import mask_command_lines, mask_config_value, mask_line, mask_text
from stutters_mcp_core.formatting import MASK, is_secret_key
from stutters_mcp_core.secrets import scrub_text
from stutters_mcp_core.testing import as_caller
from stutters_mcp_core.writes import secret_parameters

from .demo import BASE, SPEC, DemoApp, make_settings

# --- the key regex ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["reg_key", "regKey", "RegKey", "nat_id", "natID"])
def test_new_secret_keys(key: str) -> None:
    assert is_secret_key(key)


@pytest.mark.parametrize(
    "key", ["community", "communityList", "communities", "natType", "region", "name", "keyIndex"]
)
def test_keys_that_stay_visible(key: str) -> None:
    assert not is_secret_key(key)  # a BGP route's `community` in a response is not a secret


@pytest.mark.parametrize("key", ["registration_key", "registrationKey"])
def test_registration_key_stays_visible(key: str) -> None:
    # fmc previews show `registration_key: 'from FMC_FTD_REG_KEY'`: a source label, not the key itself
    # (fmc's own _mask_registration_keys leaves it visible); the secret travels as `reg_key`.
    assert not is_secret_key(key)
    out = redact_args({key: "from FMC_FTD_REG_KEY", "reg_key": "cisco123", "regKey": "c1", "nat_id": "n1"})
    assert out == {key: "from FMC_FTD_REG_KEY", "reg_key": MASK, "regKey": MASK, "nat_id": MASK}


def test_redact_args_masks_ftd_registration_and_snmp_community() -> None:
    out = redact_args({"reg_key": "cisco123", "nat_id": "nat-42", "community": "Publ1c", "name": "ftd1"})
    assert out == {"reg_key": MASK, "nat_id": MASK, "community": MASK, "name": "ftd1"}


# --- the config line masker ------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, leaked",
    [
        ("enable secret 0 Sup3rS3cret", "Sup3rS3cret"),
        ("enable password Plain1", "Plain1"),
        ("enable secret 9 $9$nhEmQVczB7dqsO$X.HsgL6x1il0RxkOSSvyQYwucySCt7qFm4v7pqCxkKM", "$9$nhEm"),
        ("username admin privilege 15 secret 9 $9$abcdef", "$9$abcdef"),
        ("username admin password 5 $5$Xy role network-admin", "$5$Xy"),
        ("username ops algorithm-type scrypt secret Pa55word", "Pa55word"),
        ("snmp-server community Publ1c RO", "Publ1c"),
        ("snmp-server host 192.0.2.10 version 2c TrapC0mm", "TrapC0mm"),
        ("snmp-server user mon grp v3 auth sha AuthPw1 priv aes 128 PrivPw1", "AuthPw1"),
        ("snmp-server user mon grp v3 auth sha AuthPw1 priv aes 128 PrivPw1", "PrivPw1"),
        (" key 7 0822455D0A16", "0822455D0A16"),
        (" key-string 7 13061E010803", "13061E010803"),
        ("radius-server key Radk3y", "Radk3y"),
        ("tacacs-server key 7 0605002F", "0605002F"),
        ("crypto isakmp key IkeK3y address 0.0.0.0", "IkeK3y"),
        ("  pre-shared-key local Loc4lPsk", "Loc4lPsk"),
        (" ip ospf message-digest-key 1 md5 7 OspfK3y", "OspfK3y"),
        ("ntp authentication-key 1 md5 NtpK3y 7", "NtpK3y"),
        (" neighbor 198.51.100.1 password BgpPw", "BgpPw"),
        (" password 7 045802150C2E", "045802150C2E"),
    ],
)
def test_ios_secret_lines_masked(line: str, leaked: str) -> None:
    for out in (mask_line(line), mask_text(line), mask_command_lines(line), scrub_text(line)):
        assert leaked not in out and MASK in out


@pytest.mark.parametrize(
    "line",
    [
        "hostname r1",
        "key chain OSPF",
        "crypto key generate rsa modulus 2048",
        "crypto isakmp key ${LAB_IKE_PSK} address 0.0.0.0",
        "enable secret ${LAB_ENABLE_SECRET}",
        "enable secret $enable_secret",
        "snmp-server community $!{community} RO",
        "service password-encryption",
        "interface GigabitEthernet1",
    ],
)
def test_config_lines_without_a_secret_kept(line: str) -> None:
    assert mask_text(line) == line and scrub_text(line) == line


@pytest.mark.parametrize(
    "text",
    [
        "Password must be at least 8 characters",
        "The secret is out there",
        "key not found in the keyring",
        "community list updated",
        "Basic configuration is applied",
    ],
)
def test_prose_left_alone_by_scrub_text(text: str) -> None:
    assert scrub_text(text) == text


def test_multiline_config_masks_each_line_only() -> None:
    config = "hostname r1\nenable secret 0 Sup3r\ninterface Gi1\n ip address 192.0.2.1 255.255.255.0\n"
    out = scrub_text(config)
    assert "Sup3r" not in out and out.count("\n") == config.count("\n")
    assert "hostname r1" in out and "ip address 192.0.2.1 255.255.255.0" in out


def test_mask_config_value_walks_lists_and_dicts() -> None:
    value = {"nodes": [{"label": "r1", "config": "hostname r1\nusername a secret 0 Pw1"}], "n": 3}
    out = mask_config_value(value)
    assert "Pw1" not in json.dumps(out) and out["nodes"][0]["label"] == "r1" and out["n"] == 3
    assert mask_config_value(["key 7 ABCDEF12"]) == [f"key 7 {MASK}"]


def test_mask_text_is_more_thorough_than_scrub_text() -> None:
    # a bare 'secret X' with trailing words is not a command line for scrub_text, but is config text
    line = " secret Pw remark x"
    assert scrub_text(line) == line and "Pw" not in mask_text(line)


# --- redact_args and the per-argument hook -----------------------------------------------------


def test_redact_args_scrubs_config_lines_and_nested_config() -> None:
    out = redact_args(
        {
            "lines": ["hostname r1", "enable secret 0 Sup3r"],
            "nodes": [{"label": "r1", "config": "snmp-server community Publ1c RO"}],
            "topology_yaml": (
                "nodes:\n  - configuration: |-\n      hostname r1\n      enable secret 0 Yaml1\n"
            ),
        }
    )
    text = json.dumps(out)
    assert "Sup3r" not in text and "Publ1c" not in text and "Yaml1" not in text
    assert out["lines"][0] == "hostname r1"


def test_redact_args_with_redactors() -> None:
    args = {"blob": "opaque-secret-blob", "lines": [" secret Pw remark"], "name": "x", "empty": None}
    out = redact_args(args, redactors={"blob": True, "lines": mask_config_value, "empty": True})
    assert out == {"blob": MASK, "lines": [f" secret {MASK} remark"], "name": "x", "empty": None}


def test_failing_redactor_masks_the_value() -> None:
    def boom(v: Any) -> Any:
        raise RuntimeError("bad")

    assert redact_args({"x": "visible?"}, redactors={"x": boom}) == {"x": MASK}


def test_registered_redactors_by_tool_name() -> None:
    register_arg_redactors("demo_c28_tool", {"blob": True, "off": False})
    try:
        assert set(arg_redactors("demo_c28_tool")) == {"blob"}
        assert set(arg_redactors("demo_c28_tool@lab2")) == {"blob"}
        assert redact_args({"blob": "zz"}, tool="demo_c28_tool") == {"blob": MASK}
        assert redact_args({"blob": "zz"}, tool="other_tool") == {"blob": "zz"}
    finally:
        register_arg_redactors("demo_c28_tool", {})
    assert arg_redactors("demo_c28_tool") == {}


def test_secret_parameters_found_in_annotations() -> None:
    async def tool(
        a: SecretStr,
        b: SecretStr | None,
        c: Annotated[SecretStr, Field(description="x")],
        d: Annotated[SecretStr | None, Field(description="y")] = None,
        e: str = "",
        f: int = 0,
    ) -> None: ...

    assert secret_parameters(tool) == {"a", "b", "c", "d"}


def test_registrar_rejects_unknown_redact_argument() -> None:
    reg = ToolRegistrar(FastMCP("t"), make_settings(), "demo")
    with pytest.raises(ValueError, match="nope"):

        @reg.read(name="demo_c28_typo", redact={"nope": True})
        async def demo_c28_typo(x: str) -> str:
            return x


# --- end to end: the audit tool_call event and two-person review -------------------------------


def _register_c28(reg: ToolRegistrar) -> None:
    @reg.write(title="Push config", redact={"blob": True, "lines": mask_config_value})
    async def demo_push_config(
        ctx: Context,
        device: Annotated[str, Field(description="Device")],
        lines: Annotated[list[str], Field(description="Config lines")],
        blob: Annotated[str, Field(description="An opaque secret blob")] = "",
        enable: Annotated[SecretStr | None, Field(description="Enable secret")] = None,
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Push config lines to a device. Preview-first."""
        args = {"device": device, "lines": lines, "blob": blob, "enable": enable}
        return await preview_or_apply(
            ctx,
            tool="demo_push_config",
            args=args,
            confirm=confirm,
            plan=Plan(f"Push {len(lines)} lines to {device}", {"lines": lines}),
            apply=lambda: _noop(),
        )


async def _noop() -> dict[str, Any]:
    return {"ok": True}


ARGS = {
    "device": "r1",
    "lines": ["hostname r1", " secret Pw9 remark", "enable secret 0 Sup3r"],
    "blob": "opaque-blob-123",
    "enable": "EnableS3cret",
}
LEAKS = ("Pw9", "Sup3r", "opaque-blob-123", "EnableS3cret")


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1.2.3"}))
        yield mock


async def test_audit_and_two_person_review_mask_hooked_arguments(
    api: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    settings = make_settings(mcp_enable_writes=True, mcp_two_person_tools="demo_push_config")
    server = build_server(SPEC, settings, client_factory=DemoApp, register=[_register_c28])
    async with Client(server) as c:
        with as_caller("alice"):
            prev = (await c.call_tool("demo_push_config", ARGS)).structured_content
        assert prev is not None and prev["status"] == "preview" and "preview_id" in prev
        with as_caller("bob"):
            reviewed = await c.call_tool("demo_review_change", {"token": prev["preview_id"]})
            review = reviewed.structured_content
    assert review is not None
    shown_args = json.dumps(review["args"])
    assert not [s for s in LEAKS if s in shown_args], shown_args
    # the plan's details get the conservative scrub (command lines), not the argument hook
    shown = json.dumps(review)
    assert "Sup3r" not in shown and "opaque-blob-123" not in shown and "EnableS3cret" not in shown
    assert review["args"]["blob"] == MASK and review["args"]["enable"] == MASK
    assert review["args"]["lines"][0] == "hostname r1" and review["args"]["device"] == "r1"
    calls = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "stutters_mcp_core.audit" and '"tool_call"' in r.getMessage()
    ]
    pushed = [e for e in calls if e["tool"] == "demo_push_config"]
    assert pushed, calls
    logged = json.dumps(pushed)
    assert not [s for s in LEAKS if s in logged], logged
