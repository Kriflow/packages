# sound_bank

The Sound Bank connector: an MCP server a swarm stage's agents call to fill a world's Sound Bank,
the sound effects a Template's kit plays. It is a Connector Package (ADR 0019), off until a world
enables it. Everything Kriflow knows of it is `kriflow-connector.toml`.

## Tools

- `sound_search(words)`: candidate pages on BigSoundBank and Wikimedia Commons, CC0 or public
  domain only.
- `sound_fetch(page, name, description, tags?, start?, length?, cut?)`: fetch one sound from such
  a page and land it as a Draft.
- `sound_keep(file, name, description, source, licence, tags?, cut?, dry_run?)`: land a take an
  earlier stage of the run made (a speech vocal effect), by its absolute path in the run's
  folder, as a Draft. `dry_run` shapes it and makes its Demo in a scratch folder only and answers
  its length: the bank is untouched.

A Draft is `<name>.wav` (48 kHz mono, silence trimmed, cut at `cut` seconds, default 6) with its
`index.json` entry (`"draft": true`, source, licence, tags) and a Demo in `demos/` (a waveform
video with the sound, and a still). A Template's kit refuses a Draft until it is approved. Any
page, audio file or redirect off the two sources, and any licence but CC0 or public domain, is
refused. A name an approved sound has is refused too.

## What it may use

- The `sounds` Owned Folder (`$KRIFLOW_ASSETS/sounds`), the Sound Bank itself: read and write.
- `permissions.folders = ["worlds/{world}/runs"]`: it reads the world's run folders. A tool server
  lent to a swarm stage is not confined to that stage, so without this it could read no take an
  earlier stage made (issue 15).
- The network: the two sources only.
- ffmpeg from `imageio-ffmpeg`, installed beside it by `uv sync` (`$FFMPEG` overrides).

## Setup

None.

## Upgrading from 0.0.1

0.0.1 was a local package (the Connectors page's add form) running a Template's
`tools/fetch_sound.py` on the same sounds folder. 0.1.0 is the same three tools, keeps that
folder as its Owned Folder and adds `dry_run`; install it beside 0.0.1 and move the world's pin.
