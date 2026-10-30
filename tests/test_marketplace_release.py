"""Marketplace release ZIP contract regressions (offline)."""

from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest

from scripts.validate_marketplace_release import REQUIRED, validate_zip

DOMAIN = "airzoneclouddaikin"
MANIFEST = {
    "domain": DOMAIN,
    "name": "DKN Cloud for HASS",
    "categories": ["climate.heat_pumps_air_conditioning"],
    "config_flow": True,
    "issue_tracker": "https://github.com/eXPerience83/DKNCloud-HASS/issues",
    "version": "0.5.0a2",
}


def _create_zip(
    tmp_path: Path,
    *,
    missing: str | None = None,
    extra: dict[str, bytes] | None = None,
    manifest: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    """Create a synthetic integration ZIP and an independent HACS manifest."""
    hacs = tmp_path / "hacs.json"
    hacs.write_text(
        json.dumps(
            {
                "name": "DKN Cloud for HASS",
                "content_in_root": False,
                "homeassistant": "2026.10.4",
                "zip_release": True,
                "filename": "airzoneclouddaikin.zip",
            }
        ),
        encoding="utf-8",
    )
    archive = tmp_path / "airzoneclouddaikin.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for filename in sorted(REQUIRED - {missing}):
            content = (
                json.dumps(MANIFEST if manifest is None else manifest).encode()
                if filename == "manifest.json"
                else b"fixture"
            )
            zf.writestr(filename, content)
        for filename, content in (extra or {}).items():
            zf.writestr(filename, content)
    return archive, hacs


def test_valid_release_matches_versioned_tag(tmp_path: Path) -> None:
    """An integration-root ZIP with matching release tag passes."""
    archive, hacs = _create_zip(tmp_path)
    validate_zip(archive, hacs, "v0.5.0a2")


def test_release_tag_must_match_manifest(tmp_path: Path) -> None:
    """Mismatch between published release tag and manifest is rejected."""
    archive, hacs = _create_zip(tmp_path)
    with pytest.raises(ValueError, match="Release tag"):
        validate_zip(archive, hacs, "v0.5.0a1")


@pytest.mark.parametrize(
    "filename",
    [
        "custom_components/airzoneclouddaikin/secrets.py",
        "../escape",
        "folder/../../escape",
        "__pycache__/module.pyc",
        "folder\\evil.py",
        "__MACOSX/file",
    ],
)
def test_reject_unsafe_archive_member(tmp_path: Path, filename: str) -> None:
    """Reject non-usable archive layout, traversal, or generated content."""
    archive, hacs = _create_zip(tmp_path, extra={filename: b"nope"})
    with pytest.raises(ValueError, match="Unsafe|Incorrect|Generated"):
        validate_zip(archive, hacs)


def test_missing_required_file(tmp_path: Path) -> None:
    """A ZIP cannot install without its config flow."""
    archive, hacs = _create_zip(tmp_path, missing="config_flow.py")
    with pytest.raises(ValueError, match="missing required"):
        validate_zip(archive, hacs)


def test_no_duplicate_members(tmp_path: Path) -> None:
    """Duplicate member names must not silently shadow the intended file."""
    archive, hacs = _create_zip(tmp_path)
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("manifest.json", json.dumps(MANIFEST))
    with pytest.raises(ValueError, match="Duplicate"):
        validate_zip(archive, hacs)


def test_symlinks_not_shipped(tmp_path: Path) -> None:
    """Release ZIP must not contain symbolic links."""
    archive, hacs = _create_zip(tmp_path)
    with zipfile.ZipFile(archive, "a") as zf:
        info = zipfile.ZipInfo("linked.py")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "secret.py")
    with pytest.raises(ValueError, match="Symlink"):
        validate_zip(archive, hacs)


def test_manifest_domain_and_category(tmp_path: Path) -> None:
    """Marketplace only installs the expected integration domain/category."""
    archive, hacs = _create_zip(tmp_path, manifest={**MANIFEST, "domain": "wrong"})
    with pytest.raises(ValueError, match="domain"):
        validate_zip(archive, hacs)

    archive, hacs = _create_zip(tmp_path, manifest={**MANIFEST, "categories": []})
    with pytest.raises(ValueError, match="category"):
        validate_zip(archive, hacs)


def test_hacs_package_metadata_must_match_zip(tmp_path: Path) -> None:
    """Only install the exact asset declared in the root HACS manifest."""
    archive, hacs = _create_zip(tmp_path)
    data = json.loads(hacs.read_text())
    data["filename"] = "wrong.zip"
    hacs.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="filename"):
        validate_zip(archive, hacs)


def test_invalid_json_manifest_fails(tmp_path: Path) -> None:
    """Malformed ZIP manifest is rejected before publication."""
    archive, hacs = _create_zip(tmp_path)
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("manifest.json", b"{broken")
    with pytest.raises(ValueError, match="Duplicate"):
        validate_zip(archive, hacs)
