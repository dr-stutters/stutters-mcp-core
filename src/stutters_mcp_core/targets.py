"""Several appliances per server, selected per call (roadmap G10, ADR-004).

    ISE_URL=https://ise-lab ...          the default instance (ISE_DEFAULT_INSTANCE names it; "default")
    ISE_INSTANCES=prod,dr                more instances, each with its own prefix:
    ISE_PROD_URL=https://ise-prod  ISE_PROD_USERNAME=...  ISE_PROD_PASSWORD_FILE=...

With more than one instance, every tool gets an optional `instance` argument; tools call `get_client(ctx)` as
before and receive the selected target's client. With one target nothing changes.
"""

from __future__ import annotations

import functools
import inspect
import re
from collections.abc import Callable
from contextvars import ContextVar
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
NOT_INHERITED = frozenset({"url", "username", "password", "token", "verify_ssl", "ca_bundle"})

current_target: ContextVar[str | None] = ContextVar("stutters_target", default=None)


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


def with_target_argument(fn: F, names: list[str], default: str) -> F:  # noqa: UP047
    """Wrap a tool so it takes an optional `instance` (one of `names`) and runs with it selected. Sync tools
    stay sync (FastMCP runs them in a worker thread, which inherits the context variable)."""
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
    choices = Literal[tuple(names)]  # type: ignore[valid-type]
    annotation = Annotated[
        choices | None,  # type: ignore[valid-type]
        Field(description=f"Which appliance instance to use: {', '.join(names)} (default: {default})."),
    ]

    def select(kwargs: dict[str, Any]) -> Any:
        target = kwargs.pop(ARGUMENT, None)
        if target is not None and target not in names:  # FastMCP validates the enum first; direct calls
            raise InvalidRequest(f"Unknown instance {target!r}; configured: {', '.join(names)}")
        return current_target.set(target or default)

    wrapper: Callable[..., Any]
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            token = select(kwargs)
            try:
                return await fn(*args, **kwargs)
            finally:
                current_target.reset(token)

        wrapper = async_wrapper
    else:

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            token = select(kwargs)
            try:
                return fn(*args, **kwargs)
            finally:
                current_target.reset(token)

        wrapper = sync_wrapper

    param = inspect.Parameter(ARGUMENT, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=annotation)
    params = [*sig.parameters.values(), param]
    wrapper.__signature__ = sig.replace(parameters=params)  # type: ignore[union-attr]
    wrapper.__annotations__ = {**hints, ARGUMENT: annotation}
    return wrapper  # type: ignore[return-value]
