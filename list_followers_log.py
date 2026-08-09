#!/usr/bin/env python3
"""
Fetch all Instagram followers of the logged-in account and append them to a
log file in the same tab-separated format used by refollow_cycle.py:

    <timestamp>\t<account>\t<action>\t<username>\t<url>

Requirements:
    pip install instagrapi

Usage:
    python list_followers_log.py [credentials_file]

    Credentials are read from a file (default: credentials.txt) shaped like:
        usr : your_username
        pass : your_password

Notes:
    - Use your own account. Scraping at high volume can get your account
      rate-limited or temporarily blocked by Instagram, so go easy.
    - A session file (session.json) is saved after the first login so you
      don't have to re-enter credentials (and trip 2FA/challenges) every run.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    LoginRequired,
    TwoFactorRequired,
)

SESSION_FILE = Path("session.json")
LOG_FILE = Path("followers_log.txt")
CREDENTIALS_FILE = Path("credentials.txt")
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"
ACTION = "follower"


def complete_two_factor(cl: Client, username: str) -> None:
    """Finish a 2FA login using the challenge already issued by Instagram.

    Reuses the original two_factor_identifier (so no second code is sent).
    Instagram doesn't report which channel it actually used to send the
    code, so when more than one is enabled on the account, ask which one
    arrived instead of guessing - a wrong guess makes Instagram reject even
    a correct code.
    """
    info = cl.last_json.get("two_factor_info", {})
    identifier = info.get("two_factor_identifier")
    phone = info.get("obfuscated_phone_number", "your phone")

    channels = []
    if info.get("sms_two_factor_on"):
        channels.append(("1", f"SMS to {phone}"))
    if info.get("whatsapp_two_factor_on"):
        channels.append(("6", f"WhatsApp to {phone}"))
    if info.get("totp_two_factor_on"):
        channels.append(("3", "authenticator app"))
    if not channels:
        channels = [("1", f"SMS to {phone}")]

    if len(channels) == 1:
        method, where = channels[0]
    else:
        print("Instagram may have sent the code via any of these channels:")
        for i, (_, label) in enumerate(channels, start=1):
            print(f"  {i}. {label}")
        choice = input(f"Which one did you receive? [1-{len(channels)}, default 1]: ").strip()
        idx = int(choice) - 1 if choice.isdigit() and 1 <= int(choice) <= len(channels) else 0
        method, where = channels[idx]

    code = input(f"Enter the 2FA code ({where}): ").strip()
    data = {
        "verification_code": code,
        "phone_id": cl.phone_id,
        "_csrftoken": cl.token,
        "two_factor_identifier": identifier,
        "username": username,
        "trust_this_device": "1",
        "guid": cl.uuid,
        "device_id": cl.android_device_id,
        "waterfall_id": str(uuid4()),
        "verification_method": method,
    }
    cl.private_request("accounts/two_factor_login/", data, login=True)
    cl.authorization_data = cl.parse_authorization(
        cl.last_response.headers.get("ig-set-authorization")
    )
    cl.login_flow()
    cl.last_login = time.time()


def read_credentials(path: Path) -> tuple[str, str]:
    """Read username/password from a file.

    Accepts either:
        usr : username          or    username
        pass : password               password
    Keys recognised: usr/user/username and pass/password (case-insensitive).
    """
    if not path.exists():
        sys.exit(
            f"Credentials file '{path}' not found.\n"
            "Create it with two lines:\n"
            "    usr : your_username\n"
            "    pass : your_password"
        )

    username = password = None
    positional = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        for sep in (":", "="):
            if sep in line:
                key, value = line.split(sep, 1)
                key, value = key.strip().lower(), value.strip()
                if key in ("usr", "user", "username"):
                    username = value
                elif key in ("pass", "password", "pwd"):
                    password = value
                break
        else:
            positional.append(line)

    if username is None and positional:
        username = positional[0]
    if password is None and len(positional) > 1:
        password = positional[1]

    if not username or not password:
        sys.exit(f"Could not read both username and password from '{path}'.")

    return username, password


def session_belongs_to(username: str) -> bool:
    """True if the saved session was created for this username."""
    try:
        saved = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return saved.get("_account_username") == username
    except Exception:
        return False


def save_session(cl: Client, username: str) -> None:
    """Persist the session, tagged with the account it belongs to."""
    settings = cl.get_settings()
    settings["_account_username"] = username
    SESSION_FILE.write_text(json.dumps(settings), encoding="utf-8")


def login(credentials_file: Path) -> Client:
    """Log in to Instagram, reusing a saved session when possible."""
    cl = Client()

    username, password = read_credentials(credentials_file)

    if SESSION_FILE.exists() and not session_belongs_to(username):
        print("Credentials changed; resetting saved session.")
        SESSION_FILE.unlink()

    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(username, password)
            cl.get_timeline_feed()
            print("Reused existing session.")
            return cl
        except (LoginRequired, Exception):
            print("Saved session expired, logging in fresh...")
            cl = Client()

    try:
        cl.login(username, password)
    except TwoFactorRequired:
        complete_two_factor(cl, username)
    except BadPassword:
        sys.exit("Login failed: incorrect password.")
    except ChallengeRequired:
        sys.exit(
            "Instagram issued a challenge (suspicious login). "
            "Approve the login in the Instagram app, then run again."
        )

    save_session(cl, username)
    print("Logged in and saved session.")
    return cl


def main() -> None:
    credentials_file = Path(sys.argv[1]) if len(sys.argv) > 1 else CREDENTIALS_FILE
    cl = login(credentials_file)

    account = cl.username
    user_id = cl.user_id
    print("Fetching followers (this can take a while for large accounts)...")

    followers = cl.user_followers(user_id, amount=0)

    timestamp = datetime.now().strftime(TIMESTAMP_FMT)
    lines = []
    for follower in followers.values():
        url = f"https://www.instagram.com/{follower.username}/"
        lines.append(f"{timestamp}\t{account}\t{ACTION}\t{follower.username}\t{url}")

    header = f"Total followers: {len(lines)}"
    body = "\n".join([header, *lines])
    LOG_FILE.write_text(body + "\n", encoding="utf-8")

    print(f"Saved {len(lines)} followers to {LOG_FILE.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
