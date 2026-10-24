#!/usr/bin/env python3
"""Render public recurring expenses using the latest available ECB reference rates."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

try:
    from .generate_homelab_status import THEMES, write_cards_atomically
except ImportError:
    from generate_homelab_status import THEMES, write_cards_atomically

FX_URL = "https://api.frankfurter.dev/v1/latest?base=EUR&symbols=CNY,USD,TRY,GBP,CAD"
CONFIG = Path(__file__).resolve().parents[1] / "data" / "recurring-expenses.json"
CURRENCIES = ("USD", "TRY", "GBP", "CAD", "EUR")
SYMBOLS = {"USD": "US$", "TRY": "₺", "GBP": "£", "CAD": "C$", "EUR": "€"}


class ExpenseError(ValueError):
    pass


def positive_decimal(value: object) -> Decimal:
    if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
        raise ExpenseError("invalid amount or exchange rate")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ExpenseError("invalid amount or exchange rate") from None
    if not number.is_finite() or not 0 < number <= 1_000_000:
        raise ExpenseError("invalid amount or exchange rate")
    return number


@dataclass(frozen=True)
class Rates:
    date: date
    cny: dict[str, Decimal]


def parse_rates(payload: object, *, today: date | None = None) -> Rates:
    try:
        if not isinstance(payload, dict) or payload.get("base") != "EUR" or isinstance(payload.get("amount"), bool) or payload.get("amount") != 1:
            raise ExpenseError("unexpected exchange-rate base")
        observed = date.fromisoformat(payload["date"])
        age = ((today or datetime.now(UTC).date()) - observed).days
        if not 0 <= age <= 7:
            raise ExpenseError("exchange-rate date is stale or in the future")
        quotes = {code: positive_decimal(payload["rates"][code]) for code in ("CNY", "USD", "TRY", "GBP", "CAD")}
        return Rates(observed, {code: quotes["CNY"] / (Decimal(1) if code == "EUR" else quotes[code]) for code in CURRENCIES})
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ExpenseError):
            raise
        raise ExpenseError("invalid exchange-rate response") from None


def fetch_rates() -> Rates:
    request = urllib.request.Request(FX_URL, headers={"Accept": "application/json", "User-Agent": "profile-recurring-costs/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(65_537)
                if response.status != 200 or len(raw) > 65_536:
                    raise ExpenseError("invalid exchange-rate response")
            return parse_rates(json.loads(raw, parse_float=Decimal))
        except urllib.error.HTTPError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise ExpenseError("exchange-rate fetch failed") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise ExpenseError("exchange-rate fetch failed") from None
        except (UnicodeError, json.JSONDecodeError):
            raise ExpenseError("invalid exchange-rate response") from None
        time.sleep(2 ** attempt)
    raise ExpenseError("exchange-rate fetch failed")


def load_expenses(path: Path = CONFIG) -> list[dict]:
    try:
        rows = json.loads(path.read_text())
        if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
            raise ExpenseError("expected 1–20 expense entries")
        for row in rows:
            if not isinstance(row, dict):
                raise ExpenseError("invalid expense entry")
            for key, limit in (("name", 42),):
                value = row[key]
                if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
                    raise ExpenseError("invalid expense label")
            if row["category"] not in ("AI SERVICES", "INFRASTRUCTURE", "DIGITAL ESSENTIALS") or row["currency"] not in CURRENCIES:
                raise ExpenseError("invalid category or currency")
            if type(row["months"]) is not int or row["months"] not in (1, 12):
                raise ExpenseError("supported payment cycles are monthly and yearly")
            if type(row["quantity"]) is not int or not 1 <= row["quantity"] <= 100:
                raise ExpenseError("invalid subscription quantity")
            row["amount"] = positive_decimal(row["amount"])
        return rows
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        raise ExpenseError("invalid expense configuration") from None


def monthly_cny(row: dict, rates: Rates) -> Decimal:
    return row["amount"] * row["quantity"] * rates.cny[row["currency"]] / row["months"]


def money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def render_svg(rows: list[dict], rates: Rates, theme_name: str, *, now: datetime | None = None) -> str:
    theme = THEMES[theme_name]
    accent = "#58a6ff" if theme_name == "dark" else "#0969da"
    monthly = sum((monthly_cny(row, rates) for row in rows), Decimal(0))
    groups = [category for category in ("AI SERVICES", "INFRASTRUCTURE", "DIGITAL ESSENTIALS") if any(row["category"] == category for row in rows)]
    height = 210 + 54 * len(groups) + sum(60 if row["months"] == 12 else 48 for row in rows) + 12 * (len(groups) - 1) + 100
    updated = (now or datetime.now(UTC)).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M UTC+8")
    def text(x, y, value, kind="body", color=None, anchor="start"):
        return f'<text class="{kind}" x="{x}" y="{y}" fill="{color or theme.text}" text-anchor="{anchor}">{html.escape(value)}</text>'
    def rect(x, y, w, h, color, rx=8):
        return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{color}"/>'
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="680" height="{height}" viewBox="0 0 680 {height}" role="img" aria-labelledby="cost-title cost-desc">',
           '<title id="cost-title">Internet recurring expenses</title>',
           f'<desc id="cost-desc">Monthly estimate CNY {money(monthly)}. Annualized estimate CNY {money(monthly * 12)}. ECB reference rates dated {rates.date}; fetched via Frankfurter. Estimates exclude bank conversion fees.</desc>',
           '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.title{font-size:20px;font-weight:700}.total{font-size:30px;font-weight:700}.body{font-size:13px;font-weight:600}.small{font-size:11px}.label{font-size:10px;font-weight:600}.section{font-size:11px;font-weight:700}</style>',
           f'<rect x="0.5" y="0.5" width="679" height="{height-1}" rx="14" fill="{theme.background}" stroke="{theme.border}"/>',
           text(28,35,"Internet · Recurring Costs","title"), text(28,58,"AI subscriptions · infrastructure · CNY estimates","small",theme.muted)]
    for x,label,value in ((28,"MONTHLY EQUIVALENT",monthly),(350,"ANNUALIZED COST",monthly*12)):
        out += [rect(x,80,302,102,theme.row),text(x+18,104,label,"label",theme.muted),text(x+18,143,"¥"+money(value),"total"),text(x+18,166,"CNY · at the displayed reference rates","small",theme.muted)]
    y = 210
    for group_index, group in enumerate(groups):
        if group_index:
            y += 12
        out += [text(28,y+12,group,"section",accent),
                text(36,y+37,"SERVICE / PLAN","label",theme.muted),
                text(380,y+37,"AMOUNT","label",theme.muted,"end"),
                text(492,y+37,"CYCLE","label",theme.muted,"end"),
                text(644,y+37,"ANNUALIZED","label",theme.muted,"end")]
        y += 54
        for row in rows:
            if row["category"] != group:
                continue
            charge = row["amount"] * row["quantity"]
            symbol = SYMBOLS[row["currency"]]
            amount = symbol + money(charge)
            annual = symbol + money(charge * 12 / row["months"])
            cycle = "Monthly" if row["months"] == 1 else "Yearly"
            row_height = 52 if row["months"] == 12 else 40
            baseline = y + (22 if row["months"] == 12 else 26)
            out += [rect(20,y,640,row_height,theme.row),
                    text(36,baseline,row["name"]),
                    text(380,baseline,amount,"body",anchor="end"),
                    text(492,baseline,cycle,"small",theme.muted,"end"),
                    text(644,baseline,annual,"body",anchor="end")]
            if row["months"] == 12:
                out.append(text(36,y+42,"Equivalent to "+symbol+money(charge / 12)+" / month","small",theme.muted))
            y += row_height + 8
    out += [f'<line x1="28" y1="{y+8}" x2="652" y2="{y+8}" stroke="{theme.border}"/>',text(28,y+30,f"FX {rates.date} · ECB via Frankfurter · latest available reference rates","small",theme.muted),text(28,y+49,"1 unit → CNY: "+" · ".join(f"{code} {rates.cny[code]:.4f}" for code in CURRENCIES if any(row["currency"] == code for row in rows)),"small",theme.muted),text(28,y+68,"Estimates exclude bank FX fees · annualized at current subscriptions","small",theme.muted),text(28,y+87,"Updated "+updated,"small",theme.muted),'</svg>']
    return "\n".join(out)+"\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("assets/status"))
    args = parser.parse_args()
    try:
        rows, rates = load_expenses(), fetch_rates()
        write_cards_atomically({f"internet-costs-{theme}.svg": render_svg(rows,rates,theme) for theme in THEMES},args.output_dir)
    except (ExpenseError, RuntimeError) as exc:
        print(f"expense-card: {exc}", file=sys.stderr)
        return 1
    print(f"expense-card: generated 2 cards; FX date {rates.date}; monthly CNY {money(sum((monthly_cny(r,rates) for r in rows),Decimal(0)))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
