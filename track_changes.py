#!/usr/bin/env python3
"""
Compare the current Instagram followers/following lists against the last
saved snapshot (followers_log.txt / following_log.txt) and log what changed.

Writes to followers_diff_log.txt and following_diff_log.txt, each entry in
the same tab-separated format used elsewhere in this project:

    <timestamp>\t<account>\t<action>\t<username>\t<url>

where <action> is "added" or "removed". Each run also writes a one-line
summary header before its entries:

    2026-08-09 06:30:00  shikhar_mutta_68  followers  added: 3  removed: 2

After computing the diff, followers_log.txt / following_log.txt are
overwritten with the new current snapshot, so the next run diffs against
today's state.

Requirements:
    pip install instagrapi

Usage:
    python track_changes.py [credentials_file]

    Credentials are read from a file (default: credentials.txt) shaped like:
        usr : your_username
        pass : your_password

Notes:
    - Use your own account. Scraping at high volume can get your account
      rate-limited or temporarily blocked by Instagram, so go easy.
    - A session file (session.json) is saved after the first login so you
      don't have to re-enter credentials (and trip 2FA/challenges) every run.
    - First run has nothing to diff against, so everyone shows up as
      "added" - that's expected.
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
CREDENTIALS_FILE = Path("credentials.txt")
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"

FOLLOWERS_SNAPSHOT = Path("followers_log.txt")
FOLLOWING_SNAPSHOT = Path("following_log.txt")
FOLLOWERS_DIFF_LOG = Path("followers_diff_log.txt")
FOLLOWING_DIFF_LOG = Path("following_diff_log.txt")


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


def read_snapshot(path: Path) -> set[str]:
    """Usernames from a previous followers_log.txt/following_log.txt snapshot.

    Skips the "Total ...: N" header line. Missing file means no prior
    snapshot (first run) - everything will show up as "added".
    """
    if not path.exists():
        return set()

    usernames = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 5:
            usernames.add(parts[3])
    return usernames


def write_snapshot(path: Path, header_label: str, account: str, action: str,
                    usernames: list[str], timestamp: str) -> None:
    """Overwrite a followers_log.txt/following_log.txt style snapshot."""
    lines = [
        f"{timestamp}\t{account}\t{action}\t{username}\thttps://www.instagram.com/{username}/"
        for username in usernames
    ]
    header = f"Total {header_label}: {len(lines)}"
    path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")


def diff_and_log(label: str, snapshot_path: Path, diff_log_path: Path,
                  current: dict, account: str, action: str,
                  timestamp: str) -> tuple[int, int]:
    """Diff `current` usernames against the prior snapshot and append to the diff log.

    Returns (added_count, removed_count).
    """
    previous = read_snapshot(snapshot_path)
    current_usernames = set(current.keys())

    added = sorted(current_usernames - previous, key=str.lower)
    removed = sorted(previous - current_usernames, key=str.lower)

    summary = f"{timestamp}\t{account}\t{label}\tadded: {len(added)}\tremoved: {len(removed)}"
    entries = [
        f"{timestamp}\t{account}\tadded\t{username}\thttps://www.instagram.com/{username}/"
        for username in added
    ] + [
        f"{timestamp}\t{account}\tremoved\t{username}\thttps://www.instagram.com/{username}/"
        for username in removed
    ]

    with diff_log_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join([summary, *entries]) + "\n")

    write_snapshot(
        snapshot_path, label, account, action,
        sorted(current_usernames, key=str.lower), timestamp,
    )

    return len(added), len(removed)


def main() -> None:
    credentials_file = Path(sys.argv[1]) if len(sys.argv) > 1 else CREDENTIALS_FILE
    cl = login(credentials_file)

    account = cl.username
    user_id = cl.user_id
    timestamp = datetime.now().strftime(TIMESTAMP_FMT)

    print("Fetching followers...")
    followers = cl.user_followers(user_id, amount=0)
    followers_added, followers_removed = diff_and_log(
        "followers", FOLLOWERS_SNAPSHOT, FOLLOWERS_DIFF_LOG,
        followers, account, "follower", timestamp,
    )

    print("Fetching following...")
    following = cl.user_following(user_id, amount=0)
    following_added, following_removed = diff_and_log(
        "following", FOLLOWING_SNAPSHOT, FOLLOWING_DIFF_LOG,
        following, account, "following", timestamp,
    )

    print(
        f"Followers: {followers_added} added, {followers_removed} removed "
        f"-> logged to {FOLLOWERS_DIFF_LOG.resolve()}"
    )
    print(
        f"Following: {following_added} added, {following_removed} removed "
        f"-> logged to {FOLLOWING_DIFF_LOG.resolve()}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
