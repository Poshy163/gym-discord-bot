# Configuration

Everything is configured from the dashboard's **Settings** tab. `docker-compose.yml`
carries no configuration at all — just the image, a restart policy, two ports and
a volume.

```yaml
services:
  gym-bot:
    image: ghcr.io/poshy163/gym-discord-bot:latest
    restart: unless-stopped
    ports:
      - "8098:8080"
      - "8099:8081"
    volumes:
      - gym-data:/data

volumes:
  gym-data:
```

## First boot

1. `docker compose up -d`
2. Open `http://<host>:8099` and set a dashboard password.
3. **Settings → Discord → Bot token**, paste your token, Save.

The bot starts as soon as a token is saved. Until then the dashboard runs on its
own — that is the whole reason the container is split into a supervisor and a
bot process, and it is what makes step 3 possible at all.

> **The claim window is open.** Between steps 1 and 2 anyone who can reach port
> 8099 can set the password and take control of the bot — including reading
> every logged message and attachment. Keep the port on a LAN or behind a
> VPN/reverse proxy, or complete setup immediately. The claim is recorded in the
> audit log with the claiming IP.

## Precedence: environment > database > default

For **every** setting, in that order, with no exceptions.

Environment wins so that an existing deployment upgrades with **zero** behaviour
change, provably by inspection rather than by trusting a migration. Whatever is
pinned in your compose file keeps winning forever; if you change nothing,
nothing changes.

The alternative (database wins) fails silently and confusingly: you would edit
your compose file, restart, and see nothing happen, with nowhere the UI would
ever think to tell you. Under environment-wins the confusing case is visible
exactly where you already are — the field shows an amber **Pinned by
environment** chip, and saving it says:

> Saved, but your compose file still overrides it.

The value is stored and becomes real the moment you remove the pin. Nothing is
discarded.

Environment is also the escape hatch of last resort. If a stored value somehow
breaks a boot, `docker compose run -e SETTING=value ...` overrides it without
touching the database.

## Upgrading an existing deployment

Pull the new image and `docker compose up -d`. Nothing else.

On first boot the supervisor copies every setting **you actually set** in the
environment into the database, then resolves everything exactly as before — your
environment still wins, so the values are identical. A Settings tab appears,
pre-filled, with every pinned field wearing an amber chip.

When you're ready, delete `env_file:`/`environment:` and restart. The same values
now resolve from the database and the chips disappear.

Three details worth knowing:

- **Only keys you set are imported.** Code defaults are deliberately not written
  as rows — an explicit row would freeze that default forever and silently block
  a future change to it.
- **The import runs once**, and uses `INSERT OR IGNORE` behind a sentinel, and
  never deletes rows. Clearing a setting writes NULL rather than removing the
  row, so a re-run cannot resurrect something you deliberately cleared.
- **`ENABLE_MEMBER_MIRROR` is computed from your pre-upgrade environment.**
  Before this change, member/role mirroring was implied by having set
  `WEBUI_PASSWORD`. The dashboard now always exists, so that flag was renamed
  and its value is derived from whether you had a dashboard password before. It
  requests the **privileged** Server Members intent, so getting this wrong would
  make Discord refuse the gateway.

## Applying a change

Each setting declares what it costs to apply:

| Scope | What happens | Examples |
|---|---|---|
| **hot** | Effective on the next use, no restart | admin IDs, auto-scan channels, max weight, backup schedule, message logging |
| **worker** | Staged; press **Apply & restart bot** | Discord token, timezone, schedule times, gateway intents, poll intervals |
| **env-only** | Not editable — shown disabled | `DB_PATH`, `WEBUI_PORT`, `WEBUI_BIND_HOST`, `WEBUI_DISABLED`, `STRAVA_PORT` |

Saves are staged rather than applied one at a time, so setting four reminder
fields costs one restart rather than four — each of which would otherwise re-run
the startup backfill and the command sync.

The env-only five stay in compose because the host half of every port mapping
and volume mount lives there, where the UI cannot reach. That is deliberate: it
makes locking yourself out of the dashboard structurally impossible rather than
merely unlikely. `DB_PATH` additionally cannot live inside the database it
locates.

### When a change stops the bot

Two consecutive fast failures put the bot in **quarantine**: the supervisor stops
respawning, keeps the last 200 lines of its output, and shows them in the Settings
tab with a plain-English headline —

- *Discord rejected the bot token* — paste a fresh one.
- *A privileged intent isn't enabled* — turn it on in the Developer Portal, or
  turn off Presence tracking / Mirror members.
- *No token yet* — an idle state, never treated as a failure.

It does **not** auto-revert. Auto-revert would silently undo intentional changes,
and the commonest real case is pasting a new token because the old one was
revoked — where auto-revert restores a dead token and buries the real error. The
dashboard is up regardless, so **Revert last change** is one click and tells the
truth about what it did.

> Revert cannot restore a secret. Secret history is redacted, so reverting a bad
> token **clears** it rather than restoring the previous one.

Because the supervisor stays up, the container stays healthy while the bot is
down. That is intentional — it is exactly when you need the container running so
you can fix it. Alerting that wants the stricter test should use
`/healthz?require_worker=1`.

## Secrets and encryption

Secrets are encrypted at rest with a key at **`/data/.secret_key`** (mode 0600),
generated on first boot.

**Back that file up with your database.** Without it, every stored secret and
every linked Revo, Strava and Hevy account is unrecoverable. This replaces the
old advice to back up your `.env` for the `*_FERNET_KEY` values.

What the encryption does and does not do, plainly:

- **Protects snapshots that leave the box.** The nightly backup copies the
  database file only, so `/data/backups/*.sqlite3` contains ciphertext with no
  key. That is the entire reason the key is a separate file.
- **Does not protect against read access to `/data`.** The key sits beside the
  ciphertext, owned by the same uid. A container escape, `docker cp`, or a
  `docker exec` shell yields both halves.
- **Versus before:** the keys moved from a `.env` next to your compose file into
  the data volume. Better for anyone who backs up `/data`, worse for nobody.

If you already have a `*_FERNET_KEY` set — in the environment or from a previous
version — it is used as-is and **no new key is generated**. A key is only ever
created when none of the three exists anywhere.

### Existing Fernet keys

`REVO_FERNET_KEY` is the one a generated key is stored under, because
`app/revo_client.py` reads only that name and has no fallback chain, while
`strava_client` falls back to it and `hevy_client` falls back to both. One key
under that name therefore serves all three.

## Exporting

**Settings → Dashboard account → Download .env** reproduces your whole
configuration as environment variables. It is the downgrade path, the
host-migration path, and the "I want my config in a file" path.

It contains every secret in plain text, so it sits behind a confirmation and is
logged. Treat the file like a password.

## Recovering a lost dashboard password

```
docker compose exec gym-bot python -m app.supervisor reset-password
```

This clears the stored password and reopens the claim page. Anyone who can run
`docker exec` already owns the container and its data, so this adds no exposure
— it just avoids requiring `sqlite3` surgery on the volume.

## Pinning something in compose anyway

Still supported, and still wins. Useful for values you manage with an external
secret store, or to temporarily override a stored value:

```yaml
services:
  gym-bot:
    image: ghcr.io/poshy163/gym-discord-bot:latest
    environment:
      DISCORD_TOKEN: ${DISCORD_TOKEN}
    ports: ["8098:8080", "8099:8081"]
    volumes: [gym-data:/data]
```

See [`.env.example`](../.env.example) for every recognised name.
