# Home Assistant integration

Point the bot at your [Home Assistant](https://www.home-assistant.io/) and it
reads members' smart-scale weigh-ins straight out of it. A new reading is
**logged as that member's bodyweight** — so it feeds TDEE, bodyweight-linked
protein targets, `/bodyweight_goal`, `/bodyweight_graph` and the leaderboard's
true-load lines exactly as a typed `bw 106.3` would — and **announced in the
channel**. Everything else the scale measures (body fat %, muscle mass, BMI, BMR,
water %, bone mass, …) is kept alongside it. `/ha_body` shows the newest
coherent measurement and `/ha_graph` plots each metric over time.

```
member stands on the scale
        │
        ▼  (scale → Bluetooth/Wi-Fi → Home Assistant)
  sensor.joshua_s_weight = 106.30 kg
        │
        ▼  (bot polls every HA_POLL_MINUTES)
  GET <their own HA>/api/states       (one call per connected member)
        │
        ├─▶ db.set_bodyweight()   — dedup on the scale's measurement id
        ├─▶ body_metrics          — the other nine numbers
        └─▶ embed + refreshed graph to the bodyweight-reminder channel
```

Every member connects **their own** Home Assistant, the same way Hevy and Strava
work: they run `/setup_ha` with an address and a long-lived access token, and that
token is stored **encrypted per user**. So people on different servers can all use
the feature, and no operator ever holds somebody else's house key. What they link
on top of that is their slice of that server's entity namespace.

The bot is **read-only** — it only ever issues `GET /api/states`,
`GET /api/states/<entity>`, `GET /api/history/period/…`, `GET /api/config` and
`GET /api/`. It never calls a service, so it cannot change anything in your home.

---

## Requirements

- A Home Assistant reachable from wherever the bot runs — a LAN address if the
  bot is on the same network, or a public hostname if not.
- A **long-lived access token**: Home Assistant → click your name (bottom left)
  → **Security** → *Long-lived access tokens* → **Create token**. It is shown
  once.
- A scale integration that exposes body-composition sensors. Anything works whose
  entity ids follow HA's normal shape — Renpho/Xiaomi/Withings BLE integrations,
  ESPHome, or a template sensor you wrote yourself.
- `requests` and `cryptography` (already bundled for the Strava/Revo/Hevy
  features).

## Configuration

Dashboard → **Settings → Home Assistant**:

There is deliberately **no URL or token here** — those are per member, set with
`/setup_ha`. What the dashboard owns is the global tuning:

| Setting | Default | Meaning |
| --- | --- | --- |
| Disable Home Assistant entirely | off | Turns the integration off. |
| Poll interval (minutes) | `10` | How often to check for new weigh-ins (minimum 1). One request per connected member per cycle. |
| Import weigh-ins from the last (days) | `14` | How far back past weigh-ins are imported. `0` means only the current reading. It's a rolling window, not a one-off: a weigh-in that predates it is never imported, so raise it before linking if you want more. |
| Ignore entities containing | — | Comma-separated fragments; any body sensor whose entity id contains one is ignored entirely. See below. |
| Verify the TLS certificate | on | Turn off **only** for an `https://` Home Assistant with a self-signed certificate. |

Changing any of these stages a bot restart; press **Apply & restart bot**.

Nothing else needs configuring — members connect themselves. Who is connected
shows on each member's page in the dashboard as a 🏠 chip, with the host they
connected to; their token is encrypted and is never displayed.

> These also have `HA_*` environment-variable equivalents, which take priority if
> set. See `.env.example`.

### What address to give `/setup_ha`

If the bot runs in Docker, **don't use `homeassistant.local`**. `.local` is mDNS;
a container's resolver doesn't speak it without avahi and host multicast, so the
address that works perfectly in your browser fails for the bot. It detects this
specific failure and says so rather than reporting a bare DNS error. Use instead:

1. The LAN IP — `http://192.168.1.50:8123` — if the bot is on the same network.
2. A public hostname — `https://home.example.com` — if it isn't. This is the only
   option that works for a member whose Home Assistant is somewhere else
   entirely, which is the normal case once more than one person uses the feature.
3. `extra_hosts: ["homeassistant.local:192.168.1.50"]` in `docker-compose.yml`,
   or `network_mode: host` on Linux, if you would rather keep the name.

### Where announcements go

Weigh-ins post to **`BODYWEIGHT_REMINDER_CHANNEL_ID`** — the channel the bot
already uses for the weekly "drop your current weight" nudge. Deliberately not a
new setting: the reminder and the answer to it belong in the same place. Leave it
blank and weigh-ins are still recorded, just not announced.

Nobody is @-mentioned. Announcements are always on — link only sensors you're
fine broadcasting.

Each announcement batch includes the member's refreshed global bodyweight timeline—the
same history used by `/bodyweight_graph`, including manual and HA readings across
shared servers and DMs. A poll that imports several routine readings attaches one
cumulative graph to its newest announcement; a first-link backfill attaches one
graph to the batch summary. This is a public snapshot in the configured channel.
If the bot lacks permission to attach files, it retries the announcement without
the graph.

## Member usage

- `/setup_ha url:<address> token:<token>` — connect your Home Assistant. Best run
  in a **DM with the bot** so the token isn't typed into a channel; the reply is
  ephemeral either way. If your scale is the only thing on there with body
  sensors, this links it for you and you're done.
- `/ha_entities` — list the body sensors the bot can see, grouped by person. It
  shows **no weights except your own**: a Home Assistant server is a household, so
  it often carries sensors for people who aren't in the Discord at all and have no
  say in whether their weight shows up here. What it does show is when each one
  last read, which is how you identify yours — you just stood on it.
- `/ha_link entity:joshua_s` — claim yours. A full entity id
  (`sensor.joshua_s_weight`) or a friendly name (`Joshua`) works too.
- `/ha_body [member]` — the latest body-composition numbers on file.
- `/ha_graph metric:<measurement> [member]` — a private PNG trend chart for one
  scale metric (body fat, muscle mass, water, BMI, BMR, and so on). At least two
  different measurement days are required.
- `/ha_status` — your link, and when it was last checked.
- `/ha_unlink` — disconnect. Your stored access token is **deleted**. Your
  recorded weight history is kept, and so is the record of which weigh-ins were
  already imported, so reconnecting later picks up where it left off instead of
  importing everything a second time.
- `/ha_help` — the in-Discord version of this page.

Admins can pass `member:` to `/ha_link` and `/ha_unlink` to act on someone else's
behalf; everyone else can only link themselves.

A set of sensors can only be claimed by **one** member. Linking a prefix somebody
else already owns is refused — otherwise a member could claim another person's
scale and have their weigh-ins imported and announced under the wrong name. An
admin can reassign a prefix (the previous owner's link is removed, since two
links to one scale would import every weigh-in twice), which is how a genuine
mix-up gets fixed.

## Body-composition trends

`/ha_body` deliberately uses one real scale measurement: its weight and only the
composition values recorded at that exact time. It never combines today's
hand-entered weight with last week's BMI, or a current body-fat value with an old
muscle reading and presents them as one snapshot. Where a metric has history,
the card also shows its neutral net change.

`/ha_graph` collapses multiple readings on one local day to their mean, keeps the
raw daily points visible, and overlays a trailing three-day trend. The
response is ephemeral because body composition is more sensitive than a public
lift chart. The member page in the operator dashboard shows the same histories
as compact neutral sparklines. When a member runs `/coach` for themselves it
receives a bounded, explicitly caveated summary and keeps that report private;
composition is omitted when somebody requests a report for another member.

Consumer bioimpedance scales estimate composition from electrical impedance.
Hydration, a recent meal, exercise, skin temperature, time of day and foot
contact can all move an individual result. These views are for consistent,
multi-reading direction—not diagnosis—and the bot never calls an increase or
decrease inherently good.

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

Either way the poll is idempotent: a bot restart or a re-link both recompute
the same keys and cannot double-log or re-announce.

**Switching between the two.** An integration update can start publishing a
measurement log for a scale that previously had none, which changes the key scheme
mid-life — the same physical weigh-in then has a different key and would import
again. So on the measurement-id path the bot skips anything at or before the newest
weigh-in it has already recorded. The trade-off is that a measurement back-dated
into the log after the fact isn't picked up.

## Undoing a weigh-in

A scale logs things you don't want kept: a test step-on, a half-finished
measurement, or one it assigned to the wrong person's profile. A stray weight is
not cosmetic — it moves TDEE, the bodyweight-linked protein target and every
true-load line on the leaderboard — so there are two ways to remove one.

**React ❌ on the announcement.** The member it belongs to, or an admin, can react
with ❌ and that weigh-in is removed along with the body-composition numbers
measured with it. The message is rewritten to say what happened. This works on
announcements posted *before* this feature existed too: the bot identifies the
weigh-in from the weight and timestamp in the embed. On a first-link summary
("Imported 4 past weigh-ins") the ❌ undoes **the whole batch**, which is the case
you want after a bad import.

An undone weigh-in does **not** come back. The record of having seen it is kept
deliberately, so the next poll doesn't re-import what you just removed.

**Or delete individual readings in the dashboard.** Open the member → **Bodyweight
trend** → *Recent weigh-ins*, and each row has a delete link. That's the route for
picking one bogus point out of an otherwise-good import — a `107.30 kg` that was
really somebody else standing on the scale, say. Deletions are audited like every
other dashboard edit.

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
| Weigh-ins import but aren't announced | `BODYWEIGHT_REMINDER_CHANNEL_ID` is unset. `/ha_status` says which. |
