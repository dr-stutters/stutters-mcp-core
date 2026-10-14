import asyncio
import time
from typing import Any

import httpx
import pytest
import respx

from stutters_mcp_core.errors import (
    AuthError,
    NotFound,
    RateLimited,
    Unreachable,
    UpstreamError,
    ValidationFailed,
)
from stutters_mcp_core.http import (
    ApiClient,
    BasicAuth,
    LoginTokenAuth,
    NoPaging,
    OffsetLimit,
    PageNumber,
    RateLimiter,
    Token,
    default_error_detail,
)

BASE = "https://api.test"


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake(delay: float, *a: Any) -> None:
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("stutters_mcp_core.http.client.asyncio.sleep", fake)
    return slept


@respx.mock
async def test_basic_auth_header_and_json() -> None:
    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200, json={"ok": 1}))
    async with ApiClient(BASE, auth=BasicAuth("a", "b")) as c:
        assert await c.get("/x") == {"ok": 1}
    assert route.calls.last.request.headers["Authorization"] == "Basic YTpi"


@respx.mock
async def test_login_token_single_flight_and_relogin_on_401() -> None:
    logins = 0

    async def login(client: ApiClient) -> Token:
        nonlocal logins
        logins += 1
        await asyncio.sleep(0)
        return Token({"X-Token": f"t{logins}"})

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tok = request.headers["X-Token"]
        seen.append(tok)
        if tok == "t1" and len(seen) > 3:
            return httpx.Response(401)
        return httpx.Response(200, json={"tok": tok})

    respx.get(f"{BASE}/r").mock(side_effect=handler)
    async with ApiClient(BASE, auth=LoginTokenAuth(login)) as c:
        results = await asyncio.gather(*(c.get("/r") for _ in range(3)))
        assert logins == 1 and all(r == {"tok": "t1"} for r in results)
        assert await c.get("/r") == {"tok": "t2"}  # 401 -> one re-login -> retried
    assert logins == 2


@respx.mock
async def test_login_token_refresh_hook_used_before_login() -> None:
    async def login(client: ApiClient) -> Token:
        return Token({"X-Token": "fresh"}, Token.expiring_in(-1), {"refresh": "r1"})

    async def refresh(client: ApiClient, old: Token) -> Token | None:
        assert old.data["refresh"] == "r1"
        return Token({"X-Token": "refreshed"}, Token.expiring_in(3600))

    respx.get(f"{BASE}/r").mock(side_effect=lambda r: httpx.Response(200, json=r.headers["X-Token"]))
    async with ApiClient(BASE, auth=LoginTokenAuth(login, refresh=refresh, margin=0)) as c:
        assert await c.get("/r") == "fresh"
        assert await c.get("/r") == "refreshed"  # expired -> refresh, not login


@respx.mock
async def test_429_retried_with_retry_after_even_for_post(fast_sleep: list[float]) -> None:
    route = respx.post(f"{BASE}/p").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(201, json={"id": 1})]
    )
    async with ApiClient(BASE, retries=2) as c:
        assert await c.post("/p", {"a": 1}) == {"id": 1}
    assert route.call_count == 2 and fast_sleep == [7.0]


@respx.mock
async def test_503_not_retried_for_post_but_retried_for_get(fast_sleep: list[float]) -> None:
    respx.post(f"{BASE}/p").mock(return_value=httpx.Response(503))
    get = respx.get(f"{BASE}/g").mock(side_effect=[httpx.Response(503), httpx.Response(200, json=[])])
    async with ApiClient(BASE, retries=2, product="FMC") as c:
        with pytest.raises(UpstreamError, match="FMC returned HTTP 503"):
            await c.post("/p")
        assert await c.get("/g") == []
    assert get.call_count == 2


@respx.mock
async def test_rate_limited_after_retries(fast_sleep: list[float]) -> None:
    respx.get(f"{BASE}/g").mock(return_value=httpx.Response(429))
    async with ApiClient(BASE, retries=1) as c:
        with pytest.raises(RateLimited):
            await c.get("/g")


@pytest.mark.parametrize(
    ("status", "body", "exc", "text"),
    [
        (401, {"message": "bad creds"}, AuthError, "ISE_USERNAME"),
        (404, {}, NotFound, "Not found"),
        (400, {"ERSResponse": {"messages": [{"title": "name exists"}]}}, ValidationFailed, "rejected"),
        (422, {"error": {"messages": [{"description": "bad field"}]}}, ValidationFailed, "bad field"),
        (302, "", UpstreamError, "may be disabled"),
    ],
)
@respx.mock
async def test_error_mapping(status: int, body: Any, exc: type[Exception], text: str) -> None:
    headers = {"Location": "/admin/"} if status == 302 else {}
    kwargs: dict[str, Any] = {"json": body} if body != "" else {}
    respx.get(f"{BASE}/e").mock(return_value=httpx.Response(status, headers=headers, **kwargs))
    async with ApiClient(BASE, product="ISE", env_prefix="ISE_") as c:
        with pytest.raises(exc, match=text):
            await c.get("/e")


async def test_tls_failure_hint() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed")

    transport = httpx.MockTransport(boom)
    async with ApiClient(BASE, env_prefix="CML_", retries=0, transport=transport) as c:
        with pytest.raises(Unreachable, match="CML_VERIFY_SSL=false"):
            await c.get("/x")


async def test_refuses_absolute_url_on_other_host() -> None:
    async with ApiClient(BASE) as c:
        with pytest.raises(ValueError, match="outside"):
            await c.get("https://evil.test/x")


def test_default_error_detail_shapes() -> None:
    assert default_error_detail({"response": {"detail": "x"}}) == "x"
    assert default_error_detail([{"message": "a"}, {"message": "b"}]) == "a | b"
    assert default_error_detail(None) is None


@respx.mock
async def test_offset_limit_pager_multiple_pages() -> None:
    data = list(range(5))

    def handler(request: httpx.Request) -> httpx.Response:
        off, lim = int(request.url.params["offset"]), int(request.url.params["limit"])
        return httpx.Response(200, json={"items": data[off : off + lim], "paging": {"count": 5}})

    respx.get(f"{BASE}/l").mock(side_effect=handler)
    async with ApiClient(BASE) as c:
        page = await OffsetLimit(items="items", total="paging.count", max_page=2).fetch_page(
            c, "/l", limit=4, offset=1
        )
    assert page.items == [1, 2, 3, 4] and page.total == 5 and page.next_offset is None


@respx.mock
async def test_offset_limit_one_based_without_total() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        off = int(request.url.params["offset"])
        return httpx.Response(200, json={"response": [off] if off <= 3 else []})

    respx.get(f"{BASE}/l").mock(side_effect=handler)
    async with ApiClient(BASE) as c:
        page = await OffsetLimit(items="response", one_based=True, max_page=1).fetch_page(c, "/l", limit=10)
    assert page.items == [1, 2, 3] and page.total == 3


@respx.mock
async def test_offset_limit_without_total_flags_more() -> None:
    respx.get(f"{BASE}/l").mock(side_effect=lambda r: httpx.Response(200, json={"items": [1, 2]}))
    async with ApiClient(BASE) as c:
        page = await OffsetLimit(items="items", max_page=2).fetch_page(c, "/l", limit=2, offset=4)
    assert page.total is None and page.more and page.next_offset == 6 and not page.complete


@respx.mock
async def test_fetch_all_reports_partial() -> None:
    from stutters_mcp_core.http import fetch_all

    respx.get(f"{BASE}/l").mock(
        side_effect=lambda r: httpx.Response(200, json={"items": [0] * 3, "total": 99})
    )
    async with ApiClient(BASE) as c:
        page = await fetch_all(OffsetLimit(items="items", total="total", max_page=3), c, "/l", cap=6)
    assert len(page.items) == 6 and not page.complete


@respx.mock
async def test_page_number_pager_unaligned_offset() -> None:
    data = list(range(7))

    def handler(request: httpx.Request) -> httpx.Response:
        p, s = int(request.url.params["page"]), int(request.url.params["size"])
        chunk = data[(p - 1) * s : p * s]
        return httpx.Response(200, json={"SearchResult": {"total": 7, "resources": chunk}})

    respx.get(f"{BASE}/e").mock(side_effect=handler)
    pager = PageNumber(items="SearchResult.resources", total="SearchResult.total", max_size=3)
    async with ApiClient(BASE) as c:
        page = await pager.fetch_page(c, "/e", limit=3, offset=2)
    assert page.items == [2, 3, 4] and page.total == 7 and page.next_offset == 5


@respx.mock
async def test_no_paging_slices() -> None:
    respx.get(f"{BASE}/all").mock(return_value=httpx.Response(200, json=list(range(10))))
    async with ApiClient(BASE) as c:
        page = await NoPaging().fetch_page(c, "/all", limit=3, offset=8)
    assert page.items == [8, 9] and page.total == 10


async def test_rate_limiter_paces_requests() -> None:
    limiter = RateLimiter(per_minute=600, max_concurrency=2)  # 10/s, burst capacity 100
    limiter._tokens = 0  # start empty to observe pacing
    start = time.monotonic()
    for _ in range(3):
        async with limiter.slot():
            pass
    assert time.monotonic() - start >= 0.25


@respx.mock
async def test_ok_status_returns_instead_of_raising() -> None:
    respx.get(f"{BASE}/mnt").mock(return_value=httpx.Response(500, text="<x>34110</x>"))
    async with ApiClient(BASE, retries=0) as c:
        resp = await c.get("/mnt", ok_status={500}, expect="response")
    assert resp.status_code == 500 and "34110" in resp.text
