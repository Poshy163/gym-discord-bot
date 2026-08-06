# Strava integration

The bot can post a Discord embed to a shared feed channel the moment any linked
member finishes a Strava activity. It uses Strava's OAuth2 + **webhook push**, so
new workouts arrive in real time — no polling.

```
member finishes a run  ──▶  Strava  ──POST──▶  bot /strava/webhook
                                                     │
                                                     ▼
                                       fetch full activity (OAuth)
                                                     │
                                                     ▼
                                  post embed to STRAVA_FEED_CHANNEL_ID
```

Because webhooks are a *push*, the bot runs a small `aiohttp` web server
(`app/strava_web.py`) that **must be reachable from the public internet over
HTTPS**. If you can't expose a public URL, this integration won't work as-is
(you'd need to switch to a polling design instead).

---

## 1. Register a Strava API application

1. Go to <https://www.strava.com/settings/api> and create an application.
2. Note the **Client ID** and **Client Secret**.
3. Set **Authorization Callback Domain** to the *bare domain* of your public URL
   (e.g. `bot.example.com` — no scheme, no path). This must match
   `STRAVA_PUBLIC_URL`.

Strava apps start with a low rate limit (100 req/15 min, 1000/day) and a cap of
~1 athlete until you request an increase — fine for a small server.

## 2. Expose the bot publicly

The bot listens on `STRAVA_PORT` (default `8080`) at `STRAVA_BIND_HOST` (default
`0.0.0.0`). Put a reverse proxy (Caddy, nginx, Cloudflare Tunnel, …) in front to
terminate TLS, so that:

- `https://<your-domain>/strava/callback` → bot `:8080/strava/callback`
- `https://<your-domain>/strava/webhook`  → bot `:8080/strava/webhook`

`docker-compose.yml` publishes `8080:8080` for this.

## 3. Configure it

Open the dashboard → **Settings → Strava** and fill in:

| Field | Example |
| --- | --- |
| Client ID | `12345` |
| Client secret | from the Strava API page |
| Public base URL | `https://bot.example.com` |
| Feed channel | the channel workouts should post to |
| Webhook verify token | any random string |

Save, then press **Apply & restart bot**.

You do **not** need to set an encryption key — the bot manages its own and
stores it at `/data/.secret_key`. (Only set one explicitly if you are migrating
an existing deployment and already have stored tokens. See
[CONFIG.md](CONFIG.md#secrets-and-encryption).)

The integration is **idle** (boots fine, does nothing) until the client ID,
client secret and public URL are all set. There is a **Disable Strava entirely**
toggle in the same section to force it off.

> Prefer configuring in `docker-compose.yml`? Every field above has an
> equivalent environment variable, and environment values take priority. See
> `.env.example`.

## 4. Webhook subscription (automatic)

With `STRAVA_AUTO_SUBSCRIBE=1` (the default), the bot **creates the webhook
subscription itself** — a few seconds after startup (retrying so a tunnel that's
still coming up doesn't miss it) and again whenever someone links. You normally
don't have to do anything here.

There's exactly **one subscription per Strava app**, shared by every linked
athlete (it's not per-user or per-channel). If the public callback URL changes,
the bot deletes the stale subscription and recreates it automatically.

Manual controls are still available (owner-only):

```
/strava_subscribe             # force-create it now
/strava_subscription          # show the active subscription
/strava_unsubscribe <id>      # delete it
```

Set `STRAVA_AUTO_SUBSCRIBE=0` if you'd rather manage it by hand.

## 5. Members link their accounts

Each member who wants their workouts posted runs:

```
/strava_link
```

The bot replies (privately) with an authorize link. They approve in the browser,
get redirected to `/strava/callback`, and the bot stores their **encrypted**
tokens and DMs a confirmation. From then on, new activities post automatically.

Other commands:

- `/strava_status` — check whether you're linked.
- `/strava_latest [member]` — post the most recent activity on demand (yours by
  default, or another linked member's).
- `/strava_backfill [days] [limit] [member] [all_linked]` — recover activities
  missed while the API app, webhook subscription, bot, or feed was unavailable.
  It defaults to your last 30 days and at most 25 feed posts. Members can
  backfill themselves; the bot owner can select another member or set
  `all_linked:true`. Re-run it to continue after the limit.
- `/strava_unlink` — revoke access and delete the stored tokens.

### Recovering after the Strava API app was inactive

Once API access is active again, make sure the webhook exists with
`/strava_subscription` (run `/strava_subscribe` if it does not), then run:

```text
/strava_backfill days:30 all_linked:true
```

The backfill is resumable and safe to repeat: it walks forward from each
athlete's last handled activity, posts oldest-first, and keeps a durable
per-activity ledger so a webhook arriving at the same time cannot double-post.
If the result says activities are still queued, run the command again.

## 5a. Route maps (optional Mapbox basemap)

Each posted activity includes an image:

1. the athlete's **photo** if they attached one, else
2. a **route map** if the activity has GPS, else
3. just the stat embed (strength/indoor workouts).

By default the route map is a bare orange line on a dark background (rendered
locally with matplotlib — no API key). For a map that looks like the Strava
website (streets/terrain under the route), set a **Mapbox** token:

```dotenv
STRAVA_MAPBOX_TOKEN=pk.xxxx…
```

Get a free token at <https://account.mapbox.com/access-tokens/>. With it set,
maps are rendered via Mapbox's Static Images API (Discord fetches the URL
directly) with a green start pin, red finish pin, and retina (`@2x`) output.
Without it, the local silhouette is used.

Pick the basemap style with `STRAVA_MAP_STYLE` (default `outdoors-v12`):

```dotenv
STRAVA_MAP_STYLE=satellite-streets-v12   # or streets-v12, outdoors-v12
```

> **"Hidden" route segments:** if you use Strava **privacy zones**, Strava strips
> those portions from the polyline *before* sending it to any third-party app —
> the bot never receives them, so they can't be drawn (with or without Mapbox).
> Adjust your privacy zones in Strava settings if you want the full track shown.

## 5b. Optional filters & units

- **`STRAVA_SPORT_TYPES`** — comma-separated allow-list of Strava `sport_type`
  values (e.g. `Run,Ride,WeightTraining`). Empty posts every type.
- **`STRAVA_MIN_DISTANCE_M`** — skip distance-sport activities under N metres
  (e.g. `1000` ignores sub-1 km walks). `0` disables.
- **`STRAVA_MIN_DURATION_S`** — skip activities under N seconds. `0` disables.
- **`STRAVA_IMPERIAL=1`** — show miles/feet/°F instead of km/m/°C.

## 6. Behaviour notes

- **Scope:** `read,activity:read`. Private activities are fetched but **not
  posted** (we respect the privacy flag).
- **Live edits:** when you **rename** an activity, the posted embed is edited to
  match; if you flip it to **private** or **delete** it, the post is removed.
  New and backfilled posts are tracked individually; the latest legacy post
  remains supported for databases upgraded from an older bot release.
- **Create events** are what trigger a new post; `update`/`delete` drive the
  edits above.
- **Deauthorization:** revoking the bot in Strava settings auto-unlinks you and
  deletes the stored tokens.
- **De-dupe/recovery:** the continuation cursor and a durable per-activity
  ledger stop webhook retries or overlapping backfills from double-posting.
  Failed feed sends release their claim, so the next backfill can retry them.
- **Token refresh:** access tokens (~6h) are refreshed on demand via the stored
  refresh token (serialised per user); Strava rotates refresh tokens, and the
  new pair is persisted.
- **Tokens at rest:** both tokens are Fernet-encrypted in the `strava_account`
  SQLite table. The plaintext is never written to disk.
- **Weekly recap:** if the weekly report is enabled (`WEEKLY_REPORT_CHANNEL_ID`),
  it includes a per-athlete 7-day Strava summary (activities, distance, time,
  elevation).

## 7. Troubleshooting

- **`/strava_subscribe` fails with a callback error** — your public URL isn't
  reachable, TLS is invalid, or `verify_token` doesn't match. Curl
  `https://<domain>/strava/webhook?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=test`
  and confirm it echoes `{"hub.challenge":"test"}`.
- **Workouts don't post** — check `STRAVA_FEED_CHANNEL_ID` is set and the bot can
  post there; confirm the member shows up in `/strava_status`; check logs for
  `gymbot.strava` lines.
- **`Strava idle` in logs** — one of the three required env vars is missing.
