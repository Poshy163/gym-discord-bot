# Revo Fitness Client Portal — Reverse-Engineering Notes

> ⚠️ **Security note:** Credentials have been shared in plaintext during research
> more than once (most recently 2026-07). **Rotate the Revo password**, and never
> commit credentials to the repo. Treat these notes as "what we discovered" —
> usage terms of revofitness.com.au may restrict scraping. Use a single
> low-frequency poll, identify the bot in `User-Agent`, and stop if they object.
>
> 🧭 **Probing this again?** Read **§9.3** (mine the Angular templates — the
> highest-yield discovery channel, and ~30 templates are still unmined), **§8.1**
> (PerfectGym status taxonomy) and **§7.1** (Netpulse response oracle) *before*
> spending requests. Several conclusions in older revisions of this file were
> confidently wrong; each corrected one is marked ✅ **Correction** inline.

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

> ⛔ **Spread (2026-08): the guard now also covers `rewards/streaks.php` and
> `rewards/raffle.php`.** It began on `club-counter.php` / `massage-chair.php`;
> as of ~2026-08 both the streaks page (**HTML *and* the `?m=&y=` calendar
> JSON**) and the raffle page return the same 17-byte `Invalid Access! B`. This
> is what **broke the attendance tracker** — the poller read the streaks
> calendar. Re-probed exhaustively (Referer, full Chrome UA, `Sec-Fetch-*`,
> `X-Requested-With`, `X-Forwarded-For`, param/case/path variants, warm-up GET of
> the rewards index first, and a fresh re-login): **all return the identical 17
> bytes**, so it is no more client-settable here than it was for club-counter.
> **Still working (2026-08):** the rewards landing (`/portal/rewards/`),
> `ticket-tally.php` and `prize-pool.php`. See §3.2.4 for how the poller fell back
> to ticket-tally, and `revo_client.is_access_guarded()` / `RevoAccessGuarded`
> for how the code now distinguishes this from an auth/parse failure.

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
| GET | `/portal/rewards/streaks.php` | ⛔ | **Blocked (2026-08):** 200 + 17-byte `Invalid Access! B` (§1.2). Was: current weekly streak + monthly check-in calendar. |
| GET | `/portal/rewards/streaks.php?m=<MM>&y=<YYYY>` | ⛔ | **Blocked (2026-08):** same guard. Was: **JSON** per-day attendance (§3.2.1). This is the feed the attendance poller lost. |
| GET | `/portal/rewards/ticket-tally.php` | ✅ | Available tickets + dated history of how each was earned. Now also the **attendance-poll fallback** (§3.2.4). |
| GET | `/portal/rewards/raffle.php` | ⛔ | **Blocked (2026-08):** 200 + 17-byte `Invalid Access! B` (§1.2). Was: tickets + countdowns to monthly + major draws. |
| GET | `/portal/rewards/raffle.php?optval=1` | ⚠️ | **State-changing** — a pure **TOGGLE** of monthly raffle opt-in (both buttons send `optval=1`; it is *not* a `0`/`1` setter). JSON `{"Status":"0\|1"}`. **Never call this** — read the opt state from the DOM instead (§3.4). |
| GET | `/portal/rewards/major-prize-winners.php` | ✅ | Past major-draw dates + winners (initial, surname, postcode). Real draw dates; ⚠️ third-party PII (§3.4). |
| GET | `/portal/rewards/prize-pool.php` | ✅ | Same counters + current prize copy |
| GET | `/portal/rewards/faq.php` | ✅ | Static |
| GET | `/portal/rewards/terms-and-conditions.php` | ✅ | Static |
| GET | `/portal/massage-chair.php` | ⛔ | **Blocked (2026-07):** now hit by the same `Invalid Access! B` guard as club-counter (§1.2). (The `API/massage-chair-qr.php` JSON route still works, but the client never used it.) |
| GET | `/portal/API/massage-chair-qr.php` | ✅ (L2) | **JSON** `{"qrCode":"qr_<uuid>","validUntilUtc":"<iso8601>"}` — the data the QR actually encodes. ⚠️ **`hcId` is NOT required** (a bare GET returns a valid 200), and this **mints an access credential** on every call, so don't call it casually. `validUntilUtc` is the only second-resolution timestamp on the portal — a rolling `now + TTL` expiry, *not* an event time, but it does prove the backend **can** emit sub-day times, so the calendar's day-granularity is a schema choice rather than a platform limit. |
| — | `/portal/api/` vs `/portal/API/` | ✅ | The **same directory** — IIS is case-insensitive. Both index pages return the literal `:)`. |
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

#### 3.2.2 Check-in latency — why the feed can never be real-time

The attendance feed is **structurally incapable of being real-time**, and it is
worth recording exactly why so nobody re-litigates it.

**1. There is no timestamp anywhere.** The calendar's finest unit is a *day*: a
cell is `"0"` or `"1"`. No Revo backend exposes when a member walked in. This
isn't an assumption — the entire member-facing API surface has been enumerated:
- `revocentral` — every check-in/visit/history route 302s even at L2 (§1.1, §2).
- **PerfectGym ClientPortal2 — exhaustively mapped.** Both discovery channels
  were run to completion: the JS bundles, *and* all 101 fetchable Angular
  templates (§9.3), yielding **56 declared API routes**. None is a visit log. A
  35-name sweep for `Visits`/`Entrances`/`Attendance`/… returned the bogus-route
  404 for every spelling (§12.1). `Profile/Skills/GetMemberActivities` — the last
  plausible candidate, PerfectGym's activity-tracking module — returns `[]`;
  Revo doesn't use it.
- Netpulse — `check-ins` in all 6 spellings 404, and `exerciser/{uuid}/stats` is
  all zeros (§7.1).

> ✅ **The decisive evidence: Revo's own tenant feature flags.** The SPA shell
> injects `cpConfig`, and its `features` array *is* the definitive list of what
> PerfectGym has switched on for Revo:
> ```
> Login, LoginPassword, LoginMyWellness, ContractDetails, AddContract,
> AddAdditionalContract, UpgradeContract, UpgradeContractAfterCommitment,
> FreezeContract, ChangeContractPaymentSource, AddNewPaymentSource,
> PayContract, Classes, ShowAgreementsOnUserProfile
> ```
> Contracts, payments, classes, agreements. **There is no visits, attendance,
> activity or check-in feature on the tenant at all** — so the endpoints aren't
> hidden, they are not provisioned. This is stronger than any amount of probing
> and it is the reason to stop looking here. Re-read this list before re-opening
> the question; if Revo ever enables such a feature it will appear here first.

> **And there is no push channel.** The shell loads a `signalR` bundle, which
> looks promising — but it is served **0 bytes**, and every hub mount point
> (`/signalr`, `/signalr/negotiate`, `/signalr/hubs`, `/hubs`, `notificationHub`,
> …) 404s under `/ClientPortal2`. SignalR is referenced by the white-label
> framework and switched off for this tenant. No websocket, no server push.

**The rewards counters all share ONE refresh job.** The obvious hope is that some
other rewards value updates on a faster schedule than the calendar. It doesn't:
fetched in one near-simultaneous pass, the landing's streak (3) matches
`streaks.php`'s "3 WEEKS", and the landing's ticket tile (35) matches
`ticket-tally.php`'s balance (35). A mismatch would have been direct evidence of
separate jobs; there is none. `ticket-tally` is strictly *slower* (its newest
`Attendance` row lagged 4 days, date-only).

**The calendar cannot be coaxed into returning more.** `streaks.php?m=&y=` was
re-requested with `d=`, `day=`, `detail=`, `full=`, `format=`, `verbose=`,
`times=` and with `X-Requested-With: XMLHttpRequest` — **all eight returned a
byte-identical body** (same sha256, same 387 bytes). The cell universe is strictly
`null` / `"0"` / `"1"`.

**Also ruled out (2026-07), so nobody re-runs these:**

- **No undiscovered Revo host.** `api.`, `app.`, `my.`, `members.`, `member.`,
  `portal.`, `mobile.`, `account.` and `auth.revofitness.com.au` are all
  **NXDOMAIN**. The marketing site names no app backend, and a grep of its 1.26 MB
  of theme JS finds no occupancy/attendance API. `revocentral` remains the only
  member host on the domain.
- **The current phone app is still the Netpulse/EGYM one** (App Store listing
  credits EGYM and points its privacy URL at netpulse.com) — i.e. the backend we
  already exhausted, not a newer PerfectGym-native app.
- **Netpulse has exactly one push surface and it is dark.** Of 29 candidate
  push/feed/sync/device/gamification routes, 27 are 404. The survivors:
  `exerciser/{uuid}/rewards` (500, the provisioned-but-dark control) and
  `exerciser/{uuid}/notifications`, which returns
  `{status, errors, data:{lastCheckTime:<epoch-ms>, notifications:[]}}`. The
  array is **empty even with `?lastCheckTime=0`** (a cursor reset that would
  surface any retained history), and `lastCheckTime` is a client poll cursor, not
  a visit time. The tenant provisions no push categories at all, so a check-in
  notification could never appear there.
- **Arbox** (`api.arboxapp.com`, Revo's studio-booking vendor) is the one genuinely
  new member-data host, but it is WAF-protected, behind separate credentials, and
  its "check-in" is *class attendance* — not the 24/7 gym-floor entry the bot
  announces. Not a drop-in replacement, and not pursued.
- **A `/portal/API/` directory exists** (index returns `:)`, listing disabled). All
  18 read-named check-in / visit / last-visit / attendance guesses 302 to the same
  catch-all `level-two-feature.php` that `robots.txt` and `sitemap.xml` hit — the
  portal's uniform "no such route" signal, and cleanly distinguishable from
  `massage-chair-qr.php`'s real 200 JSON.
- **The only time-of-day field on the whole portal** is
  `API/massage-chair-qr.php` → `validUntilUtc`. It is a **QR expiry minted at
  request time** (returned even with no `hcId`), not a record of a visit. Useless
  as a check-in signal — and it mints an access credential, so don't call it.
- **The `Invalid Access! B` guard is not client-settable.** 21 requests (3 targets ×
  7 header shapes: bot UA, full browser UA, `X-Requested-With`, `Referer`,
  `Origin`, `Sec-Fetch-*`, `X-Forwarded-For`) all returned the identical 17-byte
  body. It keys on something we cannot set from here — consistent with the
  IP/app-context theory in §1.2, and not worth further attempts.

So the best any implementation can ever say is **"trained today"**, never
"checked in at 6:42am". The announcement wording reflects that deliberately.

**2. Revo batches attendance in, roughly twice a day.** Observed behaviour is
that a day flips to attended around **05:00 and 17:00 UTC** (≈2:30pm / 2:30am
Adelaide — the clean UTC alignment is what gives the two-batch reading away).
A morning session therefore surfaces hours later, at the next batch.

> ⚠️ This cadence is **inferred**, not measured — from observed announcement
> clustering plus the UTC alignment. `_poll_one_account` now logs
> `Revo check-in detected user=… visit_date=… detected_at=… lag_days=…` every
> time the cursor advances, so a few weeks of logs will confirm or correct it
> (and catch it changing). Check there before assuming these times.

**The delay is upstream of HTTP — there is no cache to bust.** `streaks.php` was
fetched three times 20 s apart: the responses were byte-identical, and carried
**no `Last-Modified`, no `ETag`, no `Age` and no `Cache-Control`** (bare
`Date` + `Server: Microsoft-IIS/10.0`). So the page is generated fresh per
request and reflects the rewards database *immediately*; nothing is being served
stale to us. That has two consequences:
1. The lag is in **Revo ingesting turnstile data into the rewards database**, not
   in delivery. No request-shaping, cache-busting header or query parameter can
   move it — this is not a client-side problem.
2. The cadence is **not observable from the protocol**. Headers reveal nothing
   about when the underlying data last changed, so the only way to measure it is
   to watch the data itself flip — which needs a real gym visit. That is exactly
   what the new detection log line is for.

**Consequences for the poller.** The poll interval is *not* the bottleneck —
`REVO_POLL_MINUTES` defaults to 10, against a source that changes twice a day.
Lowering it buys at most a few minutes and costs proportionally more requests
against a portal the notes ask us to treat gently (§6). What *is* worth doing:
- **Skip the fetch once today is already recorded.** The cursor only advances to
  a newer day, so between a detected check-in and midnight there is nothing left
  to learn. `_poll_one_account` returns early in that case, removing the majority
  of a training day's requests.
- **Don't say "just".** By the time the batch lands the session may be many hours
  old.

#### 3.2.3 The only real-time path: external presence, not Revo

Since no Revo backend can ever say *when* someone trained, genuinely real-time
detection has to come from a signal the member already emits. This project
already has one wired up: **Home Assistant** (`docs/HOME_ASSISTANT.md`).

The pieces are all in place, which is what makes this the realistic option:
- `ha_poll` already runs every `HA_POLL_MINUTES` (default 10, **floor 1**) and
  already calls `GET /api/states` per linked member — so `person.*` and
  `device_tracker.*` entities are **already in hand every cycle**.
  `_ha_visible_states` filters only operator-ignored entities, not by domain.
- HA's companion app sets a `device_tracker`/`person` state to the **zone name**
  on crossing, and exposes `latitude`/`longitude`/`gps_accuracy` attributes.
- We already have every club's coordinates and `revo_perfectgym.haversine_km`.

A 150 m geofence is unambiguous nationwide: excluding the two co-located
Nunawading sites, the closest pair of Revo clubs is **1.45 km apart**. Use the
**Netpulse** coordinates (§7.2) — PerfectGym's are rounded (§9) and would put the
fence in the wrong place for 25 clubs.

> ⚠️ **Not built, deliberately.** This is location tracking of members, which is a
> different privacy proposition from reading a gym's attendance flag, and it is
> the member's decision rather than the operator's. If it is ever built it must be
> strictly opt-in per member, and it should degrade to the Revo calendar rather
> than replace it. Note also that *being at the club* is not the same fact as
> *having checked in* — someone can walk past — so the honest design is to
> announce on presence and let the calendar confirm the visit later.

#### 3.2.4 Attendance poller fallback after the streaks guard (2026-08)

When the `Invalid Access! B` guard spread to `streaks.php` (§1.2), the attendance
poller — which read the per-day streaks calendar — went silent: the guarded page
parses to an empty calendar, so `latest_attended_day` was always `None` and the
cursor never advanced. No crash, just no announcements.

**Fix:** `RevoClient.get_latest_attendance(month, year)` now prefers the
calendar and **falls back to the newest `Attendance` grant on `ticket-tally.php`**
(which still renders) when the calendar raises `RevoAccessGuarded`. It returns an
`AttendanceInfo(date, source, streak_weeks)` so the poller can word the ping
honestly:

- `source == "calendar"` — a real per-day visit; announces "trained at Revo
  today / (day)" with the weekly streak.
- `source == "tickets"` — the ticket-tally `Attendance` grant, a **coarse,
  roughly-weekly reward dated to *issuance*** (§3.3), not the training day. The
  ping says "**has been training at Revo recently**" and omits the streak (the
  streaks page is guarded too, so there's no reliable number — do **not** try to
  reconstruct one from the irregular grant cadence).
  > ⚠️ Not "today", and **not "this week" either**: a grant issued Tue can reward
  > training from the *previous* week, so a week-level claim is falsifiable in
  > ordinary use. "Recently" is the true resolution of this signal.

`AttendanceInfo.streak_readable` is the other half of the contract, and it exists
because "the streak is 0/unknown" and "we couldn't read the streak" must not be
conflated when the value is **cached**:

- **readable** (the page answered, even with `None`) → write it through. A streak
  really can lapse, and freezing a stale one mis-ranks the streak leaderboard and
  feeds the AI-coach payload (`last_streak_weeks`) a number the member no longer
  has.
- **unreadable** (guarded *or* a transient 5xx/timeout) → keep the last known
  value rather than nulling it, and suppress the leaderboard on that ping, since
  nobody's cached value is being refreshed while the page is down.

The streak is read on **every** poll — including polls where this month holds no
visit yet — for the same reason: reading it only alongside a check-in would let a
cached streak freeze for anyone mid-gap and never clear for someone who churned.
It is also read **defensively**: any failure degrades to "no streak tail", never
an aborted poll. Letting a transient streaks.php error escape would drop a real
check-in announcement entirely, which is the worse failure.

Trade-offs to remember before "improving" this: the ticket fallback lags real
visits by days and collapses several visits in a week into one grant, so it will
announce at most ~weekly, never per-visit. It is strictly worse than the calendar
— it's what's left. If Revo ever lifts the guard, `get_latest_attendance`
transparently goes back to the calendar (calendar-first), no code change needed.
The cursor (`last_checkin_date`) tolerates the source switch because it only ever
advances to a lexicographically newer ISO date.

**One more guard consequence worth not re-discovering:** `raffle.php` is the only
readable source of monthly-draw **opt-in state** (§3.4), so while it is guarded
`/revo_raffle` must not describe a member's tickets as "**in the draw**" — an
opted-out member would be told their tickets are in play when they aren't. The
command drops that phrase (showing only the count) whenever the raffle page
didn't answer.

#### 3.2.5 Seeing this happen next time

The reason the 2026-08 breakage was invisible is that **this portal fails
silently**: the guard returns HTTP 200, so every request still "succeeds" and the
calendar merely parses to empty. Two things now make that observable — worth
knowing about before spending an afternoon re-deriving it:

- **`scripts/revo_health.py`** — probes every source in one pass and prints
  whether check-in tracking is healthy / degraded / down. Exit code `0`/`1`/`2`
  respectively (`3` = couldn't log in), so it also works as a cron canary. This is
  the fastest way to tell **when Revo lifts a block** and per-day tracking
  resumes. Backed by `revo_client.probe_sources()` +
  `attendance_feed_state()`.
- **The poller logs source changes**, once per change rather than per cycle:
  `Revo attendance feed DEGRADED …` when it drops to the ticket fallback, and
  `… RECOVERED …` when the calendar comes back.

Both are read-only and cost one request per source — diagnostics, not polls (§6).

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
- ✅ **Correction (2026-07): the page renders the FULL history, not "the most
  recent ~10 entries"** as this section used to claim. A live fetch returned all
  22 rows back to the `Welcome` row with no pagination, and the parsed row deltas
  **sum exactly to the headline balance** — so no row is dropped, double-counted,
  or mis-paired with the wrong date.
- ✅ **A fourth source label is live: `BONUSDAILY`** (alongside `Attendance`,
  `Monthiversary`, `Welcome`). Any code that switches on the source string has to
  tolerate it.
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
- ⚠️ **The countdown block is server-rendered with `style="display: none"` for a
  member who is opted OUT — and `parse_raffle` happily scrapes numbers out of it.**
  This was a live user-visible bug: `/revo_raffle` told an opted-out member
  "**35** tickets in the draw · Monthly draw: in **6 days**" when their tickets
  were not entered in anything.
  - **Opt state is readable from the DOM.** The page renders *both* buttons and
    hides the one that doesn't apply, so a **visible `#optIn` means the member is
    currently OUT**:
    ```html
    <button id="optOut" … data-opt-val="1" style="display: none">
    <button id="optIn"  … data-opt-val="1" >
    ```
  - Parsed by `revo_client.parse_raffle_optin(html) -> bool | None` (`None` when
    the page didn't render a readable state — never a silent `False`), and served
    together with the countdowns by `RevoClient.get_raffle() -> RaffleInfo`, which
    pairs them off a single fetch so a caller can't announce one without the other.
  - `/revo_raffle` and `/revo_summary` now warn when `opted_in is False`, and only
    ever for the member's **own** account — opt state is personal and both replies
    are public.
  - ✅ **Correction to §2's table: `optval` is a pure TOGGLE, not a `0`/`1`
    setter.** `script.js` sends `data-opt-val="1"` from *both* buttons and then
    shows/hides based on the returned `Status`. There is no read-only variant of
    that request and no way to *set* a specific state — which is the other reason
    the DOM is the only safe way to read this.
- **`major-prize-winners.php`** (✅ L1, **new to these notes**) — the only place on
  the portal that exposes actual **draw dates and outcomes**: one `<h3>` per draw
  under `<section id="major-draw-winners">`, formatted
  `Major draw (DD/MM/YYYY) <Initial>. <Surname> <postcode>, …`. Observed cadence is
  **weekly (Mondays)**, which contradicts the single "Major Draw 6 Days" countdown
  — worth resolving before anyone builds the §5-E push reminder.
  ⚠️ **Third-party PII**: other members' surname + postcode. Revo publishes it to
  logged-in members, but do **not** repost individual winners into Discord.
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
  `"🏋️ @user trained at Revo today! — streak: 7 weeks 🔥"`.
  (Wording is deliberately *not* "just checked in" — the signal is a batched
  per-day flag, so the session is often hours old by the time we see it. See
  §3.2.2 for why real-time is structurally impossible.)
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

### 7.1 The response oracle (2026-07) — read this before probing Netpulse again

An unauthenticated calibration pass established what each response actually means.
This costs **zero logins**, because 403-vs-404 is observable without auth:

| Response | Meaning |
|---|---|
| `403 {"message":"Access is denied"}` | Route **exists** and is protected (proved against the known-good `exerciser/{uuid}/membership`) |
| `500 {"message":"General Error"}` | Route exists **and is wired**, but the tenant's backing service is unconfigured — the genuine "provisioned-but-dark" signature |
| `404 {"message":"The requested resource does not exist."}` | **Not in the routing table at all** |

> ⚠️ **Correction — this invalidates a documented inference.** The older notes read
> `{"message":"The requested resource does not exist."}` as *"Netpulse knows the
> route but Revo's tenant lacks it"*. It is simply Netpulse's **generic 404 body**:
> a bogus control route (`zzz-no-such-route-xyz`) returns it byte-for-byte. So
> `gym-busyness` is not a "dark tenant feature" — **that route does not exist**.
> Likewise `check-ins/history` does **not** return `{"checkIns": []}`; it 404s, as
> do six other spellings. The end conclusion (no per-visit feed on Netpulse) is
> unchanged, but the evidence behind it was weaker than documented. Exactly one
> route shows the true provisioned-but-dark 500: `exerciser/{uuid}/rewards`.

- **Occupancy & check-ins are unavailable here.** Every club in the directory
  reports `"mms": "perfectgym"` — **Revo runs member management / access /
  occupancy on PerfectGym, not Netpulse.** That `mms: perfectgym` signal is
  exactly what pointed us at the **PerfectGym ClientPortal2** backend, whose
  occupancy endpoint **did** restore `/busy` (see **§8**). (A per-visit feed is
  still unavailable — see §12.)
- **What Netpulse *does* give:** the member's **membership** (type/subtype/join
  date) and — the valuable one — a full **club directory** (§7.2).
- **Dead, but provisioned:** `exerciser/{uuid}/stats` returns 200 with every
  counter a hard zero (workouts/calories/distance/goals) — Netpulse's
  workout-tracking module is unused by Revo, and this is *not* a visit counter.
  `exerciser/{uuid}/challenges` and `challenges` both return `[]`.

### 7.2 Club directory — `GET company/children` (**NO AUTHENTICATION**)

The richest public club record Revo publishes, and the **only** source of opening
hours, a phone number, and each club's IANA timezone. Implemented as
`revo_netpulse.fetch_club_directory()` / `shared_club_directory()` (6h TTL).

> ✅ **It needs no credentials at all.** A bare session with no `JSESSIONID`
> returns 200 with a byte-identical body. `NetpulseClient.get_clubs()` was calling
> `_ensure_login()` for nothing. The credential-free path means hours and geo cost
> no account risk and have no session to expire — which is why `/revo_clubs` uses
> it for every caller, linked or not.

> ✅ **`responseType` is ignored.** `basic`, `full` and no parameter at all return
> the same bytes (sha256 `986a41b8…`, 77707 B, 77 clubs). The old "try
> `responseType=full`" note is moot — `basic` was returning every field the whole
> time; the client was **discarding** them, not failing to request them.

Per club: `uuid`, `name`, `timezone` (IANA, 77/77), `phone`, `email`, `url`,
`mms`, `gymChainId`, `photos`, `workingHours`, `workingHoursFreeText`, and
`address{addressLine1, city, postalCode, stateOrProvince, lat, lng}`.

- **`stateOrProvince` is a poor state source** — null for 64 of 77, and one club
  spells it `"Victoria"` rather than `"VIC"`. Use
  `revo_perfectgym.STATE_BY_STATE_ID` (§9) instead.
- **Hours are human copy, not structured data.** Across all 77 clubs (539 day
  cells) there are only these shapes:
  - `"Open 24 Hours\nStaffed from 9am - 8pm"` — **420 cells**, two distinct facts
    packed into one string. The staffed window must **not** be read as the opening
    window; the club is card-accessible all night either way.
  - `"00:00-23:59"` — 14 cells, the machine-readable form of 24/7.
  - `"Open from 5:30am - 10pm"` / `"Open 7:00am-7:00pm"` / `"Open from 6am - 9pm"`
    — ~35 cells across the handful of limited-hours clubs.
  - 10 clubs have `workingHours: null`; **7 of them** still describe their hours in
    the HTML `workingHoursFreeText`, which the parser falls back to. Only
    **Dayton, Noarlunga and Nunawading (Original)** end up with no hours at all.
- Parsed by `revo_netpulse.parse_day_hours()` / `parse_hours()` into `DayHours`,
  and evaluated by `club_status(club, now)` → `ClubStatus(open_now, staffed_now,
  always_open, local_time, today_raw)`. **Everything degrades to `None`, never to
  a guessed "closed"** — an unknown timezone, an undescribed weekday or
  unrecognised copy all read as "unknown", because a wrong "closed" is the one
  answer that costs someone a trip. Evaluation happens in the **club's own**
  timezone: Revo spans four, so a server-local comparison would be up to three
  hours out.

#### 7.2.1 Joining Netpulse hours to the PerfectGym directory

The two backends **disagree on four club names** — Netpulse appends a state or
city (`Knoxfield Vic`, `Rivervale WA`, `Pitt St Sydney`) and spells one site
`Nunawading (Original)` where PerfectGym uses `Nunawading OG` (whose `FullName`
is `Nunawading - (Original)`).

`revo_netpulse.normalise_club_name()` reconciles all four: lowercase, strip to
bare alphanumerics, drop a trailing state/city token. Matched against **both**
PerfectGym's `Name` and `FullName` it gives a clean **1:1 join across all 77
clubs**, and the only unmatched PerfectGym entries are the two that haven't opened
(Netpulse lists a club once it trades).

> ⚠️ **Do not join these two directories by coordinates.** It looks tempting —
> both carry lat/lng, 74 of 77 nearest-matches land within 500 m — but the two
> Nunawading sites are **138 m apart**, so nearest-neighbour silently hands
> `Nunawading` the *other* club's hours and leaves `Nunawading OG` unmatched.
> That is a wrong answer that looks entirely plausible. Match by name.
- **Secrets:** the login + membership responses carry a `JSESSIONID`,
  `externalAuthToken`/`IdToken`/`RefreshToken`, `egymAccountId`, and a
  membership `barcode`/`agreementNumber`/`barcodeExpiresAt` (a live door-access
  credential). The client never logs, returns, or stores them; the parsers are
  the scrubbing boundary. (In practice the three `external*Token` fields and
  `egymAccountId` are **null** on Revo's tenant — the scrubbing still stands, but
  don't expect to find them populated.)
- ⚠️ **`exerciser/{uuid}/profile` is a NEW secret class — deliberately not wired
  up.** It returns 200 with real data and carries `birthday`, `weight`,
  `gender`, `phoneNumber`, `email`, `firstname`/`lastname`, `barcode`, **and
  `clientLoginId` / `clientLoginPasscode`** — the last two are login credentials
  not listed anywhere else in these notes. The only genuinely new *non*-sensitive
  facts it adds are `birthday`, `weight` and the member's own `timezone`. If it is
  ever implemented, the parser must scrub the credential fields at the boundary
  the way `parse_membership` does, and nothing about it belongs in a public
  channel.
- **Not the same vendor as bookings:** Revo's studio/pilates bookings run on
  **Arbox** (`revoFitness.arbox.app.com`) — a different backend again, only
  relevant if class bookings are ever wanted.

### 7.3 The app front — probed 2026-07, nothing new

Revo's phone app is the client for this backend, so "does the *app* see more than
we do?" is a fair question. It doesn't. Four angles, all negative:

- **`X-NP-API-Version` is ignored.** `1.0`, `1.5`, `2.0`, `3.0`, `4.0`, `5.0`,
  `99.0` and omitting the header entirely all return **byte-identical** bodies
  (same sha256). There is no newer API generation to opt into, and it does not
  enrich an authenticated payload either — `exerciser/{uuid}/membership` returns
  the same 16 keys at every version.
- **`devicePlatform` is ignored.** `ANDROID` / `IOS` / `WEB` are byte-identical,
  so the iOS app (the one that shows the live counter) gets no special surface.
- **There is no app/tenant config blob.** 34 of 38 candidates 404 —
  `config`, `settings`, `features`, `modules`, `brand`, `tenant`, `version`,
  and the `company/{uuid}/…` variants of each. Netpulse has no equivalent of
  PerfectGym's decisive `cpConfig` list.
- **`company/{clubUuid}/classes` returns `[]`** — this was flagged as "the single
  best follow-up on this backend"; it is now tested and empty, matching the
  PerfectGym class calendar (§9.2).
- **No workout read surface.** The app advertises workout tracking, but all 21
  `exerciser/{uuid}/{workouts,trainings,sessions,logs,history,…}` spellings are
  ABSENT. Only a **top-level `workout` exists, and it is `405` (POST-only)** — the
  app *submits* workouts, and reading them back is not provisioned for this
  tenant. Consistent with `exerciser/{uuid}/stats` being all zeros (§7.1).

**What the app's own store listing claims** is the useful corroboration, because a
feature it advertises would need an endpoint behind it:

| Advertised feature | What backs it | Already ours? |
|---|---|---|
| "Live Member Counter" | anonymous per-club head-count | yes — `/busy` (§8) |
| "Manage Your Membership" | PerfectGym contracts | yes — §10, §14.1 |
| "Track your workouts" | *self-logged* workouts, POST-only | n/a (that's Hevy's job) |
| "Scan for Access" | the app **emits** the door QR | — see below |
| "Connect your Devices" | Apple Health / Fitbit / Garmin / Strava | third-party |

> 🔑 **The listing never offers a visit or check-in history** — and "Scan for
> Access" explains why. The app's role in a check-in is to **emit the credential**,
> not to record the event. The record lands on PerfectGym's *access-control /
> operator* side, exactly where §12 places visit history. There is nothing to read
> back because the member-facing product was never designed to show it.

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

### 8.1 Response taxonomy — how to tell a real route from a missing one

Established by probing with bogus controls alongside every real call. Without
this, an absent route and a real-but-misparameterised one are indistinguishable
and a sweep produces nonsense.

| Response | Meaning |
|---|---|
| `404` + a ~17 KB HTML error page | **No such route.** |
| `400` + `Error occured. CorrelationId:"…"` | Route **exists**; params missing/wrong. |
| `499` + that same CorrelationId body | Route exists, handler ran and refused — **also proves existence**. |
| `403` | Route exists; this member/tenant isn't entitled (e.g. `Family/*`, `Files/*`). |
| `200` + bare `null`, `[]`, or 0 bytes | Route exists but had nothing to say — **often a bad argument rather than a real empty result** (see the `DailyClasses` trap in §9.2). |

⚠️ A `200` is the weakest signal here, not the strongest. Always run the same call
with a deliberately invalid argument: if the bogus and the real call return the
same bytes, the response carries no information about your argument at all.

### 8.2 How `/busy` behaves now

- **No club arg:** shows the member's **home club** live count (identity via the
  rewards-landing fav club, count via the PerfectGym board) **plus a
  "🔥 Busiest right now" top-5 board** — scoped to the user's state when it's
  known (label says e.g. *"in SA"*), else nationwide.
- **With a club arg:** case-insensitively finds that club/suburb in the board
  and shows its count, appending *"X% of Y capacity"* **only when `UsersLimit`
  is not null**. **Geo enrichment (2026-07):** the named-club line now also joins
  the board to the club directory (`join_occupancy_to_dir`, §8.2) and appends a
  **Google-Maps link** when the directory has coordinates for that club —
  best-effort, so a directory outage silently omits the link and never breaks
  `/busy`. State scoping continues to use `state_for_club` (unchanged).
- **Graceful degradation:** prefers the shared `REVO_USER`/`REVO_PASS` account
  (keeps `/busy` working for unlinked users), then the invoking user's linked
  credentials. If PerfectGym is unavailable/login fails it falls back to the web
  rewards-landing fav-club count (§3.5), then to a clear "live counter
  temporarily unavailable" — `/busy` never hard-errors. Still gated by
  `REVO_DISABLED` + `available()`. The shared-first-then-linked resolution is
  hoisted to `bot._perfectgym_occupancy(user_id)` / `_perfectgym_directory(user_id)`
  so `/revo_clubs` reuses the exact same logic + TTL caches.

## 9. Club directory + geo — `Geo/GetClubList` (public, no PII)

The occupancy board (§8) carries **no club-id and no coordinates**. The public
club directory does — it's the same list the "find a club" map draws from — so
it's joined to occupancy *by name* for ids/geo. Read-only, no PII.

- **DIRECTORY:** `GET /ClientPortal2/Geo/GetClubList` (CpAuthToken cookie) →
  `200` JSON array (79 clubs as of 2026-07):
  ```json
  [
    {"Id":25,"Name":"Modbury","FullName":"Modbury","Address":"976 North East Road",
     "City":{"Id":"66731","Name":"Modbury","Country":"AU"},
     "ClubNumber":"404","Latitude":-34.829,"Longitude":138.692,
     "OpeningDate":"2022-11-01T00:00:00","OpeningDateLocal":"2022-11-01T00:00:00",
     "BrandingId":null,"StateId":3},
    …
  ]
  ```
  - Fields kept: `Id`, `Name`, `FullName`, `Address`, `City` (nested `{Name}` **or**
    a bare string — both accepted), `ClubNumber`, `Latitude`/`Longitude` (float,
    `null` preserved as `None`), `OpeningDate`.
  - ⚠️ **These coordinates are rounded — don't use them for anything precise.**
    15 clubs sit at ≤3 decimal places (≥110 m) and two at 2 (~1.1 km). Measured
    against Netpulse's, **25 of 77 pins are >200 m out and Pitt St is 987 m out**;
    worse, the two Nunawading sites are given *identical* coordinates here even
    though they are 138 m apart. **Netpulse's directory (§7.2) carries 6–14
    decimal places** for almost every club — use it for map pins and for any
    distance/geofence work, and keep these only as the fallback.
    `_format_revo_club_detail` already prefers the Netpulse coordinate.
  - `FullName` differs from `Name` for exactly one club today (`Nunawading OG` →
    `Nunawading - (Original)`); callers fall back to `name`. `OpeningDateLocal` is
    identical to `OpeningDate` for all 79 clubs, and `BrandingId` is `null` for all
    79 — neither is worth reading.
  - ✅ **Correction (2026-07): `StateId` is NOT unmappable, and treating it as such
    was silently breaking state scoping.** The mapping is
    **`1=NSW, 3=SA, 5=VIC, 6=WA`** — derived by cross-referencing all 79 directory
    clubs against the curated name table: **zero conflicts** (NSW 5/5, SA 16/16,
    VIC 19/19, WA 35/35), and every club the curated table *couldn't* place lands on
    the id its suburb implies. Only those four ids appear (Revo has no QLD/TAS/NT/ACT
    clubs). Now `revo_perfectgym.STATE_BY_STATE_ID` is the **primary** state source,
    with `revo_client.state_for_club(name)` as the fallback.
    - **Why it mattered:** the curated name table only grows when a human edits it,
      so it had gone stale — it failed to place 4 live clubs (**Melrose Park**, SA,
      *already open since 2026-07-21 with 12 people in it*; **Nunawading OG**, VIC;
      **Altona North**, VIC; **Nedlands**, WA) and still lists 12 names absent from
      the live directory. A club with `state=None` is filtered out of **every**
      state-scoped surface — `/busy`'s board, its quietest line, and `/revo_clubs`'
      home-state list — so Melrose Park was simply invisible.
    - The mapping is a **whitelist**: an unconfirmed id yields `None` and falls back
      to the curated name, so an interstate expansion degrades to the old behaviour
      rather than leaking a raw integer as a state code.
  - ⚠️ **The directory lists clubs BEFORE they open** (`OpeningDate` in the future),
    **and those clubs are also on the live occupancy board reporting `0`.** Left
    alone they win "quietest right now" outright: before this was handled, `/busy`
    nationwide reported **Altona North — 0 in club** as the quietest Revo gym, a
    club that doesn't open until 2026-08-18. Note the two bugs *masked* each other —
    Altona North was excluded from VIC's board only because its state was unknown, so
    fixing `StateId` alone would have moved the bad row from the national board onto
    VIC's. Handled by `revo_perfectgym.has_opened()` / `enrich_occupancy()`;
    `future_openings()` exposes the coming-soon list deliberately instead.
- **Caching:** the directory changes on the order of *months*, so it gets its own
  cache with a **6h TTL** (`CLUB_DIR_TTL_SECONDS`), separate from the 60s
  occupancy cache so a directory fetch never evicts (or is evicted by) the
  fast-moving counts.
- **Secrets:** none — this endpoint returns no member data. `parse_club_list()`
  is still the scrubbing boundary (whitelisted public fields only).

Exposed as `PerfectGymClient.get_club_list() -> list[ClubDirEntry(id, name,
address, city, club_number, lat, lng, opening_date, state, full_name)]`, with:
- `haversine_km(a,b)` — pure great-circle distance (km).
- `nearest_clubs(entries, origin_name, limit)` — clubs sorted by distance from a
  named origin (origin + uncoordinated clubs excluded).
- `join_occupancy_to_dir(occupancy, directory)` — attaches directory
  `id`/`lat`/`lng`/`opening_date` to each occupancy row by name (unmatched rows
  kept with `None` geo).
- `has_opened(entry, today=None)` — is this club trading yet? A missing or
  unparseable `OpeningDate` counts as **open** (the directory's placeholder dates
  are all in the past, and an undateable club is far likelier to be trading).
- `future_openings(directory, today=None, state=None)` — not-yet-open clubs,
  soonest first, optionally one state.
- `enrich_occupancy(occupancy, directory, today=None)` — the join that makes the
  live board correct: fills in the `state` the occupancy payload omits (it carries
  no `StateId`, only a name and an address) and **drops not-yet-open clubs**.
  Returns a new list, so it is safe to call on the cached tuples. A row with no
  directory match is kept as-is — a live count is never discarded just because the
  directory lags — and an empty directory makes it a no-op, so a directory outage
  degrades to the old behaviour instead of failing the command.
- module-level `shared_club_list()` / `club_list_with_client(client)` (6h TTL).

`bot._perfectgym_occupancy()` applies `enrich_occupancy` to every board it hands
out, so `/busy` and `/revo_clubs` both get the corrected data with no per-command
changes. The directory side is 6h-cached, so this costs nothing in the steady state.
`/revo_clubs` renders a not-yet-open club as `opens 18 Aug 2026` rather than a
head-count — the info is genuinely useful, it just must not read as a live number.

### 9.1 `/revo_clubs`

- **No arg:** lists every club in the caller's **home state** (identity via the
  rewards-landing fav club → `state_for_club`, same as `/busy`), each rendered
  `Name — Suburb (X in club now)` by joining the live board. Degrades to
  "count unavailable" per club when the board is down; if the home state can't be
  determined it asks the user to name a club or link.
- **Club arg:** that club's **address** (+ postcode), a **Google-Maps link** from
  lat/lng, its **state**, its **opening hours / staffed status right now**, its
  **live count**, a **phone number**, and the **nearest 3 other clubs** (with
  distance + each one's live count). Public data → shared account is fine; still
  gated by `REVO_DISABLED` + `available()`.
  - Hours/phone/postcode come from the **credential-free Netpulse directory**
    (§7.2), joined by normalised name (§7.2.1) via `bot._club_hours_entry()`.
    Entirely best-effort: a Netpulse outage, a missing dep, or a club it doesn't
    list yet just omits those lines.
  - A club that **hasn't opened yet** shows `🚧 Not open yet — opens 18 Aug 2026`
    and stops there, rather than reporting the `0` the live board carries for it.
- This supersedes the deferred Netpulse `/revo_clubs` idea — there is a **single**
  clubs command, backed by PerfectGym `Geo/GetClubList`.

### 9.2 Classes — 3 clubs run them, and the timetable is NOT reachable

`GET /ClientPortal2/Clubs/GetAvailableClassesClubs` returns the same club shape as
the directory, filtered to the clubs that actually run classes. Today that is
**3 of 79: Cranbourne, Glenelg, Pitt St.** (Revo's studio/pilates bookings run on
**Arbox**, a fourth backend — see §7.)

A `/revo_classes` command **cannot** be built today — not because the routes are
missing, but because **Revo publishes no timetables through PerfectGym**. The real
timetable routes were found (via §9.3) and all answer `200`:

| Route | Params | Result |
|---|---|---|
| `Classes/ClassCalendar/DailyClasses` | `clubId`, `date=YYYY-MM-DD` | `{CalendarData:[{Hour,Classes:[…]}], PagerData:{…}}` — `CalendarData` **`[]` every time** |
| `Classes/ClassCalendar/WeeklyClasses` | `clubId`, `date` (anchors 7 days) | zone → hour → day grid; `CalendarData` **`[]` every time** |
| `Classes/ClassCalendar/GetCalendarFilters` | `clubId` | 5 arrays, **all empty** |

Coverage behind that verdict: all 3 class-running clubs × every 7-day page across
the whole bookable horizon, plus 8 spot-check clubs, past/future dates, and the
SPA's full parameter set — `CalendarData` was `[]` in 100% of 40 calls.

Two traps worth recording, both of which make a naive probe *look* successful:
- `DailyClasses` **silently resets an unparseable or out-of-range `date` to today**
  and returns a normal empty envelope — no `400`, no error. A bogus `clubId`
  returns `200` with a bare `null` body.
- `GetCalendarFilters` returns a **byte-identical 110-byte constant** for all 33
  club ids tried, *and* with no `clubId` at all, *and* with no session cookie. It
  therefore carries **zero per-club information** — do not read its emptiness as
  evidence about any particular club.

The other two class routes — `Classes/ClassCalendar/Details` (`classId`) and
`GetClassTickets` (`classId`,`userId`) — need a `classId` only a timetable listing
could supply, so they're unreachable in practice. `MyCalendar` covers *your own
bookings*, not what's on (§12.1). The routes are ready if Revo ever loads
timetables; until then there is nothing to render.

### 9.3 How to find new routes: mine the Angular templates, not the JS bundles

The most productive discovery channel on ClientPortal2, and the one that found
`ContractList`, `DailyClasses`/`WeeklyClasses`, `GetProfileForEdit` and
`GetFamilyMembersForEdit` — **none of which appear in the JS bundles at all**.

The SPA declares many of its API calls in **HTML templates**, not JavaScript. In
the minified bundle `$View` is literally `function $View(n,t){return t}`, so every
`templateUrl` string is a **fetchable path**:

```
GET /ClientPortal2/<Area>/Views/<Name>        → 200 text/html (raw Angular partial)
GET /ClientPortal2/<Area>/Components/<X>/<Y>  → 200 text/html
```

Each partial carries the two things a probe needs:

```html
<baf:data-source url="Classes/ClassCalendar/DailyClasses" params="{…}" name="…">
<baf:model name="params">{ clubId, date, categoryId, trainerId, zoneId }</baf:model>
```

i.e. **the real route and its exact parameter names**. Controls confirm the signal
is real: 4/4 invented template paths behave differently from real ones.

Recommended order for any future spelunking:
1. Pull the bundles (`loadJs("bundles/…")` in the SPA shell) → module/route list.
2. **Fetch every `<Area>/Views/<Name>` template** and grep for `baf:data-source`.
3. Only then probe, using the §8.1 status taxonomy to tell real routes apart.

Roughly **30 listed templates are still unmined** — one cheap unauthenticated-ish
GET each, and the single highest-yield thing to do next.

## 10. Membership status slice — `User.Member.NotificationsData`

The login response body (the member profile, §8) contains a
`NotificationsData` object with the contract-health flags the portal dashboard
shows. We project a **narrow, non-sensitive** slice of it — **no** UserNumber,
email, photo, ids, or token:

```json
{"User":{"Member":{"NotificationsData":{
  "ContractStatus":"Current",
  "HasInvalidContractPaymentMethod":false,
  "HasMemberCardAssigned":true,
  "RemainingDeposit":0.0
}}}}
```

- `parse_membership_status(profile) -> MembershipStatus(contract_status,
  payment_ok, has_card)`. **`payment_ok` inverts** `HasInvalidContractPaymentMethod`
  (the portal shows the positive), and a **missing** flag stays `None` — "unknown",
  never a silent "ok". A missing/garbage profile yields all-`None`, never raises.
- Stashed off the login body at auth time; served by
  `PerfectGymClient.get_membership_status()`. Safe to log/embed (no identity).
- **`/revo_summary`** now adds a best-effort line
  `💳 Membership: {contract_status}` (`, payment issue ⚠` when `payment_ok is
  False`) from the **target's own** linked client, wrapped like the Netpulse
  membership line — a PerfectGym outage or missing dep degrades silently and never
  sinks the rest of the summary.

## 11. `/revo_card` — the member's entry barcode (SENSITIVE)

`User.Member.UserNumber` is the **physical entry BARCODE** — an *access
credential*, not just an id (possessing it is enough to walk through a Revo
turnstile). `/revo_card` is the one feature that surfaces it, under hard rules:

- **`get_card_number()` is the SOLE public exposure.** `UserNumber` is stashed on
  a private `client._user_number`, appears in no dataclass/repr, and is **never
  logged** (the login log line is email + home_club_id only). The
  `test_get_card_number_is_the_only_public_barcode_exposure` test dynamically
  calls every no-arg public method and asserts the barcode leaks from
  `get_card_number()` and nowhere else.
- **EPHEMERAL on every path** — `interaction.response`/`followup` are all
  `ephemeral=True`, so no one but the caller ever sees the barcode.
- **OWN creds ONLY.** Credential resolution is
  `bot._revo_card_client_for_user(user_id)`, which reads **only**
  `db.get_revo_account(user_id)` and **NEVER** falls back to the shared
  `REVO_USER` account the way `/busy` does — falling back would hand one member
  the *host's* door barcode. Unlinked → refused with "Link your own account first
  with `/revo_link`". This is asserted by
  `test_revo_card_client_never_uses_shared_account` +
  `test_revo_card_client_refuses_unlinked_user`.
- **Symbology: Code128** (the default when the exact symbology is undetermined).
  Rendered to a PNG by a **lazily-imported** `python-barcode` (+ `pillow`) inside
  the command; if the lib is missing the bot still boots and the command
  **degrades to showing the number as text**. The reply carries the caveat
  *"if it doesn't scan, use the Revo app."* `bot._render_card_barcode()` never
  logs the number, even on a render failure.
- The contract status (§10) is shown alongside the barcode.

## 12. Member visit history — CONFIRMED UNREACHABLE (do not re-probe)

Per-visit / per-club / per-timestamp check-in history is **not** available to a
member client on **any** Revo backend:
- **Web portal (`revocentral`):** every check-in/visit/history route 302s even at
  L2 — mobile-app-only (§1.1, §2, §5-H).
- **Netpulse (EGYM):** `check-ins/history` **404s** (it does *not* return
  `{"checkIns": []}` — that older claim is corrected in §7.1) and
  occupancy isn't provisioned for Revo's tenant — Revo runs on PerfectGym (§7).
- **PerfectGym ClientPortal2:** exposes live all-clubs occupancy (§8), the public
  club directory (§9) and the login-profile membership slice (§10). It does **not**
  expose a member-facing visit-history endpoint — that data lives behind the
  **operator API** (staff/kiosk auth), which this project deliberately does not
  touch. The per-day **streaks calendar** (§3.2.1) remains the finest check-in
  signal available, and the attendance poller drives off it. **Do not re-probe for
  a member visit-history endpoint — it is operator-API-only.**

### 12.1 Re-confirmed 2026-07, with a validated oracle

This conclusion has been re-tested rather than inherited, because §1.2 was wrong
once before. A 35-name sweep (`Visits`, `Entrances`, `Entries`, `Attendance`,
`History`, `Access`, `Turnstile`, `ClubVisits`, `MemberVisits`, `Statistics`,
`Activity` × several `Area/Controller/Action` spellings) returned the 17 KB
bogus-route 404 page for **every** name. The sweep is trustworthy because the same
run contained a positive control: `MyCalendar/MyCalendar/Get*Activities` returned
`400` on a bare GET and `200` on a POST, proving 400-vs-404 discriminates real
routes from absent ones.

**There is also no richer mobile API.** The Revo iOS app rides this same
ClientPortal2 cookie surface (plus Netpulse), not a versioned REST API:
`/api/v1/`, `/api/v2/`, `/ClientPortal2/api/`, `/swagger`, `/swagger/v1/swagger.json`
and `/openapi.json` are all 404, and no `Authorization: Bearer` flow exists. The
operator app *is* present but gated — `/MobileApp/`, `/GoFit/`, `/pgapi/` all
302 to the `/Pgm/` staff portal, which a member `CpAuthToken` does not unlock.

> ⚠️ **`MyCalendar/MyCalendar/GetPastActivities` is NOT a visit log** — the name
> invites exactly that mistake. It, plus `GetRecentActivities` /
> `GetFutureActivities`, are the paginated feeds behind `GetCalendar`'s
> `PastItems`/`RecentItems`/`FutureItems`: **class / PT / facility BOOKINGS**.
> POST with `{"page":N}`, shape `{Page, Items, HasMore}`. All three return
> `Items: []` for a member who doesn't book classes, which is most Revo members —
> Revo runs classes at only 3 of 79 clubs (§9.2).

See **§14** for the two member reads that *were* confirmed new — neither is a visit
log, and neither is implemented.

## 13. Profile first name + photo — `/seeprofile` and auto-nicknames

The login profile (`User.Member`) also carries the member's `FirstName` (a
non-secret display name) and `PhotoUrl`. Both are read off the stashed login body
by two accessors on `PerfectGymClient`:

- **`get_first_name()`** → the non-secret first name. Safe to display/log.
- **`get_photo_url(refresh=False)`** → the `PhotoUrl`. This is a **signed, short-
  lived CDN capability URL** (`https://pgaustoragev2.perfectgymcdn.com/…&sig=…`,
  valid ~10 min): whoever holds it can fetch the photo with no auth, so it is
  stashed on a private `client._photo_url`, appears in no dataclass/repr, and is
  **NEVER logged** (the login log line stays email + home_club_id only). Because
  the signature expires, pass `refresh=True` to force a fresh login (re-signing the
  URL) before reading. `download_photo(url)` fetches the bytes with an
  unauthenticated GET (the signature *is* the credential) — its callers download
  immediately and catch errors **without** logging the URL-bearing message.

**`/seeprofile`** (owner-approved) renders a roster of *every* linked member's Revo
photo + first name — one embed per member (image = an attached in-memory file,
title = the first name, falling back to the guild display name). It enumerates
`db.list_revo_accounts()` and, for **each** member, builds **that member's OWN**
per-user client (`_perfectgym_client_for_user` — never the shared account, never
one member's client for another's photo), refreshes for a valid signed URL, and
**downloads the bytes immediately** to attach them (the raw signed URL never lands
in message history). The fan-out (N logins + downloads) runs in an executor;
per-member failures are skipped with a trailing "(couldn't load N members)" note.

**Auto-nicknames.** Bot-wide nicknames (`db.set_user_nickname`, surfaced via
`_bot_name` and used for chat targeting by `_resolve_nickname_target`) now
**auto-populate from the PerfectGym first name on `/revo_link`**: after a
successful link the bot best-effort fetches `get_first_name()` via the member's new
per-user client and overwrites their nickname (owner-approved; non-fatal if the
fetch fails). The old manual `/set_nick` + `/remove_nick` commands were **retired**
— nicknames are no longer set by hand.

## 14. Confirmed-new member reads — documented, deliberately NOT implemented

Two endpoints returned rich, real, non-duplicated member data. Both were
reproduced independently against the live backend. **Neither is wired up**, and
that is a decision rather than an omission: this bot posts into shared Discord
channels, and both carry data classes nothing in the repo currently handles.

They are written up in enough detail to build from directly if wanted.

### 14.1 `GET /Profile/Contracts/ContractList?userId=<own Member.Id>` — the real contract

Today the bot's *entire* membership surface is one scraped flag —
`MembershipStatus.contract_status` off the login body (§10), rendered as
`💳 Membership: Current`. This is the actual contract behind it.

- `userId` is **required** (400 without it). CpAuthToken cookie. ~2 KB.
- Shape: `{Contracts:[…], Addons:[…], CanAddContract, CanAddAdditionalContract}`.
  Each row: `Name`, `Club{Id,Name,…}`, `CommitmentPeriod`, `SignupDate`,
  `StartDate`, `EndDate`, `CommitmentDate`, `CurrentFreezeEndDate`,
  **`NextPaymentDate`**, `Cost{Gross,Net,Tax}`, `PaymentInterval`, `PaymentPlanId`,
  `IsAdditional`, `CanBeEarlyTerminated`, `CollectedDeposit`/`RemainingDeposit`.
- **`Addons` are separate contracts**, each with its own id, cost and next-payment
  date — not a name list on the main contract. Derive `is_addon` from which array
  a row came from, not only from `IsAdditional`.
- `ShortDescription`/`FullDescription` come back **empty** — `Name` is the only
  reliable plan label. `CurrentFreezeEndDate` is in-schema but was never observed
  populated (the test account isn't frozen).
- Genuinely new capability it would unlock: plan + price + billing interval, and
  **`NextPaymentDate` / `CurrentFreezeEndDate` are pollable** — "your membership
  bills in 3 days", "your freeze ends tomorrow". Nothing in the bot can do that.

> 🔒 **If this is ever built, these are not optional.**
> 1. **Own credentials only** — the `/revo_card` rule (§11). Falling back to the
>    shared `REVO_USER` account would show every caller the *host's* plan and price.
> 2. **`ephemeral=True` on every path.** This is personal financial data.
> 3. **`userId` is never user-supplied** — always the caller's own `Member.Id` from
>    their own login body. Don't put a `userId` parameter on any function a command
>    can reach; whether a foreign id is honoured server-side was deliberately not
>    tested.
> 4. **No module-level cache.** `shared_club_occupancy`/`shared_club_list` are
>    cached because they're the same public list for everyone. This is per-member —
>    a shared cache is a cross-member leak.
> 5. Project a narrow whitelist at the parser, as `parse_membership_status` does;
>    never log the body.

### 14.2 `GET /Profile/Profile/GetProfileForEdit?userId=<own Member.Id>` — full profile

Returns `Model.PersonalData` with `BirthDate` + `IsBirthDateMasked`, `ReferralCode`,
`Sex`, full `Address`+`PostalCode`, `Email`, `Phone`, `Photo{Url,TempToken}` and
**`UserNumber` — the door-access barcode (§11)**. Note it is *not* a superset of the
login profile: it adds those fields but **drops** `HomeClubId`/`DefaultClubId`/
`NotificationsData`.

The only genuinely new facts are `BirthDate` and `ReferralCode`.

> 🔒 **The most PII-dense body on the backend.** One GET returns an entry barcode,
> an unmasked date of birth, an email, a phone number, a street address and a live
> signed photo URL. If a birthday feature is ever wanted: whitelist to **`(month,
> day)` only — never the year**, return `None` when `IsBirthDateMasked`, store MM-DD
> so a DB leak reveals nothing about age, fetch once at `/revo_link` time (never
> from the daily loop), and treat announcing it as opt-in — a member did not consent
> to a public birthday post just because the bot could read it off a billing page.

**Do not** use this as a photo-URL refresh shortcut for `/seeprofile`. The
signature does rotate per GET, but `get_photo_url(refresh=True)` already works, and
this trades one login for pulling a far more sensitive body.

### 14.3 Also confirmed: `Profile/Profile/GetFamilyMembersForEdit?userId=<id>`

The friends-and-family list that `Family/Family/GetFriendsAndFamily` 403s on, under
a different name: `[{UserId, UserNumber, FirstName, LastName, PhotoUrl, Relation,
BindedDate, EndDate}]`. `Relation` is `FamilyChild | FriendAccess`. Carries
`UserNumber` (barcode) and a signed `PhotoUrl` **for other people**, so the same
rules as §11 apply per row. The test account has no relatives, so only the
self-row returns — the populated shape is unverified.
