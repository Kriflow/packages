"""The Sound Bank connector: the package installed through the real install path, given to a world
as a default, and its MCP server started as a swarm stage's agents get it (sandboxed, not
confined to the stage), keeping a take an earlier stage of the run made (issue 15). Then the
licence rule and the source allowlist, offline."""

import json
import math
import struct
import wave
from contextlib import closing
from pathlib import Path
from typing import Any

import anyio
import pytest
import sound_bank as sb
from mcp import Client, StdioServerParameters

from kriflow_connectors import (
    MIGRATIONS,
    Connectors,
    Credentials,
    FakeTransport,
    McpTransport,
    SqliteStorage,
    read_manifest,
)
from kriflow_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from kriflow_kernel.sqlite import connect, migrate

HERE = Path(__file__).parent
VERSION = read_manifest(HERE)["version"]
"""The version this folder releases, so a release never reddens its own tests."""
W = "w"


def registry(data: Path, transport: Any) -> Connectors:
    db = data / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    connectors = Connectors(SqliteStorage(db), transport, Credentials(data / ".env"), 0.05)
    connectors.packages.repo = HERE.parents[1]  # this repository
    return connectors


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("data")
    registry(data, FakeTransport()).install_defaults()  # installed, though off by default
    assert (data / "packages" / "sound_bank" / VERSION / ".venv").is_dir()
    return data / "packages"


@pytest.fixture
def connectors(installed: Path, tmp_path: Path) -> Connectors:
    (tmp_path / "packages").symlink_to(installed)
    connectors = registry(tmp_path, McpTransport())
    connectors.install_defaults()  # installed already: only remembered
    connectors.offer_defaults(W)
    connectors.enable(W, "sound_bank", VERSION)  # off by default
    return connectors


def take(path: Path, seconds: float = 1.0) -> Path:
    """A 440 Hz tone with silence around it, as a vocal-effect take."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 24000
    frames = [0] * (rate // 4)
    frames += [
        int(9000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(rate * seconds))
    ]
    frames += [0] * (rate // 4)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    return path


def keep(connectors: Connectors, **arguments: Any) -> tuple[bool, str]:
    """`sound_keep` over stdio MCP on the server a swarm stage (run_1 / stage_3) is lent."""
    servers = connectors.agent_servers(W, ["sound_bank"], None, run=("run_1", "stage_3"))
    config = servers["sound_bank"]
    params = StdioServerParameters(
        command=config["command"], args=config["args"], env=config["env"]
    )

    async def call() -> Any:
        async with Client(params, cache=None) as client:
            return await client.call_tool("sound_keep", arguments)

    result = anyio.run(call)
    return result.is_error, result.content[0].text


ARGS = {"name": "snore", "description": "a cartoon snore", "source": "made", "licence": "CC0"}


def test_a_swarm_stage_keeps_a_take_an_earlier_stage_of_its_run_made(
    connectors: Connectors, tmp_path: Path
) -> None:
    vocal = take(tmp_path / "worlds" / W / "runs" / "run_1" / "stage_2" / "vocal-2.wav")
    bank = tmp_path / "worlds" / W / "assets" / "sounds"

    error, text = keep(connectors, file=str(vocal), dry_run=True, **ARGS)
    assert not error, text
    tried = json.loads(text)
    assert tried["dry_run"] and 0.9 < tried["seconds"] < 1.1  # the silence trimmed
    assert tried["bank"] == str(bank)
    assert not (bank / "index.json").exists()  # a dry run never touches the bank

    error, text = keep(connectors, file=str(vocal), **ARGS)
    assert not error, text
    assert (bank / "snore.wav").is_file() and (bank / "demos" / "snore.mp4").is_file()
    with wave.open(str(bank / "snore.wav")) as w:
        assert (w.getframerate(), w.getnchannels()) == (48000, 1)
    [entry] = json.loads((bank / "index.json").read_text())["sounds"]
    assert entry["draft"] and entry["source"] == "made" and entry["licence"] == "CC0"


def test_a_file_outside_the_worlds_runs_is_refused(connectors: Connectors, tmp_path: Path) -> None:
    other = take(tmp_path / "worlds" / "other" / "runs" / "run_9" / "stage_1" / "vocal.wav")
    error, text = keep(connectors, file=str(other), dry_run=True, **ARGS)
    assert error and str(other) in text  # "no such file", or the OS's "not permitted"


def test_only_cc0_or_public_domain_is_free() -> None:
    assert sb.free("CC0") and sb.free("Public domain") and sb.free("PD-self")
    assert not sb.free("CC BY 4.0") and not sb.free("CC0 and CC BY-SA") and not sb.free("")


def test_only_the_two_sources_are_reached() -> None:
    assert sb.allowed("https://bigsoundbank.com/snore-s1234.html")
    for url in ("http://bigsoundbank.com/x", "https://freesound.org/x", "https://evil.com/?u=x"):
        with pytest.raises(sb.Refused):
            sb.allowed(url)
