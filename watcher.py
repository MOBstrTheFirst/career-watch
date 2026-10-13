#!/usr/bin/env python3
"""
Career page watcher -> Telegram alerts.

For every page in sites.yaml it:
  1. downloads the page (plain HTTP, or a headless browser for JavaScript pages),
  2. extracts job-like links (and the visible text as a backup),
  3. compares with the last run (state.json),
  4. sends a Telegram message with the page link + new postings.

Usage:
  python watcher.py                  # normal run (what GitHub Actions does)
  python watcher.py --check          # test all sites: print what is found, save nothing, send nothing
  python watcher.py --check Siemens  # test only sites whose name contains "Siemens"
  python watcher.py --test-telegram  # send a test message
"""
import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SITES_FILE = ROOT / "sites.yaml"
STATE_FILE = ROOT / "state.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}

# A link counts as a "job link" if its URL matches this pattern.
JOB_URL_RE = re.compile(
    r"job|career|karriere|stelle|position|vacanc|opening|requisition|posting|"
    r"werkstudent|praktik|intern|thesis|abschlussarbeit|ausschreibung|recruit|bewerb",
    re.I)
# Words that suggest a text line is a job title (used by the text backup check).
JOB_TEXT_RE = re.compile(
    r"werkstudent|working student|studentische|student|intern|praktik|thesis|"
    r"abschlussarbeit|masterarbeit|m/w/d|w/m/d|f/m/d|m/f/d|d/m/w|all genders|"
    r"engineer|ingenieur|developer|entwickler|scientist|wissenschaftl|researcher",
    re.I)
SKIP_URL_RE = re.compile(
    r"^(mailto:|tel:|javascript:|#)|linkedin\.com|xing\.com|facebook\.com|twitter\.com|"
    r"//x\.com|instagram\.com|youtube\.com|kununu\.com|glassdoor\.|tiktok\.com", re.I)
# Query parameters that change on every visit (would cause fake "new" links).
VOLATILE_PARAMS_RE = re.compile(
    r"^(utm_.*|sid|session.*|jsessionid|token|_ga|_gl|gclid|fbclid|ts|timestamp|cache|rnd|random|src|source)$",
    re.I)

TG_API = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")
FAIL_ALERT_AFTER = 3        # consecutive failed runs before you get warned
FORGET_LINK_AFTER_DAYS = 60  # links unseen this long are forgotten
MIN_TEXT_CHARS = 300        # less visible text than this = page probably not loaded


# ----------------------------------------------------------------------------- helpers
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def short_hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]


def normalize_url(url):
    url = re.sub(r";jsessionid=[^?#]*", "", url, flags=re.I)
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not VOLATILE_PARAMS_RE.match(k)])
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc.lower(), path, "", query, ""))


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("WARNING: state.json is broken, starting fresh", file=sys.stderr)
    return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ----------------------------------------------------------------------------- parsing
def parse_pages(pages, page_url, link_re):
    """pages = list of (html, base_url). Returns (links dict, text lines list, text char count)."""
    links, lines = {}, []
    own = normalize_url(page_url)
    for raw_html, base in pages:
        soup = BeautifulSoup(raw_html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or SKIP_URL_RE.search(href):
                continue
            absu = urljoin(base, href)
            if not absu.startswith("http"):
                continue
            if not link_re.search(absu):
                continue
            nu = normalize_url(absu)
            if nu == own:
                continue
            title = " ".join(a.get_text(" ", strip=True).split())[:150]
            if not title:
                title = a.get("title") or a.get("aria-label") or nu
            # keep the longest title seen for this URL
            if len(title) > len(links.get(nu, "")):
                links[nu] = title
        for tag in soup(["script", "style", "noscript", "svg", "template"]):
            tag.decompose()
        for ln in soup.get_text("\n").splitlines():
            ln = " ".join(ln.split())
            if len(ln) >= 3:
                lines.append(ln)
    return links, lines, sum(len(l) for l in lines)


# ----------------------------------------------------------------------------- fetching
async def fetch_http(client, url):
    r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=30)
    r.raise_for_status()
    return [(r.text, str(r.url))]


async def fetch_browser(browser, url, wait_for=None):
    ctx = await browser.new_context(user_agent=UA, locale="de-DE", viewport={"width": 1366, "height": 2000})
    try:
        page = await ctx.new_page()

        async def block_heavy(route):
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", block_heavy)

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        if wait_for:
            try:
                await page.wait_for_selector(wait_for, timeout=20000)
            except Exception:
                pass
        # scroll to trigger lazy-loaded job lists
        for _ in range(4):
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(700)
        await page.wait_for_timeout(1500)

        out = [(await page.content(), page.url)]
        for fr in page.frames[1:]:  # job lists embedded in iframes (Personio, softgarden, ...)
            try:
                if fr.url and fr.url.startswith("http"):
                    out.append((await fr.content(), fr.url))
            except Exception:
                pass
        return out
    finally:
        await ctx.close()


class BrowserPool:
    """Starts Chromium only if some site needs it."""
    def __init__(self):
        self._pw = self._browser = None
        self._lock = asyncio.Lock()

    async def get(self):
        async with self._lock:
            if self._browser is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(args=["--disable-dev-shm-usage"])
            return self._browser

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


async def scan_site(site, defaults, client, pool, prev):
    """Returns dict with links, lines, used_js, or error."""
    url = site["url"]
    js = str(site.get("js", defaults.get("js", "auto"))).lower()
    link_re = re.compile(site["link_pattern"], re.I) if site.get("link_pattern") else JOB_URL_RE
    wait_for = site.get("wait_for")

    def good(res):
        return res[0] or res[2] >= MIN_TEXT_CHARS

    errors = []
    # 1) plain HTTP (skip if js: true, or auto-mode learned that this site needs JS)
    if js == "false" or (js == "auto" and not prev.get("needs_js")):
        try:
            res = parse_pages(await fetch_http(client, url), url, link_re)
            # In auto mode a page with no job links is retried in the browser.
            if js == "false" and good(res):
                return {"links": res[0], "lines": res[1], "used_js": False}
            if js == "auto" and res[0]:
                return {"links": res[0], "lines": res[1], "used_js": False}
            if js == "false":
                errors.append(f"page nearly empty ({res[2]} chars)")
        except Exception as e:
            errors.append(f"HTTP: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"[:200])
            if js == "false":
                return {"error": "; ".join(errors)}

    # 2) headless browser
    if js in ("true", "auto"):
        try:
            browser = await pool.get()
            res = parse_pages(await fetch_browser(browser, url, wait_for), url, link_re)
            if good(res):
                return {"links": res[0], "lines": res[1], "used_js": True}
            errors.append(f"browser: page nearly empty ({res[2]} chars) - blocked or not loaded")
        except Exception as e:
            errors.append(f"browser: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"[:200])
    return {"error": "; ".join(errors) or "unknown error"}


# ----------------------------------------------------------------------------- telegram
async def telegram_send(client, text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("\n[telegram not configured] would send:\n" + text + "\n")
        return True
    chunks, cur = [], ""
    for part in text.split("\n"):
        if len(cur) + len(part) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += part + "\n"
    chunks.append(cur)
    for c in chunks:
        r = await client.post(f"{TG_API}/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}, timeout=30)
        if r.status_code != 200:
            print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
            return False
    return True


def esc(s):
    return html.escape(str(s), quote=True)


# ----------------------------------------------------------------------------- compare
def compare(site, prev, cur, mode):
    """Returns (message or None, new_state)."""
    name, url = site["name"], site["url"]
    ts = now_iso()
    old_links = prev.get("links", {})
    old_lines = set(prev.get("line_hashes", []))
    first_run = "line_hashes" not in prev

    cur_links, cur_lines = cur["links"], cur["lines"]
    use_links = mode == "links" or (mode == "auto" and len(cur_links) > 0)

    # links memory (keeps links for a while even if they vanish, so re-appearing ones don't re-alert)
    links_state = dict(old_links)
    new_links = []
    for u, title in cur_links.items():
        if u in links_state:
            links_state[u]["last_seen"] = ts
        else:
            links_state[u] = {"title": title, "first_seen": ts, "last_seen": ts}
            new_links.append((u, title))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=FORGET_LINK_AFTER_DAYS)).isoformat()
    links_state = {u: v for u, v in links_state.items() if v["last_seen"] >= cutoff}

    seen_line = set()
    added_lines = []
    for ln in cur_lines:
        h = short_hash(ln)
        if h not in old_lines and h not in seen_line:
            added_lines.append(ln)
        seen_line.add(h)

    new_state = {
        "name": name, "links": links_state, "line_hashes": sorted(seen_line),
        "mode": "links" if use_links else "text", "needs_js": cur["used_js"],
        "fails": 0, "last_ok": ts, "last_error": None,
    }
    if first_run:
        return None, new_state, []

    header = f"<b>{esc(name)}</b>\n<a href=\"{esc(url)}\">Open careers page</a>\n"
    if use_links:
        if new_links:
            items = "\n".join(f"• <a href=\"{esc(u)}\">{esc(t)}</a>" for u, t in new_links[:15])
            more = f"\n…and {len(new_links) - 15} more" if len(new_links) > 15 else ""
            ev = [{"site": name, "title": t, "url": u, "kind": "new"} for u, t in new_links]
            return f"NEW: {len(new_links)} new link(s)\n{header}{items}{more}", new_state, ev
        # Backup check: job-looking text appeared but no new link (e.g. jobs without real <a> links)
        jobby = [l for l in added_lines if JOB_TEXT_RE.search(l) and len(l) < 200]
        if jobby:
            items = "\n".join(f"• {esc(l)}" for l in jobby[:10])
            ev = [{"site": name, "title": l[:150], "url": url, "kind": "possible"} for l in jobby[:10]]
            return f"POSSIBLE new posting (text changed, no new link)\n{header}{items}", new_state, ev
        return None, new_state, []

    # text mode
    if added_lines:
        items = "\n".join(f"• {esc(l[:160])}" for l in added_lines[:10])
        more = f"\n…and {len(added_lines) - 10} more line(s)" if len(added_lines) > 10 else ""
        ev = [{"site": name, "title": f"{len(added_lines)} new text line(s), e.g. {added_lines[0][:100]}",
               "url": url, "kind": "text"}]
        return f"CHANGED: page text changed\n{header}{items}{more}", new_state, ev
    return None, new_state, []


# ----------------------------------------------------------------------------- daily summary + commands
META_KEY = "_meta"
HELP_TEXT = ("Commands (read every ~15 min, so replies can take 15-30 min):\n"
             "/status - is the watcher OK, counts since the last daily summary, sites with problems\n"
             "/list - all watched pages with their state\n"
             "/help - this message")


def get_tz(cfg):
    try:
        return ZoneInfo(cfg.get("timezone", "Europe/Berlin"))
    except Exception:
        return timezone.utc


def site_health(state, sites):
    ok, failing, waiting = 0, [], 0
    for s in sites:
        st = state.get(s["url"], {})
        if st.get("fails", 0) > 0:
            failing.append(f"• {esc(s['name'])} (failed {st['fails']}x: {esc((st.get('last_error') or '')[:100])})")
        elif "line_hashes" in st:
            ok += 1
        else:
            waiting += 1
    return ok, failing, waiting


def build_report(cfg, state, sites, title):
    meta = state[META_KEY]
    events = meta.get("events", [])
    new = [e for e in events if e["kind"] == "new"]
    other = [e for e in events if e["kind"] != "new"]
    ok, failing, waiting = site_health(state, sites)
    lines = [f"<b>{esc(title)}</b>",
             "Watcher is alive.",
             f"Runs: {meta.get('runs', 0)} | Alerts sent: {meta.get('alerts', 0)}",
             "", f"<b>New postings: {len(new)}</b>"]
    if new:
        by_site = {}
        for e in new:
            by_site.setdefault(e["site"], []).append(e)
        shown = 0
        for site, items in by_site.items():
            lines.append(f"<i>{esc(site)}</i> ({len(items)})")
            for e in items:
                if shown >= 40:
                    break
                lines.append(f"• <a href=\"{esc(e['url'])}\">{esc(e['title'])}</a>")
                shown += 1
        if len(new) > shown:
            lines.append(f"…and {len(new) - shown} more")
    if other:
        lines += ["", f"Possible postings / page changes: {len(other)}"]
        lines += [f"• {esc(e['site'])}: <a href=\"{esc(e['url'])}\">{esc(e['title'][:100])}</a>" for e in other[:15]]
    lines += ["", f"Sites OK: {ok} of {len(sites)}" + (f" ({waiting} not checked yet)" if waiting else "")]
    if failing:
        lines += ["<b>Sites with problems (jobs there may be missed):</b>"] + failing[:25]
    return "\n".join(lines)


def daily_summary(cfg, state, sites):
    """Sends once per day at `daily_summary_time` (local). If GitHub runs late, it is sent on the next run."""
    t = cfg.get("daily_summary_time", "23:55")
    if t is None or t is False:
        return None
    hh, mm = (int(x) for x in str(t).split(":"))
    now = datetime.now(get_tz(cfg))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < slot:
        slot -= timedelta(days=1)
    key = slot.date().isoformat()
    meta = state[META_KEY]
    if "last_summary_for" not in meta:      # first ever run: don't send a summary right away
        meta["last_summary_for"] = key
        return None
    if meta["last_summary_for"] == key:
        return None
    text = build_report(cfg, state, sites, f"Daily summary – {slot:%a %d %b %Y}")
    late_min = int((now - slot).total_seconds() // 60)
    if late_min > 30:
        text += f"\n\n(Sent {late_min} min late – GitHub started the run late.)"
    meta.update({"last_summary_for": key, "runs": 0, "alerts": 0, "events": []})
    return text


def build_list(state, sites):
    lines = [f"<b>Watched pages: {len(sites)}</b>"]
    for s in sites:
        st = state.get(s["url"], {})
        if st.get("fails", 0) > 0:
            tag = f"PROBLEM ({st['fails']}x)"
        elif "line_hashes" in st:
            tag = f"OK – {st.get('mode')} mode, {len(st.get('links', {}))} links"
        else:
            tag = "not checked yet"
        lines.append(f"• <a href=\"{esc(s['url'])}\">{esc(s['name'])}</a>: {tag}")
    return "\n".join(lines)


async def handle_commands(client, cfg, state, sites):
    """Reads new Telegram messages sent to the bot and answers /status, /list, /help."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return []
    meta = state[META_KEY]
    api = f"{TG_API}/bot{token}"
    if not meta.get("commands_set"):        # show the commands in Telegram's "/" menu (once)
        try:
            cmds = [{"command": "status", "description": "Is the watcher OK? Counts + problems"},
                    {"command": "list", "description": "All watched pages"},
                    {"command": "help", "description": "How the commands work"}]
            r = await client.post(f"{api}/setMyCommands", json={"commands": cmds}, timeout=20)
            meta["commands_set"] = r.status_code == 200
        except Exception:
            pass
    try:
        r = await client.get(f"{api}/getUpdates", timeout=30,
                             params={"offset": meta.get("tg_offset", 0), "timeout": 0,
                                     "allowed_updates": json.dumps(["message"])})
        data = r.json()
    except Exception as e:
        print(f"Could not read Telegram commands: {e}", file=sys.stderr)
        return []
    if not data.get("ok"):
        print(f"Could not read Telegram commands: {data}", file=sys.stderr)
        return []

    replies = []
    stamp = datetime.now(get_tz(cfg)).strftime("%H:%M")
    for upd in data.get("result", []):
        meta["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat):   # ignore everyone except you
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/status":
            replies.append(build_report(cfg, state, sites, f"Status at {stamp} (since last daily summary)"))
        elif cmd == "/list":
            replies.append(build_list(state, sites))
        elif cmd in ("/help", "/start"):
            replies.append(HELP_TEXT)
        else:
            replies.append("Unknown command.\n" + HELP_TEXT)
    return replies


# ----------------------------------------------------------------------------- main
async def run(args):
    cfg = load_yaml(SITES_FILE)
    defaults = cfg.get("defaults", {}) or {}
    sites = [s for s in cfg.get("sites", []) if s.get("url")]
    for s in sites:
        s.setdefault("name", urlparse(s["url"]).netloc)
    if args.check is not None and args.check != "*":
        sites = [s for s in sites if args.check.lower() in s["name"].lower()]
    if not sites:
        sys.exit("No sites found in sites.yaml (or no name matched).")

    state = load_state()
    sem = asyncio.Semaphore(int(cfg.get("concurrency", 4)))
    pool = BrowserPool()
    messages, baselines, events = [], [], []

    async with httpx.AsyncClient(http2=False) as client:
        if args.test_telegram:
            ok = await telegram_send(client, "Career watcher: test message OK.")
            print("Telegram test:", "sent" if ok else "FAILED")
            return

        async def one(site):
            async with sem:
                prev = {} if args.check is not None else state.get(site["url"], {})
                cur = await scan_site(site, defaults, client, pool, prev)
                return site, prev, cur

        results = await asyncio.gather(*(one(s) for s in sites))
        await pool.close()

        for site, prev, cur in results:
            name, url = site["name"], site["url"]
            mode = str(site.get("mode", defaults.get("mode", "auto"))).lower()

            if args.check is not None:
                if "error" in cur:
                    print(f"\n=== {name}: ERROR - {cur['error']}")
                    continue
                print(f"\n=== {name}  [{'browser' if cur['used_js'] else 'http'}] "
                      f"{len(cur['links'])} job links, {len(cur['lines'])} text lines")
                for u, t in list(cur["links"].items())[:args.show]:
                    print(f"  - {t[:70]!r:72} {u}")
                if not cur["links"]:
                    print("  (no job links -> this site will be watched by TEXT changes)")
                continue

            if "error" in cur:
                st = state.setdefault(url, {"name": name})
                st["fails"] = st.get("fails", 0) + 1
                st["last_error"] = cur["error"]
                print(f"[fail {st['fails']}] {name}: {cur['error']}")
                if st["fails"] == FAIL_ALERT_AFTER:
                    messages.append(f"WARNING: cannot read <b>{esc(name)}</b> for {FAIL_ALERT_AFTER} runs.\n"
                                    f"<a href=\"{esc(url)}\">Open page</a>\nError: {esc(cur['error'])}\n"
                                    "New jobs on this page will be MISSED until fixed.")
                continue

            # A page that used to have job links but now has none is suspicious (broken load) -> count as fail.
            if prev.get("mode") == "links" and mode == "auto" and not cur["links"] and len(prev.get("links", {})) >= 3:
                st = state[url]
                st["fails"] = st.get("fails", 0) + 1
                st["last_error"] = "no job links found this time (page may not have loaded)"
                print(f"[suspicious {st['fails']}] {name}: 0 links now")
                if st["fails"] == FAIL_ALERT_AFTER:
                    messages.append(f"WARNING: <b>{esc(name)}</b> shows 0 job links for {FAIL_ALERT_AFTER} runs "
                                    f"(was {len(prev['links'])}).\n<a href=\"{esc(url)}\">Open page</a>")
                continue

            if prev.get("fails", 0) >= FAIL_ALERT_AFTER:
                messages.append(f"OK again: <b>{esc(name)}</b> can be read again.")

            msg, new_st, ev = compare(site, prev, cur, mode)
            state[url] = new_st
            events.extend(ev)
            if "line_hashes" not in prev:
                baselines.append(f"• {esc(name)}: {new_st['mode']} mode, {len(cur['links'])} links, "
                                 f"{'browser' if cur['used_js'] else 'http'}")
            if msg:
                messages.append(msg)
            print(f"[ok] {name}: {new_st['mode']}, {len(cur['links'])} links" + (" -> ALERT" if msg else ""))

        if args.check is not None:
            return

        # forget sites removed from sites.yaml
        wanted = {s["url"] for s in cfg.get("sites", []) if s.get("url")}
        for u in list(state):
            if u not in wanted and u != META_KEY:
                del state[u]

        # counters for the daily summary / status
        meta = state.setdefault(META_KEY, {})
        meta["runs"] = meta.get("runs", 0) + 1
        meta["alerts"] = meta.get("alerts", 0) + len(messages)
        stamp = now_iso()
        meta["events"] = (meta.get("events", []) + [dict(e, at=stamp) for e in events])[-500:]
        meta["last_run"] = stamp

        if not args.no_notify:
            messages.extend(await handle_commands(client, cfg, state, sites))
        summary = daily_summary(cfg, state, sites)
        if summary:
            messages.append(summary)
        save_state(state)

        if baselines:
            messages.insert(0, f"Started watching {len(baselines)} new page(s) (no alerts on the first run):\n"
                            + "\n".join(baselines))
        sent_ok = True
        for m in messages:
            if not args.no_notify:
                sent_ok = await telegram_send(client, m) and sent_ok
            else:
                print("\n[no-notify]\n" + m)
        if not sent_ok:
            sys.exit("Some Telegram messages could not be sent (check token / chat ID).")


def main():
    ap = argparse.ArgumentParser(description="Watch careers pages and alert via Telegram.")
    ap.add_argument("--check", nargs="?", const="*", default=None,
                    help="test mode: print what is found per site (optionally filter by name); saves/sends nothing")
    ap.add_argument("--show", type=int, default=10, help="links to print per site in --check mode")
    ap.add_argument("--test-telegram", action="store_true", help="send a test Telegram message")
    ap.add_argument("--no-notify", action="store_true", help="print alerts instead of sending them")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
