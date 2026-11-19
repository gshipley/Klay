#!/usr/bin/env python3
"""Check Flatpak's pinned Python distributions against PyPI's advisory feed.

This checks the exact source files and versions without installing them. It does
not audit native libraries bundled in wheels or supplied by the Flatpak runtime;
Qt and runtime security advisories still need separate review.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


DEFAULT_MANIFEST = Path(__file__).resolve().parent / "flatpak/com.grantshipley.Klay.Devel.json"


def canonical_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned_distributions(manifest):
    """Read every PyPI artifact, including dependencies nested in modules."""
    pins = {}

    def visit(module):
        if not isinstance(module, dict):
            raise ValueError("External module files must be included in the dependency audit")
        for source in module.get("sources", []):
            url = source.get("url", "")
            parsed = urlsplit(url)
            if parsed.hostname != "files.pythonhosted.org":
                if module.get("name", "").startswith("python3-"):
                    raise ValueError(f"Unsupported Python source in {module['name']}: {url}")
                continue
            if parsed.scheme != "https" or source.get("type") != "file":
                raise ValueError(f"Expected an HTTPS PyPI file: {url}")
            filename = unquote(parsed.path.rsplit("/", 1)[-1])
            if filename.endswith(".whl"):
                parts = filename[:-4].split("-")
                if len(parts) not in (5, 6):
                    raise ValueError(f"Invalid wheel filename: {filename}")
                name, version = parts[:2]
            else:
                suffix = next((s for s in (".tar.gz", ".zip") if filename.endswith(s)), None)
                if suffix is None or "-" not in filename[:-len(suffix)]:
                    raise ValueError(f"Unsupported distribution filename: {filename}")
                name, version = filename[:-len(suffix)].rsplit("-", 1)
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", version):
                raise ValueError(f"Invalid distribution identity: {filename}")
            digest = source.get("sha256", "")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"Missing SHA256 pin: {filename}")
            key = canonical_name(name)
            pin = {"name": key, "version": version, "filename": filename, "url": url, "sha256": digest}
            if key in pins and pins[key] != pin:
                raise ValueError(f"Conflicting distribution pins: {key}")
            pins[key] = pin
        for child in module.get("modules", []):
            visit(child)

    visit(manifest)
    if not pins:
        raise ValueError("No pinned Python distributions found")
    return [pins[name] for name in sorted(pins)]


def fetch_metadata(name, version):
    request = Request(
        f"https://pypi.org/pypi/{name}/{version}/json",
        headers={"User-Agent": "Klay-dependency-audit", "Accept": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        payload = response.read(16 * 1024 * 1024 + 1)
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError("PyPI metadata exceeds size limit")
    return json.loads(payload)


def check_pin(pin, metadata):
    info = metadata["info"]
    if canonical_name(info["name"]) != pin["name"] or info["version"] != pin["version"]:
        raise ValueError(f"PyPI returned a different package/version for {pin['name']}")
    files = [entry for entry in metadata["urls"] if entry["filename"] == pin["filename"]]
    if len(files) != 1 or files[0]["url"] != pin["url"] or files[0]["digests"]["sha256"] != pin["sha256"]:
        raise ValueError(f"Artifact URL/SHA256 does not match PyPI for {pin['filename']}")
    if files[0].get("yanked"):
        raise ValueError(f"Pinned artifact has been yanked: {pin['filename']}")
    advisories = metadata["vulnerabilities"]
    if not isinstance(advisories, list):
        raise ValueError("Invalid PyPI vulnerability metadata")
    active = []
    seen = set()
    for advisory in advisories:
        if advisory.get("withdrawn"):
            continue
        identifiers = {advisory["id"], *advisory.get("aliases", [])}
        if not identifiers & seen:
            active.append(advisory)
        seen.update(identifiers)
    return active


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    try:
        pins = pinned_distributions(json.loads(args.manifest.read_text()))
        found = False
        for pin in pins:
            advisories = check_pin(pin, fetch_metadata(pin["name"], pin["version"]))
            print(f"{pin['name']}=={pin['version']}: {len(advisories)} active advisories")
            for advisory in advisories:
                found = True
                fixed = ", ".join(advisory.get("fixed_in", [])) or "not listed"
                print(f"  {advisory['id']} (fixed in: {fixed}) {advisory.get('link', '')}")
        print("Scope: pinned Python distributions only; bundled Qt/native libraries and the Flatpak runtime require separate review.")
        return 1 if found else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Dependency audit failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
