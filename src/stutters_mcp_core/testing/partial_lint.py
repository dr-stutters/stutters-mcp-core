"""Find call sites that read a whole collection and throw away whether it was complete.

`fetch_all` (and a server's wrapper, such as `client.all(...)`) returns a `Page` whose `complete`,
`partial` and `note` say when the collection was larger than the cap. A call site that only keeps
`.items` turns a cut collection into an answer that looks whole. This lint lists those sites:

    from stutters_mcp_core.testing.partial_lint import discarded_complete

    def test_partial_collections_are_reported():          # a warning list, or assert == [] when clean
        for finding in discarded_complete("src/ise_mcp", calls=("fetch_all", "all")):
            warnings.warn(str(finding))

Or from a shell: `python -m stutters_mcp_core.testing.partial_lint src/ise_mcp --calls fetch_all,all`.

A call site is fine when the page itself reaches anything other than `.items` (`page.complete`,
`page.note`, `list_result(page)`, `page_of(..., source=page)`, `return page`), when it passes
`on_partial="raise"`, or when the line carries `# partial-ok: <reason>`.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CALLS = ("fetch_all",)
ALLOW_MARK = "# partial-ok"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    function: str
    call: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.function}: {self.call}(...) {self.reason}"


def _call_name(node: ast.AST, calls: frozenset[str]) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Name) and f.id in calls and f.id != "all":  # the builtin all() is not a fetch
        return f.id
    if isinstance(f, ast.Attribute) and f.attr in calls:
        return f.attr
    return None


def _raises_on_partial(call: ast.Call) -> bool:
    return any(
        k.arg == "on_partial" and isinstance(k.value, ast.Constant) and k.value.value == "raise"
        for k in call.keywords
    )


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _function_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    cur = parents.get(node)
    while cur is not None and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
        cur = parents.get(cur)
    return cur


def _only_items(name: str, scope: ast.AST, assign: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when every later read of `name` in `scope` is `name.items`."""
    for n in ast.walk(scope):
        if not (isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)):
            continue
        if n.lineno < getattr(assign, "lineno", 0):
            continue
        parent = parents.get(n)
        if not (isinstance(parent, ast.Attribute) and parent.attr == "items"):
            return False
    return True


def _scan(source: str, path: str, calls: frozenset[str]) -> Iterator[Finding]:
    tree = ast.parse(source, filename=path)
    lines = source.splitlines()
    parents = _parents(tree)
    for node in ast.walk(tree):
        name = _call_name(node, calls)
        if name is None:
            continue
        assert isinstance(node, ast.Call)
        if _raises_on_partial(node):
            continue
        func = _function_of(node, parents)
        fname = getattr(func, "name", "<module>")
        if ALLOW_MARK in lines[node.lineno - 1] or (
            node.end_lineno and any(ALLOW_MARK in ln for ln in lines[node.lineno - 1 : node.end_lineno])
        ):
            continue
        outer: ast.AST = node
        parent = parents.get(outer)
        if isinstance(parent, ast.Await):
            outer, parent = parent, parents.get(parent)
        if isinstance(parent, ast.Attribute):
            if parent.attr == "items":
                yield Finding(path, node.lineno, fname, name, "keeps only .items; .complete is discarded")
            continue
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                scope = func if func is not None else tree
                if _only_items(targets[0].id, scope, parent, parents):
                    yield Finding(
                        path,
                        node.lineno,
                        fname,
                        name,
                        f"result '{targets[0].id}' is only read as .items; .complete is never checked",
                    )
        # Anything else (an argument, a return, a comprehension source) hands the page on: fine.


def discarded_complete(
    roots: str | Path | Iterable[str | Path], *, calls: Iterable[str] = DEFAULT_CALLS
) -> list[Finding]:
    """Call sites under `roots` (files or directories of .py files) that discard `.complete`."""
    names = frozenset(calls)
    if isinstance(roots, (str, Path)):
        roots = [roots]
    files: list[Path] = []
    for r in roots:
        p = Path(r)
        files += sorted(p.rglob("*.py")) if p.is_dir() else [p]
    out: list[Finding] = []
    for f in files:
        if "_core" in f.parts:  # the vendored core copy defines fetch_all; it is not a call site
            continue
        out += _scan(f.read_text(encoding="utf-8"), str(f), names)
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    calls: Iterable[str] = DEFAULT_CALLS
    if "--calls" in args:
        i = args.index("--calls")
        calls = [c for c in args[i + 1].split(",") if c]
        del args[i : i + 2]
    findings = discarded_complete(args or ["src"], calls=calls)
    for finding in findings:
        print(finding)
    print(f"{len(findings)} call site(s) discard .complete")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
