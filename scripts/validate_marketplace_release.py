"""Check the integration release ZIP against the HA 2026.11 Marketplace contract.

The Marketplace installs an integration-root ZIP as declared by hacs.json.
Validate the exact release archive before making it available to users.
"""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import stat
import zipfile
from pathlib import Path

DOMAIN = "airzoneclouddaikin"
REQUIRED = frozenset(
    {
        "__init__.py",
        "manifest.json",
        "config_flow.py",
        "airzone_api.py",
        "climate.py",
        "sensor.py",
        "translations/en.json",
        "brand/icon.png",
    }
)
GENERATED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache"})
CATEGORY = "climate.heat_pumps_air_conditioning"


def _object(data: object, filename: str) -> dict[str, object]:
    """Require a JSON object, not a list or scalar."""
    if not isinstance(data, dict):
        raise ValueError(f"{filename} must contain a JSON object")
    return data


def _json_bytes(data: bytes, filename: str) -> dict[str, object]:
    """Read strict UTF-8 JSON and return its dictionary."""
    try:
        return _object(json.loads(data.decode("utf-8")), filename)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid UTF-8 JSON in {filename}: {error}") from error


def validate_zip(
    archive: Path, hacs_manifest: Path, expected_release_tag: str | None = None
) -> None:
    """Validate Marketplace install layout, metadata, and optional release tag."""
    hacs = _json_bytes(hacs_manifest.read_bytes(), str(hacs_manifest))
    if hacs.get("zip_release") is not True:
        raise ValueError("hacs.json must set zip_release=true")
    if hacs.get("filename") != archive.name:
        raise ValueError("hacs.json filename must match the release ZIP basename")
    if hacs.get("content_in_root") is not False:
        raise ValueError("hacs.json content_in_root must be false")
    if not isinstance(hacs.get("name"), str) or not hacs["name"].strip():
        raise ValueError("hacs.json requires a nonempty name")
    if not isinstance(hacs.get("homeassistant"), str):
        raise ValueError("hacs.json needs a Home Assistant minimum version")

    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if not members:
            raise ValueError("Release ZIP is empty")

        seen: set[str] = set()
        for item in members:
            name = item.filename
            parts = name.split("/")
            if (
                not name
                or "\\" in name
                or os.path.isabs(name)
                or ntpath.isabs(name)
                or any(part in ("..", ".") for part in parts)
            ):
                raise ValueError(f"Unsafe ZIP member path: {name!r}")
            if parts[0] in ("custom_components", "__MACOSX"):
                raise ValueError(f"Incorrect ZIP root: {name!r}")
            if name in seen:
                raise ValueError(f"Duplicate ZIP member: {name!r}")
            seen.add(name)
            if any(part in GENERATED_DIRS for part in parts) or name.endswith(
                (".pyc", ".pyo")
            ):
                raise ValueError(f"Generated/cache file in release ZIP: {name!r}")
            if item.create_system == 3 and stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError(f"Symlink in release ZIP: {name!r}")

        files = {item.filename for item in members if not item.is_dir()}
        missing = sorted(REQUIRED - files)
        if missing:
            raise ValueError(f"Release ZIP missing required files: {missing}")
        corrupt = zf.testzip()
        if corrupt is not None:
            raise ValueError(f"Corrupt ZIP member: {corrupt}")

        manifest = _json_bytes(zf.read("manifest.json"), "manifest.json")

    if manifest.get("domain") != DOMAIN:
        raise ValueError(f"Unexpected integration domain: {manifest.get('domain')!r}")
    if manifest.get("categories") != [CATEGORY]:
        raise ValueError(
            "Marketplace category must identify heat pumps/air conditioning"
        )
    if manifest.get("config_flow") is not True:
        raise ValueError("Marketplace integration requires its config flow")
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("manifest.json requires a nonempty version")
    if (
        not isinstance(manifest.get("issue_tracker"), str)
        or not manifest["issue_tracker"].strip()
    ):
        raise ValueError("manifest.json requires issue_tracker")
    if expected_release_tag is not None and expected_release_tag != f"v{version}":
        raise ValueError(
            f"Release tag {expected_release_tag!r} does not match manifest v{version}"
        )
    print(f"Marketplace release ZIP validated: {archive.name} ({DOMAIN} v{version})")


def main() -> None:
    """Validate the archive created in package CI or a release workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--hacs-json", type=Path, default=Path("hacs.json"))
    parser.add_argument("--expected-release-tag")
    args = parser.parse_args()
    try:
        validate_zip(args.archive, args.hacs_json, args.expected_release_tag)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Marketplace ZIP validation failed: {error}\n")


if __name__ == "__main__":
    main()
