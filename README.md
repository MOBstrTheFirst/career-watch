# Career page watcher → Telegram

Checks your list of careers pages 4× per hour on GitHub (free, runs while your PC is off).
When a new job link appears, you get a Telegram message with the page link and the new job links.

## How it decides "something changed"
- **links mode** (used when the page has job links): alerts on every *new* job link.
  Backup: if new job-like text appears (e.g. "Werkstudent … (m/w/d)") without a new link, you get a "POSSIBLE new posting" alert.
- **text mode** (used when no job links are found): alerts on any new text line. More false alarms.
- **noise filters**: login, page-number, filter, share and language links are ignored; changing session codes are removed from links; cookie-banner text and random codes are ignored in text mode; a text line seen in the last 14 days does not alert again.
- **Safety warnings** (so you don't silently miss jobs): after 3 failed runs in a row, or when a page that had job links suddenly shows none, you get a WARNING message.
- **CRASH alert**: if a whole run fails or times out, a separate GitHub job sends you a Telegram message with a link to the error log.
- **Daily summary at 23:55 Berlin time** (also the "I'm alive" check): runs and alerts that day, every new posting grouped by company with links, possible changes, and sites with problems. **If it doesn't arrive by ~00:30, the watcher has stopped** - open the Actions tab. If GitHub starts the run late, the summary comes on the next run and says how late. Change the time with `daily_summary_time` in `sites.yaml`.

## Telegram commands
Send these to your bot (it also shows them in the "/" menu). Replies come on the next run, so expect **15-30 min delay** - the script is not running between checks.
- `/status` - is it OK, counts and new postings since the last daily summary, sites with problems
- `/list` - all watched pages and their state
- `/help`
Only your chat ID gets answers; messages from anyone else are ignored.
- First run per page = baseline only (no alerts). You get one summary message listing every page and its mode.

## 1. Create the Telegram bot (5 min)
1. In Telegram, open **@BotFather** → send `/newbot` → choose a name and a username ending in `bot`.
2. Copy the **token** it gives you (looks like `123456:ABC-...`). Keep it secret.
3. Open your new bot and press **Start** (send it any message).
4. In a browser open `https://api.telegram.org/bot<TOKEN>/getUpdates` → find `"chat":{"id": 123456789` → that number is your **chat ID**.

## 2. Put it on GitHub (10 min)
1. Create a new **public** repository (see "Why public" below) and upload all files from this folder, keeping the `.github/workflows/` folder.
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN` = your token
   - `TELEGRAM_CHAT_ID` = your chat ID
3. Repo → **Settings → Actions → General → Workflow permissions** → choose **Read and write permissions** → Save.
4. Repo → **Actions** tab → enable workflows if asked → open **career-watch** → **Run workflow**. You should get the "Started watching…" message.

**Why public:** private repos get 2,000 free Action minutes/month. 4 runs/hour = ~2,900 runs/month, so a private repo runs out. Public repos have no minute limit on standard runners. Your secrets stay hidden; only the list of pages (`sites.yaml`, `state/`) is visible.

## 3. Add your pages
Edit `sites.yaml`. The example URLs there are placeholders — check each one in your browser.
- Use a **filtered search URL** (location + "Werkstudent"), so all relevant jobs are on the first page. The script does not click "next page".
- Workday / SuccessFactors / Avature sites → add `js: true`.

## 4. Test a page before adding it (recommended)
On your PC (Windows, PowerShell):
```
py -m pip install -r requirements.txt
py -m playwright install chromium
py watcher.py --check              # all sites
py watcher.py --check Siemens      # one site
```
It prints the job links it found. If it shows **0 job links** but the page clearly lists jobs, try `js: true` and/or `wait_for: "a[href*='job']"`. If it still finds none, the page will be watched in text mode.

Test Telegram locally:
```
$env:TELEGRAM_BOT_TOKEN="..."; $env:TELEGRAM_CHAT_ID="..."; py watcher.py --test-telegram
```

## Limits to know
- GitHub's scheduler is not exact: runs are often 5–20 min late, and sometimes skipped under load.
- **The list is split into 4 parts** that run at the same time on 4 separate GitHub machines (`.github/workflows/watch.yml`). Each part checks ~140 pages in ~3–5 min and has its own 14-min limit. A page always stays in the same part.
- Safety nets: one page can take at most 150 s; after 10 min a part starts no new pages (the rest are checked next run), so a part never hits the 14-min limit. If a part still crashes, you get a CRASH message and the daily summary names the part.
- More parts: change `PARTS: 4` **and** the `part: [1, 2, 3, 4]` list in `watch.yml` together. Old state is reused, nothing is re-alerted.
- Some sites block cloud servers (bot protection). These show up as WARNING messages; check them by hand.
- GitHub may pause scheduled workflows in repos with no activity for 60 days. The bot's own state commits should count as activity; if you ever get no messages for days, check the Actions tab.
