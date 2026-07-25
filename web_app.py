#!/usr/bin/env python3
"""
Local Streamlit web UI for this project: log in, browse the accounts that
don't follow you back as a thumbnail grid, multi-select them, and bulk
unfollow or unfollow+refollow.

Run:
    pip install streamlit instagrapi wcwidth
    streamlit run web_app.py

This is a local, single-user tool - it binds to localhost by default and
holds your live Instagram session in memory. Don't expose it beyond your own
machine. It reuses the same credentials.txt / session.json / whitelist.txt
and log files as the CLI scripts (get_followers.py, unfollow.py,
refollow_cycle.py), so whatever you whitelist there is respected here too.
"""

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import requests
import streamlit as st
from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    PleaseWaitFewMinutes,
    TwoFactorRequired,
)

SESSION_FILE = Path("session.json")
CREDENTIALS_FILE = Path("credentials.txt")
WHITELIST_FILE = Path("whitelist.txt")
UNFOLLOW_LOG_FILE = Path("unfollowed_log.txt")
REFOLLOW_LOG_FILE = Path("refollow_log.txt")

DEFAULT_RUN_LIMIT = 50
DEFAULT_MIN_DELAY = 30
DEFAULT_MAX_DELAY = 60
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"
GRID_COLUMNS = 3
THUMB_WIDTH = 120
PAGE_SIZE = 50  # accounts shown per page

st.set_page_config(page_title="IG Follow/Unfollow", page_icon="📷", layout="wide")


# --------------------------------------------------------------------------- #
# Shared helpers (same behavior/file formats as the CLI scripts)              #
# --------------------------------------------------------------------------- #
def read_credentials_file(path: Path) -> tuple[str, str]:
    """Best-effort prefill for the login form. Returns ("", "") if missing."""
    if not path.exists():
        return "", ""
    username = password = ""
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
    if not username and positional:
        username = positional[0]
    if not password and len(positional) > 1:
        password = positional[1]
    return username, password


def session_belongs_to(username: str) -> bool:
    try:
        saved = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return saved.get("_account_username") == username
    except Exception:
        return False


def save_session(cl: Client, username: str) -> None:
    settings = cl.get_settings()
    settings["_account_username"] = username
    SESSION_FILE.write_text(json.dumps(settings), encoding="utf-8")


def load_whitelist() -> set[str]:
    if not WHITELIST_FILE.exists():
        return set()
    names = set()
    for raw in WHITELIST_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.lstrip("@").split()[0].lower()
        if name:
            names.add(name)
    return names


def add_to_whitelist(username: str) -> None:
    """Append a username to whitelist.txt, unless it's already there."""
    if username.lower() in load_whitelist():
        return
    with WHITELIST_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"{username}\n")


def list_whitelist_entries() -> list[str]:
    """Usernames currently in whitelist.txt, original casing, de-duplicated."""
    if not WHITELIST_FILE.exists():
        return []
    seen: set[str] = set()
    names = []
    for raw in WHITELIST_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.lstrip("@").split()[0]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return sorted(names, key=str.lower)


def remove_from_whitelist(username: str) -> None:
    """Remove a username from whitelist.txt (comments/blank lines untouched)."""
    if not WHITELIST_FILE.exists():
        return
    kept = []
    for raw in WHITELIST_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and line.lstrip("@").split()[0].lower() == username.lower():
            continue
        kept.append(raw)
    WHITELIST_FILE.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def fetch_thumbnail(cl: Client, url: str) -> tuple[bytes | None, str | None]:
    """Fetch a profile picture's bytes server-side instead of handing the raw
    URL to the browser (Instagram's CDN frequently rejects hotlinked/browser
    requests). Tries instagrapi's own session first (matches the Instagram
    app's headers), then falls back to a plain browser-like request.

    Returns (bytes, None) on success, or (None, reason) on failure - the
    reason is shown in the UI so a real failure cause can be seen instead of
    silently guessing.
    """
    try:
        resp = cl.private.get(url, timeout=8)
        if resp.status_code == 200 and resp.content:
            return resp.content, None
        primary_error = f"HTTP {resp.status_code} (instagrapi session)"
    except Exception as exc:
        primary_error = f"{type(exc).__name__}: {exc} (instagrapi session)"

    try:
        resp = requests.get(
            url,
            timeout=8,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            },
        )
        if resp.status_code == 200 and resp.content:
            return resp.content, None
        return None, f"{primary_error}; HTTP {resp.status_code} (plain request)"
    except Exception as exc:
        return None, f"{primary_error}; {type(exc).__name__}: {exc} (plain request)"


def log_action(log_file: Path, account: str, action: str, username: str) -> None:
    """Append-only audit trail, same format as the CLI scripts."""
    url = f"https://www.instagram.com/{username}/"
    line = f"{datetime.now().strftime(TIMESTAMP_FMT)}\t{account}\t{action}\t{username}\t{url}\n"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(line)


def two_factor_channels(cl: Client) -> list[tuple[str, str]]:
    """Channels Instagram reports as enabled. "1"=SMS, "6"=WhatsApp, "3"=TOTP."""
    info = cl.last_json.get("two_factor_info", {})
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
    return channels


def submit_two_factor(cl: Client, username: str, method: str, code: str) -> None:
    info = cl.last_json.get("two_factor_info", {})
    identifier = info.get("two_factor_identifier")
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
    save_session(cl, username)


def attempt_session_reuse(username: str, password: str) -> Client | None:
    """Try the saved session first, like the CLI scripts do."""
    if not (SESSION_FILE.exists() and session_belongs_to(username)):
        return None
    cl = Client()
    try:
        cl.load_settings(SESSION_FILE)
        cl.login(username, password)
        cl.get_timeline_feed()  # validate the session is still alive
        return cl
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Session state                                                               #
# --------------------------------------------------------------------------- #
ss = st.session_state
ss.setdefault("stage", "login")
ss.setdefault("client", None)
ss.setdefault("username", "")
ss.setdefault("non_followers", None)  # list[(uid, UserShort)] once loaded
ss.setdefault("whitelist_skipped", 0)
ss.setdefault("thumb_cache", {})  # profile_pic_url -> (bytes | None, error | None)
ss.setdefault("page", 0)
ss.setdefault("last_search", "")


# --------------------------------------------------------------------------- #
# Stage: login                                                                #
# --------------------------------------------------------------------------- #
if ss["stage"] == "login":
    st.title("📷 Instagram Follow/Unfollow")
    st.caption(
        "Runs only on your machine (localhost). Reuses credentials.txt, "
        "session.json and whitelist.txt from this folder."
    )

    default_user, default_pass = read_credentials_file(CREDENTIALS_FILE)

    with st.form("login_form"):
        username = st.text_input("Username", value=default_user)
        password = st.text_input("Password", value=default_pass, type="password")
        submitted = st.form_submit_button("Log in", type="primary")

    if submitted:
        if not username or not password:
            st.error("Enter both a username and password.")
        else:
            with st.spinner("Logging in..."):
                cl = attempt_session_reuse(username, password)
                if cl is not None:
                    ss["client"], ss["username"], ss["stage"] = cl, username, "dashboard"
                    st.rerun()
                else:
                    cl = Client()
                    try:
                        cl.login(username, password)
                        save_session(cl, username)
                        ss["client"], ss["username"], ss["stage"] = cl, username, "dashboard"
                        st.rerun()
                    except TwoFactorRequired:
                        ss["client"], ss["username"], ss["stage"] = cl, username, "2fa"
                        st.rerun()
                    except BadPassword:
                        st.error("Incorrect password.")
                    except ChallengeRequired:
                        st.error(
                            "Instagram issued a challenge (suspicious login). "
                            "Approve the login in the Instagram app, then try again."
                        )
                    except Exception as exc:
                        st.error(f"Login failed: {exc}")

# --------------------------------------------------------------------------- #
# Stage: 2FA                                                                   #
# --------------------------------------------------------------------------- #
elif ss["stage"] == "2fa":
    st.title("Two-factor authentication")
    cl: Client = ss["client"]
    channels = two_factor_channels(cl)
    labels = [label for _, label in channels]
    method_by_label = {label: method for method, label in channels}

    choice_label = st.radio("Which channel did you receive the code on?", labels, index=0)
    code = st.text_input("Verification code")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Submit code", type="primary"):
            try:
                submit_two_factor(cl, ss["username"], method_by_label[choice_label], code.strip())
                ss["stage"] = "dashboard"
                st.rerun()
            except Exception as exc:
                st.error(f"Verification failed: {exc}")
    with col2:
        if st.button("Cancel"):
            ss["stage"], ss["client"] = "login", None
            st.rerun()

# --------------------------------------------------------------------------- #
# Stage: dashboard                                                             #
# --------------------------------------------------------------------------- #
elif ss["stage"] == "dashboard":
    cl: Client = ss["client"]
    st.title(f"📷 @{ss['username']}")

    top1, top2 = st.columns([1, 5])
    with top1:
        if st.button("Log out"):
            ss["stage"], ss["client"], ss["non_followers"] = "login", None, None
            st.rerun()

    if ss["non_followers"] is None:
        st.info("Nothing loaded yet.")
        if st.button("Load non-followers", type="primary"):
            with st.spinner("Fetching following + followers (live)..."):
                whitelist = load_whitelist()
                user_id = cl.user_id
                following = cl.user_following(user_id, use_cache=False, amount=0)
                followers = cl.user_followers(user_id, use_cache=False, amount=0)
                skipped = 0
                targets = []
                for uid, user in following.items():
                    if uid in followers:
                        continue
                    if user.username.lower() in whitelist:
                        skipped += 1
                        continue
                    targets.append((uid, user))
                targets.sort(key=lambda t: t[1].username.lower())
                ss["non_followers"] = targets
                ss["whitelist_skipped"] = skipped
            st.rerun()
        st.stop()

    targets = ss["non_followers"]
    st.write(
        f"**{len(targets)}** accounts don't follow you back "
        f"({ss['whitelist_skipped']} whitelisted accounts hidden this load)."
    )

    if st.button("Refresh list"):
        ss["non_followers"] = None
        st.rerun()

    whitelist_entries = list_whitelist_entries()
    with st.expander(f"🔖 Whitelisted accounts ({len(whitelist_entries)})"):
        if not whitelist_entries:
            st.caption("No whitelisted accounts yet.")
        for name in whitelist_entries:
            wcol1, wcol2, wcol3 = st.columns([3, 1, 1])
            with wcol1:
                st.write(f"@{name}")
            with wcol2:
                st.link_button(
                    "View profile", f"https://www.instagram.com/{name}/", use_container_width=True
                )
            with wcol3:
                if st.button("Remove", key=f"unwl_{name}", use_container_width=True):
                    remove_from_whitelist(name)
                    st.rerun()

    # --------------------------------------------------------------------- #
    # Bulk actions - kept at the top so they're reachable without scrolling #
    # past a full page of thumbnails.                                       #
    # --------------------------------------------------------------------- #
    selected = [(uid, user) for uid, user in targets if ss.get(f"sel_{uid}")]
    st.write(f"**{len(selected)} selected**")

    with st.expander("Advanced settings (pacing / safety)"):
        run_limit = st.number_input("Max accounts per run", min_value=1, max_value=200, value=DEFAULT_RUN_LIMIT)
        min_delay = st.number_input("Min delay between actions (s)", min_value=5, value=DEFAULT_MIN_DELAY)
        max_delay = st.number_input("Max delay between actions (s)", min_value=int(min_delay), value=DEFAULT_MAX_DELAY)
        st.caption(
            "The tab must stay open for the whole run - a big batch with "
            "unfollow+refollow can take a long time (e.g. 50 accounts ≈ up to "
            "~1.5 hours at default pacing). For unattended long runs, use the "
            "unfollow.py / refollow_cycle.py CLI scripts instead."
        )

    action_col1, action_col2 = st.columns(2)
    run_unfollow = action_col1.button("Unfollow selected", disabled=not selected)
    run_cycle = action_col2.button("Unfollow + Refollow selected", disabled=not selected)

    if run_unfollow or run_cycle:
        batch = selected[: int(run_limit)]
        if len(selected) > run_limit:
            st.warning(f"{len(selected)} selected, processing first {run_limit} (per-run cap).")

        progress = st.progress(0.0, text=f"0/{len(batch)} processed (0%)")
        status = st.empty()
        log_box = st.empty()
        lines: list[str] = []

        def log_line(text: str) -> None:
            lines.append(text)
            log_box.code("\n".join(lines[-40:]))

        def update_progress(i: int, total: int) -> None:
            progress.progress(i / total, text=f"{i}/{total} processed ({int(i / total * 100)}%)")

        done_uids = set()
        failures = 0
        stopped = False
        processed = 0

        for i, (uid, user) in enumerate(batch, start=1):
            processed = i
            try:
                cl.user_unfollow(uid)
                log_action(UNFOLLOW_LOG_FILE, ss["username"], "unfollow", user.username)
                log_line(f"[{i}/{len(batch)}] unfollowed @{user.username}")
                done_uids.add(uid)
                failures = 0
            except PleaseWaitFewMinutes:
                log_line("Instagram says to wait a few minutes - stopping to stay safe.")
                stopped = True
                break
            except Exception as exc:
                failures += 1
                log_line(f"[{i}/{len(batch)}] FAILED to unfollow @{user.username}: {exc}")
                update_progress(i, len(batch))
                if failures >= 3:
                    log_line("Too many consecutive failures - stopping to stay safe.")
                    stopped = True
                    break
                continue

            if run_cycle:
                status.write(f"Waiting before re-following @{user.username}...")
                time.sleep(random.uniform(min_delay, max_delay))
                try:
                    cl.user_follow(uid)
                    log_action(REFOLLOW_LOG_FILE, ss["username"], "refollow", user.username)
                    log_line(f"[{i}/{len(batch)}] refollowed @{user.username}")
                    failures = 0
                except PleaseWaitFewMinutes:
                    log_line("Instagram says to wait a few minutes - stopping to stay safe.")
                    update_progress(i, len(batch))
                    stopped = True
                    break
                except Exception as exc:
                    failures += 1
                    log_line(f"[{i}/{len(batch)}] FAILED to refollow @{user.username}: {exc}")
                    if failures >= 3:
                        log_line("Too many consecutive failures - stopping to stay safe.")
                        update_progress(i, len(batch))
                        stopped = True
                        break

            update_progress(i, len(batch))
            ss[f"sel_{uid}"] = False

            if i < len(batch):
                status.write("Pausing before the next account...")
                time.sleep(random.uniform(min_delay, max_delay))

        status.write("Stopped early." if stopped else "Done.")

        # Accounts that were actually unfollowed are no longer non-followers
        # (whether or not they were re-followed) - drop them from the working list.
        if done_uids:
            ss["non_followers"] = [t for t in ss["non_followers"] if t[0] not in done_uids]

    st.divider()

    # Re-read: the action block above may have removed accounts.
    targets = ss["non_followers"]

    search = st.text_input("Filter by username or name", "")
    filtered = (
        [
            (uid, user)
            for uid, user in targets
            if search.lower() in user.username.lower()
            or search.lower() in (user.full_name or "").lower()
        ]
        if search
        else targets
    )

    if search != ss["last_search"]:
        ss["page"] = 0
        ss["last_search"] = search
    total_pages = max(1, -(-len(filtered) // PAGE_SIZE))  # ceil division
    ss["page"] = min(max(ss["page"], 0), total_pages - 1)

    page_col1, page_col2, page_col3 = st.columns([1, 3, 1])
    with page_col1:
        if st.button("◀ Prev", disabled=ss["page"] <= 0):
            ss["page"] -= 1
            st.rerun()
    with page_col2:
        st.write(f"Page {ss['page'] + 1} of {total_pages} ({len(filtered)} accounts, {PAGE_SIZE} per page)")
    with page_col3:
        if st.button("Next ▶", disabled=ss["page"] >= total_pages - 1):
            ss["page"] += 1
            st.rerun()

    page_start = ss["page"] * PAGE_SIZE
    shown = filtered[page_start : page_start + PAGE_SIZE]

    sel1, sel2, _ = st.columns([1, 1, 4])
    with sel1:
        if st.button("Select all on page"):
            for uid, _ in shown:
                ss[f"sel_{uid}"] = True
            st.rerun()
    with sel2:
        if st.button("Select none on page"):
            for uid, _ in shown:
                ss[f"sel_{uid}"] = False
            st.rerun()

    # Prefetch any thumbnails not already cached (parallel, server-side).
    uncached_urls = {
        str(user.profile_pic_url)
        for _, user in shown
        if user.profile_pic_url and str(user.profile_pic_url) not in ss["thumb_cache"]
    }
    if uncached_urls:
        with st.spinner(f"Loading {len(uncached_urls)} thumbnails..."):
            with ThreadPoolExecutor(max_workers=8) as executor:
                for url, data in zip(uncached_urls, executor.map(lambda u: fetch_thumbnail(cl, u), uncached_urls)):
                    ss["thumb_cache"][url] = data

    # Thumbnail grid
    for row_start in range(0, len(shown), GRID_COLUMNS):
        row = shown[row_start : row_start + GRID_COLUMNS]
        cols = st.columns(GRID_COLUMNS)
        for col, (uid, user) in zip(cols, row):
            with col:
                pic_url = str(user.profile_pic_url) if user.profile_pic_url else None
                cached = ss["thumb_cache"].get(pic_url) if pic_url else None
                thumb, _thumb_error = cached if cached else (None, None)

                img_col, btn_col = st.columns([1, 1])
                with img_col:
                    if thumb:
                        st.image(thumb, use_container_width=True)
                    else:
                        # Silent fallback - no error text, just a generic avatar.
                        st.markdown(
                            f"<div style='width:{THUMB_WIDTH}px;height:{THUMB_WIDTH}px;"
                            "display:flex;align-items:center;justify-content:center;"
                            "background:rgba(128,128,128,0.2);border-radius:50%;font-size:2.5em;'>"
                            "👤</div>",
                            unsafe_allow_html=True,
                        )
                with btn_col:
                    st.link_button(
                        "View profile",
                        f"https://www.instagram.com/{user.username}/",
                        use_container_width=True,
                    )
                    if st.button("Whitelist", key=f"wl_{uid}", use_container_width=True):
                        add_to_whitelist(user.username)
                        ss["non_followers"] = [t for t in ss["non_followers"] if t[0] != uid]
                        ss["whitelist_skipped"] += 1
                        ss.pop(f"sel_{uid}", None)
                        st.rerun()
                    if st.button(
                        "Unfollow",
                        key=f"uf_{uid}",
                        use_container_width=True,
                        help="Unfollows this account immediately (skips the bulk-action pacing delay).",
                    ):
                        try:
                            cl.user_unfollow(uid)
                            log_action(UNFOLLOW_LOG_FILE, ss["username"], "unfollow", user.username)
                            ss["non_followers"] = [t for t in ss["non_followers"] if t[0] != uid]
                            ss.pop(f"sel_{uid}", None)
                            st.toast(f"Unfollowed @{user.username}")
                        except Exception as exc:
                            st.toast(f"Failed to unfollow @{user.username}: {exc}", icon="⚠️")
                        st.rerun()

                st.checkbox(f"@{user.username}", key=f"sel_{uid}", help=user.full_name or "")
