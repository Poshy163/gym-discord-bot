# Apple Health / Fitness

This integration imports Apple Watch and iPhone workout summaries without a
Strava subscription or another paid service. HealthKit is device-local, so the
bot cannot poll Apple directly: a personal iPhone Shortcut sends the workout to
Gym Bot's authenticated HTTPS endpoint.

Only the fields put in the Shortcut dictionary leave the phone. The private
bearer token is shown once by Discord and stored by the bot only as a SHA-256
hash.

## Host setup

1. Expose the bot's integration server (`STRAVA_BIND_HOST` / `STRAVA_PORT`) at
   a public HTTPS URL. The existing Strava reverse proxy or tunnel can be reused.
2. Set `APPLE_HEALTH_PUBLIC_URL=https://your-bot.example.com`. Leave it blank to
   reuse `STRAVA_PUBLIC_URL`.
3. Optionally set `APPLE_HEALTH_FEED_CHANNEL_ID`. Leave it blank to reuse
   `STRAVA_FEED_CHANNEL_ID`; if both are blank, imports remain available in
   `/cardio apple_recent` and `/coach` without public posts.
4. Restart the worker, then check `GET /healthz`.

The receiver is:

```text
POST https://your-bot.example.com/apple-health/workouts
Authorization: Bearer MEMBER_TOKEN
Content-Type: application/json
```

Do not put the token in the URL. Query strings commonly appear in proxy access
logs.

## Member setup

Run `/cardio apple_link` in Discord. Copy the endpoint and one-time token
directly into a private Shortcut. If it is lost, use
`/cardio apple_link rotate:true`; rotation immediately invalidates the old
Shortcut.

### Workout End automation

In Shortcuts:

1. Open **Automation → + → Apple Watch Workout → Ends**.
2. Choose the workout types (or Any Workout), select **Run Immediately**, and
   disable notification/confirmation prompts if iOS offers those options.
3. Use the automation's Workout input, plus **Get Details of Health Sample**, to
   read the workout UUID, activity type, start date, end date, duration, active
   energy, distance, average heart rate, elevation, and source when available.
4. Add a **Dictionary** using the contract below. Missing optional Health fields
   should be omitted rather than replaced with text such as `N/A`.
5. Add **Get Contents of URL**:
   - URL: the endpoint from `/cardio apple_link`
   - Method: `POST`
   - Request body: `JSON`, using the Dictionary
   - Header: `Authorization` = `Bearer MEMBER_TOKEN`

The recommended dictionary is:

```json
{
  "id": "Health workout UUID",
  "activity": "Running",
  "started_at": "2026-08-06T07:00:00+09:30",
  "ended_at": "2026-08-06T07:42:00+09:30",
  "duration_minutes": 42,
  "active_kcal": 410,
  "distance_m": 7200,
  "avg_heart_rate": 151,
  "elevation_m": 42,
  "effort": 7,
  "source_name": "Apple Watch"
}
```

`activity`, `started_at`, and either `ended_at` or `duration_minutes` are
required. Dates must include a timezone. `effort` is an optional personal 1–10
rating; it is useful to the coach but is not required to match an Apple field.
Distance is metres and elevation is metres.

The receiver also accepts Apple's common camel-case names (`workoutType`,
`startDate`, `endDate`, `activeCalories`, `distanceMeters`,
`averageHeartRate`, and `sourceName`).

## Automatic replay / backfill

Personal automations occasionally miss a run because the phone was offline,
restarting, or unable to reach the public URL. Add a second daily automation:

1. Run at a convenient time while the phone is normally online.
2. **Find Health Samples** where Type is Workout and Start Date is in the last
   7 days.
3. Repeat each result, build the same dictionary as above, and add each
   dictionary to a list.
4. Wrap the list in a Dictionary under the key `workouts`, then POST it to the
   same endpoint and bearer token.

```json
{
  "workouts": [
    {
      "id": "workout-uuid-1",
      "activity": "Cycling",
      "started_at": "2026-08-05T17:00:00+09:30",
      "ended_at": "2026-08-05T18:00:00+09:30"
    }
  ]
}
```

Up to 100 workouts can be sent per request. Replaying the same window is safe:
the Health workout UUID is converted to a stable identity and duplicates are
reported but not stored or posted again. If no UUID is supplied, the bot derives
the identity from activity, start, end, and duration.

For a larger one-time history import, widen the Find Health Samples date range
and send it in batches of 100. Keep the daily automation at 7 days afterward.

## Commands

- `/cardio apple_link [rotate]` creates or rotates the private Shortcut token.
- `/cardio apple_status` shows the import count and last received time.
- `/cardio apple_recent [limit]` shows recent imports privately.
- `/cardio apple_help` gives the in-Discord setup summary.
- `/cardio apple_unlink` invalidates the token and deletes imported workout
  rows.

Unlinking does not delete Discord messages already posted to a public feed.
Apple Health imports are included in `/coach`, with an explicit instruction not
to double-count a workout that also appears in native cardio or lifting logs.

## Response and troubleshooting

A successful response reports `accepted`, `duplicates`, and any per-item
`rejected` errors. Common responses:

- `401 missing bearer token`: the Authorization header is absent or malformed.
- `401 invalid bearer token`: rotate the link and update the Shortcut.
- `400 ... must include a timezone`: format the Shortcut date as ISO 8601 with
  its offset.
- `400 provide ended_at or duration_minutes`: include one of those values.
- HTTP 413: the request exceeds 1 MiB; reduce the replay date range.

The bot logs import counts and rejected authentication attempts, but never logs
the supplied token or full Health payload.
