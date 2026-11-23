"""The browser connector against a fake site on loopback (spec seam 4): no real login is used.

The package is installed once through the real install path (Chromium downloaded into it), then
driven over stdio MCP as an agent's tools would, with a World Login in its environment as the
core hands one: `sign_in` fills on the login's origin, refuses another origin, a framing page
and a cross-origin frame without typing, computes TOTP, reuses a saved session and replaces a
stale one, and asks a person (a request file, answered here) for an SMS code or a captcha. No
answer, recording or file name holds a secret. Last, the same package runs in the real sandbox
behind a live API: a connector stage signs in, a second run reuses the session, a foreign URL is
refused and a Person Check is answered over HTTP, and no secret is anywhere in the run folder."""

import fcntl
import hashlib
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import browser as br
import pytest
from fastapi.testclient import TestClient

from kriflow_api.main import create_app
from kriflow_connectors import (
    MIGRATIONS,
    Connector,
    ConnectorError,
    Connectors,
    Credentials,
    FakeTransport,
    McpTransport,
    SqliteStorage,
)
from kriflow_connectors.fake import default_list
from kriflow_kernel import MIGRATIONS as KERNEL_MIGRATIONS
from kriflow_kernel.sqlite import connect, migrate

HERE = Path(__file__).parent
VERSION = "1.0.0"
USER, PASSWORD = "ana", "Pa55-word-77031"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
SMS = "482913"
HIDDEN = "[secret: shop_admin]"
SHIPPED = (
    "browser.py",
    "install_chromium.py",
    "swarmify-connector.toml",
    "pyproject.toml",
    "uv.lock",
)


class FakeSite:
    """Two origins on loopback. Origin A: `/login` (a form posting username and password),
    then `/otp` (an authenticator code) or `/sms` (a code it "sent") when `second` says so,
    `/captcha` (a form behind a captcha), `/account` (only with the session cookie; it echoes
    the password and the cookie, to prove scrubbing), `/export.csv` (a download), `/` (links to
    origin B) and `/embed` (origin B's form in a frame). Origin B: the same form (the phishing
    case) and `/frame`, a page framing origin A's form. `expire()` forgets every session."""

    def __init__(self, second: str | None = None) -> None:
        self.second = second
        self.sessions: set[str] = set()
        self.pending: set[str] = set()
        self.posts: list[tuple[str, str]] = []
        site = self

        def handler(origin: str) -> type[BaseHTTPRequestHandler]:
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: Any) -> None:
                    pass

                def cookie(self, name: str) -> str:
                    jar = SimpleCookie(self.headers.get("Cookie") or "")
                    return jar[name].value if name in jar else ""

                def do_GET(self) -> None:
                    path = self.path
                    signed = self.cookie("sid") in site.sessions
                    if origin == "b":
                        if path == "/frame":
                            return self.page(f"<h1>Partner</h1><iframe src='{site.a}/login'"
                                             " width=600 height=400></iframe>")  # fmt: skip
                        return self.page(form("/login"))
                    if path == "/":
                        return self.page(
                            f"<a href='{site.b}/login'>Partner</a> <a href='{site.b}/frame'>"
                            "Framed</a> <a href='/embed'>Embed</a> <a href='/account'>Account</a>"
                        )
                    if path == "/embed":
                        return self.page(f"<iframe src='{site.b}/login' width=600 height=400>")
                    if path == "/login":
                        return self.page(form("/login"))
                    if path == "/captcha":
                        captcha = "<div class=g-recaptcha data-sitekey=k style='height:80px'>"
                        return self.page(captcha + "I am not a robot</div>" + form("/login"))
                    if path in ("/otp", "/sms"):
                        ask = (
                            "Enter the code we sent to ...67"
                            if path == "/sms"
                            else "Authenticator code"
                        )
                        return self.page(code_form(path, ask))
                    if path == "/account" and signed:
                        sid = self.cookie("sid")
                        said = f"Signed in as {USER}. pw={PASSWORD} sid={sid}"
                        return self.page(f"<h1>Account</h1><p>{said}</p>"
                                         "<a href='/export.csv'>Export</a>")  # fmt: skip
                    if path == "/export.csv" and signed:
                        return self.send(
                            200,
                            "order,total\n1,9.50\n",
                            {
                                "Content-Type": "text/csv",
                                "Content-Disposition": 'attachment; filename="orders.csv"',
                            },
                        )
                    self.send(303, "", {"Location": "/login"})

                def do_POST(self) -> None:
                    site.posts.append((origin, self.path))
                    length = int(self.headers.get("Content-Length", 0))
                    got = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                    if origin != "a":
                        return self.send(403, "No.")
                    if self.path == "/login":
                        if (got.get("username"), got.get("password")) != (USER, PASSWORD):
                            return self.page(form("/login", "Wrong username or password."))
                        if site.second:
                            token = os.urandom(8).hex()
                            site.pending.add(token)
                            to = "/otp" if site.second == "totp" else "/sms"
                            return self.send(303, "", {"Location": to, **cookie("pending", token)})
                        return self.signed_in()
                    if self.cookie("pending") not in site.pending:
                        return self.send(303, "", {"Location": "/login"})
                    now = time.time()
                    good = (
                        {br.totp(TOTP_SECRET, now + d) for d in (-30, 0, 30)}
                        if self.path == "/otp"
                        else {SMS}
                    )
                    if got.get("code") not in good:
                        return self.page(code_form(self.path, "That code did not work."))
                    self.signed_in()

                def signed_in(self) -> None:
                    sid = os.urandom(12).hex()
                    site.sessions.add(sid)
                    self.send(303, "", {"Location": "/account", **cookie("sid", sid)})

                def page(self, body: str) -> None:
                    self.send(200, f"<!doctype html><title>Shop</title><body>{body}</body>")

                def send(
                    self, status: int, text: str, headers: dict[str, str] | None = None
                ) -> None:
                    body = text.encode()
                    self.send_response(status)
                    for k, v in {"Content-Type": "text/html", **(headers or {})}.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            return Handler

        self._servers = [ThreadingHTTPServer(("127.0.0.1", 0), handler(o)) for o in "ab"]
        self.host_a, self.host_b = (f"127.0.0.1:{s.server_address[1]}" for s in self._servers)
        self.a, self.b = f"http://{self.host_a}", f"http://{self.host_b}"

    def expire(self) -> None:
        self.sessions.clear()

    def __enter__(self) -> "FakeSite":
        for server in self._servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_: object) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()


def form(action: str, error: str = "") -> str:
    alert = f"<p role=alert>{error}</p>" if error else ""
    return (
        f"<h1>Sign in</h1>{alert}<form method=post action={action}>"
        "<label>Username <input name=username autocomplete=username></label>"
        "<label>Password <input type=password name=password></label>"
        "<button>Sign in</button></form>"
    )


def code_form(action: str, ask: str) -> str:
    return (
        f"<form method=post action={action}><label for=code>{ask}</label>"
        "<input id=code name=code inputmode=numeric autocomplete=one-time-code>"
        "<button>Verify</button></form>"
    )


def cookie(name: str, value: str) -> dict[str, str]:
    return {"Set-Cookie": f"{name}={value}; Path=/; HttpOnly"}


@pytest.fixture
def site() -> Iterator[FakeSite]:
    with FakeSite() as s:
        yield s


# What needs no browser


def test_urls_are_allowed_only_on_the_logins_domains() -> None:
    domains = ["shop.example.com", "*.myshop.com", "127.0.0.1:8080"]
    assert br.allowed("https://shop.example.com/login", domains)
    assert br.allowed("https://shop.example.com:8443/", domains)  # no port: any port
    assert br.allowed("https://admin.myshop.com/", domains)
    assert not br.allowed("https://myshop.com/", domains)  # strict subdomains only
    assert not br.allowed("https://evilmyshop.com/", domains)
    assert not br.allowed("http://shop.example.com/", domains)  # plain http off loopback
    assert br.allowed("http://127.0.0.1:8080/x", domains)
    assert not br.allowed("http://127.0.0.1:8081/x", domains)
    assert not br.allowed("https://shop.example.com.evil.org/", domains)
    assert not br.allowed("file:///etc/passwd", domains)
    assert not br.allowed("javascript:alert(1)", domains)


def test_totp_matches_rfc_6238() -> None:
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238's SHA1 key, base32
    assert br.totp(secret, 59) == "287082"
    assert br.totp(secret, 1111111109) == "081804"


def test_a_cookie_is_the_logins_when_its_site_is_sent_it() -> None:
    login = br.Login("x", ["admin.myshop.com", "*.shop.org"], "u", "pw1234", None, None)
    assert login.owns_cookie("admin.myshop.com") and login.owns_cookie(".myshop.com")
    assert login.owns_cookie("a.shop.org") and not login.owns_cookie("evil.com")


# The installed package, over stdio MCP


@pytest.fixture(scope="session")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real install for every test worker (its Chromium is a large download), from a Default
    List naming this folder: the packages folder holding it."""
    shipped = b"".join((HERE / n).read_bytes() for n in SHIPPED)
    # pytest keeps the base folder's parent across runs: a changed package installs anew
    shared = tmp_path_factory.getbasetemp().parent / f"browser-{hashlib.sha1(shipped).hexdigest()}"
    shared.mkdir(exist_ok=True)
    data = shared / "data"
    with (shared / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (data / "packages" / "browser" / VERSION / ".swarmify-install.json").is_file():
            for left in (data, shared / "list"):  # what a failed install left
                shutil.rmtree(left, ignore_errors=True)
            data.mkdir()
            listed = default_list(shared / "list", {"browser": HERE})
            with closing(connect(data / "swarmify.db")) as conn:
                migrate(conn, KERNEL_MIGRATIONS, MIGRATIONS)
            connectors = Connectors(
                SqliteStorage(data / "swarmify.db"), FakeTransport(), Credentials(data / ".env"), 0
            )
            connectors.packages.repo = data / "repo"
            with pytest.MonkeyPatch.context() as m:
                m.setenv("SWARMIFY_DEFAULT_PACKAGES", str(listed))
                assert "browser" not in connectors.install_defaults()  # off by default
    assert (data / "packages" / "browser" / VERSION / ".browsers").is_dir()
    return data / "packages"


def login_env(
    site: FakeSite, home: Path, totp: bool = False, session: str | None = None
) -> dict[str, str]:
    if session is not None:
        (home / "login-session.json").write_text(session)
    return {
        "SWARMIFY_LOGIN_NAME": "shop_admin",
        "SWARMIFY_LOGIN_DOMAINS": site.host_a,
        "SWARMIFY_LOGIN_USERNAME": USER,
        "SWARMIFY_LOGIN_PASSWORD": PASSWORD,
        **({"SWARMIFY_LOGIN_TOTP": TOTP_SECRET} if totp else {}),
        "SWARMIFY_LOGIN_SESSION": str(home / "login-session.json"),
    }


@contextmanager
def served(
    installed: Path, tmp_path: Path, env: dict[str, str]
) -> Iterator[Callable[..., dict[str, Any]]]:
    """The package's server with the login's values in its environment, HOME its scratch folder
    and a stage folder (`tmp_path/world/runs/r/s`) to record into. Ends (recording finished)
    when the block does."""
    home, world = tmp_path / "home", tmp_path / "world"
    home.mkdir(parents=True, exist_ok=True)
    (world / "runs" / "r" / "s").mkdir(parents=True, exist_ok=True)
    stage = {"SWARMIFY_STAGE_DIR": str(world / "runs" / "r" / "s")}
    folder = installed / "browser" / VERSION
    package = Connector(
        "browser", "w", "Browser", "tool", [".venv/bin/python", "browser.py"], folder=str(folder)
    )
    with McpTransport().session(package, {"HOME": str(home), **stage, **env}) as call:
        yield lambda tool, **args: call(tool, args)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def stage_files(tmp_path: Path) -> list[Path]:
    return [p for p in (tmp_path / "world" / "runs").rglob("*") if p.is_file()]


@contextmanager
def person(home: Path, *answers: dict[str, str]) -> Iterator[list[dict[str, Any]]]:
    """Answers each Person Check request the process writes in HOME's `asks/` with the next of
    `answers`, as the core does once a person has; the requests, as they came."""
    asked: list[dict[str, Any]] = []
    stop = threading.Event()

    def watch() -> None:
        left = list(answers)
        while not stop.is_set():
            for request in sorted((home / "asks").glob("*.json")):
                if request.name.endswith(".answer.json") or any(
                    a["id"] == request.stem for a in asked
                ):
                    continue
                asked.append(json.loads(request.read_text()))
                answer = request.with_name(f"{request.stem}.answer.json")
                answer.write_text(json.dumps(left.pop(0)))
            time.sleep(0.05)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield asked
    finally:
        stop.set()
        thread.join()


def secrets_in(texts: list[str], *values: str) -> list[str]:
    return [v for v in values if any(v in t for t in texts)]


def test_sign_in_fills_reuses_its_session_and_replaces_a_stale_one(
    installed: Path, tmp_path: Path, home: Path, site: FakeSite
) -> None:
    with served(installed, tmp_path, login_env(site, home)) as call:
        said = call("sign_in", url=f"{site.a}/account")
        assert said["login_result"] == "signed_in", said
        account = call("read_text")["text"]
        assert f"pw={HIDDEN} sid={HIDDEN}" in account
        assert PASSWORD not in json.dumps(call("snapshot"))
    saved = (home / "login-session.out.json").read_text()
    (sid,) = (c["value"] for c in json.loads(saved)["cookies"] if c["name"] == "sid")
    assert sid in site.sessions
    assert (home / "login-session.out.json").stat().st_mode & 0o777 == 0o600

    # The next process is handed that session: already signed in, nothing typed
    posts = len(site.posts)
    with served(installed, tmp_path / "2", login_env(site, home, session=saved)) as call:
        assert call("sign_in", url=f"{site.a}/account")["login_result"] == "reused_session"
    assert len(site.posts) == posts

    # The site forgot it: a fresh sign-in replaces it
    site.expire()
    with served(installed, tmp_path / "3", login_env(site, home, session=saved)) as call:
        assert call("sign_in", url=f"{site.a}/account")["login_result"] == "signed_in"
    fresh = json.loads((home / "login-session.out.json").read_text())["cookies"]
    assert [c["value"] for c in fresh if c["name"] == "sid"] != [sid]

    # A wrong password fails with the site's own words
    env = {**login_env(site, home), "SWARMIFY_LOGIN_PASSWORD": "Not-the-pa55word"}
    with served(installed, tmp_path / "4", env) as call:
        failed = call("sign_in", url=f"{site.a}/login")
    assert failed == {"login_result": "failed", "message": "Wrong username or password."}


def test_sign_in_never_types_on_another_origin_or_a_frame_of_one(
    installed: Path, tmp_path: Path, home: Path, site: FakeSite
) -> None:
    with served(installed, tmp_path, login_env(site, home)) as call:
        refused = call("sign_in", url=f"{site.b}/login")
        assert refused["login_result"] == "refused_origin"
        with pytest.raises(ConnectorError, match="is not on this stage's sites"):
            call("open", url=f"{site.b}/login")
        with pytest.raises(ConnectorError, match="is not on this stage's sites"):
            call("open", url="https://evil.example.org/")
        for link in ("Partner", "Framed", "Embed"):  # a page that took the agent elsewhere
            call("open", url=f"{site.a}/")
            tree = call("snapshot")["snapshot"]
            ref = next(line for line in tree.splitlines() if f'"{link}"' in line)
            call("click", ref=ref.split("[ref=")[1].split("]")[0])
            said = call("sign_in")
            assert said["login_result"] == "refused_origin", (link, said)
            assert "nothing was typed" in said["message"]
    assert site.posts == []  # no form was submitted anywhere
    assert not (home / "login-session.out.json").exists()


def test_totp_is_computed_and_a_code_it_cannot_make_asks_a_person(
    installed: Path, tmp_path: Path, home: Path, site: FakeSite
) -> None:
    site.second = "totp"
    with served(installed, tmp_path, login_env(site, home, totp=True)) as call:
        assert call("sign_in", url=f"{site.a}/account")["login_result"] == "signed_in"
    assert not (home / "asks").exists()  # no person needed

    site.second = "sms"
    scratch = tmp_path / "2" / "home"  # the second process's HOME
    scratch.mkdir(parents=True)
    with (
        person(scratch, {"code": SMS}) as asked,
        served(installed, tmp_path / "2", login_env(site, home)) as call,
    ):
        said = call("sign_in", url=f"{site.a}/account")
        assert said["login_result"] == "signed_in", said
        text = call("read_text")["text"]
    (request,) = asked
    assert (request["kind"], request["ask"]) == ("code", "Enter the code we sent to ...67")
    assert Path(request["screenshot"]).read_bytes().startswith(b"\x89PNG")
    assert Path(request["screenshot"]).is_relative_to(scratch)
    assert HIDDEN in text
    log = (tmp_path / "2" / "world/runs/r/s/browser/actions.jsonl").read_text()
    assert secrets_in([log, json.dumps(said)], SMS, PASSWORD) == []


def test_a_captcha_asks_for_a_look_and_stop_ends_the_sign_in(
    installed: Path, tmp_path: Path, home: Path, site: FakeSite
) -> None:
    answers = ({"action": "try_again"}, {"action": "stop"})
    with (
        person(home, *answers) as asked,
        served(installed, tmp_path, login_env(site, home)) as call,
    ):
        with pytest.raises(ConnectorError, match="You stopped the sign-in check"):
            call("sign_in", url=f"{site.a}/captcha")
    assert [a["kind"] for a in asked] == ["look", "look"]
    assert site.posts == []


def test_it_records_and_downloads_into_the_stage_folder_and_leaks_no_secret(
    installed: Path, tmp_path: Path, home: Path, site: FakeSite
) -> None:
    with served(installed, tmp_path, login_env(site, home)) as call:
        call("open", url=f"{site.a}/login")
        tree = call("snapshot")["snapshot"]
        ref = next(line for line in tree.splitlines() if "Password" in line and "textbox" in line)
        call("type", ref=ref.split("[ref=")[1].split("]")[0], text="typed-by-the-agent")
        assert "typed-by-the-agent" not in call("snapshot")["snapshot"]  # [hidden]
        assert call("sign_in")["login_result"] == "signed_in"
        tree = call("snapshot")["snapshot"]
        export = next(line for line in tree.splitlines() if '"Export"' in line)
        got = call("download", ref=export.split("[ref=")[1].split("]")[0])
        shot = call("screenshot", name=f"after {PASSWORD} sign-in")
        call("scroll", direction="down")
        call("press", key="End")
        assert call("wait_for", text="Account", seconds=2)["found"] is True
        call("back")
    stage = tmp_path / "world/runs/r/s"
    assert Path(got["path"]) == stage / "browser/downloads/orders.csv"
    assert Path(got["path"]).read_text() == "order,total\n1,9.50\n"
    assert Path(shot["path"]).parent == stage / "browser" and PASSWORD not in shot["path"]
    files = stage_files(tmp_path)
    names = sorted(p.name for p in files)
    assert any(n.endswith(".webm") for n in names), names
    assert sum(n.startswith("page-") and n.endswith(".png") for n in names) >= 3, names
    assert not [n for n in names if n.endswith((".zip", ".har", ".trace", ".network"))], names
    actions = [
        json.loads(line) for line in (stage / "browser/actions.jsonl").read_text().splitlines()
    ]
    assert [a["tool"] for a in actions][:4] == ["open", "snapshot", "type", "snapshot"]
    assert actions[2]["args"]["text"] == "[hidden]"
    assert {a["status"] for a in actions if a["tool"] == "sign_in"} == {"signed_in"}
    session = json.loads((home / "login-session.out.json").read_text())
    cookies = [c["value"] for c in session["cookies"]]
    for f in files:
        data = f.read_bytes()
        assert not [s for s in (PASSWORD, *cookies) if s.encode() in data], f
        assert not secrets_in([str(f)], PASSWORD, *cookies), f


def test_without_a_login_it_browses_only_its_own_loopback(
    installed: Path, tmp_path: Path, site: FakeSite
) -> None:
    with served(installed, tmp_path, {}) as call:
        with pytest.raises(ConnectorError, match="sign_in needs a login"):
            call("sign_in", url=f"{site.a}/login")
        with pytest.raises(ConnectorError, match="is not on this stage's sites"):
            call("open", url=f"{site.a}/login")


# In the real sandbox, behind a live API


def test_a_connector_stage_signs_in_in_the_sandbox_and_nothing_leaks(
    installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site: FakeSite
) -> None:
    monkeypatch.setenv("SWARMIFY_LOGIN_ALLOW_LOOPBACK", "1")  # the fake site is on loopback
    data = tmp_path / "data"
    data.mkdir()
    (data / "packages").symlink_to(installed)  # installed already: only remembered
    with TestClient(create_app(data, poll_seconds=0.05, retro=False)) as client:
        w = client.post("/api/worlds", json={"name": "Shop"}).json()["id"]
        enabled = client.put(f"/api/worlds/{w}/packages/browser", json={"version": VERSION})
        assert enabled.is_success, enabled.text
        body = {
            "name": "shop_admin",
            "domains": [site.host_a],
            "username": USER,
            "password": PASSWORD,
        }
        assert client.post(f"/api/worlds/{w}/logins", json=body).status_code == 201

        def stage(url: str) -> dict[str, Any]:
            yaml = (
                "name: Sign\nstages:\n  - connector: sign\n    calls: browser\n"
                f"    params: {{login: shop_admin, tool: sign_in, url: '{url}'}}\n"
            )
            saved = client.post(f"/api/worlds/{w}/workflows", json={"yaml": yaml})
            assert saved.status_code == 201, saved.text
            path = f"/api/workflows/{saved.json()['workflow_id']}/runs"
            run = client.post(path, json={}).json()
            deadline = time.monotonic() + 120
            while (run := client.get(f"/api/runs/{run['id']}").json())["status"] not in (
                "done",
                "failed",
            ):
                assert time.monotonic() < deadline, run
                for check in client.get("/api/person-checks").json():
                    client.post(f"/api/person-checks/{check['id']}", json={"code": SMS})
                time.sleep(0.1)
            return run

        def said(run: dict[str, Any]) -> dict[str, Any]:
            assert run["status"] == "done", run
            return client.get(f"/api/stages/{run['stages'][0]['id']}/call").json()["response"]

        assert said(stage(f"{site.a}/account"))["login_result"] == "signed_in"
        assert said(stage(f"{site.a}/account"))["login_result"] == "reused_session"
        assert said(stage(f"{site.b}/login"))["login_result"] == "refused_origin"
        site.second, posts = "sms", len(site.posts)
        site.expire()
        assert said(stage(f"{site.a}/account"))["login_result"] == "signed_in"
        assert ("a", "/sms") in site.posts[posts:]  # the person's code went to the site

        uses = client.get(f"/api/worlds/{w}/logins/shop_admin/uses").json()
        assert [u["result"] for u in uses][::-1] == [
            "signed_in", "reused_session", "refused_origin", "signed_in",
        ]  # fmt: skip
    assert ("b", "/login") not in site.posts
    runs = data / "worlds" / w / "runs"
    assert list(runs.rglob("*.webm")) and list(runs.rglob("actions.jsonl"))
    with closing(connect(data / "swarmify.db")) as conn:
        events = json.dumps([list(r) for r in conn.execute("SELECT payload FROM events")])
    values = (PASSWORD, SMS, *site.sessions)
    assert secrets_in([events], *values) == []
    for f in runs.rglob("*"):
        assert not f.is_file() or not secrets_in([f.read_bytes().decode("latin-1")], *values), f
    assert not list((data / "sandbox").rglob("login*.json"))
