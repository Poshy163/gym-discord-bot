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
```

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

Changing any of these stages a bot restart; press **Apply & restart bot**.

The integration is **on by default** when `requests`/`cryptography` are
available; importing works even without a feed channel.

> These also have `HEVY_*` environment-variable equivalents, which take priority
> if set. See `.env.example`.

## Member usage

- `/hevy_link api_key:<key>` — paste the key from Hevy → Settings → API. Best run
  in a **DM** so the key stays private; the reply is always ephemeral and the key
  is stored **encrypted**.
- `/hevy_status` — show whether you're linked and when it last synced.
- `/hevy_unlink` — delete your stored key and import history.

Only `/hevy_link` replies privately (so the key never appears in a channel); the
other Hevy commands reply publicly.

## Behaviour notes

- **No double-logging:** each Hevy workout id is recorded once imported, so
  repeated polls never re-import. Unlinking clears that history.
- **First sync is quiet:** the poll right after linking imports your recent
  workouts as lifts but does **not** post feed embeds (so linking doesn't spam
  the channel with backfill). New workouts after that post normally.
- **What imports:** one lift per *weighted working set* (positive `weight_kg`);
  bodyweight-only/cardio sets are skipped. Exercise names are canonicalised, so a
  Hevy "Bench Press (Barbell)" lands on the same equipment as a chat-logged
  "bench".
- Workouts are filed under the **server you linked from** (or your `/server`
  default when linking via DM).
