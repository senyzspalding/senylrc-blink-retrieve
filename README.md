# senylrc-blink-retrieve

A small daemon that downloads motion clips from Blink cameras (Sync Module 2
local storage) to a local folder, and emails alerts when something looks wrong.

## What it does

- Polls Blink every `BLINK_POLL_SECONDS` and saves new clips to
  `<BLINK_OUTPUT_DIR>/<camera>/<camera>_<YYYYmmdd-HHMMSS>.mp4` (local time).
- Remembers what it has downloaded in a state file, so restarts do not
  re-download history.
- Deletes clips from the sync module's USB stick once they are older than
  `BLINK_DELETE_OLDER_THAN_DAYS` **and** confirmed saved locally.
- Alerts (email and/or webhook) on:
  - no clips from any camera for `BLINK_ALERT_HOURS`
  - a camera battery state other than "ok"
  - any clip recorded on a weekend or outside business hours

## Files

| File | Purpose |
|------|---------|
| `blink-fetch/blink_login.py` | One-time interactive login (password + 2FA); saves a refresh-token file |
| `blink-fetch/blink_fetch_daemon.py` | The long-running poller |
| `blink-fetch/blink-fetch.service` | systemd unit with the deployed configuration |
| `Blink Camera Clip Fetcher — Claude Code Setup Reference.md` | Full setup and operations notes |

## Setup

```bash
python3 -m venv /opt/blink-fetch/venv
/opt/blink-fetch/venv/bin/pip install blinkpy aiohttp
/opt/blink-fetch/venv/bin/python3 /opt/blink-fetch/blink_login.py   # once, interactive
sudo cp blink-fetch/blink-fetch.service /etc/systemd/system/
sudo systemctl enable --now blink-fetch.service
journalctl -u blink-fetch.service -f
```

The credentials file (default `/etc/blink-fetch/credentials.json`) holds a
refresh token; keep it `chmod 600` and never commit it.

## Configuration

All settings are environment variables. The docstring at the top of
`blink_fetch_daemon.py` is the authoritative list with defaults; the shipped
values are in `blink-fetch.service`.

If the daemon logs that login failed with saved credentials, the refresh token
has expired: re-run `blink_login.py`.
