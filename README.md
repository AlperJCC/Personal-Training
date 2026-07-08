# gx-redeem — Personal Training Check-In Kiosk

Dedicated kiosk (tablet + USB/Bluetooth barcode scanner acting as a
keyboard wedge) that lets a member scan their membership barcode and
automatically redeems one Personal Training session from their package
registration — no staff or member interaction beyond the scan itself.

This deployment is scoped to the Personal Training category only
(`REDEEM_CATEGORY_ID`). It structurally cannot see or touch a different
paid program the member happens to also have (e.g. swim lesson packages),
since the offering cache is built from a category-scoped search and the
per-scan roster loop only iterates that cache. If another program needs
this same kiosk treatment, deploy a separate instance with a different
`REDEEM_CATEGORY_ID` rather than generalizing this one.

## Flow

1. Barcode scanned → `GET /members?barcode={barcode}&is_active_only=true`
   → member_id, name, photo.
2. Loop over the cached, category-scoped offering list →
   `GET /programs/{program_id}/offerings/{offering_id}/roster/{member_id}`
   for each, to find active package registrations with
   `remaining_instances > 0`.
3. If multiple active packages match, auto-pick the one expiring soonest.
4. Check `redeemed_sessions` on the chosen registration for a redemption
   dated today — this is both the daily cap (1/day) and the duplicate-scan
   guard.
5. Resolve `instructor_id` from the offering's default `instructor_name`
   via the local `INSTRUCTORS` mapping.
6. `POST /packages/redeem`.
7. Return a `status` the kiosk UI maps to a sound cue and on-screen message.

## Response contract (`POST /api/scan`)

| `status` | `reason` | Meaning | Sound | Note logged to Daxko? |
|---|---|---|---|---|
| `redeemed` | — | Success | Success chime | No |
| `declined` | `daily_cap` | Already redeemed today | Soft double-beep | No |
| `error` | `no_active_package` | No active package in this category | Error buzz | Yes |
| `error` | `unmapped_instructor` | Default instructor has no ID mapping | Error buzz | Yes |
| `error` | `api_error` | Daxko API call failed / cache not loaded | Error buzz | Yes |
| `error` | `member_not_found` | Barcode didn't match a member | Error buzz | No (no member to attach a note to) |

Failure notes are written via `POST /members/{member_id}/notes` with
`note_type: "Staff Action"`, `author: "gx-redeem kiosk"` — a note, not an
alert, so it doesn't interrupt staff at every future check-in but leaves a
reviewable trail. Note-writing is best-effort and never blocks or crashes
the scan response.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Daxko credentials + REDEEM_CATEGORY_ID
python app.py           # dev server on http://localhost:5000
```

## Config (env vars)

| Var | Required | Notes |
|---|---|---|
| `DAXKO_CLIENT_ID` / `DAXKO_CLIENT_SECRET` | yes | Same Render env var pattern as `daxko-dashboard` / `jcc-pos` |
| `DAXKO_OAUTH_SCOPE` | no (default `OPS_5413`) | |
| `REDEEM_CATEGORY_ID` | yes | Daxko category/tag ID for Personal Training — confirm exact ID from Daxko admin, don't guess |
| `BRANCH_ID` | no (default `B725`) | |
| `INSTRUCTORS_JSON` | no | JSON object `{"instructor_id": "Full Name", ...}` — overrides the hardcoded placeholder mapping without a code change |
| `CACHE_REFRESH_INTERVAL_SECONDS` | no (default 86400) | Offering cache refresh cadence |

## Deployment (Render)

- `WEB_CONCURRENCY=1`, start command: `gunicorn -c gunicorn.conf.py app:app`
- The offering-cache refresh loop is started as a background thread from
  gunicorn's `post_fork` hook, not at module import time — same fix that
  stabilized `gx-schedule`.
- `GET /healthz` reports process + cache-freshness status for the Render
  health check.

## Known open items (carried over from the handoff spec)

- **`INSTRUCTORS` mapping needs real `instructor_id` values before
  production.** The values in `app.py` are test/placeholder IDs. Set
  `INSTRUCTORS_JSON` once the real IDs are confirmed from Daxko admin or a
  live cart-registration response — no code change needed.
- **No confirmation/undo step** — fully automatic by design, so a mis-scan
  or wrong barcode read means a real redemption happens. A "last
  redemption" quick-undo affordance for staff is a reasonable v2 if this
  becomes a problem in practice; out of scope for v1.
- **`member_not_found` has nowhere to attach an audit note** (no
  `member_id` to attach it to) — fails silently on the audit-trail side by
  design; only the on-screen error is shown.
- **OAuth request shape is inferred, not confirmed.** `get_access_token()`
  in `app.py` implements a standard client-credentials POST (JSON body:
  `client_id`, `client_secret`, `grant_type: client_credentials`, `scope`)
  against `https://operations.oauth2.partners.daxko.com/token`, matching
  the handoff doc's description. Diff this against the actual
  `daxko-dashboard`/`jcc-pos` implementation before relying on it in
  production, in case the real request body differs.
- Sound cues are synthesized tones via the Web Audio API
  (`static/kiosk.js`) rather than sourced audio files, so there's nothing
  to deploy/host — swap in real audio files there if a different sound is
  wanted later.
