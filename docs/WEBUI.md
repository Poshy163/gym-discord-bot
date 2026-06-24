# Web dashboard

An authenticated, browser-based operator dashboard for the gym bot. It runs as
a **second web server** (separate from the Strava callback server) on its own
port and reads/writes the same SQLite database the bot uses. Use it to browse
and edit everything the bot tracks, see each member's roles, and read a unified
audit log — without going through Discord.

It is **disabled by default** and only starts once you set a login password.

## What you get

| Tab | Shows |
| --- | --- |
| **Overview** | Server totals (members, roles, lifts, lifters, exercises) and the latest audit activity. |
| **Members** | Every member with display name, username, role count, join date. Click through to a per-member page with lift/nutrition counters, latest bodyweight, linked Strava/Revo status, full role list, and that member's own audit history. |
| **Roles** | Each role with its colour, position, and a live member count → list. |
| **Audit** | A filterable (role / member / data) feed of changes — see below. |
| **Lifts / Calories / Protein** | The raw entries (optionally filtered to one member), with inline **delete**, and **edit** for lifts. |

## The audit log

The audit log is a single append-only trail in the `audit_log` table, written
by the bot from gateway events and by the dashboard on every edit. It covers:

- **Roles** — a member gaining or losing a role; roles created, deleted, or
  renamed.
- **Members** — joins, leaves, nickname changes, and username changes.
- **Data** — lifts / calories / protein logged through normal bot use (after
  the startup history backfill settles, so re-imports don't flood it) and every
  add/delete/edit performed from the dashboard. Dashboard edits are attributed
  to `web:<ip>` since the dashboard has a single shared login rather than
  per-user identity.

## Setup

1. **Pick a password and a port.** In your `.env`:

   ```dotenv
   WEBUI_PASSWORD=choose-a-strong-secret
   WEBUI_PORT=8081          # default; change if 8081 is taken
   WEBUI_BIND_HOST=0.0.0.0  # bind address inside the container
   ```

   Leaving `WEBUI_PASSWORD` blank keeps the dashboard off. `WEBUI_DISABLED=1` is
   a hard kill-switch that overrides a set password.

2. **Enable the Server Members intent.** The dashboard mirrors the guild's
   members and roles and listens for member/role changes, which requires the
   privileged **Server Members** intent. Turn it on in the
   [Discord Developer Portal](https://discord.com/developers/applications) under
   **Bot → Privileged Gateway Intents → Server Members Intent**. Enabling the
   dashboard flips the intent on in code automatically, but Discord will refuse
   the gateway connection if the portal toggle is off.

3. **Expose the port.** With Docker Compose the example already maps
   `8081:8081`. Match the host port to `WEBUI_PORT` if you changed it.

4. **Restart the bot.** On boot it logs `Web dashboard enabled on 0.0.0.0:8081`
   and syncs the current member/role state into the database.

5. **Open it.** Browse to `http://<host>:8081/`, enter the password, and you're
   in. Sessions are cookie-based and last a week; they reset when the bot
   restarts.

## Security notes

- **Always front it with HTTPS** (a reverse proxy such as Caddy/nginx, or a
  VPN/Tailscale) in any non-trivial deployment. The login password and session
  cookie travel in plaintext over HTTP otherwise, and the dashboard exposes
  member data.
- The dashboard has a **single shared password**, no per-user accounts. Anyone
  with the password can view and edit data. Treat it as operator-only.
- Sessions live in memory, so a restart logs everyone out. There is no rate
  limiting on the login form — keep the port off the public internet.

## How the data stays in sync

- On startup (and on **↻ Sync** in the header, or when the bot joins a new
  guild) the bot does a full refresh of roles + members for each guild.
- Live gateway events keep it current: `on_member_join` / `on_member_remove`,
  `on_member_update` (roles + nickname), `on_user_update` (username), and the
  `on_guild_role_*` events.
- The mirror lives in the `members`, `member_roles`, `guild_roles`, and
  `guild_meta` tables; a member who leaves is kept (marked not-present) so old
  audit rows still resolve to a name.

## Turning it off

Set `WEBUI_PASSWORD=` (blank) or `WEBUI_DISABLED=1` and restart. The server
won't start, and the Server Members intent is no longer requested (unless
`ENABLE_PRESENCE_TRACKING` also needs it). The mirrored tables are harmless if
left in place.
