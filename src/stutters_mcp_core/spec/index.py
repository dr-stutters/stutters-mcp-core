"""A compact, searchable index over one or more OpenAPI 3.x / Swagger 2.0 documents.

Raw vendor specs are large (FMC's is tens of MB). The index keeps, per operation: method, path,
summary, tags, parameters and request/response schema names; per schema: a one-line summary of each
field (type, enum, required). It serialises to JSON for the disk cache.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from stutters_mcp_core.errors import InvalidRequest

INDEX_FORMAT = 3
EXAMPLE_CHARS = 3000  # per example, serialised
DETAIL_CHARS = 2000  # full descriptions
METHODS = ("get", "post", "put", "patch", "delete")


@dataclass
class Param:
    name: str
    location: str  # query | path | header
    required: bool = False
    summary: str = ""
    description: str = ""  # full text (summary is cut to one short line)


@dataclass
class Operation:
    source: str  # which spec/surface this came from
    method: str
    path: str
    operation_id: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)
    request: str | None = None
    response: str | None = None
    deprecated: bool = False
    description: str = ""  # full operation description
    examples: dict[str, str] = field(default_factory=dict)  # {"request"|"response": JSON text}


@dataclass
class SpecIndex:
    version: str
    operations: list[Operation] = field(default_factory=list)
    # name -> {"source", "description", "required", "fields": {field: summary}, "enum"?}
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- search -------------------------------------------------------------------------

    def search(self, query: str, *, limit: int = 20, source: str | None = None) -> list[dict[str, Any]]:
        terms = [t for t in re.split(r"[\s/._{}-]+", query.lower()) if t]
        if not terms:
            return []

        def score(primary: str, *rest: str) -> int:
            hay = " ".join((primary, *rest)).lower()
            if not all(t in hay for t in terms):
                return 0
            return sum(3 if t in primary.lower() else 1 for t in terms)

        hits: list[tuple[int, dict[str, Any]]] = []
        for op in self.operations:
            if source and op.source != source:
                continue
            if s := score(op.path, op.operation_id, op.summary, " ".join(op.tags)):
                hit: dict[str, Any] = {
                    "kind": "operation",
                    "source": op.source,
                    "name": f"{op.method} {op.path}",
                    "summary": op.summary[:160],
                }
                if op.deprecated:
                    hit["deprecated"] = True
                    s -= 2
                hits.append((s + 1, hit))
        for name, sch in self.schemas.items():
            if source and sch.get("source") != source:
                continue
            if s := score(name, sch.get("description", "")):
                hits.append(
                    (
                        s,
                        {
                            "kind": "schema",
                            "source": sch.get("source"),
                            "name": name,
                            "summary": sch.get("description", "")[:160],
                        },
                    )
                )
        hits.sort(key=lambda h: -h[0])
        return [h[1] for h in hits[:limit]]

    # --- definitions --------------------------------------------------------------------

    def operations_at(self, path: str, source: str | None = None) -> list[Operation]:
        want = _norm_path(path)
        return [
            op
            for op in self.operations
            if _norm_path(op.path) == want and (source is None or op.source == source)
        ]

    def definition(self, name: str, *, source: str | None = None, examples: bool = False) -> dict[str, Any]:
        """A schema by name, or every operation at a path ('/<resource>/{id}' or with a method
        prefix 'GET /api/...'). With `examples`, operations also carry the spec's request/response
        examples and the full (untruncated) operation and parameter descriptions."""
        name = name.strip()
        if name in self.schemas:
            return {"schema": name, **self.schemas[name]}
        method = None
        m = re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+(\S+)$", name, re.IGNORECASE)
        if m:
            method, name = m.group(1).upper(), m.group(2)
        if "/" in name:
            ops = [op for op in self.operations_at(name, source) if method is None or op.method == method]
            if not ops:
                near = self.search(name, limit=5, source=source)
                hint = "; ".join(str(h["name"]) for h in near)
                raise InvalidRequest(
                    f"No operations at {name}." + (f" Close matches: {hint}" if hint else "")
                )
            out: dict[str, Any] = {"path": ops[0].path, "operations": []}
            for op in ops:
                d = asdict(op)
                if examples:
                    if not d["examples"]:
                        d["examples"] = "none in the spec"
                else:
                    d.pop("examples", None)
                    d.pop("description", None)
                    for prm in d["params"]:
                        prm.pop("description", None)
                if not d.get("description"):
                    d.pop("description", None)
                if op.request and op.request in self.schemas:
                    d["request_fields"] = self.schemas[op.request].get("fields", {})
                    d["request_required"] = self.schemas[op.request].get("required", [])
                out["operations"].append(d)
            return out
        lower = {k.lower(): k for k in self.schemas}
        if name.lower() in lower:
            key = lower[name.lower()]
            return {"schema": key, **self.schemas[key]}
        close = difflib.get_close_matches(name, list(self.schemas), n=5, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise InvalidRequest(
            f"Unknown schema or path {name!r}.{hint} Use the search_spec tool to find names."
        )

    # --- serialisation ------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "format": INDEX_FORMAT,
            "version": self.version,
            "operations": [asdict(o) for o in self.operations],
            "schemas": self.schemas,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SpecIndex:
        if d.get("format") != INDEX_FORMAT:
            raise ValueError("index format mismatch")
        ops = []
        for o in d["operations"]:
            o = dict(o)
            o["params"] = [Param(**p) for p in o.get("params", [])]
            ops.append(Operation(**o))
        return cls(version=d["version"], operations=ops, schemas=d["schemas"])

    def merge(self, other: SpecIndex) -> None:
        self.operations.extend(other.operations)
        for k, v in other.schemas.items():
            self.schemas.setdefault(k, v)


def _norm_path(p: str) -> str:
    p = "/" + p.strip().strip("/")
    return re.sub(r"\{[^}]+\}", "{}", p).lower()


# --- building ----------------------------------------------------------------------------


def _ref_name(node: Any) -> str | None:
    if isinstance(node, dict):
        if "$ref" in node:
            return str(node["$ref"]).rsplit("/", 1)[-1]
        for k in ("allOf", "oneOf", "anyOf"):
            for sub in node.get(k, []) or []:
                if r := _ref_name(sub):
                    return r
        if node.get("type") == "array":
            return _ref_name(node.get("items"))
    return None


def _summarise_prop(p: Any) -> str:
    if not isinstance(p, dict):
        return "any"
    parts: list[str] = []
    if "enum" in p:
        parts.append("enum: " + "|".join(str(c) for c in p["enum"] if c not in ("", None))[:300])
    elif ref := _ref_name(p):
        parts.append(f"list of {ref}" if p.get("type") == "array" else ref)
    else:
        t = p.get("type", "object" if "properties" in p else "any")
        if t == "array":
            items = p.get("items", {})
            if isinstance(items, dict) and "enum" in items:
                t = "list of enum: " + "|".join(str(c) for c in items["enum"])[:200]
            else:
                t = f"list of {items.get('type', 'any') if isinstance(items, dict) else 'any'}"
        elif t == "object" and isinstance(p.get("properties"), dict):
            t = "object {" + ", ".join(list(p["properties"])[:12]) + "}"
        parts.append(str(t) + (f" ({p['format']})" if "format" in p else ""))
    for k in ("minimum", "maximum", "maxLength", "pattern", "default"):
        if k in p:
            parts.append(f"{k} {p[k]}")
    if p.get("nullable"):
        parts.append("nullable")
    if p.get("readOnly"):
        parts.append("read-only")
    if d := p.get("description"):
        parts.append(str(d).split("\n")[0][:120])
    return ", ".join(parts)


def _flatten_schema(
    s: dict[str, Any], schemas: dict[str, Any], depth: int = 0
) -> tuple[dict[str, Any], list[str]]:
    """(properties, required) including allOf parents, one level of $ref indirection."""
    props: dict[str, Any] = dict(s.get("properties", {}) or {})
    required: list[str] = list(s.get("required", []) or [])
    if depth < 4:
        for sub in s.get("allOf", []) or []:
            if isinstance(sub, dict):
                target = schemas.get(_ref_name(sub) or "", sub) if "$ref" in sub else sub
                if isinstance(target, dict):
                    p, r = _flatten_schema(target, schemas, depth + 1)
                    props = {**p, **props}
                    required += r
    return props, required


def _summarise_schema(s: dict[str, Any], schemas: dict[str, Any], source: str) -> dict[str, Any]:
    out: dict[str, Any] = {"source": source}
    if d := s.get("description"):
        out["description"] = str(d)[:300]
    props, required = _flatten_schema(s, schemas)
    if required:
        out["required"] = sorted(set(required))
    if props:
        out["fields"] = {k: _summarise_prop(v) for k, v in props.items()}
    if "enum" in s:
        out["enum"] = s["enum"]
    if s.get("type") == "array" and (ref := _ref_name(s)):
        out["items"] = ref
    return out


def _example_text(value: Any) -> str:
    import json

    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), default=str)
    return text if len(text) <= EXAMPLE_CHARS else text[:EXAMPLE_CHARS] + "...(truncated)"


def _media_example(media: Any) -> Any:
    """First example in an OpenAPI 3 media-type object (example, examples{}.value, schema.example)."""
    if not isinstance(media, dict):
        return None
    if "example" in media:
        return media["example"]
    exs = media.get("examples")
    if isinstance(exs, dict):
        for ex in exs.values():
            if isinstance(ex, dict) and "value" in ex:
                return ex["value"]
    schema = media.get("schema")
    if isinstance(schema, dict) and "example" in schema:
        return schema["example"]
    return None


def _detail(text: Any) -> str:
    t = str(text or "").strip()
    return t if len(t) <= DETAIL_CHARS else t[:DETAIL_CHARS] + "..."


def build_index(spec: dict[str, Any], *, source: str = "default", path_prefix: str = "") -> SpecIndex:
    """Index an OpenAPI 3.x or Swagger 2.0 document. `path_prefix` is prepended to every path
    (Swagger 2 `basePath` is applied automatically; OpenAPI 3 server paths are not)."""
    swagger2 = str(spec.get("swagger", "")).startswith("2")
    schemas: dict[str, Any] = (
        spec.get("definitions", {}) if swagger2 else spec.get("components", {}).get("schemas", {})
    ) or {}
    base = path_prefix.rstrip("/")
    if swagger2 and spec.get("basePath", "/") not in ("", "/"):
        base += "/" + str(spec["basePath"]).strip("/")

    ops: list[Operation] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters", []) or []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params: list[Param] = []
            body_schema: Any = None
            op_examples: dict[str, str] = {}
            for p in [*shared, *(op.get("parameters", []) or [])]:
                if not isinstance(p, dict):
                    continue
                if "$ref" in p:
                    p = (
                        spec.get("parameters", {}).get(_ref_name(p) or "", {})
                        if swagger2
                        else (spec.get("components", {}).get("parameters", {}).get(_ref_name(p) or "", {}))
                    )
                if p.get("in") == "body":
                    body_schema = p.get("schema")
                    for ex in (
                        p.get("x-examples"),
                        (body_schema or {}).get("example") if isinstance(body_schema, dict) else None,
                    ):
                        if ex is not None and "request" not in op_examples:
                            if isinstance(ex, dict) and "application/json" in ex:
                                ex = ex["application/json"]
                            op_examples["request"] = _example_text(ex)
                    continue
                if p.get("in") in ("query", "path", "header") and p.get("name"):
                    schema = p.get("schema", p)
                    params.append(
                        Param(
                            p["name"],
                            p["in"],
                            bool(p.get("required")),
                            _summarise_prop(
                                {**schema, "description": p.get("description", schema.get("description", ""))}
                            ),
                            _detail(p.get("description", schema.get("description", ""))),
                        )
                    )
            if not swagger2:
                content = (op.get("requestBody") or {}).get("content", {}) or {}
                for ctype in ("application/json", *content):
                    if ctype in content:
                        body_schema = content[ctype].get("schema")
                        ex = _media_example(content[ctype])
                        if ex is not None:
                            op_examples["request"] = _example_text(ex)
                        break
            resp_schema = None
            for code, r in (op.get("responses") or {}).items():
                if not str(code).startswith("2") or not isinstance(r, dict):
                    continue
                if swagger2:
                    resp_schema = r.get("schema")
                    rex = (
                        (r.get("examples") or {}).get("application/json")
                        if isinstance(r.get("examples"), dict)
                        else None
                    )
                    if rex is None and isinstance(resp_schema, dict):
                        rex = resp_schema.get("example")
                    if rex is not None:
                        op_examples["response"] = _example_text(rex)
                else:
                    rc = r.get("content", {}) or {}
                    for ctype in ("application/json", *rc):
                        if ctype in rc:
                            resp_schema = rc[ctype].get("schema")
                            ex = _media_example(rc[ctype])
                            if ex is not None:
                                op_examples["response"] = _example_text(ex)
                            break
                break
            # Inline request bodies get a synthetic schema so their fields are still describable.
            req_name = _ref_name(body_schema)
            if req_name is None and isinstance(body_schema, dict) and body_schema.get("properties"):
                req_name = f"{op.get('operationId') or method + path}:request"
                schemas = {**schemas, req_name: body_schema}
            ops.append(
                Operation(
                    source=source,
                    method=method.upper(),
                    path=base + path,
                    operation_id=str(op.get("operationId", "")),
                    summary=str(op.get("summary") or op.get("description") or "")
                    .strip()
                    .split("\n")[0][:300],
                    tags=[str(t) for t in op.get("tags", []) or []],
                    params=params,
                    request=req_name,
                    response=_ref_name(resp_schema),
                    deprecated=bool(op.get("deprecated")),
                    description=_detail(op.get("description")),
                    examples=op_examples,
                )
            )

    summarised = {
        name: _summarise_schema(s, schemas, source) for name, s in schemas.items() if isinstance(s, dict)
    }
    version = str((spec.get("info") or {}).get("version", "unknown"))
    return SpecIndex(version=version, operations=ops, schemas=summarised)
