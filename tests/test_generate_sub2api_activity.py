from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from unittest import mock

from scripts import generate_sub2api_activity as activity_card


FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 9, 5, 2, 5, tzinfo=UTC)


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_payload(
    trend: list[dict[str, object]],
    stats: dict[str, object] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {"trend": trend}
    if stats is not None:
        data["stats"] = stats
    return {"code": 0, "message": "ok", "data": data}


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"
        self.headers["Content-Length"] = str(len(body))

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class RecordingOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.request = None
        self.timeout = None

    def open(self, request: object, timeout: float) -> FakeResponse:
        self.request = request
        self.timeout = timeout
        return self.response


class FailingOpener:
    def open(self, request: object, timeout: float) -> FakeResponse:
        raise urllib.error.URLError(
            "https://private-api.example.invalid/admin?key=raw-secret"
        )


class HTTPErrorOpener:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self.body = body

    def open(self, request: object, timeout: float) -> FakeResponse:
        raise urllib.error.HTTPError(
            "https://private-api.example.invalid/admin?key=raw-secret",
            self.status,
            "private upstream detail",
            Message(),
            io.BytesIO(self.body),
        )


class ActivityCardTests(unittest.TestCase):
    def test_fixture_is_sorted_merged_and_sanitized_before_rendering(self) -> None:
        snapshot = activity_card.parse_snapshot(load_fixture("sub2api_valid.json"))

        self.assertEqual(snapshot.total_requests, 51_388)
        self.assertEqual(snapshot.total_tokens, 5_620_000_000)
        self.assertEqual(
            [point.day.isoformat() for point in snapshot.points],
            ["2026-08-07", "2026-08-08", "2026-08-09"],
        )
        self.assertEqual(snapshot.points[-1].requests, 2_050)
        self.assertEqual(snapshot.points[-1].total_tokens, 206_600_000)

        svg = activity_card.render_svg(snapshot, "light", generated_at=FIXED_NOW)
        root = ET.fromstring(svg)
        self.assertEqual(root.attrib["width"], "680")
        self.assertEqual(root.attrib["height"], "220")
        self.assertIn("51.4K", svg)
        self.assertIn("5.62B", svg)
        self.assertIn("Requests / day", svg)
        self.assertIn("Tokens / day", svg)
        self.assertIn("Self-hosted AI Gateway", svg)
        self.assertIn("powered by Sub2API", svg)
        self.assertEqual(svg.count("<polyline "), 2)
        self.assertEqual(svg.count("<polygon "), 2)
        self.assertIn("Updated 2026-09-05 02:05 UTC", svg)
        for forbidden in (
            "PRIVATE-MODEL-SHOULD-NOT-LEAK",
            "PRIVATE-GROUP-SHOULD-NOT-LEAK",
            "private-user@example.invalid",
            "PRIVATE-KEY-SHOULD-NOT-LEAK",
            "https://private-api.example.invalid",
            "9876.54",
            "1234.56",
        ):
            self.assertNotIn(forbidden, svg)

    def test_missing_stats_uses_merged_trend_totals(self) -> None:
        payload = make_payload(
            [
                {"date": "2026-09-02", "requests": 7, "total_tokens": 70},
                {"date": "2026-09-01", "requests": 3, "total_tokens": 30},
                {"date": "2026-09-02", "requests": 2, "total_tokens": 20},
            ]
        )

        snapshot = activity_card.parse_snapshot(payload)

        self.assertEqual(snapshot.total_requests, 12)
        self.assertEqual(snapshot.total_tokens, 120)
        self.assertEqual(len(snapshot.points), 2)
        self.assertEqual(snapshot.points[-1].requests, 9)

    def test_total_tokens_can_be_derived_from_allowlisted_components(self) -> None:
        snapshot = activity_card.parse_snapshot(
            make_payload(
                [
                    {
                        "date": "2026-09-05",
                        "requests": 1,
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "cache_creation_tokens": 3,
                        "cache_read_tokens": 40,
                    }
                ]
            )
        )

        self.assertEqual(snapshot.points[0].total_tokens, 55)
        self.assertEqual(snapshot.total_tokens, 55)

    def test_empty_trend_renders_two_no_activity_charts(self) -> None:
        snapshot = activity_card.parse_snapshot(
            make_payload([], {"total_requests": 0, "total_tokens": 0})
        )
        svg = activity_card.render_svg(snapshot, "dark", generated_at=FIXED_NOW)

        ET.fromstring(svg)
        self.assertEqual(svg.count("No activity"), 2)
        self.assertNotIn("<polyline ", svg)
        self.assertIn(">0</text>", svg)

    def test_single_point_and_all_zero_series_render_without_invalid_numbers(self) -> None:
        single = activity_card.parse_snapshot(
            make_payload(
                [{"date": "2026-09-05", "requests": 42, "total_tokens": 4200}]
            )
        )
        single_svg = activity_card.render_svg(single, "light", generated_at=FIXED_NOW)
        self.assertEqual(single_svg.count("<polyline "), 2)
        self.assertGreaterEqual(single_svg.count("<circle "), 2)

        all_zero = activity_card.parse_snapshot(
            make_payload(
                [
                    {"date": "2026-09-04", "requests": 0, "total_tokens": 0},
                    {"date": "2026-09-05", "requests": 0, "total_tokens": 0},
                ]
            )
        )
        zero_svg = activity_card.render_svg(all_zero, "dark", generated_at=FIXED_NOW)
        lowered = zero_svg.lower()
        self.assertNotIn("nan", lowered)
        self.assertNotIn("inf", lowered)
        ET.fromstring(zero_svg)

    def test_large_counters_format_and_render(self) -> None:
        large = 987_654_321_012_345_678_901_234
        snapshot = activity_card.parse_snapshot(
            make_payload(
                [{"date": "2026-09-05", "requests": large, "total_tokens": large}],
                {"total_requests": large, "total_tokens": large},
            )
        )

        svg = activity_card.render_svg(snapshot, "light", generated_at=FIXED_NOW)
        ET.fromstring(svg)
        self.assertIn(activity_card.format_compact(large), svg)
        self.assertNotIn("nan", svg.lower())
        self.assertNotIn("inf", svg.lower())

    def test_invalid_dates_and_counters_are_rejected(self) -> None:
        invalid_points = (
            {"date": "2026-09-05T00:00:00Z", "requests": 1, "total_tokens": 1},
            {"date": "2026-02-30", "requests": 1, "total_tokens": 1},
            {"date": "2026-09-05", "requests": -1, "total_tokens": 1},
            {"date": "2026-09-05", "requests": True, "total_tokens": 1},
            {"date": "2026-09-05", "requests": 1.5, "total_tokens": 1},
        )
        for point in invalid_points:
            with self.subTest(point=point):
                with self.assertRaisesRegex(activity_card.ActivityCardError, "invalid"):
                    activity_card.parse_snapshot(make_payload([point]))

    def test_duplicate_json_keys_are_rejected(self) -> None:
        raw = b'{"code":0,"code":1,"data":{"trend":[]}}'
        with self.assertRaisesRegex(activity_card.ActivityCardError, "invalid"):
            activity_card._decode_json(raw)

    def test_valid_payload_writes_light_and_dark_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "nested"
            snapshot = activity_card.generate_cards_from_payload(
                load_fixture("sub2api_valid.json"),
                output_dir,
                generated_at=FIXED_NOW,
            )

            self.assertEqual(len(snapshot.points), 3)
            contents = []
            for filename in activity_card.OUTPUT_FILENAMES.values():
                content = (output_dir / filename).read_text(encoding="utf-8")
                ET.fromstring(content)
                contents.append(content)
            self.assertNotEqual(contents[0], contents[1])

    def test_invalid_payload_preserves_last_known_good_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            light = output_dir / activity_card.OUTPUT_FILENAMES["light"]
            light.write_text("last-known-good", encoding="utf-8")

            with self.assertRaises(activity_card.ActivityCardError):
                activity_card.generate_cards_from_payload(
                    {"code": 1, "data": {"trend": []}}, output_dir
                )

            self.assertEqual(light.read_text(encoding="utf-8"), "last-known-good")
            self.assertFalse(
                (output_dir / activity_card.OUTPUT_FILENAMES["dark"]).exists()
            )

    def test_fetch_uses_get_api_key_and_safe_error_messages(self) -> None:
        body = json.dumps(load_fixture("sub2api_valid.json")).encode("utf-8")
        for root_url in ("https://example.invalid", "https://example.invalid/"):
            with self.subTest(root_url=root_url):
                opener = RecordingOpener(FakeResponse(body))
                payload = activity_card.fetch_snapshot(
                    root_url,
                    "fixture-admin-key",
                    timeout_seconds=9,
                    opener=opener,
                    as_of=FIXED_NOW.date(),
                )

                self.assertEqual(payload["code"], 0)
                self.assertEqual(opener.timeout, 9)
                request = opener.request
                self.assertIsNotNone(request)
                self.assertEqual(request.get_method(), "GET")
                self.assertEqual(request.get_header("X-api-key"), "fixture-admin-key")
                self.assertEqual(
                    request.get_header("User-agent"), activity_card.USER_AGENT
                )
                parsed_url = urllib.parse.urlsplit(request.full_url)
                self.assertEqual(
                    parsed_url.path, "/api/v1/admin/dashboard/snapshot-v2"
                )
                query = dict(urllib.parse.parse_qsl(parsed_url.query))
                self.assertEqual(query["start_date"], "2026-08-06")
                self.assertEqual(query["end_date"], "2026-09-04")
                self.assertEqual(query["granularity"], "day")
                self.assertEqual(query["include_stats"], "true")
                self.assertEqual(query["include_trend"], "true")
                self.assertEqual(query["include_model_stats"], "false")
                self.assertEqual(query["include_group_stats"], "false")
                self.assertEqual(query["include_users_trend"], "false")

        custom_opener = RecordingOpener(FakeResponse(body))
        activity_card.fetch_snapshot(
            "https://example.invalid/private/dashboard-snapshot",
            "fixture-admin-key",
            opener=custom_opener,
            as_of=FIXED_NOW.date(),
        )
        self.assertEqual(
            urllib.parse.urlsplit(custom_opener.request.full_url).path,
            "/private/dashboard-snapshot",
        )

        with self.assertRaises(activity_card.ActivityCardError) as raised:
            activity_card.fetch_snapshot(
                "https://example.invalid/api/v1/admin/dashboard/snapshot-v2",
                "fixture-admin-key",
                opener=FailingOpener(),
            )
        self.assertEqual(str(raised.exception), "snapshot fetch failed")
        self.assertNotIn("https://", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_insecure_credentialed_and_secret_query_urls_are_rejected(self) -> None:
        invalid_urls = (
            "http://example.invalid/api/v1/admin/dashboard/snapshot-v2",
            "https://user:pass@example.invalid/api/v1/admin/dashboard/snapshot-v2",
            "https://example.invalid/api/v1/admin/dashboard/snapshot-v2?api_key=secret",
            "https://example.invalid/api/v1/admin/dashboard/snapshot-v2?granularity=day",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaises(activity_card.ActivityCardError):
                    activity_card.fetch_snapshot(url, "fixture-key", opener=FailingOpener())

    def test_http_errors_are_reduced_to_safe_categories(self) -> None:
        cases = (
            (
                401,
                b'{"code":"INVALID_ADMIN_KEY","message":"private"}',
                "snapshot administrator key was rejected",
            ),
            (
                401,
                b'{"code":"UNAUTHORIZED","message":"private"}',
                "snapshot authentication header was not received",
            ),
            (401, b'{"code":"UNKNOWN"}', "snapshot authentication failed"),
            (403, b"private proxy page", "snapshot request was forbidden"),
            (
                423,
                b'{"code":"ADMIN_COMPLIANCE_ACK_REQUIRED"}',
                "snapshot administrator compliance acknowledgement is required",
            ),
            (404, b"", "snapshot endpoint was not found"),
            (429, b"", "snapshot request was rate limited"),
            (500, b"", "snapshot fetch failed"),
        )
        for status, body, expected in cases:
            with self.subTest(status=status):
                with self.assertRaisesRegex(
                    activity_card.ActivityCardError, f"^{expected}$"
                ) as raised:
                    activity_card.fetch_snapshot(
                        "https://example.invalid",
                        "fixture-key",
                        opener=HTTPErrorOpener(status, body),
                    )
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("https://", str(raised.exception))

    def test_cli_can_generate_from_fixture_without_environment_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "cards"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(activity_card, "_utc_now", return_value=FIXED_NOW),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = activity_card.main(
                    [
                        "--input-json",
                        str(FIXTURES / "sub2api_valid.json"),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("generated 2 sanitized cards", stdout.getvalue())
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                set(activity_card.OUTPUT_FILENAMES.values()),
            )


if __name__ == "__main__":
    unittest.main()
