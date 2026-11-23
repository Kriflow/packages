"""The index lists every package of this repository as the core's review would, and every
Manifest passes the core's Manifest check. Needs the core's environment (README, Tests)."""

from pathlib import Path

from kriflow_connectors.index import read_index
from kriflow_connectors.packages import permissions, read_manifest

REPO = Path(__file__).parents[1]


def test_the_index_lists_every_package_as_reviewed() -> None:
    entries = read_index(str(REPO / "index.toml"))
    ours = [e for e in entries if e["git"] == "https://github.com/Kriflow/packages.git"]
    folders = sorted(p.parent.name for p in REPO.glob("*/swarmify-connector.toml"))
    assert sorted(e["name"] for e in ours) == folders
    for e in ours:
        manifest = read_manifest(REPO / e["subfolder"])
        assert manifest["name"] == e["name"]
        assert e["permissions"] == permissions(manifest), e["name"]
