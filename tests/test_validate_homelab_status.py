from __future__ import annotations

import os
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from scripts import generate_homelab_status as generator
from scripts import generate_sub2api_activity as activity_generator
from scripts import validate_homelab_status as validator
from tests.test_generate_homelab_status import TEST_SALT, load_fixture


FIXED_NOW = datetime(2026, 9, 5, 2, 5, tzinfo=UTC)


class OutputValidationTests(unittest.TestCase):
    def _generate(self, directory: Path) -> None:
        generator.generate_cards_from_payload(
            load_fixture("status_valid.json"),
            TEST_SALT,
            directory,
            now=FIXED_NOW,
        )
        activity_payload = json.loads(
            (Path(__file__).parent / "fixtures" / "sub2api_valid.json").read_text(
                encoding="utf-8"
            )
        )
        activity_generator.generate_cards_from_payload(
            activity_payload,
            directory,
            generated_at=FIXED_NOW,
        )

    def test_accepts_generated_light_and_dark_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            self._generate(output)
            with mock.patch.dict(
                os.environ,
                {
                    "KOMARI_STATUS_URL": "https://private.example/api/rpc2",
                    "HOMELAB_ALIAS_SALT": TEST_SALT,
                    "SUB2API_SNAPSHOT_URL": (
                        "https://private-ai.example/api/v1/admin/dashboard/snapshot-v2"
                    ),
                    "SUB2API_ADMIN_API_KEY": "fixture-admin-key",
                    "SUB2API_WAF_BYPASS_TOKEN": "fixture-waf-bypass-token",
                },
                clear=True,
            ):
                validator.validate_output_directory(output)

    def test_rejects_identifier_in_generated_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            self._generate(output)
            path = output / "homelab-status-light.svg"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "Homelab · Live Status",
                    "11111111-1111-4111-8111-111111111111",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(validator.ValidationError, "identifier"):
                validator.validate_output_directory(output)

    def test_rejects_ipv6_address_in_generated_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            self._generate(output)
            path = output / "homelab-status-dark.svg"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "anonymized aggregate telemetry",
                    "2001:db8::1234",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(validator.ValidationError, "address"):
                validator.validate_output_directory(output)

    def test_rejects_extra_publishable_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            self._generate(output)
            (output / "status.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(validator.ValidationError, "exactly four"):
                validator.validate_output_directory(output)

    def test_rejects_private_ai_gateway_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            self._generate(output)
            path = output / "sub2api-activity-light.svg"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "Self-hosted AI Gateway",
                    "PRIVATE MODEL DETAIL",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(validator.ValidationError, "private detail"):
                validator.validate_output_directory(output)

    def test_rejects_configured_sub2api_endpoint_literal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            self._generate(output)
            private_url = (
                "https://private-ai.example/api/v1/admin/dashboard/snapshot-v2"
            )
            path = output / "sub2api-activity-dark.svg"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "Self-hosted AI Gateway",
                    private_url,
                    1,
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"SUB2API_SNAPSHOT_URL": private_url},
                clear=True,
            ):
                with self.assertRaisesRegex(
                    validator.ValidationError, "private configuration"
                ):
                    validator.validate_output_directory(output)


if __name__ == "__main__":
    unittest.main()
