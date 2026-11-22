"""The packages repository's rules, stdlib only. Run from anywhere inside the repository.

    python tools/check.py               refuse a broken bump, index entry or help_url (exit 1)
    python tools/check.py untagged      the release tags main still needs, one per line
    python tools/check.py changed BASE  the packages whose files differ from BASE, one per line;
                                        "all" when something outside a package folder changed

Rules (README, Releases):
- A package whose shipped files (all but Markdown, test_*.py and conftest.py) changed since its
  newest `<name>-v<version>` tag must raise its Manifest version above that tag.
- Each index entry for this repository names a package folder, `subfolder` is that folder, `ref`
  is `<name>-v<Manifest version>` and `contract` is the Manifest's (absent means 1); every package
  has an entry.
- A version being released (no tag yet) points every Setup `help_url` into its own folder here.
  Tagged versions are frozen, so the help links they shipped with are not checked again.
"""

import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

REPO_URL = "https://github.com/Kriflow/packages"
MANIFEST = "swarmify-connector.toml"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def packages(root: Path) -> dict[str, dict]:
    """Each package folder's Manifest, by folder name."""
    return {m.parent.name: tomllib.loads(m.read_text()) for m in sorted(root.glob(f"*/{MANIFEST}"))}


def newest_tag(root: Path, name: str) -> str | None:
    tags = git(root, "tag", "-l", f"{name}-v*").split()
    return max(tags, key=lambda t: version_key(t.removeprefix(f"{name}-v")), default=None)


def shipped(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix != ".md" and not p.name.startswith("test_") and p.name != "conftest.py"


def help_urls(value: object) -> list[str]:
    if isinstance(value, dict):
        return [u for k, v in value.items() for u in ([v] if k == "help_url" else help_urls(v))]
    if isinstance(value, list):
        return [u for v in value for u in help_urls(v)]
    return []


def problems(root: Path) -> list[str]:
    out = []
    manifests = packages(root)
    for name, manifest in manifests.items():
        version = manifest["version"]
        if manifest.get("name") != name:
            out.append(f"{name}: its Manifest name is {manifest.get('name')!r}, not the folder's")
        tag = newest_tag(root, name)
        if tag:
            changed = [f for f in git(root, "diff", "--name-only", tag, "--", name).split()]
            changed += git(root, "ls-files", "--others", "--exclude-standard", name).split()
            tagged = tag.removeprefix(f"{name}-v")
            if any(map(shipped, changed)) and version_key(version) <= version_key(tagged):
                out.append(f"{name}: files changed since {tag}; raise its version above {tagged}")
        if not tag or version_key(version) > version_key(tag.removeprefix(f"{name}-v")):
            for url in help_urls(manifest):
                if not url.startswith(f"{REPO_URL}/tree/main/{name}"):
                    out.append(f"{name}: help_url {url} is outside {REPO_URL}/tree/main/{name}")
    index = tomllib.loads((root / "index.toml").read_text()).get("packages", [])
    ours = [e for e in index if e.get("git", "").removesuffix(".git") == REPO_URL]
    for e in ours:
        name = e.get("name")
        manifest = manifests.get(name)
        if manifest is None:
            out.append(f"index: {name} has no package folder here")
            continue
        want = {
            "subfolder": name,
            "ref": f"{name}-v{manifest['version']}",
            "contract": manifest.get("contract", 1),
        }
        for field, value in want.items():
            if e.get(field, 1 if field == "contract" else None) != value:
                out.append(f"index: {name} {field} is {e.get(field)!r}, should be {value!r}")
    for name in sorted(set(manifests) - {e.get("name") for e in ours}):
        out.append(f"index: {name} has no entry")
    return out


def untagged(root: Path) -> list[str]:
    tags = set(git(root, "tag", "-l", "*-v*").split())
    wanted = [f"{name}-v{m['version']}" for name, m in packages(root).items()]
    return [t for t in wanted if t not in tags]


def changed(root: Path, base: str) -> list[str]:
    files = git(root, "diff", "--name-only", f"{base}...HEAD").split()
    names = set(packages(root))
    tops = {f.split("/")[0] for f in files}
    if tops - names - {"index.toml", "README.md", "LICENSE"}:
        return ["all"]
    return sorted(tops & names)


def main(argv: list[str]) -> int:
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    if argv[:1] == ["untagged"]:
        print("\n".join(untagged(root)))
        return 0
    if argv[:1] == ["changed"] and len(argv) == 2:
        print("\n".join(changed(root, argv[1])))
        return 0
    if argv:
        print(__doc__)
        return 2
    found = problems(root)
    print("\n".join(found) or "ok")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
