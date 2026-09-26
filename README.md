# lotto-watch

A tiny, free, no-server alert bot. Once a day it checks, and pings your phone (and/or email) when:

| Alert | When |
|---|---|
| **Lotto Max is at the MAX** | Next jackpot hits the $90M cap. Repeats once per draw while it stays capped (set `ALERT_EVERY_CAPPED_DRAW=false` for just the first). |
| **Getting close** | Jackpot reaches $80M (usually 1–2 draws before the cap). `LOTTO_MAX_WARN_AT=0` turns it off. |
| **Super Draw announced** | A Lotto 6/49 (or any) Super Draw with a future date shows up on PlayNow/WCLC, or OLG publishes Super Draw game conditions for this month or the next 3. |
| **Super Draw is TONIGHT** | Morning of a known Super Draw. |
| **Needs attention** | 3 runs in a row couldn't read the data (a website changed). You'll never silently stop getting alerts. |

It's one Python file with no dependencies, run by GitHub Actions on a schedule. Cost: $0.

## Setup (about 10 minutes)

**1. Get the ntfy app for push notifications**
- Install **ntfy** (iOS App Store / Google Play).
- Tap **+** and subscribe to a topic with a long random name, e.g. `lotto-7f3kq9x2mzp4`.
  Anyone who knows the name can read it, so treat it like a password.

**2. Put the code on GitHub**
- Create a new **private** repo and upload everything in this folder, keeping `.github/workflows/lotto-watch.yml` at that path.

**3. Add your secrets**: repo **Settings → Secrets and variables → Actions → New repository secret**
- `NTFY_TOPIC` = your topic name (just the name, not the URL)
- *Optional email* (Gmail example, needs an [app password](https://myaccount.google.com/apppasswords)):
  `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=465`, `SMTP_USER=you@gmail.com`, `SMTP_PASSWORD=<app password>`, `EMAIL_TO=you@gmail.com`

**4. Test it**: **Actions → lotto-watch → Run workflow**
- `test-notify` → you should get a test push within seconds.
- `dry-run` → check the log shows a line like `Lotto Max: $65 million for 2026-09-29 (source: WCLC)`.
  (If it says `jackpot not found`, a site layout differs from what was expected. Paste the log to Claude and it's a one-line regex fix.)

That's it. It runs daily at ~9:17 am Toronto time.

## Optional settings
Repo **Settings → Secrets and variables → Actions → Variables** tab:

| Variable | Default | Meaning |
|---|---|---|
| `LOTTO_MAX_WARN_AT` | `80000000` | Early-warning level; `0` disables |
| `ALERT_EVERY_CAPPED_DRAW` | `true` | `false` = only the first draw at the cap |
| `HEARTBEAT_WEEKDAY` | *(off)* | `0`=Mon … `6`=Sun: weekly quiet "still running, jackpot is $X" push |

## How it works
- **Lotto Max jackpot:** read from WCLC's home page (an official provincial lottery; it shows the next jackpot in plain HTML), with lottery.fm as a fallback. OLG's own site loads the number with JavaScript, so it isn't used.
- **Super Draws:** scans PlayNow's (BCLC) Super Draw page and WCLC for "Super Draw" followed by a future date, and probes OLG's predictable game-conditions URLs (`…/lotto-649-super-draw/november-2026.html`) for the next few months.
- **State:** `state.json` gets committed back to the repo so you get each alert once. The daily commit also keeps GitHub from auto-disabling the schedule (it does that after 60 days with no repo activity).
- **Failure handling:** if a notification can't be sent, the run fails and GitHub emails you.

## Run locally / tests
```bash
python lotto_watch.py --dry-run        # print what it would alert on
NTFY_TOPIC=your-topic python lotto_watch.py --test-notify
python -m unittest -v                  # offline tests with saved page snippets
```

Draw times: Lotto Max Tue/Fri 10:30 pm ET · Lotto 6/49 Wed/Sat 10:30 pm ET.
Unofficial tool: always confirm on olg.ca before buying or claiming.
