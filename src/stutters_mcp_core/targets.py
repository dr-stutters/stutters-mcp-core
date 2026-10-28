"""Several appliances per server, selected per call (roadmap G10, ADR-004).

    ISE_URL=https://ise-lab ...          the default instance (ISE_DEFAULT_INSTANCE names it; "default")
    ISE_INSTANCES=prod,dr                more instances, each with its own prefix:
    ISE_PROD_URL=https://ise-prod  ISE_PROD_USERNAME=...  ISE_PROD_PASSWORD_FILE=...

With more than one instance, every tool gets an optional `instance` argument; tools call `get_client(ctx)` as
before and receive the selected target's client. With one target nothing changes.

Read-tier tools also take `instance='*'` (every instance) or a comma list ('prod,dr') (C74): the call runs
once per instance, at most MCP_FANOUT_CONCURRENCY at a time, and returns
`{"instances": {name: {"result": ...} | {"error": "..."}}, "partial": bool}`. Write and dangerous tools refuse
a fan-out: a change names one appliance.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import re
from collections.abc import Callable, Sequence
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

from pydantic import Field

from stutters_mcp_core.errors import InvalidRequest

if TYPE_CHECKING:
    from stutters_mcp_core.settings import BaseServerSettings

F = TypeVar("F", bound=Callable[..., Any])

ARGUMENT = "instance"  # the tool argument ("target" is already a parameter of several tools)
TARGET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Settings a named instance never inherits from the default instance: where to connect, as whom, and how
# the TLS connection is checked. Every SecretStr field and a product's `per_instance_fields` are added.
NOT_INHERITED = frozenset(
    {"url", "username", "password", "token", "verify_ssl", "ca_bundle"}
    | {"read_only", "two_person", "change_window", "max_blast_radius"}  # per-instance write rules (C15)
    | {"require_backup_hours"}  # per-instance backup-age rail (C73)
)

current_target: ContextVar[str | None] = ContextVar("stutters_target", default=None)

log = logging.getLogger(__name__)

FANOUT_ALL = "*"
DEFAULT_FANOUT_CONCURRENCY = 4
# A fan-out read's result (C74); each tool's output schema is widened to accept it (fanout_output_schema).
FANOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "instances": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {"result": {}, "error": {"type": "string"}},
            },
        },
        "partial": {"type": "boolean"},
    },
    "required": ["instances", "partial"],
}


def parse_target_names(spec: str | None, default: str) -> list[str]:
    """[default, *named] from `<P>_INSTANCES`."""
    names = [default]
    for raw in re.split(r"[,\s]+", spec or ""):
        name = raw.strip().lower()
        if not name:
            continue
        if not TARGET_NAME_RE.match(name):
            raise ValueError(
                f"instance name {raw!r}: use lowercase letters, digits and _ (start with a letter)"
            )
        if name in names:
            raise ValueError(f"instance {name!r} is listed twice (the default instance is {default!r})")
        names.append(name)
    return names


def target_settings(base: BaseServerSettings, name: str) -> BaseServerSettings:
    """Settings for named instance `name`: the same class read with prefix `<P><NAME>_`. Fields the instance
    doesn't set are inherited from the default instance, except URL, credentials, TLS checking, every
    secret, and the product's `per_instance_fields`; the merged result is validated as a whole."""
    from stutters_mcp_core.settings import _is_secret

    cls = type(base)
    prefix = str(cls.model_config.get("env_prefix", ""))
    sub_prefix = f"{prefix}{name.upper()}_"
    config = {**cls.model_config, "env_prefix": sub_prefix}
    sub_cls = type(f"{cls.__name__}_{name}", (cls,), {"model_config": config})
    env_file = cls.model_config.get("env_file")
    try:
        own = sub_cls(_env_file=env_file).model_fields_set  # what the instance sets itself
    except ValueError as e:
        raise ValueError(f"instance {name!r} (variables {sub_prefix}*): {e}") from None
    keep = NOT_INHERITED | frozenset(getattr(cls, "per_instance_fields", frozenset()))
    inherit = {
        field: getattr(base, field)
        for field, info in cls.model_fields.items()
        if field not in own
        and field not in keep
        and not _is_secret(info.annotation)
        and not field.startswith("mcp_")
        and field not in ("instances", "default_instance")
        and field in base.model_fields_set
    }
    try:
        merged = sub_cls(_env_file=env_file, **inherit, instances=None)
    except ValueError as e:
        raise ValueError(f"instance {name!r} (variables {sub_prefix}*): {e}") from None
    result: BaseServerSettings = merged
    return result


def target_name() -> str | None:
    """The target selected for the current tool call (None = the default target)."""
    return current_target.get()


class TargetState(dict[str, Any]):
    """Lifespan state whose per-appliance entries ("client", "settings", "specs") follow the instance
    selected for the current call. Only `state[key]`, `state.get(key)` and `key in state` are routed;
    iteration, `items()`, `copy()` and `{**state}` see the shared entries only, so tools must look
    entries up by key (get_client / get_settings)."""

    PER_TARGET = ("client", "settings", "specs")

    def __init__(self, default: str, per_target: dict[str, dict[str, Any]], shared: dict[str, Any]) -> None:
        super().__init__(shared)
        self.default = default
        self.per_target = per_target

    def _bucket(self) -> dict[str, Any]:
        return self.per_target[current_target.get() or self.default]

    def __getitem__(self, key: str) -> Any:
        if key in self.PER_TARGET:
            return self._bucket()[key]
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.PER_TARGET:
            return self._bucket().get(key, default)
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str) and key in self.PER_TARGET:
            return key in self._bucket()
        return super().__contains__(key)

    @property
    def names(self) -> list[str]:
        return list(self.per_target)


def fanout_names(value: Any, names: Sequence[str]) -> list[str] | None:
    """The instances an `instance` value fans out to: all of `names` for '*', the listed ones (in order, each
    once) for 'a,b'. None when the value names one instance or is unset. Unknown names: InvalidRequest."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text == FANOUT_ALL:
        return list(names)
    if "," not in text:
        return None
    picked: list[str] = []
    for raw in text.split(","):
        name = raw.strip()
        if not name:
            continue
        if name not in names:
            raise InvalidRequest(f"Unknown instance {name!r}; configured: {', '.join(names)}")
        if name not in picked:
            picked.append(name)
    if not picked:
        raise InvalidRequest(f"instance={value!r} names no instance; configured: {', '.join(names)}")
    return picked


def fanout_legs(value: Any, names: Sequence[str]) -> int:
    """How many appliance calls an `instance` value makes (1 unless it fans out); never raises."""
    try:
        picked = fanout_names(value, names)
    except InvalidRequest:
        return 1
    return len(picked) if picked else 1


def fanout_output_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """A tool's output schema widened so a fan-out result also validates (clients check structured content
    against it). `$defs` and FastMCP's result wrapping (`{"result": <value>}` for str, list... returns, whose
    `properties.result` clients read) stay at the root."""
    if schema is None:
        return None
    if schema.get("x-fastmcp-wrap-result"):
        out = {k: v for k, v in schema.items() if k != "required"}
        out["anyOf"] = [{"required": schema.get("required", ["result"])}, FANOUT_SCHEMA]
        return out
    inner = {k: v for k, v in schema.items() if k != "$defs"}
    out = {"type": "object", "anyOf": [inner, FANOUT_SCHEMA]}
    if "$defs" in schema:
        out["$defs"] = schema["$defs"]
    return out


def _leg_value(value: Any) -> tuple[Any, str]:
    """(structured value, text) of one instance's result: a ToolResult's structured content (or its text,
    e.g. a markdown table), else the JSON-able return value."""
    from fastmcp.tools import ToolResult
    from pydantic_core import to_jsonable_python

    if isinstance(value, ToolResult):
        text = "\n".join(str(getattr(c, "text", "")) for c in value.content)
        return (value.structured_content if value.structured_content is not None else text), text
    data = to_jsonable_python(value)
    return data, data if isinstance(data, str) else json.dumps(data, separators=(",", ":"), default=str)


def _leg_error(e: Exception, scrub: Callable[[str], str] | None) -> str:
    from fastmcp.exceptions import ToolError

    from stutters_mcp_core.errors import UpstreamError

    message = str(e) or type(e).__name__
    if not isinstance(e, ToolError | InvalidRequest | UpstreamError | ValueError):
        log.warning("fan-out leg failed with %s", type(e).__name__)
        message = f"{type(e).__name__}: {message}"
    return scrub(message) if scrub is not None else message


def fanout_result(legs: dict[str, tuple[bool, Any, str]]) -> Any:
    """The fan-out ToolResult from {instance: (ok, value or error, text)}."""
    from fastmcp.tools import ToolResult

    instances: dict[str, dict[str, Any]] = {}
    blocks: list[str] = []
    for name, (ok, value, text) in legs.items():
        instances[name] = {"result": value} if ok else {"error": value}
        blocks.append(f"## instance {name}\n{text}" if ok else f"## instance {name}: error\n{value}")
    failed = [n for n, (ok, _, _) in legs.items() if not ok]
    # TODO(C30 #82): when Page.note lands, also say which instances failed in the page note.
    payload = {"instances": instances, "partial": bool(failed)}
    header = (
        f"partial: {len(failed)} of {len(legs)} instances failed ({', '.join(failed)})"
        if failed
        else f"all {len(legs)} instances answered"
    )
    return ToolResult(content="\n\n".join([header, *blocks]), structured_content=payload)


def with_target_argument(  # noqa: UP047
    fn: F,
    names: list[str],
    default: str,
    *,
    fanout: bool = False,
    concurrency: int = DEFAULT_FANOUT_CONCURRENCY,
    scrub: Callable[[str], str] | None = None,
) -> F:
    """Wrap a tool so it takes an optional `instance` (one of `names`) and runs with it selected. Sync tools
    stay sync (FastMCP runs them in a worker thread, which inherits the context variable).

    `fanout` (read tools): `instance` also takes '*' or 'a,b'; the tool then runs once per instance (at most
    `concurrency` at a time; a sync tool runs them one after another in its worker thread) and returns
    `fanout_result`. An instance that fails is reported with its error (`scrub` masks secrets in it) and
    `partial: true`; the others still answer. Without `fanout`, such a value is refused (InvalidRequest)."""
    import typing

    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception as e:  # noqa: BLE001 - e.g. TYPE_CHECKING-only names under postponed annotations
        raise ValueError(
            f"{fn.__name__}: can't resolve its type hints for the multi-instance wrapper ({e}); "
            "import the annotated types at module level"
        ) from None
    sig = inspect.signature(fn)
    sig = sig.replace(
        parameters=[p.replace(annotation=hints.get(n, p.annotation)) for n, p in sig.parameters.items()],
        return_annotation=hints.get("return", sig.return_annotation),
    )
    if ARGUMENT in sig.parameters:
        raise ValueError(
            f"{fn.__name__} already has an {ARGUMENT!r} parameter; it can't be multi-instance wrapped"
        )
    annotation: Any
    if fanout:
        one = "|".join(re.escape(n) for n in names)
        annotation = Annotated[
            str | None,
            Field(
                description=f"Which appliance instance to use: {', '.join(names)} (default: {default}). "
                f"'*' (every instance) or a comma list such as '{names[0]},{names[1]}' runs this read on "
                "each and returns {instances: {name: {result} or {error}}, partial}.",
                pattern=rf"^\s*(\*|({one})(\s*,\s*({one}))*\s*,?)\s*$",
                json_schema_extra={"x-fanout": True},
            ),
        ]
    else:
        choices = Literal[tuple(names)]  # type: ignore[valid-type]
        annotation = Annotated[
            choices | None,
            Field(description=f"Which appliance instance to use: {', '.join(names)} (default: {default})."),
        ]

    def select(kwargs: dict[str, Any]) -> tuple[list[str] | None, Token[str | None] | None]:
        """Pops `instance`: (the instances to fan out to, None), or (None, the token selecting the one)."""
        target = kwargs.pop(ARGUMENT, None)
        picked = fanout_names(target, names)
        if picked is not None:
            if not fanout:
                raise InvalidRequest(
                    f"{fn.__name__} changes the appliance, so it runs on one instance: instance={target!r} "
                    f"(several instances) is only for read tools. Name one of: {', '.join(names)}."
                )
            return picked, None
        if isinstance(target, str):
            target = target.strip()
        if target is not None and target not in names:  # FastMCP validates the schema first; direct calls
            raise InvalidRequest(f"Unknown instance {target!r}; configured: {', '.join(names)}")
        return None, current_target.set(target or default)

    wrapper: Callable[..., Any]
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            picked, token = select(kwargs)
            if picked is None:
                assert token is not None
                try:
                    return await fn(*args, **kwargs)
                finally:
                    current_target.reset(token)
            gate = asyncio.Semaphore(max(1, concurrency))

            async def leg(name: str) -> tuple[bool, Any, str]:
                async with gate:
                    token = current_target.set(name)
                    try:
                        value, text = _leg_value(await fn(*args, **dict(kwargs)))
                    except Exception as e:  # noqa: BLE001 - one instance failing doesn't fail the others
                        message = _leg_error(e, scrub)
                        return False, message, message
                    finally:
                        current_target.reset(token)
                    return True, value, text

            results = await asyncio.gather(*(leg(n) for n in picked))
            return fanout_result(dict(zip(picked, results, strict=True)))

        wrapper = async_wrapper
    else:

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            picked, token = select(kwargs)
            if picked is None:
                assert token is not None
                try:
                    return fn(*args, **kwargs)
                finally:
                    current_target.reset(token)
            legs: dict[str, tuple[bool, Any, str]] = {}
            for name in picked:  # a sync tool's legs run in turn, in its worker thread
                token = current_target.set(name)
                try:
                    value, text = _leg_value(fn(*args, **dict(kwargs)))
                    legs[name] = (True, value, text)
                except Exception as e:  # noqa: BLE001
                    message = _leg_error(e, scrub)
                    legs[name] = (False, message, message)
                finally:
                    current_target.reset(token)
            return fanout_result(legs)

        wrapper = sync_wrapper

    param = inspect.Parameter(ARGUMENT, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=annotation)
    params = [*sig.parameters.values(), param]
    wrapper.__signature__ = sig.replace(parameters=params)  # type: ignore[union-attr]
    wrapper.__annotations__ = {**hints, ARGUMENT: annotation}
    return wrapper  # type: ignore[return-value]


def with_task_guard(fn: F, *, server_has_auth: Callable[[], bool], scrub: Callable[[str], str]) -> F:  # noqa: UP047
    """Wrap a tool that runs as an MCP background task (C12). The task worker doesn't run the FastMCP
    middleware chain, so this does two things the middleware would:
    - fail closed when the caller's credentials didn't reach the worker (e.g. the token expired while the task
      was queued): never fall back to the stdio identity `local`;
    - scrub secrets out of the error message returned through tasks/get."""
    import typing

    from fastmcp.exceptions import ToolError

    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{fn.__name__}: can't resolve its type hints for the task wrapper ({e})") from None
    sig = inspect.signature(fn)
    sig = sig.replace(
        parameters=[p.replace(annotation=hints.get(n, p.annotation)) for n, p in sig.parameters.items()],
        return_annotation=hints.get("return", sig.return_annotation),
    )

    @functools.wraps(fn)
    async def task_wrapper(*args: Any, **kwargs: Any) -> Any:
        from fastmcp.server.dependencies import get_access_token
        from fastmcp_tasks.context import get_task_context

        if get_task_context() is not None and server_has_auth() and get_access_token() is None:
            raise ToolError(
                "The caller's credentials were no longer valid when this background task ran (they expire "
                "while a task waits). Nothing was done; call the tool again."
            )
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            message = str(e)
            clean = scrub(message)
            if clean != message or not isinstance(e, ToolError):
                raise ToolError(clean) from None
            raise

    task_wrapper.__signature__ = sig  # type: ignore[attr-defined]
    task_wrapper.__annotations__ = dict(hints)
    return task_wrapper  # type: ignore[return-value]
