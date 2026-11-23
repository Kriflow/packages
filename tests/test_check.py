"""The check tool on a throwaway repository: what it refuses and what it lets through."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import check  # noqa: E402

URL = "https://github.com/Kriflow/packages"
MANIFEST = """name = "echo"
version = "{version}"

[setup]
help_url = "{help}"
"""
ENTRY = """[[packages]]
name = "echo"
description = "Says it back."
git = "{url}.git"
subfolder = "echo"
ref = "echo-v{ref}"
contract = {contract}
permissions = []
"""


def write(
    root: Path,
    version: str = "1.0.0",
    ref: str = "1.0.0",
    contract: int = 1,
    help: str = f"{URL}/tree/main/echo#setup",
) -> None:
    (root / "echo").mkdir(exist_ok=True)
    (root / "echo/swarmify-connector.toml").write_text(MANIFEST.format(version=version, help=help))
    (root / "index.toml").write_text(ENTRY.format(url=URL, ref=ref, contract=contract))


def commit(root: Path, tag: str | None = None) -> None:
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    if tag:
        run("tag", tag)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write(tmp_path)
    (tmp_path / "echo/echo.py").write_text("print(1)\n")
    commit(tmp_path, "echo-v1.0.0")
    return tmp_path


def test_a_tagged_unchanged_package_passes(repo: Path) -> None:
    assert check.problems(repo) == []
    assert check.untagged(repo) == []


def test_a_shipped_change_without_a_bump_is_refused(repo: Path) -> None:
    (repo / "echo/echo.py").write_text("print(2)\n")
    assert check.problems(repo) == [
        "echo: files changed since echo-v1.0.0; raise its version above 1.0.0"
    ]
    write(repo, version="1.0.1", ref="1.0.1")
    assert check.problems(repo) == []
    commit(repo)
    assert check.untagged(repo) == ["echo-v1.0.1"]


def test_a_manifest_renamed_to_kriflow_is_a_release_and_still_found(repo: Path) -> None:
    """Kriflow reads `kriflow-connector.toml` or `swarmify-connector.toml`; renaming the file
    changes what a tag ships, so it needs a new version like any other shipped change."""
    (repo / "echo/swarmify-connector.toml").rename(repo / "echo/kriflow-connector.toml")
    assert check.problems(repo) == [
        "echo: files changed since echo-v1.0.0; raise its version above 1.0.0"
    ]
    (repo / "echo/kriflow-connector.toml").write_text(
        MANIFEST.format(version="1.0.1", help=f"{URL}/tree/main/echo#setup")
    )
    (repo / "index.toml").write_text(ENTRY.format(url=URL, ref="1.0.1", contract=1))
    assert check.problems(repo) == []
    commit(repo)
    assert check.untagged(repo) == ["echo-v1.0.1"]


def test_docs_and_tests_change_without_a_bump(repo: Path) -> None:
    (repo / "echo/README.md").write_text("# echo\n")
    (repo / "echo/test_echo.py").write_text("def test(): pass\n")
    assert check.problems(repo) == []


def test_an_index_entry_off_its_manifest_is_refused(repo: Path) -> None:
    write(repo, version="1.0.0", ref="0.9.0", contract=2)
    assert check.problems(repo) == [
        "index: echo ref is 'echo-v0.9.0', should be 'echo-v1.0.0'",
        "index: echo contract is 2, should be 1",
    ]


def test_a_release_with_a_help_url_outside_its_folder_is_refused(repo: Path) -> None:
    elsewhere = "https://github.com/Kriflow/kriflow/tree/main/connectors/echo#setup"
    write(repo, version="1.1.0", ref="1.1.0", help=elsewhere)
    assert check.problems(repo) == [f"echo: help_url {elsewhere} is outside {URL}/tree/main/echo"]


def test_changed_names_the_packages_a_branch_touched(repo: Path) -> None:
    (repo / "echo/README.md").write_text("# echo\n")
    commit(repo)
    assert check.changed(repo, "HEAD~1") == ["echo"]
    (repo / "tools").mkdir()
    (repo / "tools/x.py").write_text("")
    commit(repo)
    assert check.changed(repo, "HEAD~1") == ["all"]
