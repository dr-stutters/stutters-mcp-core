"""Poll an asynchronous job on the product until it finishes.

    result = await poll_until(
        lambda: client.get(f"/dna/intent/api/v1/task/{task_id}"),
        done=lambda r: r["response"].get("endTime") is not None,
        failed=lambda r: r["response"].get("isError"),
        describe=lambda r: r["response"].get("progress", ""),
        ctx=ctx, timeout=300,
    )

Used for Catalyst Center tasks/executions, FMC deployments and registrations, ISE tasks and CML
convergence. Sends MCP progress notifications while waiting when a Context is supplied.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Context

from stutters_mcp_core.errors import TaskFailed, UpstreamTimeout

log = logging.getLogger(__name__)


async def poll_until[T](
    fetch: Callable[[], Awaitable[T]],
    *,
    done: Callable[[T], bool],
    failed: Callable[[T], Any] | None = None,
    describe: Callable[[T], str] | None = None,
    timeout: float = 600.0,  # noqa: ASYNC109 - overall polling deadline
    interval: float = 2.0,
    backoff: float = 1.5,
    max_interval: float = 15.0,
    ctx: Context | None = None,
    what: str = "task",
) -> T:
    """Call `fetch` until `done(result)`. Raises TaskFailed if `failed(result)` is truthy (its value,
    if a string, becomes the message) and UpstreamTimeout after `timeout` seconds."""
    start = time.monotonic()
    wait = interval
    polls = 0
    while True:
        result = await fetch()
        polls += 1
        if failed is not None and (reason := failed(result)):
            detail = reason if isinstance(reason, str) else (describe(result) if describe else "")
            raise TaskFailed(f"{what} failed" + (f": {detail}" if detail else ""))
        if done(result):
            return result
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            last = describe(result) if describe else ""
            raise UpstreamTimeout(
                f"{what} still running after {int(elapsed)}s"
                + (f" ({last})" if last else "")
                + ". It may still finish; check its status again later."
            )
        if ctx is not None:
            msg = describe(result) if describe else f"waiting for {what}"
            try:
                await ctx.report_progress(progress=elapsed, total=timeout, message=msg)
            except Exception:  # progress is best-effort; never fail the poll over it
                log.debug("progress notification failed", exc_info=True)
        await asyncio.sleep(min(wait, max(0.0, timeout - elapsed)))
        wait = min(max_interval, wait * backoff)
