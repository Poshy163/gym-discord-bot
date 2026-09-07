# Application audit, 7 September 2026

## Outcome

Confirmed fixes are implemented in the repository. The production dashboard was inspected read-only, and updated code was exercised in disposable local environments. No production account settings, rewards, Discord messages, or existing user databases were changed. No dependency or schema migration is required. Validation preceded the requested commit and push to main; production deployment remains separate.

## Revo discrepancy: confirmed cause and repair

The Android app uses a Netpulse-issued BMA SSO token to open Revo Central rewards pages. The integration previously sent only the portal's `Member` cookie. Cookie login succeeds, but the protected feature pages redirect to `/?closePage` without the app token. The same account and pages return HTTP 200 with the verified token flow. This establishes the missing authentication context as the cause of the current failure; it does not establish the implementation of the older `Invalid Access! B` guard.

| Capability | Cookie-only portal | Updated integration, live verified |
|---|---|---|
| Attendance calendar | Redirects to closePage | August: 31 day cells; September: 30 day cells; exact visit-day parsing succeeds |
| Ticket history | Redirects to closePage | Numeric balance and 29 readable history rows |
| Raffle | Redirects to closePage | Both draw countdowns and explicit entry state readable |
| Prize pool | Redirects to closePage | Both prize descriptions readable |
| Weekly streak / ticket total | Landing tiles remain readable | Preserved independently; all source probes pass |
| All-club occupancy | PerfectGym API already working | Existing 81-club path preserved |
| Membership / club information | Netpulse already working | Existing safe metadata/directory path preserved |

### Android evidence

Static inspection used Revo Fitness `com.netpulse.mobile.revofitness`, version 4.3 (404), downloaded from the [APKPure mirror](https://apkpure.net/revo-fitness/com.netpulse.mobile.revofitness/download). The XAPK SHA-256 matches its published hash: `27b3651632e78f354d63c88c40cf8397b4eab576c23d5b1ff3e691131b20e793`. The extracted base APK SHA-256 is `73e0b2b101b9aeacb1dd4694afd3fb79959a2f1c8166e64e00e1d7efd9a51fc4`. This is mirror provenance, not an independently verified Play-signing certificate. The APK was not installed or executed. JADX 1.5.6 extracted the relevant native code and resources, and the bundled JavaScript was inspected directly. The full native decompile reported 307 errors in other classes; the authentication/configuration methods cited below were readable and their contract was independently verified against the server. Decompiled third-party source is kept outside the repository.

The trace is reproducible from these APK locations:

1. `assets/f85e675a-production/assets/index-DLbHWMlC.js` marks raffle and streak links `needSSO`, reads `portalsContext.authToken`, opens each URL with a `token` query parameter, and handles `closePage` as the WebView end condition.
2. `MicroWebAppApi.java` declares `GET /np/micro-web-app/v1.0/exercisers/{userId}/tokens/{partner}`. `GetMwaAuthTokenUseCase.java` obtains the configured partner and passes `accessToken` into the web context. `MicroWebAppAuthToken.java` identifies `partner`, `accessToken`, and `accessTokenExpiresAt`.
3. `ConfigClient.java` declares the application-layout endpoint. `SystemConfigKt.java` encodes app version 4.3 as `40300`. An authenticated read of `/np/dynamic-features/v2.1/exercisers/{uuid}/application-layout?appVersion=40300` identifies the Revo dashboard (`partnerAppId=f85e675a`) as partner `BMA`.
4. A normal authorised Netpulse credential login followed by the exact BMA token endpoint succeeds. Passing its token to the calendar, raffle, ledger and prize requests makes each readable without a portal login. The token has a server-supplied expiry; it is not a static key or a login-response token guessed from another service.
5. The [current rewards JavaScript](https://revocentral.revofitness.com.au/portal/rewards/assets/js/script.js) forwards the token with calendar month/year. It defines activity codes 1=Gym, 2=Les Mills, 3=Gym & Les Mills. The integration counts only 1 and 3 as gym attendance and rejects unknown codes.

`NetpulseClient.get_rewards_token()` holds the credential privately in memory, refreshes before expiry, serializes requests, and clears rejected/failed refresh state. `RevoClient` lazily creates its own account-matched mobile session and sends tokens only to four fixed rewards paths. It never follows redirects with the token and retries a rejected token once. Raffle action parameters are not supported. No new configuration, database migration or dependency is required.

All seven `probe_sources` checks now report `ok`, including the precise calendar attendance feed. This was also verified through live reads from the rebuilt production Python 3.12 Docker image in a disposable container with no Discord worker. Token values, login responses, cookies, personal identifiers, and raw account pages were not stored in fixtures or documentation. The official app UI was not exercised on a phone; the APK authentication contract and equivalent server reads were verified. Production is not yet updated.

## Confirmed application fixes

- **Revo session handling:** per-account request/login serialization; one reauthentication attempt for actual expiry, including password-form HTML returned with HTTP 200. Failed authentication invalidates old session context. Netpulse reconstructs membership URLs after UUID refresh. Non-login redirects and rate limits are not hidden by authentication loops. Legacy counter reads no longer share a linked account's favourite-club ID through a global cache.
- **Data correctness:** unavailable portal responses fail explicitly; the ticket balance is separate from attendance history. PerfectGym rejects malformed/non-finite/missing occupancy counts instead of ranking a broken row as an empty gym. Known zero remains zero.
- **Private diagnostics:** Revo login logs and health output omit emails, member IDs, cookies, tokens and signed URLs. Network failures report stage/type without raw request URLs or response bodies.
- **Discord:** reconnect jobs cannot overlap; failed attendance delivery retains the previous cursor for retry. Default mention parsing is disabled, with deliberate notifications opting in. Summary and raffle output preserve verified ticket totals while reporting unavailable history/entry details. Streak-only refresh never advances a visit cursor.
- **WebUI:** corrected inline JavaScript argument escaping for role names, food aliases, lift edit payloads, Hevy template IDs, clipboard links and attachment links. Added accessible connection/action states, explicit cached Revo metadata, system/dark/light themes, light navigation styling and setting-control labels. Nested routes retain the existing absolute favicon. Mobile cache labels wrap cohesively.
- **Backend:** failed/unverifiable backup files no longer rotate out good backups. Cancellation drains the actual backup/verification thread before database closure. Open Food Facts rejects malformed structures and non-finite nutrition numbers.

## Audit coverage

`app.supervisor` owns SQLite, encrypted settings, the WebUI, backup jobs and a separate Discord worker. `workerlink` provides local RPC. `app.bot` owns Discord commands, background polling, account-client caches, notifications and message ingestion. `app.db` owns additive migrations, global member data and guild-specific community data. `config` and `settings_service` resolve environment/database settings and secret storage. Revo Central, Netpulse and PerfectGym remain separate adapters. Hevy, Strava, Apple Health, nutrition lookup and AI clients were reviewed through their storage and presentation consumers; existing optional integration code was preserved.

UI route coverage: setup/login/logout, overview, members/detail, activity, sleep, messages, voice, roles/detail, leaderboard, audit, lifts, Hevy, calories, protein, settings, media and health. Existing shared navigation, loading/empty/retry states, component lifecycle, permissions and API contracts were reviewed. Code review does not establish live correctness of every third-party service.

## Validation

Final validation results:

- Baseline `python -m pytest -q`: **1880 passed** before changes.
- Updated `python -m pytest -q`: **1982 passed** on local Python 3.14 (430 dependency deprecation warnings).
- Production Python 3.12 image with pinned requirements, `python -m pytest -q -p no:cacheprovider tests`: **1982 passed**, 92 dependency deprecation warnings, no skips. Test files were mounted read-only at `/app/tests`; pytest was installed only in the disposable test container.
- Android regression coverage adds exact token transport, expiry/refresh failure, concurrency, account isolation, rejected-token retry limits, redirect/action refusal, safe errors, real HTTP transport DEBUG-log redaction, and gym versus Les Mills calendar codes.
- Focused Revo session/parser tests: concurrent cold/expired sessions, repeated expiry, auth redirects, HTTP-200 password forms, malformed/empty responses, 429/503, independent account data, privacy and partial ticket functionality.
- `python -m compileall -q app scripts tests` and `git diff --check`: passed.
- `python -m ruff check app tests scripts --select E9,F63,F7,F82 --ignore F821`: passed. New shared HTTP helper/session tests also pass the F and E9 rules.
- Fatal Ruff comparison: the existing bot's 374 dynamic-config F821 diagnostics were identical before/after; no new diagnostic messages. The suite's unbound-global analysis understands those dynamic bindings. Unrestricted style lint is not clean in the baseline and was not presented as passing.
- `docker build -t gym-discord-bot:audit-local .`; isolated non-root startup with fresh tmpfs data and no Discord token. `/healthz` returns 200, `/healthz?require_worker=1` correctly returns 503, `/setup` and nested-route favicon return 200. Production remained untouched.
- Browser inspection at desktop size and 390 x 844: routes, empty states, nested-route sign-in, member detail, saved-food and alias dialogs using apostrophes, both themes, mobile wrapping, and unavailable-worker action state. The local preview uses synthetic records only.

The first container test mount omitted the application-relative source-analysis files; this was identified from collection differences and corrected for the final run. Intermediate failures while agents were editing were resolved before final validation. Discord interactions were exercised through mocks, not live messages. No static type-checker is configured and this inline Python/HTML application has no separate npm frontend build.

## Files to review

- `app/revo_client.py`, `app/revo_netpulse.py`, `app/revo_perfectgym.py`, `app/revo_http.py`
- `app/bot.py`, `app/webui.py`, `app/supervisor.py`, `app/food_lookup.py`
- `scripts/revo_health.py`, `scripts/preview_webui.py`
- Regression coverage in `tests/test_revo_sessions_audit.py`, `tests/test_revo_*.py`, `tests/test_bot_helpers.py`, `tests/test_supervisor.py`, `tests/test_food_lookup.py`, and WebUI tests.
