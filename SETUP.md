# Profile card maintenance

The profile embeds static light/dark SVG images. GitHub README images cannot run
JavaScript, switch chart windows, or display interactive chart tooltips. The AI
card therefore shows 7D, 30D, and 90D together. The publisher retains exactly four
SVG files in a single-root-commit branch, with no raw response or JSON artifact.

## Data windows

The Sub2API collector requests 90 completed UTC days, excluding today. Each
window's totals are derived from daily trend rows, not the dashboard's lifetime
stats. Missing dates are treated as no recorded usage. 7D and 30D show daily
values; 90D shows consecutive weekly daily averages (the final bin has six days).
Each chart has an independent vertical scale. Values only cover data retained
by Sub2API; deleted history cannot be recovered by the card.

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

## Refresh frequency

- Homelab: minute 17 and 47 of every hour (48 scheduled attempts per day).
- Sub2API: 03:23 UTC daily (11:23 Asia/Shanghai).
- Both: manual `both` runs and matching generator/workflow code pushes.

These are scheduled attempts, not an availability guarantee: GitHub can delay
or drop scheduled jobs under load, and inactive public repositories can have
schedules disabled after 60 days. Check Actions when the image timestamp is
stale; the last successful image is retained on fetch failure.

## Homelab privacy

The collector reads Komari's visible directory and latest status in a JSON-RPC
batch. Only configured names, online state, and uptime reach the image. Hidden
nodes and nodes absent from the directory are omitted. Unsafe, missing or
duplicate names fall back to aliases derived using `HOMELAB_ALIAS_SALT` (at least
16 UTF-8 bytes). Names are XML-escaped and sized to fit the name column.

Configured names can themselves disclose a provider, purpose or machine size;
review those names in Komari. IPs, UUIDs, costs, regions and raw telemetry are
not copied as separate fields. No Komari link is enabled in the README: linking
it would associate the profile with the complete public dashboard, including
the details visible there. Add an HTML link around its `picture` only after
accepting that broader public association.
