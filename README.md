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
```

Rules it uses:

- `equipment: value` (or bare `equipment value` when a `kg` / `plates` unit is
  present) is treated as a lift.
- `Xkg` / `X kg` -> X kg.
- `N plates` -> N × 20 kg (configurable via `PLATE_KG` in `app/parser.py`).
- `BW+Xkg` -> X kg recorded with the bodyweight-add flag.
- `A - B kg` range -> the higher number (treated as top working weight).
- Many aliases are unified (e.g. `pec dec`, `pec fly`, `chest fly`, `pekdek`
  all become `pec dec`). See `app/aliases.py`.

The bot reacts with ✅ on messages whose lifts were stored, and de-duplicates
per `(message_id, equipment)` so edits / re-parses won't double-count.

## Slash commands

- `/stats [user]` — personal bests for a user.
- `/progress <equipment> [user]` — best lift per calendar month, with deltas.
- `/leaderboard <equipment>` — top 25 in the server for that lift.
- `/log <equipment> <weight_kg> [bodyweight]` — manual entry.
- `/parse <message_id>` — re-parse a specific message in the current channel.

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
