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

Stats & progress:

- `/help` — in-bot command reference.
- `/stats [user]` — personal bests for a user.
- `/summary [user]` — profile overview (totals, top PRs, most trained, gains,
  current weekly streak).
- `/overview <equipment> [user]` — consistency overview for one lift: logs,
  active weeks, streak, spacing, current weight, and best.
- `/checkin [user]` — copy/paste stat template prefilled with current bests.
- `/stale [user] [days]` — lifts that haven't been updated recently.
- `/progress <equipment> [user]` — best lift per calendar month, with deltas.
- `/graph <equipment> [user]` — PNG chart of daily-best weight over time with a
  running-best reference line.
- `/history <equipment> [user]` — per-entry timeline for one user.
- `/recent [user] [limit]` — most recent entries across all equipment.
- `/leaderboard <equipment>` — top 25 in the server for that lift.
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
- `/calories today [user]` — today's entries and total vs target.
- `/calories week [user]` — per-day totals for the last 7 days.
- `/calories undo` — remove your most recent entry.
- `/calories stop` — stop tracking (history is kept; `setup` re-enables).

Saved foods (personal name → calorie shortcuts):

- `/calories food_set <name> <amount>` — save a food, e.g.
  `/calories food_set coffee 5` or `/calories food_set "protein shake" 250kj`.
- Then log it by **just typing the name** in chat — `coffee`, or `2 coffee` /
  `coffee x2` for multiple servings — and the bot reacts ✅. `/calories add
  coffee` works too.
- `/calories food_list` — show your saved foods.
- `/calories food_remove <name>` — delete one.

Saved foods are per-user, so your `coffee` and someone else's can be different
amounts. Chat shortcuts only fire on an exact full-message match of a food
you've defined (optionally with a serving count), so normal chatter is never
mistaken for food. They're matched live only — unlike `650kcal`-style posts,
plain food words aren't picked up by the history backfill.

Everyone with a calorie target gets a personal AI summary in the weekly
report (see below).

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
- `/rename <old> <new>` — merge one equipment name into another.
- `/purge <equipment>` — delete every row for a lift name.
- `/alias_add <phrase> <equipment>` — teach the bot a server-specific alias
  (e.g. "hack sled" → "leg press"). Custom aliases apply to both
  slash-command inputs and auto-parsed chat messages.
- `/alias_remove <phrase>` · `/alias_list`

Auto-parsing also celebrates PRs: when a stored lift beats your previous best
for that equipment, the bot's reply tags it with 🎉 and shows the old → new
weight. Duplicate posts (same `message_id` + equipment) get a 🔁 reaction
instead of a second ✅ so nothing is double-counted.

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
(adherence to target, consistency, one encouraging note). Without
`GEMINI_API_KEY` the calorie section falls back to plain stats lines.

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

## Setup

1. Create a Discord application + bot at <https://discord.com/developers/applications>.
   Enable the **Message Content Intent** on the Bot page.
2. Copy the bot token.
3. Invite the bot to your server with scopes `bot` + `applications.commands`
   and at minimum these permissions: View Channels, Send Messages,
   Read Message History, Add Reactions, Use Slash Commands.
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
