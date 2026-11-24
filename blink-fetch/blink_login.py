#!/usr/bin/env python3
"""
One-time interactive setup: logs into Blink (prompting for 2FA if needed)
and saves the resulting auth/refresh token to a JSON file so the daemon
script never has to log in interactively again.

Run this once, by hand, from a terminal (it needs to prompt you):

    source /opt/blink-fetch/venv/bin/activate
    python3 /opt/blink-fetch/blink_login.py

It will ask for your Blink email/username and password, and for the
2FA code Blink texts/emails you. The resulting credentials file contains
a long-lived refresh token, not your password, but treat it like a
password anyway (chmod 600, owned by the service user).
"""
import asyncio
import getpass
import os
import sys

from aiohttp import ClientSession
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import json_save

# Where the token file is written; must match BLINK_CREDENTIALS for the daemon.
CRED_PATH = os.environ.get(
    "BLINK_CREDENTIALS", "/etc/blink-fetch/credentials.json"
)


async def main():
    """Prompt for Blink credentials, complete 2FA if required, and save the
    resulting token file to CRED_PATH (mode 0600). Exits non-zero on failure."""
    username = input("Blink account email: ").strip()
    password = getpass.getpass("Blink account password: ")

    session = ClientSession()
    auth = Auth({"username": username, "password": password}, session=session, no_prompt=True)
    blink = Blink(session=session)
    blink.auth = auth

    try:
        await blink.start()
    except Exception as exc:
        # blinkpy raises BlinkTwoFARequiredError when a 2FA code is needed
        if type(exc).__name__ != "BlinkTwoFARequiredError":
            raise
        code = input("Enter the 2FA code Blink sent you: ").strip()
        ok = await blink.send_2fa_code(code)
        if not ok:
            print("2FA verification failed. Run this script again.")
            await session.close()
            sys.exit(1)

    if not blink.available:
        print("Login did not complete successfully. Check credentials and retry.")
        await session.close()
        sys.exit(1)

    os.makedirs(os.path.dirname(CRED_PATH), exist_ok=True)
    await blink.save(CRED_PATH)
    os.chmod(CRED_PATH, 0o600)
    print(f"Login successful. Credentials saved to {CRED_PATH}")

    print("\nCameras found:")
    for name, camera in blink.cameras.items():
        print(f"  - {name}")

    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
