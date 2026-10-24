# Profile card maintenance

The profile embeds static light/dark SVG images. GitHub README images cannot run
JavaScript, switch chart windows, or display interactive chart tooltips. The AI
card therefore shows 7D, 30D, and 90D together. The publisher retains exactly six
SVG files in a single-root-commit branch, with no raw response or JSON artifact.

## Data windows

All cards display update timestamps in UTC+8. The Sub2API collector requests
90 completed Asia/Shanghai (UTC+8) calendar days, excluding today, with the
explicit `timezone=Asia/Shanghai` query parameter. Each
window's totals are derived from daily trend rows, not the dashboard's lifetime
stats. Missing dates are treated as no recorded usage. 7D and 30D show daily
values; 90D shows consecutive weekly daily averages (the final bin has six days).
Each chart has an independent vertical scale. Values only cover data retained
by Sub2API; deleted history cannot be recovered by the card.

Set the Actions secret `SUB2API_USER_ID` to the positive numeric ID of the one
user whose activity you own (from Sub2API user management, not an upstream
account ID). Every live snapshot request must include this `user_id` filter.
Missing or invalid IDs stop generation before any network request; there is no
fallback to site-wide statistics. The email and ID are not rendered into cards.
Keep `include_stats=false`: the dashboard lifetime stats are site-wide even
when a user filter is present. Cards show requests and tokens, not balance or
remaining monetary credit. Re-run both cards after setting the secret.

## ESA WAF rule

Set the Actions secret `SUB2API_WAF_BYPASS_TOKEN` to a separate random value
(recommended: 32 random bytes encoded as 64 hexadecimal characters). Do not
reuse the administrator API key. Valid values use 32-256 ASCII letters, digits,
underscores or hyphens. The request uses this exact User-Agent:

```text
sub2api-activity-card/1.0 profile/<SUB2API_WAF_BYPASS_TOKEN>
```

In ESA, match all available conditions together:

- Hostname equals the Sub2API hostname.
- URI path equals `/api/v1/admin/dashboard/snapshot-v2`.
- User Agent equals the full string above, with the actual token substituted.
- HTTP method equals `GET`, if supported by the rule editor.

If the editor only offers the full URI rather than URI path, the request includes
date query parameters: use a path-aware condition or a prefix ending in
`/api/v1/admin/dashboard/snapshot-v2?`, not an exact full-URI match without them.
Skip only the rule or protection that causes the rejection, where ESA allows
that scope. Put the exception before the rejecting rule and verify its event
log. Do not disable the entire WAF or broadly allow overseas traffic.

For rollout only, an unset secret retains `sub2api-activity-card/1.0 profile`.
Configure the secret before tightening ESA. Remove the old prefix-only
exception, otherwise it also allows requests without the secret. User-Agent
can be copied and can appear in access logs; the suffix is a WAF marker, not
an authentication replacement. Sub2API still checks the independent
administrator key in `x-api-key`. No legacy custom token header is sent.

After configuring the exception, run **Refresh privacy-bounded profile cards**
with target `sub2api` or `both`. Matching code push events refresh both cards;
Sub2API also has its daily schedule. A failed generation leaves the previous published cards
unchanged. Rotate any administrator key previously shared in a conversation.

## Safe troubleshooting

The Sub2API step publishes allowlisted `card-diag` lines: attempt number,
category, HTTP status (0 means unavailable), elapsed milliseconds and whether
another attempt will follow. Categories distinguish timeout, DNS, TLS, network,
HTTP, non-JSON content type, JSON decoding, size and validation failures.
The scheduled workflow uses a 30-second socket timeout and at most 3 attempts
with 2s/4s backoff for temporary network failures or HTTP 408/429/500/502/503/504.
The timeout is per socket operation, not a total request deadline; the workflow
also has its existing 8-minute overall limit. Authentication, WAF denial, TLS,
invalid data and rendering failures are not retried. No Retry-After header,
redirect target, raw exception, response body, URL, email, ID, key or UA token
is printed. Private logs are still cleared and never uploaded as artifacts.
The run summary shows actual stage outcomes because `continue-on-error` can
otherwise make a failed generator's step badge look successful. Failure still
preserves the last published cards. Homelab errors get coarse allowlisted
categories; the new network retry policy applies only to Sub2API.

## Refresh frequency

- Homelab: minute 17 and 47 of every hour (48 scheduled attempts per day).
- Sub2API: 03:23 UTC daily (11:23 Asia/Shanghai).
- Expenses: 03:23 UTC daily, together with Sub2API.
- All three card types: manual `both` runs and matching generator/workflow/data pushes.
- Expenses alone: manual `expenses` runs.

These are scheduled attempts, not an availability guarantee: GitHub can delay
or drop scheduled jobs under load, and inactive public repositories can have
schedules disabled after 60 days. Check Actions when the image timestamp is
stale; the last successful image is retained on fetch failure.

## Homelab privacy

The collector reads Komari's visible directory and latest status in a JSON-RPC
batch. Only configured names, online state, uptime, CPU core count and total RAM
reach the image. CPU count and RAM come from the visible node directory's
`cpu_cores` and `mem_total` fields (RAM is bytes, displayed in GiB or MiB).
These are OS-reported capacities, not inferred plan sizes or live utilization.
Missing/invalid specs display `—`; offline nodes retain their known specs. Hidden
nodes and nodes absent from the directory are omitted. Unsafe, missing or
duplicate names fall back to aliases derived using `HOMELAB_ALIAS_SALT` (at least
16 UTF-8 bytes). Names are XML-escaped and sized to fit the name column.

Configured names can themselves disclose a provider, purpose or machine size;
review those names in Komari. IPs, UUIDs, costs, regions and raw telemetry are
not copied as separate fields. The profile owner has requested a public link to `https://monitor.6yfz.cn/`.
The README wraps the Homelab picture in that link; its SVG has a
"View dashboard" hint. The dashboard exposes its own public details.

## Internet expense ledger

Edit `data/recurring-expenses.json` to maintain the public recurring charges.
`amount` is the original-currency charge **per subscription per payment cycle**,
`quantity` multiplies that charge, and `months` is 1 (monthly) or 12 (annual).
Plus is TRY 499.99 monthly; the two Team subscriptions are separate GBP 5.50 monthly entries; Netcup is EUR 11.88 once a year, equivalent
to EUR 0.99 per month. Amounts, annualized charges and annual-payment monthly equivalents are derived from these values.

Every expense generation fetches Frankfurter v1's latest ECB reference rates
for CNY, USD, TRY, GBP and CAD, all quoted against EUR for the same date. The
CNY conversion is EUR/CNY divided by EUR/source-currency; EUR uses EUR/CNY
directly. These are reference estimates, not bank settlement rates. Weekends
and holidays use the latest published business-day rates; the card displays
their date, conversion rates and generation time. Responses over seven days
old, future dates, missing currencies and nonpositive/nonfinite rates fail
rather than producing misleading totals. Temporary fetch errors get up to
three attempts. No API key or new secret is needed.

Monthly CNY equals amount × quantity × exchange rate ÷ cycle months.
Annualized CNY is the unrounded monthly total × 12, not historical spending.
Decimal arithmetic is used throughout; display values round half up to two
places, so individually rounded rows can differ from the total by a cent.
Currency conversion fees are excluded. Only the two summary totals use CNY. Each item shows its original charge,
payment cycle and annualized amount in its original currency; annual-payment
rows also explain their original-currency monthly equivalent.

The publisher allows exactly six SVGs and no raw rate JSON. A requested-source
failure prevents publishing the whole set and preserves the previous cards
and their timestamps. The ledger allows public cost/billing wording while all
identifier, credential and active-SVG checks remain enabled.
