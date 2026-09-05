#!/usr/bin/env python3
"""Generate anonymized SVG status cards from Komari's JSON-RPC endpoint.

Only the online state and uptime are retained for rendering. Node identifiers
are converted to deterministic HMAC aliases and never written to disk or logs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


RPC_REQUEST = {
    "jsonrpc": "2.0",
    "method": "common:getNodesLatestStatus",
    "params": {},
    "id": 1,
}
USER_AGENT = "homelab-status-card/1.0"
MAX_RESPONSE_BYTES = 1_048_576
MAX_NODES = 64
MAX_UPTIME_SECONDS = 100 * 366 * 86_400
MIN_SALT_BYTES = 16
MIN_ALIAS_LENGTH = 6
MAX_FUTURE_SKEW_SECONDS = 300
DEFAULT_MAX_AGE_SECONDS = 600
DEFAULT_TIMEOUT_SECONDS = 15
OUTPUT_FILENAMES = {
    "light": "homelab-status-light.svg",
    "dark": "homelab-status-dark.svg",
}


class StatusCardError(RuntimeError):
    """A safe-to-display status-card generation error."""


@dataclass(frozen=True)
class _RawNodeState:
    node_id: str
    online: bool
    uptime_seconds: int


@dataclass(frozen=True)
class NodeState:
    alias: str
    online: bool
    uptime_seconds: int


@dataclass(frozen=True)
class Theme:
    background: str
    border: str
    text: str
    muted: str
    row: str
    online: str
    offline: str
    summary_background: str


THEMES = {
    "light": Theme(
        background="#ffffff",
        border="#d0d7de",
        text="#1f2328",
        muted="#656d76",
        row="#f6f8fa",
        online="#1a7f37",
        offline="#cf222e",
        summary_background="#ddf4ff",
    ),
    "dark": Theme(
        background="#0d1117",
        border="#30363d",
        text="#e6edf3",
        muted="#8b949e",
        row="#161b22",
        online="#3fb950",
        offline="#f85149",
        summary_background="#122d42",
    ),
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent bearer credentials from being forwarded through redirects."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        return None


def _validate_status_url(value: str) -> urllib.parse.SplitResult:
    if (
        not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise StatusCardError("KOMARI_STATUS_URL is missing or invalid")

    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise StatusCardError("KOMARI_STATUS_URL is missing or invalid") from None

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.path and not parsed.path.startswith("/"))
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise StatusCardError("KOMARI_STATUS_URL is missing or invalid")
    return parsed


def build_status_url(status_url: str) -> str:
    """Accept either a Komari root URL or an explicit JSON-RPC endpoint."""

    parsed = _validate_status_url(status_url)
    path = parsed.path
    if path in ("", "/"):
        path = "/api/rpc2"
    return urllib.parse.urlunsplit(parsed._replace(path=path))


def _validate_salt(salt: str) -> bytes:
    if not isinstance(salt, str) or len(salt.encode("utf-8")) < MIN_SALT_BYTES:
        raise StatusCardError(
            f"HOMELAB_ALIAS_SALT must contain at least {MIN_SALT_BYTES} bytes"
        )
    return salt.encode("utf-8")


def _validate_token(token: str | None) -> str | None:
    if token is None or token == "":
        return None
    if len(token) > 4096 or "\r" in token or "\n" in token:
        raise StatusCardError("KOMARI_BEARER_TOKEN is invalid")
    return token


def _validate_positive_number(value: float, label: str, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        or value > maximum
    ):
        raise StatusCardError(f"{label} is invalid")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StatusCardError("status response is invalid")
        result[key] = value
    return result


def fetch_rpc_response(
    status_url: str,
    bearer_token: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Any | None = None,
) -> Mapping[str, Any]:
    """Fetch and decode one bounded JSON-RPC response without logging secrets."""

    url = build_status_url(status_url)
    token = _validate_token(bearer_token)
    timeout = _validate_positive_number(timeout_seconds, "timeout", 60)

    body = json.dumps(RPC_REQUEST, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    client = opener or urllib.request.build_opener(_NoRedirectHandler())

    try:
        with client.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status != 200:
                raise StatusCardError("status endpoint returned an invalid response")

            content_type = response.headers.get_content_type()
            if content_type != "application/json":
                raise StatusCardError("status endpoint returned an invalid response")

            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    parsed_length = int(content_length)
                    if parsed_length < 0 or parsed_length > MAX_RESPONSE_BYTES:
                        raise StatusCardError("status response is too large")
                except ValueError:
                    raise StatusCardError("status endpoint returned an invalid response") from None

            raw_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw_body) > MAX_RESPONSE_BYTES:
                raise StatusCardError("status response is too large")

        decoded = raw_body.decode("utf-8", errors="strict")
        payload = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except StatusCardError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise StatusCardError("status response is invalid") from None
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        raise StatusCardError("status fetch failed") from None
    except Exception:
        raise StatusCardError("status fetch failed") from None

    if not isinstance(payload, dict):
        raise StatusCardError("status response is invalid")
    return payload


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise StatusCardError("status response is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise StatusCardError("status response is invalid") from None
    if parsed.tzinfo is None:
        raise StatusCardError("status response is invalid")
    return parsed.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_node_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise StatusCardError("status response is invalid")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        raise StatusCardError("status response is invalid") from None
    return value


def _alias_digest(salt: bytes, node_id: str) -> str:
    digest = hmac.new(salt, node_id.encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=")


def derive_aliases(node_ids: Iterable[str], salt: str) -> dict[str, str]:
    """Return stable, opaque aliases with only colliding prefixes extended."""

    secret = _validate_salt(salt)
    identifiers = list(node_ids)
    if len(identifiers) != len(set(identifiers)):
        raise StatusCardError("status response is invalid")

    digests = {node_id: _alias_digest(secret, node_id) for node_id in identifiers}
    lengths = {node_id: MIN_ALIAS_LENGTH for node_id in identifiers}

    while True:
        groups: dict[str, list[str]] = {}
        for node_id, digest in digests.items():
            prefix = digest[: lengths[node_id]]
            groups.setdefault(prefix, []).append(node_id)

        collisions = [group for group in groups.values() if len(group) > 1]
        if not collisions:
            break

        for group in collisions:
            for node_id in group:
                lengths[node_id] += 1
                if lengths[node_id] > len(digests[node_id]):
                    raise StatusCardError("could not derive unique node aliases")

    return {
        node_id: f"NODE-{digests[node_id][: lengths[node_id]]}"
        for node_id in identifiers
    }


def parse_node_states(
    payload: Mapping[str, Any],
    salt: str,
    *,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> list[NodeState]:
    """Validate a response and retain only aliases, online state, and uptime."""

    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int):
        raise StatusCardError("max age is invalid")
    _validate_positive_number(max_age_seconds, "max age", 86_400)

    current_time = now or _utc_now()
    if current_time.tzinfo is None:
        raise StatusCardError("current time is invalid")
    current_time = current_time.astimezone(UTC)

    if (
        not isinstance(payload, Mapping)
        or payload.get("jsonrpc") != "2.0"
        or payload.get("id") != RPC_REQUEST["id"]
        or "error" in payload
    ):
        raise StatusCardError("status response is invalid")

    result = payload.get("result")
    if not isinstance(result, Mapping) or not result or len(result) > MAX_NODES:
        raise StatusCardError("status response is invalid")

    raw_states: list[_RawNodeState] = []
    for raw_node_id, raw_status in result.items():
        node_id = _validate_node_id(raw_node_id)
        if not isinstance(raw_status, Mapping):
            raise StatusCardError("status response is invalid")

        online = raw_status.get("online")
        uptime = raw_status.get("uptime")
        if not isinstance(online, bool):
            raise StatusCardError("status response is invalid")
        if (
            isinstance(uptime, bool)
            or not isinstance(uptime, int)
            or uptime < 0
            or uptime > MAX_UPTIME_SECONDS
        ):
            raise StatusCardError("status response is invalid")

        observed_at = _parse_timestamp(raw_status.get("time"))
        age_seconds = (current_time - observed_at).total_seconds()
        if age_seconds < -MAX_FUTURE_SKEW_SECONDS:
            raise StatusCardError("status response is invalid")
        if online and age_seconds > max_age_seconds:
            raise StatusCardError("status data is stale")

        raw_states.append(
            _RawNodeState(
                node_id=node_id,
                online=online,
                uptime_seconds=uptime,
            )
        )

    aliases = derive_aliases((state.node_id for state in raw_states), salt)
    sanitized = [
        NodeState(
            alias=aliases[state.node_id],
            online=state.online,
            uptime_seconds=state.uptime_seconds,
        )
        for state in raw_states
    ]
    return sorted(sanitized, key=lambda state: state.alias)


def format_uptime(seconds: int) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        raise StatusCardError("uptime is invalid")
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def render_svg(
    states: Sequence[NodeState],
    theme_name: str,
    *,
    generated_at: datetime | None = None,
) -> str:
    if theme_name not in THEMES or not states:
        raise StatusCardError("could not render status card")
    rendered_at = generated_at or _utc_now()
    if rendered_at.tzinfo is None:
        raise StatusCardError("could not render status card")
    rendered_at = rendered_at.astimezone(UTC)
    updated_label = rendered_at.strftime("%Y-%m-%d %H:%M UTC")
    theme = THEMES[theme_name]
    width = 680
    row_height = 44
    row_start = 110
    height = 146 + row_height * len(states)
    online_count = sum(state.online for state in states)
    node_descriptions = " ".join(
        (
            f"{state.alias} online with {format_uptime(state.uptime_seconds)} uptime."
            if state.online
            else f"{state.alias} offline."
        )
        for state in states
    )

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
            'aria-labelledby="card-title card-description">'
        ),
        '<title id="card-title">Homelab Status</title>',
        (
            '<desc id="card-description">'
            f'{online_count} of {len(states)} anonymized nodes online. '
            f'{node_descriptions} Updated {updated_label}.'
            '</desc>'
        ),
        (
            '<style>'
            'text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}'
            '.title{font-size:20px;font-weight:700}'
            '.subtitle,.label,.footer{font-size:12px}'
            '.alias,.state,.uptime{font-size:13px;font-weight:600}'
            '.label{font-weight:600;letter-spacing:.08em}'
            '</style>'
        ),
        (
            f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
            f'rx="14" fill="{theme.background}" stroke="{theme.border}"/>'
        ),
        (
            f'<text class="title" x="28" y="35" fill="{theme.text}">'
            'Homelab · Live Status</text>'
        ),
        (
            f'<text class="subtitle" x="28" y="58" fill="{theme.muted}">'
            'anonymized aggregate telemetry</text>'
        ),
        (
            f'<rect x="516" y="20" width="136" height="32" rx="16" '
            f'fill="{theme.summary_background}"/>'
        ),
        (
            f'<text class="state" x="584" y="41" text-anchor="middle" '
            f'fill="{theme.text}">{online_count} / {len(states)} online</text>'
        ),
        f'<text class="label" x="36" y="92" fill="{theme.muted}">NODE</text>',
        f'<text class="label" x="462" y="92" fill="{theme.muted}">STATE</text>',
        (
            f'<text class="label" x="644" y="92" text-anchor="end" '
            f'fill="{theme.muted}">UPTIME</text>'
        ),
        f'<line x1="20" y1="101" x2="660" y2="101" stroke="{theme.border}"/>',
    ]

    for index, state in enumerate(states):
        top = row_start + index * row_height
        baseline = top + 24
        state_label = "ONLINE" if state.online else "OFFLINE"
        state_color = theme.online if state.online else theme.offline
        uptime_label = format_uptime(state.uptime_seconds) if state.online else "—"
        lines.extend(
            [
                (
                    f'<rect x="20" y="{top}" width="640" height="36" rx="8" '
                    f'fill="{theme.row}"/>'
                ),
                (
                    f'<text class="alias" x="36" y="{baseline}" '
                    f'fill="{theme.text}">{state.alias}</text>'
                ),
                f'<circle cx="448" cy="{baseline - 4}" r="5" fill="{state_color}"/>',
                (
                    f'<text class="state" x="462" y="{baseline}" '
                    f'fill="{state_color}">{state_label}</text>'
                ),
                (
                    f'<text class="uptime" x="644" y="{baseline}" text-anchor="end" '
                    f'fill="{theme.text}">{uptime_label}</text>'
                ),
            ]
        )

    lines.extend(
        [
            (
                f'<text class="footer" x="28" y="{height - 20}" '
                f'fill="{theme.muted}">Updated {updated_label}</text>'
            ),
            "</svg>",
        ]
    )
    svg = "\n".join(lines) + "\n"
    try:
        ET.fromstring(svg)
    except ET.ParseError:
        raise StatusCardError("could not render status card") from None
    return svg


def build_cards(
    states: Sequence[NodeState],
    *,
    generated_at: datetime | None = None,
) -> dict[str, str]:
    return {
        OUTPUT_FILENAMES[theme_name]: render_svg(
            states,
            theme_name,
            generated_at=generated_at,
        )
        for theme_name in OUTPUT_FILENAMES
    }


def write_cards_atomically(cards: Mapping[str, str], output_dir: Path) -> None:
    """Stage every complete file before replacing its destination."""

    staged: dict[Path, Path] = {}
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in cards.items():
            destination = output_dir / filename
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{filename}.",
                suffix=".tmp",
                dir=output_dir,
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                staged[destination] = Path(temporary.name)

        for destination, temporary_path in staged.items():
            os.replace(temporary_path, destination)
            destination.chmod(0o644)
    except Exception:
        raise StatusCardError("could not write status cards") from None
    finally:
        for temporary_path in staged.values():
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def generate_cards_from_payload(
    payload: Mapping[str, Any],
    salt: str,
    output_dir: Path,
    *,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> list[NodeState]:
    generated_at = now or _utc_now()
    states = parse_node_states(
        payload,
        salt,
        now=generated_at,
        max_age_seconds=max_age_seconds,
    )
    cards = build_cards(states, generated_at=generated_at)
    write_cards_atomically(cards, output_dir)
    return states


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate anonymized light and dark Komari status cards."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("assets/status"),
        help="Directory for generated SVG files (default: assets/status)",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_MAX_AGE_SECONDS,
        help="Maximum age for an online node sample (default: 600)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTPS request timeout (default: 15)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    status_url = os.environ.get("KOMARI_STATUS_URL", "")
    salt = os.environ.get("HOMELAB_ALIAS_SALT", "")
    bearer_token = os.environ.get("KOMARI_BEARER_TOKEN")

    try:
        payload = fetch_rpc_response(
            status_url,
            bearer_token=bearer_token,
            timeout_seconds=args.timeout_seconds,
        )
        states = generate_cards_from_payload(
            payload,
            salt,
            args.output_dir,
            max_age_seconds=args.max_age_seconds,
        )
    except StatusCardError as error:
        print(f"status-card: {error}", file=sys.stderr)
        return 1

    print(f"status-card: generated 2 cards for {len(states)} anonymized nodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
