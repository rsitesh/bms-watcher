# Avengers: Doomsday ticket watcher (BookMyShow, Hyderabad)

Checks BookMyShow every ~10 minutes from GitHub's servers (your laptop can be off). When bookings open it sends you a **WhatsApp message listing the cinemas** and **calls your phone** once.

You also get one short WhatsApp each evening saying it's still watching, and a warning if BookMyShow starts blocking it, so it never fails silently.

---

## Setup (about 20 minutes)

### 1. Put the code on GitHub
1. Create a free account at github.com.
2. Create a new repository → make it **Public** (scheduled runs are unlimited and free on public repos; your phone number and keys are stored as encrypted *secrets*, which are never visible to anyone, even on a public repo).
3. Upload all these files, keeping the folder structure (`.github/workflows/watch.yml` must be in that exact path).

> Prefer a private repo? It works, but the free plan has 2,000 minutes/month. Change the cron line in `watch.yml` to `*/30 * * * *` so you stay within it.

### 2. WhatsApp alerts (free, via CallMeBot)
1. Go to **callmebot.com → Free WhatsApp API** and get the current CallMeBot phone number listed there.
2. Save it in your contacts, then send it this WhatsApp message: `I allow callmebot to send me messages`
3. You'll receive an API key in reply.

### 3. Phone call alerts (Twilio)
1. Sign up at twilio.com (the trial includes free credit, enough for many calls).
2. In the console, copy your **Account SID** and **Auth Token**, and get a Twilio phone number.
3. On a trial account you can only call **verified numbers**: go to *Phone Numbers → Verified Caller IDs* and verify your mobile.
4. Trial calls start with a short "trial account" message; then press any key and the alert is read out.

Skip this step if you only want WhatsApp; the watcher simply won't call.

### 4. Add your secrets to GitHub
Repo → **Settings → Secrets and variables → Actions → Secrets → New repository secret**:

| Secret | Example |
|---|---|
| `CALLMEBOT_PHONE` | `+919876543210` |
| `CALLMEBOT_APIKEY` | `123456` |
| `TWILIO_ACCOUNT_SID` | `ACxxxxxxxx…` |
| `TWILIO_AUTH_TOKEN` | `xxxxxxxx…` |
| `TWILIO_FROM_NUMBER` | your Twilio number, e.g. `+1415…` |
| `CALL_TO_NUMBER` | `+919876543210` |

### 5. (Recommended) Add the movie's event code
Open the movie on BookMyShow (Hyderabad). The URL ends in a code like `ET00412345`. Add it under **Variables** (same page as secrets, *Variables* tab):

| Variable | Value | Notes |
|---|---|---|
| `BMS_EVENT_CODES` | `ET00412345` | Several allowed, comma-separated (e.g. Telugu/Hindi/IMAX versions). If left empty, the script tries to discover it automatically. |
| `BMS_DATES` | `20261217,20261218` | Dates to check (default: 17 and 18 Dec). |
| `VENUE_FILTER` | `prasads,amb,imax` | Optional: only alert when one of these cinemas opens. Leave empty to alert on any cinema. |
| `HEARTBEAT_HOUR_IST` | `21` | Hour for the daily "still watching" message; `off` to disable. |
| `CALL_ON_POSSIBLE` | `true` | Optional: also call (not just WhatsApp) if the "Book tickets" button appears but the cinema list can't be read. |

### 6. Test everything
Go to **Actions → Avengers Doomsday ticket watcher → Run workflow**:

1. **mode = test-notify**: you should get a test WhatsApp and a test call within a minute.
2. **mode = probe**: checks that cinema detection works, using a movie that's running *right now*. Open any current movie on BookMyShow Hyderabad, and from its URL (`…/movies/hyderabad/<slug>/<code>`) enter the slug (e.g. `some-movie-name`) and code. Open the run's log/summary: it should list today's cinemas. Nothing is sent in probe mode.
3. **mode = check**: a normal run. After this, it runs on its own every ~10 minutes.

If the probe lists cinemas, you're all set. If it shows `BLOCKED`, see Troubleshooting.

---

## How it works
For each event code and date, the script opens the BookMyShow booking page for Hyderabad. Before bookings open, BookMyShow sends it back to the movie page; once they open, the page contains the cinema list, which it reads. It remembers which cinemas it already told you about (in `state.json`, committed back to the repo), so you get a new WhatsApp when more cinemas open, but only **one** phone call.

It stops on its own after the last date in `BMS_DATES` has passed. You can also disable it any time under Actions → the workflow → "…" → Disable workflow.

## Troubleshooting
- **Logs say BLOCKED:** BookMyShow sometimes blocks traffic from cloud servers. You'll get a WhatsApp warning after ~1 hour of continuous blocking. Options: wait (blocks are often temporary), or run the same script on any always-on machine with a home connection, such as an old Android phone using Termux (`pkg install python && pip install -r requirements.txt`, then run `python check_tickets.py` in a loop every 10 minutes with the same environment variables set).
- **No WhatsApp received:** re-check the API key and that the phone number includes `+91`. CallMeBot occasionally changes its number; redo step 2 if needed.
- **Runs show up late:** GitHub can delay scheduled runs by several minutes during busy periods. That's normal.
- **Scheduled runs stopped:** GitHub pauses schedules on repos with no activity for 60 days. The daily heartbeat commit keeps it active; if you turned the heartbeat off, re-enable the workflow from the Actions tab.

## Running locally (optional)
```bash
pip install -r requirements.txt
export CALLMEBOT_PHONE=+91... CALLMEBOT_APIKEY=...
python check_tickets.py --dry-run      # check, print only
python check_tickets.py --test-notify  # test alerts
```

Please keep the interval at 10 minutes or more; it's gentle on BookMyShow and less likely to get blocked. Automated access may not be permitted by BookMyShow's terms, so use it for personal alerts only.
