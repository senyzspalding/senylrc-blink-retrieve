#!/usr/bin/env python3
"""
Long-running daemon that polls Blink for new clips (both cloud-listed
motion clips and Sync Module 2 local-storage clips) and downloads any
new ones to a local folder on this machine.

Runs as a systemd service (see blink-fetch.service). It stays alive
in one process so it can track "have I already downloaded this clip"
across polls without hammering the manifest from scratch, but it also
keeps a small state file on disk so a restart doesn't re-download the
whole clip history.

Config is via environment variables (set them in the systemd unit or
an EnvironmentFile):
    BLINK_CREDENTIALS   path to the credentials.json from blink_login.py
                        (default: /etc/blink-fetch/credentials.json)
    BLINK_OUTPUT_DIR    where to save clips (default: /srv/blink-clips)
    BLINK_STATE_FILE    dedupe state file (default: /var/lib/blink-fetch/state.json)
    BLINK_POLL_SECONDS  how often to check for new clips (default: 120)
    BLINK_KEEP_DAYS     how long to remember downloaded-clip timestamps
                        for dedupe purposes (default: 14)

    BLINK_DELETE_OLDER_THAN_DAYS
                        delete clips from the sync module's USB stick once
                        they are older than this many days AND already
                        confirmed downloaded to BLINK_OUTPUT_DIR (default:
                        14; set to 0 to never delete from the sync module)
    BLINK_CLEANUP_INTERVAL_HOURS
                        how often to run that cleanup pass (default: 24 --
                        no need to hammer the sync module every poll)

    BLINK_ALERT_HOURS   hours of total silence (no clip from ANY camera)
                        before alerting (default: 48; 0 disables alerting)
    BLINK_ALERT_REPEAT_HOURS
                        re-send the alert every this many hours while
                        still silent, so one missed alert isn't the only
                        one (default: 12)
    BLINK_ALERT_WEBHOOK_URL
                        if set, POST a small JSON payload here on alert
                        (works with Slack/Discord incoming webhooks, or
                        any endpoint that accepts {"text": "..."})
    BLINK_ALERT_EMAIL_TO, BLINK_ALERT_EMAIL_FROM,
    BLINK_SMTP_HOST, BLINK_SMTP_PORT, BLINK_SMTP_USER, BLINK_SMTP_PASS
                        if BLINK_ALERT_EMAIL_TO is set, send an email
                        alert via this SMTP server (STARTTLS on the
                        given port; leave USER/PASS unset for an open
                        relay)

    BLINK_BATTERY_ALERT_REPEAT_HOURS
                        alert (via the same webhook/email as above) when
                        a camera's own battery_state isn't "ok", then
                        re-alert at most this often per camera while it
                        stays that way (default: 24; 0 disables this
                        check). Piggybacks on data already fetched each
                        poll, so there's no extra API cost.

    BLINK_TIMEZONE      IANA timezone used both for the timestamp in
                        saved clip filenames and for deciding what counts
                        as "business hours" below (default: America/New_York
                        -- shows as EST or EDT automatically depending on
                        the date, which is what you want for correct wall
                        clock times year-round)
    BLINK_AFTER_HOURS_ALERT
                        "true" to email/webhook-alert on EVERY clip that
                        lands on a weekend, or on a weekday outside
                        BLINK_BUSINESS_START_HOUR-BLINK_BUSINESS_END_HOUR
                        (default: true; set "false" to disable)
    BLINK_BUSINESS_START_HOUR, BLINK_BUSINESS_END_HOUR
                        24-hour local start/end of business hours on
                        weekdays (default: 7 and 18, i.e. 7am-6pm;
                        anything outside that, plus all of Sat/Sun,
                        triggers BLINK_AFTER_HOURS_ALERT)
"""
import asyncio
import json
import logging
import os
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from aiohttp import ClientSession
from blinkpy import api
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import json_load

CRED_PATH = os.environ.get("BLINK_CREDENTIALS", "/etc/blink-fetch/credentials.json")
OUTPUT_DIR = os.environ.get("BLINK_OUTPUT_DIR", "/srv/blink-clips")
STATE_PATH = os.environ.get("BLINK_STATE_FILE", "/var/lib/blink-fetch/state.json")
POLL_SECONDS = int(os.environ.get("BLINK_POLL_SECONDS", "120"))
KEEP_DAYS = int(os.environ.get("BLINK_KEEP_DAYS", "14"))
DELETE_OLDER_THAN_DAYS = float(os.environ.get("BLINK_DELETE_OLDER_THAN_DAYS", "14"))
CLEANUP_INTERVAL_HOURS = float(os.environ.get("BLINK_CLEANUP_INTERVAL_HOURS", "24"))

LOCAL_TZ = ZoneInfo(os.environ.get("BLINK_TIMEZONE", "America/New_York"))
AFTER_HOURS_ALERT = os.environ.get("BLINK_AFTER_HOURS_ALERT", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
BUSINESS_START_HOUR = int(os.environ.get("BLINK_BUSINESS_START_HOUR", "7"))
BUSINESS_END_HOUR = int(os.environ.get("BLINK_BUSINESS_END_HOUR", "18"))

ALERT_HOURS = float(os.environ.get("BLINK_ALERT_HOURS", "48"))
ALERT_REPEAT_HOURS = float(os.environ.get("BLINK_ALERT_REPEAT_HOURS", "12"))
ALERT_WEBHOOK_URL = os.environ.get("BLINK_ALERT_WEBHOOK_URL", "").strip()
ALERT_EMAIL_TO = os.environ.get("BLINK_ALERT_EMAIL_TO", "").strip()
ALERT_EMAIL_FROM = os.environ.get("BLINK_ALERT_EMAIL_FROM", "blink-fetch@localhost")
SMTP_HOST = os.environ.get("BLINK_SMTP_HOST", "localhost")
SMTP_PORT = int(os.environ.get("BLINK_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("BLINK_SMTP_USER", "")
SMTP_PASS = os.environ.get("BLINK_SMTP_PASS", "")

BATTERY_ALERT_REPEAT_HOURS = float(
    os.environ.get("BLINK_BATTERY_ALERT_REPEAT_HOURS", "24")
)

logging.basicConfig(
    level=os.environ.get("BLINK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("blink-fetch")


def slugify(name: str) -> str:
    """Make a camera name filesystem-safe: runs of characters outside
    A-Z a-z 0-9 . _ - become "_"; falls back to "camera" if nothing is left."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "camera"


def load_state() -> dict:
    """Load the dedupe/alert state from STATE_PATH.

    Returns a dict with keys: downloaded (clip key -> ISO time), last_clip_at,
    last_alert_at and battery_alerts. A missing or corrupt file yields fresh
    defaults rather than an error."""
    state = {
        "downloaded": {},
        "last_clip_at": None,
        "last_alert_at": None,
        "battery_alerts": {},
    }
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r") as f:
                state.update(json.load(f))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read state file (%s), starting fresh", exc)
    return state


def save_state(state: dict):
    """Persist state atomically (write a .tmp file, then os.replace) so a
    crash mid-write can never leave a truncated state file."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def prune_state(state: dict):
    """Forget downloaded-clip entries older than KEEP_DAYS (and entries with
    unparseable timestamps) so the state file does not grow forever."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    kept = {}
    for key, iso_time in state["downloaded"].items():
        try:
            ts = datetime.fromisoformat(iso_time)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts >= cutoff:
            kept[key] = iso_time
    state["downloaded"] = kept


def _bump_last_clip(state: dict, iso_time: str):
    """Advance state["last_clip_at"] if iso_time is newer (ISO strings sort
    chronologically). Feeds the silence alert in check_silence()."""
    current = state.get("last_clip_at")
    if current is None or iso_time > current:
        state["last_clip_at"] = iso_time


async def delete_clip_from_sync_module(blink: Blink, clip_path: str) -> bool:
    """Delete a clip from the sync module's local storage (the USB stick).

    Mirrors blinkpy's own LocalStorageMediaItem.delete_video(): the
    request/download URL and the delete URL are the same path with
    "request" swapped for "delete".
    """
    delete_path = clip_path.replace("request", "delete")
    url = blink.urls.base_url + delete_path
    try:
        resp = await api.http_post(blink, url, json=False)
        if resp.status == 200:
            return True
        log.warning("Delete failed (status %s) for %s", resp.status, clip_path)
        return False
    except Exception:
        log.exception("Error deleting clip %s from sync module", clip_path)
        return False


def send_webhook_alert(message: str):
    """POST {"text": message} to BLINK_ALERT_WEBHOOK_URL, if configured.
    Failures are logged, never raised, so one bad channel cannot block others."""
    if not ALERT_WEBHOOK_URL:
        return
    import urllib.request

    body = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(
        ALERT_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception:
        log.exception("Failed to POST alert webhook")


def send_email_alert(message: str, subject: str = "Blink alert"):
    """Email an alert to BLINK_ALERT_EMAIL_TO via the configured SMTP server.

    With BLINK_SMTP_USER set, a certificate-verified STARTTLS session is
    required before logging in; if it cannot be established the alert is not
    sent, so credentials and alert text never cross the network in plaintext.
    Without auth (typical local postfix relay), STARTTLS is tried and plain
    delivery is used as a fallback. Logs on failure instead of raising."""
    if not ALERT_EMAIL_TO:
        return
    msg = MIMEText(message)
    msg["Subject"] = subject
    msg["From"] = ALERT_EMAIL_FROM
    msg["To"] = ALERT_EMAIL_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            if SMTP_USER:
                # Never authenticate without verified TLS. Any failure here
                # (no STARTTLS offered, bad/self-signed cert) raises and the
                # alert is logged as failed instead of leaking credentials.
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(SMTP_USER, SMTP_PASS)
            else:
                try:
                    smtp.starttls()
                    smtp.ehlo()
                except Exception:
                    # STARTTLS not offered, or a self-signed cert on a local
                    # relay -- no credentials are sent, so fall back to plain
                    # delivery rather than aborting the whole alert.
                    pass
            smtp.sendmail(ALERT_EMAIL_FROM, [ALERT_EMAIL_TO], msg.as_string())
    except Exception:
        log.exception("Failed to send alert email")


def send_alert(message: str, subject: str = "Blink alert"):
    """Log an alert and fan it out to every configured channel (webhook, email)."""
    log.warning("ALERT: %s", message)
    send_webhook_alert(message)
    send_email_alert(message, subject=subject)


def is_after_hours(local_dt: datetime) -> bool:
    """True for any weekend time, or a weekday time outside
    BLINK_BUSINESS_START_HOUR-BLINK_BUSINESS_END_HOUR."""
    if local_dt.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return True
    return not (BUSINESS_START_HOUR <= local_dt.hour < BUSINESS_END_HOUR)


def maybe_alert_after_hours(camera_name: str, local_dt: datetime):
    """Send an alert for a new clip if after-hours alerting is enabled and
    local_dt falls on a weekend or outside business hours."""
    if not AFTER_HOURS_ALERT:
        return
    if not is_after_hours(local_dt):
        return
    when_str = local_dt.strftime("%A %Y-%m-%d %I:%M %p %Z")
    send_alert(
        f"Blink camera '{camera_name}' recorded activity outside business "
        f"hours: {when_str}.",
        subject=f"Blink after-hours activity: {camera_name}",
    )


def check_silence(state: dict):
    """Alert if no clip from any camera has landed in BLINK_ALERT_HOURS."""
    if ALERT_HOURS <= 0:
        return

    now = datetime.now(timezone.utc)
    last_clip_at = state.get("last_clip_at")

    if last_clip_at is None:
        # First run: don't alarm immediately, just start the clock.
        state["last_clip_at"] = now.isoformat()
        return

    try:
        last_dt = datetime.fromisoformat(last_clip_at)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        state["last_clip_at"] = now.isoformat()
        return

    silent_hours = (now - last_dt).total_seconds() / 3600
    if silent_hours < ALERT_HOURS:
        return

    last_alert_at = state.get("last_alert_at")
    if last_alert_at:
        try:
            last_alert_dt = datetime.fromisoformat(last_alert_at)
            if last_alert_dt.tzinfo is None:
                last_alert_dt = last_alert_dt.replace(tzinfo=timezone.utc)
            hours_since_alert = (now - last_alert_dt).total_seconds() / 3600
            if hours_since_alert < ALERT_REPEAT_HOURS:
                return
        except ValueError:
            pass

    send_alert(
        f"No Blink clips from any camera in over {silent_hours:.1f} hours "
        f"(last clip: {last_dt.isoformat()}). Check that the cameras, "
        f"sync module and this fetcher are all online.",
        subject="Blink alert: no recent activity",
    )
    state["last_alert_at"] = now.isoformat()


def check_battery_health(blink: Blink, state: dict):
    """Alert if any camera's own battery_state isn't "ok".

    Uses camera data already fetched this poll (via blink.refresh() in
    fetch_new_clips) -- no extra requests to Blink's API.
    """
    if BATTERY_ALERT_REPEAT_HOURS <= 0:
        return

    now = datetime.now(timezone.utc)
    battery_alerts = state.setdefault("battery_alerts", {})

    for name, camera in blink.cameras.items():
        status = (camera.battery_state or "").strip().lower()
        if not status:
            # Unknown/not reported -- don't guess, don't alert.
            continue

        if status == "ok":
            battery_alerts.pop(name, None)
            continue

        last = battery_alerts.get(name)
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                hours_since = (now - last_dt).total_seconds() / 3600
                if hours_since < BATTERY_ALERT_REPEAT_HOURS:
                    continue
            except ValueError:
                pass

        details = []
        if camera.battery_level is not None:
            details.append(f"level={camera.battery_level}")
        if camera.battery_voltage is not None:
            details.append(f"voltage={camera.battery_voltage / 100:.2f}V")
        detail_str = f" ({', '.join(details)})" if details else ""

        send_alert(
            f"Blink camera '{name}' reports battery status "
            f"'{status}'{detail_str}. Consider replacing/recharging it.",
            subject=f"Blink alert: low battery ({name})",
        )
        battery_alerts[name] = now.isoformat()


async def build_blink() -> Blink:
    """Create an authenticated Blink client from the saved credentials file.

    Raises RuntimeError if login fails (e.g. expired refresh token), in which
    case blink_login.py must be re-run by hand."""
    session = ClientSession()
    login_data = await json_load(CRED_PATH)
    auth = Auth(login_data, session=session, no_prompt=True)
    blink = Blink(session=session)
    blink.auth = auth
    ok = await blink.start()
    if not ok:
        await session.close()
        raise RuntimeError(
            "Blink login failed using saved credentials. "
            "Re-run blink_login.py to refresh them."
        )
    return blink


async def fetch_new_clips(blink: Blink, state: dict):
    """Refresh Blink and download every clip not already in state["downloaded"].

    Clips are saved as <OUTPUT_DIR>/<camera>/<camera>_<local YYYYmmdd-HHMMSS>.mp4,
    written to a .part file first and renamed so partial downloads are never
    mistaken for complete ones. A clip is recorded as downloaded only after
    it is confirmed on disk, which is what makes later deletion safe."""
    await blink.refresh(force=True)

    for sync_name, sync_module in blink.sync.items():
        for camera_name, records in sync_module.last_records.items():
            for record in records:
                iso_time = record["time"]
                key = f"{sync_name}|{camera_name}|{iso_time}"
                if key in state["downloaded"]:
                    continue

                clip_path = record["clip"]
                cam_dir = os.path.join(OUTPUT_DIR, slugify(camera_name))
                os.makedirs(cam_dir, exist_ok=True)

                try:
                    dt = datetime.fromisoformat(iso_time)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    dt = datetime.now(timezone.utc)
                local_dt = dt.astimezone(LOCAL_TZ)
                fname = f"{slugify(camera_name)}_{local_dt.strftime('%Y%m%d-%H%M%S')}.mp4"
                out_path = os.path.join(cam_dir, fname)

                # This key hasn't been seen before (checked above), so
                # this is a genuinely new clip -- worth an after-hours
                # check regardless of which branch below saves it.
                maybe_alert_after_hours(camera_name, local_dt)

                if os.path.exists(out_path):
                    state["downloaded"][key] = iso_time
                    _bump_last_clip(state, iso_time)
                    continue

                log.info("Downloading %s -> %s", clip_path, out_path)
                try:
                    resp = await blink.do_http_get(clip_path)
                    if resp.status != 200:
                        log.warning(
                            "Download failed (status %s) for %s", resp.status, clip_path
                        )
                        continue
                    data = await resp.read()
                    tmp_path = out_path + ".part"
                    with open(tmp_path, "wb") as f:
                        f.write(data)
                    os.replace(tmp_path, out_path)
                    # Only mark as done (and eligible for later deletion
                    # from the sync module) once the file is confirmed on
                    # disk -- cleanup_old_clips() is what actually deletes
                    # it, once it's past BLINK_DELETE_OLDER_THAN_DAYS.
                    state["downloaded"][key] = iso_time
                    _bump_last_clip(state, iso_time)
                except Exception:
                    log.exception("Error downloading clip for %s", camera_name)


async def cleanup_old_clips(blink: Blink, state: dict):
    """Delete clips from each sync module's USB stick once they're older
    than BLINK_DELETE_OLDER_THAN_DAYS -- but only clips we've already
    confirmed are saved to BLINK_OUTPUT_DIR, so a slow or failed download
    never loses the only copy of a clip.
    """
    if DELETE_OLDER_THAN_DAYS <= 0:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=DELETE_OLDER_THAN_DAYS)

    for sync_name, sync_module in blink.sync.items():
        await sync_module.update_local_storage_manifest()
        # blinkpy doesn't expose the manifest publicly; this mirrors what
        # check_new_videos() itself reads internally.
        manifest = list(sync_module._local_storage.get("manifest", []))
        manifest_id = sync_module._local_storage.get("last_manifest_id")

        for item in manifest:
            created = item.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= cutoff:
                continue

            camera_name = item.name
            iso_time = item.created_at.isoformat()
            key = f"{sync_name}|{camera_name}|{iso_time}"

            if key not in state["downloaded"]:
                log.info(
                    "Not deleting %s clip from %s (not yet confirmed "
                    "downloaded)",
                    camera_name,
                    iso_time,
                )
                continue

            clip_path = item.url(manifest_id)
            if await delete_clip_from_sync_module(blink, clip_path):
                log.info(
                    "Deleted clip older than %s day(s) from sync module: "
                    "%s %s",
                    DELETE_OLDER_THAN_DAYS,
                    camera_name,
                    iso_time,
                )


def cleanup_due(state: dict) -> bool:
    """True if the sync-module cleanup has never run or CLEANUP_INTERVAL_HOURS
    have passed since state["last_cleanup_at"]."""
    last = state.get("last_cleanup_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    return hours_since >= CLEANUP_INTERVAL_HOURS


async def main():
    """Entry point: log in, then poll forever.

    Each cycle downloads new clips, runs the silence and battery checks, runs
    the periodic cleanup, saves state and refreshes the credentials file.
    After 5 consecutive failed cycles the Blink session is rebuilt."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    state = load_state()

    blink = await build_blink()
    log.info(
        "Logged in. Watching %d camera(s): %s",
        len(blink.cameras),
        ", ".join(blink.cameras.keys()),
    )

    consecutive_errors = 0
    while True:
        loop_start = time.monotonic()
        try:
            await fetch_new_clips(blink, state)
            check_silence(state)
            check_battery_health(blink, state)

            if cleanup_due(state):
                try:
                    await cleanup_old_clips(blink, state)
                except Exception:
                    log.exception("Error during sync-module cleanup pass")
                state["last_cleanup_at"] = datetime.now(timezone.utc).isoformat()

            prune_state(state)
            save_state(state)
            # Keep the saved refresh token current in case it rotated.
            await blink.save(CRED_PATH)
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            log.exception("Error during poll (failure #%d)", consecutive_errors)
            if consecutive_errors >= 5:
                # Something is likely wrong with the session (e.g. expired
                # refresh token); rebuild it from scratch on the next loop.
                log.warning("Too many consecutive errors, rebuilding Blink session")
                try:
                    await blink.auth.session.close()
                except Exception:
                    pass
                try:
                    blink = await build_blink()
                except Exception:
                    log.exception("Could not rebuild Blink session")
                consecutive_errors = 0

        elapsed = time.monotonic() - loop_start
        await asyncio.sleep(max(1, POLL_SECONDS - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
