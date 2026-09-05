from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "homelab-status.yml"


class ProfileWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_has_separate_schedules_and_manual_targets(self) -> None:
        self.assertRegex(
            self.text,
            r"(?s)push:\s*\n\s*branches:\s*\n\s*- master.*?scripts/\*\*.*?tests/\*\*",
        )
        self.assertIn('- cron: "17,47 * * * *"', self.text)
        self.assertIn('- cron: "23 3 * * *"', self.text)
        self.assertRegex(
            self.text,
            r"(?s)workflow_dispatch:.*?options:\s*\n"
            r"\s*- both\s*\n\s*- homelab\s*\n\s*- sub2api",
        )
        self.assertIn('"17,47 * * * *") refresh_homelab=true', self.text)
        self.assertIn('"23 3 * * *") refresh_sub2api=true', self.text)
        self.assertRegex(
            self.text,
            r'(?s)\[\[ "\$EVENT_NAME" == "push" \]\].*?refresh_homelab=true.*?refresh_sub2api=true',
        )

    def test_generates_and_publishes_exactly_four_named_cards(self) -> None:
        self.assertIn("scripts/generate_homelab_status.py", self.text)
        self.assertIn("scripts/generate_sub2api_activity.py", self.text)
        for filename in (
            "homelab-status-dark.svg",
            "homelab-status-light.svg",
            "sub2api-activity-dark.svg",
            "sub2api-activity-light.svg",
        ):
            self.assertIn(filename, self.text)
        self.assertIn('"${#published_paths[@]}" -ne 4', self.text)
        self.assertIn("validate_homelab_status.py", self.text)

    def test_preserves_previous_cards_until_the_new_set_is_valid(self) -> None:
        seed_index = self.text.index(
            "Select cards and seed the last successful versions"
        )
        homelab_index = self.text.index("Generate homelab cards")
        sub2api_index = self.text.index("Generate AI gateway activity cards")
        validate_index = self.text.index("Validate all publishable output")
        publish_index = self.text.index(
            "Publish the one-commit status-card branch"
        )
        self.assertLess(seed_index, homelab_index)
        self.assertLess(homelab_index, sub2api_index)
        self.assertLess(sub2api_index, validate_index)
        self.assertLess(validate_index, publish_index)
        self.assertIn("git show \"$fetched_sha:$expected_file\"", self.text)
        self.assertIn("refresh_homelab=true", self.text)
        self.assertIn("refresh_sub2api=true", self.text)
        self.assertGreaterEqual(
            self.text.count("the published cards were left unchanged"), 3
        )

    def test_both_sources_are_checked_before_any_failed_run_is_reported(self) -> None:
        self.assertRegex(
            self.text,
            r"(?s)name: Generate homelab cards.*?"
            r"id: generate_homelab.*?continue-on-error: true",
        )
        self.assertRegex(
            self.text,
            r"(?s)name: Generate AI gateway activity cards.*?"
            r"id: generate_sub2api.*?continue-on-error: true",
        )
        guard = (
            "steps.generate_homelab.outcome != 'failure' && "
            "steps.generate_sub2api.outcome != 'failure'"
        )
        self.assertGreaterEqual(self.text.count(guard), 2)
        self.assertIn("name: Report a failed card source", self.text)
        self.assertIn("steps.generate_homelab.outcome == 'failure'", self.text)
        self.assertIn("steps.generate_sub2api.outcome == 'failure'", self.text)

    def test_secret_preflight_messages_do_not_echo_secret_values(self) -> None:
        self.assertIn(
            "HOMELAB_ALIAS_SALT must contain at least 16 bytes.", self.text
        )
        self.assertIn(
            "KOMARI_STATUS_URL must be an HTTPS root URL or endpoint", self.text
        )
        self.assertIn(
            "SUB2API_SNAPSHOT_URL must be an HTTPS root URL or endpoint", self.text
        )

    def test_publish_is_a_guarded_single_root_commit(self) -> None:
        self.assertIn("init --initial-branch=status-card", self.text)
        self.assertIn('if [[ "$local_parent_count" -ne 0 ]]', self.text)
        self.assertIn('if [[ "$remote_parent_count" -ne 0 ]]', self.text)
        self.assertIn(
            'if [[ "$current_remote_sha" != "$EXPECTED_REMOTE_SHA" ]]',
            self.text,
        )
        self.assertIn(
            '"--force-with-lease=refs/heads/status-card:${EXPECTED_REMOTE_SHA}"',
            self.text,
        )

    def test_secrets_are_scoped_and_generator_logs_are_suppressed(self) -> None:
        for secret in (
            "KOMARI_STATUS_URL",
            "HOMELAB_ALIAS_SALT",
            "KOMARI_BEARER_TOKEN",
            "SUB2API_SNAPSHOT_URL",
            "SUB2API_ADMIN_API_KEY",
        ):
            self.assertIn("${{ secrets." + secret + " }}", self.text)
        self.assertNotRegex(
            self.text,
            r"(?m)^\s{4}env:\s*$",
            "secrets must not be placed at job scope",
        )
        self.assertGreaterEqual(self.text.count("set +x"), 5)
        self.assertIn('>"$generator_log" 2>&1', self.text)
        self.assertIn('>"$validator_log" 2>&1', self.text)
        self.assertNotIn("toJson(secrets)", self.text)
        self.assertNotIn("ACTIONS_STEP_DEBUG", self.text)

    def test_actions_are_pinned_and_checkout_does_not_persist_credentials(self) -> None:
        uses = re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", self.text)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
        self.assertIn("persist-credentials: false", self.text)
        self.assertRegex(self.text, r"(?m)^permissions: \{\}\s*$")
        self.assertRegex(
            self.text,
            r"(?m)^\s{4}permissions:\s*\n\s{6}contents: write\s*$",
        )


if __name__ == "__main__":
    unittest.main()
