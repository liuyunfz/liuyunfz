#!/usr/bin/env python3
"""Validate that generated profile cards contain only safe SVG content."""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit


EXPECTED_FILES = {
    "homelab-status-dark.svg",
    "homelab-status-light.svg",
    "sub2api-activity-dark.svg",
    "sub2api-activity-light.svg",
}
MIN_FILE_BYTES = 512
MAX_FILE_BYTES = 256 * 1024
FORBIDDEN_PATTERNS = (
    re.compile(
        rb"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        rb"[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
    ),
    re.compile(rb"(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])"),
    re.compile(rb"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    re.compile(
        rb"(?i)(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}"
        rb"[0-9a-f]{0,4}(?![0-9a-f:])"
    ),
    re.compile(
        rb"(?i)(?<![a-z0-9._%+-])[a-z0-9._%+-]+@"
        rb"[a-z0-9.-]+\.[a-z]{2,}(?![a-z0-9.-])"
    ),
)
FORBIDDEN_TERMS = re.compile(
    rb"(?i)(?:actual[_ -]?cost|api[_ -]?key|x-api-key|"
    rb"(?<![a-z0-9])(?:authorization|balance|bearer|billing|cost|endpoint|"
    rb"group|hardware|model|price|provider|region|secret|traffic|user|"
    rb"workload)(?![a-z0-9]))"
)
ALLOWED_TAGS = {
    "svg",
    "title",
    "desc",
    "style",
    "rect",
    "text",
    "line",
    "circle",
    "polygon",
    "polyline",
}
ALLOWED_ATTRIBUTES = {
    "width",
    "height",
    "viewBox",
    "role",
    "aria-labelledby",
    "id",
    "class",
    "x",
    "y",
    "rx",
    "fill",
    "stroke",
    "x1",
    "y1",
    "x2",
    "y2",
    "cx",
    "cy",
    "r",
    "text-anchor",
    "points",
    "stroke-width",
    "stroke-opacity",
    "fill-opacity",
    "stroke-linecap",
    "stroke-linejoin",
}
UNSAFE_FRAGMENTS = ("javascript:", "data:", "@import", "url(")


class ValidationError(RuntimeError):
    """A safe-to-display validation failure."""


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _forbidden_literals() -> tuple[bytes, ...]:
    endpoint = os.environ.get("KOMARI_STATUS_URL", "")
    hostname = urlsplit(endpoint).hostname or ""
    values = (
        endpoint,
        hostname,
        os.environ.get("HOMELAB_ALIAS_SALT", ""),
        os.environ.get("KOMARI_BEARER_TOKEN", ""),
        os.environ.get("SUB2API_SNAPSHOT_URL", ""),
        urlsplit(os.environ.get("SUB2API_SNAPSHOT_URL", "")).hostname or "",
        os.environ.get("SUB2API_ADMIN_API_KEY", ""),
        os.environ.get("SUB2API_WAF_BYPASS_TOKEN", ""),
    )
    encoded_values = (value.encode("utf-8") for value in values if value)
    return tuple(value for value in encoded_values if len(value) >= 8)


def validate_card(path: Path, forbidden_literals: tuple[bytes, ...]) -> None:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"{path.name}: expected a regular SVG file")
        data = path.read_bytes()
    except OSError:
        raise ValidationError(f"{path.name}: could not read generated SVG") from None

    if not MIN_FILE_BYTES <= len(data) <= MAX_FILE_BYTES:
        raise ValidationError(f"{path.name}: unexpected SVG size")
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper or b"<!--" in data:
        raise ValidationError(f"{path.name}: forbidden XML content")
    if any(literal in data for literal in forbidden_literals):
        raise ValidationError(f"{path.name}: private configuration found in SVG")
    if any(pattern.search(data) for pattern in FORBIDDEN_PATTERNS):
        raise ValidationError(f"{path.name}: identifier or address found in SVG")
    if FORBIDDEN_TERMS.search(data):
        raise ValidationError(f"{path.name}: private detail found in SVG")

    try:
        text = data.decode("utf-8", errors="strict")
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError):
        raise ValidationError(f"{path.name}: malformed SVG") from None

    if _local_name(root.tag) != "svg":
        raise ValidationError(f"{path.name}: root element is not SVG")

    title_count = 0
    description_count = 0
    for element in root.iter():
        tag = _local_name(element.tag)
        if tag not in ALLOWED_TAGS:
            raise ValidationError(f"{path.name}: disallowed SVG element")
        if tag == "title":
            title_count += 1
        elif tag == "desc":
            description_count += 1

        for raw_attribute, value in element.attrib.items():
            attribute = _local_name(raw_attribute)
            if attribute not in ALLOWED_ATTRIBUTES or attribute.lower().startswith("on"):
                raise ValidationError(f"{path.name}: disallowed SVG attribute")
            if any(fragment in value.lower() for fragment in UNSAFE_FRAGMENTS):
                raise ValidationError(f"{path.name}: unsafe SVG content")

        if element.text and any(
            fragment in element.text.lower() for fragment in UNSAFE_FRAGMENTS
        ):
            raise ValidationError(f"{path.name}: unsafe SVG content")

    if title_count != 1 or description_count != 1:
        raise ValidationError(f"{path.name}: accessibility metadata is missing")


def validate_output_directory(output_dir: Path) -> None:
    try:
        entries = list(output_dir.iterdir())
    except OSError:
        raise ValidationError("generated output directory is unavailable") from None

    if {entry.name for entry in entries} != EXPECTED_FILES:
        raise ValidationError("generated output does not contain exactly four cards")

    forbidden_literals = _forbidden_literals()
    for entry in sorted(entries):
        validate_card(entry, forbidden_literals)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate generated anonymized profile SVG cards."
    )
    parser.add_argument("output_dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        validate_output_directory(args.output_dir)
    except ValidationError as error:
        print(f"status-card validation: {error}", file=sys.stderr)
        return 1
    print("status-card validation: four sanitized SVG files are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
