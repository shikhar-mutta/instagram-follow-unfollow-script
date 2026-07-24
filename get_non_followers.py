#!/usr/bin/env python3
"""
Find Instagram accounts you follow that do NOT follow you back.

Reads credentials from a file (default: credentials.txt) shaped like:
    usr : your_username
    pass : your_password

Requirements:
    pip install instagrapi

Usage:
    python get_non_followers.py [credentials_file]

Outputs:
    following.txt       - everyone you follow
    non_followers.txt   - those you follow who don't follow you back

Notes:
    - Reuses session.json so you don't log in every run.
    - Go easy: fetching both lists for large accounts hits the API a lot
      and can get you rate-limited.
"""

import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    LoginRequired,
    TwoFactorRequired,
)
from wcwidth import wcswidth

SESSION_FILE = Path("session.json")
CREDENTIALS_FILE = Path("credentials.txt")
FOLLOWING_FILE = Path("following.txt")
NON_FOLLOWERS_FILE = Path("non_followers.txt")
COL_WIDTH = 61  # display width of the username+name column (URL starts after it)


def pad_display(text: str, width: int = COL_WIDTH) -> str:
    """Left-justify by on-screen width (handles emoji/wide unicode)."""
    w = wcswidth(text)
    if w < 0:  # text has control chars; fall back to character count
        w = len(text)
    return text + " " * max(1, width - w)


def read_credentials(path: Path) -> tuple[str, str]:
    """Read username/password from a file (key:value or plain two lines)."""
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


def complete_two_factor(cl: Client, username: str) -> None:
    """Finish a 2FA login using the challenge already issued by Instagram.

    Reuses the original two_factor_identifier (so no second code is sent) and
    picks the right verification method (authenticator app vs SMS).
    """
    info = cl.last_json.get("two_factor_info", {})
    identifier = info.get("two_factor_identifier")
    phone = info.get("obfuscated_phone_number", "your phone")

    # Diagnostic: show which 2FA channels Instagram reports as enabled.
    enabled = [k for k, v in info.items() if k.endswith("_two_factor_on") and v]
    print(f"2FA methods available: {enabled or 'unknown'}")

    # Pick the verification method Instagram actually used to send the code.
    # "6" = WhatsApp, "3" = authenticator app (TOTP), "1" = SMS text message.
    if info.get("whatsapp_two_factor_on"):
        method, where = "6", f"WhatsApp to {phone}"
    elif info.get("totp_two_factor_on"):
        method, where = "3", "authenticator app"
    else:
        method, where = "1", f"SMS to {phone}"

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

    # If credentials point to a different account, drop the old session.
    if SESSION_FILE.exists() and not session_belongs_to(username):
        print("Credentials changed; resetting saved session.")
        SESSION_FILE.unlink()

    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(username, password)
            cl.get_timeline_feed()  # validate the session is still alive
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


def write_list(path: Path, users, label: str) -> None:
    """Write a count header then fixed-width `username + name | url` lines."""
    lines = []
    for u in users:
        left = f"{u.username}  {u.full_name or ''}".rstrip()
        url = f"https://www.instagram.com/{u.username}/"
        lines.append(f"{pad_display(left)}{url}")
    lines.sort(key=str.lower)

    header = f"Total {label}: {len(lines)}"
    body = "\n".join([header, "-" * len(header), *lines])
    path.write_text(body + "\n", encoding="utf-8")


def main() -> None:
    credentials_file = Path(sys.argv[1]) if len(sys.argv) > 1 else CREDENTIALS_FILE
    cl = login(credentials_file)

    user_id = cl.user_id

    # use_cache=False forces a fresh fetch from Instagram every run.
    print("Fetching the people you follow (live)...")
    following = cl.user_following(user_id, use_cache=False, amount=0)

    print("Fetching your followers (live)...")
    followers = cl.user_followers(user_id, use_cache=False, amount=0)

    # Save the full following list.
    write_list(FOLLOWING_FILE, following.values(), "following")

    # Not following back = people you follow whose id isn't in your followers.
    non_followers = [u for uid, u in following.items() if uid not in followers]
    write_list(NON_FOLLOWERS_FILE, non_followers, "not following back")

    print(f"\nYou follow {len(following)} accounts; {len(followers)} follow you.")
    print(f"Saved all following to {FOLLOWING_FILE.resolve()}")
    print(
        f"{len(non_followers)} don't follow you back "
        f"-> saved to {NON_FOLLOWERS_FILE.resolve()}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
