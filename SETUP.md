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

Create a separate random token, for example with `openssl rand -hex 32`, and put
it in the GitHub Actions secret `SUB2API_WAF_BYPASS_TOKEN`. Do not use the admin
API key as this token. Allowed characters are ASCII letters, digits, `_` and `-`.

The request's exact User-Agent becomes:

```text
sub2api-activity-card/1.0 profile/<the-token-you-generated>
```

In ESA, match all available conditions together:

- Hostname equals the Sub2API hostname.
- URI path equals `/api/v1/admin/dashboard/snapshot-v2`.
- User Agent equals the complete string above.
- HTTP method equals `GET`, if supported by the rule editor.

If the editor only offers the full URI rather than URI path, the request includes
date query parameters: use a path-aware condition or a prefix ending in
`/api/v1/admin/dashboard/snapshot-v2?`, not an exact full-URI match without them.
Skip only the rule or protection that causes the rejection, where ESA allows
that scope. Put the exception before the rejecting rule and verify its event
log. Do not disable the entire WAF or broadly allow overseas traffic.

User-Agent is spoofable and can appear in access logs. This token is a scoped
WAF exception marker, not an authentication replacement: Sub2API still checks
the independent administrator key in `x-api-key`. The legacy
`x-profile-card-token` header is also sent for compatibility.

After configuring the exception, run **Refresh privacy-bounded profile cards**
with target `sub2api` or `both`. Push events refresh Homelab only; Sub2API also has
its daily schedule. A failed generation leaves the previous published cards
unchanged. Rotate any administrator key previously shared in a conversation.

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
