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
- Conclusion: those endpoints don't really exist on the web portal. Visit history / QR check-in / per-club timestamped data lives **only in the mobile app**, which talks to a different backend (likely a JSON API gated by an app-issued bearer token, not the `Member` cookie).

> Implication for the bot: scraping the web portal will **never** give us per-visit timestamps or the specific club someone checked into. The `Attendance` rows in `ticket-tally.php` (date only, no club, no time) remain the most granular check-in signal available without reverse-engineering the mobile app.

## 2. Endpoint inventory

Status legend: ✅ accessible at level 1 · 🔒 redirects to `/portal/level-two-feature.php` (L2 only) · 🟡 marketing/static.

| Method | Path | Status | What it returns |
|---|---|---|---|
| GET | `/portal/login.php` | ✅ | Login form |
| POST | `/portal/login.php` | ✅ | Sets `Member` cookie, 302 → rewards |
| GET | `/portal/` | 403 | (Direct index forbidden) |
| GET | `/portal/api/` | ✅ | Returns literal `:)` — no JSON API mounted here |
| GET | `/portal/club-counter.php` | ✅ | **Live occupancy for every club** + 24h history |
| GET | `/portal/rewards/` | ✅ | Rewards landing (ticket count + favourite-club summary) |
| GET | `/portal/rewards/streaks.php` | ✅ | Current weekly streak + monthly check-in calendar |
| GET | `/portal/rewards/streaks.php?m=<MM>&y=<YYYY>` | ✅ | **JSON** per-day attendance for any month (see §3.2.1) |
| GET | `/portal/rewards/ticket-tally.php` | ✅ | Available tickets + dated history of how each was earned |
| GET | `/portal/rewards/raffle.php` | ✅ | Tickets + countdowns to monthly + major draws |
| GET | `/portal/rewards/raffle.php?optval=<0\|1>` | ⚠️ | **State-changing** — toggles monthly raffle opt-in. JSON `{"Status":"0\|1"}`. Do **not** call casually. |
| GET | `/portal/rewards/prize-pool.php` | ✅ | Same counters + current prize copy |
| GET | `/portal/rewards/faq.php` | ✅ | Static |
| GET | `/portal/rewards/terms-and-conditions.php` | ✅ | Static |
| GET | `/portal/massage-chair.php` | ✅ (L2) | Renders a QR code image only — no JSON data |
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

Inline `<script>` defines:

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

- `in_club` is a **zero-padded string** of the *current* head-count (refresh to update).
- `barGraphData[i]["<hour>"]` is an integer headcount estimate per hour 1-24, same order as the visible list of clubs on the page (which mirrors `clubCounterLists`). Useful for "busyness" charts.
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

Visible content (sample from this account):
```
Tickets Available: 10
+1 Tickets  Attendance     11/05/2026
+1 Tickets  Attendance     08/05/2026
+1 Tickets  Attendance     07/05/2026
+1 Tickets  Monthiversary  27/04/2026
+1 Tickets  Attendance     23/04/2026
+1 Tickets  Attendance     17/04/2026
+1 Tickets  BONUSDAILY     14/04/2026
+1 Tickets  Attendance     14/04/2026
+1 Tickets  BONUSDAILY     07/04/2026
+1 Tickets  Welcome        07/04/2026
…
```
- "Tickets Available" is the headline number (digit-grouped — concatenate the four `<digit>` cells).
- Each row = `(delta_int, source_string, date_dd/mm/yyyy)`.
- **Attendance rows = effective check-in log.** This is the closest thing to per-user
  visit history available at level 1.

### 3.4 Raffle / Prize pool

- `raffle.php` — shows `Monthly Draw N days` and `Major Draw N days` countdowns; current ticket balance.
- `prize-pool.php` — same numbers + current prize description (HTML-only; no structured field).

### 3.5 Rewards landing — `/portal/rewards/`

Renders ticket digits and the member's favourite-club name (e.g. `"Modbury"`).

## 4. Reference scraper

A working crawler that authenticates and dumps each page's parsed data lives at
[scripts/revo_scrape.py](scripts/revo_scrape.py). It uses a single `requests.Session`
and a 30-second cache so we never hammer Revo.

## 5. Possible bot additions

Scoped to data we can actually read at **level 1**. (Things requiring L2 are noted.)

### A. Live "who's at the gym?" command — high value, easy
- New cog `app/revo.py` with command `!busy [club]`.
- Response: `"Modbury: 57 in club right now (peak today: 110 @ 6pm)"`.
- Uses `clubCounterLists` + `barGraphData`.
- Auto-suggest the user's `favoriteClubId`.

### B. Personal check-in feed — high value, requires inferred check-ins
- Poll `ticket-tally.php` every ~15 minutes per linked user.
- On a new `Attendance` row, post to a configured Discord channel:
  `"@user just checked into the gym (streak: 7 weeks) 💪"`.
- Store last-seen ticket date per user in `db.py` to dedupe.
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

### D. Heat-map graph integration
- Reuse `app/graphing.py` to render a 24×7 heatmap from `barGraphData` for any club.
- `!heatmap Modbury` → posts an image.

### E. Raffle / draw reminders
- Read `Monthly Draw N days` from `raffle.php`.
- Schedule pings: "Major draw closes in 24h — you have N tickets".

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
