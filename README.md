# Kriflow packages

The first-party Connector Packages of [Kriflow](https://github.com/Kriflow/kriflow), one folder
each, and the curated index of every package operators can browse on Kriflow's Connectors page,
first-party or not. Formerly `swarmify-connectors`; GitHub redirects the old name.

## Layout

```
blender_build/  blender_render/  hyperframes_build/  hyperframes_render/  meta/
remotion_build/  remotion_render/  slack/  speech/  upload_post/  youtube/
index.toml      the curated index
tools/          the repository's check tool and test runner
tests/          tests of the index and the check tool
```

Each folder is one package, named exactly as the `name` in its `swarmify-connector.toml` (its
Manifest), with its own README, its tests and its version. `python_script` stays in the core.
The packages' history before the move is kept: it was imported from the core's `connectors/`.

## Releases

A release tags one package: `<name>-v<version>`, from its Manifest's `version`
(`youtube-v1.6.0`). Releasing YouTube does not release Meta.

- **Releasing is merging.** When a merged Manifest names a version with no tag yet, CI creates
  the tag after that package's tests pass.
- **Bump rule.** A pull request that changes a package's shipped files (everything but
  Markdown, `test_*.py` and `conftest.py`) since its newest tag must raise its version above that
  tag. Tags are the lock: what a tag names never changes, and a ruleset refuses moving or
  deleting `*-v*` tags.
- **Index rule.** A pull request that releases a package updates its entry in `index.toml`:
  `git` is this repository, `subfolder` the folder, `ref` the new tag, `contract` as the Manifest
  says, `permissions` exactly as Kriflow's review lists them. The index lists each package once,
  at its newest release; any older tag still installs through Install a package.
- **help_url.** A release points every Setup `help_url` at its own README here
  (`https://github.com/Kriflow/packages/tree/main/<name>#setup`).

Kriflow installs its default packages from this repository at the tags its Default List pins;
the box never picks up a release by itself.

## Package contract

A Manifest may declare `contract = N`, the Package Contract it was written for (everything a
package relies on from the core: the Manifest schema, the tools the core calls and the answers
it reads, what its process is given). Absent means 1. Kriflow runs a range of contracts and
refuses a package outside it with a message saying what to do. The core's README lists each
contract number and what it added. An index entry carries the same `contract`.

## The index format

Kriflow reads `index.toml` from `SWARMIFY_CONNECTOR_INDEX`, by default this repository's raw
`main/index.toml`. Point that setting at your own fork, over https, or at a local file to run a
private index. One `[[packages]]` table per package:

```toml
[[packages]]
name = "speech"                      # the `name` in the package's swarmify-connector.toml
description = "Voices a script, one take per scene."  # one or two plain sentences
git = "https://github.com/you/your-connector.git"     # public, https, no credentials in it
subfolder = "speech"                 # optional: where the package is when not at the root
ref = "v1.0.0"                       # the recommended tag (or commit)
contract = 1                         # optional: the Package Contract its Manifest declares
permissions = [                      # every permission its Manifest declares, as Kriflow's review lists them
  "network: api.example.org",
  "credentials: EXAMPLE_API_KEY",
]
```

- `name`: lowercase letters, digits and `_`, starting with a letter, matching the Manifest.
- `permissions`: one line per permission, `<kind>: <value>`. The kinds are `network`,
  `install_network`, `folders`, `services`, `owned_folders`, `credentials`, `sign_in` and
  `capabilities`. The simplest way to get them right is Kriflow's own review: on the
  Connectors page choose Install a package, enter your git URL and ref, and copy the list it
  shows (or `POST /api/packages/review` and copy `permissions`).
- Fields the format does not name are ignored, so a newer index still reads in an older
  Kriflow. A file that is not valid TOML, or an entry missing a field, makes Kriflow show the
  operator the first bad field instead of the list, so keep the file valid.
- The file stays under 1 MB.

The permissions here are a summary for browsing. They are never what an operator approves:
installing an entry clones it at its ref, reads its Manifest without running anything, and asks
the operator to approve the permissions that clone actually declares.

## Adding a third-party package

Open a pull request that adds one `[[packages]]` table to `index.toml`, at the end, and nothing
else. In the description, link the package's repository and say in a sentence what it is for.
To update an entry (a new recommended ref, a changed permission), change only that table.

Before opening it:

1. The package installs from a clean Kriflow through Install a package, at the `ref` you list,
   and its review shows exactly the `permissions` you list.
2. The repository is public and has a license.
3. The README of the package says what it does, what it needs (Services, accounts, Credentials)
   and how to set it up.

Reviewers check:

- The entry parses and has every field, and `name` matches the Manifest at that ref.
- The `git` URL is a public https repository; `ref` is a tag (preferred) or a commit, not a
  moving branch.
- The review of the clone at `ref`, run in Kriflow, lists exactly the entry's `permissions`.
- Every permission is one the description explains: each `network` host, folder, Service,
  Credential and Capability has a reason a reader can see. Broad grants (a wildcard host,
  `folders` outside the package's own) need a stated reason.
- The install step only installs dependencies from its runtime's registry and the hosts in
  `install_network`.
- The description is plain and accurate and makes no claim the package does not back.
- One package per pull request; no change to other entries.

Merging an entry lists a package; it does not vouch for it. Operators approve every package's
permissions themselves when they install it.

## Tests

The packages' tests import the core (its Connectors, kernel, workflows and api modules), so they
run inside a checkout of [Kriflow/kriflow](https://github.com/Kriflow/kriflow), against its
`main`:

```sh
git clone https://github.com/Kriflow/kriflow core
(cd core && uv sync --all-packages && pnpm install)
tools/test_packages.sh core youtube meta     # or: all
python tools/check.py                        # the bump, index and help_url rules
```

`test_packages.sh` copies every package folder over the core's `connectors/<name>` and runs
pytest there, as the tests ran before the move. CI does the same on every pull request (the
changed packages), nightly (all of them) and before tagging a release. Real-network and real
render tests stay opt-in, as each package's README says.

## License

MIT, see `LICENSE`. A folder with its own license keeps it: the speech engine's Chatterbox
server (`speech/chatterbox`) is AGPL-3.0, as its `pyproject.toml` says.
