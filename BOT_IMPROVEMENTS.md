# Gym Discord Bot — Improvement Analysis & Roadmap

*Generated 2026-07-15 from a multi-agent audit of the codebase (`app/`, ~29k LOC), the SQLite dump (`gym-…T110645Z.sqlite3`, 37 tables), and the `#gym-and-strava` transcript (663 human messages, Apr 17 – Jun 2 2026). Findings are grounded in queries against the dump, quotes from the transcript, and `file:line` references into the source.*

---

## 1. Executive summary — what the data actually says

The bot is **not dying, it's migrating.** The story in the numbers is a hand-off from a social lift tracker to a personal nutrition tracker:

- **Manual lift logging has collapsed.** Distinct lifters per week fell from **6 (May) to 1 (July)**. Five of six lifters' last manual lift was in May (Dumbaay 05-04, Jaidyn 05-08, Dos 05-12, Cookie Monster 05-25, musk 06-26). The `lifts` table only looks alive because **63% of its rows (591/935) are one user's Hevy auto-imports.**
- **Nutrition became the sticky feature.** Calorie logging launched mid-June and immediately ran at **57 → 217 → 169 → 134 entries/week** across 5 users, with two *live perfect streaks* (Poshy 27 days, Cookie Monster 24 days). In the 3-week `audit_log`, `calorie_add` fired **463 times vs `lift_add` 128** — the calorie path is now the bot's hot surface, by 3.6×.
- **The channel went quiet.** 186 messages on the May-8 feature-launch day; **zero human messages Jun 3 – Jul 14.** Engagement historically spiked *only when the bot shipped or posted something* — which makes the bot's own proactive posts the single proven traffic lever, and it currently has no monthly recap and a daily recap that silently skips itself on no-lift days.

**The three biggest opportunities:**

1. **Feed the loop that works.** Nutrition + streaks are carrying the bot; the mechanics that would amplify them (public streak milestones, streak-loss protection, nutrition in the daily post, a "trained-but-didn't-log" nudge from Revo attendance) are either absent or built-but-undiscovered. These are mostly *small* changes.
2. **Convert dead telemetry into content.** `message_log` (11.7k rows), `presence_events` (4.5k), `voice_events`, `activity_events` and Revo streak calendars are pure write-only data no feature reads. A monthly/weekly "Wrapped" recap is the founding promise ("*ill make a discord bot to track everyones month by month*") and the proven re-engagement format.
3. **Fix the trust + friction gaps that killed lift logging.** Silent parse failures (retry-until-it-sticks loops), no `discord.ui` buttons anywhere, mis-attributed proxy logs, and destructive commands with no admin gate or audit trail. The calorie path gives instant feedback and thrives; the lift path fails silently and churned.

### Adopt-don't-build: features that already exist and just need surfacing

Several high-value "features" are **already fully built and simply undiscovered or unenriched** — cheaper wins than anything net-new:

| Already shipped | State | Fix |
|---|---|---|
| `/calories remind` streak-saver (`streak_saver_loop`, [bot.py:2638](app/bot.py#L2638)) | Runs every 15 min over an **empty** `calorie_reminder_prefs` table (0 rows) — it's the 21st of 21 subcommands, never surfaced | Offer it with a button at the moment a streak breaks |
| Revo raffle-deadline push | Designed in `docs/REVO_PORTAL.md §5`, marked *"Still open (push)"*, **unbuilt** | ~40 lines reusing the existing poll loop |
| `_suggest_equipment` fuzzy matcher ([bot.py:1310](app/bot.py#L1310)) | Exists, called **only** from `/purge` and `/rename` — never on a failed chat parse | Wire it into the on_message lift fall-through |
| Meals, lift goals, bodyweight goals | Shipped, **0 rows each** | Surface contextually or retire |

---

## 2. Themes

1. **Nutrition + streaks are the engine now — build on the winner.** Every cheap engagement mechanic should attach to the calorie reply, the streak counter, or the daily/weekly post, because that's where the users already are (463 interactions in 3 weeks).
2. **Proactive posts are the only proven traffic driver.** The channel responds to bot *events*, not to commands people must remember to run. Scheduled recaps, nudges, and milestone posts are worth more here than new pull-commands.
3. **Silent failure is the enemy.** The calorie path succeeds because it always replies; the lift path churned partly because an unknown machine name produces *nothing*. Feedback-on-failure and one-tap fixes are the biggest lift-revival lever.
4. **This is a 5–8 person banter server, not a 1000-member community.** 2-person duels, "we hit it together" server goals, and cheeky named badges fit; sprawling XP ladders and generic big-server gamification would flop. Jaidyn's 591-row import already makes the all-time leaderboard demoralizingly frozen.
5. **Trust and consent are load-bearing.** The worst historical incident was mis-attributed ghost entries ("*Infact almost all of those are wrong*"), and a member has objected on record to tracking ("*Can you remove the bot tracking people wtf*"). Attribution correctness, audit trails, and opt-in nudges aren't optional polish here.

---

## 3. Prioritized roadmap

Effort: **S** ≤ ~1 day · **M** ~2–5 days · **L** > 1 week. Priority is re-balanced globally for *this* community.

### 🟢 Quick Wins (1–3 days each)

---

#### QW-1 · Admin-gate & audit the destructive commands (`/purge`, `/rename all`, cross-user `/delete_entry`)
- **Priority:** High · **Effort:** S · **Security**
- **Problem:** `/purge` deletes every guild-wide row for an equipment name gated only by `confirm:True` — no `ADMIN_USER_IDS` check ([bot.py:5632-5674](app/bot.py#L5632)). `/rename` can rewrite another member's rows (`user=`) or the whole guild (`scope=all`). Neither `db.delete_equipment` ([db.py:1229](app/db.py#L1229)) nor `rename_equipment` writes an audit row — the 799-row `audit_log` contains **zero** purge/rename actions. A wipe of all 935 lifts is currently silent and unattributable.
- **Why valuable:** In a banter server the real threat isn't a hacker, it's a mate nuking someone's PRs for laughs — today that's unrecoverable except from backups and leaves no trace of who did it.
- **Implementation:** Reuse the admin-gate pattern already in `cleanup_resurrected` ([bot.py:5265](app/bot.py#L5265)) and `suppress_message` ([bot.py:5401](app/bot.py#L5401)). Add `_audit(...)` calls inside `delete_equipment`/`rename_equipment`/`delete_entry` using the `actor_id/actor_name` pattern that `set_*` functions already use ([db.py:1811+](app/db.py#L1811)). Self-scoped `/rename` stays open so normal typo-fixing is unaffected.
- **Impact:** Closes the only unaudited destructive surfaces; every mutation becomes attributable in the dashboard Audit tab. *(Platform lens — verified novel & grounded.)*

#### QW-2 · Fix proxy-log audit attribution (+ optional per-user proxy opt-out)
- **Priority:** High · **Effort:** S · **Security**
- **Problem:** Anyone can log for anyone via `@mention`/nickname ([bot.py:3744-3752](app/bot.py#L3744)). `db.add_lifts` audits `actor_id=user_id` — the **target's** id ([db.py:1070](app/db.py#L1070)) — so a troll posting a fake lift for a victim produces an audit row saying *the victim* did it. Same in `calorie_add`.
- **Why valuable:** Ghost/mis-attributed entries were the #1 trust complaint that nearly killed passive parsing ("*You hit 295 kg on leg press and I swear we did 90*"). Proxy logging is also *heavily used legitimately* (Poshy logs ~20 mention-prefixed lifts/session for friends) — so keep the feature, just make it honest.
- **Implementation:** Thread `message.author` into `add_lifts`/`calorie_add`/bodyweight writes as `actor_id/actor_name` (params already exist on `set_*`). Optionally add a `no_proxy` opt-out flag on `user_dm_prefs`, checked next to the existing blacklist check at [bot.py:3755](app/bot.py#L3755).
- **Impact:** Audit trail becomes forensically correct for the ~30% of entries logged on someone's behalf; troll cleanup becomes confident. *(Platform lens — verified.)*

#### QW-3 · Put nutrition in the daily update & stop it silently skipping
- **Priority:** High · **Effort:** S
- **Problem:** `_daily_update_text` returns `None` whenever `total_lifts == 0` ([bot.py:2766-2768](app/bot.py#L2766)) — verified. With manual lifting dead, the bot's only daily automated touchpoint is skipped most days, while the thing people *do* every day (609 calorie entries, 5-6 users/day) never appears in it.
- **Why valuable:** The daily post is the cheapest recurring surface the bot owns, and streaks are the proven hook. Making it reflect real behavior turns a dead loop into a daily streak-leaderboard moment.
- **Implementation:** Extend `_daily_update_text` with a "🔥 Nutrition" block from existing primitives (`db.calorie_total_between` per user, effective target via `app/targets.py`, `_calorie_streak`, protein vs `protein_goals`, yesterday's Revo check-ins). Only return `None` when **both** lifts and nutrition are empty. `/daily_update` shares the helper and inherits the fix free.
- **Impact:** Converts most currently-skipped days into posted recaps; daily public streak visibility. *(Automation lens.)*

#### QW-4 · Public nutrition streak milestones (mirror the Revo pattern)
- **Priority:** High · **Effort:** S
- **Problem:** Milestone celebrations fire **only** for Revo attendance (`revo_client.streak_milestone`, [bot.py:9506](app/bot.py#L9506)) — verified. Calorie/protein streaks show only as a private `🔥 N` suffix on the logger's own reply, even though they're the highest-frequency behavior in the server.
- **Why valuable:** Public milestones at 7/14/21/30/50/100 days give a social payoff on the single most-performed action, and fuel a rivalry that already exists (Cookie Monster is exactly 3 days behind Poshy — a race the bot never mentions).
- **Implementation:** Add a day-based sibling of `streak_milestone` next to `_logging_streak` ([bot.py:738](app/bot.py#L738)); upgrade the calorie/protein reply to a channel-visible "🎉 30-day streak!" line when a threshold is crossed; track last-celebrated milestone per user via `app_meta` to avoid repeats.
- **Impact:** Near-free dopamine loop on an action performed ~20×/day server-wide. *(Engagement lens.)*

#### QW-5 · Surface the streak-saver at the moment it's needed (+ 1 grace day)
- **Priority:** High · **Effort:** S
- **Problem:** `/calories remind` and `streak_saver_loop` are fully built ([bot.py:2638](app/bot.py#L2638)) but `calorie_reminder_prefs` has **0 rows** — it's the 21st of 21 subcommands. Meanwhile the users who need it keep breaking streaks (musk max gap 7d / current 0; Dos 13→2; Jaidyn 12→2). The streak counter is also strictly consecutive, so one missed day erases weeks.
- **Why valuable:** The fix is *discovery, not machinery*. Loss-aversion protection is what keeps mid-tier streakers playing instead of quitting after one break.
- **Implementation:** (a) When a user logs after a gap that broke a ≥5-day streak, append a one-time nudge with a `discord.ui.Button` "Protect this streak — evening reminder" that writes `calorie_reminder_prefs` directly (one tap vs discovering a subcommand). (b) Extend `_logging_streak` to tolerate 1 grace day per rolling 7, rendered as 🧊 in `/calories week` — a pure change slotting into the existing 57 calorie tests. Dedup the prompt via `app_meta`.
- **Impact:** Retains the 3–4 users whose streaks keep dying; finally populates a zero-row table and gives `streak_saver_loop` something to do. *(Engagement + UX + Automation lenses — converged.)*

#### QW-6 · Enrich `/coach` with Revo attendance, Strava & sleep
- **Priority:** High · **Effort:** S
- **Problem:** `_build_progress_payload` ([bot.py:13199-13297](app/bot.py#L13199)) sends Gemini only lifts, nutrition and bodyweight. The bot already holds Revo check-in ground truth (5 accounts, streaks to 15 wks), 3 Strava athletes, and nightly sleep for 4 tracked users — so `/coach` literally can't see that someone trains 4×/week but logs nothing.
- **Why valuable:** The coach's core weakness is conflating "didn't train" with "didn't log" — its own prompt fights this. Real attendance data resolves it: *"you checked in at Revo 12× this month but logged 2 sessions."*
- **Implementation:** Pure delta on existing code — add `attendance` (from `revo_account` + the streak calendar `/revo_calendar` already fetches), `cardio` (reuse the weekly-report Strava recap helper), and `sleep` (`presence.nightly_sleep_sessions`/`sleep_stats`, [presence.py:350](app/presence.py#L350)) blocks to the payload, gated on tracking membership. `thinking_budget` already set — no prompt plumbing.
- **Impact:** Materially better `/coach` for all 5 Revo-linked members at ~1-2k extra tokens/call. *(AI lens.)*

#### QW-7 · Off-box backup replication + snapshot integrity check
- **Priority:** High · **Effort:** S · **Ops**
- **Problem:** Nightly snapshots (`BACKUP_KEEP=14`, [bot.py:244-256](app/bot.py#L244)) land in the **same** Docker volume as the live DB — `docker-compose.yml`'s own comment admits it needs off-boxing. One volume failure loses the live DB *and* all 14 backups: 935 lifts, 252 days of history, 5 members' Fernet-encrypted Revo passwords. Snapshots are never verified restorable.
- **Why valuable:** Users abandoned their phone notes for the bot; this history exists nowhere else. Total-loss protection is the highest-impact ops fix for one evening's work.
- **Implementation:** Bind-mount `./backups:/data/backups` (comment already spells it out) + an `rclone`/`restic` off-box sync; extend `db_backup` ([bot.py:2713](app/bot.py#L2713)) to open each snapshot read-only and run `PRAGMA integrity_check` + a row-count sanity check, logging failures loudly. Document that `.env` (the Fernet keys) must be backed up separately or credentials are unrecoverable.
- **Impact:** Converts "same-disk copies" into real disaster recovery; corruption caught the night it happens. *(Platform lens — verified.)*

#### QW-8 · Add the four missing indexes
- **Priority:** Medium · **Effort:** S · **Perf**
- **Problem:** Every index on the nutrition/bodyweight/message tables leads with `guild_id`, but nutrition/bodyweight are **global-per-user** (stored under guild 0, queried `WHERE user_id` only). `EXPLAIN QUERY PLAN` on the dump shows `SCAN calorie_entries + TEMP B-TREE`, `SCAN bodyweights`, and the dashboard channel view temp-sorting all 11.7k message rows per page load.
- **Why valuable:** Calorie queries run on every one of the 463 adds/3wk plus the 15-min streak-saver loop — the hottest paths. Fix = three `CREATE INDEX` lines.
- **Implementation:** Append to the `SCHEMA` block: `calorie_entries(user_id, logged_at)`, `bodyweights(user_id, recorded_at)`, `message_log(guild_id, channel_id, at)`, and `protein_entries(user_id, logged_at)`. `CREATE INDEX IF NOT EXISTS` self-applies via the existing `executescript(SCHEMA)` — no migration entry needed.
- **Impact:** Removes full scans + temp sorts on the most frequent queries; future-proofs toward 96k message rows/yr. *(Platform lens — verified; today's tables are small, so this is future-proofing.)*

#### QW-9 · Complete `.env.example` & remove the hardcoded admin default
- **Priority:** Medium · **Effort:** S · **Security/Ops**
- **Problem:** `ADMIN_USER_IDS` defaults to the repo owner's Discord ID ([bot.py:176](app/bot.py#L176)), so every third-party deploy silently grants that account ❌-undo rights. ~16 env vars the code reads are missing from `.env.example` (all `REVO_*`, `HEVY_*`, `ENABLE_PRESENCE_TRACKING`, `ENABLE_VOICE_TRACKING`, `ADMIN_USER_IDS`, `GAME_ICONS_*`, `LOG_FORMAT`), and it ships real-looking guild/channel IDs as "examples."
- **Implementation:** Change the default to `''`; add Revo/Hevy/tracking blocks mirroring the `STRAVA_*` comment style; replace real IDs with obvious placeholders.
- **Impact:** Reproducible deploys; removes implicit admin access from every non-author deployment. *(Platform lens — verified.)*

#### QW-10 · Lift value bounds + repair the 5 bad legacy rows
- **Priority:** Medium · **Effort:** S · **Data quality**
- **Problem:** The dump holds three `weight_kg=0.0` lifts (ids 25/26/114) and 9999/7500-kcal entries accepted verbatim from `9999c`/`7500c`. These skew `/tdee`, adherence stats and PR charts. *(Verify note: kcal ≤10k and protein ≤400 caps **already exist** — [bot.py:13475](app/bot.py#L13475)/[14976](app/bot.py#L14976) — so scope is narrower than "add all bounds.")*
- **Implementation:** Add a lift **lower** bound (`weight_kg > 0`) in `_split_reasonable_lifts` ([bot.py:879](app/bot.py#L879), which caps only the upper side today); tighten the kcal cap from 10000 so 9999 bounces; one-off delete/repair of the 5 known-bad rows.
- **Impact:** Stops outliers corrupting averages and leaderboards. *(Platform lens — verified **partial**: the broad claim was wrong; these are the genuine gaps.)*

#### QW-11 · Add three context menus: "Log lifts from this", "Not a lift", "Gym stats"
- **Priority:** Medium · **Effort:** S · **Mobile UX**
- **Problem:** There are **zero** context menus in the codebase (verified). Correction tools require raw message IDs (`/parse <message_id>`, `/suppress_message`) — on mobile that means enabling Developer Mode and long-pressing to copy an ID. Yet the group constantly acts on each other's already-typed messages.
- **Implementation:** Register three `app_commands.ContextMenu` wrappers over the existing tested handlers: message→`parse_cmd` body with `interaction.message`, message→`suppress_message` (admin-gated), user→`stats_cmd` (ephemeral). Sync via the existing `/sync`.
- **Impact:** Removes the Developer-Mode ID dance; makes re-parse/suppress usable mid-session on a phone; a zero-typing entry to `/stats` for the 17 members who've never run a command. *(UX lens.)*

---

### 🟡 Short-Term (1–2 weeks)

---

#### ST-1 · "You trained but didn't log" nudge from Revo/Hevy/Strava ground truth
- **Priority:** High · **Effort:** M
- **Problem:** Manual lift logging is the biggest churn problem (6→1 lifters/week) while attendance *continues* — `revo_attendance_poll` detects check-ins every 10 min ([bot.py:9389](app/bot.py#L9389)) but never cross-references logging. The dump proves the gap: Cookie Monster has a 15-week Revo streak, check-in 07-11, but no manual lift since 05-25. The group already does this by hand: "*I went gym yesterday bot*" → "*Well you didn't log it you lemon*."
- **Why valuable:** A nudge fired *only* on days the bot **knows** you trained is the highest-precision re-engagement lever available, and it automates the peer-nagging that's already part of the fun.
- **Implementation:** Extend `streak_saver_loop`'s evening pass: for each `revo_account` with `last_checkin_date == today` (and/or a Hevy/Strava activity today), if `lifts` **and** `calorie_entries` are empty for that local day, DM one playful nudge (reuse `_streak_saver_check_user`'s mark-before-send / closed-DM-safe pattern). Skip Hevy-linked users (auto-logged), respect blacklist, **strictly opt-in** (Cookie Monster objected to tracking once). Templated DM — no Gemini cost.
- **Impact:** Reaches the 5 Revo-linked members (4 of the 6 historical lifters) on exactly the days they trained. *(Engagement + AI + Automation — converged.)*

#### ST-2 · "Did you mean …?" on a failed lift parse, with one-tap fix + auto-alias
- **Priority:** High · **Effort:** M
- **Problem:** When a lift message fails to parse (unknown machine), the bot does **nothing** — the on_message fall-through only reacts for >500kg rejections. This caused the dominant documented pain: retry-until-it-sticks loops (six tricep pushdown/pulldown reposts on 05-11; "*Why is this fucking not not working*" 04-27; "*what dis shit not working*" 05-25), each needing a manual `/alias_add`.
- **Why valuable:** Turns the #1 user-facing failure into a 2-tap fix and permanently teaches the bot the group's vocabulary. Likely the single biggest lever to revive manual lift logging — the calorie path thrives precisely because it always replies.
- **Implementation:** In the lift branch, detect "weight token present but parser returned nothing," then call the **already-existing-but-unused-here** `_suggest_equipment` ([bot.py:1310](app/bot.py#L1310)) — free `difflib` for typos — plus an optional single Gemini flash call for semantic matches ("chest fly"→"pec dec"). Reply with a `discord.ui.View`: "Log as ‹candidate›" (stores the lift **and** writes the phrase into `custom_aliases`) / "Not a lift" (`suppress_message`). Restrict buttons to author/target like reaction-undo ([bot.py:4489](app/bot.py#L4489)). *Note: this introduces the first `discord.ui` usage in the codebase — a small reusable pattern several other recs build on.*
- **Impact:** Eliminates the retry-repost loop (≥4 separate days in the transcript); every fix grows the alias table (only 5 custom aliases exist vs 67 equipment strings with ~7 duplicate clusters). *(UX + AI — converged.)*

#### ST-3 · Weekly "movers" leaderboards (most-improved, not all-time)
- **Priority:** Medium · **Effort:** S
- **Problem:** `/leaderboard` is an all-time top-25 of global bests — with 5-8 actives it's frozen, and Jaidyn's 591-row Hevy backfill (reaching to Nov 2025) permanently owns it. `weekly_report` lists PRs but ranks nobody.
- **Why valuable:** Delta-based boards reset every Monday so everyone starts at zero — the only leaderboard format that stays alive in a small competitive pool.
- **Implementation:** Add a "Movers this week" section to `weekly_report` ([bot.py:3301](app/bot.py#L3301)): PR deltas from `_new_prs_for_lifts` ([bot.py:1408](app/bot.py#L1408)), tonnage from the `/tonnage` aggregation, logging-days from `_entry_local_days`, adherence from `app/targets.py`. Format like `/revo_streak_compare`. Optionally add a `window: 7d|30d` param to `/leaderboard`.
- **Impact:** Every weekly report becomes contestable without out-lifting Jaidyn's import history. *(Engagement lens.)*

#### ST-4 · Cooperative weekly server goal with a daily progress bar
- **Priority:** Medium · **Effort:** S
- **Problem:** 17 of 25 members (68%) have never touched a tracking feature, and every competitive surface is winner-skewed. Nothing gives the group a shared target where a casual member's single log matters.
- **Why valuable:** Small friends groups rally around "we hit it together" better than ladders. A weekly team goal ("15 Revo check-ins + 30 calorie-log days") makes marginal contributions visible and gives the actives a reason to recruit the passives — the biggest reach lever.
- **Implementation:** Store the week's target in `app_meta` (`db.meta_set`), auto-scaled from the trailing 4-week average. Monday kickoff post + one progress-bar line in `_daily_update_text`; result in `weekly_report`. Counts come from tables already populated — no new collection.
- **Impact:** First mechanic where the 17 non-users' participation is asked for by *friends*, not the bot; failure states produce banter, not shame. *(Engagement lens.)*

#### ST-5 · "Save as a shortcut?" button when a calorie note repeats
- **Priority:** Medium · **Effort:** S
- **Problem:** Saved foods/meals are the bot's best typing-reducer (one word logs kcal+protein) but adoption is 2 of 6 nutrition users (`calorie_foods`: Poshy 7, musk 1; `calorie_meals`: 0). Everyone else re-types free-text notes on the 463-adds/3wk hot path — the feature is buried at subcommands 11–16 of 21.
- **Implementation:** In `_handle_calorie_message` and `/calories add`, when the same normalized note has appeared ≥3× for that user and no `calorie_foods` row matches, attach a one-time `discord.ui.Button` "Save 'toastie' as a shortcut (620 kcal)?" (prefill with the median of past amounts) that calls the existing `food_set` logic. Record a declined-flag so it never re-prompts.
- **Impact:** Moves the 4 non-adopters onto the one-word path they demonstrably need. *(UX lens.)*

#### ST-6 · Rebuild `/help` as a topic-select menu with a "start here" landing page
- **Priority:** Medium · **Effort:** S
- **Problem:** `/help` ([bot.py:5956-6157](app/bot.py#L5956)) fires two mega-embeds with ten fields and ~90 command lines — ~6 phone screens with no navigation — and buries the one thing a new member needs ("just type `bench 80kg`") under the full 60-command surface.
- **Implementation:** Keep the handler; move each existing `add_field` block into a topic dict. Send a compact landing embed (the "start here" trio: post a lift in chat, `/calories setup`, `/revo_link`) with a `discord.ui.Select` of topics whose callback edits the ephemeral message to that topic's embed. Existing `STRAVA_DISABLED`/`_hevy_enabled` gates become conditional options.
- **Impact:** First-contact help drops from ~6 screens to one; each topic fits a mobile screen. *(UX lens.)*

#### ST-7 · Retention pruning for the unbounded telemetry tables + media archive
- **Priority:** Medium · **Effort:** S · **Ops/Privacy**
- **Problem:** Zero `DELETE` paths for `message_log` (~1,850 rows/wk → ~96k/yr), `presence_events` (accelerating 141→800/wk), `voice_events`, `activity_events`, `audit_log`; the permanently-downloaded media archive grows uncapped. Nothing consumes most of this after a few weeks, yet it accumulates forever alongside every member's full chat content (retained even for blacklisted members).
- **Implementation:** Add `db.prune_events(table, older_than)` next to the existing presence purge ([db.py:2986](app/db.py#L2986)), invoked from `db_backup` **after** each snapshot (so pruned data is always in a kept backup). Env knobs (`MESSAGE_LOG_RETENTION_DAYS=365`, `PRESENCE_RETENTION_DAYS=180`, …; `0 = keep forever`). Walk `MEDIA_DIR` deleting files whose row was pruned; `VACUUM` after large prunes.
- **Impact:** Permanently caps DB + disk growth; shrinks the standing PII surface. *(Platform + Automation — converged.)*

#### ST-8 · Confirm-before-store for *implausible* lifts (per-user relative bound)
- **Priority:** Medium · **Effort:** M
- **Problem:** The only guard is a flat `MAX_WEIGHT_KG=500`. Values absurd *for the person* sail through: a "65 reps" post stored as 65kg, Poshy's fake 250kg bench needing a manual revert nobody believed ("*nothing seems reverted to me*"). Every bad row erodes trust.
- **Implementation:** In `_split_reasonable_lifts`, compare each lift to the user's existing best for that equipment (the PR lookup already runs pre-insert, [bot.py:3896](app/bot.py#L3896)); if `weight > max(best×1.5, best+40kg)`, don't store — reply with the parsed interpretation and "Yes, store" / "No, ignore" buttons (author/target only). No new tables (unconfirmed = not inserted).
- **Impact:** Stops the documented corruption events with zero friction on normal progressive-overload posts. *(UX lens; complements QW-10.)*

#### ST-9 · Revo raffle-deadline reminder + ticket-race post (build documented feature E)
- **Priority:** Medium · **Effort:** S
- **Problem:** The one real-world reward loop (Revo raffle tickets) is view-only, and the docs' own roadmap item — a "Major draw closes in 24h" push — is explicitly unbuilt (`docs/REVO_PORTAL.md §5`). 5 members hold real ticket balances.
- **Implementation:** In `revo_attendance_poll`, reuse the draw-countdown parsing behind `/revo_raffle` to fire a 24h-before post; dedup with `db.meta_set('last_raffle_reminder')` exactly as the docs spec. Append a ticket ranking formatted like `/revo_streak_compare`.
- **Impact:** A timely, real-prize ping per draw cycle; nudges unlinked actives to `/revo_link`. *(Engagement + Automation — converged; retires a documented TODO.)*

#### ST-10 · Gemini usage accounting + batch the weekly per-member calls
- **Priority:** Medium · **Effort:** S · **Ops**
- **Problem:** No visibility into token spend, and `_calorie_ai_summaries` makes one **serial** Gemini call per member inside the weekly-report loop ([bot.py:3123-3162](app/bot.py#L3123)) — 6 calls × 2-8s + retry backoff, eating free-tier RPM (the retry/backup-model machinery exists precisely because 429s happen). Every AI rec below adds to an unmonitored budget.
- **Implementation:** `usageMetadata` is already in the API response ([gemini_client.py:298](app/gemini_client.py#L298)) — persist per-call rows (feature, model, tokens, latency) to a `gemini_usage` table via the `_migrate` ALTER pattern; surface totals in `/version` or the dashboard. Refactor `_calorie_ai_summaries` to one JSON-array call, keeping the per-member plain-stats fallback.
- **Impact:** Operational guardrail that de-risks every other AI feature; ~6× cut in weekly-report AI latency and calls. *(AI lens; realistically <$1/month at this scale — worth confirming with data.)*

---

### 🟠 Medium-Term (1–2 months)

---

#### MT-1 · Monthly & weekly "Wrapped" recap — the founding promise, from dead telemetry
- **Priority:** High · **Effort:** M
- **Problem:** The bot was founded for month-over-month comparison ("*ill make a discord bot to track everyones month by month*"), but that only exists as the on-demand `/progress`. No monthly post exists (the 9 task loops are daily/weekly/poll only). Meanwhile `message_log` (11.7k), `presence_events` (4.5k), `voice_events`, `activity_events` are pure write-only telemetry no feature reads, and the channel has been silent since Jun 2.
- **Why valuable:** One high-production shared comparison per month is exactly the format the group asked for, on the *only* cadence proven to drive traffic (bot events). It's also the payoff that converts ~18k dormant rows into content the whole server appears in.
- **Implementation:** New `app/server_recap.py` pure module (mirroring `app/overview.py`): per-user message counts by channel; VC hours by pairing `voice_events` join/leave; top games via `presence.summarize_activity_sets`; Revo streaks; PR deltas (`_new_prs_for_lifts`); nutrition adherence (`app/targets.py`); rendered to a shareable PNG via `app/graphing.py`. Optionally feed one compact JSON blob to Gemini for a banter-toned narrative (reuse the tolerant `_parse_recap_json`, [bot.py:3178](app/bot.py#L3178)). New monthly `tasks.loop` cloned from `weekly_report`, deduped via `app_meta`. ~1 flash call/month.
- **Impact:** Highest-leverage engagement feature; directly attacks the 6-week channel death. *(Engagement + AI + Automation — three lenses converged on this.)*

#### MT-2 · First-run onboarding: welcome message with action buttons
- **Priority:** Medium · **Effort:** M
- **Problem:** No first-run experience — `on_member_join` ([bot.py:10985](app/bot.py#L10985)) only mirrors the member for the dashboard. The newest member ("active", joined 07-13) started logging calories with **no goal set**, so their replies can't show progress. 68% of members have never engaged.
- **Implementation:** Extend `on_member_join` to DM (gym-channel fallback) three buttons: "Set calorie target" (a `discord.ui.Modal` feeding the `/calories setup` logic), "How do I log lifts?" (the `/help` landing snippet), "Link Revo" (`/help_revo_link` content). Plus a first-log tip: when a stored row is the user's first ever, append one line explaining ❌-undo and `/calories setup`.
- **Impact:** Every future member gets a working path to their first log without reading anything; fixes the "entries but no goal" state the newest user is already in. *(UX lens.)*

#### MT-3 · `/duel` — time-boxed head-to-head challenge with a resolution post
- **Priority:** Medium · **Effort:** M
- **Problem:** `/compare` is a static all-time snapshot, but the group's real behavior is time-boxed rivalry ("*im going for 100kg next week*", constant same-machine arguments) with no stakes and no finish line.
- **Implementation:** New `duels` table; `/duel` picks a metric (calorie-log days, tonnage, Revo check-ins, adherence %); opponent accepts via ✅ on the existing `reply_tracking`/`on_raw_reaction_add` plumbing; daily score line appended in `_daily_update_text`; winner announced from the daily loop. Metrics reuse `/tonnage`, `_entry_local_days`, `_calc_streaks`. Deliberately a 2-person format — right for 5 actives, pointless at 1000.
- **Impact:** A repeatable game with built-in trash-talk at start, midpoint, finish. *(Engagement lens.)*

#### MT-4 · Badge set in the group's own vocabulary (Plate Club, Iron Streak, Revo Regular)
- **Priority:** Medium · **Effort:** M
- **Problem:** No achievements exist, and abstract self-set targets demonstrably don't stick here (`goals` and `bodyweight_goals` both 0 rows). Yet the group self-assigns identity milestones in plate language ("*Legs 3.5 plates chill*", "*4 plates each side*") and chases round numbers.
- **Implementation:** New pure `app/badges.py` (joins the 522-test suite) computing state from `lifts`/`calorie_entries`/`revo_account`; plate thresholds reuse the `/plates` math. Check at store time right after `_new_prs_for_lifts` so earning posts instantly ("🏅 joined the 2-Plate Bench Club"); persist in a `badges` table; display the shelf in `/summary` and the monthly Wrapped. **Curated-and-few** (Plate Club I/II/III, 30-Day Iron Streak, 15-Week Revo Regular, 100-Log Data Gremlin) — a 50-badge grid would be noise at this scale.
- **Impact:** Permanent bragging rights between sessions; the concrete named target the empty `goals` table failed to be. *(Engagement lens.)*

#### MT-5 · Equipment merge/alias-audit tool (semi-automated cleanup)
- **Priority:** Medium · **Effort:** M
- **Problem:** 67 equipment strings contain ≥7 duplicate clusters (tricep pushdown ×4 spellings, calf raise ×5, pec dec/butterfly/chest fly, rear delt fly ×3) against only 5 `custom_aliases` rows — silently fragmenting PRs, `/progress`, `/machine`, `/leaderboard`. Cleanup today is manual admin work.
- **Implementation:** Add a clustering helper to `app/aliases.py` (difflib + token-set overlap, unit-testable against the 7 known clusters). New admin `/merge_review` posts one ephemeral embed per cluster with "Merge into X" / "Skip" buttons calling `db.rename_equipment(scope=all)` **and** seeding an alias (+ audit rows per QW-1). Optionally an AI batch pass (`/alias_audit`) for semantic matches, and a monthly detector that DMs the admin only when *new* clusters appear.
- **Impact:** One pass fixes ~200+ misfiled rows; ongoing effort drops from "notice, investigate, type exact strings" to "tap a button." *(AI + Automation — converged.)*

#### MT-6 · `/ask` — natural-language Q&A over your own tracked data
- **Priority:** Medium · **Effort:** M
- **Problem:** ~60 commands + 36 subcommands; users don't discover them and instead ask in chat ("*what was mine*", "*wtf is chin assist*"). No way to ask "when did I last do legs?" or "am I eating enough protein on gym days?" without knowing the command.
- **Implementation:** Model on `coach_cmd` ([bot.py:13323](app/bot.py#L13323)): defer, build context with `_build_progress_payload(guild_id, user.id, days=90)` + the guild equipment/alias list + the question, call Gemini with a system prompt mandating answers **only** from the supplied JSON (quote exact numbers). Reuse `_deny_invisible_target` for the same privacy rule as `/coach`. Usage-driven cost, so it can't run away.
- **Impact:** A discovery/usability multiplier over every existing data feature; also absorbs the recurring FAQ class. *(AI lens.)*

#### MT-7 · Plateau/stall detection via e1RM math
- **Priority:** Medium · **Effort:** S
- **Problem:** 58 (user, equipment) pairs are progression-chartable and 591 Hevy rows carry reps, but nothing detects a *stall* — `/stale` only flags recency. The group is PR-driven and never resolved the 1RM-vs-6RM convention.
- **Implementation:** Add `plateau_detect(rep_sets, window_weeks=6)` to the tested `app/training_math.py`, computing per-equipment e1RM series via the existing `parser.estimated_one_rep_max` ([parser.py:506](app/parser.py#L506)) over the same feed `_e1rm_progression` already uses. Inject a `stalled_lifts` list into `/coach` and a one-line callout into `weekly_report`. e1RM sidesteps the PR-vs-reps ambiguity by comparing like with like.
- **Impact:** Real coach value for the 3 users with enough data (Jaidyn 22, musk 19, Poshy 19 chartable pairs). *(AI lens.)*

#### MT-8 · Lightweight gym-session RSVP with Revo attendance receipts
- **Priority:** Medium · **Effort:** M
- **Problem:** The group coordinates sessions in plain chat with zero tooling ("*Friday we hit it up???*", "*can we go gym at like 4:30?*") plus flake-shaming ("*bro has prolly gone to watch netflix like the last 10 times*"). Nothing in ~60 commands touches scheduling, and nothing closes the loop on who actually showed — even though the Revo poller knows.
- **Implementation:** One upcoming session per guild. New `gym_sessions` table; `/session plan time:"fri 4:30pm"` (reuse the on_message date-hint parser) posts an embed; RSVP via 👍/❌ on the existing reaction dispatcher; a due-check in the 15-min loop pings RSVP'd members 30 min before with the live `/busy` head count; next morning cross-check RSVPs vs `revo_account.last_checkin_date` and post a playful roll-call.
- **Impact:** Gives the most common non-logging ritual a home; 2-3 organic bot posts per planned session in a quiet channel. *(Automation lens; shares plumbing with ST-1.)*

#### MT-9 · Unified per-user notification preferences (`/notify`) + weekly bodyweight DM
- **Priority:** Medium · **Effort:** M
- **Problem:** Notification surfaces have grown ad-hoc (channel-broadcast reminders, per-user streak DMs, per-account Revo announcements) with no single opt-out — notable since a member objected to bot surveillance and the promised weekly weigh-in prompt ("*It's gonna ask weekly*") only shipped as a channel broadcast (`bodyweights` has just 30 rows). Recs ST-1/QW-5/MT-8 add more nudges.
- **Implementation:** Generalize `calorie_reminder_prefs` into a `reminder_prefs` table (`user_id, kind, hour, minute, last_sent`) — kinds: `calorie_streak`, `bodyweight_weekly`, `revo_nudge`, `session_ping`, `revo_announce_optout`. One ephemeral `/notify` (view + toggle) replaces `/calories remind` (keep as alias). Drive all DM kinds from the existing loop; a per-user Monday bodyweight DM (only if no weigh-in in 7 days) finally delivers the promised feature.
- **Impact:** Makes the whole reminder program consent-based before three more nudge types land; grows the 30-row `bodyweights` table that TDEE, goals, and true-load calcs all depend on. *(Automation lens.)*

#### MT-10 · Test harness for the on_message dispatch pipeline & reaction-undo
- **Priority:** Medium · **Effort:** M · **Reliability**
- **Problem:** All 522 tests cover extracted pure modules; the 240-line 12-step on_message dispatch ([bot.py:3662-3901](app/bot.py#L3662)) and the race-protected reaction-undo are `pragma: no cover` — yet this exact surface produced every historical data-corruption incident (ghost entries, misattribution, the `1.5874e+19kg` GIF-URL parse).
- **Implementation:** Follow the repo's own pattern — extract the step-ordering into a pure dispatch function (like `app/message_targeting.py` did for targeting) returning an action enum + payload, leaving on_message a thin adapter. Unit-test the ordering matrix; test reaction-undo against a temp-file `Database` (the `test_db.py` fixture style).
- **Impact:** Regression protection for the riskiest surface; makes the MT cog refactor safe. *(Platform lens — verified.)*

---

### 🔵 Long-Term Strategic

---

#### LT-1 · Split `bot.py` into discord.py Cogs
- **Priority:** Medium · **Effort:** L · **Maintainability**
- **Problem:** `bot.py` is 15,331 lines (53% of the app) holding all commands, 9 task loops, every event handler, config, and integration glue. Natural seams already exist (the `calories`/`protein`/`track` groups; contiguous per-domain line ranges), but one namespace makes coupling and conflicts inevitable as features ship weekly.
- **Implementation:** Incremental, one domain per PR. Extract config (`bot.py:100-380`) into `app/config.py` first; then `commands.GroupCog` subclasses for the existing groups (lowest risk), then revo/strava/hevy/lifts/admin, each carrying its own `tasks.loop`. Inject `db` at `setup_hook`.
- **Impact:** Unblocks handler-level testing (MT-10), parallel work, and per-integration kill switches; no user-visible change. *(Platform lens — verified.)*

#### LT-2 · Consolidate the flat command namespace into groups
- **Priority:** Low · **Effort:** M · **Discoverability**
- **Problem:** ~60 top-level commands flood the `/` picker (11 `revo_*`, 7 `strava_*`, 6 `hevy_*`, 4 bodyweight-ish, 4 alias/equipment) while `calories`/`protein`/`track` already show the group pattern. The picker *is* the discovery surface on mobile. Two near-duplicate exports (`/export`, `/export_lifts`) also exist.
- **Implementation:** Mechanical decorator swap moving `revo_*`/`strava_*`/`hevy_*`/`bodyweight_*`/`alias_*` under groups mirroring `calories_group`; delete one duplicate export. Ship in one release with a `#gym-and-strava` announcement (old names disappear). Best done *alongside* LT-1.
- **Impact:** Cuts top-level entries ~60→~25 so `/log`, `/stats`, `/graph`, `/calories` stop drowning. *(UX lens; low priority — breaking change for tiny active user base.)*

#### LT-3 · Pounds / unit support & a smarter plate helper
- **Priority:** Medium · **Effort:** M
- **Problem:** Pounds aren't supported — a member's "225 bench" (lb) was logged as **225 kg**, and users misread "*the pounds on the plates as kgs*." `/plates` exists but is pure arithmetic and can't disambiguate kg/lb plates.
- **Implementation:** Extend `app/parser.py` to accept `lb`/`lbs`/`#` and convert (÷2.2046), storing kg with a note; add a per-user default-unit pref; extend `/plates` to take a unit and flag kg/lb plate confusion. Unit-test in the existing 31 parser tests.
- **Impact:** Removes a recurring class of silent 2.2× data errors on a machine-heavy, mixed-gym group. *(Surfaced by chat analysis; not in the lens sets — included for completeness.)*

#### LT-4 · Structured rep semantics (weight×reps, 1RM vs 6RM tags, reps+added-weight)
- **Priority:** Low · **Effort:** L
- **Problem:** The group never resolved whether a number is a 1RM, a 6-rep working weight, or a rep count ("*PR btw not rep smh*", "*we are doing reps on leg press right not pr*"), and there's no way to log "dips 6 + 20kg." Manual entries store no reps (only the 591 Hevy rows do). This ambiguity caused repeated data disputes.
- **Implementation:** Extend the parser to accept `weight x reps` and `reps + added weight`, storing reps on manual rows too; let the e1RM math (MT-7) normalize across schemes so leaderboards compare like with like. Larger because it touches the parser, storage, PR logic, and every display surface — sequence it *after* LT-1/MT-7.
- **Impact:** Resolves the longest-running data-convention dispute; makes cross-person comparison meaningful. *(Surfaced by chat analysis; sequence late.)*

#### LT-5 · Cross-gym machine-profile normalization
- **Priority:** Low · **Effort:** L
- **Problem:** Explicitly requested and endorsed in chat — musk: "*include machine weight in to stats for consistency throughout gyms*"; Cookie Monster: "*This is a really good point*." Numbers aren't comparable across gyms (75kg bar vs 45kg, per-arm vs shared stacks, angled vs flat leg press), which the README itself disclaims.
- **Implementation:** Optional per-(gym, equipment) metadata (added weight, per-arm flag, ratio); apply as an annotation layer on `/leaderboard`/`/compare` (never mutate stored values). Heavy because it needs a data-entry surface and touches every social display; genuinely community-requested, so worth it eventually.
- **Impact:** Makes cross-gym leaderboards *fair* — the fairness gripe that recurs throughout the transcript. *(Surfaced by chat analysis.)*

#### LT-6 · Sleep-vs-training correlation in `/track analyze` (consent-gated, self-service)
- **Priority:** Low · **Effort:** M
- **Problem:** `/track analyze` narrates sleep in isolation; `presence_events` are never joined to lift days, Revo check-ins, or calorie adherence — the correlation the data was ostensibly collected for.
- **Implementation:** Add a per-day join (trained? / check-in / kcal) to the analyze payload; instruct Gemini to hedge ("with only N weeks of data"). **Restrict to `target=self`** unless owner — tightening today's anyone-can-analyze-a-tracked-member behavior.
- **Impact:** High wow-factor for the 4 tracked users, but deliberately Low: only 4 consented users, ~6 weeks of joint data (weak correlations), and an on-record privacy objection. Keep opt-in and self-service. *(AI lens.)*

#### LT-7 · Encrypt `revo_account.email` & make the schema honest about foreign keys
- **Priority:** Low · **Effort:** S · **Security**
- **Problem:** `revo_account` stores 5 real members' portal emails in plaintext next to their Fernet-encrypted passwords (the email is half a credential pair and flows into backups). Separately, `db.py:635` sets `PRAGMA foreign_keys=ON` but the schema has **zero** `REFERENCES` clauses — a misleading no-op.
- **Implementation:** Reuse the `app/revo_client.py` Fernet helper to add `email_enc`, migrate + encrypt in place, null the plaintext column. Either add real FKs for the app-enforced links or delete the dead pragma with a comment.
- **Impact:** All third-party credentials encrypted at rest, including in backups; schema stops advertising guarantees it doesn't enforce. *(Platform lens — verified; interacts with QW-7 once backups go off-box.)*

---

## 4. Considered and deliberately *not* recommended

- **Generic XP/leveling ladders, large badge grids, seasonal battle-passes** — designed for big servers; would be noise at 5-8 actives. Chosen the small-server-shaped mechanics instead (2-person duels, co-op goals, curated badges).
- **Rebuilding `/compare`, `/leaderboard`, `/coach` from scratch** — they already exist and work; the recs *enrich* them (time windows, movers, attendance data) rather than replace them.
- **Aggressive proactive Revo/presence surveillance features** — capped or made opt-in throughout because of the on-record privacy objection; consent is a hard constraint here, not a nice-to-have.
- **Per-visit/per-club Revo tracking & hourly busyness heatmap** — confirmed *impossible* via the web portal (`docs/REVO_PORTAL.md`); would need mobile-app reverse-engineering. Not pursued.

## 5. Suggested sequencing

1. **Week 1 (security + winner-amplification):** QW-1, QW-2, QW-3, QW-4, QW-5, QW-7, QW-9. Mostly small, mostly High — locks down the destructive surfaces and doubles down on the nutrition/streak loop that's actually carrying the bot.
2. **Week 2-3 (revive lifts + reduce friction):** QW-6, QW-8, QW-10, QW-11, ST-1, ST-2. The parse-failure fix (ST-2) introduces the reusable `discord.ui` button pattern the rest of the roadmap leans on.
3. **Month 1-2 (re-engagement engine):** MT-1 (Wrapped) first — it's the founding promise and the biggest lever — then the engagement/AI/automation MTs as capacity allows.
4. **Ongoing / strategic:** LT-1 (Cogs) as a background refactor that unblocks testing and everything after; LT-3/LT-4/LT-5 when the group's cross-gym/rep-convention debates resurface.

---

*Method: 4 parallel analysis agents (code inventory, DB usage, DB schema/health, chat transcript) → 5 recommendation lenses (engagement, UX, AI/data, automation, platform) → adversarial verification → this synthesis. The platform lens was independently verified (all 10 items confirmed novel & grounded; one downgraded to "partial" — see QW-10). The other four lenses' verification passes were interrupted by a usage limit; their load-bearing claims were spot-checked directly against the source before inclusion (daily-update silent-skip, Revo-only milestones, unused `_suggest_equipment`, zero `discord.ui`/context-menus — all confirmed).*
