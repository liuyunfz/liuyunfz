#!/usr/bin/env python3
"""Generate sanitized Sub2API activity cards from dashboard snapshot-v2 JSON.

The raw admin response is reduced to a small allowlisted data model before any
SVG is built.  Only calendar dates, request counts, and token counts survive
that boundary; account, model, group, balance, and billing details are ignored.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Sequence


USER_AGENT = "sub2api-activity-card/1.0 profile"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 16 * 1024
MAX_RAW_POINTS = 1_000
ROLLING_DAYS = 90
DISPLAY_WINDOWS = (7, 30, 90)
MAX_UNIQUE_POINTS = ROLLING_DAYS
MAX_COUNTER = 10**30
DEFAULT_TIMEOUT_SECONDS = 15
OUTPUT_FILENAMES = {
    "light": "sub2api-activity-light.svg",
    "dark": "sub2api-activity-dark.svg",
}
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "total_tokens",
)


class ActivityCardError(RuntimeError):
    """A safe-to-display activity-card generation error."""


@dataclass(frozen=True)
class ActivityPoint:
    day: date
    requests: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ActivitySnapshot:
    total_requests: int
    total_tokens: int
    points: tuple[ActivityPoint, ...]


@dataclass(frozen=True)
class Theme:
    background: str
    border: str
    text: str
    muted: str
    grid: str
    badge_background: str
    badge_text: str
    requests: str
    tokens: str


THEMES = {
    "light": Theme(
        background="#ffffff",
        border="#d0d7de",
        text="#1f2328",
        muted="#656d76",
        grid="#d8dee4",
        badge_background="#ddf4ff",
        badge_text="#0969da",
        requests="#0969da",
        tokens="#8250df",
    ),
    "dark": Theme(
        background="#0d1117",
        border="#30363d",
        text="#e6edf3",
        muted="#8b949e",
        grid="#30363d",
        badge_background="#122d42",
        badge_text="#58a6ff",
        requests="#58a6ff",
        tokens="#bc8cff",
    ),
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent a privileged API key from being forwarded through redirects."""

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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ActivityCardError("snapshot response is invalid")
        result[key] = value
    return result


def _decode_json(raw: bytes) -> Mapping[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ActivityCardError("snapshot response is too large")
    try:
        decoded = raw.decode("utf-8", errors="strict")
        payload = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except ActivityCardError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise ActivityCardError("snapshot response is invalid") from None
    if not isinstance(payload, dict):
        raise ActivityCardError("snapshot response is invalid")
    return payload


def load_snapshot_file(path: Path) -> Mapping[str, Any]:
    """Load a bounded local fixture or captured snapshot."""

    try:
        if not path.is_file():
            raise ActivityCardError("snapshot input is unavailable")
        with path.open("rb") as stream:
            raw = stream.read(MAX_RESPONSE_BYTES + 1)
    except ActivityCardError:
        raise
    except OSError:
        raise ActivityCardError("snapshot input is unavailable") from None
    return _decode_json(raw)


def _validate_snapshot_url(value: str) -> urllib.parse.SplitResult:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise ActivityCardError("SUB2API_SNAPSHOT_URL is missing or invalid")

    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ActivityCardError("SUB2API_SNAPSHOT_URL is missing or invalid") from None

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.path and not parsed.path.startswith("/"))
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise ActivityCardError("SUB2API_SNAPSHOT_URL is missing or invalid")
    return parsed


def build_snapshot_url(snapshot_url: str, *, as_of: date | None = None) -> str:
    """Build the fixed, privacy-minimal rolling-window dashboard query."""

    parsed = _validate_snapshot_url(snapshot_url)
    path = parsed.path
    if path in ("", "/"):
        path = "/api/v1/admin/dashboard/snapshot-v2"
    reference_day = as_of or datetime.now(UTC).date()
    if isinstance(reference_day, datetime) or not isinstance(reference_day, date):
        raise ActivityCardError("snapshot date is invalid")

    end_day = reference_day - timedelta(days=1)
    start_day = reference_day - timedelta(days=ROLLING_DAYS)
    query = urllib.parse.urlencode(
        (
            ("start_date", start_day.isoformat()),
            ("end_date", end_day.isoformat()),
            ("granularity", "day"),
            ("include_stats", "false"),
            ("include_trend", "true"),
            ("include_model_stats", "false"),
            ("include_group_stats", "false"),
            ("include_users_trend", "false"),
        )
    )
    return urllib.parse.urlunsplit(parsed._replace(path=path, query=query))


def _validate_api_key(value: str | None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or "\r" in value
        or "\n" in value
    ):
        raise ActivityCardError("SUB2API_ADMIN_API_KEY is missing or invalid")
    return value


def _validate_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        or value > 60
    ):
        raise ActivityCardError("timeout is invalid")
    return float(value)


def build_user_agent(waf_bypass_token: str | None = None) -> str:
    """Use an optional secret suffix while existing ESA rules are migrated."""

    if waf_bypass_token in (None, ""):
        return USER_AGENT
    if (
        not isinstance(waf_bypass_token, str)
        or not 32 <= len(waf_bypass_token) <= 256
        or any(
            not (c.isascii() and (c.isalnum() or c in "_-"))
            for c in waf_bypass_token
        )
    ):
        raise ActivityCardError("SUB2API_WAF_BYPASS_TOKEN is invalid")
    return f"{USER_AGENT}/{waf_bypass_token}"


def _read_safe_http_error_code(error: urllib.error.HTTPError) -> str | None:
    """Read only a bounded, allowlisted error code for safe diagnostics."""

    try:
        raw = error.read(MAX_ERROR_RESPONSE_BYTES + 1)
        if len(raw) > MAX_ERROR_RESPONSE_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (AttributeError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if code in {
        "ADMIN_COMPLIANCE_ACK_REQUIRED",
        "FORBIDDEN",
        "INVALID_ADMIN_KEY",
        "UNAUTHORIZED",
    }:
        return code
    return None


def _safe_http_error_message(error: urllib.error.HTTPError) -> str:
    code = _read_safe_http_error_code(error)
    if code == "INVALID_ADMIN_KEY":
        return "snapshot administrator key was rejected"
    if code == "UNAUTHORIZED":
        return "snapshot authentication header was not received"
    if code == "ADMIN_COMPLIANCE_ACK_REQUIRED" or error.code == 423:
        return "snapshot administrator compliance acknowledgement is required"
    if error.code == 401:
        return "snapshot authentication failed"
    if error.code == 403:
        return "snapshot request was forbidden"
    if error.code == 404:
        return "snapshot endpoint was not found"
    if error.code == 429:
        return "snapshot request was rate limited"
    return "snapshot fetch failed"


def fetch_snapshot(
    snapshot_url: str,
    api_key: str | None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Any | None = None,
    *,
    as_of: date | None = None,
    waf_bypass_token: str | None = None,
) -> Mapping[str, Any]:
    """Fetch one bounded snapshot-v2 response without exposing credentials."""

    url = build_snapshot_url(snapshot_url, as_of=as_of)
    key = _validate_api_key(api_key)
    timeout = _validate_timeout(timeout_seconds)
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": build_user_agent(waf_bypass_token),
        "x-api-key": key,
    }
    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )
    client = opener or urllib.request.build_opener(_NoRedirectHandler())

    try:
        with client.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status != 200:
                raise ActivityCardError("snapshot endpoint returned an invalid response")
            if response.headers.get_content_type() != "application/json":
                raise ActivityCardError("snapshot endpoint returned an invalid response")

            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    parsed_length = int(content_length)
                except ValueError:
                    raise ActivityCardError(
                        "snapshot endpoint returned an invalid response"
                    ) from None
                if parsed_length < 0 or parsed_length > MAX_RESPONSE_BYTES:
                    raise ActivityCardError("snapshot response is too large")

            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except ActivityCardError:
        raise
    except urllib.error.HTTPError as error:
        raise ActivityCardError(_safe_http_error_message(error)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ActivityCardError("snapshot fetch failed") from None
    except Exception:
        raise ActivityCardError("snapshot fetch failed") from None

    return _decode_json(raw)


def _parse_counter(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_COUNTER
    ):
        raise ActivityCardError("snapshot response is invalid")
    return value


def _checked_sum(left: int, right: int) -> int:
    total = left + right
    if total > MAX_COUNTER:
        raise ActivityCardError("snapshot response is invalid")
    return total


def _parse_day(value: Any) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise ActivityCardError("snapshot response is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ActivityCardError("snapshot response is invalid") from None
    if parsed.isoformat() != value:
        raise ActivityCardError("snapshot response is invalid")
    return parsed


def _parse_point(raw: Any) -> ActivityPoint:
    if not isinstance(raw, Mapping):
        raise ActivityCardError("snapshot response is invalid")

    day = _parse_day(raw.get("date"))
    requests = _parse_counter(raw.get("requests"))
    token_values: dict[str, int] = {}
    for key in TOKEN_FIELDS[:-1]:
        token_values[key] = _parse_counter(raw.get(key, 0))

    if "total_tokens" in raw:
        token_values["total_tokens"] = _parse_counter(raw["total_tokens"])
    else:
        total = 0
        for key in TOKEN_FIELDS[:-1]:
            total = _checked_sum(total, token_values[key])
        token_values["total_tokens"] = total

    return ActivityPoint(day=day, requests=requests, **token_values)


def _merge_points(left: ActivityPoint, right: ActivityPoint) -> ActivityPoint:
    if left.day != right.day:
        raise ActivityCardError("snapshot response is invalid")
    return ActivityPoint(
        day=left.day,
        requests=_checked_sum(left.requests, right.requests),
        input_tokens=_checked_sum(left.input_tokens, right.input_tokens),
        output_tokens=_checked_sum(left.output_tokens, right.output_tokens),
        cache_creation_tokens=_checked_sum(
            left.cache_creation_tokens, right.cache_creation_tokens
        ),
        cache_read_tokens=_checked_sum(
            left.cache_read_tokens, right.cache_read_tokens
        ),
        total_tokens=_checked_sum(left.total_tokens, right.total_tokens),
    )


def parse_snapshot(payload: Mapping[str, Any]) -> ActivitySnapshot:
    """Validate snapshot-v2 and return only allowlisted aggregate activity."""

    if (
        not isinstance(payload, Mapping)
        or isinstance(payload.get("code"), bool)
        or payload.get("code") != 0
    ):
        raise ActivityCardError("snapshot response is invalid")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ActivityCardError("snapshot response is invalid")
    raw_trend = data.get("trend")
    if not isinstance(raw_trend, list) or len(raw_trend) > MAX_RAW_POINTS:
        raise ActivityCardError("snapshot response is invalid")

    by_day: dict[date, ActivityPoint] = {}
    for raw_point in raw_trend:
        point = _parse_point(raw_point)
        existing = by_day.get(point.day)
        by_day[point.day] = point if existing is None else _merge_points(existing, point)

    if len(by_day) > MAX_UNIQUE_POINTS:
        raise ActivityCardError("snapshot response is invalid")
    points = tuple(by_day[day] for day in sorted(by_day))

    derived_requests = 0
    derived_tokens = 0
    for point in points:
        derived_requests = _checked_sum(derived_requests, point.requests)
        derived_tokens = _checked_sum(derived_tokens, point.total_tokens)

    return ActivitySnapshot(
        total_requests=derived_requests,
        total_tokens=derived_tokens,
        points=points,
    )


def format_compact(value: int) -> str:
    """Format a non-negative counter without converting large integers to float."""

    _parse_counter(value)
    units = (
        (10**15, "Q"),
        (10**12, "T"),
        (10**9, "B"),
        (10**6, "M"),
        (10**3, "K"),
    )
    for threshold, suffix in units:
        if value >= threshold:
            scaled = Decimal(value) / Decimal(threshold)
            decimals = 2 if scaled < 10 else 1 if scaled < 100 else 0
            quantum = Decimal(1).scaleb(-decimals)
            rendered = format(scaled.quantize(quantum, rounding=ROUND_HALF_UP), "f")
            rendered = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
            return f"{rendered}{suffix}"
    return f"{value:,}"


def _series_coordinates(
    values: Sequence[int | float],
    *,
    left: float,
    top: float,
    width: float,
    height: float,
) -> list[tuple[float, float]]:
    if not values:
        return []
    maximum = max(values)
    if len(values) == 1:
        x_values = [left + width / 2]
    else:
        x_values = [left + width * index / (len(values) - 1) for index in range(len(values))]
    if maximum == 0:
        y_values = [top + height for _ in values]
    else:
        y_values = [top + height - height * value / maximum for value in values]
    return list(zip(x_values, y_values, strict=True))


def _render_chart(
    *,
    label: str,
    values: Sequence[int | float],
    top: int,
    color: str,
    theme: Theme,
    left: float = 246.0,
    width: float = 408.0,
) -> list[str]:
    plot_top = float(top + 12)
    height = 38.0
    bottom = plot_top + height
    maximum = max(values, default=0)
    peak_label = "&lt;1" if 0 < maximum < 1 else format_compact(round(maximum))
    lines = [
        f'<text class="chart-label" x="{left}" y="{top}" fill="{theme.text}">{label}</text>',
        (
            f'<text class="chart-peak" x="{left + width}" y="{top}" text-anchor="end" '
            f'fill="{theme.muted}">peak {peak_label}</text>'
        ),
    ]
    for fraction in (0.0, 0.5, 1.0):
        y = plot_top + height * fraction
        lines.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + width}" y2="{y:.1f}" '
            f'stroke="{theme.grid}" stroke-opacity="0.7"/>'
        )

    coordinates = _series_coordinates(
        values,
        left=left,
        top=plot_top,
        width=width,
        height=height,
    )
    if not coordinates or maximum == 0:
        lines.append(
            f'<text class="empty" x="{left + width / 2}" y="{plot_top + 25:.1f}" '
            f'text-anchor="middle" fill="{theme.muted}">No activity</text>'
        )
        return lines

    points = " ".join(f"{x:.1f},{y:.1f}" for x, y in coordinates)
    if len(coordinates) > 1 and maximum > 0:
        area_points = f"{left:.1f},{bottom:.1f} {points} {left + width:.1f},{bottom:.1f}"
        lines.append(
            f'<polygon points="{area_points}" fill="{color}" fill-opacity="0.12"/>'
        )
    lines.append(
        f'<polyline points="{points}" fill="none" stroke="{color}" '
        'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>'
    )
    last_x, last_y = coordinates[-1]
    lines.append(
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.2" '
        f'fill="{theme.background}" stroke="{color}" stroke-width="2"/>'
    )
    return lines


def _utc_now() -> datetime:
    return datetime.now(UTC)


def window_series(snapshot: ActivitySnapshot, as_of: date, days: int) -> tuple[list[int], list[int]]:
    """Use completed UTC days; missing daily rows represent no recorded usage."""

    if days not in DISPLAY_WINDOWS:
        raise ActivityCardError("activity window is invalid")
    by_day = {point.day: point for point in snapshot.points}
    requests, tokens = [], []
    for offset in range(days, 0, -1):
        point = by_day.get(as_of - timedelta(days=offset))
        requests.append(point.requests if point else 0)
        tokens.append(point.total_tokens if point else 0)
    return requests, tokens


def weekly_daily_averages(values: Sequence[int]) -> list[float]:
    """Smooth consecutive seven-day bins without inflating the partial bin."""

    return [sum(values[i:i + 7]) / len(values[i:i + 7]) for i in range(0, len(values), 7)]


def render_svg(
    snapshot: ActivitySnapshot,
    theme_name: str,
    *,
    generated_at: datetime | None = None,
) -> str:
    if theme_name not in THEMES or not isinstance(snapshot, ActivitySnapshot):
        raise ActivityCardError("could not render activity card")
    rendered_at = generated_at or _utc_now()
    if rendered_at.tzinfo is None:
        raise ActivityCardError("could not render activity card")
    rendered_at = rendered_at.astimezone(UTC)
    updated_label = rendered_at.strftime("%Y-%m-%d %H:%M UTC")
    theme = THEMES[theme_name]

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            '<svg xmlns="http://www.w3.org/2000/svg" width="680" height="336" '
            'viewBox="0 0 680 336" role="img" '
            'aria-labelledby="activity-title activity-description">'
        ),
        '<title id="activity-title">Self-hosted AI Gateway Activity</title>',
        (
            '<desc id="activity-description">'
            'Requests and tokens over the last 7, 30, and 90 completed UTC days. '
            f'Each chart is independently scaled. Updated {updated_label}.'
            '</desc>'
        ),
        (
            '<style>'
            'text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}'
            '.title{font-size:19px;font-weight:700}'
            '.badge{font-size:12px;font-weight:700}'
            '.metric-label{font-size:10px;font-weight:700}'
            '.metric{font-weight:700}'
            '.chart-label{font-size:10px;font-weight:650}'
            '.chart-peak,.footer,.empty{font-size:10px}'
            '</style>'
        ),
        (
            f'<rect x="0.5" y="0.5" width="679" height="335" rx="8" '
            f'fill="{theme.background}" stroke="{theme.border}"/>'
        ),
        (
            f'<text class="title" x="24" y="31" fill="{theme.text}">'
            'Self-hosted AI Gateway</text>'
        ),
        f'<text class="footer" x="654" y="30" text-anchor="end" fill="{theme.muted}">COMPLETED UTC DAYS</text>',
        f'<line x1="24" y1="45" x2="654" y2="45" stroke="{theme.grid}"/>',
    ]
    for index, days in enumerate(DISPLAY_WINDOWS):
        left = 24 + index * 220
        request_values, token_values = window_series(snapshot, rendered_at.date(), days)
        request_label = format_compact(sum(request_values))
        token_label = format_compact(sum(token_values))
        request_size = min(25, 180 / max(1, len(request_label)) / 0.65)
        token_size = min(25, 180 / max(1, len(token_label)) / 0.65)
        start = (rendered_at.date() - timedelta(days=days)).strftime("%b %d")
        end = (rendered_at.date() - timedelta(days=1)).strftime("%b %d")
        if index:
            lines.append(f'<line x1="{left - 13}" y1="58" x2="{left - 13}" y2="291" stroke="{theme.grid}"/>')
        lines.extend([
            f'<text class="badge" x="{left}" y="67" fill="{theme.badge_text}">{days}D</text>',
            f'<text class="footer" x="{left + 190}" y="67" text-anchor="end" fill="{theme.muted}">{start} - {end}</text>',
            f'<text class="metric-label" x="{left}" y="94" fill="{theme.muted}">REQUESTS</text>',
            f'<text class="metric" font-size="{request_size:.1f}" x="{left}" y="120" fill="{theme.text}">{request_label}</text>',
            f'<text class="metric-label" x="{left}" y="200" fill="{theme.muted}">TOKENS</text>',
            f'<text class="metric" font-size="{token_size:.1f}" x="{left}" y="226" fill="{theme.text}">{token_label}</text>',
        ])
        if days == 90:
            request_values = weekly_daily_averages(request_values)
            token_values = weekly_daily_averages(token_values)
        for label, values, top, color in (
            ("Requests / day", request_values, 137, theme.requests),
            ("Tokens / day", token_values, 243, theme.tokens),
        ):
            lines.extend(_render_chart(label=label, values=values, top=top,
                                       color=color, theme=theme, left=left, width=190))
    lines.extend(
        [
            (
                f'<text class="footer" x="24" y="321" fill="{theme.muted}">'
                f'powered by Sub2API · Updated {updated_label}</text>'
            ),
            f'<text class="footer" x="654" y="321" text-anchor="end" fill="{theme.muted}">90D: weekly daily avg · separate scales</text>',
            '</svg>',
        ]
    )
    svg = "\n".join(lines) + "\n"
    try:
        ET.fromstring(svg)
    except ET.ParseError:
        raise ActivityCardError("could not render activity card") from None
    return svg


def build_cards(
    snapshot: ActivitySnapshot,
    *,
    generated_at: datetime | None = None,
) -> dict[str, str]:
    return {
        OUTPUT_FILENAMES[theme_name]: render_svg(
            snapshot,
            theme_name,
            generated_at=generated_at,
        )
        for theme_name in OUTPUT_FILENAMES
    }


def write_cards_atomically(cards: Mapping[str, str], output_dir: Path) -> None:
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
        raise ActivityCardError("could not write activity cards") from None
    finally:
        for temporary_path in staged.values():
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def generate_cards_from_payload(
    payload: Mapping[str, Any],
    output_dir: Path,
    *,
    generated_at: datetime | None = None,
) -> ActivitySnapshot:
    rendered_at = generated_at or _utc_now()
    snapshot = parse_snapshot(payload)
    cards = build_cards(snapshot, generated_at=rendered_at)
    write_cards_atomically(cards, output_dir)
    return snapshot


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate sanitized light and dark Sub2API activity cards."
    )
    parser.add_argument(
        "--input-json",
        type=Path,
        help="Read a local snapshot-v2 JSON fixture instead of HTTPS.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("assets/status"),
        help="Directory for generated SVG files (default: assets/status)",
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
    try:
        if args.input_json is not None:
            payload = load_snapshot_file(args.input_json)
        else:
            payload = fetch_snapshot(
                os.environ.get("SUB2API_SNAPSHOT_URL", ""),
                os.environ.get("SUB2API_ADMIN_API_KEY"),
                timeout_seconds=args.timeout_seconds,
                waf_bypass_token=os.environ.get("SUB2API_WAF_BYPASS_TOKEN"),
            )
        snapshot = generate_cards_from_payload(payload, args.output_dir)
    except ActivityCardError as error:
        print(f"activity-card: {error}", file=sys.stderr)
        return 1

    print(
        "activity-card: generated 2 sanitized cards "
        f"from {len(snapshot.points)} daily samples"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
