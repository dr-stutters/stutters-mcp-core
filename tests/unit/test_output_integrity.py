"""C30: partial collections say so, markdown tables name dropped columns, big objects stay valid JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from stutters_mcp_core.formatting import (
    list_result,
    max_chars,
    object_result,
    set_max_chars,
    shrink_object,
    to_markdown,
)
from stutters_mcp_core.http import (
    PARTIAL_NOTE,
    ApiClient,
    OffsetLimit,
    Page,
    PartialCollection,
    fetch_all,
    page_of,
)
from stutters_mcp_core.testing.partial_lint import discarded_complete, main

BASE = "https://api.example.com"


def _collection(n: int) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        off, lim = int(request.url.params["offset"]), int(request.url.params["limit"])
        items = [{"id": f"obj-{i}", "name": f"n{i}"} for i in range(off, min(n, off + lim))]
        return httpx.Response(200, json={"items": items, "paging": {"count": n}})

    return handler


@respx.mock
async def test_fetch_all_over_cap_notes_partial() -> None:
    respx.get(f"{BASE}/objs").mock(side_effect=_collection(620))
    pager = OffsetLimit(items="items", total="paging.count", max_page=100)
    async with ApiClient(BASE) as c:
        page = await fetch_all(pager, c, "/objs", cap=500)
    assert len(page.items) == 500 and not page.complete and page.partial
    assert page.note and "620 items" in page.note and "cap 500" in page.note
    meta = page.meta()
    assert meta["partial"] is True and meta["note"] == page.note


@respx.mock
async def test_fetch_all_under_cap_has_no_note() -> None:
    respx.get(f"{BASE}/objs").mock(side_effect=_collection(40))
    async with ApiClient(BASE) as c:
        page = await fetch_all(OffsetLimit(items="items", total="paging.count", max_page=100), c, "/objs")
    assert page.complete and not page.partial and page.note is None
    assert "partial" not in page.meta() and "note" not in page.meta()


@respx.mock
async def test_fetch_all_raise_on_partial_for_safety_checks() -> None:
    respx.get(f"{BASE}/assignments").mock(side_effect=_collection(260))
    async with ApiClient(BASE) as c:
        with pytest.raises(PartialCollection, match="260 items"):
            await fetch_all(
                OffsetLimit(items="items", total="paging.count", max_page=100),
                c,
                "/assignments",
                cap=200,
                on_partial="raise",
            )


@respx.mock
async def test_partial_page_flows_through_page_of_and_list_result() -> None:
    respx.get(f"{BASE}/objs").mock(side_effect=_collection(620))
    async with ApiClient(BASE) as c:
        page = await fetch_all(
            OffsetLimit(items="items", total="paging.count", max_page=100), c, "/objs", cap=500
        )
    matches = [r for r in page.items if r["name"].endswith("7")]
    out = page_of(matches, limit=10, source=page)
    assert out.partial and out.note == page.note
    payload: Any = list_result(out).structured_content
    assert payload["partial"] is True and "620 items" in payload["note"]
    md = list_result(out, fmt="markdown").content[0].text  # type: ignore[union-attr]
    assert "partial: True" in md and "620 items" in md


def test_page_of_note_and_partial_defaults() -> None:
    assert page_of([1, 2], limit=5).meta() == {"offset": 0, "returned": 2, "total": 2}
    assert page_of([1], limit=5, partial=True).note == PARTIAL_NOTE
    assert page_of([1], limit=5, note="Stale rows hidden.").meta()["note"] == "Stale rows hidden."
    # A complete source page adds nothing.
    assert not page_of([1], limit=5, source=Page([1], 0, 1)).partial


def test_list_result_keeps_page_note_and_truncation_note() -> None:
    old = max_chars()
    set_max_chars(2000)
    try:
        rows = [{"id": i, "blob": "x" * 200} for i in range(40)]
        page = Page(rows, 0, 40, partial=True, note="Partial results: cap hit.")
        payload: Any = list_result(page).structured_content
        assert "truncated" in payload["note"] and payload["note"].endswith("Partial results: cap hit.")
        assert payload["partial"] is True
    finally:
        set_max_chars(old)


def test_to_markdown_names_hidden_columns_and_keeps_id() -> None:
    row = {f"k{i}": i for i in range(10)} | {"id": "abc"}  # 11 keys, id last
    md = to_markdown([row])
    header = md.splitlines()[0]
    assert "| id |" in header and "k9" not in header and header.count("|") == 11
    assert md.splitlines()[-1] == "(+1 columns not shown: k9; use response_format=json)"


def test_to_markdown_no_note_when_everything_fits_or_columns_given() -> None:
    assert "not shown" not in to_markdown([{"a": 1, "b": 2}])
    assert "not shown" not in to_markdown([{f"k{i}": i for i in range(15)}], columns=["k0", "k1"])


def test_list_result_markdown_carries_column_note() -> None:
    rows = [{f"field{i}": i for i in range(13)}]
    text = list_result(Page(rows, 0, 1), fmt="markdown").content[0].text  # type: ignore[union-attr]
    assert "(+3 columns not shown: field10, field11, field12; use response_format=json)" in text


def _big_object() -> dict[str, Any]:
    return {
        "id": "policy-1",
        "name": "mcp-big",
        "description": "lab policy",
        "rules": [
            {"id": f"r{i}", "name": f"rule-{i}", "action": "ALLOW", "pad": "y" * 40} for i in range(500)
        ],
        "settings": {"logging": True, "members": [f"member-{i}" for i in range(100)]},
    }


def test_object_result_truncates_structurally() -> None:
    obj = _big_object()
    assert len(json.dumps(obj)) > 40_000
    result = object_result(obj, fetch_hints={"rules": "fmc_list_access_rules(policy=...)"})
    text = result.content[0].text  # type: ignore[union-attr]
    assert len(text) <= max_chars()
    data = json.loads(text)  # valid JSON
    assert result.structured_content == data
    assert data["id"] == "policy-1" and data["name"] == "mcp-big" and data["description"] == "lab policy"
    assert "rules" not in data and data["settings"]["members"]  # the largest list goes, the rest stays
    [entry] = data["truncated_fields"]
    assert entry["field"] == "rules" and entry["items"] == 500
    assert entry["fetch"] == "fmc_list_access_rules(policy=...)"
    assert "truncated_fields" in data["note"]


def test_object_result_drops_several_lists_largest_first() -> None:
    old = max_chars()
    set_max_chars(1000)
    try:
        obj = {"id": 1, "a": ["x" * 50] * 40, "b": {"c": ["y" * 50] * 20}, "d": ["z" * 10] * 5}
        data: Any = object_result(obj).structured_content
        assert [f["field"] for f in data["truncated_fields"]] == ["a", "b.c"]
        assert data["d"] == ["z" * 10] * 5 and data["b"] == {}
    finally:
        set_max_chars(old)


def test_object_result_unsplittable_value_still_valid_json() -> None:
    result = object_result({"id": "x", "name": "n" * 50_000})
    data = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert data["truncated"] is True and len(result.content[0].text) <= max_chars()  # type: ignore[union-attr]


def test_object_result_wrapper_stays_in_budget_with_escaping() -> None:
    # A quote-heavy value grows when JSON-escaped inside the wrapper's `text`.
    quoted = json.dumps({f"k{i}": "v" for i in range(5000)})
    result = object_result({"id": "x", "name": quoted}, partial=True)
    text = result.content[0].text  # type: ignore[union-attr]
    data = json.loads(text)
    assert len(text) <= max_chars() and data["truncated"] is True and data["partial"] is True
    assert data["note"].startswith(PARTIAL_NOTE)


def test_object_result_shrink_keeps_own_note() -> None:
    obj = {"id": 1, "note": "Own note.", "rules": ["x" * 100] * 500}
    data: Any = object_result(obj, partial=True).structured_content
    assert data["note"].startswith(f"Own note. {PARTIAL_NOTE} Object too large")
    assert data["note"].count(PARTIAL_NOTE) == 1


def test_object_result_top_level_list_is_trimmed_valid() -> None:
    result = object_result([{"id": i, "pad": "p" * 100} for i in range(1000)])
    data: Any = result.structured_content
    assert data["total"] == 1000 and 0 < data["returned"] < 1000 and len(data["results"]) == data["returned"]


def test_object_result_markdown_after_shrink() -> None:
    text = object_result(_big_object(), fmt="markdown").content[0].text  # type: ignore[union-attr]
    assert "**truncated_fields**" in text and "**name**: mcp-big" in text


def test_object_result_partial_source_note() -> None:
    src = Page([1], 0, 9, partial=True, note="Partial results: /x has 9 items.")
    data: Any = object_result({"count": 1}, source=src).structured_content
    assert data == {"count": 1, "partial": True, "note": "Partial results: /x has 9 items."}
    data = object_result({"count": 1, "note": "Own note."}, partial=True).structured_content
    assert data["note"] == f"Own note. {PARTIAL_NOTE}"
    assert object_result({"count": 1}).structured_content == {"count": 1}


def test_shrink_object_small_is_unchanged_but_framed() -> None:
    out = shrink_object({"id": 1, "x": [1, 2]})
    assert out["x"] == [1, 2] and out["truncated_fields"] == []


LINT_SOURCE = """
async def bad_items(app):
    page = await fetch_all(app.pager, app.api, "/a")
    return [r["id"] for r in page.items]

async def bad_inline(app):
    return len((await app.all("/b")).items)

async def good_checked(app):
    page = await fetch_all(app.pager, app.api, "/c")
    if not page.complete:
        pass
    return page.items

async def good_handed_on(app):
    page = await fetch_all(app.pager, app.api, "/d")
    return list_result(page_of(page.items, limit=5, source=page))

async def good_raise(app):
    page = await fetch_all(app.pager, app.api, "/e", on_partial="raise")
    return page.items

async def good_marked(app):
    page = await fetch_all(app.pager, app.api, "/f")  # partial-ok: cap is far above the product max
    return page.items

def builtin_all(xs):
    ok = all(xs)
    return ok
"""


def test_partial_lint_finds_discarded_complete(tmp_path: Path) -> None:
    src = tmp_path / "pkg" / "tools.py"
    src.parent.mkdir()
    src.write_text(LINT_SOURCE)
    vendored = tmp_path / "pkg" / "_core" / "paging.py"
    vendored.parent.mkdir()
    vendored.write_text("async def x(p):\n    page = await fetch_all(p)\n    return page.items\n")
    findings = discarded_complete(tmp_path / "pkg", calls=("fetch_all", "all"))
    assert sorted(f.function for f in findings) == ["bad_inline", "bad_items"]
    assert all(str(src) in str(f) for f in findings)
    assert [f.function for f in discarded_complete(src)] == ["bad_items"]  # default: fetch_all only


def test_partial_lint_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "t.py").write_text(LINT_SOURCE)
    assert main([str(tmp_path), "--calls", "fetch_all,all"]) == 1
    assert "2 call site(s)" in capsys.readouterr().out
    (tmp_path / "t.py").write_text("x = 1\n")
    assert main([str(tmp_path)]) == 0
