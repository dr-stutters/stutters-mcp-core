"""scripts/bundle_core.py (ADR-005): vendoring core into a server package and the drift checks."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bundle_core.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bundle_core", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bundle_core"] = mod
    spec.loader.exec_module(mod)
    return mod


bc = _load()


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def set_version(text: str, version: str) -> str:
    return re.sub(r'^version = "[^"]*"', f'version = "{version}"', text, count=1, flags=re.MULTILINE)


def release(core: Path, version: str) -> None:
    """Commit a new core version and tag it v<version>."""
    pyproject = core / "pyproject.toml"
    pyproject.write_text(set_version(pyproject.read_text(), version))
    git(core, "commit", "-q", "-am", version)
    git(core, "tag", f"v{version}")


@pytest.fixture
def core(tmp_path: Path) -> Path:
    """A throwaway core repo: this checkout's package, pyproject, LICENSE and script, tagged v9.9.9."""
    repo = tmp_path / "core"
    shutil.copytree(
        ROOT / "src" / "stutters_mcp_core",
        repo / "src" / "stutters_mcp_core",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (repo / "pyproject.toml").write_text(set_version((ROOT / "pyproject.toml").read_text(), "9.9.9"))
    shutil.copy(ROOT / "LICENSE", repo / "LICENSE")
    (repo / "scripts").mkdir()
    shutil.copy(SCRIPT, repo / "scripts" / "bundle_core.py")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "core")
    git(repo, "tag", "v9.9.9")
    return repo


def make_server(tmp_path: Path, core: Path, extras: str = "") -> Path:
    server = tmp_path / "server"
    (server / "src" / "demo_mcp").mkdir(parents=True)
    (server / "src" / "demo_mcp" / "__init__.py").write_text("from demo_mcp._core import __version__\n")
    (server / "scripts").mkdir()
    shutil.copy(SCRIPT, server / "scripts" / "bundle_core.py")
    commit = git(core, "rev-parse", "v9.9.9")
    (server / "pyproject.toml").write_text(f"""[project]
name = "demo-mcp-server"
version = "1.0.0"
dependencies = [
    "defusedxml>=0.7.1",
    # >>> bundled stutters-mcp-core
    # <<< bundled stutters-mcp-core
]

[tool.bundle-core]
tag = "v9.9.9"
commit = "{commit}"
{extras}

[tool.uv.build-backend]
module-name = "demo_mcp"
""")
    return server


def run(server: Path, core: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(server / "scripts" / "bundle_core.py"), *args, "--source", str(core)],
        cwd=server,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_vendor_rewrites_imports_and_merges_dependencies(tmp_path: Path, core: Path) -> None:
    server = make_server(tmp_path, core)
    code, out = run(server, core, "vendor")
    assert code == 0, out
    dest = server / "src" / "demo_mcp" / "_core"
    meta = json.loads((dest / "BUNDLE.json").read_text())
    assert meta["tag"] == "v9.9.9" and meta["commit"] == git(core, "rev-parse", "HEAD")
    assert (dest / "LICENSE").is_file() and (dest / "py.typed").is_file()
    for py in dest.rglob("*.py"):
        text = py.read_text()
        assert "from stutters_mcp_core" not in text and "import stutters_mcp_core" not in text, py
    init = (dest / "__init__.py").read_text()
    assert f'__version__ = "{meta["version"]}"' in init and "importlib.metadata" not in init
    pyproject = (server / "pyproject.toml").read_text()
    assert '    "fastmcp>=4.1,<5",\n' in pyproject and '"defusedxml>=0.7.1"' in pyproject
    assert "fastmcp-tasks" not in pyproject
    # the bundle imports as demo_mcp._core and reports core's version
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import demo_mcp, demo_mcp._core.server as s; print(demo_mcp.__version__, s.__name__)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(server / "src")},
    )
    assert probe.stdout.split() == [meta["version"], "demo_mcp._core.server"], probe.stderr
    assert run(server, core, "check")[0] == 0


def test_extras_add_their_dependencies(tmp_path: Path, core: Path) -> None:
    server = make_server(tmp_path, core, extras='extras = ["tasks"]')
    assert run(server, core, "vendor")[0] == 0
    assert '"fastmcp-tasks>=4.1,<5",' in (server / "pyproject.toml").read_text()
    server2 = make_server(tmp_path / "x", core, extras='extras = ["nope"]')
    code, out = run(server2, core, "vendor")
    assert code == 2 and "no extra 'nope'" in out


def test_check_fails_when_not_vendored_or_edited(tmp_path: Path, core: Path) -> None:
    server = make_server(tmp_path, core)
    code, out = run(server, core, "check", "--offline")
    assert code == 1 and "not vendored" in out
    run(server, core, "vendor")
    (server / "src" / "demo_mcp" / "_core" / "errors.py").write_text("# hand edit\n")
    code, out = run(server, core, "check", "--offline")
    assert code == 1 and "edited by hand" in out


def test_check_fails_when_the_pin_is_bumped(tmp_path: Path, core: Path) -> None:
    server = make_server(tmp_path, core)
    run(server, core, "vendor")
    release(core, "9.9.10")
    pyproject = server / "pyproject.toml"
    pyproject.write_text(bc.set_pin(pyproject.read_text(), "v9.9.10", git(core, "rev-parse", "v9.9.10")))
    code, out = run(server, core, "check", "--offline")
    assert code == 1 and "drift: pinned core v9.9.10" in out and "bundled copy is v9.9.9" in out
    code, out = run(server, core, "check")
    assert code == 1 and "stale" in out  # the dependency block still names v9.9.9
    assert run(server, core, "vendor")[0] == 0
    assert run(server, core, "check")[0] == 0


def test_moved_tag_is_refused(tmp_path: Path, core: Path) -> None:
    server = make_server(tmp_path, core)
    run(server, core, "vendor")
    (core / "README.md").write_text("moved\n")
    git(core, "add", "-A")
    git(core, "commit", "-q", "-m", "moved")
    git(core, "tag", "-f", "v9.9.9")
    code, out = run(server, core, "check")
    assert code == 1 and "tag moved" in out
    code, out = run(server, core, "vendor")
    assert code == 2 and "tag moved" in out


def test_update_moves_the_pin(tmp_path: Path, core: Path) -> None:
    server = make_server(tmp_path, core)
    run(server, core, "vendor")
    release(core, "9.10.0")
    code, out = run(server, core, "update", "--tag", "v9.10.0")
    assert code == 0, out
    text = (server / "pyproject.toml").read_text()
    assert 'tag = "v9.10.0"' in text and git(core, "rev-parse", "v9.10.0") in text
    assert "# >>> bundled stutters-mcp-core v9.10.0" in text
    assert run(server, core, "check")[0] == 0


def test_stale_script_copy_fails_check(tmp_path: Path, core: Path) -> None:
    server = make_server(tmp_path, core)
    run(server, core, "vendor")
    with (server / "scripts" / "bundle_core.py").open("a") as f:
        f.write("# local change\n")
    code, out = run(server, core, "check")
    assert code == 1 and "differs from the copy in core v9.9.9" in out


def test_git_dependency_left_behind_fails_check(tmp_path: Path, core: Path) -> None:
    server = make_server(tmp_path, core)
    run(server, core, "vendor")
    pyproject = server / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace('"defusedxml>=0.7.1",', '"stutters-mcp-core", "defusedxml>=0.7.1",')
    )
    code, out = run(server, core, "check", "--offline")
    assert code == 1 and "still depends on stutters-mcp-core" in out


def _wheel(server: Path, path: Path, requires: list[str], drop: str | None = None) -> None:
    dest = server / "src" / "demo_mcp" / "_core"
    with zipfile.ZipFile(path, "w") as zf:
        for f in dest.rglob("*"):
            rel = f.relative_to(server / "src").as_posix()
            if f.is_file() and rel != f"demo_mcp/_core/{drop}":
                zf.write(f, rel)
        meta = "Metadata-Version: 2.4\nName: demo-mcp-server\n" + "".join(
            f"Requires-Dist: {r}\n" for r in requires
        )
        zf.writestr("demo_mcp_server-1.0.0.dist-info/METADATA", meta)


def test_dist_check(tmp_path: Path, core: Path) -> None:
    server = make_server(tmp_path, core)
    run(server, core, "vendor")
    deps = json.loads((server / "src" / "demo_mcp" / "_core" / "BUNDLE.json").read_text())["dependencies"]
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    _wheel(server, good / "demo_mcp_server-1.0.0-py3-none-any.whl", ["defusedxml>=0.7.1", *deps])
    assert run(server, core, "check", "--offline", "--dist", str(good))[0] == 0
    _wheel(
        server,
        bad / "demo_mcp_server-1.0.0-py3-none-any.whl",
        ["stutters-mcp-core", deps[0]],
        drop="errors.py",
    )
    code, out = run(server, core, "check", "--offline", "--dist", str(bad))
    assert code == 1
    assert "files differ from the pinned tag" in out and "lacks core dependencies" in out
    assert "still requires stutters-mcp-core" in out


def test_rewrite_rejects_imports_it_cannot_rename() -> None:
    assert (
        bc.rewrite_imports("from stutters_mcp_core.x import y\n", "p._core", "a.py")
        == "from p._core.x import y\n"
    )
    out = bc.rewrite_imports("import stutters_mcp_core.x as y\n", "p._core", "a.py")
    assert out == "import p._core.x as y\n"
    with pytest.raises(bc.BundleError, match="bare"):
        bc.rewrite_imports("import stutters_mcp_core.x\n", "p._core", "a.py")
    with pytest.raises(bc.BundleError, match="used as a name"):
        bc.rewrite_imports("x = stutters_mcp_core\n", "p._core", "a.py")


def test_requirement_keys_ignore_spacing_and_quote_style() -> None:
    assert bc._req_key('tomli>=2; python_version < "3.11"') == bc._req_key(
        "tomli >= 2 ; python_version<'3.11'"
    )


def test_tag_must_match_the_core_version(tmp_path: Path, core: Path) -> None:
    server = make_server(tmp_path, core)
    git(core, "commit", "-q", "--allow-empty", "-m", "forgot the bump")
    git(core, "tag", "v9.11.0")  # pyproject still says 9.9.9
    code, out = run(server, core, "update", "--tag", "v9.11.0")
    assert code == 2 and "tag v9.11.0 holds core version 9.9.9" in out
