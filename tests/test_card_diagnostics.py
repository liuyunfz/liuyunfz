import io
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr
import urllib.error
from email.message import Message

from scripts import generate_sub2api_activity as activity
from scripts import report_card_diagnostics as reporter


class Response:
    status = 200

    def __init__(self, body=b'{"code":0,"data":{"trend":[]}}', content_type="application/json"):
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.dict(activity.os.environ, {"SUB2API_USER_ID": "42"}),
                        mock.patch.object(activity.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fetch(self, opener):
        return activity.fetch_snapshot("https://private.example.invalid", "private-api-key",
                                       opener=opener, waf_bypass_token="private-token-" + "x" * 40)

    def test_timeout_then_success_keeps_scope_and_emits_safe_timings(self):
        opener = mock.Mock()
        opener.open.side_effect = [TimeoutError("private-api-key https://private.example.invalid"), Response()]
        log = io.StringIO()
        with redirect_stderr(log):
            self.assertEqual(self.fetch(opener)["code"], 0)
        self.assertEqual(opener.open.call_count, 2)
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(requests[0].full_url, requests[1].full_url)
        self.assertIn("user_id=42", requests[0].full_url)
        self.assertIn("timezone=Asia%2FShanghai", requests[0].full_url)
        activity.time.sleep.assert_called_once_with(2)
        lines = log.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(reporter.ATTEMPT.fullmatch(line) for line in lines))
        self.assertIn("category=timeout", lines[0])
        self.assertIn("retry=1", lines[0])
        self.assertIn("category=success", lines[1])
        self.assertNotIn("private", log.getvalue())

    def test_network_failures_stop_after_three_attempts(self):
        for reason, category in ((TimeoutError("private"), "timeout"),
                                 (socket.gaierror(-3, "private"), "dns"),
                                 (ConnectionResetError("private"), "network")):
            with self.subTest(category=category):
                opener = mock.Mock()
                opener.open.side_effect = urllib.error.URLError(reason)
                log = io.StringIO()
                activity.time.sleep.reset_mock()
                with redirect_stderr(log), self.assertRaises(activity.FetchError):
                    self.fetch(opener)
                self.assertEqual(opener.open.call_count, 3)
                self.assertEqual(activity.time.sleep.call_args_list, [mock.call(2), mock.call(4)])
                self.assertIn(f"category={category}", log.getvalue())
                self.assertIn("retry=0", log.getvalue().splitlines()[-1])
                self.assertNotIn("private", log.getvalue())

    def test_tls_failure_is_not_retried(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError("private"))
        with redirect_stderr(io.StringIO()), self.assertRaises(activity.FetchError) as error:
            self.fetch(opener)
        self.assertEqual(error.exception.category, "tls")
        self.assertEqual(opener.open.call_count, 1)
        activity.time.sleep.assert_not_called()

    def test_http_retry_policy_and_status_diagnostics(self):
        for status in (301, 400, 401, 403, 404, 408, 423, 429, 500, 501, 502, 503, 504):
            with self.subTest(status=status):
                opener = mock.Mock()
                def fail(*args, **kwargs):
                    raise urllib.error.HTTPError("https://private.invalid", status, "private", Message(),
                                                 io.BytesIO(b'{"code":[],"message":"private"}'))
                opener.open.side_effect = fail
                log = io.StringIO()
                with redirect_stderr(log), self.assertRaises(activity.FetchError):
                    self.fetch(opener)
                self.assertEqual(opener.open.call_count, 3 if status in {408,429,500,502,503,504} else 1)
                self.assertIn(f"http={status}", log.getvalue())
                self.assertNotIn("private", log.getvalue())

    def test_bad_json_or_html_is_not_retried(self):
        for response, category in ((Response(b"private invalid json"), "json"),
                                   (Response(b"private challenge", "text/html"), "content_type")):
            opener = mock.Mock()
            opener.open.return_value = response
            with redirect_stderr(io.StringIO()), self.assertRaises(activity.FetchError) as error:
                self.fetch(opener)
            self.assertEqual(error.exception.category, category)
            self.assertEqual(opener.open.call_count, 1)

    def test_schema_failure_is_logged_without_raw_response(self):
        opener = mock.Mock()
        opener.open.return_value = Response(b'{"code":0,"data":{"trend":"private"}}')
        with tempfile.TemporaryDirectory() as directory:
            log = io.StringIO()
            with mock.patch.object(activity.urllib.request, "build_opener", return_value=opener), \
                 mock.patch.dict(activity.os.environ, {"SUB2API_SNAPSHOT_URL":"https://private.invalid",
                                                       "SUB2API_ADMIN_API_KEY":"private-key"}), redirect_stderr(log):
                self.assertEqual(activity.main(["--output-dir", directory]), 1)
            self.assertIn("activity-card: snapshot response is invalid", log.getvalue())
            self.assertNotIn("private", log.getvalue())
            self.assertFalse(list(Path(directory).iterdir()))

    def test_reporter_filters_injection_traceback_and_secret_values(self):
        valid = "card-diag source=sub2api attempt=1 category=timeout http=0 elapsed_ms=15001 retry=1"
        private = "private-token https://private.invalid user_id=42 ::error::injected"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log"
            path.write_text("Traceback " + private + "\n" + valid + "\n" + valid + private +
                            "\nactivity-card: could not write activity cards\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                reporter.report(path, failed=True)
            self.assertEqual(output.getvalue().splitlines(), [valid, "card-diag sub2api: output write failed"])
            path.write_text(private)
            output = io.StringIO()
            with redirect_stdout(output):
                reporter.report(path, failed=True)
            self.assertEqual(output.getvalue(), "card-diag category=unclassified; raw details suppressed\n")

    def test_validator_diagnostics_allow_only_fixed_filename_and_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log"
            path.write_text("status-card validation: homelab-status-dark.svg: private configuration found in SVG\n"
                            "status-card validation: PRIVATE.svg: malformed SVG\n")
            output = io.StringIO()
            with redirect_stdout(output):
                reporter.report(path, failed=True)
            self.assertEqual(output.getvalue(), "card-diag validation: homelab-status-dark.svg: private configuration found in SVG\n")


if __name__ == "__main__":
    unittest.main()
