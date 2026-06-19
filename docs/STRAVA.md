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

## 3. Configure environment

See `.env.example` for the full block. Minimum:

```dotenv
STRAVA_CLIENT_ID=12345
STRAVA_CLIENT_SECRET=...
STRAVA_PUBLIC_URL=https://bot.example.com
STRAVA_FEED_CHANNEL_ID=123456789012345678
STRAVA_WEBHOOK_VERIFY_TOKEN=some-random-string
# Reuses REVO_FERNET_KEY if unset:
STRAVA_FERNET_KEY=<fernet key>
```

Generate a Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The integration is **idle** (boots fine, does nothing) until `STRAVA_CLIENT_ID`,
`STRAVA_CLIENT_SECRET` and `STRAVA_PUBLIC_URL` are all set. Set `STRAVA_DISABLED=1`
to force it off.

## 4. Create the webhook subscription (once)

After the bot is running and publicly reachable, the **owner** runs:

```
/strava_subscribe
```

Strava immediately calls back to `/strava/webhook` to validate the
`verify_token`, then starts pushing events. Check/clear it with:

```
/strava_subscription          # show the active subscription
/strava_unsubscribe <id>      # delete it
```

Only one subscription per app is allowed.

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
- `/strava_unlink` — revoke access and delete the stored tokens.

## 6. Behaviour notes

- **Scope:** `read,activity:read`. Private activities are fetched but **not
  posted** (we respect the privacy flag).
- **Aspect filtering:** only `create` events post. Edits (`update`) and
  `delete` events are ignored.
- **De-dupe:** the last announced activity id is stored per user, so Strava's
  retry deliveries don't double-post.
- **Token refresh:** access tokens (~6h) are refreshed on demand via the stored
  refresh token; Strava rotates refresh tokens, and the new pair is persisted.
- **Tokens at rest:** both tokens are Fernet-encrypted in the `strava_account`
  SQLite table. The plaintext is never written to disk.

## 7. Troubleshooting

- **`/strava_subscribe` fails with a callback error** — your public URL isn't
  reachable, TLS is invalid, or `verify_token` doesn't match. Curl
  `https://<domain>/strava/webhook?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=test`
  and confirm it echoes `{"hub.challenge":"test"}`.
- **Workouts don't post** — check `STRAVA_FEED_CHANNEL_ID` is set and the bot can
  post there; confirm the member shows up in `/strava_status`; check logs for
  `gymbot.strava` lines.
- **`Strava idle` in logs** — one of the three required env vars is missing.
