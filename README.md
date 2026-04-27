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
- `/checkin [user]` — copy/paste stat template prefilled with current bests.
- `/stale [user] [days]` — lifts that haven't been updated recently.
- `/progress <equipment> [user]` — best lift per calendar month, with deltas.
- `/graph <equipment> [user]` — PNG chart of weight over time with a
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

Logging & editing:

- `/log <equipment> <weight_kg> [user] [bodyweight]` — manual entry, optionally
  for another user (🎉 on PRs).
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

## Weekly check-in reminder

Set `REMINDER_CHANNEL_ID` in `.env` to have the bot post a weekly prompt asking
everyone to drop their current bests. Defaults are **Wednesday 12:00** in
`DISPLAY_TIMEZONE` (defaults to `Australia/Adelaide`, which handles ACST/ACDT
automatically). Tune with `REMINDER_WEEKDAY` (0=Mon … 6=Sun), `REMINDER_HOUR`,
`REMINDER_MINUTE`, and optionally `REMINDER_ROLE_ID` to @mention a role.

## Daily gym update

Set `DAILY_UPDATE_CHANNEL_ID` in `.env` to have the bot post a daily recap of
yesterday's activity: total lifts, active lifters, popular lifts, and PRs.
Defaults are **08:00** in `DISPLAY_TIMEZONE`. Tune with `DAILY_UPDATE_HOUR` and
`DAILY_UPDATE_MINUTE`. Empty days are skipped by default; set
`DAILY_UPDATE_POST_EMPTY=true` if you want a quiet "no lifts" update too.

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
