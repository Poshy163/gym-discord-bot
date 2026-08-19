# Hevy integration

Link a [Hevy](https://www.hevyapp.com/) account and the bot will, on a short
polling interval, **import each new workout as lifts** (so it shows up in
`/stats`, leaderboards and PRs alongside chat-logged lifts) **and post a feed
embed** summarising the workout to a shared channel.

```
member finishes a Hevy workout
        │
        ▼  (bot polls every HEVY_POLL_MINUTES)
  GET api.hevyapp.com/v1/workouts   (per-user API key)
        │
        ├─▶ import exercises/sets as lifts  (dedup on Hevy workout id)
        └─▶ post embed to HEVY_FEED_CHANNEL_ID

member logs a bodyweight (chat, /bodyweight, or a linked smart scale)
        │
        └─▶ POST/PUT api.hevyapp.com/v1/body_measurements
```

Data flows **both ways**: workouts come in, weigh-ins go out.

Unlike Strava, Hevy uses a **per-user API key** (no OAuth) and the bot only makes
**outbound** calls — there's **no public web server to expose**.

---

## Requirements

- **Hevy Pro** — the API key is a Pro feature (Hevy app → **Settings → API**).
- Nothing else. API keys are encrypted at rest with a key the bot generates and
  manages itself at `/data/.secret_key` — back that file up with your database.
- `requests` and `cryptography` (already bundled for the Strava/Revo features).

## Configuration

Dashboard → **Settings → Hevy**:

| Setting | Default | Meaning |
| --- | --- | --- |
| Disable Hevy entirely | off | Turns the integration off. |
| Feed channel | — | Channel for workout embeds. Blank → lifts still import, no feed post. |
| Poll interval (minutes) | `15` | How often to check Hevy for new workouts (minimum 1). |
| Mirror weigh-ins to Hevy | on | Write each new bodyweight to the member's Hevy body measurements. |

Changing any of these stages a bot restart; press **Apply & restart bot**.

The integration is **on by default** when `requests`/`cryptography` are
available; importing works even without a feed channel.

> These also have `HEVY_*` environment-variable equivalents, which take priority
> if set. See `.env.example`.

## Member usage

- `/hevy_link api_key:<key>` — paste the key from Hevy → Settings → API. Best run
  in a **DM** so the key stays private; the reply is always ephemeral and the key
  is stored **encrypted**.
- `/hevy_status` — show whether you're linked, your Hevy profile, when it last
  synced, and whether weigh-ins are being mirrored.
- `/hevy_unlink` — delete your stored key and import history.

Only `/hevy_link` replies privately (so the key never appears in a channel); the
other Hevy commands reply publicly.

## Behaviour notes

- **Rate limits are retried, not surrendered to.** Hevy rate-limits per API key
  and one sync can burst a dozen requests, so a 429 is retried up to four times,
  honouring Hevy's own `Retry-After` when it sends one and backing off 2/4/8s
  when it doesn't. Without this the first refusal abandoned the whole sync.
- **Exercise names follow Hevy's own vocabulary.** Hevy writes machines as
  `Butterfly (Pec Deck)` and `Seated Shoulder Press (Machine)`; both used to fork
  their own equipment rather than meeting a chat-logged `pec dec` or `ohp` in the
  same PR and leaderboard. Assisted variants stay on a bodyweight-assisted key,
  where the logged kg is read as machine *assistance* (true load = bodyweight −
  assistance) rather than as load.
- **Renames are applied retroactively, once, and announced.** A Hevy workout id
  is recorded as imported for good, so nothing will ever re-read it — an alias
  fix would otherwise only help future workouts and leave the existing ones in
  the wrong bucket. A one-shot migration (`hevy_equip_recanon_v2`, gated on
  `app_meta` like its v1 predecessor) re-derives equipment from the Hevy title
  kept in each lift's `raw`, and leaves a summary the bot posts to the feed on
  its next start. It says so out loud because a rename merges two histories:
  somebody's PR can move without them having lifted anything.

  **Any future alias change affecting a Hevy title needs its own `_vN` key** —
  there is no other route back to those rows.
- **Weighted and assisted bodyweight lifts are told apart.** "Pull Up
  (Weighted)" and "Pull Up (Assisted)" both canonicalise to `pull ups`, which
  the bot reads as an assistance-logged lift — so a +20kg weighted pull-up used
  to be recorded as 20kg of *assistance*, giving a true load of bodyweight minus
  20 instead of plus. At 104kg bodyweight that is 83.9kg instead of 123.9kg: a
  40kg error in the wrong direction that makes adding weight look like a
  regression. Hevy states which it is in the exercise template's `type`
  (`bodyweight_weighted` vs `bodyweight_assisted`), so the importer consults the
  template catalogue it already fetches. With no template available the
  assistance reading stands, since that is the commoner logging style and what
  the equipment name implies.
- **No double-logging:** each Hevy workout id is recorded once imported, so
  repeated polls never re-import. Unlinking clears that history.
- **First sync is quiet:** the poll right after linking imports your recent
  workouts as lifts and posts a **single** summary embed for the whole backfill,
  rather than one embed per historical workout. New workouts after that each
  post their own.
- **What imports:** one lift per set with a positive `weight_kg` — including
  warmup sets, which are counted separately in the embed but still logged.
  Sets with no weight (bodyweight-only, cardio) become no lift. Exercise names
  are canonicalised, so a Hevy "Bench Press (Barbell)" lands on the same
  equipment as a chat-logged "bench".
- **Lift-free workouts still post.** A pure calisthenics or treadmill session
  produces no lifts, and used to vanish silently — imported, but never shown.
  It now gets a feed embed describing what it *did* record: reps, distance and
  time, with the volume line omitted rather than reading "0 kg".
- **What the embed shows:** exercises, sets (working vs warmup), reps, volume,
  duration, per-exercise breakdown and top set, plus — where Hevy has the data —
  a **muscle-group split**, RPE, dropset and to-failure counts, distance/time
  per exercise, the workout description and your Hevy profile link.
- Workouts are filed under the **server you linked from** (or your `/server`
  default when linking via DM).

## Weigh-ins are mirrored into Hevy

Hevy's API also stores **body measurements**, so the bot writes to it as well as
reading from it. Whenever a bodyweight is recorded — typed in chat, logged with
`/bodyweight`, or imported from a member's Home Assistant smart scale — it is
written to that member's Hevy body measurements for the day.

- **What is sent:** `weight_kg` always; `fat_percent` and `lean_mass_kg` too when
  the reading came from a scale that reports body fat / lean mass. The bot has no
  source for Hevy's tape-measure fields (waist, chest, arms, ...) and never
  invents one.
- **Your hand-entered measurements are safe.** Hevy's update endpoint overwrites
  *every* field it is sent and nulls the ones it isn't, so a naive write would
  wipe the circumferences you typed into the Hevy app. The bot creates the day's
  entry if it is free, and otherwise re-reads it and merges its three values over
  the top, leaving everything else untouched. If Hevy already holds the same
  numbers, nothing is written at all.
- **One entry per day.** Hevy keys measurements by calendar date, so two weigh-ins
  on the same day collapse into one — the later wins, which is what the Hevy app
  shows anyway.
- **Implausible readings are dropped, not clamped.** A smart scale glitching to
  6553.5 kg is never mirrored (it would be far harder to retract from Hevy than
  from the bot).
- **Linking a scale doesn't backfill Hevy.** The first import after linking Home
  Assistant pushes only its newest reading — the *reconcile* below is what fills
  in history, once, deliberately.
- **Hevy failures never fail a weigh-in.** The push happens *after* the weigh-in
  is safely in the bot's database, and any error is logged and swallowed — the
  bot's log is the source of truth and Hevy is the copy.
- Turn it off with **Mirror weigh-ins to Hevy** in the dashboard, or
  `HEVY_PUSH_BODYWEIGHT=0`.

### The one-time reconcile

Mirroring only covers weigh-ins from the moment it is switched on, which would
leave every existing member with two half-histories. So the **first** poll after
an account is linked merges them, in both directions at once:

| | Bot has the day | Bot doesn't |
| --- | --- | --- |
| **Hevy has the day** | left alone | imported as a weigh-in |
| **Hevy doesn't** | pushed to Hevy | — |

- **Already-linked members get this too.** The marker starts NULL, so an account
  linked long before the feature existed looks exactly like a fresh one to the
  poll and is caught up on its next pass. Nobody has to re-link.
- **A day both sides already have is never touched.** That is what stops the
  reconcile echoing — re-importing a weight the bot pushed moments earlier — and
  what makes re-running it (`/hevy_sync`) a no-op: anything imported the first
  time is now one of the bot's own days. There is no ledger table; the two
  histories *are* the ledger.
- **Imported days land at local midday**, matching how the bot materialises any
  other date without a time — midnight would sort before every same-day scale
  reading and could collide exactly with one.
- Body fat and lean mass come across with the weight. Entries in Hevy that record
  only circumferences describe no weigh-in and are skipped, as are implausible
  weights.
- **Bounded to the most recent 200 entries per side.** Hevy pages body
  measurements ten at a time, so an unbounded merge on a years-old daily-weigh-in
  account would be hundreds of round trips on first contact.
- **Nothing older than 180 days is imported.** A lone year-old entry lands in a
  stretch the bot has no other data for, where the trend line either side of it
  is pure interpolation across the gap — it drags the chart's window back months
  and skews the headline trend for the sake of one point. Pushing *out* to Hevy
  is not age-limited; filling Hevy's own history costs nothing.
- **The merge is claimed before it runs**, so the poll firing on startup while
  somebody runs `/hevy_sync` cannot both walk the same history. A day is also
  re-checked immediately before it is written, because `bodyweights` has no
  unique constraint and would happily accept a second identical row.
- If Hevy is unreachable the marker is left unset, so the merge is retried next
  poll rather than being silently skipped forever.
