#!/usr/bin/env python3
"""
lotto_watch.py - alerts you when:
  1. The Lotto Max jackpot reaches its cap ($90M), with an optional early warning ($80M).
  2. A Super Draw (e.g. Lotto 6/49 Super Draw) is announced, plus a reminder on the day.

Standard library only (Python 3.9+). Designed to run once a day from GitHub Actions,
but works from any cron. State lives in a small JSON file so you only get each alert once.

Config (all via environment variables):
  NTFY_TOPIC            ntfy.sh topic to push to (treat it like a password)
  NTFY_SERVER           default https://ntfy.sh
  NTFY_TOKEN            optional access token if you use a protected topic / self-hosted ntfy
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO, EMAIL_FROM   optional email
  LOTTO_MAX_CAP         default 90000000
  LOTTO_MAX_WARN_AT     default 80000000 (0 disables the early warning)
  ALERT_EVERY_CAPPED_DRAW  default true  (false = only the first draw at the cap)
  HEARTBEAT_WEEKDAY     optional, 0=Mon..6=Sun: send a weekly "still alive" status
  FAIL_ALERT_AFTER      default 3 consecutive failed runs before a "scraper broken" alert
  STATE_FILE            default state.json next to this script
  DRY_RUN               true = print alerts instead of sending (state is still not saved)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path
from urllib import error, request
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


CONFIG_ERRORS: list[str] = []


def env_int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        # Don't crash before alerts go out; main() fails the run afterwards so it gets noticed.
        CONFIG_ERRORS.append(f"{name}={v!r} is not a whole number, using {default}")
        return default


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


CAP = env_int("LOTTO_MAX_CAP", 90_000_000)
WARN_AT = env_int("LOTTO_MAX_WARN_AT", 80_000_000)
ALERT_EVERY_CAPPED_DRAW = env_bool("ALERT_EVERY_CAPPED_DRAW", True)
FAIL_ALERT_AFTER = env_int("FAIL_ALERT_AFTER", 3)
# A drop smaller than this is treated as noise (sources round differently), not a win.
RESET_DROP = 5_000_000

# Where the data comes from. WCLC is one of the official operators and renders the
# next Lotto Max jackpot server-side; lottery.fm is an independent fallback.
LOTTO_MAX_SOURCES = [
    ("WCLC", "https://www.wclc.com/home.htm"),
    ("lottery.fm", "https://lottery.fm/ca/lotto-max"),
]
SUPER_DRAW_PAGES = [
    ("PlayNow (BCLC)", "https://www.playnow.com/lottery/promotions/649-super-draw/"),
    ("WCLC", "https://www.wclc.com/home.htm"),
]
# OLG publishes game conditions for each Super Draw ahead of time at a predictable URL
# (seen for Nov 2025, Dec 2025, Aug 2026), so we probe the current month and the next few.
OLG_CONDITION_URL = "https://www.olg.ca/en/lottery/game-conditions/lotto-649-super-draw/{month}-{year}.html"
OLG_MONTHS_AHEAD = 3

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
_MONTH_RE = (r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
             r"Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")
DATE_RE = re.compile(_MONTH_RE + r"\s+(\d{1,2}),?\s+(20\d\d)", re.I)
SUPER_RE = re.compile(r"super\s?-?\s?draw", re.I)


# --------------------------------------------------------------------------- fetching
def fetch(url: str, timeout: int = 30) -> tuple[int, str]:
    req = request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
    })
    try:
        with request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except error.HTTPError as e:
        return e.code, ""


def to_text(page: str) -> str:
    """HTML -> one line of plain text. Parsing works on this, so layout changes hurt less."""
    page = re.sub(r"(?s)<!--.*?-->", " ", page)  # commented-out drafts/stale promos
    page = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


def parse_date(month: str, day: str, year: str) -> dt.date | None:
    m = month.lower().rstrip(".")[:3]
    idx = next((i for i, name in enumerate(MONTHS) if name.startswith(m)), None)
    try:
        return dt.date(int(year), idx + 1, int(day)) if idx is not None else None
    except ValueError:
        return None


def long_date(d: dt.date) -> str:
    """'Saturday, October 24, 2026' (strftime's %-d doesn't work on Windows)."""
    return f"{d:%A, %B} {d.day}, {d.year}"


# --------------------------------------------------------------------------- Lotto Max
WCLC_MAX_RE = re.compile(
    r"\$\s*(\d{1,3}(?:\.\d+)?)\s*Million\s*"          # $ 65 Million
    r"(?:\d+\s*x\s*\$\s*1\s*Million\s*Prizes?\s*)?"   # 8 x $1 Million Prize (MaxMillions, optional)
    r"\d+\s*x\s*\$\s*100,000\s*"                      # 65 x $100,000 (MaxPlus - unique to Lotto Max)
    r"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s*)?"
    + _MONTH_RE + r"\s+(\d{1,2}),?\s+(20\d\d)",
    re.I,
)
LOTTERYFM_RE = re.compile(r"NEXT\s*DRAW\s*estimated\s*jackpot\s*C?\$\s*(\d{1,3}(?:\.\d+)?)\s*M", re.I)


def parse_wclc_lotto_max(text: str) -> tuple[int, dt.date | None] | None:
    m = WCLC_MAX_RE.search(text)
    if not m:
        return None
    return int(float(m.group(1)) * 1_000_000), parse_date(m.group(2), m.group(3), m.group(4))


def parse_lotteryfm_lotto_max(text: str) -> tuple[int, dt.date | None] | None:
    m = LOTTERYFM_RE.search(text)
    return (int(float(m.group(1)) * 1_000_000), None) if m else None


PARSERS = {"WCLC": parse_wclc_lotto_max, "lottery.fm": parse_lotteryfm_lotto_max}


def next_lotto_max_draw(now: dt.datetime) -> dt.date:
    """Draws are Tuesday and Friday at 10:30 pm Eastern."""
    d = now.date()
    if d.weekday() in (1, 4) and now.time() < dt.time(22, 30):
        return d
    for i in range(1, 8):
        cand = d + dt.timedelta(days=i)
        if cand.weekday() in (1, 4):
            return cand
    raise AssertionError("unreachable")


def get_lotto_max(now: dt.datetime, fetcher=fetch) -> tuple[int, dt.date, str] | None:
    for name, url in LOTTO_MAX_SOURCES:
        try:
            status, page = fetcher(url)
        except Exception as e:  # network errors, timeouts
            print(f"[lotto max] {name}: {e}", file=sys.stderr)
            continue
        if status != 200:
            print(f"[lotto max] {name}: HTTP {status}", file=sys.stderr)
            continue
        got = PARSERS[name](to_text(page))
        if not got:
            print(f"[lotto max] {name}: jackpot not found on page", file=sys.stderr)
            continue
        amount, draw = got
        if not 5_000_000 <= amount <= 500_000_000:  # sanity check against mis-parses
            print(f"[lotto max] {name}: implausible amount {amount}", file=sys.stderr)
            continue
        if draw is None or draw < now.date():
            draw = next_lotto_max_draw(now)
        return amount, draw, name
    return None


def fmt_money(n: int) -> str:
    return f"${n / 1_000_000:g} million"


def check_lotto_max(state: dict, amount: int, draw: dt.date) -> list[dict]:
    lm = state.setdefault("lotto_max", {})
    alerts = []
    draw_s = draw.isoformat()
    when = f"{draw:%A, %B} {draw.day}"

    # Jackpot was won (or dropped below our thresholds): re-arm the alerts for next time.
    floor = min(WARN_AT or CAP, CAP)
    if amount < floor or amount < lm.get("last_jackpot", 0) - RESET_DROP:
        lm.pop("warned", None)
        lm.pop("capped", None)
        lm.pop("last_cap_draw", None)

    if amount >= CAP:
        first = not lm.get("capped")
        if (first or ALERT_EVERY_CAPPED_DRAW) and lm.get("last_cap_draw") != draw_s:
            alerts.append({
                "title": "Lotto Max is at the MAX" if first else "Lotto Max still at the max",
                "body": (f"{fmt_money(amount)} jackpot for {when}'s draw (10:30 pm ET). "
                         f"Anything above the cap goes into extra $1M MaxMillions prizes."),
                "priority": "high", "tags": "moneybag,rotating_light", "expires": draw_s,
            })
            lm["capped"] = True
            lm["last_cap_draw"] = draw_s
    elif WARN_AT and amount >= WARN_AT and not lm.get("warned"):
        alerts.append({
            "title": "Lotto Max getting close to the max",
            "body": (f"{fmt_money(amount)} for {when}'s draw. The cap is {fmt_money(CAP)}, "
                     f"usually a draw or two away if nobody wins."),
            "priority": "default", "tags": "chart_with_upwards_trend", "expires": draw_s,
        })
        lm["warned"] = True

    lm["last_jackpot"] = amount
    lm["last_draw"] = draw_s
    return alerts


# --------------------------------------------------------------------------- Super Draws
def guess_game(snippet: str) -> str:
    s = snippet.lower()
    if re.search(r"lotto\s*max", s):
        return "Lotto Max"
    if "daily grand" in s:
        return "Daily Grand"
    return "Lotto 6/49"  # by far the most common Super Draw game


def super_draw_dates(text: str, today: dt.date, window: int = 160) -> dict[dt.date, str]:
    """Future dates that appear right after a mention of 'Super Draw' -> game name."""
    found: dict[dt.date, str] = {}
    for m in SUPER_RE.finditer(text):
        dm = DATE_RE.search(text[m.end(): m.end() + window])
        if dm:
            d = parse_date(*dm.groups())
            if d and d >= today:
                found.setdefault(d, guess_game(text[max(0, m.start() - 40): m.end() + 10]))
    return found


def olg_condition_urls(today: dt.date) -> list[tuple[str, dt.date]]:
    out = []
    y, m = today.year, today.month
    for _ in range(OLG_MONTHS_AHEAD + 1):
        out.append((OLG_CONDITION_URL.format(month=MONTHS[m - 1], year=y), dt.date(y, m, 1)))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def find_super_draws(today: dt.date, fetcher=fetch) -> tuple[dict[str, tuple[str, str]], int]:
    """Returns ({key: (game, description)}, number of pages that loaded and mention Super Draws).
    key is an ISO date when known, else YYYY-MM when only the month is known."""
    found: dict[str, tuple[str, str]] = {}
    ok = 0
    for name, url in SUPER_DRAW_PAGES:
        try:
            status, page = fetcher(url)
        except Exception as e:
            print(f"[super draw] {name}: {e}", file=sys.stderr)
            continue
        if status != 200:
            print(f"[super draw] {name}: HTTP {status}", file=sys.stderr)
            continue
        text = to_text(page)
        # Loading isn't enough: a moved or emptied page would otherwise look healthy forever.
        if SUPER_RE.search(text):
            ok += 1
        for d, game in super_draw_dates(text, today).items():
            found.setdefault(d.isoformat(), (game, f"{game} Super Draw on {long_date(d)} (via {name})"))

    for url, month_start in olg_condition_urls(today):
        try:
            status, page = fetcher(url)
        except Exception as e:
            print(f"[super draw] OLG probe: {e}", file=sys.stderr)
            continue
        if status != 200:
            continue  # 404 is expected for months with no Super Draw
        text = to_text(page)
        month_name = MONTHS[month_start.month - 1]
        if not (SUPER_RE.search(text) and month_name in text.lower()):
            continue  # soft 404 / unrelated page
        ok += 1
        dates = {d: g for d, g in super_draw_dates(text, today, window=400).items()
                 if (d.year, d.month) == (month_start.year, month_start.month)}
        if dates:
            for d, game in dates.items():
                found.setdefault(d.isoformat(), (game, f"{game} Super Draw on {long_date(d)} (via OLG)"))
        else:
            key = month_start.strftime("%Y-%m")
            if not any(k.startswith(key) for k in found):
                found[key] = ("Lotto 6/49",
                              f"Lotto 6/49 Super Draw in {month_start:%B %Y} (via OLG) - {url}")
    return found, ok


def check_super_draws(state: dict, found: dict[str, tuple[str, str]], today: dt.date) -> list[dict]:
    sd = state.setdefault("super_draws", {"announced": [], "reminded": []})
    games = sd.setdefault("games", {})  # exact date -> game, for the day-of reminder
    alerts = []
    for key, (game, desc) in sorted(found.items()):
        if len(key) == 10:
            games.setdefault(key, game)
        # A month-only key is superseded once we learn the exact date in that month.
        if key in sd["announced"]:
            continue
        already = (key[:7] in sd["announced"]) if len(key) == 10 else \
            any(k.startswith(key) for k in sd["announced"])
        if already:
            sd["announced"].append(key)
            continue
        alert = {"title": "Super Draw announced", "body": desc, "priority": "default", "tags": "tada"}
        if len(key) == 10:
            alert["expires"] = key
        alerts.append(alert)
        sd["announced"].append(key)
    # Day-of reminder for any known exact date.
    today_s = today.isoformat()
    if today_s in sd["announced"] and today_s not in sd["reminded"]:
        alerts.append({"title": "Super Draw is TONIGHT",
                       "body": f"{games.get(today_s, 'Lotto 6/49')} Super Draw tonight - "
                               f"ticket sales close at 10:30 pm ET.",
                       "priority": "high", "tags": "alarm_clock", "expires": today_s})
        sd["reminded"].append(today_s)
    # Keep the state file small: forget anything older than ~a year.
    cutoff = (today - dt.timedelta(days=400)).isoformat()
    sd["announced"] = sorted({k for k in sd["announced"] if k >= cutoff[:len(k)]})
    sd["reminded"] = sorted({k for k in sd["reminded"] if k >= cutoff})
    sd["games"] = {k: g for k, g in sorted(games.items()) if k >= cutoff}
    return alerts


# --------------------------------------------------------------------------- notifying
def send_ntfy(title: str, body: str, priority: str, tags: str) -> bool:
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if os.getenv("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    req = request.Request(f"{server}/{topic}", data=body.encode(), headers=headers, method="POST")
    with request.urlopen(req, timeout=30) as r:
        return 200 <= r.status < 300


def send_email(title: str, body: str) -> bool:
    host, to = os.getenv("SMTP_HOST"), os.getenv("EMAIL_TO")
    if not (host and to):
        return False
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = os.getenv("EMAIL_FROM") or os.getenv("SMTP_USER") or to
    msg["To"] = to
    msg.set_content(body)
    port = env_int("SMTP_PORT", 465)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
    return True


def notify(alert: dict, dry_run: bool) -> None:
    print(f"ALERT: {alert['title']} - {alert['body']}")
    if dry_run:
        return
    sent, errors = False, []
    for fn, args in ((send_ntfy, (alert["title"], alert["body"], alert["priority"], alert["tags"])),
                     (send_email, (alert["title"], alert["body"]))):
        try:
            sent = fn(*args) or sent
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    if errors:
        print("Notification errors: " + "; ".join(errors), file=sys.stderr)
    if not sent:
        # send_all() keeps the alert for a retry and main() then fails the run, so GitHub emails you.
        raise RuntimeError("No notification channel succeeded (set NTFY_TOPIC and/or SMTP_*).")


# --------------------------------------------------------------------------- main
def track_health(state: dict, key: str, ok: bool, what: str) -> list[dict]:
    h = state.setdefault("health", {})
    n = h.get(key, 0)
    if ok:
        h[key] = 0
        if n >= FAIL_ALERT_AFTER:
            return [{"title": "Lotto watch recovered", "body": f"{what} is working again.",
                     "priority": "low", "tags": "white_check_mark"}]
        return []
    h[key] = n + 1
    if h[key] == FAIL_ALERT_AFTER:
        return [{"title": "Lotto watch needs attention",
                 "body": (f"{what} has failed {h[key]} runs in a row - a website probably changed. "
                          f"Check the GitHub Actions log."),
                 "priority": "high", "tags": "warning"}]
    return []


def run(state: dict, now: dt.datetime, fetcher=fetch) -> list[dict]:
    alerts: list[dict] = []
    today = now.date()

    lm = get_lotto_max(now, fetcher)
    alerts += track_health(state, "lotto_max_failures", lm is not None, "Reading the Lotto Max jackpot")
    if lm:
        amount, draw, source = lm
        print(f"Lotto Max: {fmt_money(amount)} for {draw} (source: {source})")
        alerts += check_lotto_max(state, amount, draw)

    found, ok_sources = find_super_draws(today, fetcher)
    alerts += track_health(state, "super_draw_failures", ok_sources > 0, "Checking for Super Draws")
    print("Super Draws seen: " + ("; ".join(desc for _, desc in found.values()) or "none"))
    alerts += check_super_draws(state, found, today)

    hb = env_int("HEARTBEAT_WEEKDAY", -1)
    if hb == today.weekday() and state.get("last_heartbeat") != today.isoformat():
        jp = state.get("lotto_max", {}).get("last_jackpot")
        alerts.append({"title": "Lotto watch: weekly check-in",
                       "body": f"Still running. Lotto Max is at {fmt_money(jp) if jp else 'unknown'}.",
                       "priority": "min", "tags": "robot"})
        state["last_heartbeat"] = today.isoformat()

    state["last_run"] = now.isoformat(timespec="seconds")
    return alerts


def send_all(state: dict, alerts: list[dict], today: dt.date, dry_run: bool, notifier=notify) -> int:
    """Send leftovers from a failed run, then the new alerts. Anything that can't be sent goes
    back into state["outbox"] for next run, so it isn't lost and the ones that worked don't repeat.
    Returns how many failed."""
    today_s = today.isoformat()
    queued = [a for a in state.pop("outbox", []) if a.get("expires", today_s) >= today_s]
    failed = []
    for a in queued + alerts:
        try:
            notifier(a, dry_run)
        except RuntimeError as e:
            print(f"Not sent ({a['title']}): {e}", file=sys.stderr)
            failed.append(a)
    if failed:
        state["outbox"] = failed
    return len(failed)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print alerts, don't send or save state")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    ap.add_argument("--state", default=os.getenv("STATE_FILE") or str(Path(__file__).with_name("state.json")))
    args = ap.parse_args()
    dry = args.dry_run or env_bool("DRY_RUN", False)

    failed = 0
    if args.test_notify:
        notify({"title": "Lotto watch test", "body": "If you can read this, alerts are working.",
                "priority": "default", "tags": "white_check_mark"}, dry)
    else:
        path = Path(args.state)
        state = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
        now = dt.datetime.now(TZ)
        failed = send_all(state, run(state, now), now.date(), dry)
        if not dry:
            path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    if CONFIG_ERRORS:
        print("Config problems: " + "; ".join(CONFIG_ERRORS), file=sys.stderr)
    # Fail loudly (GitHub emails the repo owner) - state, including the outbox, is already saved.
    return 1 if failed or CONFIG_ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())
