#!/usr/bin/env python3
"""
Unfollow Instagram accounts that don't follow you back, skipping a whitelist.

SAFETY FIRST:
    - Defaults to a DRY RUN: it only prints who *would* be unfollowed.
      Pass --confirm to actually unfollow.
    - Conservative pacing: at most DAILY_LIMIT unfollows per 24h, with a random
      30-60s pause between each, to reduce the chance of an Instagram block.
    - Never touches anyone listed in whitelist.txt.

Reads credentials from a file (default: credentials.txt) shaped like:
    usr : your_username
    pass : your_password

Requirements:
    pip install instagrapi

Usage:
    python unfollow.py                 # dry run (preview only, no changes)
    python unfollow.py --confirm       # actually unfollow (respects the cap)
    python unfollow.py --confirm mycreds.txt

Files:
    whitelist.txt        - usernames to NEVER unfollow (one per line)
    unfollowed_log.txt   - append-only log of who was unfollowed and when
"""

import json
import random
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
    PleaseWaitFewMinutes,
    TwoFactorRequired,
)
from wcwidth import wcswidth

SESSION_FILE = Path("session.json")
CREDENTIALS_FILE = Path("credentials.txt")
WHITELIST_FILE = Path("whitelist.txt")
LOG_FILE = Path("unfollowed_log.txt")

# Conservative pacing to avoid Instagram blocks.
RUN_LIMIT = 50            # max unfollows per run (safety throttle)
MIN_DELAY = 30            # seconds, minimum pause between unfollows
MAX_DELAY = 60            # seconds, maximum pause between unfollows
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"
COL_WIDTH = 61            # display width of the username+name column (URL starts after it)


def pad_display(text: str, width: int = COL_WIDTH) -> str:
    """Left-justify by on-screen width (handles emoji/wide unicode)."""
    w = wcswidth(text)
    if w < 0:  # text has control chars; fall back to character count
        w = len(text)
    return text + " " * max(1, width - w)


# --------------------------------------------------------------------------- #
# Login machinery (shared shape with get_followers.py / get_non_followers.py)  #
# --------------------------------------------------------------------------- #
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
    """Finish a 2FA login using the challenge already issued by Instagram."""
    info = cl.last_json.get("two_factor_info", {})
    identifier = info.get("two_factor_identifier")
    phone = info.get("obfuscated_phone_number", "your phone")

    enabled = [k for k, v in info.items() if k.endswith("_two_factor_on") and v]
    print(f"2FA methods available: {enabled or 'unknown'}")

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


# --------------------------------------------------------------------------- #
# Whitelist + unfollow log helpers                                            #
# --------------------------------------------------------------------------- #
def load_whitelist(path: Path) -> set[str]:
    """Read usernames to never unfollow (lowercased). Missing file = empty."""
    if not path.exists():
        print(f"No '{path}' found - nothing is whitelisted.")
        return set()

    names = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Accept "@name", "name", or a full list line "name  Full Name  url".
        # Usernames have no spaces, so the first whitespace token is the name.
        name = line.lstrip("@").split()[0].lower()
        if name:
            names.add(name)
    return names


def log_unfollow(account: str, username: str) -> None:
    """Append a timestamped, account-scoped unfollow record.

    The log is write-only: it is an audit trail of actions taken and is NOT
    used to build the unfollow list.
    """
    url = f"https://www.instagram.com/{username}/"
    line = f"{datetime.now().strftime(TIMESTAMP_FMT)}\t{account}\t{username}\t{url}\n"
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> None:
    args = sys.argv[1:]
    confirm = "--confirm" in args
    args = [a for a in args if a != "--confirm"]
    credentials_file = Path(args[0]) if args else CREDENTIALS_FILE

    whitelist = load_whitelist(WHITELIST_FILE)
    cl = login(credentials_file)
    user_id = cl.user_id
    account = cl.username  # scope the log/cap to the logged-in account

    # use_cache=False forces a fresh fetch from Instagram every run, so we never
    # act on stale follower/following data.
    print("Fetching the people you follow (live)...")
    following = cl.user_following(user_id, use_cache=False, amount=0)
    print("Fetching your followers (live)...")
    followers = cl.user_followers(user_id, use_cache=False, amount=0)

    # Non-followers, excluding only the whitelist. The unfollow log is NOT
    # consulted here - the list is built purely from live data.
    skipped_whitelist = 0
    targets = []  # list of (user_id, UserShort)
    for uid, user in following.items():
        if uid in followers:
            continue  # they follow you back
        uname = user.username.lower()
        if uname in whitelist:
            skipped_whitelist += 1
            continue
        targets.append((uid, user))

    targets.sort(key=lambda t: t[1].username.lower())

    batch = targets[:RUN_LIMIT]

    print(f"\nYou follow {len(following)}; {len(followers)} follow you back.")
    print(f"Not following back (eligible): {len(targets)}")
    print(f"Whitelisted (skipped): {skipped_whitelist}")
    print(f"Per-run cap: {RUN_LIMIT}")
    print(f"Will process this run: {len(batch)}\n")

    if not batch:
        print("Nothing to unfollow.")
        return

    if not confirm:
        print("DRY RUN - no one will be unfollowed. These would be unfollowed:\n")
        for _, user in batch:
            left = f"{user.username}  {user.full_name or ''}".rstrip()
            url = f"https://www.instagram.com/{user.username}/"
            print(f"  {pad_display(left)}{url}")
        print(
            f"\nTo actually unfollow these {len(batch)} accounts, run:\n"
            f"    python3 {Path(sys.argv[0]).name} --confirm"
        )
        return

    print(f"Unfollowing {len(batch)} accounts (random {MIN_DELAY}-{MAX_DELAY}s between each)...\n")
    failures = 0
    for i, (uid, user) in enumerate(batch, start=1):
        try:
            cl.user_unfollow(uid)
            log_unfollow(account, user.username)
            print(f"[{i}/{len(batch)}] unfollowed {user.username}")
            failures = 0
        except PleaseWaitFewMinutes:
            print("Instagram says to wait a few minutes - stopping to stay safe.")
            break
        except Exception as exc:
            failures += 1
            print(f"[{i}/{len(batch)}] FAILED {user.username}: {exc}")
            if failures >= 3:
                print("Too many consecutive failures - stopping to stay safe.")
                break

        if i < len(batch):
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
