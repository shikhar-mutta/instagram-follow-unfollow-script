# Instagram Follow/Unfollow Script

Small set of Python scripts, built on [instagrapi](https://github.com/subzeroid/instagrapi), that let you:

- Download the full list of your Instagram followers.
- Find accounts you follow that don't follow you back.
- Safely unfollow those non-followers, with a dry run, daily/per-run cap, random delays, and a whitelist of accounts to never touch.
- Optionally unfollow non-followers and immediately send a follow request back to them, to refresh the relationship.
- Do all of the above from a local web UI with account thumbnails and multi-select, instead of the CLI.

## Use Case

If you follow a lot of accounts, it's hard to tell who follows you back and who doesn't. Instagram's own UI doesn't expose this easily, and doing it by hand for hundreds/thousands of accounts is impractical. This project automates that:

1. **Audit** — pull your followers and following lists into plain text files.
2. **Diff** — compute who you follow that doesn't follow you back.
3. **Clean up** — unfollow those accounts in a controlled, rate-limited way, while protecting anyone on a whitelist (friends, family, brands you want to keep following regardless of follow-back status).

## Files

| File | Purpose |
|---|---|
| [get_followers.py](get_followers.py) | Logs in and saves your full followers list to `followers.txt`. |
| [get_non_followers.py](get_non_followers.py) | Logs in, saves `following.txt` and `non_followers.txt` (people you follow who don't follow you back). |
| [unfollow.py](unfollow.py) | Unfollows accounts from the non-follower list, skipping anyone in `whitelist.txt`. Defaults to a dry run. |
| [refollow_cycle.py](refollow_cycle.py) | Unfollows non-followers, then sends a follow request back to each shortly after. Defaults to a dry run. |
| [web_app.py](web_app.py) | Local Streamlit web UI: log in, browse non-followers as a thumbnail grid, multi-select, bulk unfollow or unfollow+refollow. |
| [list_followers_log.py](list_followers_log.py) | Logs in and saves your current followers to `followers_log.txt` in the same timestamped log format as the audit logs. |
| [list_following_log.py](list_following_log.py) | Logs in and saves everyone you currently follow to `following_log.txt`, same log format. |
| [track_changes.py](track_changes.py) | Compares today's followers/following against the last saved snapshot and logs exactly who was added/removed, with counts. |
| `credentials.txt` | Your Instagram username/password (not committed — see [Security](#security)). |
| `session.json` | Cached login session so you don't have to re-authenticate (and re-trigger 2FA/challenges) every run. |
| `whitelist.txt` | Usernames that should never be touched, one per line. |
| `unfollowed_log.txt` | Append-only audit log of every account unfollowed by `unfollow.py`, with timestamp. |
| `refollow_log.txt` | Append-only audit log of unfollow/refollow actions from `refollow_cycle.py`, with timestamp. |
| `unfollow_preview.txt` | Written on every `unfollow.py` dry run: the exact list that would be unfollowed. |
| `refollow_preview.txt` | Written on every `refollow_cycle.py` dry run: the exact list that would be cycled. |
| `followers.txt` / `following.txt` / `non_followers.txt` | Generated output lists. |
| `followers_log.txt` / `following_log.txt` | Current-snapshot lists, timestamped, written by `list_followers_log.py` / `list_following_log.py` (and refreshed by `track_changes.py`). |
| `followers_diff_log.txt` / `following_diff_log.txt` | Append-only history of changes: who was added/removed each time `track_changes.py` runs, with counts. |

## Requirements

- Python 3.10+
- [instagrapi](https://github.com/subzeroid/instagrapi)
- [wcwidth](https://pypi.org/project/wcwidth/)
- [streamlit](https://streamlit.io/) — only needed for `web_app.py`

```bash
pip install instagrapi wcwidth streamlit
```

## Setup

Create `credentials.txt` in the project directory:

```
usr : your_username
pass : your_password
```

(`user`/`username` and `pass`/`password`/`pwd` are also accepted as keys, and `=` works as a separator too.)

Optionally create `whitelist.txt` with one username per line (accounts to never unfollow):

```
close_friend_1
@another_account
```

## Usage

**1. Fetch your followers**

```bash
python3 get_followers.py
```

Saves a sorted list to `followers.txt`.

**2. Find who doesn't follow you back**

```bash
python3 get_non_followers.py
```

Saves `following.txt` (everyone you follow) and `non_followers.txt` (people you follow who don't follow you back).

**3. Unfollow non-followers**

```bash
python3 unfollow.py              # dry run - only prints who WOULD be unfollowed
python3 unfollow.py --confirm    # actually unfollows (respects the cap and whitelist)
```

Every dry run also saves the full list to `unfollow_preview.txt`. Open it, decide who you actually want to keep, add those usernames to `whitelist.txt`, then re-run the dry run to confirm they're excluded before passing `--confirm`.

You can also point any script at a different credentials file:

```bash
python3 unfollow.py --confirm mycreds.txt
```

**4. Unfollow non-followers, then refollow them**

```bash
python3 refollow_cycle.py              # dry run - only prints who WOULD be cycled
python3 refollow_cycle.py --confirm    # actually unfollows then refollows (respects the cap and whitelist)
```

For each eligible account this does: unfollow → wait 30-60s → send a follow request back → wait 30-60s → move to the next account. The idea is that reappearing in someone's notifications/followers list can prompt a follow back. It targets the same live-computed non-follower list as `unfollow.py` (whitelist respected), just as a separate script so plain permanent unfollows and the cycle behavior stay independent.

Every dry run also saves the full list to `refollow_preview.txt` for the same review-then-whitelist workflow described above.

**5. Web UI (thumbnails + bulk actions)**

```bash
streamlit run web_app.py
```

Opens a local browser tab (`http://localhost:8501` by default). Log in (2FA supported), click **Load non-followers** to fetch the live list with profile-picture thumbnails, tick the accounts you want, then click **Unfollow selected** or **Unfollow + Refollow selected**. A progress bar and live log show what's happening. Same whitelist, same session file, same audit logs (`unfollowed_log.txt` / `refollow_log.txt`) as the CLI scripts — anyone in `whitelist.txt` never shows up in the grid.

This is local-only (binds to `localhost`) and holds your live Instagram session in memory for as long as the tab/server is open — don't expose the port beyond your own machine. Because it's driven from a browser tab, keep the tab open for the whole run; for very large unattended batches, prefer `unfollow.py` / `refollow_cycle.py` instead.

**6. Log current followers / following**

```bash
python3 list_followers_log.py
python3 list_following_log.py
```

Each overwrites its output file (`followers_log.txt` / `following_log.txt`) with the current list, one line per account:

```
Total followers: 1219
2026-08-09 06:17:42	shikhar_mutta_68	follower	shivaah_15	https://www.instagram.com/shivaah_15/
```

**7. Track who was added/removed**

```bash
python3 track_changes.py
```

Compares today's followers and following against the last saved `followers_log.txt` / `following_log.txt` snapshot, then:

- Appends a summary + per-account entry to `followers_diff_log.txt` and `following_diff_log.txt`:

  ```
  2026-08-09 06:30:00	shikhar_mutta_68	followers	added: 3	removed: 2
  2026-08-09 06:30:00	shikhar_mutta_68	added	new_follower1	https://www.instagram.com/new_follower1/
  2026-08-09 06:30:00	shikhar_mutta_68	removed	old_follower1	https://www.instagram.com/old_follower1/
  ```
- Refreshes `followers_log.txt` / `following_log.txt` with today's snapshot, so the *next* run diffs against today instead of stacking up drift.

Run it regularly (e.g. daily via cron) to build a history of who unfollowed you and who followed you back over time. The very first run has nothing to diff against, so everyone will show up as "added" — that's expected.

## Safety Features

`unfollow.py` and `refollow_cycle.py` are intentionally conservative to reduce the risk of Instagram flagging or blocking your account:

- **Dry run by default** — nothing changes unless you pass `--confirm`.
- **Per-run cap** — processes at most `RUN_LIMIT` (default 50) accounts per run.
- **Randomized delays** — waits 30-60 seconds (random) between actions (`refollow_cycle.py` pauses both between the unfollow and the refollow, and between accounts).
- **Whitelist** — accounts in `whitelist.txt` are never touched.
- **Live data only** — always fetches fresh followers/following lists before acting, never relies on stale files.
- **Auto-stop on trouble** — stops early if Instagram asks to "wait a few minutes" or if 3 actions fail in a row.
- **Audit log** — every action is appended to `unfollowed_log.txt` (or `refollow_log.txt`) with a timestamp and account name.

Note that unfollow/refollow churn is more visible to Instagram's spam detection than a plain one-way unfollow, so use `refollow_cycle.py` sparingly and keep the per-run cap conservative.

## Session & 2FA

All scripts (including the web UI) share the same login flow:

- On first run, you're logged in with `credentials.txt` and the session is cached to `session.json` so subsequent runs skip the login step.
- If `session.json` belongs to a different account than `credentials.txt`, it's discarded and a fresh login happens.
- If your account has 2FA enabled, you'll be prompted for a code (SMS, authenticator app, or WhatsApp, whichever Instagram used).
- If Instagram issues a login challenge (suspicious login), approve it in the Instagram app, then re-run the script.

## Security

`credentials.txt` and `session.json` contain sensitive data (your password / an authenticated session token). They're excluded from version control via [.gitignore](.gitignore):

```
credentials.txt
session.json
__pycache__/
*.pyc
```

If either file was ever committed before `.gitignore` was added, removing them going forward isn't enough — they'll still be readable in old commits. Scrub them from git history (or rotate the exposed password) before pushing to a public remote.

## Disclaimer

This project uses an unofficial API ([instagrapi](https://github.com/subzeroid/instagrapi)) that automates actions against your own Instagram account. Use it responsibly and only on accounts you own/control:

- Automating Instagram actions is against Instagram's Terms of Service and can result in rate limiting, temporary blocks, or account bans.
- The built-in pacing/caps reduce but do not eliminate that risk.
- You are responsible for how you use these scripts.
# instagram-follow-unfollow-script
