# Home Assistant integration

Point the bot at your [Home Assistant](https://www.home-assistant.io/) and it
reads members' smart-scale weigh-ins straight out of it. A new reading is
**logged as that member's bodyweight** — so it feeds TDEE, bodyweight-linked
protein targets, `/bodyweight_goal`, `/bodyweight_graph` and the leaderboard's
true-load lines exactly as a typed `bw 106.3` would — and **announced in the
channel**. Everything else the scale measures (body fat %, muscle mass, BMI, BMR,
water %, bone mass, …) is kept alongside it and shown by `/ha_body`.

```
member stands on the scale
        │
        ▼  (scale → Bluetooth/Wi-Fi → Home Assistant)
  sensor.joshua_s_weight = 106.30 kg
        │
        ▼  (bot polls every HA_POLL_MINUTES)
  GET <HA_BASE_URL>/api/states        (one call, all members)
        │
        ├─▶ db.set_bodyweight()   — dedup on the scale's measurement id
        ├─▶ body_metrics          — the other nine numbers
        └─▶ embed to the bodyweight-reminder channel
```

The difference from Strava and Hevy is worth being explicit about: those store a
**credential per member**. Home Assistant is **one server with one credential**,
and every member's sensors live on it. So the URL and token are operator
settings, and what a member links is their slice of the entity namespace.

The bot is **read-only** — it only ever issues `GET /api/states`,
`GET /api/states/<entity>`, `GET /api/history/period/…`, `GET /api/config` and
`GET /api/`. It never calls a service, so it cannot change anything in your home.

---

## Requirements

- Home Assistant, reachable from wherever the bot runs.
- A **long-lived access token**: Home Assistant → click your name (bottom left)
  → **Security** → *Long-lived access tokens* → **Create token**.
- A scale integration that exposes body-composition sensors. Anything works whose
  entity ids follow HA's normal shape — Renpho/Xiaomi/Withings BLE integrations,
  ESPHome, or a template sensor you wrote yourself.
- `requests` (already bundled for the Strava/Revo/Hevy features).

## Configuration

Dashboard → **Settings → Home Assistant**:

| Setting | Default | Meaning |
| --- | --- | --- |
| Disable Home Assistant entirely | off | Turns the integration off. |
| Home Assistant URL | — | e.g. `http://192.168.1.50:8123`. A trailing `/` or `/api` is trimmed for you. |
| Long-lived access token | — | Stored encrypted. Fully masked in the dashboard. |
| Poll interval (minutes) | `10` | How often to check for new weigh-ins (minimum 1). |
| Import weigh-ins from the last (days) | `14` | How far back past weigh-ins are imported. `0` means only the current reading. It's a rolling window, not a one-off: a weigh-in that predates it is never imported, so raise it before linking if you want more. |
| Ignore entities containing | — | Comma-separated fragments; any body sensor whose entity id contains one is ignored entirely. See below. |
| Verify the TLS certificate | on | Turn off **only** for an `https://` Home Assistant with a self-signed certificate. |

Changing any of these stages a bot restart; press **Apply & restart bot**.

The integration stays off until both a URL and a token are set — an unconfigured
deployment never starts the poll and the `/ha_*` commands say so.

> These also have `HA_*` environment-variable equivalents, which take priority if
> set. See `.env.example`.

### `homeassistant.local` and Docker

If the bot runs in Docker, **use the LAN IP, not `homeassistant.local`**.
`.local` is mDNS; a container's resolver doesn't speak it without avahi and host
multicast, so the address that works perfectly in your browser fails here. The
bot detects this specific failure and says so rather than reporting a bare DNS
error. The alternatives, in order of least trouble:

1. `HA_BASE_URL=http://192.168.1.50:8123` — just use the IP.
2. `extra_hosts: ["homeassistant.local:192.168.1.50"]` in `docker-compose.yml`.
3. `network_mode: host` (Linux only).

### Where announcements go

Weigh-ins post to **`BODYWEIGHT_REMINDER_CHANNEL_ID`** — the channel the bot
already uses for the weekly "drop your current weight" nudge. Deliberately not a
new setting: the reminder and the answer to it belong in the same place. Leave it
blank and weigh-ins are still recorded, just not announced.

Nobody is @-mentioned. Members who don't want their numbers posted run
`/ha_alerts enabled:false`, which keeps syncing but stops the announcing.

## Member usage

- `/ha_entities` — list the body sensors the bot can see, grouped by person. It
  shows **no weights except your own**: a Home Assistant server is a household, so
  it often carries sensors for people who aren't in the Discord at all and have no
  `/ha_alerts` opt-out to reach for. What it does show is when each one last read,
  which is how you identify yours — you just stood on it.
- `/ha_link entity:joshua_s` — claim yours. A full entity id
  (`sensor.joshua_s_weight`) or a friendly name (`Joshua`) works too.
- `/ha_body [member]` — the latest body-composition numbers on file.
- `/ha_sync` — check for a new weigh-in right now.
- `/ha_status` — your link, and when it was last checked.
- `/ha_alerts enabled:<true|false>` — announce your weigh-ins, or keep them quiet.
- `/ha_unlink` — stop syncing. Your recorded weight history is kept, and so is
  the record of which weigh-ins were already imported, so re-linking later picks
  up where it left off instead of importing everything a second time.
- `/ha_help` — the in-Discord version of this page.

Admins can pass `member:` to `/ha_link` and `/ha_unlink` to act on someone else's
behalf; everyone else can only link themselves.

A set of sensors can only be claimed by **one** member. Linking a prefix somebody
else already owns is refused — otherwise a member could claim another person's
scale and have their weigh-ins imported and announced under the wrong name, which
would also defeat that person's `/ha_alerts` opt-out. An admin can reassign a
prefix (the previous owner's link is removed, since two links to one scale would
import every weigh-in twice), which is how a genuine mix-up gets fixed.

## How linking works

A body-composition scale in Home Assistant produces one sensor per metric, all
sharing a per-person prefix, because HA's slugifier turns *"Joshua's Weight"*
into `sensor.joshua_s_weight`:

```
sensor.joshua_s_weight                       106.30 kg
sensor.joshua_s_body_fat_percentage            35.2 %
sensor.joshua_s_muscle_mass                   65.48 kg
sensor.joshua_s_basal_metabolic_rate           1838 kcal
...
```

Linking stores the **prefix** (`joshua_s`), not the ten entity ids. So a metric
your scale starts reporting later is picked up on the next poll with no re-link
and no migration — and adding support for a metric the bot has never seen is one
entry in `METRICS` in `app/ha_client.py`.

The entity you linked from is stored too, as a fallback for the one case the
prefix scheme can't cover: a weight sensor renamed by hand so it no longer shares
a prefix with its siblings.

## What counts as a new weigh-in

Getting this wrong means either duplicate announcements or silently missed
weigh-ins, so it's worth being precise. There are two mechanisms, and the bot
prefers the first whenever it's available.

**1. The scale's own measurement id (preferred).** Some integrations publish their
full measurement log as an attribute on the weight entity — the Renpho BLE
integration uses `weight_history`:

```json
"weight_history": [
  {"measurement_id": "a1df05dc…", "timestamp": "2026-07-30T03:19:55+00:00",
   "weight": 106.3, "weight_unit": "kg", "body_fat": 35.2},
  …
]
```

When that's there, the bot reads it instead of the sensor's state, because it is
better on all three counts: the `measurement_id` is stable across Home Assistant
restarts, two genuine weigh-ins that land on the same kilogram are still two
weigh-ins, and the timestamp is when the *measurement* happened rather than when
HA noticed. It also means history needs no recorder at all — which matters more
than it sounds; see below.

**2. The weight sensor's `last_changed` (fallback).** For a scale that publishes
no such attribute, a weigh-in is identified by when the weight sensor last
changed value. Two consequences, both intended:

- Standing on the scale twice and reading *exactly* the same kg is one weigh-in.
  Home Assistant doesn't move `last_changed` when the value is unchanged, and the
  alternative announces the same number on every poll.
- Restarting **Home Assistant** re-creates every entity with a fresh
  `last_changed` while the value is unchanged, which would otherwise read as a new
  weigh-in for everybody. The last imported weight is stored per member, and an
  unchanged value is recognised as a restored state — so a routine HA update
  doesn't announce a round of duplicates.

Either way the poll is idempotent: a bot restart, a re-link, or spamming
`/ha_sync` all recompute the same keys and cannot double-log or re-announce.

**Switching between the two.** An integration update can start publishing a
measurement log for a scale that previously had none, which changes the key scheme
mid-life — the same physical weigh-in then has a different key and would import
again. So on the measurement-id path the bot skips anything at or before the newest
weigh-in it has already recorded. The trade-off is that a measurement back-dated
into the log after the fact isn't picked up.

## Behaviour notes
- **A sleeping scale is not a weigh-in.** Scales report `unavailable` most of the
  time and `unknown` before their first reading; neither is ever imported.
- **Units follow the entity.** `unit_of_measurement` is read per sensor on every
  poll and `lb`/`g`/`st` are converted to kg, because changing an entity's display
  unit in HA changes the reported value with no other signal. HA's global
  `unit_system` is deliberately ignored — it reports metric mass as `g`.
- **Implausible readings are dropped, not stored.** A weight above
  `MAX_WEIGHT_KG` (default 500) is logged and skipped; a BLE glitch reporting
  6553.5 kg would otherwise sit in the table forever poisoning every true-weight
  calculation.
- **First link is one message, not twenty.** The history import posts a single
  summary embed with the count, date range and latest weight, rather than an alert
  per historical weigh-in.
- **Scale entities are often excluded from the recorder.** On the install this was
  built against, `/api/history/period` returns *nothing* for the Renpho sensors
  while returning history happily for everything else. So if your scale doesn't
  publish a measurement-log attribute, backfill may find nothing at all and the
  bot simply starts from the next weigh-in — which is why the attribute path is
  preferred rather than merely nice. Where the recorder *is* the source,
  `purge_keep_days` defaults to **10 days**, so asking for more than that usually
  returns less. Long-term statistics keep a weight sensor's hourly means
  indefinitely, but only over the websocket API, which the bot doesn't use.
- **One person, two weight sensors.** An Apple Health or Google Fit bridge often
  creates `sensor.<name>_iphone_weight` and then never writes to it, so it sits at
  `unavailable` forever. Three things keep that from mattering: `/ha_link` ranks a
  sensor that *has* a current reading above one that merely matches your name more
  tightly (linking the dead one looks like success and then never syncs);
  `/ha_entities` collects everything nothing is writing to into one line at the
  bottom rather than giving it equal billing; and **Ignore entities containing**
  removes it altogether — set it to `_iphone` and those entities are not listed,
  not linkable and never polled. To link a silent group deliberately — a new scale
  nobody has stood on yet — give its exact prefix or entity id.
- **Weigh-ins are filed under the server you linked from** (or your `/server`
  default when linking via DM), matching Hevy and Strava. Bodyweight itself is
  global per member — one body, one timeline, whichever server it arrived in.
- **A renamed entity means re-linking.** The REST API exposes no entity registry,
  so a renamed sensor is indistinguishable from a deleted one. `/ha_status` shows
  what you're linked to and `/ha_entities` shows what exists.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| "Couldn't resolve homeassistant.local" | mDNS from Docker. Use the LAN IP — see above. |
| "Home Assistant rejected the access token" | The token was revoked, or pasted with a line break. Tokens are one long line; the dashboard rejects one containing whitespace. |
| "this machine's IP is in its ip_bans.yaml" | A 403. Home Assistant's `http.ban` middleware blocked the bot. Remove the entry from `ip_bans.yaml` and restart HA — retrying won't clear it. |
| `/ha_entities` finds nothing | Either the scale integration isn't set up, or nobody has stood on it yet — most scales create their entities only after the first reading. |
| No weigh-in after standing on the scale | Check the sensor actually changed value in HA (Developer tools → States). An unchanged value is not a new weigh-in. |
| Weigh-ins import but aren't announced | `BODYWEIGHT_REMINDER_CHANNEL_ID` is unset, or you ran `/ha_alerts enabled:false`. `/ha_status` says which. |
