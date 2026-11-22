"""Names the speech engines' compose file keeps through the rename to Kriflow (Kriflow/kriflow#564):
its project and model volume hold the containers and models already on the box, and it joins the
core's `swarmify` network. Moved with the package from the core's tools/test_frozen_names.py."""

from pathlib import Path

import yaml


def test_compose_keeps_its_project_volume_and_the_core_s_network() -> None:
    speech = yaml.safe_load((Path(__file__).parent / "compose.yaml").read_text())
    assert speech["name"] == "swarmify-speech"
    assert speech["networks"]["default"] == {"name": "swarmify", "external": True}
    assert speech["volumes"]["speech-models"]["name"] == "swarmify_speech-models"
