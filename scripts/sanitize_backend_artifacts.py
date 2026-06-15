"""Sanitize DKN/Airzone Cloud backend artifacts for safe sharing."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import zipfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TECHNICAL_FIELDS = {
    "power",
    "mode",
    "modes",
    "scenary",
    "sleep_time",
    "cold_consign",
    "heat_consign",
    "cold_speed",
    "heat_speed",
    "availables_speeds",
    "min_temp_unoccupied",
    "max_temp_unoccupied",
    "min_limit_cold",
    "max_limit_cold",
    "min_limit_heat",
    "max_limit_heat",
    "online",
    "connection_date",
}

SENSITIVE_KEY_PARTS = {
    "address",
    "auth",
    "cookie",
    "device_id",
    "email",
    "firstname",
    "installation_id",
    "lastname",
    "location",
    "mac",
    "name",
    "password",
    "pin",
    "relation_id",
    "surname",
    "token",
    "user_email",
    "user_token",
    "username",
}

ID_KEYS = {
    "deviceid",
    "device_id",
    "id",
    "installationid",
    "installation_id",
    "relationid",
    "relation_id",
}

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
MAC_RE = re.compile(r"(?i)\b(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}\b")
TOKEN_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(user_token|token|authentication_token|password|pin)"
    r"(\s*[=:]\s*)(['\"]?)[^'\"\s,&;}]+"
)
HEADER_RE = re.compile(
    r"(?im)^(authorization|cookie|set-cookie|x-csrf-token)\s*:\s*.+$"
)
JSONISH_SECRET_RE = re.compile(
    r'(?i)("(?:user_token|token|authentication_token|password|pin|email)"\s*:\s*)"[^"]*"'
)
URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _placeholder_for_key(key: str) -> str:
    normalized = key.lower()
    if "email" in normalized or normalized in {"username", "user"}:
        return "<REDACTED_EMAIL>"
    if "password" in normalized:
        return "<REDACTED_PASSWORD>"
    if "token" in normalized or "auth" in normalized:
        return "<REDACTED_TOKEN>"
    if "mac" in normalized:
        return "<REDACTED_MAC>"
    if "pin" in normalized:
        return "<REDACTED_PIN>"
    if "name" in normalized:
        return "<REDACTED_NAME>"
    if "address" in normalized or "location" in normalized:
        return "<REDACTED_LOCATION>"
    if normalized in ID_KEYS or normalized.endswith("_id"):
        return "<REDACTED_ID>"
    return "<REDACTED>"


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    if normalized in TECHNICAL_FIELDS:
        return False
    if normalized in ID_KEYS or normalized.endswith("_id"):
        return True
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def sanitize_url(url: str) -> str:
    """Return a URL with sensitive query values and raw host data removed."""
    parts = urlsplit(url)
    query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _is_sensitive_key(key):
            query.append((key, _placeholder_for_key(key)))
        else:
            query.append((key, sanitize_text(value)))
    path = re.sub(
        r"(?i)(/devices/)[^/?#]+",
        rf"\1{_placeholder_for_key('device_id')}",
        parts.path,
    )
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def sanitize_text(value: str) -> str:
    """Redact secrets and personal data from arbitrary text."""
    sanitized = URL_RE.sub(lambda match: sanitize_url(match.group(0)), value)
    sanitized = HEADER_RE.sub(lambda match: f"{match.group(1)}: <REDACTED>", sanitized)
    sanitized = JSONISH_SECRET_RE.sub(
        lambda match: f'{match.group(1)}"<REDACTED>"',
        sanitized,
    )
    sanitized = EMAIL_RE.sub("<REDACTED_EMAIL>", sanitized)
    sanitized = MAC_RE.sub("<REDACTED_MAC>", sanitized)
    sanitized = TOKEN_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}"
            f"{match.group(3)}{_placeholder_for_key(match.group(1))}"
        ),
        sanitized,
    )
    return sanitized


def sanitize_data(value: Any) -> Any:
    """Redact sensitive values from parsed JSON-like data."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                sanitized[key] = _placeholder_for_key(str(key))
            else:
                sanitized[key] = sanitize_data(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_data(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def extract_technical_snapshots(value: Any) -> list[dict[str, Any]]:
    """Extract device-state fields that are useful and safe for backend analysis."""
    snapshots: list[dict[str, Any]] = []

    def _walk(item: Any) -> None:
        if isinstance(item, dict):
            technical = {
                key: sanitize_data(item[key]) for key in TECHNICAL_FIELDS if key in item
            }
            if technical:
                if "id" in item:
                    technical["device_id"] = "<REDACTED_ID>"
                snapshots.append(technical)
            for child in item.values():
                _walk(child)
        elif isinstance(item, list):
            for child in item:
                _walk(child)

    _walk(value)
    return snapshots


@dataclass
class SanitizedArtifacts:
    """Sanitized extraction result."""

    endpoints: Counter[str] = field(default_factory=Counter)
    payload_shapes: Counter[str] = field(default_factory=Counter)
    commands: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    redactions: Counter[str] = field(default_factory=Counter)
    files_processed: int = 0


def _redaction_counts(before: str, after: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    counts["email"] += len(EMAIL_RE.findall(before))
    counts["mac"] += len(MAC_RE.findall(before))
    counts["sensitive_header"] += len(HEADER_RE.findall(before))
    counts["secret_assignment"] += len(TOKEN_ASSIGNMENT_RE.findall(before))
    counts["json_secret"] += len(JSONISH_SECRET_RE.findall(before))
    if before != after:
        counts["files_with_redactions"] += 1
    return counts


def _safe_json_loads(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _json_candidates(text: str) -> Iterable[Any]:
    parsed = _safe_json_loads(text)
    if parsed is not None:
        yield parsed
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("{", "[")):
            continue
        parsed_line = _safe_json_loads(stripped)
        if parsed_line is not None:
            yield parsed_line


def _iter_zip_files(input_path: Path) -> Iterable[tuple[str, str]]:
    with zipfile.ZipFile(input_path) as archive:
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if info.is_dir() or path.is_absolute() or ".." in path.parts:
                continue
            with archive.open(info) as file:
                text = file.read().decode("utf-8", errors="replace")
            yield path.as_posix(), text


def _iter_files(input_path: Path) -> Iterable[tuple[str, str]]:
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        yield from _iter_zip_files(input_path)
        return

    if input_path.is_file():
        yield input_path.name, input_path.read_text(encoding="utf-8", errors="replace")
        return

    for path in sorted(input_path.rglob("*")):
        if path.is_file():
            yield (
                path.relative_to(input_path).as_posix(),
                path.read_text(encoding="utf-8", errors="replace"),
            )


def _endpoint_from_url(url: str) -> str:
    parts = urlsplit(url)
    return parts.path or "/"


def _payload_shape(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    if "event" in value and isinstance(value["event"], dict):
        event = value["event"]
        return f"event:{event.get('option', '<unknown>')}"
    if "device" in value and isinstance(value["device"], dict):
        return "device:" + ",".join(sorted(value["device"]))
    return "root:" + ",".join(sorted(str(key) for key in value))


def _command_from_payload(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("event"), dict):
        event = value["event"]
        return f"{event.get('option', '<unknown>')}={event.get('value', '<unknown>')}"
    shape = _payload_shape(value)
    return shape


def _extract_records(name: str, text: str, result: SanitizedArtifacts) -> None:
    sanitized_text = sanitize_text(text)
    result.redactions.update(_redaction_counts(text, sanitized_text))
    result.files_processed += 1

    for url in URL_RE.findall(text):
        result.endpoints[_endpoint_from_url(url)] += 1

    status_matches = re.findall(
        r"(?i)\b(?:status|statuscode|http)\D{0,12}([1-5]\d\d)\b", text
    )
    parsed_any = False
    for parsed in _json_candidates(text):
        parsed_any = True
        safe_data = sanitize_data(parsed)
        result.snapshots.extend(extract_technical_snapshots(parsed))
        shape = _payload_shape(parsed)
        command = _command_from_payload(parsed)
        if shape:
            result.payload_shapes[shape] += 1
        if command:
            result.commands.append(
                {
                    "source": name,
                    "command": command,
                    "payload_shape": shape,
                    "status": status_matches[0] if status_matches else None,
                    "payload": safe_data,
                }
            )
    if parsed_any:
        return

    if "evidence" in name.lower() or "out_of_range" in sanitized_text.lower():
        result.warnings.append(
            f"{name}: contains evidence-only or out-of-range wording"
        )

    for status in status_matches:
        result.commands.append({"source": name, "status": status})


def sanitize_artifacts(input_path: Path, output_dir: Path) -> SanitizedArtifacts:
    """Sanitize historical backend artifacts into shareable summaries."""
    result = SanitizedArtifacts()
    output_dir.mkdir(parents=True, exist_ok=True)

    for name, text in _iter_files(input_path):
        _extract_records(name, text, result)

    commands_path = output_dir / "commands.jsonl"
    with commands_path.open("w", encoding="utf-8") as file:
        for command in result.commands:
            file.write(json.dumps(sanitize_data(command), sort_keys=True) + "\n")

    snapshots_path = output_dir / "snapshots.jsonl"
    with snapshots_path.open("w", encoding="utf-8") as file:
        for snapshot in result.snapshots:
            file.write(json.dumps(sanitize_data(snapshot), sort_keys=True) + "\n")

    findings_lines = [
        "# Sanitized Backend Findings",
        "",
        "## Endpoints",
        *[f"- `{endpoint}`: {count}" for endpoint, count in result.endpoints.items()],
        "",
        "## Payload Shapes",
        *[f"- `{shape}`: {count}" for shape, count in result.payload_shapes.items()],
        "",
        "## Warnings",
        *[f"- {warning}" for warning in result.warnings],
    ]
    (output_dir / "findings.md").write_text(
        "\n".join(findings_lines).rstrip() + "\n",
        encoding="utf-8",
    )

    report = {
        "files_processed": result.files_processed,
        "commands": len(result.commands),
        "snapshots": len(result.snapshots),
        "redactions": dict(result.redactions),
    }
    (output_dir / "redaction_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    summary = [
        "Sanitized DKN/Airzone Cloud backend artifact summary",
        f"Files processed: {result.files_processed}",
        f"Commands extracted: {len(result.commands)}",
        f"Snapshots extracted: {len(result.snapshots)}",
        "No raw credentials, query tokens, MACs, PINs, installation IDs, or device IDs "
        "are intentionally preserved.",
    ]
    (output_dir / "SUMMARY.sanitized.txt").write_text(
        "\n".join(summary) + "\n",
        encoding="utf-8",
    )

    return result


def build_parser() -> argparse.ArgumentParser:
    """Build the sanitizer CLI parser."""
    parser = argparse.ArgumentParser(
        description="Sanitize DKN/Airzone Cloud backend evidence artifacts.",
    )
    parser.add_argument("input", type=Path, help="Input folder or ZIP file")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output folder; defaults to <input-parent>/sanitized",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the output folder if it already exists",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the sanitizer CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    input_path = args.input.resolve()
    output_dir = (
        args.output.resolve()
        if args.output is not None
        else input_path.parent / "sanitized"
    )
    if output_dir.exists():
        if not args.overwrite:
            parser.error(f"Output folder already exists: {output_dir}")
        shutil.rmtree(output_dir)

    result = sanitize_artifacts(input_path, output_dir)
    print(f"Processed {result.files_processed} file(s)")
    print(f"Wrote sanitized artifacts to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
