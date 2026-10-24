from datetime import UTC, date, datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

from scripts import generate_internet_costs as costs
from scripts import validate_homelab_status as validator


TODAY = date(2026, 10, 25)
PAYLOAD = {"base": "EUR", "amount": 1, "date": "2026-10-23", "rates": {
    "USD": "1.1193", "TRY": "55.6715", "GBP": "0.83728", "CAD": "1.5665", "CNY": "7.466",
}}


class ExpenseTests(unittest.TestCase):
    def setUp(self):
        self.rates = costs.parse_rates(PAYLOAD, today=TODAY)
        self.rows = costs.load_expenses()

    def test_cross_rates_quantity_and_annual_payment(self):
        self.assertEqual(self.rates.cny["EUR"], Decimal("7.466"))
        self.assertEqual(self.rates.cny["GBP"], Decimal("7.466") / Decimal("0.83728"))
        team, netcup = self.rows[1], self.rows[4]
        self.assertEqual(team["amount"] * team["quantity"], Decimal("5.50"))
        self.assertEqual(self.rows[2]["amount"], Decimal("5.50"))
        self.assertEqual(netcup["months"], 12)
        self.assertEqual(costs.monthly_cny(netcup, self.rates), Decimal("0.99") * Decimal("7.466"))
        self.assertEqual([costs.money(costs.monthly_cny(r, self.rates)) for r in self.rows], ["67.05", "49.04", "49.04", "21.45", "7.39"])
        total = sum(costs.monthly_cny(r, self.rates) for r in self.rows)
        self.assertEqual(costs.money(total), "193.98")
        self.assertEqual(costs.money(total * 12), "2,327.73")

    def test_rejects_missing_nonfinite_negative_and_wrong_base_quotes(self):
        for code, value in (("CNY", "NaN"), ("GBP", 0), ("CAD", -1), ("USD", "Infinity"), ("USD", True), ("TRY", 0)):
            with self.subTest(code=code, value=value):
                bad = {**PAYLOAD, "rates": {**PAYLOAD["rates"], code: value}}
                with self.assertRaises(costs.ExpenseError):
                    costs.parse_rates(bad, today=TODAY)
        for bad in ({**PAYLOAD, "base": "USD"}, {**PAYLOAD, "rates": {}}, {**PAYLOAD, "amount": True}):
            with self.assertRaises(costs.ExpenseError):
                costs.parse_rates(bad, today=TODAY)

    def test_weekend_quotes_allowed_stale_and_future_rejected(self):
        self.assertEqual(self.rates.date, date(2026, 10, 23))
        for rate_date in ("2026-10-17", "2026-10-26", "not-a-date"):
            with self.assertRaises(costs.ExpenseError):
                costs.parse_rates({**PAYLOAD, "date": rate_date}, today=TODAY)

    def test_bad_configuration_cannot_render_misleading_totals(self):
        source = json.loads(costs.CONFIG.read_text())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data.json"
            for key, value in (("months", 0), ("months", True), ("quantity", 0), ("currency", "XYZ"), ("amount", "-2")):
                path.write_text(json.dumps([{**source[0], key: value}]))
                with self.assertRaises(costs.ExpenseError):
                    costs.load_expenses(path)

    def test_rendered_cards_pass_sanitizer_and_describe_bill_units(self):
        with tempfile.TemporaryDirectory() as temp:
            for theme in ("light", "dark"):
                svg = costs.render_svg(self.rows, self.rates, theme, now=datetime(2026,10,25,tzinfo=UTC))
                path = Path(temp) / f"internet-costs-{theme}.svg"
                path.write_text(svg)
                validator.validate_card(path, ())
                self.assertIn("₺499.99", svg)
                self.assertIn("₺5,999.88", svg)
                self.assertIn("OpenAI · Team 01", svg)
                self.assertIn("OpenAI · Team 02", svg)
                self.assertEqual(svg.count("£5.50"), 2)
                self.assertEqual(svg.count("£66.00"), 2)
                self.assertNotIn("CNY / MONTH", svg)
                self.assertEqual(svg.count("¥"), 2)
                self.assertIn("€11.88", svg)
                self.assertIn("Equivalent to €0.99 / month", svg)
                self.assertIn("FX 2026-10-23", svg)
                path.write_text(svg.replace("Internet recurring expenses", "secret api_key"))
                with self.assertRaises(validator.ValidationError):
                    validator.validate_card(path, ())

    def test_failure_leaves_prior_output_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "internet-costs-light.svg"
            path.write_text("previous successful card")
            with mock.patch.object(costs, "fetch_rates", side_effect=costs.ExpenseError("exchange-rate fetch failed")), mock.patch("sys.argv", ["generator", "--output-dir", temp]):
                self.assertEqual(costs.main(), 1)
            self.assertEqual(path.read_text(), "previous successful card")

    def test_temporary_network_failure_retries_with_a_bound(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps(PAYLOAD).encode()
        with mock.patch.object(costs.urllib.request, "urlopen", side_effect=[urllib.error.URLError("temporary"), response]) as fetch, mock.patch.object(costs.time, "sleep"), mock.patch.object(costs, "parse_rates", return_value=self.rates):
            self.assertEqual(costs.fetch_rates(), self.rates)
            self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
