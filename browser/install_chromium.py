"""The install step's second half: Playwright's Chrome Headless Shell and its ffmpeg (for the
videos) into `.browsers/` of this folder, for this platform (the box: Linux amd64).

Playwright's own downloader loses the last bytes of a large file when the sandbox's proxy closes
the connection (seen on 2026-11-22: most tries of the 100 MB headless shell failed with a size
mismatch, while curl through the same proxy got it whole). So each file Playwright names
(`playwright install --dry-run`) is fetched here, resuming with a Range request where a try was
cut short, unpacked with its file modes, and marked installed as Playwright marks it. Playwright's
own install runs last: it finds everything in place, or fetches what is still missing.
"""

import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx

HERE = Path(__file__).parent
ENV = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(HERE / ".browsers")}
INSTALL = [str(HERE / ".venv" / "bin" / "playwright"), "install", "--only-shell", "chromium"]
TRIES = 8
MARKER = "INSTALLATION_COMPLETE"  # what Playwright writes once a browser is unpacked


def fetch(urls: list[str], to: Path) -> None:
    """`to` whole from the first of `urls` that answers, resuming a cut-short try."""
    to.unlink(missing_ok=True)
    total = None
    for attempt in range(TRIES):
        have = to.stat().st_size if to.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream(
                "GET", urls[attempt % len(urls)], headers=headers, follow_redirects=True,
                timeout=60,
            ) as r:  # fmt: skip
                r.raise_for_status()
                if have and r.status_code != 206:  # no resume here: start over
                    have = 0
                total = have + int(r.headers["content-length"])
                with to.open("ab" if have else "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
        except httpx.HTTPError as exc:
            print(f"try {attempt + 1}: {exc}", file=sys.stderr)
        if total is not None and to.stat().st_size == total:
            return
        time.sleep(1)
    raise SystemExit(f"could not download {urls[0]} whole in {TRIES} tries")


def unpack(archive: Path, folder: Path) -> None:
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            path = z.extract(info, folder)
            if mode := (info.external_attr >> 16) & 0o777:
                os.chmod(path, mode)


def main() -> None:
    plan = subprocess.run(
        [*INSTALL, "--dry-run"], env=ENV, capture_output=True, text=True, check=True
    ).stdout
    for block in plan.split("\n\n"):
        where = re.search(r"Install location:\s+(\S+)", block)
        urls = re.findall(r"Download (?:url|fallback \d+):\s+(\S+)", block)
        if not where or not urls:
            continue
        folder = Path(where[1])
        if (folder / MARKER).is_file():
            continue
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder.with_suffix(".zip")
        fetch(urls, archive)
        unpack(archive, folder)
        archive.unlink()
        (folder / MARKER).touch()
    subprocess.run(INSTALL, env=ENV, check=True)


if __name__ == "__main__":
    main()
