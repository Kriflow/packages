# browser

The browser connector: a headless Chromium that agents drive with tools on sites that have no
API, signing in with a World Login they never see. It is our own MCP server over Playwright
(ADR 0015), not a vendor's, and a Connector Package (ADR 0019) that accepts a World Login
(`[login] accepts = true`). It is not enabled by default: a world turns it on on the Connectors
page. Everything Kriflow knows of it is `kriflow-connector.toml`.

## Setup

Nothing to fill in. The package needs no Credentials; the sign-in details come from a **World
Login** the world keeps on its **Settings** page (name, domains, username, password and,
optionally, the authenticator secret). A stage names the login:

```yaml
- swarm: restock
  goal: Download this week's orders from the shop admin
  tools: [browser]
  login: shop_admin

- connector: sign_in
  calls: browser
  params: {login: shop_admin, tool: sign_in, url: "https://admin.myshop.com/"}
```

Only that stage's browser gets the login, and its sandbox reaches only the login's domains
(plus `localhost`, the package's own). With the authenticator secret saved, sign-in needs no
person; without it, a site that sends a code by SMS or email stops the run at a **Person Check**
(Needs you) where you type the code within 10 minutes.

Installing it downloads Playwright's Chrome Headless Shell and its ffmpeg (about 100 MB) into the
version's folder, from `cdn.playwright.dev` (which serves Chrome for Testing from
`storage.googleapis.com`) or `playwright.download.prss.microsoft.com`. `install_chromium.py`
fetches the files Playwright names itself, resuming a download the sandbox's proxy cuts short
(Playwright's own downloader fails on those), then lets Playwright check the result. Kriflow's
image already carries the shared libraries Chrome needs, as the render packages use them.

## Tools

- `sign_in(url?)`: opens `url` (or uses the current page) with the login's Saved Session loaded,
  then: no password field on the page answers `reused_session`; a password field on a page, or
  in a frame, whose origin is not one of the login's domains (or plain `http` off loopback)
  answers `refused_origin` and types nothing; otherwise it fills the username and password,
  submits, fills an authenticator code it computes (RFC 6238) or asks a person for one, and
  answers `signed_in`, or `failed` with the site's error text. A captcha asks a person to look
  (Try again or Stop). Its answer carries `login_result`, which the core records as the
  `login.used` event, and the session is saved after it.
- `open(url)` and `back()`: a URL outside the login's domains (and `localhost`) is refused before
  the sandbox would block it.
- `snapshot()`: the page's accessibility tree with element refs (`e12`); password inputs read
  `[hidden]`. `read_text(ref?)`: the visible text of the page or an element.
- `click(ref)`, `type(ref, text, submit?)`, `select(ref, value)`, `press(key)`,
  `scroll(direction)`, `wait_for(text?, seconds?)`.
- `screenshot(name)`: a PNG in the stage's `browser/` folder. `download(ref)`: clicks and saves
  the file in `browser/downloads/`. Both answer the path.

Not offered, on purpose: running JavaScript, reading cookies or storage, tracing, HAR export and
file upload, so an agent has no path to the session or the password.

## What it keeps

- **Secrets in answers:** every answer, error and recording text has the password, the
  authenticator secret, each session cookie value and any code a person typed replaced by
  `[secret: <login>]` before it leaves the process; the core scrubs again.
- **Recording:** in the stage's `browser/` folder, a WebM video per page, a PNG after each page
  change and `actions.jsonl` (tool, arguments with text typed into a password input as
  `[hidden]`, URL, time, result). Never a Playwright trace or a network log, which would keep
  the posted password and the cookies. Pixels are not scrubbed: a password field shows dots, a
  typed one-time code may show for its 30 s life.
- **Saved Session:** after a sign-in, and when the process ends, the login's cookies and
  storage (Playwright's storage state, only for the login's domains) go to
  `login-session.out.json` beside the session the core handed in; the core keeps it as a secret
  of the login, and a session the site rejects is replaced by a fresh sign-in.
- **Person Check:** a request file `asks/<id>.json` (`{id, kind: code | look, ask, screenshot}`)
  in the process's HOME, with its screenshot there; the core opens the check and writes
  `asks/<id>.answer.json`, which the process polls. Stop, or no answer in 10 minutes, fails the
  stage.

Its tests (`test_browser.py`) run against a fake site on two loopback origins: the package
installed through the real install path, driven over stdio MCP, and once more in the real
sandbox behind a live API. No real login is used.
