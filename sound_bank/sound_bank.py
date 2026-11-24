#!/usr/bin/env python3
"""The Sound Bank's MCP server (README.md beside this file): fetch a sound from BigSoundBank or
Wikimedia Commons only (CC0 or public domain), or keep a made take (a vocal effect from the speech
connector), and land it in the world's Sound Bank as a Draft with an audio Demo.

    .venv/bin/python sound_bank.py mcp [<sounds folder>]

The Sound Bank is the `sounds` Owned Folder, $KRIFLOW_ASSETS/sounds. A Draft is the WAV (48 kHz
mono, silence trimmed, cut at `cut` s, default 6) plus its index.json entry with "draft": true,
source and licence; its Demo is sounds/demos/<name>.mp4 (a waveform with the sound) and .png. A
Template's kit refuses a Draft until it is approved. Any page, audio file or redirect off the two
sources, and any licence but CC0 or public domain, is refused. ffmpeg is imageio-ffmpeg's ($FFMPEG
overrides)."""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import wave
from pathlib import Path

HOSTS = {
    "bigsoundbank.com",
    "www.bigsoundbank.com",
    "commons.wikimedia.org",
    "upload.wikimedia.org",
}
UA = {"User-Agent": "sound-bank-fetch/1.0 (CC0 sound fetcher)"}
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def _ffmpeg():
    if os.environ.get("FFMPEG"):
        return os.environ["FFMPEG"]
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG = _ffmpeg()


class Refused(Exception):
    pass


def allowed(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or (u.hostname or "").lower() not in HOSTS:
        raise Refused(
            f"{url}: not an allowed source (https on BigSoundBank or Wikimedia Commons only)"
        )
    return url


class OnlyAllowed(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_open = urllib.request.build_opener(OnlyAllowed()).open


def get(url):
    with _open(urllib.request.Request(allowed(url), headers=UA), timeout=60) as r:
        return r.read()


def free(licence):
    """CC0 or public domain, by name; never a CC BY or a mix."""
    t = " ".join(licence.lower().split())
    mixed = {"by", "sa", "nc", "nd", "gfdl", "gpl"} & set(re.split(r"[^a-z0-9.]+", t))
    return bool(re.match(r"(cc0|cc-zero|public domain|pd\b|pd-)", t)) and not mixed


def source_info(page, get=get):
    """{audio, licence, source, title} for a sound page; refuses a page off the allowlist or not
    CC0/public domain."""
    allowed(page)
    host = urllib.parse.urlsplit(page).hostname.lower()
    if host.endswith("bigsoundbank.com"):
        h = get(page).decode("utf-8", "replace")
        lic = re.search(r"(?is)>\s*License\s*</h2>\s*<p[^>]*>\s*<strong>(.*?)\(\s*<a", h)
        lic = html.unescape(re.sub(r"<[^>]+>", "", lic.group(1))).strip() if lic else ""
        audio = next(
            (
                json.loads(s)
                for s in re.findall(r'(?s)<script type="application/ld\+json">(.*?)</script>', h)
                if '"AudioObject"' in s
            ),
            None,
        )
        if not audio:
            raise Refused(f"{page}: no sound on that page")
        enc = sorted(
            audio.get("encoding", []), key=lambda e: e.get("encodingFormat") != "audio/wav"
        )
        url, title = (
            enc[0]["contentUrl"],
            audio.get("name", "").split(" \u2014 ")[0].split(" - BigSoundBank")[0],
        )
        who = (audio.get("author") or {}).get("name", "")
    else:
        m = re.search(r"/wiki/(File:[^?#]+)", page)
        if not m:
            raise Refused(
                f"{page}: not a Commons file page (https://commons.wikimedia.org/wiki/File:...)"
            )
        q = urllib.parse.urlencode(
            {
                "action": "query",
                "format": "json",
                "prop": "imageinfo",
                "iiprop": "url|extmetadata",
                "titles": urllib.parse.unquote(m.group(1)),
            }
        )
        pages = json.loads(get(f"https://commons.wikimedia.org/w/api.php?{q}"))["query"]["pages"]
        info = next(iter(pages.values())).get("imageinfo")
        if not info:
            raise Refused(f"{page}: no such file on Commons")
        meta, url, title = info[0]["extmetadata"], info[0]["url"], m.group(1)
        lic = meta.get("LicenseShortName", {}).get("value", "")
        who = re.sub(r"<[^>]+>", "", html.unescape(meta.get("Artist", {}).get("value", ""))).strip()
    if not free(lic):
        raise Refused(f"{page}: licence {lic or 'unknown'!r} is not CC0 or public domain")
    allowed(url)
    return {
        "audio": url,
        "licence": lic,
        "title": title,
        "source": f"{'BigSoundBank' if 'bigsoundbank' in host else 'Wikimedia Commons'} "
        f'"{title}", {page}' + (f" ({who})" if who else ""),
    }


def search(words, get=get):
    """Candidate pages on both sources: [{page, title, licence (when the listing says)}]."""
    q = urllib.parse.quote_plus(words)
    h = get(f"https://bigsoundbank.com/search?q={q}").decode("utf-8", "replace")
    found = [
        {
            "page": f"https://bigsoundbank.com{p}",
            "title": t.replace("-", " "),
            "licence": "CC0 (BigSoundBank)",
        }
        for p, t in dict.fromkeys(re.findall(r"""href=['"](/([a-z0-9-]+)-s\d{4}\.html)['"]""", h))
    ]
    api = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrnamespace": 6,
            "gsrsearch": f"{words} filetype:audio",
            "gsrlimit": 20,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiextmetadatafilter": "LicenseShortName",
        }
    )
    pages = (
        json.loads(get(f"https://commons.wikimedia.org/w/api.php?{api}"))
        .get("query", {})
        .get("pages", {})
    )
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        lic = ii.get("extmetadata", {}).get("LicenseShortName", {}).get("value", "")
        if free(lic):
            found.append({"page": ii.get("descriptionurl"), "title": p["title"], "licence": lic})
    return found[:30]


def _ff(*args):
    r = subprocess.run(
        [FFMPEG, "-v", "error", "-y", *map(str, args)], capture_output=True, text=True
    )
    if r.returncode:
        raise Refused(f"ffmpeg: {r.stderr.strip()[-300:]}")


def land(src, name, description, tags, bank, source, licence, cut=6.0, start=0.0, length=None):
    """Shape src into <bank>/<name>.wav, record it as a Draft in index.json and make its Demo.
    Returns the entry."""
    if not NAME.match(name):
        raise Refused(f"{name!r}: a name is lowercase letters, digits, _ or -")
    if not str(source).strip() or not str(licence).strip():
        raise Refused(f"{name}: a sound needs its source and licence")
    bank = Path(bank).resolve()
    (bank / "demos").mkdir(parents=True, exist_ok=True)
    index = bank / "index.json"
    data = json.loads(index.read_text()) if index.is_file() else {"sounds": []}
    old = next((s for s in data["sounds"] if s.get("name") == name), None)
    if old and not old.get("draft"):
        raise Refused(f"{name}: an approved sound has that name; pick another")
    cut = max(0.1, min(float(cut), 10.0))
    trim = "silenceremove=start_periods=1:start_threshold=-50dB"
    chain = (
        f"atrim=start={float(start)}"
        + (f":duration={float(length)}" if length else "")
        + f",asetpts=PTS-STARTPTS,{trim},areverse,{trim},areverse,atrim=end={cut},"
        "afade=t=in:d=0.005,areverse,afade=t=in:d=0.03,areverse"
    )
    wav, demo, still = (
        bank / f"{name}.wav",
        bank / "demos" / f"{name}.mp4",
        bank / "demos" / f"{name}.png",
    )
    _ff("-i", src, "-af", chain, "-ac", 1, "-ar", 48000, "-c:a", "pcm_s16le", wav)
    _ff(
        "-i",
        wav,
        "-filter_complex",
        "[0:a]showwaves=s=720x360:mode=cline:colors=white,format=yuv420p[v]",
        "-map",
        "[v]",
        "-map",
        "0:a",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-shortest",
        demo,
    )
    _ff(
        "-i",
        wav,
        "-filter_complex",
        "color=c=0x15151a:s=720x240[b];[0:a]showwavespic=s=720x240:colors=white[w];[b][w]overlay",
        "-frames:v",
        1,
        still,
    )
    entry = {
        "name": name,
        "file": wav.name,
        "tags": [t for t in tags if t],
        "description": description,
        "source": source,
        "licence": licence,
        "draft": True,
        "demo": f"demos/{demo.name}",
    }
    data["sounds"] = [s for s in data["sounds"] if s.get("name") != name] + [entry]
    index.write_text(json.dumps(data, indent=1) + "\n")
    return {**entry, "file": str(wav), "demo": str(demo), "still": str(still)}


def fetch(page, name, description, tags, bank, cut=6.0, start=0.0, length=None, get=get):
    info = source_info(page, get)
    with tempfile.TemporaryDirectory() as d:
        raw = Path(d) / ("raw" + (Path(urllib.parse.urlsplit(info["audio"]).path).suffix or ".bin"))
        raw.write_bytes(get(info["audio"]))
        return land(
            raw, name, description, tags, bank, info["source"], info["licence"], cut, start, length
        )


def keep(file, name, description, tags, bank, source, licence, cut=6.0, dry_run=False):
    """With dry_run, shape it and make its Demo in a scratch folder: the bank is never touched."""
    if not Path(file).is_file():
        raise Refused(
            f"{file}: no such file, or one this server may not read (it reads the world's runs/)"
        )
    if not dry_run:
        return land(file, name, description, tags, bank, source, licence, cut)
    with tempfile.TemporaryDirectory() as d:
        entry = land(file, name, description, tags, d, source, licence, cut)
        with wave.open(entry["file"]) as w:
            seconds = round(w.getnframes() / w.getframerate(), 3)
        sizes = {k: Path(entry[k]).stat().st_size for k in ("file", "demo", "still")}
    return {**entry, "dry_run": True, "seconds": seconds, "bytes": sizes, "bank": str(bank)}


TOOLS = [
    {
        "name": "sound_search",
        "description": "Search BigSoundBank and Wikimedia Commons for CC0 or public domain sounds: "
        "returns candidate pages to pass to sound_fetch.",
        "inputSchema": {
            "type": "object",
            "required": ["words"],
            "properties": {"words": {"type": "string"}},
        },
    },
    {
        "name": "sound_fetch",
        "description": "Fetch one sound from a BigSoundBank or Wikimedia Commons page (nothing "
        "else is allowed; CC0 or public domain only), shape it (48 kHz mono, silence trimmed, cut "
        "at `cut` seconds) and land it in the Sound Bank as a Draft with its source and licence; "
        "answers its entry and its Demo (a waveform video with the sound) and still. start and "
        "length pick a part of a long recording first.",
        "inputSchema": {
            "type": "object",
            "required": ["page", "name", "description"],
            "properties": {
                "page": {"type": "string"},
                "name": {"type": "string", "description": "lowercase, digits, _ or -"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "start": {"type": "number"},
                "length": {"type": "number"},
                "cut": {"type": "number", "maximum": 10},
            },
        },
    },
    {
        "name": "sound_keep",
        "description": "Land a made WAV (a vocal-effect take from the speech connector, by its "
        "absolute path in the run's folder) in the Sound Bank as a Draft with the source and "
        "licence given, and make its Demo. dry_run shapes it and makes the Demo in a scratch "
        "folder only, answering its length: the bank is untouched.",
        "inputSchema": {
            "type": "object",
            "required": ["file", "name", "description", "source", "licence"],
            "properties": {
                "file": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string"},
                "licence": {"type": "string"},
                "cut": {"type": "number", "maximum": 10},
                "dry_run": {"type": "boolean"},
            },
        },
    },
]


def call(name, a, bank):
    if name == "sound_search":
        return search(a["words"])
    if name == "sound_fetch":
        return fetch(
            a["page"],
            a["name"],
            a["description"],
            a.get("tags", []),
            bank,
            a.get("cut", 6.0),
            a.get("start", 0.0),
            a.get("length"),
        )
    if name == "sound_keep":
        return keep(
            a["file"],
            a["name"],
            a["description"],
            a.get("tags", []),
            bank,
            a["source"],
            a["licence"],
            a.get("cut", 6.0),
            bool(a.get("dry_run")),
        )
    raise Refused(f"no tool {name}")


def mcp(bank):
    """A minimal MCP stdio server (newline-delimited JSON-RPC): initialize, tools/list,
    tools/call."""
    for line in sys.stdin:
        if not line.strip():
            continue
        m = json.loads(line)
        if "id" not in m:
            continue  # a notification
        method, p = m.get("method"), m.get("params") or {}
        if method == "initialize":
            res = {
                "protocolVersion": p.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sound_bank", "version": "0.1.0"},
            }
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                out, err = (
                    json.dumps(call(p["name"], p.get("arguments") or {}, bank), indent=1),
                    False,
                )
            except (Refused, KeyError, OSError, ValueError) as e:
                out, err = (
                    f"refused: {e}" if isinstance(e, Refused) else f"{type(e).__name__}: {e}",
                    True,
                )
            res = {"content": [{"type": "text", "text": out}], "isError": err}
        elif method == "ping":
            res = {}
        else:
            print(
                json.dumps(
                    {"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": method}}
                ),
                flush=True,
            )
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)


def main():
    a = sys.argv[1:]
    if not a or a[0] != "mcp" or len(a) > 2:
        raise SystemExit(__doc__)
    assets = os.environ.get("KRIFLOW_ASSETS") or os.environ.get("SWARMIFY_ASSETS")
    bank = a[1] if len(a) == 2 else assets and str(Path(assets) / "sounds")
    if not bank:
        raise SystemExit("no Sound Bank: $KRIFLOW_ASSETS is unset and no folder was given")
    mcp(bank)


if __name__ == "__main__":
    main()
