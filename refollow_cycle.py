#!/usr/bin/env python3
"""
Unfollow accounts that don't follow you back, then send a follow request to
them again shortly after (unfollow -> pause -> refollow -> pause -> next).

This "refresh" cycle puts you back near the top of their notifications /
followers list, which sometimes prompts a follow back. Rapid unfollow/refollow
churn is more noticeable to Instagram's spam detection than a plain unfollow,
so this is intentionally slower and capped - use sparingly.

SAFETY FIRST:
    - Defaults to a DRY RUN: it only prints who *would* be cycled.
      Pass --confirm to actually unfollow + refollow.
    - Conservative pacing: at most RUN_LIMIT accounts per run, with a random
      30-60s pause between the unfollow and the refollow, and another random
      30-60s pause before moving to the next account.
    - Never touches anyone listed in whitelist.txt.

Reads credentials from a file (default: credentials.txt) shaped like:
    usr : your_username
    pass : your_password

Requirements:
    pip install instagrapi wcwidth

Usage:
    python3 refollow_cycle.py                 # dry run (preview only, no changes)
    python3 refollow_cycle.py --confirm        # actually unfollow + refollow
    python3 refollow_cycle.py --confirm mycreds.txt

Files:
    whitelist.txt      - usernames to NEVER touch (one per line)
    refollow_log.txt   - append-only log of every unfollow/refollow action
"""

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

import json

SESSION_FILE = Path("session.json")
CREDENTIALS_FILE = Path("credentials.txt")
WHITELIST_FILE = Path("whitelist.txt")
LOG_FILE = Path("refollow_log.txt")
PREVIEW_FILE = Path("refollow_preview.txt")

# Conservative pacing to avoid Instagram blocks.
RUN_LIMIT = 50            # max accounts cycled per run (safety throttle)
MIN_DELAY = 30            # seconds, minimum pause between actions
MAX_DELAY = 60            # seconds, maximum pause between actions
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"
COL_WIDTH = 61            # display width of the username+name column (URL starts after it)


def pad_display(text: str, width: int = COL_WIDTH) -> str:
    """Left-justify by on-screen width (handles emoji/wide unicode)."""
    w = wcswidth(text)
    if w < 0:  # text has control chars; fall back to character count
        w = len(text)
    return text + " " * max(1, width - w)


# --------------------------------------------------------------------------- #
# Login machinery (shared shape with get_followers.py / get_non_followers.py / #
# unfollow.py)                                                                 #
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
    """Finish a 2FA login using the challenge already issued by Instagram.

    Instagram doesn't report which channel it actually used to send the
    code, so when more than one is enabled on the account, ask which one
    arrived instead of guessing - a wrong guess makes Instagram reject even
    a correct code.
    """
    info = cl.last_json.get("two_factor_info", {})
    identifier = info.get("two_factor_identifier")
    phone = info.get("obfuscated_phone_number", "your phone")

    # "1" = SMS, "6" = WhatsApp, "3" = authenticator app (TOTP).
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
# Whitelist + action log helpers                                              #
# --------------------------------------------------------------------------- #
def load_whitelist(path: Path) -> set[str]:
    """Read usernames to never touch (lowercased). Missing file = empty."""
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


def log_action(account: str, action: str, username: str) -> None:
    """Append a timestamped, account-scoped record of an unfollow/refollow.

    The log is write-only: it is an audit trail of actions taken and is NOT
    used to build the target list.
    """
    url = f"https://www.instagram.com/{username}/"
    line = f"{datetime.now().strftime(TIMESTAMP_FMT)}\t{account}\t{action}\t{username}\t{url}\n"
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
    account = cl.username  # scope the log to the logged-in account

    # use_cache=False forces a fresh fetch from Instagram every run, so we never
    # act on stale follower/following data.
    print("Fetching the people you follow (live)...")
    following = cl.user_following(user_id, use_cache=False, amount=0)
    print("Fetching your followers (live)...")
    followers = cl.user_followers(user_id, use_cache=False, amount=0)

    # Non-followers, excluding only the whitelist.
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
        print("Nothing to cycle.")
        return

    if not confirm:
        lines = []
        for _, user in batch:
            left = f"{user.username}  {user.full_name or ''}".rstrip()
            url = f"https://www.instagram.com/{user.username}/"
            lines.append(f"{pad_display(left)}{url}")

        header = f"Would unfollow then refollow ({len(batch)}) - dry run, nothing changed yet"
        body = "\n".join([header, "-" * len(header), *lines])
        PREVIEW_FILE.write_text(body + "\n", encoding="utf-8")

        print("DRY RUN - no changes made. These would be unfollowed, then re-followed:\n")
        for line in lines:
            print(f"  {line}")
        print(f"\nFull list saved to {PREVIEW_FILE.resolve()}")
        print(
            f"Review it - add any usernames you want to keep to '{WHITELIST_FILE}', "
            "then run:\n"
            f"    python3 {Path(sys.argv[0]).name} --confirm"
        )
        return

    print(
        f"Cycling {len(batch)} accounts (unfollow -> {MIN_DELAY}-{MAX_DELAY}s -> "
        f"refollow -> {MIN_DELAY}-{MAX_DELAY}s -> next)...\n"
    )
    failures = 0
    for i, (uid, user) in enumerate(batch, start=1):
        try:
            cl.user_unfollow(uid)
            log_action(account, "unfollow", user.username)
            print(f"[{i}/{len(batch)}] unfollowed {user.username}")
        except PleaseWaitFewMinutes:
            print("Instagram says to wait a few minutes - stopping to stay safe.")
            break
        except Exception as exc:
            failures += 1
            print(f"[{i}/{len(batch)}] FAILED to unfollow {user.username}: {exc}")
            if failures >= 3:
                print("Too many consecutive failures - stopping to stay safe.")
                break
            continue  # skip the refollow if the unfollow itself failed

        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        try:
            cl.user_follow(uid)
            log_action(account, "refollow", user.username)
            print(f"[{i}/{len(batch)}] refollowed {user.username}")
            failures = 0
        except PleaseWaitFewMinutes:
            print("Instagram says to wait a few minutes - stopping to stay safe.")
            break
        except Exception as exc:
            failures += 1
            print(f"[{i}/{len(batch)}] FAILED to refollow {user.username}: {exc}")
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
