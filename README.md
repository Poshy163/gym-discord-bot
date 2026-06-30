# Gym Discord Bot

A Discord bot that reads the gym-stats posts your friends already write, parses
out the equipment + weight, and tracks progress month-by-month so you can
settle all "did you actually get stronger?" debates with data.

## What it tracks

Auto-parses posts like:

```
Shoulder press: 45 kg
Lat pulldown: 43 kg
Bench Press: 55 kg
Incline bench 70
Dips: BW+20kg
Leg Press: 6 plates
Leg curls: 50 - 77 kg
@Cookie Monster squat 55kg
```

Rules it uses:

- `equipment: value` (or bare `equipment value` when a `kg` / `plates` unit is
  present) is treated as a lift.
- `Xkg` / `X kg` -> X kg.
- `N plates` -> N × 20 kg (configurable via `PLATE_KG` in `app/parser.py`).
- `BW+Xkg` -> X kg recorded with the bodyweight-add flag.
- `A - B kg` range -> the higher number (treated as top working weight).
- A leading Discord user mention logs the lift for that person instead of the
  message author, so `@Cookie Monster squat 55kg` stores Cookie Monster's squat.
- Passive auto-logging only stores structured lift lines such as
  `leg press 295kg`, `squat: 70kg`, or `@Cookie Monster calf raise 90kg`.
  Conversational sentences are ignored unless you explicitly run `/parse`.
- Weights over `MAX_WEIGHT_KG` are skipped as likely typos/fakes. The default is
  `500`, which catches mistakes like `2200kg` before they hit leaderboards.
- Many aliases are unified (e.g. `pec dec`, `pec fly`, `chest fly`, `pekdek`
  all become `pec dec`). See `app/aliases.py`.

The bot reacts with ✅ on messages whose lifts were stored, and de-duplicates
per `(message_id, equipment)` so edits / re-parses won't double-count.

## Slash commands

Every command also works in a **DM with the bot** (and as a user-installed app),
not just in a server channel — see [Direct messages](#direct-messages) below.

Stats & progress:

- `/help` — in-bot command reference.
- `/server [server]` — only useful in DMs: pick which server your DM commands
  act on when you're in more than one with the bot. With no argument it shows
  your current default and the servers you share.
- `/stats [user]` — personal bests for a user.
- `/summary [user]` — profile overview (totals, top PRs, most trained, gains,
  current weekly streak).
- `/coach [user] [days]` — an AI progress report built from **all** of a
  member's tracked data (lifting summary, PRs, biggest gains, goals, training
  frequency, bodyweight trend, and calorie/protein goals + recent totals). The
  whole dataset is handed to Gemini, which writes a personalised strength +
  nutrition breakdown with concrete next steps. It's told that missing/zero
  values mean "not logged" (a tracking gap to nudge about), not real zeros — so
  it won't claim you ate 0 calories or lost progress just because data is
  absent. Requires `GEMINI_API_KEY`.
- `/overview <equipment> [user]` — consistency overview for one lift: logs,
  active weeks, streak, spacing, current weight, and best.
- `/checkin [user]` — copy/paste stat template prefilled with current bests.
- `/stale [user] [days]` — lifts that haven't been updated recently.
- `/progress <equipment> [user]` — best lift per calendar month, with deltas.
- `/graph <equipment> [user]` — PNG chart of daily-best weight over time with a
  running-best reference line.
- `/history <equipment> [user]` — per-entry timeline for one user.
- `/recent [user] [limit]` — most recent entries across all equipment.
- `/leaderboard <equipment>` — top 25 of **this server's members** for that
  lift. Lifts are global per user, so each member is ranked by their all-time
  best across every server, but the board itself only lists this community.
- `/machine <equipment>` — everyone's timeline on one lift.
- `/compare <user> [equipment]` — head-to-head PRs with a win tally.
- `/serverstats` — server-wide totals, top lifters, and popular equipment.
- `/daily_update [days_ago]` — post a daily server recap (PRs, active lifters,
  popular lifts).

Goals:

- `/goal_set <equipment> <target_kg> [bodyweight]` — set a personal goal.
- `/goals [user]` — show active goals with percentage progress bars.
- `/goal_remove <equipment>` — remove one of your goals.

When a logged lift reaches a goal, the bot celebrates with 🎯 in its reply and
clears the goal automatically.

Lifts, PRs and goals are **global per user** — everything you log follows you
across every server the bot is in (and DMs): your bests, history, tonnage,
streaks, `/progress`, `/coach`, goals and `/stats` all aggregate every server.
Only the *social* surfaces stay per-community: `/leaderboard`, `/machine` and
`/serverstats` show this server's members/activity (ranked by global bests).
Editing/cleanup of **your own** data is global too (`/change_weight`,
`/rename scope:mine`, `/delete_entry`), while guild-wide admin ops
(`/rename scope:all`, `/purge`) deliberately stay scoped to the current server.

Calories:

- `/calories setup <target>` — set your daily intake target. Accepts kcal or
  kJ (`2500`, `2500c`, `8700kj` — kJ is converted at 4.184 kJ/kcal).
- `/calories add <amount> [note]` — log something you ate, again in kcal or
  kJ. The reply shows a progress bar against your daily target.
- Or just type it in chat: a message that is **only** an amount — `650kcal`,
  `200c`, or `2700kj` — gets logged automatically, the bot reacts ✅ and
  replies with your running total. The message must be nothing but the amount
  (so "1500cal is crazy work" is ignored); add a description with
  `/calories add` instead. `@user 650kcal` logs it for someone else.
  Chat calorie posts are also picked up by the startup/`/backfill` history
  scan (deduped per message, dated to the original post), so messages sent
  while the bot was offline — or before you set a target — get imported once
  you've run `/calories setup`.
- **Backdating:** add a day to a chat log to file it under that day —
  `650kcal yesterday`, `200c monday`, `coffee yesterday`, `40p 3 days ago`, or
  an ISO date (`500c 2026-06-28`). Works for calorie, protein, saved-food and
  combined posts; the reply notes the day it landed on. The slash commands take
  it too: `/calories add 650 day:yesterday`, `/protein add 40 day:monday`.
- `/calories today [user]` — today's entries and total vs target.
- `/calories week [user]` — per-day totals for the last 7 days (with your 🔥
  logging streak).
- `/calories leaderboard` — ranks the server's trackers by current logging
  streak.
- `/calories edit <amount> [note]` — fix the amount of your most recent entry.
- `/calories undo` — remove your most recent entry. To remove a *specific*
  entry instead, react ❌ on the bot's `🍽️ +N cal` reply for it (the logger,
  the target, or an admin can do this).
- **Edit to fix:** editing the original chat message updates the stored entry —
  e.g. correcting `1730c` to `1730kj` recomputes the calories; deleting the
  amount removes it.
- **Streaks:** log on consecutive days and the bot shows a 🔥 streak on replies
  and in `/calories week`; it stays alive until a whole day passes unlogged.
- `/calories stop` — stop tracking (history is kept; `setup` re-enables).

Calorie tracking is **global per user** — your daily goal, saved foods and
logged entries follow you across every server the bot is in, and in DMs. (Same
for protein and bodyweight.)

Saved foods (personal name → calorie shortcuts):

- `/calories food_set <name> <amount> [protein]` — save a food, e.g.
  `/calories food_set coffee 5` or `/calories food_set "protein shake" 250kj 30`.
  The optional **protein** (grams) is logged automatically whenever you log the
  food — handy for shakes, chicken, etc. Re-running `food_set` with just a new
  calorie amount updates the calories and **keeps** the saved protein.
- Then log it by **just typing the name** in chat — `coffee`, or `2 coffee` /
  `coffee x2` for multiple servings — and the bot reacts ✅. If the food has a
  protein value and you're protein-tracking, it logs both at once (one ❌ undoes
  both). `/calories add coffee` works too.
- `/calories food_list` — show your saved foods (with protein where set).
- `/calories food_remove <name>` — delete one.

Saved foods are per-user, so your `coffee` and someone else's can be different
amounts. Chat shortcuts only fire on an exact full-message match of a food
you've defined (optionally with a serving count), so normal chatter is never
mistaken for food. They're matched live only — unlike `650kcal`-style posts,
plain food words aren't picked up by the history backfill.

Everyone with a calorie target gets a personal AI summary in the weekly
report (see below).

### Protein (optional daily max)

A lightweight, separate tracker for keeping protein **under** a daily ceiling
(it flags when you go over, rather than nudging you to hit a goal).

- `/protein setup <grams>` — set your daily max, e.g. `180`.
- `/protein add <grams> [note]` — log protein, e.g. `/protein add 40 chicken`.
- Or just type **`40p`** / **`40g protein`** / **`protein 40`** in chat — the
  bot reacts ✅ and replies with your running total vs your max (with a ⚠️ once
  you're over). An explicit `p`/`protein` marker is required, so a bare number
  or a `40kg` lift is never mistaken for protein.
- `/protein today [user]` · `/protein week [user]` — totals vs your max (with
  your 🔥 logging streak).
- `/protein edit <grams> [note]` — fix the amount of your most recent entry.
- `/protein undo` — remove your most recent entry, or react ❌ on the bot's
  reply to remove that specific one (the logger, the target, or an admin).
- `/protein stop` — stop tracking (history kept; `setup` re-enables).

**Log both at once:** a message with both amounts — e.g. `500c and 40p` (also
`40p 500c`, `2700kj and 40g protein`) — records the calorie *and* the protein
entry together and replies with both totals. Only what you're tracking is
logged; the rest is skipped with a note. React ❌ on the reply to remove both.

Logging & editing:

- `/log <equipment> <weight_kg> [user] [bodyweight]` — manual entry, optionally
  for another user (🎉 on PRs).
- `/bodyweight [weight_kg] [user]` — record (or view) your current bodyweight.
  You can also just post `bodyweight 100kg` (or `body weight: 100kg`, or
  `bw 80`) in chat, and `@user bodyweight 100kg` to set someone else's.
  Once on file, the bot annotates bodyweight-relative lifts with the **true
  load** in inline replies and the leaderboard:
  - assisted pull-up logged as `pull ups 70kg` with bodyweight 100kg →
    **30kg actual** (machine assistance is subtracted)
  - weighted dip logged as `BW+20kg` with bodyweight 100kg →
    **120kg actual**
- `/undo` — remove your most recent entry.
- **React ❌ on the bot's reply** to undo the specific lifts that reply stored.
  The logger and target lifter can do this; other users' reactions are ignored.
- `/parse <message_id>` — re-parse a specific message in the current channel.
- `/delete_entry <equipment> <date>` — remove one day's entries.
- `/change_weight <equipment> <weight_kg> [user] [date]` — change the latest
  matching entry's weight for you or another user.
- `/swap_weights <first_equipment> <second_equipment> [user] [date]` — swap
  weights between two latest matching entries (for example, leg curl ↔ leg
  extension when a post mixed them up).

Discovery & utilities:

- `/equipment_list` — every canonical equipment name the bot knows.
- `/aliases <equipment>` — all spellings the parser accepts for a lift
  (including any custom aliases configured in this server).
- `/export [user]` — download lifts as a CSV attachment.
- `/ping` · `/version`

Maintenance (available to everyone):

- `/backfill [limit]` — rescan this channel's history.
- `/rename <old> <new>` — merge one equipment name into another. Defaults to
  *your* rows and applies **globally** (every server); `scope:all` renames the
  whole server's rows (and repoints its aliases) and stays scoped to it.
- `/purge <equipment>` — delete every row for a lift name in **this server**
  (guild-wide admin op; doesn't reach other servers).
- `/alias_add <phrase> <equipment>` — teach the bot a server-specific alias
  (e.g. "hack sled" → "leg press"). Custom aliases apply to both
  slash-command inputs and auto-parsed chat messages.
- `/alias_remove <phrase>` · `/alias_list`

Auto-parsing also celebrates PRs: when a stored lift beats your previous best
for that equipment, the bot's reply tags it with 🎉 and shows the old → new
weight. PRs are **all-time across every server** (lifts are global per user), so
beating a best you set elsewhere still counts. Duplicate posts (same
`message_id` + equipment) get a 🔁 reaction instead of a second ✅ so nothing is
double-counted.

Long stat dumps are compacted in bot replies after `PARSE_REPLY_MAX_ITEMS`
rows so check-ins don't flood the channel. The entries are still stored; the
reply just hides the tail.

Presence & sleep (requires `ENABLE_PRESENCE_TRACKING=true`):

- `/track start <user>` · `/track stop <user>` — (owner) begin/stop recording a
  member's online/offline transitions.
- `/track schedule <user> [days]` — online/offline summary, per-weekday and
  per-hour heatmap, and an estimated sleep window.
- `/track raw <user> [days]` — raw status/activity timeline.
- `/track export <user> [days] [fmt]` — (owner) DM yourself a member's derived
  sleep data. `fmt=csv` gives a nightly sleep table (bedtime, wake, hours);
  `fmt=json` gives the full raw presence dump plus sessions.
- `/track analyze <user> [days]` — send the derived sleep sessions to
  Google's Gemini API and get back plain-language trends (anyone can run it on
  a tracked member). Requires
  `GEMINI_API_KEY` (see `.env.example`); `GEMINI_MODEL` defaults to
  `gemini-2.5-flash`.

Sleep data here is *inferred* from Discord presence (long offline stretches),
not a real sleep tracker — treat it as an approximation.

## Weekly check-in reminder

Set `REMINDER_CHANNEL_ID` in `.env` to have the bot post a weekly prompt asking
everyone to drop their current bests. Defaults are **Wednesday 12:00** in
`DISPLAY_TIMEZONE` (defaults to `Australia/Adelaide`, which handles ACST/ACDT
automatically). Tune with `REMINDER_WEEKDAY` (0=Mon … 6=Sun), `REMINDER_HOUR`,
`REMINDER_MINUTE`, and optionally `REMINDER_ROLE_ID` to @mention a role.

## Weekly bodyweight reminder

A second weekly reminder nudges everyone to update their bodyweight via
`/bodyweight` so true-load annotations on pull-ups, dips, etc. stay accurate.
By default it inherits `REMINDER_CHANNEL_ID` and posts **Monday 07:30** in
`DISPLAY_TIMEZONE`. Tune with `BODYWEIGHT_REMINDER_CHANNEL_ID`,
`BODYWEIGHT_REMINDER_WEEKDAY`, `BODYWEIGHT_REMINDER_HOUR`,
`BODYWEIGHT_REMINDER_MINUTE`, and `BODYWEIGHT_REMINDER_ROLE_ID`.

## Daily gym update

Set `DAILY_UPDATE_CHANNEL_ID` in `.env` to have the bot post a daily recap of
yesterday's activity: total lifts, active lifters, popular lifts, and PRs.
Defaults are **08:00** in `DISPLAY_TIMEZONE`. Tune with `DAILY_UPDATE_HOUR` and
`DAILY_UPDATE_MINUTE`. Empty days are skipped by default; set
`DAILY_UPDATE_POST_EMPTY=true` if you want a quiet "no lifts" update too.

## Weekly report (Sunday)

Every Sunday evening the bot posts a 7-day recap: total lifts, PRs, most
active members, popular lifts — plus a **🍎 weekly calorie check-in** with a
short Gemini-written summary for each member tracking via `/calories`
(adherence to target, consistency, one encouraging note) and a **🥩 weekly
protein check-in** (days logged, average vs max, and how often they went over).
Without `GEMINI_API_KEY` the calorie section falls back to plain stats lines;
the protein section is always plain stats.

The channel comes from `WEEKLY_REPORT_CHANNEL_ID`, falling back to
`DAILY_UPDATE_CHANNEL_ID` then `REMINDER_CHANNEL_ID`. Defaults are **Sunday
18:00** in `DISPLAY_TIMEZONE`; tune with `WEEKLY_REPORT_WEEKDAY`,
`WEEKLY_REPORT_HOUR`, and `WEEKLY_REPORT_MINUTE`. `/weekly_report` posts the
same report on demand.

## Strava workout feed

Link members' Strava accounts and the bot posts an embed to
`STRAVA_FEED_CHANNEL_ID` the moment they finish an activity (run, ride, lift,
etc.) — distance, time, pace, heart rate, and a link back to Strava. It uses
OAuth2 + Strava's real-time webhook push, so the bot runs a small web server
that must be reachable over public HTTPS.

Quick version: register a Strava API app, set `STRAVA_CLIENT_ID`,
`STRAVA_CLIENT_SECRET`, `STRAVA_PUBLIC_URL` and `STRAVA_FEED_CHANNEL_ID`, run
`/strava_subscribe` once (owner), then members run `/strava_link`. Full
walkthrough — including the reverse-proxy setup — is in
[docs/STRAVA.md](docs/STRAVA.md). Set `STRAVA_DISABLED=1` to turn it off.

Commands: `/strava_link`, `/strava_unlink`, `/strava_status`, `/strava_latest`
(show the most recent activity on demand), and owner-only `/strava_subscribe`,
`/strava_subscription`, `/strava_unsubscribe`.

## Direct messages

You can run **any** command in a DM with the bot, not just in a server channel.
Because a DM has no server attached, the bot has to work out *which* server's
data to use:

- If you share exactly **one** server with the bot, it's used automatically.
- If you share **several**, run `/server` once to pick a default (it has
  autocomplete). Change it any time with `/server` again.
- If the bot can't tell, it asks you to set one with `/server`.

**Privacy:** you can only look up another user's info if you **share a server**
with them. Looking up someone you don't share a server with is refused — this
holds in DMs and in servers alike.

For DM commands to appear, the app must be enabled as a **User Install**
integration in the Discord Developer Portal (Installation → enable *User
Install*), and each person re-authorises the app to their account. Guild-only
installs keep working in servers exactly as before.

## Web dashboard

An authenticated operator dashboard (separate web server, default port `8081`)
for browsing and editing everything the bot tracks without touching Discord:

- **Overview** — server totals and the most recent activity.
- **Members** — searchable list with avatars; drill into one member for their
  lift/nutrition counters, **today's calories & protein vs goal** (progress
  bars), a **bodyweight trend chart**, **lift goals**, their **saved foods**
  (add/edit/delete, with protein), roles, linked Strava/Revo, and history.
  You can **grant or remove roles** on a member (the ✕ on a role chip, or the
  **+ Add role** picker), **remove a timeout** (the Moderation box shows an
  active timeout and a **Remove timeout** button when the bot can act), and
  **invite a user by ID** (➕ Invite by ID): the bot mints a one-use invite and
  DMs it to them, falling back to a copyable link if their DMs are closed. All
  three are written to the audit log. They need the bot to have **Manage Roles**
  / **Moderate Members** / **Create Invite** in the target server, and Discord
  still enforces role hierarchy (it can't assign a role above its own top role,
  or moderate someone who outranks it).
- **Roles** — each role with its colour and live member list.
- **Messages** — a per-channel chat log of **everything** members post: full
  text, and a **permanent local copy of every attachment** (images, videos, GIFs
  and any other uploaded file) so they survive Discord's expiring CDN links and
  message deletion. Edited messages show their latest text (tagged *edited*) and
  deleted ones are flagged 🗑️ rather than vanishing. Controlled by
  `ENABLE_MESSAGE_LOGGING` / `ENABLE_MEDIA_DOWNLOAD` (see `.env.example`).
- **Leaderboard** — pick an exercise and see the ranked best lifts (with
  medals + avatars).
- **Audit log** — a unified, filterable, paged trail of role changes (including
  *who* made them), member join/leave/nickname/username changes and kicks/bans,
  and every data event: logs, **undos/reverts** (with who triggered them),
  goals, bodyweight, saved foods, and dashboard edits.
- **Lifts / Calories / Protein** — the raw entries, searchable, with inline
  delete and (for lifts) edit. Every change is written to the audit log.

Profile pictures appear throughout, and the whole thing is a modern dark UI.

**Auto un-timeout** (`AUTO_UNTIMEOUT=true`, on by default): the bot immediately
clears any timeout placed on a member, recording each removal in the audit log.
It needs **Moderate Members** and a role above the target (Discord enforces both),
and never acts on the guild owner or anyone it doesn't outrank.

It is **off until you set `WEBUI_PASSWORD`**. Enabling it also turns on the
privileged **Server Members** intent, which you must additionally toggle on for
the bot in the Discord Developer Portal. Full setup is in
[docs/WEBUI.md](docs/WEBUI.md).

## Setup

1. Create a Discord application + bot at <https://discord.com/developers/applications>.
   Enable the **Message Content Intent** on the Bot page.
2. Copy the bot token.
3. Invite the bot to your server with scopes `bot` + `applications.commands`
   and at minimum these permissions: View Channels, Send Messages,
   Read Message History, Add Reactions, Use Slash Commands. To use the
   dashboard's **role grants** add **Manage Roles**, for **remove timeout** add
   **Moderate Members**, and for **invite by ID** add **Create Invite**. To let
   commands run in DMs, enable the **User Install**
   integration under *Installation* in the Developer Portal (see
   [Direct messages](#direct-messages)).
4. Clone this repo on your server and create `.env` from the template:

   ```bash
   cp .env.example .env
   # edit .env and paste DISCORD_TOKEN
   ```

5. (Optional) set `GYM_CHANNEL_IDS` to a comma-separated list of channel IDs
   so the bot only auto-parses the gym channel(s).
6. Run it:

   ```bash
   docker compose up -d --build
   ```

Data lives in the `gym-data` Docker volume at `/data/gym.sqlite3` — back it up
if you care about your PRs.

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate      # PowerShell
pip install -r requirements.txt
copy .env.example .env      # edit token
python -m app.bot
```

Set `GUILD_ID` in `.env` during development so slash commands register
instantly on your test server instead of waiting for the global rollout.

## Notes on numbers

Different gyms calibrate cable machines differently, so the weight numbers
aren't directly comparable between people on pulley exercises. The bot just
records what you post — take cross-gym leaderboards with a pinch of salt.
