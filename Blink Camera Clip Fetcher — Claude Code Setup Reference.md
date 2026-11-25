# Blink Camera Clip Fetcher — Claude Code Setup Reference

Oct 13, 2026

## Overview

This is a small long-running daemon that pulls Blink camera clips down to local disk, since a Blink Sync Module 2 plugged into a server's USB port only draws power from it -- it does not expose the USB storage stick as a drive. The only way to get the video off is over the network through Blink's own (unofficial) cloud API, via the `blinkpy` Python library.

**Currently deployed as:** the `blink-fetch.service` systemd unit on an Ubuntu server, running as the `<user>` user, saving clips to `/home/<user>/blink-clips/<camera>/`.

**What it does, each poll (every 2 minutes by default):**

1. Logs into Blink using a saved refresh token (no 2FA after the first run).
2. Calls `blink.refresh()`, which pulls both ordinary cloud-listed motion clips and Sync Module 2 local-storage clips.
3. Downloads any clip not already seen (tracked in an on-disk dedupe file, not just in memory).
4. Once a day, deletes clips from the sync module's USB stick once they're older than a retention window *and* already confirmed saved locally.
5. Checks for three alert conditions (camera silence, low battery, after-hours/weekend activity) and emails `alerts@example.com` through the server's local postfix relay when triggered.

It is a single Python file (`blink_fetch_daemon.py`) with no external services besides `blinkpy`, `aiohttp`, and the local mail relay.

## Files

| File | Purpose |
| --- | --- |
| `blink_login.py` | One-time interactive script. Prompts for Blink email/password, then the 2FA code, and saves the resulting OAuth token + refresh token to a credentials JSON file (default `/etc/blink-fetch/credentials.json`). Run this once per Blink account; the daemon never needs to log in interactively again as long as that file exists. |
| `blink_fetch_daemon.py` | The long-running daemon. Loads the saved credentials, logs in (refreshing the token, no 2FA), then loops forever: poll for new clips, download them, run cleanup/alert checks, sleep, repeat. All config is environment variables (full list in Environment Variables below) -- the file's own top-of-file docstring is the authoritative reference if this doc and the code ever drift. |
| `blink-fetch.service` | systemd unit. Sets `User`/`Group` to the account that should own the clips (clips land in that user's home directory in the current deployment), sets every `BLINK_*` env var via `Environment=` lines, and points `ExecStart` at the daemon inside a dedicated venv. |

No other files are required. A `README-blink-fetch.md` was also produced during the original build-out with install commands; this doc supersedes it as the canonical reference going forward.

## Environment variables

**Core**

| Variable | Default | Purpose |
| --- | --- | --- |
| `BLINK_CREDENTIALS` | `/etc/blink-fetch/credentials.json` | Path to the file `blink_login.py` writes. |
| `BLINK_OUTPUT_DIR` | `/srv/blink-clips` | Where downloaded clips are saved, one subfolder per camera. |
| `BLINK_STATE_FILE` | `/var/lib/blink-fetch/state.json` | On-disk dedupe/alert state -- survives restarts. |
| `BLINK_POLL_SECONDS` | `120` | How often to check for new clips. |
| `BLINK_KEEP_DAYS` | `14` | How long dedupe records are kept in the state file (not clip retention). |

**Sync-module cleanup**

| Variable | Default | Purpose |
| --- | --- | --- |
| `BLINK_DELETE_OLDER_THAN_DAYS` | `14` | Delete a clip from the sync module's USB stick once it's older than this *and* already confirmed saved locally. `0` disables deletion entirely. |
| `BLINK_CLEANUP_INTERVAL_HOURS` | `24` | How often the cleanup pass runs (it rebuilds the sync module's manifest, so it's not run every poll). |

**Silence alert**

| Variable | Default | Purpose |
| --- | --- | --- |
| `BLINK_ALERT_HOURS` | `48` | Hours of total silence (no clip from any camera) before alerting. `0` disables. |
| `BLINK_ALERT_REPEAT_HOURS` | `12` | Re-send while still silent, so one missed alert isn't the only one. |

**Battery alert**

| Variable | Default | Purpose |
| --- | --- | --- |
| `BLINK_BATTERY_ALERT_REPEAT_HOURS` | `24` | Alert when a camera's own `battery_state` isn't `"ok"`; re-alert at most this often per camera while it stays that way. `0` disables. Piggybacks on data already fetched each poll -- no extra API calls. |

**After-hours / weekend alert**

| Variable | Default | Purpose |
| --- | --- | --- |
| `BLINK_TIMEZONE` | `America/New_York` | IANA timezone used both for clip filename timestamps and for the business-hours check below. Resolves to EST/EDT automatically depending on the date. |
| `BLINK_AFTER_HOURS_ALERT` | `true` | Alert on *every* clip recorded on a weekend, or on a weekday outside business hours. Not rate-limited -- each is a distinct event. |
| `BLINK_BUSINESS_START_HOUR` / `BLINK_BUSINESS_END_HOUR` | `7` / `18` | 24-hour local start/end of weekday business hours. |

**Alert delivery (shared by all three alert types above)**

| Variable | Default | Purpose |
| --- | --- | --- |
| `BLINK_ALERT_WEBHOOK_URL` | unset | POST `{"text": "..."}` here on alert (Slack/Discord-compatible). |
| `BLINK_ALERT_EMAIL_TO` | unset | Recipient address. In the current deployment: `alerts@example.com`. |
| `BLINK_ALERT_EMAIL_FROM` | `blink-fetch@localhost` | From address. Current deployment: `blink-fetch@example.com`. |
| `BLINK_SMTP_HOST` / `BLINK_SMTP_PORT` | `localhost` / `587` | SMTP relay. Current deployment uses the server's local postfix on port `25`, no auth. |
| `BLINK_SMTP_USER` / `BLINK_SMTP_PASS` | unset | Only needed if the relay requires auth. When set, a certificate-verified STARTTLS session is required before login; if it fails, the email is not sent. |

The daemon's own top-of-file docstring in `blink_fetch_daemon.py` is always the source of truth if this table and the code disagree.

## Install steps (fresh Ubuntu server)

Pick a service account -- if clips will land in that account's own home directory (as in the current deployment), run the daemon as that user directly rather than a separate system account, to avoid cross-account permission fights.

```bash
# Directories (NOT sudo for anything the service user must write to --
# see "sudo mkdir creates root-owned dirs" below)
sudo mkdir -p /opt/blink-fetch /etc/blink-fetch /var/lib/blink-fetch
mkdir -p /home/<user>/blink-clips

# Code + venv
sudo cp blink_login.py blink_fetch_daemon.py /opt/blink-fetch/
sudo cp blink-fetch.service /etc/systemd/system/
cd /opt/blink-fetch
sudo python3 -m venv venv
sudo ./venv/bin/pip install --upgrade pip
sudo ./venv/bin/pip install blinkpy

# Ownership (recursive -- see pitfalls below)
sudo chown -R <user>:<user> /etc/blink-fetch /var/lib/blink-fetch
```

**One-time login** (interactive, needs the Blink account's 2FA code):

```bash
/opt/blink-fetch/venv/bin/python3 /opt/blink-fetch/blink_login.py
```

This writes `credentials.json` and lists the cameras found -- confirm the expected camera names appear.

**Edit `blink-fetch.service`** before copying it in: set `User`/`Group`, `BLINK_OUTPUT_DIR`, and the alert email/webhook settings for this deployment.

**Start it:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now blink-fetch.service
journalctl -u blink-fetch.service -f
```

Watch for `Logged in. Watching N camera(s): ...` and, once motion happens, `Downloading ... -> ...` lines.

## Lessons learned / pitfalls (from the original deployment)

All of these actually happened during the first rollout. Worth re-reading before debugging a fresh deployment that looks broken in a similar way.

1. **`sudo mkdir -p` on a directory the service user must write to creates it owned by `root`.** The daemon then fails every poll with `PermissionError` trying to create a camera subfolder inside it, even though the parent and everything else looks fine. Fix: create any directory the service account writes into (like its clips folder) as a plain `mkdir`, not `sudo mkdir` -- or `chown -R` it afterward.
2. **`chown` without `-R` only changes the directory itself, not files already inside it.** A `credentials.json` created by an earlier run under a different user stays unreadable (or unwritable) by the new user even after the containing directory is reassigned. Always `chown -R` when reassigning ownership of an existing, populated directory.
3. **Copying `blink-fetch.service` from the wrong location silently keeps stale settings.** If a browser or download tool saves an updated file as `blink-fetch (1).service` instead of overwriting, a plain `cp blink-fetch.service ...` grabs the stale copy -- no error, just old `User=`/`Environment=` values persisting after a "successful" redeploy. `systemctl show blink-fetch.service -p User -p Group` is the fast way to confirm what's actually active, independent of what the file on disk is assumed to say.
4. **An unclosed `aiohttp.ClientSession` leak.** The original code created a `ClientSession()` and passed it to `Blink(session=session)`, but constructed `Auth(login_data, no_prompt=True)` *without* passing that same session -- so `Auth` silently created its own second session internally, and the one meant to be shared was never used or closed. Visible as `ERROR Unclosed client session` in the journal on every run. Fixed by passing `session=session` into `Auth(...)` explicitly so both objects share the one session.
5. **Testing email via local postfix can surface unrelated postfix misconfiguration** (e.g. a missing `/etc/postfix/generic.db` from a `smtp_generic_maps` setting) that has nothing to do with this daemon. If a test `sendmail` fails with a postfix-internal error, that's a mail-server config issue to fix independent of this code -- check `mail.log`/`journalctl` for postfix itself, not the daemon.
6. **A "no activity in N hours" alert can be completely accurate, not a bug.** The daemon correctly detected a real \~66-hour quiet stretch over a long weekend once; the alert fired partway through that window reporting the hours elapsed *at send time*. By the time someone checks the app afterward, new clips have naturally arrived and it looks like a false alarm -- it isn't. Before assuming the silence alert is broken, check the actual gap between the last clip before the alert and the first clip after it in the journal (`Found N new clip(s) in local storage manifest`); if there's a genuine multi-hour gap matching the alert, it worked correctly.
7. **`blinkpy`'s own "new clip" bookkeeping (`last_manifest_read`) is in-memory per process, not persisted.** On every service restart it reports the *entire* local-storage manifest as "new" (e.g. `Found 529 new clip(s)`), which looks alarming in the log. This is harmless: the daemon's own on-disk dedupe (`BLINK_STATE_FILE`) independently tracks what's actually been downloaded and silently skips anything already saved, regardless of what `blinkpy` itself thinks is "new" after a restart.

## Operational notes: how this actually behaves day to day

**Dedupe survives restarts, even though blinkpy's own bookkeeping doesn't.** blinkpy keeps an in-memory manifest-read cursor that resets to zero every time the process restarts, so right after a restart the log will say something like `Found 529 new clip(s)` — that's blinkpy re-reading the *entire* sync-module manifest from scratch, not the daemon re-downloading anything. The daemon's own on-disk `state.json` (`downloaded` dict, keyed by clip id/path) is the real source of truth for "have we already saved this one," and it's checked independently of blinkpy's internal state. So a big "Found N new clip(s)" line after a restart is expected and harmless — watch for actual `Downloading ...` lines to see what's really new.

**"Has N clips available for download" is a running total, not a queue.** This line reflects the total count of clips still sitting on the sync module's local storage, not a backlog the daemon is behind on. It's normal for this number to stay flat (or even repeat) across several polls with no new `Downloading` lines in between — it just means no new motion has happened, not that something is stuck. It only drops when the cleanup pass deletes old, already-downloaded clips from the sync module.

**Cleanup only ever deletes clips that are confirmed downloaded.** Once a day (`BLINK_CLEANUP_INTERVAL_HOURS`), the daemon rebuilds the sync module's manifest and deletes a clip from the USB stick only if it is both older than `BLINK_DELETE_OLDER_THAN_DAYS` and already present in the on-disk `downloaded` state. A slow or failed download can never cost the only copy — worst case, cleanup just skips that clip until it's confirmed saved.

**The three alert types, recapped:**

| Alert | Fires when | Repeat behavior | Stops when |
| --- | --- | --- | --- |
| Silence | No camera has produced a clip in `BLINK_ALERT_HOURS` (default 48) | Re-sent every `BLINK_ALERT_REPEAT_HOURS` (default 12) while still silent | A new clip arrives from any camera (no "all clear" message is sent) |
| Low battery | A camera's `battery_state` isn't `"ok"` | Re-sent at most every `BLINK_BATTERY_ALERT_REPEAT_HOURS` (default 24), per camera | That camera reports `"ok"` again |
| After-hours / weekend | A clip lands outside `BLINK_BUSINESS_START_HOUR`–`BLINK_BUSINESS_END_HOUR` on a weekday, or any time on Sat/Sun (all in `BLINK_TIMEZONE`) | Not rate-limited — every qualifying clip sends its own email | N/A (event-based, not a standing condition) |

All three are delivered the same way: logged to `journalctl -u blink-fetch.service` always, plus emailed to `BLINK_ALERT_EMAIL_TO` (currently `alerts@example.com`) from `BLINK_ALERT_EMAIL_FROM` (`blink-fetch@example.com`) via the local postfix relay on `localhost:25`, no auth. If a webhook URL is ever configured (`BLINK_ALERT_WEBHOOK_URL`), the same alert also posts there.

**A silence alert can look like a false positive in hindsight — check timestamps before assuming it's a bug.** If someone sees fresh clips in the Blink app shortly after receiving a "no activity" alert, it usually means the alert fired partway through a real gap (e.g. an \~66-hour quiet stretch from a Saturday afternoon into Tuesday morning), and activity simply resumed before anyone checked the inbox. Confirm with `journalctl -u blink-fetch.service` — look for the gap between the last `Downloading` line before the alert and the first one after. If the daemon was actually still running and healthy (no crash/restart in between), the alert was correct, just stale-feeling by the time it's read, often because of a quiet weekend. There's an open, undecided question on whether to raise `BLINK_ALERT_HOURS` above 48 (e.g. to 72–80) specifically to cut down on weekend-triggered silence alerts — check with the user before changing it; it was left as an offer, not a decision.
