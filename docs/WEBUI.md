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
| **Members** | Searchable list with avatars. Click through to a rich per-member page: lift/nutrition counters, **today's calories & protein vs goal** (progress bars, measured against whichever target set today falls under), **nutrition targets** (weekday + weekend calories and protein, editable — leave a weekend box empty to use the weekday number all week, clear a weekday box to switch that tracker off; changes apply from today and never re-score past days), a **bodyweight trend sparkline**, and Home Assistant **body-composition trends** (the latest value, change from the previous reading, and up to 90 recent points for every recognized scale metric), **lift goals** with progress, the member's **saved foods** (add / edit / delete, including protein), linked Strava/Revo/Home Assistant status, full role list, and audit history. Consumer smart-scale composition values are estimates affected by hydration and measurement conditions, so the dashboard presents deltas neutrally and labels them as non-medical trends. |
| **Activity** | A game/presence feed for every tracked user (window default **7 days**): avatar with a live status dot (online/idle/dnd/offline), what they're playing right now (with the game's art when Discord exposes rich-presence assets, else a clean coloured tile), and their most-played games with playtime bars. **📜 Log** on any card (or clicking a game in its most-played list) opens that member's **session history** — every stretch of a title with its clock times and length ("Rainbow Six Siege · 22:04 → 00:31 · 2h 27m"), newest first and grouped by day, with a still-running stint marked live. Click a title to filter the log to it. Times are shown in `DISPLAY_TIMEZONE`. Needs `ENABLE_PRESENCE_TRACKING=true` + users added via `/track start`. |
| **Messages** | A **Discord-style message browser**: a channel sidebar (each channel with its logged-message count, most-active first) and a chat pane showing that channel's history grouped by author, like Discord — including **photos/images and GIFs** (Tenor/Giphy GIFs play inline; user mentions render as names). Messages are logged for **all members and bots** once they post (`ENABLE_MESSAGE_LOGGING`, on by default; set `=false` to disable) — so the bot's own announcements appear too. On startup the bot **back-scans recent history** so it isn't empty on a fresh deploy (`MESSAGE_LOG_BACKFILL_DAYS`, default 30, `0` to skip). You can blacklist a member straight from a chat message (hover → **🚫**) or by ID via the sidebar **🚫 Blacklist** button, with a reason. A blacklisted member **can no longer add anything to the bot** — chat logging (lifts/calories/protein/bodyweight) *and* slash/prefix commands are all blocked — but their **messages are still logged and kept** (nothing is deleted). The bot posts a public message **pinging them with the reason**. |
| **Voice** | **Who's in voice right now** — each occupied voice channel with its members (and 🔴 streaming / 🔇 deaf / 🔈 mute indicators), read live from Discord — plus a **join / leave / move log** of recent voice activity. Tracking uses only the non-privileged voice_states intent; on by default (`ENABLE_VOICE_TRACKING`, set `=false` to disable). |
| **Roles** | Each role with its colour, position, and a live member count → list. |
| **Leaderboard** | Pick an exercise; see the ranked best lift per member, with 🥇🥈🥉 and avatars. |
| **Audit** | A filterable (role / member / data) feed of changes, with actor + subject avatars — see below. |
| **Lifts / Calories / Protein** | The raw entries (searchable, optionally filtered to one member), with inline **delete**, and **edit** for lifts. |

Avatars (Discord profile pictures) appear throughout; a member with no avatar
falls back to a coloured initial. All edits made here — including saved-food
changes — are written to the audit log under `web:<ip>`.

The dashboard is responsive: navigation scrolls horizontally on narrow screens,
data tables remain usable without widening the whole page, and the Messages
browser stacks its channel list above chat on phones. Keyboard users can press
`/` from any searchable page to jump directly to its search field. The active
section, loading state, errors, dialogs, and action notifications are also
exposed to assistive technology.

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
    `/undo`, an edit that removed the amount), recorded with *who* triggered
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

The dashboard is always on — it is where the bot is configured, so it cannot
depend on the bot being configured. There is nothing to set up in advance.

1. **Start the container** and open `http://<host>:8099/` (the compose file maps
   host 8099 to the container's 8081).

2. **Set a password.** The first visit shows a setup page; choose a password of
   at least 12 characters. From then on it's the normal login page.

   > **The claim window is open until you do this.** Anyone who can reach the
   > port can set that password and take control of the bot. Keep it on a LAN or
   > behind a VPN, or set the password immediately after starting the container.
   > The claim is written to the audit log with the claiming IP.

3. **Enter your Discord token** under **Settings → Discord**. The bot connects
   within a few seconds.

4. **Turn on member mirroring** (optional) under **Settings → Dashboard →
   Mirror members & roles**. This populates the Members and Roles tabs and the
   audit log. It requires the privileged **Server Members** intent — turn it on
   in the [Discord Developer Portal](https://discord.com/developers/applications)
   under **Bot → Privileged Gateway Intents → Server Members Intent** *first*,
   or Discord will refuse the gateway connection and the bot won't start.

If you previously ran the dashboard with `WEBUI_PASSWORD` set, that password
keeps working and there is no setup page — see
[docs/CONFIG.md](CONFIG.md#upgrading-an-existing-deployment).

Change the password later under **Settings → Dashboard account**. That signs
out every existing session, including your own on other devices.

Lost it? `docker compose exec gym-bot python -m app.supervisor reset-password`.

## Security notes

- **Always front it with HTTPS** (a reverse proxy such as Caddy/nginx, or a
  VPN/Tailscale) in any non-trivial deployment. The login password and session
  cookie travel in plaintext over HTTP otherwise, and the dashboard exposes
  member data. The session cookie is marked `Secure` automatically when the
  request arrives over HTTPS.
- The dashboard has a **single shared password**, no per-user accounts. Anyone
  with the password can view and edit data, **including the Discord token and
  the admin user list**. Treat it as full control of the bot.
- The password is stored as a PBKDF2-HMAC-SHA256 hash (600,000 iterations), not
  in plaintext.
- The login form is rate limited: five wrong passwords from one IP triggers a
  15-minute lockout. Note the client IP is taken from `X-Forwarded-For`, so the
  limit is only as trustworthy as your proxy.
- Sessions live in memory, so a restart of the *supervisor* logs everyone out.
  A bot restart does not.

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

Set `WEBUI_DISABLED=1` in `docker-compose.yml` and restart. This is deliberately
the **only** way to disable it — it is not editable from the dashboard, because
turning off the dashboard from inside the dashboard would be a one-way door.

With it off, the bot runs on whatever configuration is already stored plus your
environment variables, and there is no way to change anything without editing
compose again. The mirrored tables are harmless if left in place.

To stop mirroring members and roles without disabling the dashboard, turn off
**Settings → Dashboard → Mirror members & roles**. That also stops the bot
requesting the Server Members intent (unless presence tracking still needs it).
