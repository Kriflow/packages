"""The browser connector: a stdio MCP server over Playwright's headless Chromium (ADR 0015) that
agents drive with tools, and that signs in with a World Login it never shows them (spec: World
Logins And Browser Connector).

The core hands a stage's login to this process alone, as `KRIFLOW_LOGIN_NAME`, `_DOMAINS`
(comma-separated), `_USERNAME`, `_PASSWORD`, `_TOTP` (when set) and `_SESSION`, the path of its
Saved Session (Playwright's storage state); a core from before the rename names them
`SWARMIFY_LOGIN_*` alone, which it still reads. `sign_in` fills the username, password and second
factor itself, and only on a page whose origin, and every frame holding the form, is one of the
login's domains (plain http only on loopback): an injected instruction cannot send the password
elsewhere. After a sign-in, and when the process ends, the session goes to
`login-session.out.json` beside the one it was handed, which the core keeps for the next run.
A code the box cannot make (SMS, email) or a captcha asks a person: a request file in `asks/`
of its HOME, which the core turns into a Person Check, and an answer file it polls for.

Every tool's answer, error and recording text has each secret value (the password, the TOTP
secret, a session cookie, a code a person typed) replaced by `[secret: <login>]`, and a password
input reads `[hidden]`. There is no tool that runs JavaScript, reads cookies or storage, traces
or exports a HAR. Each process records to its stage's `browser/` folder: a WebM video per page,
a PNG after each navigation and `actions.jsonl`, one line per tool call.

It is a Connector Package (ADR 0019): `kriflow-connector.toml` beside it is everything Kriflow
knows of it.
"""

import asyncio
import base64
import functools
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from playwright.async_api import Browser, BrowserContext, Frame, Locator, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

HERE = Path(__file__).parent
if (HERE / ".browsers").is_dir():  # the install step's Chromium, not the user's cache
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(HERE / ".browsers")

VIEWPORT = {"width": 1280, "height": 800}
SESSION_OUT = "login-session.out.json"
ASK_SECONDS = 660.0  # the core stops a Person Check after 10 minutes and answers `stop`
MIN_SECRET = 6  # as the core: a shorter value would mangle ordinary text
COOKIE_MIN = 8  # the session cookie values scrubbed, as the core's
TEXT_MAX = 20_000
STEPS = 6  # forms one sign-in may pass: password, codes, a captcha retried
DECLARED = ("localhost",)  # the Manifest's own network
PASSWORD = ("input[type=password]",)
USERNAME = tuple(
    f"input{s}"
    for s in (
        "[autocomplete=username]",
        "[type=email]",
        "[name*=user i]",
        "[name*=email i]",
        "[name*=login i]",
        "[id*=user i]",
        "[id*=email i]",
        "[type=text]",
        ":not([type])",
    )
)
CODE = tuple(
    f"input{s}"
    for s in (
        "[autocomplete=one-time-code]",
        "[name*=otp i]",
        "[name*=totp i]",
        "[name*=code i]",
        "[id*=otp i]",
        "[id*=code i]",
    )
)
CAPTCHA = (
    "[data-sitekey], .g-recaptcha, .h-captcha, .cf-turnstile, iframe[src*=captcha i],"
    " iframe[src*='challenges.cloudflare.com']",
)
ERROR_TEXT = ("[role=alert]", ".error", ".alert", ".flash")
REF = re.compile(r"^[a-z0-9]{1,16}$")
NAME = re.compile(r"[^A-Za-z0-9_-]+")


def totp(secret: str, at: float | None = None) -> str:
    """The six-digit code for a base32 `secret` at `at` (now): RFC 6238, HMAC-SHA1, 30 s."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = struct.pack(">Q", int((time.time() if at is None else at) // 30))
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def _host_port(netloc_or_domain: str) -> tuple[str, int | None]:
    parts = urlsplit(f"//{netloc_or_domain}")
    return (parts.hostname or "").lower(), parts.port


def loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def allowed(url: str, domains: list[str]) -> bool:
    """Whether `url` is on one of `domains` (an exact host with an optional port, or
    `*.example.com` for its strict subdomains, as the sandbox reads them), over https, or over
    plain http on a loopback host only. A domain without a port matches any port."""
    parts = urlsplit(url)
    host, port = (parts.hostname or "").lower(), parts.port
    if parts.scheme not in ("http", "https") or not host:
        return False
    if parts.scheme == "http" and not loopback(host):
        return False
    port = port or (443 if parts.scheme == "https" else 80)
    for domain in domains:
        want, want_port = _host_port(domain.strip().lower())
        if want_port is not None and want_port != port:
            continue
        if want.startswith("*."):
            if host.endswith(want[1:]) and host != want[2:]:
                return True
        elif host == want:
            return True
    return False


def _env(suffix: str) -> str | None:
    """A variable the core gives this process: `KRIFLOW_<suffix>`, else `SWARMIFY_<suffix>`, which
    cores from before the rename give alone (Kriflow/kriflow#595 sets both)."""
    return os.environ.get(f"KRIFLOW_{suffix}") or os.environ.get(f"SWARMIFY_{suffix}")


@dataclass
class Login:
    name: str
    domains: list[str]
    username: str
    password: str
    totp: str | None
    session: Path | None

    @classmethod
    def from_env(cls) -> "Login | None":
        if not (password := _env("LOGIN_PASSWORD")):
            return None
        session = _env("LOGIN_SESSION")
        return cls(
            _env("LOGIN_NAME") or "login",
            [d for d in (_env("LOGIN_DOMAINS") or "").split(",") if d.strip()],
            _env("LOGIN_USERNAME") or "",
            password,
            _env("LOGIN_TOTP") or None,
            Path(session) if session else None,
        )

    def owns_cookie(self, cookie_domain: str) -> bool:
        """A cookie that some host of the login's domains is sent: its own, or a parent's."""
        d = cookie_domain.lstrip(".").lower()
        for domain in self.domains:
            host = _host_port(domain.strip().lower())[0].removeprefix("*.")
            if host == d or host.endswith("." + d) or d.endswith("." + host):
                return True
        return False


def _read_session(path: Path | None) -> dict[str, Any] | None:
    """The Saved Session as Playwright's storage state, or None when there is none usable."""
    if path is None or not path.is_file():
        return None
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        return None
    return {"cookies": state["cookies"], "origins": state.get("origins") or []}


def _recording_folder() -> Path:
    """Its stage's `browser/` folder (`KRIFLOW_STAGE_DIR`, package contract 2), or HOME's when
    it runs for no stage or may not write there (a swarm stage's server without a login)."""
    home = Path(os.environ.get("HOME", ".")) / "browser"
    if stage := _env("STAGE_DIR"):
        try:
            (Path(stage) / "browser").mkdir(exist_ok=True)
            return Path(stage) / "browser"
        except OSError:
            pass  # a read-only stage folder
    home.mkdir(parents=True, exist_ok=True)
    return home


def _proxy(login: Login | None) -> dict[str, str] | None:
    """The sandbox's proxy, which decides every connection Chromium makes. Loopback skips it, as
    for every package declaring `localhost`, unless the login itself is on loopback (the fake
    site of the tests)."""
    server = next(
        (os.environ[k] for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
         if os.environ.get(k)),
        None,
    )  # fmt: skip
    if server is None:
        return None
    on_loopback = login and any(loopback(_host_port(d)[0]) for d in login.domains)
    if not on_loopback:
        os.environ["PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK"] = "1"
    parts = urlsplit(server)  # Chromium takes the proxy's credentials apart from its URL
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy |= {"username": unquote(parts.username), "password": unquote(parts.password or "")}
    return proxy


@dataclass
class Session:
    """The process's one browser context and what it records."""

    login: Login | None = field(default_factory=Login.from_env)
    secrets: set[str] = field(default_factory=set)
    context: BrowserContext | None = None
    browser: Browser | None = None
    playwright: Any = None
    folder: Path | None = None
    """Its stage's `browser/` folder (HOME's when it runs for no stage)."""
    shots: int = 0
    last_url: str = ""
    signed: bool = False
    """A sign-in worked (or its session did): the session is saved again when the process ends."""

    def __post_init__(self) -> None:
        if self.login:
            self.secrets |= {self.login.password, *([self.login.totp] if self.login.totp else [])}
            state = _read_session(self.login.session) or {"cookies": []}
            self._cookies(state["cookies"])

    @property
    def hidden(self) -> str:
        return f"[secret: {self.login.name if self.login else 'login'}]"

    def _cookies(self, cookies: list[Any]) -> None:
        for c in cookies:
            value = c.get("value") if isinstance(c, dict) else None
            if isinstance(value, str) and len(value) >= COOKIE_MIN:
                self.secrets.add(value)

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for s in sorted((s for s in self.secrets if len(s) >= MIN_SECRET), key=len)[::-1]:
                value = value.replace(s, self.hidden)
            return value
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        return value

    async def start(self) -> Page:
        if self.context is None:
            self.folder = _recording_folder()
            proxy = _proxy(self.login)  # before the driver starts: it reads the environment
            self.playwright = await async_playwright().start()
            # a sandbox's /dev may have no shared memory folder: Chromium keeps it in its temp
            self.browser = await self.playwright.chromium.launch(
                proxy=proxy,  # type: ignore[arg-type]
                args=["--disable-dev-shm-usage"],
            )
            self.context = await self.browser.new_context(
                viewport=VIEWPORT,  # type: ignore[arg-type]
                storage_state=_read_session(self.login.session) if self.login else None,  # type: ignore[arg-type]
                record_video_dir=self.folder,
                record_video_size=VIEWPORT,  # type: ignore[arg-type]
                accept_downloads=True,
            )
        return self.context.pages[-1] if self.context.pages else await self.context.new_page()

    @property
    def page(self) -> Page:
        if self.context is None or not self.context.pages:
            raise ToolError("no page is open: open a URL first")
        return self.context.pages[-1]

    async def refresh_secrets(self) -> None:
        """The login's cookies as they are now, so a new session cookie is scrubbed too."""
        if self.context is not None and self.login:
            cookies = await self.context.cookies()
            self._cookies([c for c in cookies if self.login.owns_cookie(c.get("domain", ""))])

    async def save_session(self) -> None:
        """The login's part of the browser's storage state, for the core to keep."""
        if self.context is None or self.login is None:
            return
        state = await self.context.storage_state()
        kept = {
            "cookies": [c for c in state["cookies"] if self.login.owns_cookie(c["domain"])],
            "origins": [o for o in state["origins"] if allowed(o["origin"], self.login.domains)],
        }
        self._cookies(kept["cookies"])
        home = Path(os.environ.get("HOME", "."))
        out = (self.login.session.parent if self.login.session else home) / SESSION_OUT
        tmp = out.with_name(f".{SESSION_OUT}.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(kept, f)
        tmp.replace(out)

    async def record(self, tool: str, args: dict[str, Any], status: str) -> None:
        """A PNG when the page changed, and the call's line in `actions.jsonl`."""
        if self.folder is None:
            return
        url = ""
        if self.context is not None and self.context.pages:
            page = self.context.pages[-1]
            url = page.url
            if url != self.last_url:
                self.last_url = url
                self.shots += 1
                try:
                    await page.screenshot(path=self.folder / f"page-{self.shots:03d}.png")
                except PlaywrightError:
                    pass  # a page closing as it is shot
        line = {
            "at": datetime.now(UTC).isoformat(),
            "tool": tool,
            "args": self.scrub(args),
            "url": self.scrub(url),
            "status": self.scrub(status),
        }
        with (self.folder / "actions.jsonl").open("a") as f:
            f.write(json.dumps(line) + "\n")

    async def close(self) -> None:
        """Save the session once more, then close the context (which finishes the videos)."""
        try:
            if self.signed:
                await self.save_session()
        finally:
            if self.context is not None:
                await self.context.close()
            if self.browser is not None:
                await self.browser.close()
            if self.playwright is not None:
                await self.playwright.stop()
            self.context = self.browser = self.playwright = None


S = Session()


@asynccontextmanager
async def _lifespan(_: MCPServer) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await S.close()


mcp = MCPServer("browser", lifespan=_lifespan)


def tool(description: str) -> Callable[[Callable[..., Awaitable[dict[str, Any]]]], Any]:
    """A tool whose answer and error are scrubbed and whose call is recorded. Typed text into a
    password input is logged as `[hidden]` (the tool sets `_hidden`)."""

    def wrap(fn: Callable[..., Awaitable[dict[str, Any]]]) -> Any:
        @functools.wraps(fn)
        async def run(**kwargs: Any) -> dict[str, Any]:
            args = {k: v for k, v in kwargs.items() if v not in ("", None)}
            status = "ok"
            try:
                await S.start()
                out = await fn(**kwargs)
                status = str(out.get("login_result", "ok"))
                if out.pop("_hidden", False):
                    args["text"] = "[hidden]"
                await S.refresh_secrets()
                return S.scrub(out)
            except ToolError as exc:
                status = f"refused: {exc}"
                raise ToolError(S.scrub(str(exc))) from None
            except PlaywrightError as exc:
                said = str(exc.message).splitlines()[0] if exc.message else "failed"
                status = f"failed: {said}"
                raise ToolError(S.scrub(f"{fn.__name__} failed: {said}")) from None
            finally:
                await S.record(fn.__name__, args, status)

        mcp.tool(description=description)(run)
        return run

    return wrap


def _domains() -> list[str]:
    return [*(S.login.domains if S.login else []), *DECLARED]


def _check_url(url: str) -> None:
    if not allowed(url, _domains()):
        raise ToolError(
            f"{url} is not on this stage's sites ({', '.join(_domains())}); the browser"
            " opens only those, over https (http on this box only)"
        )


def _locator(ref: str) -> Locator:
    if not REF.fullmatch(ref):
        raise ToolError(f"{ref!r} is not an element ref from snapshot, like e12")
    return S.page.locator(f"aria-ref={ref}")


async def _settle(page: Page) -> None:
    try:
        await page.wait_for_load_state("load", timeout=15_000)
    except PlaywrightError:
        pass


async def _visible(scope: Frame | Locator, selectors: tuple[str, ...]) -> Locator | None:
    """The first visible element of the first selector that finds one, in that order."""
    for selector in selectors:
        found = scope.locator(selector)
        for i in range(await found.count()):
            if await found.nth(i).is_visible():
                return found.nth(i)
    return None


async def _fields(page: Page, selectors: tuple[str, ...]) -> list[tuple[Frame, Locator]]:
    """The first visible field matching `selectors` in each frame that has one."""
    out = []
    for frame in page.frames:
        try:
            if (found := await _visible(frame, selectors)) is not None:
                out.append((frame, found))
        except PlaywrightError:
            continue  # a frame gone while looked at
    return out


def _foreign(page: Page, frames: list[Frame]) -> str | None:
    """The first page or frame holding the form that is not on the login's domains."""
    assert S.login is not None
    for url in (page.url, *(f.url for f in frames)):
        if not allowed(url, S.login.domains):
            return url
    return None


async def _error_text(page: Page) -> str:
    for frame in page.frames:
        if (said := await _visible(frame, ERROR_TEXT)) is not None:
            return (await said.inner_text()).strip()[:500]
    return ""


async def _ask(kind: str, ask: str) -> dict[str, Any]:
    """A Person Check: a request file in HOME's `asks/` with a screenshot, then the core's
    answer file, polled for: `{"code"}`, `{"action": "try_again"}` or `{"action": "stop"}`."""
    asks = Path(os.environ.get("HOME", ".")) / "asks"
    asks.mkdir(exist_ok=True)
    ask_id = secrets.token_hex(8)
    shot = asks / f"{ask_id}.png"
    await S.page.screenshot(path=shot)
    request = {"id": ask_id, "kind": kind, "ask": S.scrub(ask), "screenshot": str(shot)}
    tmp = asks / f".{ask_id}.tmp"
    tmp.write_text(json.dumps(request))
    tmp.replace(asks / f"{ask_id}.json")
    answer = asks / f"{ask_id}.answer.json"
    deadline = time.monotonic() + ASK_SECONDS
    while not answer.is_file():
        if time.monotonic() > deadline:
            raise ToolError("Nobody answered the sign-in check")
        await asyncio.sleep(0.25)
    got = json.loads(answer.read_text())
    if got.get("action") == "stop":
        raise ToolError("You stopped the sign-in check")
    if code := got.get("code"):
        S.secrets.add(str(code))
    return got


async def _label(field: Locator) -> str:
    """What a code field asks, as the site says it: its label, else its name for assistive
    technology, else its placeholder."""
    said = await field.evaluate(
        "e => (e.labels && e.labels[0] && e.labels[0].innerText)"
        " || e.getAttribute('aria-label') || e.placeholder || ''"
    )
    return str(said).strip() or "The site asks for a code."


async def _sign_in(page: Page) -> dict[str, Any]:
    assert S.login is not None
    login = S.login
    filled = typed_totp = asked_code = False
    for _ in range(STEPS):
        await _settle(page)
        if await _fields(page, CAPTCHA):
            got = await _ask("look", "The site is showing a check the box cannot pass.")
            if got.get("action") == "try_again":
                await page.reload()
            continue
        passwords = await _fields(page, PASSWORD)
        codes = [] if passwords else await _fields(page, CODE)
        if not passwords and not codes:
            break
        frames = [f for f, _ in passwords or codes]
        if (url := _foreign(page, frames)) is not None:
            origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
            return {"login_result": "refused_origin", "message": f"{origin} is not {login.name}'s"
                    f" site ({', '.join(login.domains)}): nothing was typed"}  # fmt: skip
        if passwords:
            if filled:
                said = await _error_text(page)
                return {"login_result": "failed", "message": said or "the site kept the form"}
            frame, password = passwords[0]
            form = password.locator("xpath=ancestor::form[1]")
            scope = form if await form.count() else frame
            if (user := await _visible(scope, USERNAME)) is not None and login.username:
                await user.fill(login.username)
            await password.fill(login.password)
            filled = True
            async with _navigating(page):
                await password.press("Enter")
            continue
        frame, code = codes[0]
        if login.totp and not typed_totp:
            value, typed_totp = totp(login.totp), True
        elif not asked_code:
            value, asked_code = str((await _ask("code", await _label(code)))["code"]), True
        else:
            said = await _error_text(page)
            return {"login_result": "failed", "message": said or "the site refused the code"}
        await code.fill(value)
        async with _navigating(page):
            await code.press("Enter")
    else:
        return {"login_result": "failed", "message": "the sign-in did not finish"}
    S.signed = True
    await S.save_session()
    if filled:
        return {"login_result": "signed_in", "url": page.url}
    return {"login_result": "reused_session", "url": page.url, "message": "already signed in"}


@asynccontextmanager
async def _navigating(page: Page) -> AsyncIterator[None]:
    """Wait for the navigation a submit starts, when it starts one."""
    try:
        async with page.expect_navigation(timeout=15_000):
            yield
    except PlaywrightError:
        pass


# The tools


@tool(
    "Sign in to the site of the stage's login, on the current page or after opening `url`. It "
    "fills the username, password and any second factor itself (you never see them), only on the "
    "login's own sites. Answers login_result: reused_session (already signed in), signed_in, "
    "refused_origin (the page is another site's; nothing was typed) or failed (with the site's "
    "message). A code it cannot make, or a captcha, waits for a person."
)
async def sign_in(url: str = "") -> dict[str, Any]:
    if S.login is None:
        raise ToolError("sign_in needs a login: name one in the stage, as login: shop_admin")
    if url and not allowed(url, S.login.domains):
        return {"login_result": "refused_origin", "message": f"{url} is not {S.login.name}'s"
                f" site ({', '.join(S.login.domains)}): nothing was opened or typed"}  # fmt: skip
    page = await S.start()
    if url:
        response = await page.goto(url)
        if response is None or response.status >= 400:
            said = f"answered {response.status}" if response else "did not answer"
            return {"login_result": "failed", "message": f"{url} {said}"}
    elif page.url in ("", "about:blank"):
        raise ToolError("open the site's page first, or give sign_in its url")
    return await _sign_in(page)


@tool("Open `url` (one of the stage's sites) in the browser. Answers its title and URL.")
async def open(url: str) -> dict[str, Any]:  # noqa: A001
    _check_url(url)
    page = await S.start()
    response = await page.goto(url)
    return {"url": page.url, "title": await page.title(), "status": response and response.status}


@tool("Go back one page.")
async def back() -> dict[str, Any]:
    page = S.page
    await page.go_back()
    return {"url": page.url, "title": await page.title()}


@tool(
    "The page as an accessibility tree, each element with a ref (e12) that click, type, select "
    "and download take. Password inputs read [hidden]."
)
async def snapshot() -> dict[str, Any]:
    page = S.page
    tree = await page.aria_snapshot(mode="ai")
    lines = tree.splitlines()
    for i, line in enumerate(lines):
        found = re.search(r"\btextbox\b.*\[ref=([a-z0-9]+)\]", line)
        if found and ":" in line.split("]")[-1]:
            kind = await page.locator(f"aria-ref={found[1]}").get_attribute("type")
            if (kind or "").lower() == "password":
                lines[i] = line[: line.rindex(":") + 1] + " [hidden]"
    return {"url": page.url, "title": await page.title(), "snapshot": "\n".join(lines)[:TEXT_MAX]}


@tool("The visible text of the page, or of one element by its ref.")
async def read_text(ref: str = "") -> dict[str, Any]:
    where = _locator(ref) if ref else S.page.locator("body")
    return {"url": S.page.url, "text": (await where.inner_text())[:TEXT_MAX]}


@tool("Click an element by its ref from snapshot.")
async def click(ref: str) -> dict[str, Any]:
    await _locator(ref).click()
    await _settle(S.page)
    return {"url": S.page.url, "title": await S.page.title()}


@tool("Type `text` into an input by its ref, replacing what it held; `submit` presses Enter.")
async def type(ref: str, text: str, submit: bool = False) -> dict[str, Any]:  # noqa: A001
    field_ = _locator(ref)
    hidden = (await field_.get_attribute("type") or "").lower() == "password"
    await field_.fill(text)
    if submit:
        await field_.press("Enter")
        await _settle(S.page)
    return {"url": S.page.url, "_hidden": hidden}


@tool("Choose the option `value` (its value or label) of a select by its ref.")
async def select(ref: str, value: str) -> dict[str, Any]:
    chosen = await _locator(ref).select_option(value)
    return {"url": S.page.url, "chosen": chosen}


@tool("Press a key on the page, as Enter, Escape, Tab or ArrowDown.")
async def press(key: str) -> dict[str, Any]:
    await S.page.keyboard.press(key)
    await _settle(S.page)
    return {"url": S.page.url}


@tool("Scroll the page by a screen: `direction` is down or up.")
async def scroll(direction: str = "down") -> dict[str, Any]:
    if direction not in ("down", "up"):
        raise ToolError("direction is down or up")
    await S.page.mouse.wheel(0, VIEWPORT["height"] * (1 if direction == "down" else -1))
    return {"url": S.page.url}


@tool("Wait until `text` shows on the page, or for `seconds` (at most 60) when no text is given.")
async def wait_for(text: str = "", seconds: float = 10) -> dict[str, Any]:
    seconds = max(0.0, min(float(seconds), 60.0))
    if not text:
        await asyncio.sleep(seconds)
        return {"url": S.page.url}
    try:
        await S.page.get_by_text(text).first.wait_for(timeout=seconds * 1000)
    except PlaywrightError:
        return {"url": S.page.url, "found": False}
    return {"url": S.page.url, "found": True}


@tool("Save a PNG of the page as `name` in the stage's browser/ folder. Answers its path.")
async def screenshot(name: str) -> dict[str, Any]:
    page = await S.start()
    clean = NAME.sub("-", S.scrub(name)).strip("-")[:64] or "screenshot"
    assert S.folder is not None
    path = S.folder / f"{clean}.png"
    await page.screenshot(path=path)
    return {"path": str(path)}


@tool("Click a link or button by its ref that downloads a file, and save it in the stage's "
      "browser/downloads/ folder. Answers its path.")  # fmt: skip
async def download(ref: str) -> dict[str, Any]:
    assert S.folder is not None
    async with S.page.expect_download(timeout=60_000) as started:
        await _locator(ref).click()
    got = await started.value
    folder = S.folder / "downloads"
    folder.mkdir(exist_ok=True)
    named = Path(S.scrub(got.suggested_filename))
    stem = NAME.sub("-", named.stem).strip("-")[:100] or "download"
    suffix = named.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", named.suffix) else ""
    path = folder / f"{stem}{suffix}"
    await got.save_as(path)
    return {"path": str(path)}


if __name__ == "__main__":
    mcp.run("stdio")
