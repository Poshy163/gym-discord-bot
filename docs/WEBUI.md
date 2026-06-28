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
| **Members** | Searchable list with avatars. Click through to a rich per-member page: lift/nutrition counters, **today's calories & protein vs goal** (progress bars), a **bodyweight trend sparkline**, **lift goals** with progress, the member's **saved foods** (add / edit / delete, including protein), linked Strava/Revo status, full role list, and audit history. |
| **Activity** | A game/presence feed for every tracked user (window default **30 days**): avatar with a live status dot (online/idle/dnd/offline), what they're playing right now (with the game's art when Discord exposes rich-presence assets, else a clean coloured tile), and their most-played games with playtime bars. Needs `ENABLE_PRESENCE_TRACKING=true` + users added via `/track start`. |
| **Messages** | A **Discord-style message browser**: a channel sidebar (each channel with its logged-message count, most-active first) and a chat pane showing that channel's history grouped by author, like Discord. Messages are logged for **all members** once they chat (`ENABLE_MESSAGE_LOGGING`, on by default; set `=false` to disable); on startup the bot **back-scans recent history** so it isn't empty on a fresh deploy (`MESSAGE_LOG_BACKFILL_DAYS`, default 30, `0` to skip). A **🚫 Blacklist** control lets you exclude a member by user ID with a reason: their stored messages are purged, they're never logged again, and the bot posts a public message **pinging them with the reason**. |
| **Roles** | Each role with its colour, position, and a live member count → list. |
| **Leaderboard** | Pick an exercise; see the ranked best lift per member, with 🥇🥈🥉 and avatars. |
| **Audit** | A filterable (role / member / data) feed of changes, with actor + subject avatars — see below. |
| **Lifts / Calories / Protein** | The raw entries (searchable, optionally filtered to one member), with inline **delete**, and **edit** for lifts. |

Avatars (Discord profile pictures) appear throughout; a member with no avatar
falls back to a coloured initial. All edits made here — including saved-food
changes — are written to the audit log under `web:<ip>`.

## The audit log

The audit log is a single append-only trail in the `audit_log` table, written
by the bot from gateway events and by the dashboard on every edit. It covers:

- **Roles** — a member gaining or losing a role (**with the moderator who made
  the change**, when available — see below); roles created, deleted, or renamed.
- **Members** — joins, leaves, nickname/username changes, and **kicks/bans**
  (with the moderator and reason, when the audit-log permission is granted).
- **Data** — essentially everything that mutates tracked data, after the
  startup backfill settles so re-imports don't flood it:
  - **logs** — lifts, calories, protein logged through normal bot use;
  - **reverts/undos** — when the bot removes an entry (the ❌ reaction undo,
    `/undo`, `/calories undo`, `/protein undo`), recorded with *who* triggered
    it;
  - **goals** — lift goals set/removed, calorie & protein targets set, and
    tracking turned off;
  - **bodyweight** logged;
  - **saved foods** created/removed;
  - every **add / delete / edit** performed from the dashboard itself
    (attributed to `web:<ip>`, since the dashboard has one shared login).

  The audit tab shows friendly labelled actions (🏋️ logged, ↩️ undone, 🎯 goal
  set, 👢 kicked …), is filterable by category, searchable, and pages through
  the full history with **Load more**.

### Seeing *who* changed a role

Discord's member-update gateway event says a member's roles changed but not who
changed them. To attribute role and nickname changes to the moderator who made
them, the bot reads the guild's audit log via the `on_audit_log_entry_create`
event — which requires the bot to have the **View Audit Log** permission in the
server (the moderation intent it also needs is non-privileged and on by
default). Grant that permission and audit rows read e.g. *"gained role Admin (by
Josh)"* with the actor shown in the **Actor** column.

Without that permission the change is still recorded (so nothing is lost), just
without the actor — the row reads *"gained role Admin"* and the actor shows as
`—`.

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
