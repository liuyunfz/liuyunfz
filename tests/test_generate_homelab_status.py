from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from unittest import mock

from scripts import generate_homelab_status as status_card


FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 9, 5, 2, 5, tzinfo=UTC)
TEST_SALT = "fixture-only-alias-salt"


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
        self.call_count = 0

    def open(self, request: object, timeout: float) -> FakeResponse:
        self.call_count += 1
        self.request = request
        self.timeout = timeout
        return self.response


class FailingOpener:
    def open(self, request: object, timeout: float) -> FakeResponse:
        raise urllib.error.URLError(
            "https://secret-monitor.invalid/a-raw-node-identifier"
        )


class StatusCardTests(unittest.TestCase):
    def test_fixture_is_sanitized_before_rendering(self) -> None:
        payload = load_fixture("status_valid.json")
        states = status_card.parse_node_states(
            payload,
            TEST_SALT,
            now=FIXED_NOW,
        )

        self.assertEqual(len(states), 2)
        self.assertEqual(sum(state.online for state in states), 1)
        self.assertTrue(all(state.alias.startswith("NODE-") for state in states))

        svg = status_card.render_svg(
            states,
            "light",
            generated_at=FIXED_NOW,
        )
        ET.fromstring(svg)
        for forbidden in (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            "FAKE-CLOUD-SHOULD-NOT-LEAK",
            "TEST-REGION-SHOULD-NOT-LEAK",
            "192.0.2.10",
            "987654321",
        ):
            self.assertNotIn(forbidden, svg)
        self.assertIn("ONLINE", svg)
        self.assertIn("OFFLINE", svg)
        self.assertIn(">—</text>", svg)
        self.assertIn("197d 14h", svg)
        self.assertIn("Homelab · Live Status", svg)
        self.assertIn("anonymized aggregate telemetry", svg)
        self.assertIn('width="680"', svg)
        self.assertIn("Updated 2026-09-05 02:05 UTC", svg)

    def test_aliases_match_hmac_base32_and_are_deterministic(self) -> None:
        node_id = "55555555-5555-4555-8555-555555555555"
        first = status_card.derive_aliases([node_id], TEST_SALT)
        second = status_card.derive_aliases([node_id], TEST_SALT)
        expected_prefix = status_card._alias_digest(TEST_SALT.encode(), node_id)[
            : status_card.MIN_ALIAS_LENGTH
        ]

        self.assertEqual(first, second)
        self.assertEqual(first[node_id], f"NODE-{expected_prefix}")

    def test_colliding_alias_prefixes_are_extended(self) -> None:
        first_id = "66666666-6666-4666-8666-666666666666"
        second_id = "77777777-7777-4777-8777-777777777777"
        digest_by_id = {
            first_id: "ABCDEF" + "A" * 46,
            second_id: "ABCDEF" + "B" * 46,
        }

        with mock.patch.object(
            status_card,
            "_alias_digest",
            side_effect=lambda _salt, node_id: digest_by_id[node_id],
        ):
            aliases = status_card.derive_aliases(
                [first_id, second_id],
                TEST_SALT,
            )

        self.assertEqual(aliases[first_id], "NODE-ABCDEFA")
        self.assertEqual(aliases[second_id], "NODE-ABCDEFB")

    def test_stale_online_fixture_does_not_overwrite_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            originals = {}
            for filename in status_card.OUTPUT_FILENAMES.values():
                path = output_dir / filename
                path.write_text(f"original:{filename}", encoding="utf-8")
                originals[path] = path.read_bytes()

            with self.assertRaisesRegex(status_card.StatusCardError, "stale"):
                status_card.generate_cards_from_payload(
                    load_fixture("status_stale.json"),
                    TEST_SALT,
                    output_dir,
                    now=FIXED_NOW,
                )

            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)

    def test_invalid_fixture_does_not_overwrite_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            path = output_dir / status_card.OUTPUT_FILENAMES["light"]
            path.write_text("last-known-good", encoding="utf-8")

            with self.assertRaisesRegex(status_card.StatusCardError, "invalid"):
                status_card.generate_cards_from_payload(
                    load_fixture("status_invalid.json"),
                    TEST_SALT,
                    output_dir,
                    now=FIXED_NOW,
                )

            self.assertEqual(path.read_text(encoding="utf-8"), "last-known-good")
            self.assertFalse(
                (output_dir / status_card.OUTPUT_FILENAMES["dark"]).exists()
            )

    def test_valid_fixture_writes_light_and_dark_xml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "nested"
            states = status_card.generate_cards_from_payload(
                load_fixture("status_valid.json"),
                TEST_SALT,
                output_dir,
                now=FIXED_NOW,
            )

            self.assertEqual(len(states), 2)
            contents = []
            for filename in status_card.OUTPUT_FILENAMES.values():
                content = (output_dir / filename).read_text(encoding="utf-8")
                ET.fromstring(content)
                contents.append(content)
            self.assertNotEqual(contents[0], contents[1])

    def test_fetch_uses_post_json_user_agent_and_optional_bearer(self) -> None:
        body = json.dumps(load_fixture("status_valid.json")).encode("utf-8")
        opener = RecordingOpener(FakeResponse(body))

        payload = status_card.fetch_rpc_response(
            "https://example.invalid/api/rpc2",
            bearer_token="fixture-token",
            timeout_seconds=9,
            opener=opener,
        )

        self.assertEqual(payload["id"], 1)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(opener.timeout, 9)
        request = opener.request
        self.assertIsNotNone(request)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://example.invalid/api/rpc2")
        self.assertEqual(request.get_header("User-agent"), status_card.USER_AGENT)
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
        self.assertEqual(json.loads(request.data), status_card.RPC_REQUEST)

    def test_root_url_is_expanded_to_the_komari_rpc_endpoint(self) -> None:
        body = json.dumps(load_fixture("status_valid.json")).encode("utf-8")
        for root_url in (
            "https://monitor.example.invalid",
            "https://monitor.example.invalid/",
        ):
            with self.subTest(root_url=root_url):
                opener = RecordingOpener(FakeResponse(body))
                status_card.fetch_rpc_response(
                    root_url,
                    bearer_token=None,
                    opener=opener,
                )

                self.assertEqual(
                    opener.request.full_url,
                    "https://monitor.example.invalid/api/rpc2",
                )

    def test_explicit_komari_path_is_preserved(self) -> None:
        body = json.dumps(load_fixture("status_valid.json")).encode("utf-8")
        opener = RecordingOpener(FakeResponse(body))

        status_card.fetch_rpc_response(
            "https://monitor.example.invalid/private/komari-rpc",
            bearer_token=None,
            opener=opener,
        )

        self.assertEqual(
            opener.request.full_url,
            "https://monitor.example.invalid/private/komari-rpc",
        )

    def test_fetch_error_message_never_contains_url_or_raw_identifier(self) -> None:
        with self.assertRaises(status_card.StatusCardError) as raised:
            status_card.fetch_rpc_response(
                "https://example.invalid/api/rpc2",
                opener=FailingOpener(),
            )

        message = str(raised.exception)
        self.assertEqual(message, "status fetch failed")
        self.assertNotIn("https://", message)
        self.assertNotIn("identifier", message)

    def test_insecure_or_credentialed_urls_are_rejected(self) -> None:
        invalid_urls = (
            "http://example.invalid/api/rpc2",
            "https://user:pass@example.invalid/api/rpc2",
            "https://example.invalid/api/rpc2?token=fake",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaises(status_card.StatusCardError):
                    status_card.fetch_rpc_response(url, opener=FailingOpener())

    def test_future_online_sample_is_invalid(self) -> None:
        payload = load_fixture("status_valid.json")
        first_status = next(iter(payload["result"].values()))
        first_status["time"] = "2026-09-05T03:00:00Z"
        with self.assertRaisesRegex(status_card.StatusCardError, "invalid"):
            status_card.parse_node_states(payload, TEST_SALT, now=FIXED_NOW)

    def test_cli_stale_failure_is_nonzero_and_preserves_last_good_files(self) -> None:
        payload = load_fixture("status_stale.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            output_path = output_dir / status_card.OUTPUT_FILENAMES["light"]
            output_path.write_text("last-known-good", encoding="utf-8")
            stderr = io.StringIO()
            environment = {
                "KOMARI_STATUS_URL": "https://example.invalid/api/rpc2",
                "HOMELAB_ALIAS_SALT": TEST_SALT,
            }

            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(status_card, "fetch_rpc_response", return_value=payload),
                mock.patch.object(status_card, "_utc_now", return_value=FIXED_NOW),
                redirect_stderr(stderr),
            ):
                exit_code = status_card.main(["--output-dir", str(output_dir)])

            self.assertEqual(exit_code, 1)
            self.assertIn("stale", stderr.getvalue())
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "last-known-good",
            )

    def test_uptime_format(self) -> None:
        self.assertEqual(status_card.format_uptime(0), "0m")
        self.assertEqual(status_card.format_uptime(3_900), "1h 5m")
        self.assertEqual(status_card.format_uptime(17_071_200), "197d 14h")


if __name__ == "__main__":
    unittest.main()
