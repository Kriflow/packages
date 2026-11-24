"""The Remotion render connector: the package installed through the real install path (with the
suite's fake `npm` and `npx`), given to a world as a default, started over stdio MCP in the
connector sandbox and rendering with a fake Remotion CLI as the project's own (`fake_npx.py`),
its project one of the world's Templates (ADR 0028) or a build folder of the run.
`KRIFLOW_REMOTION_PROJECT` opts in to one real render."""

import base64
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import anyio
import pytest
import remotion_render as rr
from mcp import Client, StdioServerParameters

from kriflow_connectors import (
    MIGRATIONS,
    ConnectorError,
    Connectors,
    Credentials,
    FakeTransport,
    JobCancelled,
    McpTransport,
    SqliteStorage,
    read_manifest,
)
from kriflow_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from kriflow_kernel.events import read_after
from kriflow_kernel.sqlite import connect, migrate

HERE = Path(__file__).parent
VERSION = read_manifest(HERE)["version"]
"""The version this folder releases, so a release never reddens its own tests."""
W = "w"
Given = Callable[..., Connectors]


def registry(data: Path, transport: Any) -> Connectors:
    db = data / "swarmify.db"
    with closing(connect(db)) as conn:
        migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
    connectors = Connectors(SqliteStorage(db), transport, Credentials(data / ".env"), 0.05)
    connectors.packages.repo = HERE.parents[1]  # this repository
    return connectors


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One install for the module, as Kriflow does at startup: copied to the packages folder,
    `uv sync`ed there, then the (fake) npm and Remotion browser steps."""
    data = tmp_path_factory.mktemp("data")
    defaults = registry(data, FakeTransport()).install_defaults()
    assert defaults["remotion_render"] == VERSION
    return data / "packages"


@pytest.fixture
def given(installed: Path, tmp_path: Path) -> Given:
    """A registry with data directory `data` (tmp_path by default) sharing that install, the
    package given to world `w` as a default."""

    def make(transport: Any, data: Path = tmp_path) -> Connectors:
        data.mkdir(exist_ok=True)
        (data / "packages").symlink_to(installed)
        connectors = registry(data, transport)
        connectors.install_defaults()  # installed already: only remembered
        connectors.offer_defaults(W)
        return connectors

    return make


@pytest.fixture
def real(given: Given) -> Connectors:
    return given(McpTransport())


@pytest.fixture
def fake_npx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ready Template of world `w`, its dependencies installed: a fake Remotion CLI
    (`fake_npx.py`) as its own. A Chrome Headless Shell goes on PATH, as Kriflow's image has
    one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shell = bin_dir / "chrome-headless-shell"
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    project = tmp_path / TEMPLATES / "video"
    (project / "src").mkdir(parents=True)
    cli = project / rr.CLI
    cli.parent.mkdir(parents=True)
    cli.write_text(f"#!{sys.executable}\n" + (HERE / "fake_npx.py").read_text())
    cli.chmod(0o755)
    return project


RUN = Path("worlds") / W / "runs" / "run_1"
"""Where the run's files are under the data directory: with its Templates, all a render may read
of it."""
TEMPLATES = Path("worlds") / W / "assets" / "remotion_templates"
"""The world's Remotion Templates, which the core resolves a stage's `project` in."""


def stage(
    data: Path, project: Path, composition: str = "Short", at: str = "stage_2", **params: Any
) -> dict:
    """The stage `at` rendering the run's script; its output in that stage's own folder, the
    only one it may write."""
    script = data / RUN / "stage_1" / "artifacts" / "script.md"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# Why the chase works\n\nHook, then the reveal.\n")
    return {
        "calls": "remotion_render",
        "input": "script.md",
        "output": "short.mp4",
        "params": {
            "project": str(project),
            "composition": composition,
            "input_path": str(script),
            "output_path": str(data / RUN / at / "artifacts" / "short.mp4"),
            "frontmatter": {"world": W, "run": "run_1", "stage": "render"},
            **params,
        },
    }


def rendered(config: dict) -> dict:
    return json.loads(Path(config["params"]["output_path"]).read_text())


# The package


def test_the_manifest_passes_and_shares_remotion_builds_templates() -> None:
    m = read_manifest(HERE)
    assert (m["name"], m["kind"], m["capabilities"]) == ("remotion_render", "stage", ["templates"])
    assert m["params"]["required"] == ["project", "composition"]
    assert m["permissions"]["gpu"] is True  # a 3D scene on the host's GPU, not in software
    build = read_manifest(HERE.parent / "remotion_build")
    assert m["owned_folders"] == build["owned_folders"]
    keys = ("folder", "require", "prepare", "prepare_network")
    assert m["templates"] == {k: build["templates"][k] for k in keys}


def test_install_puts_its_python_the_remotion_cli_and_its_browser_beside_it(
    installed: Path,
) -> None:
    folder = installed / "remotion_render" / VERSION
    assert (folder / ".venv" / "bin" / "python").is_file()
    assert (folder / "fake-install.log").read_text().splitlines() == [
        "npm ci --ignore-scripts --no-audit --no-fund",
        "npx --no remotion browser ensure",
        "npm run gpu-browser",  # Chrome for Testing, which draws on a GPU
    ]


def test_its_settings_reach_the_process_and_no_templates_folder_is_one(given: Given) -> None:
    """Its Templates are the world's (ADR 0028), not a folder of the data directory."""
    connectors = given(FakeTransport())
    render = connectors.find(W, "remotion_render")
    assert render is not None and render.version == VERSION
    assert connectors._env(render) == {}
    connectors.save_setup(W, "remotion_render", {}, {"concurrency": "2"})
    assert connectors._env(render) == {"SWARMIFY_RENDER_CONCURRENCY": "2"}
    with pytest.raises(ValueError, match="templates"):
        connectors.save_setup(W, "remotion_render", {"templates": "elsewhere"}, {})
    with pytest.raises(ValueError, match="Render concurrency: does not match"):
        connectors.save_setup(W, "remotion_render", {}, {"concurrency": "--gl=angle"})


# The workflow check: its project is one of the world's Templates


def test_a_saved_render_stage_names_one_of_the_worlds_templates_and_a_composition(
    given: Given, tmp_path: Path
) -> None:
    connectors = given(FakeTransport())  # the core checks it: its process never starts
    render = {"calls": "remotion_render", "input": "script.md", "output": "short.mp4"}
    assert connectors.stage_problems(W, render) == [
        "remotion_render needs params.project, the Remotion project folder",
        "remotion_render needs params.composition, the composition ID",
    ]
    named = {"project": "shorts", "composition": "Short"}
    assert connectors.stage_problems(W, {**render, "params": named}) == [
        "the Template shorts is not one of this world's Remotion render Templates"
    ]
    (tmp_path / "remotion" / "shorts").mkdir(parents=True)  # the old data directory folder
    (tmp_path / "worlds/other/assets/remotion_templates/shorts").mkdir(parents=True)
    assert len(connectors.stage_problems(W, {**render, "params": named})) == 1
    path = {"project": str(tmp_path / "remotion" / "shorts"), "composition": "Short"}
    assert connectors.stage_problems(W, {**render, "params": path}) == [
        f"project must be a Template's name, not a path: {tmp_path / 'remotion' / 'shorts'}"
    ]
    (tmp_path / TEMPLATES / "shorts").mkdir(parents=True)
    assert connectors.stage_problems(W, {**render, "params": named}) == []
    assert connectors.stage_problems(W, {"calls": "remotion_render", "params": named}) == [
        "remotion_render needs an input, the script",
        "remotion_render needs an output, the video",
    ]


# Rendering over stdio


def test_progress_reads_the_cli_lines() -> None:
    assert rr.progress("") == (0.0, "starting the render")
    assert rr.progress("Bundling 40%\nRendered 15/30, time remaining: 1s\n") == (
        0.45,
        "rendered 15/30 frames",
    )
    assert rr.progress("Rendered 30/30\nEncoded 24/30\n") == (0.98, "encoded 24/30 frames")


def test_renders_the_script_into_a_video_with_a_sidecar(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    config = stage(tmp_path, fake_npx, frames=10)  # YAML hands an int for a single frame
    call = real.run_stage(W, "run_1", "stage_2", config)
    assert call.status == "done" and call.progress == 1.0 and call.polls >= 1
    video = Path(config["params"]["output_path"])
    assert call.response is not None and call.response["output"] == str(video)
    out = rendered(config)
    assert out["composition"] == "Short" and out["entry"] == "src/index.ts"
    assert out["script"].startswith("# Why the chase works")
    assert out["scriptPath"] == config["params"]["input_path"]

    sidecar = video.with_name("short.mp4.md")
    assert call.response["sidecar"] == str(sidecar)
    text = sidecar.read_text()
    head = text.split("---\n")[1]
    assert 'type: "video"' in head and 'world: "w"' in head and 'run: "run_1"' in head
    assert 'stage: "render"' in head and "created: " in head
    assert "from the script `script.md` with composition `Short`" in text


@pytest.fixture
def own_browser(installed: Path) -> Iterator[Path]:
    """What `remotion browser ensure` leaves beside the installed package, for one test."""
    browsers = installed / "remotion_render" / VERSION / "node_modules" / ".remotion"
    shell = browsers / "chrome-headless-shell" / "linux64" / "chrome-headless-shell"
    shell.parent.mkdir(parents=True)
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    yield shell
    shutil.rmtree(browsers)


def test_its_own_browser_is_the_one_remotion_uses_else_one_on_path(
    tmp_path: Path, real: Connectors, fake_npx: Path, request: pytest.FixtureRequest
) -> None:
    shell = tmp_path / "bin" / "chrome-headless-shell"  # as Kriflow's image has
    config = stage(tmp_path, fake_npx)
    real.run_stage(W, "run_1", "stage_2", config)
    assert rendered(config)["browser"] == str(shell)

    own = request.getfixturevalue("own_browser")
    config = stage(tmp_path, fake_npx, at="stage_3")
    real.run_stage(W, "run_1", "stage_3", config)
    assert Path(rendered(config)["browser"]).resolve() == own.resolve()

    shell.unlink()  # none: a render cannot download one, having no network
    if shutil.which("chrome-headless-shell"):
        pytest.skip("a Chrome Headless Shell on this PATH")
    own.unlink()
    with pytest.raises(ConnectorError, match="no Chrome Headless Shell: reinstall"):
        real.run_stage(W, "run_1", "stage_4", stage(tmp_path, fake_npx, at="stage_4"))


def test_given_a_gpu_it_renders_there_with_chrome_for_testing_else_in_software(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the desktop box (RTX 2070 Super) a 3D scene rendered at 3.3 frames a second on the
    GPU against 0.33 in software (2026-11-03)."""
    dev, browsers = tmp_path / "dev", tmp_path / ".remotion"
    tested = browsers / "chrome-for-testing" / "linux64" / "chrome-linux64" / "chrome"
    shell = browsers / "chrome-headless-shell" / "linux64" / "x" / "chrome-headless-shell"
    for f in (tested, shell, dev / "dri" / "card0"):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")
    monkeypatch.setattr(rr, "DEV", dev)
    monkeypatch.setattr(rr, "BROWSERS", browsers)
    software = [f"--browser-executable={shell}"]
    assert rr._browser() == software  # no GPU handed in (a card node alone renders nothing)

    (dev / "dri" / "renderD128").write_text("")
    gpu = ["--chrome-mode=chrome-for-testing", "--gl=angle-egl", "--concurrency=1"]
    assert rr._browser() == [f"--browser-executable={tested}", *gpu]
    (dev / "dri" / "renderD128").unlink()
    (dev / "nvidia0").write_text("")
    assert rr._browser() == [f"--browser-executable={tested}", *gpu]

    tested.unlink()  # installed before Chrome for Testing was part of it
    assert rr._browser() == software


def test_progress_over_chunks_counts_frames_done_of_the_whole_range() -> None:
    log = "Bundling 100%\nChunk 0-599: 0/1500 frames done\nRendered 600/600\nEncoded 600/600\n"
    assert rr.progress(log) == (0.36, "rendered 600/1500 frames")
    log += "Chunk 600-1199: 600/1500 frames done\nRendered 300/600, time remaining: 9s\n"
    assert rr.progress(log) == (0.54, "rendered 900/1500 frames")
    assert rr.progress(log + "Chunk 0-1499: 1500/1500 frames done\n")[0] == 0.9


def _here(tmp_path: Path, project: Path, composition: str = "Short") -> tuple[Path, list[float]]:
    """A render started in this process, so DEV and BROWSERS patched here decide its browser;
    its job, polled until it ends, and the progress each poll saw."""
    params = stage(tmp_path, project, composition)["params"]
    job = Path(rr.remotion_render(idempotency_key="k", **params)["job_id"])
    seen, deadline = [], time.monotonic() + 30
    while (status := rr.remotion_render_status(str(job)))["status"] == "running":
        assert time.monotonic() < deadline
        seen.append(status["progress"])
        time.sleep(0.02)
    return job, seen


def _calls(job: Path) -> list[list[str]]:
    """The fake CLI's invocations, which it printed into the job's log."""
    log = Path(json.loads(job.read_text())["log"]).read_text()
    return [
        json.loads(line[15:]) for line in log.splitlines() if line.startswith("fake remotion: ")
    ]


@pytest.fixture
def gpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An NVIDIA GPU handed in and Chrome for Testing installed, for a render in this process."""
    dev, tested = tmp_path / "dev", tmp_path / ".remotion" / "chrome-for-testing/l/c/chrome"
    for f in (dev / "nvidia0", tested):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")
    monkeypatch.setattr(rr, "DEV", dev)
    monkeypatch.setattr(rr, "BROWSERS", tmp_path / ".remotion")
    return tested


def test_on_a_gpu_it_renders_in_chunks_of_fresh_browsers_then_joins_them(
    tmp_path: Path, fake_npx: Path, gpu: Path
) -> None:
    """One long-lived tab ran out of WebGL contexts at frame 1661: each 600 frames get their own
    CLI process, from one bundle, and their audio is joined with them."""
    job, seen = _here(tmp_path, fake_npx)
    state = json.loads(job.read_text())
    assert state["status"] == "done", rr._tail(Path(state["log"]))
    calls = _calls(job)
    assert [c[0] for c in calls] == [
        "bundle",
        "compositions",
        "render",
        "render",
        "render",
        "ffmpeg",
    ]
    bundle = str(rr._hidden(Path(state["output"]), "bundle"))
    renders = calls[2:5]
    assert all(c[1] == bundle and "--concurrency=1" in c for c in renders)
    assert all(f"--browser-executable={gpu}" in c and "--gl=angle-egl" in c for c in renders)
    out = json.loads(Path(state["output"]).read_text())
    assert [p["frames"] for p in out["parts"]] == ["0-599", "600-1199", "1200-1499"]
    assert [a["audio"] for a in out["audio"]] == ["0-599", "600-1199", "1200-1499"]
    assert out["parts"][0]["script"].startswith("# Why the chase works")
    assert seen == sorted(seen) and rr.remotion_render_status(str(job))["progress"] == 1
    hidden = {p.name for p in Path(state["output"]).parent.iterdir()}
    assert hidden == {"short.mp4", "short.mp4.md", ".short.mp4.job.json", ".short.mp4.log"} | {
        ".short.mp4.props.json",
        "short.contact_1.png",
        "short.contact_2.png",
    }


def test_a_failing_chunk_fails_the_job_with_its_output(
    tmp_path: Path, fake_npx: Path, gpu: Path
) -> None:
    job, _ = _here(tmp_path, fake_npx, "Broken")  # fails on frame 900, the second chunk
    status = rr.remotion_render_status(str(job))
    assert status["status"] == "failed"
    assert status["error"].startswith("remotion render of frames 600-1199 exited with 1")
    assert "No composition with the ID Broken found." in status["error"]
    assert [c[0] for c in _calls(job)] == ["bundle", "compositions", "render", "render"]
    assert not rr._hidden(Path(json.loads(job.read_text())["output"]), "parts").exists()


def test_each_gpu_chunk_waits_for_the_shared_gpu_lock_and_says_so(
    tmp_path: Path, fake_npx: Path, gpu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another GPU job holding the card: the render waits, saying so in its progress, and
    renders once it is let go; each chunk takes the lock again, so jobs share the card between
    chunks."""
    lock = tmp_path / "gpu.lock"
    monkeypatch.setenv("SWARMIFY_GPU_LOCK", str(lock))
    params = stage(tmp_path, fake_npx)["params"]
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        job = Path(rr.remotion_render(idempotency_key="k", **params)["job_id"])
        deadline = time.monotonic() + 30
        while (status := rr.remotion_render_status(str(job)))["message"] != "waiting for the GPU":
            assert status["status"] == "running" and time.monotonic() < deadline, status
            time.sleep(0.02)
        assert status["progress"] == 0
        assert [c[0] for c in _calls(job)] == ["bundle", "compositions"]  # no GPU yet
    while rr.remotion_render_status(str(job))["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert json.loads(job.read_text())["status"] == "done"
    log = Path(json.loads(job.read_text())["log"]).read_text()
    assert log.count("Waiting for the GPU") == 1  # free for the next chunks


def test_each_gpu_chunk_takes_a_gpu_slot_and_says_while_it_waits(
    tmp_path: Path, fake_npx: Path, gpu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kriflow's Resources (ADR 0030): with the gpu's Slots named, a chunk waits for a free one,
    saying so and leaving a waiting note with its run, then renders."""
    slots = tmp_path / "resources" / "gpu"
    (slots / "waiting").mkdir(parents=True)
    monkeypatch.setenv("KRIFLOW_RESOURCE_GPU", str(slots))
    monkeypatch.setenv("KRIFLOW_RESOURCE_GPU_SLOTS", "1")
    monkeypatch.setenv("KRIFLOW_RUN_ID", "run_7")
    monkeypatch.delenv("SWARMIFY_GPU_LOCK", raising=False)
    params = stage(tmp_path, fake_npx)["params"]
    with open(slots / "0.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        job = Path(rr.remotion_render(idempotency_key="k", **params)["job_id"])
        deadline = time.monotonic() + 30
        while (status := rr.remotion_render_status(str(job)))["message"] != "waiting for the GPU":
            assert status["status"] == "running" and time.monotonic() < deadline, status
            time.sleep(0.02)
        [note] = [json.loads(f.read_text()) for f in (slots / "waiting").glob("*.json")]
        assert (note["run"], note["package"]) == ("run_7", "remotion_render")
    while rr.remotion_render_status(str(job))["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert json.loads(job.read_text())["status"] == "done"
    assert not list(slots.glob("waiting/*.json"))


def test_without_a_gpu_it_is_one_cli_call(
    tmp_path: Path, fake_npx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rr, "DEV", tmp_path / "dev")
    monkeypatch.setattr(rr, "BROWSERS", tmp_path / ".remotion")
    job, _ = _here(tmp_path, fake_npx)
    assert json.loads(job.read_text())["status"] == "done"
    assert [c[0] for c in _calls(job)] == ["render"]


def test_the_render_concurrency_setting_caps_the_tabs_rendering_at_once(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    """Unset, Remotion picks; Docker sets 2, since a 3D scene in software runs out of memory
    with more (spike 2026-10-30)."""
    config = stage(tmp_path, fake_npx)
    real.run_stage(W, "run_1", "stage_2", config)
    assert rendered(config)["concurrency"] == ""

    real.save_setup(W, "remotion_render", {"concurrency": "2"}, {})
    config = stage(tmp_path, fake_npx, at="stage_3")
    real.run_stage(W, "run_1", "stage_3", config)
    assert rendered(config)["concurrency"] == "2"


def test_a_project_that_is_not_a_full_path_is_refused(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    """The core resolves a stage's Template to its folder: a bare name reaching the connector
    is never looked up beside it."""
    with pytest.raises(ConnectorError, match="project must be a folder's full path"):
        real.run_stage(W, "run_1", "stage_2", stage(tmp_path, Path("video")))


def test_a_project_without_node_modules_is_refused_saying_how_to_install_them(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    """A render never installs: the sandbox has no network for it, and a project's
    package.json may be an agent's, whose scripts would run."""
    shutil.rmtree(fake_npx / "node_modules")
    with pytest.raises(ConnectorError, match="has no node_modules: run npm ci there"):
        real.run_stage(W, "run_1", "stage_2", stage(tmp_path, fake_npx))
    assert not (fake_npx / "node_modules").exists()


def test_the_sidecar_records_the_template_commit_of_a_git_checkout(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    config = stage(tmp_path, fake_npx)
    sidecar = Path(config["params"]["output_path"]).with_name("short.mp4.md")
    real.run_stage(W, "run_1", "stage_2", config)
    assert "commit: " not in sidecar.read_text()

    git = ["git", "-C", str(fake_npx), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "template"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    config = stage(tmp_path, fake_npx, at="stage_3")
    sidecar = Path(config["params"]["output_path"]).with_name("short.mp4.md")
    real.run_stage(W, "run_1", "stage_3", config)
    assert f'commit: "{sha.stdout.strip()}"' in sidecar.read_text()


def test_progress_reaches_the_call_while_it_renders(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    real.run_stage(W, "run_1", "stage_2", stage(tmp_path, fake_npx, "Slow"))
    with closing(connect(tmp_path / "swarmify.db")) as conn:
        seen = [e.payload for e in read_after(conn, W, 0) if e.type == "connector_call.updated"]
    messages = [p["message"] for p in seen if p["status"] == "polling"]
    assert any(m and m.startswith("rendered ") for m in messages), messages
    values = [p["progress"] for p in seen if p["progress"] is not None]
    assert values == sorted(values) and values[-1] == 1.0


def test_aborting_the_run_stops_the_render_and_its_cli(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    config = stage(tmp_path, fake_npx, "Long")
    log = Path(config["params"]["output_path"]).with_name(".short.mp4.log")
    # aborted once the CLI renders frames
    real.aborted = lambda _: log.exists() and "Rendered 3/" in log.read_text()
    with pytest.raises(JobCancelled, match="aborted"):
        real.run_stage(W, "run_1", "stage_2", config)
    call = real.call_for("stage_2")
    assert call is not None and call.status == "failed"
    state = json.loads(Path(call.job_id or "").read_text())
    assert (state["status"], state["error"]) == ("failed", "the render was cancelled")
    size = log.stat().st_size
    time.sleep(0.5)  # the CLI wrote a frame every 0.1 s while it ran
    assert log.stat().st_size == size, "the CLI outlived the cancel"


def test_a_job_that_ignores_sigterm_is_killed_after_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rr, "GRACE", 0.3)
    stubborn = subprocess.Popen(
        [sys.executable, "-c", IGNORES_SIGTERM], start_new_session=True, stdout=subprocess.PIPE
    )
    assert stubborn.stdout is not None and stubborn.stdout.readline() == b"ready\n"
    job = tmp_path / ".short.mp4.job.json"
    job.write_text(json.dumps({"status": "running", "pid": stubborn.pid, "log": "/nonexistent"}))
    started = time.monotonic()
    answer = rr.remotion_render_status(str(job), cancel=True)
    assert answer["status"] == "failed" and answer["error"].startswith("the render was cancelled")
    assert stubborn.wait(timeout=5) == -signal.SIGKILL
    assert 0.3 <= time.monotonic() - started < 5


IGNORES_SIGTERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',"
    " flush=True); time.sleep(60)"
)


def test_a_failed_render_fails_the_stage_with_the_cli_output(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    with pytest.raises(ConnectorError) as raised:
        real.run_stage(W, "run_1", "stage_2", stage(tmp_path, fake_npx, "Broken"))
    assert "remotion render exited with 1" in str(raised.value)
    assert "No composition with the ID Broken found." in str(raised.value)
    call = real.call_for("stage_2")
    assert call is not None and call.status == "failed"
    assert "No composition with the ID Broken" in (call.error or "")


def test_a_render_that_writes_nothing_fails(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    with pytest.raises(ConnectorError, match="wrote no video"):
        real.run_stage(W, "run_1", "stage_2", stage(tmp_path, fake_npx, "Empty"))


@pytest.mark.parametrize(
    ("over", "problem"),
    [
        ({"input_path": "/nope/script.md"}, "the script /nope/script.md does not exist"),
        ({"project": "/nope"}, "the Remotion project /nope does not exist"),
        ({"input_path": ""}, "the render stage needs an `input`"),
        ({"composition": "--browser-executable=/bin/sh"}, "composition must be a plain value"),
    ],
)
def test_a_bad_request_is_refused_before_rendering(
    tmp_path: Path, real: Connectors, fake_npx: Path, over: dict, problem: str
) -> None:
    config = stage(tmp_path, fake_npx)
    config["params"].update(over)
    with pytest.raises(ConnectorError, match=problem):
        real.run_stage(W, "run_1", "stage_2", config)


def test_a_repeated_call_attaches_to_the_render_instead_of_starting_another(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    config = stage(tmp_path, fake_npx, "Slow")
    request = {**config["params"], "idempotency_key": "stage_2"}
    connector = real.find(W, "remotion_render")
    assert connector is not None
    env = real._env(connector)
    transport = McpTransport()
    first = transport.call(connector, "remotion_render", request, env)
    again = transport.call(connector, "remotion_render", request, env)
    assert first["job_id"] == again["job_id"]
    assert (first["message"], again["message"]) == ("render started", "render already started")
    job = Path(first["job_id"])
    deadline = time.monotonic() + 30  # its contact sheets too, on a loaded runner
    while json.loads(job.read_text())["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    done = transport.call(connector, "remotion_render_status", {"job_id": str(job)}, env)
    assert done["status"] == "done"
    assert transport.call(connector, "remotion_render", request, env)["message"] == (
        "render already started"
    )


@pytest.fixture
def real_browser(installed: Path) -> Iterator[None]:
    """The test install's npm is fake, so it has no browser: the real project's own, which its
    CLI downloaded beside it, for one real test."""
    mine = installed / "remotion_render" / VERSION / "node_modules" / ".remotion"
    theirs = Path(os.environ["KRIFLOW_REMOTION_PROJECT"]) / "node_modules" / ".remotion"
    if mine.exists() or not theirs.is_dir():
        yield
        return
    shutil.copytree(theirs, mine, symlinks=True)
    yield
    shutil.rmtree(mine)


@pytest.mark.skipif(
    not os.environ.get("KRIFLOW_REMOTION_PROJECT"),
    reason="set KRIFLOW_REMOTION_PROJECT (and KRIFLOW_REMOTION_COMPOSITION) for a real render",
)
def test_a_real_render(tmp_path: Path, real: Connectors, real_browser: None) -> None:
    real_project = tmp_path / TEMPLATES / "real"  # a world's Template, where its sandbox reads
    # without a webpack cache of earlier runs, which the read-only Template could not update
    skip = shutil.ignore_patterns(".cache")
    shutil.copytree(
        os.environ["KRIFLOW_REMOTION_PROJECT"], real_project, symlinks=True, ignore=skip
    )
    composition = os.environ.get("KRIFLOW_REMOTION_COMPOSITION", "Main")
    config = stage(tmp_path, real_project, composition, frames="0-9")
    call = real.run_stage(W, "run_1", "stage_2", config)
    video = Path(config["params"]["output_path"])
    assert call.status == "done" and video.stat().st_size > 1000
    assert video.read_bytes()[4:8] == b"ftyp"
    assert video.with_name("short.mp4.md").exists()
    assert video.with_name("short.contact_1.png").is_file()  # the bundled ffmpeg decodes it


def test_pid_zero_is_never_a_live_render() -> None:
    assert not rr.alive(0) and rr.alive(os.getpid())


# A project an agent wrote (a remotion_build stage) renders in the connector sandbox


def _built(tmp_path: Path, fake_npx: Path, given: Given) -> tuple[Connectors, dict, Path]:
    """A render stage of a project a build stage wrote, in its stage's `build` folder, with the
    data directory's keys beside it and the sandbox's blocks recorded."""
    (tmp_path / ".env").write_text("YOUTUBE_CLIENT_SECRET=s3cret\n")
    (tmp_path / ".env").chmod(0o600)
    connectors = given(McpTransport())
    connectors.blocked = []  # type: ignore[attr-defined]
    connectors.on_blocked = lambda c, at, lines: connectors.blocked.append((c.name, at, lines))  # type: ignore[attr-defined]
    project = tmp_path / RUN / "stage_2" / "build"
    project.parent.mkdir(parents=True, exist_ok=True)
    fake_npx.rename(project)
    config = stage(tmp_path, project, "Snoop")
    config["params"]["output_path"] = str(project.parent / "artifacts/video.mp4")
    return connectors, config, project


def test_a_built_project_renders_in_a_sandbox_it_cannot_leave(
    tmp_path: Path, given: Given, fake_npx: Path
) -> None:
    """The agent's code tries to read the data directory's keys, plant a file outside its
    stage and reach the internet: each is refused, and each is in the stage's run log."""
    connectors, config, project = _built(tmp_path, fake_npx, given)
    (tmp_path / "worlds" / W / "assets" / "notes.md").write_text("the world's own")
    assert connectors.run_stage(W, "run_1", "stage_2", config).status == "done"
    snooped = rendered(config)["snooped"]
    assert snooped["read_keys"].startswith("refused") and snooped["network"].startswith("refused")
    assert snooped["read_world"].startswith("refused"), snooped
    # Linux hides the data directory behind an empty folder of the sandbox's own, which takes
    # the write and is gone with it; macOS refuses it
    if sys.platform != "linux":
        assert snooped["write_outside"].startswith("refused"), snooped
    assert not (tmp_path / "planted.txt").exists()
    lines = [line for _, at, said in connectors.blocked for line in said]  # type: ignore[attr-defined]
    assert {at for _, at, _ in connectors.blocked} == {("run_1", "stage_2")}  # type: ignore[attr-defined]
    assert f"read {(tmp_path / '.env').resolve()}" in lines, lines
    assert f"write {(tmp_path / 'planted.txt').resolve()}" in lines, lines
    assert any(line.startswith("connect example.com") for line in lines), lines


def test_a_template_renders_in_a_sandbox_that_reads_nothing_else_of_the_world(
    tmp_path: Path, given: Given, fake_npx: Path
) -> None:
    """Issue 353: a direct render reads the world's Templates in place, but neither writes them
    nor reads the world's other assets."""
    connectors = given(McpTransport())
    (tmp_path / "worlds" / W / "assets" / "notes.md").write_text("the world's own")
    config = stage(tmp_path, fake_npx, "Snoop")
    assert connectors.run_stage(W, "run_1", "stage_2", config).status == "done"
    snooped = rendered(config)["snooped"]
    assert snooped["read_world"].startswith("refused"), snooped
    assert snooped["write_project"].startswith("refused"), snooped
    assert snooped["read_keys"].startswith("refused") and snooped["network"].startswith("refused")
    assert not (fake_npx / "planted.txt").exists()


@pytest.fixture
def port_3000() -> Iterator[None]:
    """Something else on Remotion's default port, as an open-webui container was."""
    try:
        server = ThreadingHTTPServer(("localhost", 3000), SimpleHTTPRequestHandler)
    except OSError:  # already held, which is the point
        yield
        return
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("built", [False, True])
def test_a_render_uses_a_free_port_not_one_another_server_holds(
    tmp_path: Path, given: Given, fake_npx: Path, port_3000: None, built: bool
) -> None:
    """Template renders and remotion_build's renders alike."""
    connectors, config = (
        _built(tmp_path, fake_npx, given)[:2]
        if built
        else (given(McpTransport()), stage(tmp_path, fake_npx))
    )
    assert connectors.run_stage(W, "run_1", "stage_2", config).status == "done"


# Contact sheets: every mp4 render leaves them beside its video


def test_a_render_leaves_contact_sheets_beside_its_video_and_names_them(
    tmp_path: Path, real: Connectors, fake_npx: Path
) -> None:
    """A frame every 2 s, 6 across and 5 down, 320 wide: the fake's 62 s (125 frames at 2
    fps, every 4th kept) make a full sheet of 30 and one of the 2 left (60 s and 62 s)."""
    from PIL import Image as Picture

    config = stage(tmp_path, fake_npx)
    assert real.run_stage(W, "run_1", "stage_2", config).status == "done"
    video = Path(config["params"]["output_path"])
    first, second = video.with_name("short.contact_1.png"), video.with_name("short.contact_2.png")
    with Picture.open(first) as a, Picture.open(second) as b:
        assert a.size == (6 * 320, 5 * (320 + 24))  # the fake's frames are square
        assert b.size == (2 * 320, 320 + 24)
        assert a.getpixel((5, 24 + 5))[0] == 0 and a.getpixel((320 + 5, 24 + 5))[0] == 4
        assert b.getpixel((320 + 5, 24 + 5))[0] == 124  # frames 120 and 124
    assert not video.with_name("short.contact_3.png").exists()
    text = video.with_name("short.mp4.md").read_text()
    assert "a frame every 2 s, 6 across and 5 down" in text
    assert "`short.contact_1.png`, `short.contact_2.png`" in text


def test_contact_sheets_that_fail_never_fail_the_render(
    tmp_path: Path, fake_npx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rr, "DEV", tmp_path / "dev")
    job, _ = _here(tmp_path, fake_npx, "Blind")  # ffmpeg names no video stream
    state = json.loads(job.read_text())
    assert state["status"] == "done"
    assert not list(Path(state["output"]).parent.glob("*.contact_*.png"))
    assert "No contact sheets: ffmpeg named no video stream" in Path(state["log"]).read_text()
    assert "Contact sheets" not in Path(state["sidecar"]).read_text()


# Stills, as a Build Session calls them: the server confined to the build's stage


def still(connectors: Connectors, **arguments: Any) -> Any:
    """`stills` called over stdio MCP on the server a Build Session of stage_2 is given."""
    server = connectors.agent_servers(W, ["remotion_render"], None, ("run_1", "stage_2"))
    config = server["remotion_render"]
    params = StdioServerParameters(
        command=config["command"], args=config["args"], env=config["env"]
    )

    async def call() -> Any:
        async with Client(params, cache=None) as client:
            return await client.call_tool("stills", arguments)

    return anyio.run(call)


@pytest.fixture
def build(tmp_path: Path, fake_npx: Path) -> Path:
    project = tmp_path / RUN / "stage_2" / "build"
    project.parent.mkdir(parents=True)
    fake_npx.rename(project)
    return project


def _cli(log: Path) -> list[list[str]]:
    return [
        json.loads(line[15:])
        for line in log.read_text().splitlines()
        if line.startswith("fake remotion: ")
    ]


def test_stills_render_the_frames_asked_from_one_bundle_and_a_sheet_of_them(
    tmp_path: Path, given: Given, build: Path
) -> None:
    from PIL import Image as Picture

    script = tmp_path / RUN / "stage_1" / "artifacts" / "script.md"
    script.parent.mkdir(parents=True)
    script.write_text("# Hook\n")
    frames = [90, 0, 30, 60, 120, 30]
    result = still(
        given(McpTransport()),
        project=str(build),
        frames=frames,
        input_path=str(script),
        props={"audit": True},
    )
    assert not result.is_error, result
    *images, said = result.content
    assert [i.type for i in images] == ["image"] * 6 and said.type == "text"
    out = build.parent / "stills" / "Main"
    paths = [out / f"{n:05d}.jpeg" for n in (0, 30, 60, 90, 120)]
    for image, path in zip(images, [*paths, build.parent / "stills" / "Main.jpeg"], strict=True):
        assert base64.b64decode(image.data) == path.read_bytes()
    with Picture.open(build.parent / "stills" / "Main.jpeg") as sheet:
        assert sheet.size == (4 * 320, 2 * (569 + 24))  # 54x96 frames, 320 wide, 4 a row
    assert said.text.startswith("Main at scale 0.5, in order: frame 0: ")
    assert "all of them on one sheet, in that order, labelled" in said.text
    calls = _cli(build.parent / "stills" / "Main.log")
    assert [c[0] for c in calls] == ["bundle"] + ["still"] * 5
    assert [c[4] for c in calls[1:]] == [f"--frame={n}" for n in (0, 30, 60, 90, 120)]
    assert all("--scale=0.5" in c and "--image-format=jpeg" in c for c in calls[1:])
    assert all(any(a.startswith("--browser-executable=") for a in c) for c in calls[1:])
    assert len({next(a for a in c if a.startswith("--port=")) for c in calls[1:]}) == 5
    props = json.loads((out / "props.json").read_text())
    assert props == {"audit": True, "script": "# Hook\n", "scriptPath": str(script)}
    assert not (out / "bundle").exists()


def test_four_stills_or_fewer_get_no_sheet(given: Given, build: Path) -> None:
    result = still(given(McpTransport()), project=str(build), frames=[5], scale=0.25)
    assert not result.is_error, result
    image, said = result.content
    assert image.type == "image" and "scale 0.25" in said.text and "sheet" not in said.text
    assert not (build.parent / "stills" / "Main.jpeg").exists()


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        ({"frames": []}, "frames must be 1 to 24 frame numbers from 0"),
        ({"frames": list(range(25))}, "frames must be 1 to 24 frame numbers from 0"),
        ({"frames": [-1]}, "frames must be 1 to 24 frame numbers from 0"),
        ({"frames": [1], "scale": 0}, "scale must be above 0"),
        ({"frames": [1], "composition": "Broken"}, "remotion still of frame 1 exited with 1"),
        ({"frames": [1], "input_path": "/nowhere.md"}, "the script /nowhere.md does not exist"),
    ],
)
def test_stills_that_cannot_render_say_why(
    given: Given, build: Path, arguments: dict, said: str
) -> None:
    result = still(given(McpTransport()), project=str(build), **arguments)
    assert result.is_error
    assert said in result.content[0].text


def test_stills_of_a_project_without_node_modules_say_how_to_install(
    given: Given, build: Path
) -> None:
    shutil.rmtree(build / "node_modules")
    result = still(given(McpTransport()), project=str(build), frames=[0])
    assert result.is_error and "has no node_modules" in result.content[0].text


def test_stills_stop_at_their_time_limit(build: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rr, "STILLS_LIMIT", 1.0)
    with pytest.raises(rr.ToolError, match="remotion still of frame 0 ran past its time limit"):
        rr.stills(str(build), [0], composition="Slow")


@pytest.mark.skipif(
    not os.environ.get("KRIFLOW_REMOTION_PROJECT"),
    reason="set KRIFLOW_REMOTION_PROJECT (and KRIFLOW_REMOTION_COMPOSITION) for real stills",
)
def test_real_stills(tmp_path: Path, given: Given, real_browser: None) -> None:
    project = tmp_path / RUN / "stage_2" / "build"  # a build's folder, as a session's
    shutil.copytree(os.environ["KRIFLOW_REMOTION_PROJECT"], project, symlinks=True)
    composition = os.environ.get("KRIFLOW_REMOTION_COMPOSITION", "Main")
    frames = [0, 5, 10, 15, 20]
    result = still(
        given(McpTransport()), project=str(project), frames=frames, composition=composition
    )
    assert not result.is_error, result
    *images, _ = result.content
    assert [i.mime_type for i in images] == ["image/jpeg"] * 6
    assert base64.b64decode(images[0].data)[:2] == b"\xff\xd8"
