"""Shared test setup of the packages' tests. tools/test_packages.sh copies it to the core's
`connectors/conftest.py`, beside the package folders it copies there, as the core had it before
the packages moved (Kriflow/kriflow#587): the Default List names only the package under test,
released from its folder in a local packages repository, as the box installs it from here. So
`install_defaults()` installs that package (and the core's python_script) and never clones
GitHub."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from kriflow_connectors.fake import default_list
from kriflow_connectors.packages import MANIFEST, NEW_MANIFEST


@pytest.fixture(scope="module", autouse=True)
def own_default_list(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    folder = Path(request.path).parent
    if not any((folder / name).is_file() for name in (NEW_MANIFEST, MANIFEST)):
        yield
        return
    listed = default_list(tmp_path_factory.mktemp("default-list"), {folder.name: folder})
    with pytest.MonkeyPatch.context() as m:
        m.setenv("KRIFLOW_DEFAULT_PACKAGES", str(listed))
        yield
