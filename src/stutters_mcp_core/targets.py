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
# Settings a named target never inherits from the default target: where to connect and as whom.
NOT_INHERITED = frozenset({"url", "username", "password", "token"})

current_target: ContextVar[str | None] = ContextVar("stutters_target", default=None)


def parse_target_names(spec: str | None, default: str) -> list[str]:
    """[default, *named] from `<P>_TARGETS`."""
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
    """Settings for named target `name`: the same class read with prefix `<P><NAME>_`, inheriting every
    unset field except url and credentials from the default target."""
    cls = type(base)
    prefix = str(cls.model_config.get("env_prefix", ""))
    sub_prefix = f"{prefix}{name.upper()}_"
    sub_cls = type(
        f"{cls.__name__}_{name}", (cls,), {"model_config": {**cls.model_config, "env_prefix": sub_prefix}}
    )
    try:
        sub = sub_cls(_env_file=cls.model_config.get("env_file"))
    except ValueError as e:
        raise ValueError(f"instance {name!r} (variables {sub_prefix}*): {e}") from None
    inherit = {
        field: getattr(base, field)
        for field in cls.model_fields
        if field not in sub.model_fields_set
        and field not in NOT_INHERITED
        and not field.startswith("mcp_")
        and field not in ("instances", "default_instance")
    }
    result: BaseServerSettings = sub.model_copy(update={**inherit, "instances": None})
    return result


def target_name() -> str | None:
    """The target selected for the current tool call (None = the default target)."""
    return current_target.get()


class TargetState(dict[str, Any]):
    """Lifespan state whose per-appliance entries ("client", "settings", "specs") follow the target
    selected for the current call."""

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
        if key in self.PER_TARGET:
            return key in self._bucket()
        return super().__contains__(key)

    @property
    def names(self) -> list[str]:
        return list(self.per_target)


def with_target_argument(fn: F, names: list[str], default: str) -> F:  # noqa: UP047
    """Wrap a tool so it takes an optional `target` (one of `names`) and runs with that target selected."""
    import typing

    hints = typing.get_type_hints(fn, include_extras=True)
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

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        target = kwargs.pop(ARGUMENT, None)
        if target is not None and target not in names:
            raise InvalidRequest(f"Unknown instance {target!r}; configured: {', '.join(names)}")
        token = current_target.set(target or default)
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result
        finally:
            current_target.reset(token)

    param = inspect.Parameter(ARGUMENT, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=annotation)
    params = list(sig.parameters.values())
    # keep **kwargs last if present
    if params and params[-1].kind is inspect.Parameter.VAR_KEYWORD:
        params.insert(len(params) - 1, param)
    else:
        params.append(param)
    wrapper.__signature__ = sig.replace(parameters=params)  # type: ignore[attr-defined]
    wrapper.__annotations__ = {**hints, ARGUMENT: annotation}
    return wrapper  # type: ignore[return-value]
