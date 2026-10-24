"""Offline demo mode: record a storyline's reads from a real appliance, replay them from a query-aware mock
(roadmap C64).

Demos fail when the lab is down. Record a storyline once while the lab is up, then run the server against
the mock: the production client is unchanged (the server's URL simply points at the mock), and
`<prefix>_status` shows `mode: mock` with the capture date.

    # 1. record: a reverse proxy in front of the appliance; point the server's URL at it, run the storyline
    python -m stutters_mcp_core.mock record --upstream https://manager.example --out routes.json \\
        --storyline "branch triage" --replace 10.61.=198.51.100. [--insecure] [--port 8080]
    #    (Ctrl-C writes routes.json; `check` lists anything that still looks like lab data or a secret)
    python -m stutters_mcp_core.mock check routes.json
    # 2. replay: answer the same requests offline
    python -m stutters_mcp_core.mock serve routes.json [--port 8080] [--name demo]

Recording forwards GETs and the login requests only (`--allow REGEX` widens that, e.g. for read-only POST
queries); every other write is refused with 403, so a storyline can't change the lab by accident. What is
stored: status, body and a few headers (content type, paging). Secret-named JSON keys and token/cookie
headers are masked, the upstream host name and every `--replace OLD=NEW` pair are substituted in paths,
queries, headers and bodies.

routes.json, version 2 (version 1, the flat `{"METHOD /path": route}` map of the examples' mock appliance,
still loads; query strings were ignored there):

    {"mock": 2, "storyline": "branch triage", "captured_at": "2026-10-24T09:00:00Z",
     "ignore_query": ["startTime", "endTime"],
     "routes": {"GET /dataservice/device": [
         {"query": {"site-id": "200"}, "status": 200, "json": {...}},
         {"status": 200, "json": {...}}]}}

A request matches a route when the route's query is a subset of the request's query (multi-valued keys
compare as lists); the route matching the most keys wins, a route without `query` is the fallback. Query
keys in `ignore_query` (time windows a tool computes from "now") are dropped on both sides. A route holds
`json` or `text` (+ `content_type`), `status` (default 200) and `headers`; `__NAME__` in a body becomes
the `--name` given to `serve`. Unmatched requests get a 404 that says the storyline never captured them.

The serving half is stdlib only: this file runs on its own in a bare `python:3.12-slim` container
(`python mock.py serve routes.json`). Recording needs httpx (any server image has it).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import signal
import sys
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

SCHEMA_VERSION = 2
WELL_KNOWN = "/.well-known/stutters-mock"  # answered by `serve`: what this mock replays
MOCK_HEADER = "X-Stutters-Mock"
# Login / token requests are forwarded and recorded (as stubs with masked tokens) so replay can log in.
DEFAULT_ALLOW = r"(?i)(auth|login|token|j_security_check|authenticate|logout)"
# Time-window parameters tools compute from the clock: never equal between capture and replay.
DEFAULT_IGNORE_QUERY = (
    "_",
    "startTime",
    "endTime",
    "starttime",
    "endtime",
    "timestamp",
    "fromTime",
    "toTime",
)
KEEP_HEADERS = ("content-type", "link", "x-total-count")
_TOKEN_HEADER = re.compile(r"(?i)(token|auth|session|csrf|xsrf|cookie|api-key)")
MASKED = "mock-masked"

Query = dict[str, list[str]]


def parse_query(qs: str) -> Query:
    return {k: v for k, v in parse_qs(qs, keep_blank_values=True).items()}


def _norm(q: Mapping[str, Any]) -> Query:
    return {str(k): [str(x) for x in v] if isinstance(v, list) else [str(v)] for k, v in q.items()}


def _flat(q: Query) -> dict[str, Any]:
    """Single values as strings, the rest as lists (how routes.json stores a query)."""
    return {k: v[0] if len(v) == 1 else v for k, v in sorted(q.items())}


@dataclass
class Route:
    status: int = 200
    json_body: Any = None
    text: str | None = None
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    query: Query | None = None  # None: matches any query (the fallback)
    has_json: bool = False

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Route:
        q = d.get("query")
        return cls(
            status=int(d.get("status", 200)),
            json_body=d.get("json"),
            text=None if d.get("text") is None else str(d["text"]),
            content_type=d.get("content_type"),
            headers={str(k): str(v) for k, v in (d.get("headers") or {}).items()},
            query=None if q is None else _norm(q),
            has_json="json" in d,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.query is not None:
            out["query"] = _flat(self.query)
        out["status"] = self.status
        if self.has_json:
            out["json"] = self.json_body
        elif self.text is not None:
            out["text"] = self.text
        if self.content_type:
            out["content_type"] = self.content_type
        if self.headers:
            out["headers"] = self.headers
        return out

    def score(self, query: Query) -> int | None:
        """How well this route fits the request query: None = no match, higher = more specific."""
        if self.query is None:
            return 0
        for k, want in self.query.items():
            got = query.get(k)
            if got is None or sorted(got) != sorted(want):
                return None
        return 1 + 2 * len(self.query) + (1 if set(self.query) == set(query) else 0)


@dataclass
class Routes:
    routes: dict[str, list[Route]] = field(default_factory=dict)  # "GET /path" -> candidates
    storyline: str | None = None
    captured_at: str | None = None
    ignore_query: tuple[str, ...] = DEFAULT_IGNORE_QUERY
    meta: dict[str, Any] = field(default_factory=dict)  # anything else in the file, kept on save

    @classmethod
    def load(cls, path: str | Path) -> Routes:
        return cls.from_dict(json.loads(Path(path).read_text()))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Routes:
        if "routes" not in data:  # version 1: {"METHOD /path": route}, query ignored
            return cls(
                routes={_key(k): [Route.from_dict(v)] for k, v in data.items() if isinstance(v, dict)},
                ignore_query=(),
            )
        if int(data.get("mock", SCHEMA_VERSION)) > SCHEMA_VERSION:
            raise ValueError(f"routes file version {data['mock']} is newer than this mock ({SCHEMA_VERSION})")
        routes: dict[str, list[Route]] = {}
        for k, v in (data.get("routes") or {}).items():
            items = v if isinstance(v, list) else [v]
            routes[_key(k)] = [Route.from_dict(r) for r in items]
        known = {"mock", "routes", "storyline", "captured_at", "ignore_query"}
        return cls(
            routes=routes,
            storyline=data.get("storyline"),
            captured_at=data.get("captured_at"),
            ignore_query=tuple(data.get("ignore_query", DEFAULT_IGNORE_QUERY)),
            meta={k: v for k, v in data.items() if k not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mock": SCHEMA_VERSION,
            "storyline": self.storyline,
            "captured_at": self.captured_at,
            **self.meta,
            "ignore_query": list(self.ignore_query),
            "routes": {k: [r.to_dict() for r in v] for k, v in sorted(self.routes.items())},
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=1, sort_keys=False) + "\n")

    def strip(self, query: Query) -> Query:
        return {k: v for k, v in query.items() if k not in self.ignore_query}

    def match(self, method: str, path: str, query: Query | None = None) -> Route | None:
        candidates = self.routes.get(f"{method.upper()} {_path(path)}") or []
        q = self.strip(query or {})
        best: tuple[int, Route] | None = None
        for r in candidates:
            s = r.score(q)
            if s is not None and (best is None or s > best[0]):
                best = (s, r)
        return best[1] if best else None

    def add(self, method: str, path: str, query: Query, route: Route) -> None:
        """Record one exchange: the same method, path and query again replaces the earlier capture."""
        route.query = self.strip(query)
        bucket = self.routes.setdefault(f"{method.upper()} {_path(path)}", [])
        bucket[:] = [r for r in bucket if r.query != route.query] + [route]

    def info(self) -> dict[str, Any]:
        return {
            "mode": "mock",
            "storyline": self.storyline,
            "captured_at": self.captured_at,
            "routes": sum(len(v) for v in self.routes.values()),
        }


def _path(p: str) -> str:
    return "/" + p.strip("/") if p.strip("/") else "/"


def _key(k: str) -> str:
    method, _, path = k.partition(" ")
    return f"{method.upper()} {_path(urlsplit(path).path)}"


# --- replay -----------------------------------------------------------------------------------


@dataclass
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes


class MockAppliance:
    """Answers requests from a Routes table. Use `serve()` for a process, `transport()` in tests."""

    def __init__(self, routes: Routes, name: str = "mock") -> None:
        self.routes = routes
        self.name = name

    def answer(self, method: str, target: str) -> Answer:
        parts = urlsplit(target)
        stamp = {MOCK_HEADER: f"captured={self.routes.captured_at or 'unknown'}"}
        if method.upper() == "GET" and _path(parts.path) == WELL_KNOWN:
            body = json.dumps({**self.routes.info(), "name": self.name}).encode()
            return Answer(200, {"Content-Type": "application/json", **stamp}, body)
        route = self.routes.match(method, parts.path, parse_query(parts.query))
        if route is None:
            msg = {
                "error": "not captured",
                "detail": f"{method.upper()} {parts.path} (query {parts.query or 'none'}) is not in this "
                f"mock's storyline {self.routes.storyline or ''!r}: record it, or ask what the demo covers",
            }
            return Answer(404, {"Content-Type": "application/json", **stamp}, json.dumps(msg).encode())
        if route.has_json:
            body = json.dumps(route.json_body).replace("__NAME__", self.name).encode()
            ctype = route.content_type or "application/json"
        else:
            body = (route.text or "").replace("__NAME__", self.name).encode()
            ctype = route.content_type or "application/xml"
        headers = {"Content-Type": ctype, **route.headers, **stamp}
        return Answer(route.status, headers, body)

    def transport(self) -> Any:
        """An httpx.MockTransport answering from these routes (for unit tests of a server)."""
        import httpx

        def handle(request: httpx.Request) -> httpx.Response:
            a = self.answer(request.method, str(request.url.raw_path, "ascii"))
            return httpx.Response(a.status, headers=a.headers, content=a.body)

        return httpx.MockTransport(handle)

    def server(self, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
        appliance = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                a = appliance.answer(self.command, self.path)
                self.send_response(a.status)
                for k, v in a.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(a.body)))
                self.end_headers()
                self.wfile.write(a.body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer

            def log_message(self, fmt: str, *args: object) -> None:
                sys.stderr.write(f"{appliance.name}: {fmt % args}\n")

        return ThreadingHTTPServer((host, port), Handler)


# --- recording --------------------------------------------------------------------------------


@dataclass
class Scrubber:
    """What a capture must not keep: lab names and addresses (`replace`), secret-named keys, tokens."""

    replace: dict[str, str] = field(default_factory=dict)

    def text(self, s: str) -> str:
        for old, new in self.replace.items():
            s = s.replace(old, new)
        return s

    def body(self, data: Any) -> Any:
        from stutters_mcp_core.formatting import mask_secrets

        return json.loads(self.text(json.dumps(mask_secrets(data))))

    def headers(self, headers: Iterable[tuple[str, str]]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in headers:
            lk = k.lower()
            if lk == "set-cookie":
                name = v.split("=", 1)[0].strip()
                out["Set-Cookie"] = f"{name}={MASKED}; Path=/"  # replay: one masked cookie is enough
            elif _TOKEN_HEADER.search(lk) and lk not in ("content-type",):
                out[k] = MASKED
            elif lk in KEEP_HEADERS:
                out[k] = self.text(v)
        return out


class Recorder:
    """Reverse proxy that records what passes through it into a Routes table."""

    def __init__(
        self,
        upstream: str,
        *,
        storyline: str,
        replace: Mapping[str, str] | None = None,
        allow: str = DEFAULT_ALLOW,
        ignore_query: Iterable[str] = DEFAULT_IGNORE_QUERY,
        verify: bool | str = True,
        transport: Any = None,
    ) -> None:
        import httpx

        self.upstream = upstream.rstrip("/")
        host = urlsplit(self.upstream).hostname or ""
        pairs = dict(replace or {})
        if host and host not in pairs:
            pairs[host] = "appliance.example"
        self.scrub = Scrubber(pairs)
        self.allow = re.compile(allow)
        self.routes = Routes(
            storyline=storyline,
            captured_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ignore_query=tuple(ignore_query),
        )
        self.lock = threading.Lock()
        self.refused: list[str] = []
        self.http = httpx.Client(verify=verify, timeout=120, follow_redirects=False, transport=transport)

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> Answer:
        parts = urlsplit(target)
        method = method.upper()
        if method != "GET" and not self.allow.search(parts.path):
            with self.lock:
                self.refused.append(f"{method} {parts.path}")
            msg = {"error": "refused by the recorder: a storyline capture only reads (see --allow)"}
            return Answer(403, {"Content-Type": "application/json"}, json.dumps(msg).encode())
        fwd = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length", "connection")}
        resp = self.http.request(method, self.upstream + target, headers=fwd, content=body or None)
        self._record(method, parts.path, parse_query(parts.query), resp)
        out_headers = {
            k: _cookie_for_http(v) if k.lower() == "set-cookie" else v
            for k, v in resp.headers.multi_items()
            if k.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding")
        }
        return Answer(resp.status_code, out_headers, resp.content)

    def _record(self, method: str, path: str, query: Query, resp: Any) -> None:
        ctype = resp.headers.get("content-type", "")
        route = Route(status=resp.status_code, headers=self.scrub.headers(resp.headers.multi_items()))
        route.headers.pop("content-type", None)
        route.headers.pop("Content-Type", None)
        if "json" in ctype:
            try:
                route.json_body, route.has_json = self.scrub.body(resp.json()), True
            except ValueError:
                route.text = self.scrub.text(resp.text)
        else:
            from stutters_mcp_core.secrets import scrub_text

            route.text = self.scrub.text(scrub_text(resp.text)) if resp.content else ""
            route.content_type = ctype.split(";")[0] or None
        q = {self.scrub.text(k): [self.scrub.text(x) for x in v] for k, v in query.items()}
        with self.lock:
            self.routes.add(method, self.scrub.text(path), q, route)

    def server(self, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def _proxy(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                try:
                    a = recorder.handle(self.command, self.path, dict(self.headers.items()), body)
                except Exception as e:  # noqa: BLE001 - report the upstream problem to the caller
                    a = Answer(502, {"Content-Type": "text/plain"}, f"recorder: {type(e).__name__}".encode())
                self.send_response(a.status)
                for k, v in a.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(a.body)))
                self.end_headers()
                self.wfile.write(a.body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _proxy

            def log_message(self, fmt: str, *args: object) -> None:
                sys.stderr.write(f"recorder: {fmt % args}\n")

        return ThreadingHTTPServer((host, port), Handler)


def _cookie_for_http(value: str) -> str:
    """The upstream's cookie made usable over the recorder's plain HTTP (no Secure, no Domain)."""
    attrs = [a for a in value.split(";") if a.strip().split("=", 1)[0].lower() not in ("secure", "domain")]
    return ";".join(attrs)


def findings(routes: Routes) -> list[str]:
    """Anything in the routes file that still looks like lab data or a secret (scrub_check's rules)."""
    from stutters_mcp_core.scrub_check import PLACEHOLDER, RULES

    text = json.dumps(routes.to_dict(), indent=1)
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        for name, rx in RULES:
            for m in rx.finditer(line):
                value = m.groupdict().get("value") or m.group(0)
                if not PLACEHOLDER.match(value) and value != MASKED:
                    out.append(f"line {no}: {name}: {value[:4]}...")
    return out


# --- command line -----------------------------------------------------------------------------


def _pairs(items: Iterable[str]) -> dict[str, str]:
    out = {}
    for item in items:
        old, sep, new = item.partition("=")
        if not sep or not old:
            raise SystemExit(f"--replace wants OLD=NEW, got {item!r}")
        out[old] = new
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stutters-mock", description="Record and replay a storyline's reads.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="replay a routes file over plain HTTP (test and demo use only)")
    s.add_argument("routes")
    # the examples' `appliance.py routes.json PORT NAME` form
    s.add_argument("legacy_port", nargs="?", type=int, help=argparse.SUPPRESS)
    s.add_argument("legacy_name", nargs="?", help=argparse.SUPPRESS)
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--name", default="mock", help="replaces __NAME__ in bodies")
    r = sub.add_parser("record", help="reverse proxy in front of an appliance that records a routes file")
    r.add_argument("--upstream", required=True, help="the appliance's base URL (https://...)")
    r.add_argument("--out", required=True)
    r.add_argument("--storyline", required=True)
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8080)
    r.add_argument("--replace", action="append", default=[], metavar="OLD=NEW", help="repeatable")
    r.add_argument("--allow", default=DEFAULT_ALLOW, help="regex of non-GET paths to forward (logins)")
    r.add_argument("--ignore-query", action="append", default=None, metavar="KEY")
    r.add_argument("--insecure", action="store_true", help="don't verify the appliance's certificate")
    r.add_argument("--ca-bundle")
    c = sub.add_parser("check", help="summarise a routes file and list what still looks like lab data")
    c.add_argument("routes")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        routes = Routes.load(args.routes)
        port = args.legacy_port or args.port
        name = args.legacy_name or args.name
        print(f"mock {name}: {routes.info()} on {args.host}:{port}", file=sys.stderr)
        MockAppliance(routes, name).server(args.host, port).serve_forever()
        return 0
    if args.cmd == "check":
        routes = Routes.load(args.routes)
        print(json.dumps(routes.info()))
        problems = findings(routes)
        for line in problems:
            print(line)
        return 1 if problems else 0

    verify: bool | str = args.ca_bundle or not args.insecure
    rec = Recorder(
        args.upstream,
        storyline=args.storyline,
        replace=_pairs(args.replace),
        allow=args.allow,
        ignore_query=DEFAULT_IGNORE_QUERY if args.ignore_query is None else args.ignore_query,
        verify=verify,
    )
    srv = rec.server(args.host, args.port)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown).start())
    print(f"recording {args.storyline!r} on http://{args.host}:{args.port} -> upstream", file=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()
    rec.routes.save(args.out)
    print(f"wrote {args.out}: {rec.routes.info()}", file=sys.stderr)
    if rec.refused:
        print(f"refused (not forwarded): {sorted(set(rec.refused))}", file=sys.stderr)
    for line in findings(rec.routes):
        print(f"check: {line}", file=sys.stderr)
    return 0


# --- status ------------------------------------------------------------------------------------


async def detect(url: str | None, *, wait_seconds: float = 2.0) -> dict[str, Any] | None:
    """`<prefix>_status`'s probe: the mock's info when `url` is a plain-HTTP mock, else None. Real
    appliances speak HTTPS, so they never get the extra request."""
    if not url or not url.lower().startswith("http://"):
        return None
    import httpx

    base = url.rstrip("/")
    try:
        parts = urlsplit(base)
        async with httpx.AsyncClient(timeout=wait_seconds, follow_redirects=False) as client:
            resp = await client.get(f"http://{parts.netloc.rpartition('@')[2]}{WELL_KNOWN}")
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("mode") != "mock":
        return None
    return {k: data.get(k) for k in ("mode", "storyline", "captured_at")}


def query_string(q: Mapping[str, Any]) -> str:
    """A routes.json query as a URL query string (handy in tests)."""
    return urlencode(_norm(q), doseq=True)


if __name__ == "__main__":
    sys.exit(main())
