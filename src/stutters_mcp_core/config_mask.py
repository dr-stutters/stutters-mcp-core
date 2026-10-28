"""Mask secrets in IOS / IOS-XE / NX-OS / ASA configuration and CLI text, line by line (C28).

Three entry points:

- `mask_line(line)`: the secret token of one config line masked ('username x secret 9 $9$...',
  'snmp-server community x RO', 'key 7 0822...', OSPF/NTP keys, IKE pre-shared keys, SNMPv3 users, trap
  communities). Use it on text you know is device configuration or CLI.
- `mask_text(text)` / `mask_config_value(value)`: every line of a config text (or every string inside a
  list or dict) through `mask_line`. `mask_config_value` is the audit redaction hook for tool arguments
  that carry config: `@reg.write(redact={"lines": mask_config_value})`.
- `mask_command_lines(text)`: the conservative form `secrets.scrub_text` applies to every log line, error
  and preview: only lines that start like an IOS command which carries a secret are masked, so prose such
  as "Password must be at least 8 characters" is left alone.

A template or recipe variable reference ('${LAB_ENABLE}', '$enable_secret') names where a secret is kept;
it is not the secret, so it stays visible. Nothing here spans a newline.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "********"  # same as formatting.MASK (not imported: secrets imports this module)

_SP = r"[ \t]+"
# An encryption type digit before the secret ('secret 9 $9$...', 'key 7 0822...').
_TYPE = rf"(?:\d{{1,2}}{_SP})?"

# Specific forms first (the first one that matches a line wins); each masks group 'secret'.
SECRET_FORMS = [
    # ip ospf message-digest-key 1 md5 7 X / ntp authentication-key 1 md5 X 7
    re.compile(
        rf"\b(?:message-digest-key|authentication-key){_SP}\d+{_SP}(?:md5|sha\S*){_SP}{_TYPE}(?P<secret>\S+)",
        re.I,
    ),
    # wpa-psk ascii 7 X
    re.compile(rf"\bwpa-psk{_SP}(?:ascii|hex){_SP}{_TYPE}(?P<secret>\S+)", re.I),
    # IKEv2 keyring 'pre-shared-key [local|remote] [hex] [0|6|7] X'; ASA 'ikev1 pre-shared-key X'
    re.compile(
        rf"\bpre-shared-key{_SP}(?:(?:local|remote){_SP})?(?:hex{_SP})?{_TYPE}(?!address\b|hostname\b)(?P<secret>\S+)",
        re.I,
    ),
    # snmp-server host H [informs|traps] [version 1|2c|3 [auth|noauth|priv]] COMMUNITY
    re.compile(
        rf"^\s*snmp-server{_SP}host{_SP}\S+(?:{_SP}(?:informs|traps))?(?:{_SP}version{_SP}(?:1|2c|3(?:{_SP}(?:auth|noauth|priv))?))?"
        rf"{_SP}(?!community\b)(?P<secret>\S+)",
        re.I,
    ),
]
# snmp-server user U G v3 auth sha X priv aes 128 Y: both secrets
SNMP_USER = re.compile(rf"^\s*snmp-server{_SP}user\b", re.I)
SNMP_USER_SECRETS = re.compile(
    rf"\b(?P<kw>auth{_SP}(?:md5|sha\S*)|priv{_SP}(?:des\S*|3des|aes(?:{_SP}\d{{3}})?)){_SP}{_TYPE}(?P<secret>\S+)",
    re.I,
)
# Generic 'keyword [type] secret'. 'key chain', 'crypto key generate', 'pre-shared-key address' etc.
# are not secrets.
GENERIC_SECRET = re.compile(
    rf"\b(?:password|secret|key-string|key|community|psk|pre-shared-key|passphrase|server-key){_SP}{_TYPE}"
    r"(?!(?:chain|generate|config-key|zeroize|import|export|storage|label|pubkey|address|hostname|"
    r"encryption|exchange|pair|rotation|hash)\b)(?P<secret>[^\s*]\S*)",
    re.I,
)

# A variable reference ('${LAB_ENABLE}', '$secret', '$!{pw}' in a Velocity template) names where a
# secret is kept; it isn't a secret itself.
_REFERENCE = re.compile(r"\$!?\{?[A-Za-z_][\w.]*\}?[,.;:)\]'\"]*")

# Lines `mask_command_lines` treats as device configuration: IOS / NX-OS / ASA commands that carry a
# secret. A bare 'password X' / 'secret X' / 'key 7 X' (line vty, radius server blocks) counts only
# when it is the whole line, so prose that starts with "Password" is left alone.
_COMMAND_LINE = re.compile(
    r"^\s*(?:no\s+)?(?:"
    r"enable\s+(?:secret|password)\b"
    r"|username\s+\S+\s+(?:(?:privilege\s+\d+|role\s+\S+|algorithm-type\s+\S+|view\s+\S+|nopassword)\s+)*"
    r"(?:secret|password)\b"
    r"|snmp-server\s+(?:community|user|host)\b"
    r"|(?:tacacs|radius)-server\b"
    r"|crypto\s+\S+"
    r"|(?:pre-shared-key|key-string|wpa-psk|server-private|ikev[12])\b"
    r"|ip\s+(?:ospf|authentication|rip)\b"
    r"|ntp\s+authentication-key\b"
    r"|neighbor\s+\S+\s+password\b"
    r"|(?:standby|vrrp)\s+\d+\s+authentication\b"
    r"|(?:area|domain)-password\b"
    r"|isis\s+password\b"
    r"|ppp\s+(?:chap|pap)\b"
    rf"|(?:password|secret|key){_SP}{_TYPE}\S+\s*$"
    r")",
    re.I,
)
# Cheap pre-check: a text without any of these words has nothing to mask (scrub_text runs on every
# log record, so most text should leave here).
_HINT = re.compile(r"secret|password|key|community|psk|passphrase|snmp", re.I)


def _mask_group(m: re.Match[str]) -> str:
    whole, start = m.group(0), m.start("secret") - m.start(0)
    if _REFERENCE.fullmatch(m.group("secret")):
        return whole
    return whole[:start] + MASK + whole[m.end("secret") - m.start(0) :]


def mask_line(line: str) -> str:
    """One config/CLI line with its secret token masked (the line unchanged when it has none)."""
    if SNMP_USER.match(line):
        return SNMP_USER_SECRETS.sub(_mask_group, line)
    for form in SECRET_FORMS:
        masked, n = form.subn(_mask_group, line, count=1)
        if n:
            return masked
    return GENERIC_SECRET.sub(_mask_group, line)


def mask_text(text: str) -> str:
    """Every line of a device config or CLI text through `mask_line`."""
    if not text or not _HINT.search(text):
        return text
    return "\n".join(mask_line(line) for line in text.split("\n"))


def mask_config_value(value: Any) -> Any:
    """`value` (a config string, a list of config lines, or a dict/list holding them) with every string
    masked by `mask_text`. The audit redaction hook for config-bearing tool arguments."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, list | tuple):
        return [mask_config_value(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_config_value(v) for k, v in value.items()}
    return value


def mask_command_lines(text: str) -> str:
    """Mask the secret on lines that start like an IOS / NX-OS / ASA command carrying one ('enable
    secret', 'username ... secret|password', 'snmp-server community', 'key 7 ...', 'crypto ... key',
    'tacacs-server key', ...). Other lines are returned unchanged."""
    if not text or not _HINT.search(text):
        return text
    lines = text.split("\n")
    changed = False
    for i, line in enumerate(lines):
        if _COMMAND_LINE.match(line):
            masked = mask_line(line)
            if masked != line:
                lines[i] = masked
                changed = True
    return "\n".join(lines) if changed else text
