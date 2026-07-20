# Revo Fitness Client Portal — Reverse-Engineering Notes

> ⚠️ **Security note:** Credentials were shared in plaintext during research. Rotate the
> Revo password and never commit credentials to the repo. Treat these notes as
> "what we discovered" — usage terms of revofitness.com.au may restrict scraping.
> Use a single low-frequency poll, identify the bot in `User-Agent`, and stop if
> they object.

## 1. Base / Auth

- **Base URL:** `https://revocentral.revofitness.com.au`
- **Login form:** `POST /portal/login.php`
  - Body: `user=<email>&password=<plain>` (form-encoded, **no CSRF token**, plain TLS only)
  - Success → `302` then `200` on `/portal/rewards/`
  - Failure → re-renders the login form with `200`
- **Session cookie:** `Member` — a URL-encoded **PHP-serialized `stdClass`** containing:
  ```
  O:8:"stdClass":2:{s:2:"id";i:<MEMBER_ID>;s:15:"membershipLevel";i:<1|2>;}
  ```
  - `id` = numeric member ID (stable per account)
  - `membershipLevel` = `1` (basic) or `2` (premium). The cookie value is set at login from the user's current membership; if you upgrade in another session you must re-login to refresh it.
- **Logout:** `GET /portal/logout.php` redirects to `/portal/level-two-feature.php` for everyone — to clear the session, just drop the cookie.

### 1.1 What L2 actually unlocks on the web portal

Verified by re-testing every gated route with a confirmed L2 cookie (and various mobile User-Agents — UA does not matter):

- **Only newly-accessible page:** `/portal/massage-chair.php` — renders a QR code (image only, no JSON).
- Every other route below (`dashboard`, `profile`, `account`, `check-in`, `checkins`, `visits`, `history`, `qr-code-reader`, `membership`, `body-scan`, `scans`, `bookings`, `pilates`, `classes`, `bring-a-friend`, `vending`, `discounts`, all `/api/*` routes) **still 302s to `/portal/level-two-feature.php`** even when `membershipLevel == 2`.
- Page byte-lengths of the always-accessible pages (`club-counter`, `rewards/*`) are **identical** for L1 and L2 — no extra data is rendered for L2 users.
- Conclusion: those endpoints don't really exist on the web portal. Visit history / QR check-in / per-club timestamped data lives **only in the mobile app**, which talks to a different backend — the **Netpulse (EGYM)** mobile API (see §7). ~~likely a JSON API gated by an app-issued bearer token~~ **Correction:** Netpulse auth is **not** an opaque bearer token — it's a **form-POST credential login** (`username`/`password`) that sets a `JSESSIONID` cookie, the same session-cookie shape as the web portal. Documented in `app/revo_netpulse.py`.

> Implication for the bot: scraping the web portal will **never** give us per-visit timestamps or the specific club someone checked into. The `Attendance` rows in `ticket-tally.php` (date only, no club, no time) remain the most granular check-in signal available without reverse-engineering the mobile app.

### 1.2 The `Invalid Access! B` guard (new 2026-07) — distinct from L2 gating

`club-counter.php` and `massage-chair.php` — the *only* two pages that
server-rendered dynamic in-club blobs (live occupancy + the access QR) — now
return **HTTP 200, `Content-Type: text/html`, Content-Length 17, body exactly
`Invalid Access! B`** (a PHP `die()` string). Reproduced under every variant:
no-param, `?id=25` (the real fav club), alt param names, every Referer/Origin,
full Chrome UA + `Sec-Fetch-*`, and session-priming.

This is **not** membership gating and **not** a data move:

- **Not L2 gating:** L2-gated routes 302-redirect; this returns a 200 with a
  literal `die()` string and fires even though the `Member` cookie decodes
  `membershipLevel == 2`.
- **Not a data move:** all 9 alternate paths (`api/club-counter.php`,
  `club-counter.json`, `rewards/api/*`, …) 302 → `/portal/`. There is no
  endpoint/JS-var/JSON-key to retarget to. The all-clubs board **cannot** be
  restored from the web.

Pattern is consistent with an **IP / app-context allowlist** (in-club kiosk /
app-webview), not a per-account check. Effect on code: `parse_club_counter()`
now finds none of `clubCounterLists` / `barGraphData` / `favoriteClubId` and
returns `({}, None)`, so `get_club_counter` degrades gracefully.

> ✅ **Correction (2026-07, occupancy restored): the earlier "the all-clubs
> board cannot be restored from the web" conclusion was too narrow — it only
> ruled out the `revocentral` **web** portal.** The live all-clubs counter the
> Revo **iOS app** shows is served by a *different* backend — **PerfectGym
> ClientPortal2** — and a single authenticated GET there returns the live
> head-count for every club at once. `/busy` now reads that (see **§8** and
> `app/revo_perfectgym.py`); the rewards-landing fav-club count (§3.5) is kept
> only as a graceful-degradation fallback.

## 2. Endpoint inventory

Status legend: ✅ accessible at level 1 · 🔒 redirects to `/portal/` (L2 only) · ⛔ access-guarded — 200 + `Invalid Access! B` (§1.2) · 🟡 marketing/static.

> ⚠️ **Redirect-target drift (2026-07):** the 🚫/🔒 gated routes below now `302 → /portal/` (which 403s), **not** `/portal/level-two-feature.php` as the older text says. Accessibility is unchanged (still blocked); only the redirect target moved. `RevoClient._get()` treats any non-login 302 as an empty body, so this is docs-only.

| Method | Path | Status | What it returns |
|---|---|---|---|
| GET | `/portal/login.php` | ✅ | Login form |
| POST | `/portal/login.php` | ✅ | Sets `Member` cookie, 302 → rewards |
| GET | `/portal/` | 403 | (Direct index forbidden) |
| GET | `/portal/api/` | ✅ | Returns literal `:)` — no JSON API mounted here |
| GET | `/portal/club-counter.php` | ⛔ | **Blocked (2026-07):** returns 200 + 17-byte `Invalid Access! B` (see §1.2). The all-clubs board is gone. Fav-club-only live count survives on the rewards landing (§3.5). |
| GET | `/portal/rewards/` | ✅ | Rewards landing (ticket count + favourite-club summary) |
| GET | `/portal/rewards/streaks.php` | ✅ | Current weekly streak + monthly check-in calendar |
| GET | `/portal/rewards/streaks.php?m=<MM>&y=<YYYY>` | ✅ | **JSON** per-day attendance for any month (see §3.2.1) |
| GET | `/portal/rewards/ticket-tally.php` | ✅ | Available tickets + dated history of how each was earned |
| GET | `/portal/rewards/raffle.php` | ✅ | Tickets + countdowns to monthly + major draws |
| GET | `/portal/rewards/raffle.php?optval=<0\|1>` | ⚠️ | **State-changing** — toggles monthly raffle opt-in. JSON `{"Status":"0\|1"}`. Do **not** call casually. |
| GET | `/portal/rewards/prize-pool.php` | ✅ | Same counters + current prize copy |
| GET | `/portal/rewards/faq.php` | ✅ | Static |
| GET | `/portal/rewards/terms-and-conditions.php` | ✅ | Static |
| GET | `/portal/massage-chair.php` | ⛔ | **Blocked (2026-07):** now hit by the same `Invalid Access! B` guard as club-counter (§1.2). (The `API/massage-chair-qr.php` JSON route still works, but the client never used it.) |
| GET | `/portal/API/massage-chair-qr.php?hcId=<id>` | ✅ (L2) | **JSON** `{"qrCode":"qr_<uuid>","validUntilUtc":"<iso8601>"}` — the data the QR actually encodes. `$intHCID` is rendered into the page. |
| GET | `/portal/API/` | ✅ | Returns literal `:)` (parallel to lowercase `/api/`) |
| GET | `/portal/dashboard.php` | 🚫 | 302 → upgrade page **even at L2**. Mobile-app-only. |
| GET | `/portal/profile.php` | 🚫 | 302 even at L2. Mobile-app-only. |
| GET | `/portal/account.php` | 🚫 | 302 even at L2. Mobile-app-only. |
| GET | `/portal/check-in.php`, `/checkins.php`, `/visits.php`, `/history.php` | 🚫 | 302 even at L2. Mobile-app-only — no per-visit data via web. |
| GET | `/portal/qr-code-reader.php` | 🚫 | 302 even at L2. Mobile-app-only. |
| GET | `/portal/level-two-feature.php` | ✅ | Upgrade-prompt page (also the redirect target for unimplemented routes) |
| any | `/portal/api/club-counter.php`, `/portal/club-counter.json`, `/portal/club-counter-data.php`, `/portal/rewards/api/*`, `/portal/rewards/streaks-ajax.php`, `/portal/rewards/data.php`, `/portal/rewards/ajax.php`, `/portal/rewards/raffle-entry.php`, `/portal/membership.php`, `/portal/body-scan.php`, `/portal/bookings.php`, `/portal/pilates.php`, `/portal/bring-a-friend.php`, `/portal/vending.php` | 🚫 | All 302 to upgrade page even at L2. |

> No clean JSON API exists at level 1. Data is **server-rendered into JS variables**
> inside the HTML. Scrape by regex / parse the `<script>` blocks.

## 3. Data shapes

### 3.1 Club Counter — `/portal/club-counter.php`

> ⛔ **Dead since 2026-07** — the page is access-guarded (§1.2) and returns
> `Invalid Access! B`. The shapes below are retained for reference only; nothing
> here can be scraped any more. **The live all-clubs board was restored via a
> different backend — PerfectGym ClientPortal2 (§8) — which is what `/busy` now
> reads.** The rewards-landing fav-club count (§3.5) remains only as a fallback.

Inline `<script>` defined (historically):

```js
clubCounterLists = {
  "Ballarat":   { "shortname": "Ballarat",   "name": "Ballarat",   "id": 78, "in_club": "025" },
  "Braybrook":  { "shortname": "Braybrook",  "name": "Braybrook",  "id": 77, "in_club": "059" },
  "Chadstone":  { "shortname": "Chadstone",  "name": "Chadstone",  "id": 73, "in_club": "095" },
  "Cranbourne": { "shortname": "Cranbourne", "name": "Cranbourne", "id": …,  "in_club": "…"  },
  …  // every Revo club nationwide
};

barGraphData = [
  { "1":10, "2":20, …, "24":10 },   // hour-of-day occupancy, one object per club, same order as the rendered list
  …
];

favoriteClubId = 25;   // the logged-in member's preferred club
```

- `in_club` is a **zero-padded string** of the *current* head-count (refresh to update). **This is the only real, live, per-club signal** — `/busy` uses it.
- `barGraphData[i]["<hour>"]` is *not* real per-club data. Re-checked 2026-06-12 with the live portal: across **76 clubs there are only 2 distinct `hourly` values — 69 clubs return `null` and the other 7 all share one identical hard-coded template** (`{1:10, …, 6:100, 7:100, 8:100, …, 20:110, …}`). It's a vestigial/placeholder busyness curve, **not** an hour-by-hour headcount. Do **not** build a per-club "peak today @ Xpm" or heatmap off it — it would be fabricated. (This invalidates the original feature-D idea in §5.)
- Page does **not** auto-refresh in the background; we must re-`GET` to update.

### 3.2 Streaks — `/portal/rewards/streaks.php`

Visible content:
- Current streak in weeks: `"6 WEEKS"`
- A monthly calendar grid (`May` then `M T W T F S S` columns) with each day rendered as either an empty cell or a marker for an attendance.
- No JS variable exposes the streak as data — extract from DOM:
  - Streak: `re.search(r'>\s*(\d+)\s*WEEKS?\s*<', html)`
  - Day cells: parse the calendar grid; cells with the "attended" CSS class indicate check-in days for that month.

#### 3.2.1 Streaks JSON variant — `streaks.php?m=<MM>&y=<YYYY>`

Discovered in the rewards page's inline `script.js` (the prev/next-month
buttons fetch it via `$.get`). When the route is called with `m` **and** `y`
query parameters, the same PHP endpoint returns a JSON document instead of
the full HTML page (Content-Type is mislabelled as `text/html`):

```json
{
  "month_name": "April",
  "weeks_data": {
    "week1": {"1": null, "2": null, "3": "0", "4": "0", "5": "0", "6": "0", "7": "0"},
    "week2": {"8": "0", "9": "1", "10": "0", "11": "1", "12": "0", "13": "0", "14": "0"},
    "week3": {"15": "0", "16": "1", "17": "0", "18": "0", "19": "1", "20": "0", "21": "0"},
    "week4": {"22": "0", "23": "0", "24": "0", "25": "1", "26": "0", "27": "0", "28": "0"},
    "week5": {"29": "1", "30": "0", "31": "0", "32": "0"},
    "week6": []
  }
}
```

Key points:
- Slot keys (`"1"`..`"42"`) are **grid positions**, not days-of-month. Weeks are Monday-start (matches the `M T W T F S S` header).
- `null` cells = leading/trailing padding for days belonging to the neighbouring month.
- `"0"` = real day with no check-in; `"1"` = real day with a check-in (flame icon).
- Day-of-month is the running count of non-null cells when read left-to-right, top-to-bottom.
- Empty trailing weeks are encoded as a JSON list `[]` rather than `{}` — watch out when iterating.
- Works for any month back to (at least) Jan 2023; pre-account-creation months simply return all zeros.
- This is the **only level-1 source for per-day attendance** — far more granular than `ticket-tally.php` (which exposes only the most recent ~10 entries).

Parsed by `app.revo_client.parse_streak_calendar()` and exposed on the
client as `RevoClient.get_streak_calendar(month, year) -> {dom: bool}`.

### 3.3 Tickets / Attendance log — `/portal/rewards/ticket-tally.php`

> ⚠️ **DOM reorder (2026-07):** each history row is now a three-column grid
> block whose children are, in order, **DATE → DELTA → SOURCE** (it used to be
> DELTA → SOURCE → DATE):
> ```html
> <div class="list … grid grid-cols-3 …">
>   <div class="font-thin">17/07/2026</div>
>   <div class="font-bold">+2 Tickets</div>
>   <div class="font-thin">Attendance</div>
> </div>
> ```
> The old flat regex (`\+?(\d+)\s*Tickets\s*([A-Za-z]+)\s*(date)`) assumed the
> old order, so it paired each source with the **next-older** row's date and
> dropped the newest row. `parse_tickets()` now iterates each `grid-cols-3`
> "list" block and reads its three children positionally (robust against future
> reorders). Also: **deltas doubled to `+2`** for recent grants (rows on/after
> ~08/05/2026); older rows are still `+1`. The int-capture handles both.

Visible content (sample, **current** order date → delta → source):
```
Tickets Available: 31
17/07/2026  +2 Tickets  Attendance
07/07/2026  +2 Tickets  Monthiversary
03/07/2026  +2 Tickets  Attendance
…
08/05/2026  +1 Tickets  Attendance   ← last +1 before the +2 cutoff
07/05/2026  +1 Tickets  Monthiversary
…
07/04/2026  +1 Tickets  Welcome      ← account/rewards start
```
- "Tickets Available" is the headline number (digit-grouped — concatenate the single-digit `<span>` cells before "Tickets Available").
- Each row = `(date_dd/mm/yyyy, delta_int, source_string)` in the DOM; `TicketRow` still exposes them as `(delta, source, date)`.
- ⚠️ **`Attendance` rows are NOT a per-visit check-in log.** Verified 2026-06-12:
  the per-day streaks calendar (§3.2.1) showed check-ins on June 1, 10, 11, while
  ticket-tally's newest `Attendance` row was June 7 — days 10 and 11 never appeared.
  The `Attendance` ticket is a roughly-**weekly reward grant**, dated to *issuance*,
  not to the day the member trained. It lags real visits by days and misses most of
  them. **Use the streaks calendar (§3.2.1) for per-day check-in detection** — that's
  what the attendance poller now does. Ticket-tally is still the source for the
  *ticket balance* and earning history (`/revo_tickets`, `/revo_raffle`).

### 3.4 Raffle / Prize pool

- `raffle.php` — shows `Monthly Draw N days` and `Major Draw N days` countdowns; current ticket balance.
- `prize-pool.php` — same numbers + current prize **copy**. Two blurbs render in
  DOM order `[monthly, major]` as `<div class="py-3 px-1"><p>…</p></div>` blocks
  (e.g. monthly *"EVERY GYM HAS A WINNER! Win Revo merch and 3 months free
  membership!"*; major *"…$50,000 cash or a brand new BYD SEALION 7 car!"*).
  Free-text only — no structured field. Parsed by
  `revo_client.parse_prize_pool(html) -> {"monthly": str|None, "major": str|None}`
  (`RevoClient.get_prize_pool()`); surfaced in `/revo_raffle` + `/revo_summary`.
  Degrades to `None` per side if Revo rewords/moves a blurb.

### 3.5 Rewards landing — `/portal/rewards/`

The landing renders the member's **favourite-club tile** as a single
`<a href=".../portal/club-counter.php?id=<ID>">` block containing:
- the fav club **id** in the href (`club-counter.php?id=25`),
- three single-digit `<span>` cells for the **live head-count** (zero-padded,
  e.g. `0`,`0`,`2` → `2`), and
- the club **name** in a `rounded-full` white pill `<div>` (e.g. `Modbury`).

This is the **only surviving live occupancy signal** now that `club-counter.php`
is guarded (§1.2), and the **replacement source for the favourite club** now
that the `favoriteClubId` JS var died with that page (it was returning `None`).

Parsed by `revo_client.parse_rewards_landing(html) -> (fav_club_id, fav_club_name,
in_club)` and exposed as `RevoClient.get_rewards_landing()`. `/busy` and
`/revo_link`'s fav-club capture both read it. Limitation: it's only the
**session account's own** fav club — not all clubs, and not an arbitrary club a
requesting user names (point them at the Revo app's Live Member Counter).

## 4. Reference scraper

A working crawler that authenticates and dumps each page's parsed data lives at
[scripts/revo_scrape.py](scripts/revo_scrape.py). It uses a single `requests.Session`
and a 30-second cache so we never hammer Revo.

## 5. Possible bot additions

> **Status (2026-06-12):** A/B/C/E/F are now **implemented** in `app/bot.py`. D is
> **not viable** (see §3.1 — `barGraphData` is a shared placeholder template, not
> per-club data). The L2 re-confirmation below means H is still blocked.
>
> Implemented slash commands: `/busy`, `/revo_link`, `/help_revo_link`,
> `/revo_unlink`, `/revo_streak`, `/revo_streak_compare`, `/revo_calendar`,
> `/revo_calendar_compare`, `/revo_summary` (combined dashboard), `/revo_tickets`
> (balance + earning history), `/revo_raffle` (tickets + draw countdowns).
> The attendance poller now also fires a one-off **streak-milestone** celebration
> (4/8/12/26/52 weeks) via `revo_client.streak_milestone()`.
>
> **L2 re-confirmation (2026-06-12):** the research account is now genuinely
> `membershipLevel == 2`. Re-probing every gated route (`check-ins`, `visits`,
> `history`, `dashboard`, `profile`, `streaks-data`, per-club counters, all
> `/api/*`) — they **still 302 to `/portal/level-two-feature.php`** even with a
> real L2 session. §1.1's conclusion holds: per-visit / per-club / per-timestamp
> data is mobile-app-only. The `Attendance` rows in `ticket-tally.php` (date only)
> remain the finest check-in signal on the web portal at any tier.

Scoped to data we can actually read at **level 1**. (Things requiring L2 are noted.)

### A. Live "who's at the gym?" command — high value, easy
- New cog `app/revo.py` with command `!busy [club]`.
- Response: `"Modbury: 57 in club right now (peak today: 110 @ 6pm)"`.
- Uses `clubCounterLists` + `barGraphData`.
- Auto-suggest the user's `favoriteClubId`.

### B. Personal check-in feed — high value
- **Done:** `revo_attendance_poll` (every `REVO_POLL_MINUTES`, default 10) reads the
  per-day streaks calendar (§3.2.1) for each linked user, tracks the most recent
  attended day in `revo_account.last_checkin_date`, and posts to the configured
  notify channel when a newer day appears:
  `"🏋️ @user just checked in at Revo! — streak: 7 weeks 🔥"`.
- ⚠️ **Originally specced against `ticket-tally.php` — that was wrong** (see §3.3):
  ticket rows are a weekly reward grant, not per-visit, so the feed lagged days and
  dropped most check-ins. Driving it off the calendar fixed the "delayed/missed"
  symptom (a June-11 visit that ticket-tally never surfaced is now announced).
- First poll after linking records a silent baseline (no backfill spam).
- (We don't get *which* club without L2, only the date.)

### C. Streak tracker / leaderboard
- Weekly cron: scrape `streaks.php` for each linked Revo account, store `(user_id, week, streak_weeks, days_attended)`.
- Per-day attendance is now also available without scraping HTML \u2014 use
  `RevoClient.get_streak_calendar(month, year)` (\u00a73.2.1) to backfill or
  graph any month's check-ins.
- Discord commands:
  - `!streak` \u2192 personal streak.
  - `!leaderboard streaks` \u2192 server-wide weekly ranking.
  - `!calendar [month]` \u2192 render a per-day attendance grid (uses the JSON variant).
  - Auto-celebrate when someone hits a milestone (4/8/12/26/52 weeks).
- **Done:** `/revo_streak`, `/revo_streak_compare`, `/revo_calendar`,
  `/revo_calendar_compare`, plus a milestone celebration appended to the
  attendance-poll ping (`revo_client.streak_milestone()`).

### D. ~~Heat-map graph integration~~ — not viable
- Originally proposed rendering a 24h heatmap from `barGraphData`. **Abandoned:**
  re-checking the live portal (§3.1) showed `barGraphData` is a single shared
  placeholder template (69/76 clubs return `null`, the rest share one identical
  curve), so any per-club heatmap would be fabricated, not real busyness.

### E. Raffle / draw reminders
- Read `Monthly Draw N days` from `raffle.php`.
- **Done (on-demand):** `/revo_raffle` shows the member's ticket balance plus the
  monthly + major draw countdowns; `/revo_tickets` shows the balance and recent
  earning history; both feed into `/revo_summary`.
- **Still open (push):** a scheduled "Major draw closes in 24h — you have N
  tickets" ping. Would need a dedup cursor (e.g. a `last_raffle_reminder` column
  via the idempotent `Database._migrate()` ALTER pattern) so it fires once per draw.

### F. Multi-account / household linking
- `!link revo <email> <password>` (DM-only, encrypted-at-rest).
  - Validate by hitting `login.php` once.
  - Store credentials encrypted with a key from env (`REVO_KEY`); never log them.
- All per-user features (B/C/E) hang off this.

### G. Lift-day correlation (ties to existing parser)
- When a user logs a lift via the existing parser, look back at their attendance log
  and tag the lift with the inferred check-in date. Enables "average lift on a
  gym day vs rest day" analytics in `app/overview.py`.

### H. ~~Per-club check-in tracking~~ — confirmed unavailable on the web portal
- Re-tested with a verified L2 cookie: `/portal/check-in.php`, `/visits.php`, `/history.php`,
  `/qr-code-reader.php`, `/dashboard.php` all still 302 to the upgrade page. They appear
  to be mobile-app-only routes.
- The mobile app almost certainly hits a separate JSON API with an app-issued token; that
  would need to be reverse-engineered (intercept TLS traffic from the phone) before any
  per-visit, per-club tracking is possible.
- Until then, the `Attendance` rows in `ticket-tally.php` (date only) remain the only
  check-in signal available.

### Suggested first slice (smallest valuable PR)
1. Land `app/revo_client.py` + `scripts/revo_scrape.py` (auth + parsers, no Discord).
2. Add `!busy [club]` (feature A) — read-only, no DB writes.
3. Add `db.py` table `revo_account(user_id PK, email, password_enc, member_id, fav_club_id, last_ticket_date)`.
4. Add `!link revo` and feature B (attendance feed) behind an opt-in flag.
5. Streak leaderboard (C) once we have ≥2 linked users.

## 6. Operational notes / risks

- **Polling rate:** keep ≥10 minutes between hits; one session per user, reuse cookies.
- **Cookie lifetime:** unknown but appears server-session based; re-login on `302 → login.php`.
- **TOS:** scraping their portal probably isn't blessed. Keep it personal-use, low volume,
  and add a kill-switch env var (`REVO_DISABLED=1`).
- **Credential storage:** if we add `!link revo`, encrypt with `cryptography.Fernet`,
  key from `REVO_FERNET_KEY`. Never echo the password back, even in error messages.
- **PHP-serialized cookie:** don't try to parse it — just pass it back verbatim. Re-login
  is cheap.
- **`membershipLevel` upgrade:** if you upgrade to L2, *all* the 🔒 endpoints in §2 become
  worth re-investigating; that's where per-club, per-timestamp data lives.

## 7. Mobile backend — Netpulse (EGYM)

Revo's phone app (`com.netpulse.mobile.revofitness`) does **not** use the
`revocentral` web portal above — it talks to a **Netpulse (EGYM)** white-label
backend at `https://revofitness.netpulse.com/np/`. See **`app/revo_netpulse.py`**
for the read-only client.

- **Auth (corrects the old "bearer token" guess in §1.1):** a **form-POST
  credential login** — `POST /np/exerciser/login` with `username`/`password`
  sets a `JSESSIONID` cookie and returns the exerciser `uuid`. Same
  session-cookie shape as the web portal; no phone-TLS interception needed.
- **Occupancy & check-ins are NOT provisioned for Revo's tenant.**
  `gym-busyness` returns `{"message":"The requested resource does not exist."}`
  and `check-ins/history` returns `{"checkIns": []}`. Every club in the
  directory reports `"mms": "perfectgym"` — **Revo runs member management /
  access / occupancy on PerfectGym, not Netpulse**, so those endpoints are dark
  *here*. That `mms: perfectgym` signal is exactly what pointed us at the
  **PerfectGym ClientPortal2** backend, whose occupancy endpoint **did** restore
  `/busy` (see **§8**). (A per-visit feed is still unavailable.)
- **What Netpulse *does* give:** the member's **membership** (type/subtype/join
  date) and a full **club directory** (name, suburb/state, hours, geo). Those
  are the only two surfaces `app/revo_netpulse.py` exposes.
- **Secrets:** the login + membership responses carry a `JSESSIONID`,
  `externalAuthToken`/`IdToken`/`RefreshToken`, `egymAccountId`, and a
  membership `barcode`/`agreementNumber`/`barcodeExpiresAt` (a live door-access
  credential). The client never logs, returns, or stores them; the parsers are
  the scrubbing boundary.
- **Not the same vendor as bookings:** Revo's studio/pilates bookings run on
  **Arbox** (`revoFitness.arbox.app.com`) — a different backend again, only
  relevant if class bookings are ever wanted.

## 8. Live occupancy — PerfectGym ClientPortal2 (the source that restored `/busy`)

Revo runs member management / access / **occupancy** on **PerfectGym** (every
Netpulse club reports `"mms": "perfectgym"`, §7). The live "Members in club"
counter shown in the Revo **iOS app** is served by PerfectGym's white-label
**ClientPortal2** at `https://revofitness.perfectgym.com/ClientPortal2`, and a
single authenticated GET returns the live head-count for **every club at once**.
This is the **same backend the app uses**, and it is what restored `/busy` to a
real all-clubs live board after the web `club-counter.php` was access-guarded
(§1.2). Implemented in **`app/revo_perfectgym.py`**.

- **Base:** `https://revofitness.perfectgym.com/ClientPortal2`
- **LOGIN:** `POST /Auth/Login`, `Content-Type: application/json`, body
  `{"RememberMe":false,"Login":<email>,"Password":<pw>}` → `200` + a
  `Set-Cookie: CpAuthToken` that a `requests.Session` carries. The **response
  body is the member profile** `{"User":{"Member":{"Id":<int>,
  "HomeClubId":<int>,…}}}` — it is **PII**; the client reads only the non-secret
  `HomeClubId` and never logs the body.
- **OCCUPANCY (all clubs, one call):** `GET /Clubs/Clubs/GetMembersInClubs`
  (send the `CpAuthToken` cookie; no CSRF needed for a GET) → `200` JSON:
  ```json
  {"UsersInClubList":[
    {"ClubName":"Modbury","ClubAddress":"…Modbury SA 5092",
     "UsersLimit":null,"UsersCountCurrentlyInClub":90},
    …78 clubs…
  ]}
  ```
  - `ClubName` (str), `ClubAddress` (str), `UsersCountCurrentlyInClub` (int —
    **the live count**), `UsersLimit` (int|null — capacity, `null` for almost
    every club). **There is no club-id field** in this payload, so the member's
    home-club identity is resolved by *name* via the rewards-landing fav club
    (§3.5), not by `HomeClubId`.
  - Suburb/state are derived per club: **`revo_client.state_for_club(name)` is
    the primary state source**, falling back to a `<Suburb> <STATE> <postcode?>`
    tail parsed from `ClubAddress` (only ~14/78 addresses carry a state token).
  - **Zero counts are real** (closed / overnight) — shown as `0`, not treated as
    missing.
- **Session expiry:** the occupancy GET redirects (3xx, with
  `allow_redirects=False`) or `401`s; the client re-logs-in once and retries
  (mirrors `RevoClient._get` / `revo_netpulse`).
- **Secrets:** the `CpAuthToken` cookie and the profile body are secret/PII. The
  client never logs, returns, or persists them; `parse_members_in_clubs()` is
  the scrubbing boundary (public club fields only). Read-only: only the login
  POST + the occupancy GET.

Exposed as `PerfectGymClient.get_club_occupancy() -> list[ClubOccupancy(name,
suburb, state, count, capacity)]`, with a module-level shared-from-env client +
per-user factory and a ~60s TTL cache (`OCCUPANCY_TTL_SECONDS`) so a burst of
`/busy` calls doesn't re-hit PerfectGym — mirroring the `revo_client` /
`revo_netpulse` patterns.

### 8.1 How `/busy` behaves now

- **No club arg:** shows the member's **home club** live count (identity via the
  rewards-landing fav club, count via the PerfectGym board) **plus a
  "🔥 Busiest right now" top-5 board** — scoped to the user's state when it's
  known (label says e.g. *"in SA"*), else nationwide.
- **With a club arg:** case-insensitively finds that club/suburb in the board
  and shows its count, appending *"X% of Y capacity"* **only when `UsersLimit`
  is not null**.
- **Graceful degradation:** prefers the shared `REVO_USER`/`REVO_PASS` account
  (keeps `/busy` working for unlinked users), then the invoking user's linked
  credentials. If PerfectGym is unavailable/login fails it falls back to the web
  rewards-landing fav-club count (§3.5), then to a clear "live counter
  temporarily unavailable" — `/busy` never hard-errors. Still gated by
  `REVO_DISABLED` + `available()`.
