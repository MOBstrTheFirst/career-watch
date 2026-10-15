#!/usr/bin/env python3
"""
Career page watcher -> Telegram alerts.

For every page in sites.yaml it:
  1. downloads the page (plain HTTP, or a headless browser for JavaScript pages),
  2. extracts job-like links (and the visible text as a backup),
  3. compares with the last run (state/ folder),
  4. sends a Telegram message with the page link + new postings.

Usage:
  python watcher.py                  # normal run (what GitHub Actions does)
  python watcher.py --check          # test all sites: print what is found, save nothing, send nothing
  python watcher.py --check Siemens  # test only sites whose name contains "Siemens"
  python watcher.py --test-telegram  # send a test message

Split mode (used by GitHub Actions, see .github/workflows/watch.yml):
  python watcher.py --shard 2/4      # check only part 2 of 4 of the list -> state/shard_2.json
  python watcher.py --report 4       # merge the 4 parts, answer Telegram commands, daily summary
Each page always lands in the same part (chosen from a hash of its URL).
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
STATE_FILE = ROOT / "state.json"          # old single-file state (read once for migration)
STATE_DIR = ROOT / "state"
META_FILE = STATE_DIR / "meta.json"
SITE_TIMEOUT = 150          # seconds: one page may never take longer than this
DEFAULT_BUDGET = 11 * 60    # seconds: stop starting new pages after this (job limit is 14 min)

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
    r"^(utm_.*|sid|session.*|jsessionid|phpsessid|token|_ga|_gl|gclid|fbclid|ts|timestamp|cache|rnd|random|"
    r"src|source|_s\.crb|crb|popupopen|_|cb|nocache)$"
    r"|.*(token|session|sessid|nonce|csrf).*",
    re.I)
# Parameter names that may carry a job's identity: never dropped, whatever their value looks like.
ID_PARAM_RE = re.compile(r"id$|^id|job|req|posting|position|vacanc|stelle|ref|oid", re.I)
# Values that look like a random code (long mix of upper case, lower case and digits, e.g. "7pZcKVjU...")
RANDOM_VALUE_RE = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[A-Za-z0-9+/=%_\-]{20,}$")

# Link texts that are never a job: login, page navigation, filters, sharing, language switch.
NAV_TITLE_RE = re.compile(
    r"(sign ?in|sign ?up|log ?in|log ?on|log ?out|anmelden|abmelden|einloggen|registrieren|register|"
    r"mein konto|my account|my profile|mein profil|"
    r"next|next page|previous|previous page|prev|last|last page|first|first page|"
    r"nächste|nächste seite|vorherige|vorherige seite|vorige|vorige seite|letzte|letzte seite|erste|erste seite|"
    r"alle filter|filter|filters|filtern|filter zurücksetzen|reset|reset filters?|alle|keine|"
    r"share|teilen|auf \w+ teilen|share on \w+|per e-?mail (teilen|senden)|link kopieren|copy link|"
    r"deutsch|english|englisch|german|de|en|fr|it|es|pl|cz|français|español|italiano|polski|čeština|"
    r"zum hauptinhalt springen|skip to (main )?content|zurück nach oben|back to top|nach oben|top|"
    r"\d{1,3}([.,]\d{3})*|[«»‹›<>←→]+)",
    re.I)
NAV_TITLE_PREFIX_RE = re.compile(r"^(kategorie|branche|category|standort|location|bereich|filter|ort|land|country)\s*:", re.I)
# Button texts that usually sit next to a real job title link (dropped only if the page has real titles too).
ACTION_TITLE_RE = re.compile(
    r"(apply|apply now|apply online|jetzt bewerben|bewerben|online bewerben|jetzt online bewerben|"
    r"zur bewerbung|bewerbung starten|job speichern|save job|merken|drucken|print)",
    re.I)
NAV_URL_RE = re.compile(
    r"wa\.me/|whatsapp\.com|whatsapp:|t\.me/share|telegram\.me/share|/sharer|[?&]share|"
    r"[?&]loginflowrequired=|/(login|signin|sign-in|logon|logout|anmelden|register|registrierung)(/|\?|$)|"
    r"/checklogin(/|\?|$)", re.I)

# Text-mode noise
COOKIE_RE = re.compile(
    r"cookie|consent|einwilligung|datenschutzeinstellung|privacy settings|tracking|"
    r"alle akzeptieren|alle ablehnen|accept all|reject all|zustimmen|ablehnen|"
    r"usercentrics|onetrust|cookiebot|borlabs", re.I)
COOKIE_BOX_RE = re.compile(
    r"cookie|consent|usercentrics|onetrust|cookiebot|borlabs|klaro|tarteaucitron|cmp-|cmpbox|gdpr|cc-window|"
    r"cc-banner|privacy-banner", re.I)
# Random codes inside text (hex / base64-like, 12+ chars with letters and digits)
RANDOM_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9+/=_\-]*\d)(?=[A-Za-z0-9+/=_\-]*[A-Za-z])[A-Za-z0-9+/=_\-]{12,}\b")
FORGET_LINE_AFTER_DAYS = 14  # text lines seen within this many days do not alert again

TG_API = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")
FAIL_ALERT_AFTER = 3        # consecutive failed runs before you get warned
FORGET_LINK_AFTER_DAYS = 60  # links unseen this long are forgotten
MIN_TEXT_CHARS = 300        # less visible text than this = page probably not loaded


# ----------------------------------------------------------------------------- helpers
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def short_hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]


JOB_KEY_RE = re.compile(r"job|req|posting|position|vacanc|stelle", re.I)


def _volatile(k, v):
    if JOB_KEY_RE.search(k):                              # may identify the job: always keep
        return False
    if VOLATILE_PARAMS_RE.match(k):
        return True
    if ID_PARAM_RE.search(k):
        return False
    if RANDOM_VALUE_RE.match(v):                          # random code, e.g. a session key
        return True
    if re.fullmatch(r"1[5-9]\d{8}(\d{3})?", v):           # a Unix timestamp (seconds or ms)
        return True
    return False


def normalize_url(url):
    url = re.sub(r";jsessionid=[^?#]*", "", url, flags=re.I)
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not _volatile(k, v)])
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc.lower(), path, "", query, ""))


def _clean_title(t):
    t = re.sub(r"[\s|•·:,.!?()\[\]\"'„“”«»‹›<>←→-]+$", "", t or "")
    return re.sub(r"^[\s|•·«»‹›<>←→-]+", "", t).strip() or (t or "").strip()


def is_nav_link(url, title):
    """True for links that are never a job (login, paging, filters, sharing, language)."""
    t = _clean_title(title)
    if NAV_URL_RE.search(url):
        return True
    if t and t != url and (NAV_TITLE_RE.fullmatch(t) or NAV_TITLE_PREFIX_RE.match(t)):
        return True
    return False


def is_action_link(title):
    t = _clean_title(title)
    return bool(t) and bool(ACTION_TITLE_RE.fullmatch(t))


def filter_links(links):
    """Removes navigation links; removes 'Apply now' buttons if the page also has real job titles."""
    links = {u: t for u, t in links.items() if not is_nav_link(u, t)}
    actions = {u for u, t in links.items() if is_action_link(t)}
    if actions and len(links) - len(actions) >= len(actions):
        links = {u: t for u, t in links.items() if u not in actions}
    return links


def clean_line(ln):
    """Text line used for change detection, or None if it is noise."""
    if COOKIE_RE.search(ln) and not JOB_TEXT_RE.search(ln):
        return None
    ln = RANDOM_TOKEN_RE.sub("#", ln)
    if len(re.sub(r"[#\W_]", "", ln)) < 3:
        return None
    return ln


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def read_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"WARNING: {path.name} is broken, starting fresh", file=sys.stderr)
    return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def shard_of(url, n):
    """Stable part number 1..n for a page (same URL -> same part, every run)."""
    return int(hashlib.md5(url.encode("utf-8")).hexdigest(), 16) % n + 1


def shard_file(k):
    return STATE_DIR / f"shard_{k}.json"


def legacy_state():
    """state.json from the old one-job version (used once, so nothing is re-baselined)."""
    return read_json(STATE_FILE, {})


# ----------------------------------------------------------------------------- parsing
def parse_pages(pages, page_url, link_re):
    """pages = list of (html, base_url). Returns (links, cleaned text lines, text char count, cleaned->raw line)."""
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
        chars = len(soup.get_text())
        for tag in soup.find_all(lambda t: t.name not in ("html", "body", "main") and
                                 COOKIE_BOX_RE.search(" ".join([t.get("id") or ""] + (t.get("class") or [])))):
            if tag.decomposed:
                continue
            if len(tag.get_text()) < max(3000, chars // 3):     # never remove a big part of the page
                tag.decompose()
        for ln in soup.get_text("\n").splitlines():
            ln = " ".join(ln.split())
            if len(ln) >= 3:
                lines.append(ln)
    links = filter_links(links)
    total = sum(len(l) for l in lines)
    raw = {}                                   # cleaned line -> original line (for old saved state)
    for l in lines:
        c = clean_line(l)
        if c:
            raw.setdefault(c, l)
    return links, list(raw), total, raw


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
    """Returns dict with links, lines, used_js, or error. Never takes longer than SITE_TIMEOUT."""
    try:
        return await asyncio.wait_for(_scan_site(site, defaults, client, pool, prev), SITE_TIMEOUT)
    except asyncio.TimeoutError:
        return {"error": f"page took longer than {SITE_TIMEOUT}s - skipped this run"}


async def _scan_site(site, defaults, client, pool, prev):
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
                return {"links": res[0], "lines": res[1], "raw": res[3], "used_js": False}
            if js == "auto" and res[0]:
                return {"links": res[0], "lines": res[1], "raw": res[3], "used_js": False}
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
                return {"links": res[0], "lines": res[1], "raw": res[3], "used_js": True}
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
        for attempt in range(5):           # 4 parts may send at the same time -> respect Telegram's rate limit
            try:
                r = await client.post(f"{TG_API}/bot{token}/sendMessage",
                                      data={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                            "disable_web_page_preview": "true"}, timeout=30)
            except httpx.HTTPError as e:
                print(f"Telegram network error: {e}", file=sys.stderr)
                await asyncio.sleep(3)
                continue
            if r.status_code == 429:
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    wait = 5
                await asyncio.sleep(min(wait, 60) + 1)
                continue
            break
        else:
            print("Telegram: gave up after 5 tries", file=sys.stderr)
            return False
        if r.status_code != 200:
            print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
            return False
        await asyncio.sleep(1.1)           # max ~1 message per second per chat
    return True


def esc(s):
    return html.escape(str(s), quote=True)


# ----------------------------------------------------------------------------- compare
def compare(site, prev, cur, mode):
    """Returns (message or None, new_state)."""
    name, url = site["name"], site["url"]
    ts = now_iso()
    # re-apply the current URL cleaning to remembered links (so a rule change never causes fake "new" links)
    old_links = {}
    for u, v in prev.get("links", {}).items():
        nu = normalize_url(u)
        if nu in old_links:
            v = dict(v, first_seen=min(v["first_seen"], old_links[nu]["first_seen"]),
                     last_seen=max(v["last_seen"], old_links[nu]["last_seen"]))
        old_links[nu] = v
    first_run = "line_hashes" not in prev and "line_seen" not in prev
    today = (datetime.now(timezone.utc).date() - datetime(1970, 1, 1).date()).days
    # text-line memory: hash -> day last seen (old state only had the last run's hashes)
    line_seen = dict(prev.get("line_seen", {}))
    for h in prev.get("line_hashes", []):
        line_seen.setdefault(h, today)

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

    # text lines: alert only for lines not seen in the last FORGET_LINE_AFTER_DAYS days
    added_lines, this_run = [], set()
    for ln in cur_lines:
        h = short_hash(ln)
        raw_h = short_hash(cur.get("raw", {}).get(ln, ln))
        if h not in line_seen and raw_h not in line_seen and h not in this_run:
            added_lines.append(ln)
        this_run.add(h)
    for h in this_run:
        line_seen[h] = today
    line_seen = {h: d for h, d in line_seen.items() if today - d <= FORGET_LINE_AFTER_DAYS}

    new_state = {
        "name": name, "links": links_state, "line_seen": dict(sorted(line_seen.items())),
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
        elif "line_seen" in st or "line_hashes" in st:
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
    if meta.get("last_missing_parts"):
        lines += [f"<b>Last run: part(s) {meta['last_missing_parts']} crashed or timed out</b> - their pages were not checked."]
    if meta.get("last_skipped"):
        lines += [f"Last run: {meta['last_skipped']} page(s) skipped (out of time) - they are checked next run."]
    if failing:
        lines += [f"<b>Sites with problems: {len(failing)} (jobs there may be missed):</b>"] + failing[:25]
        if len(failing) > 25:
            lines.append(f"…and {len(failing) - 25} more (send /list for all)")
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
        elif "line_seen" in st or "line_hashes" in st:
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
def load_sites(cfg):
    sites = [s for s in cfg.get("sites", []) if s.get("url")]
    for s in sites:
        s.setdefault("name", urlparse(s["url"]).netloc)
    return sites


async def scan_part(args, cfg, client, k, n):
    """Checks part k of n of the list. Returns (site states of this part, run info)."""
    defaults = cfg.get("defaults", {}) or {}
    all_sites = load_sites(cfg)
    sites = [s for s in all_sites if shard_of(s["url"], n) == k]
    old = read_json(shard_file(k), {})
    if old.get("n") == n:
        state = old.get("sites", {})
    else:                                   # first run, or number of parts changed: reuse any known state
        state = {}
        sources = [legacy_state()] + [read_json(p, {}).get("sites", {}) for p in STATE_DIR.glob("shard_*.json")]
        for src in sources:
            for s in sites:
                if s["url"] in src and s["url"] not in state:
                    state[s["url"]] = src[s["url"]]

    sem = asyncio.Semaphore(int(cfg.get("concurrency", 4)))
    pool = BrowserPool()
    deadline = asyncio.get_event_loop().time() + args.budget
    messages, baselines, events, skipped = [], [], [], []

    async def one(site):
        async with sem:
            if asyncio.get_event_loop().time() > deadline:     # out of time: keep old state, try next run
                return site, None, None
            prev = state.get(site["url"], {})
            cur = await scan_site(site, defaults, client, pool, prev)
            return site, prev, cur

    results = await asyncio.gather(*(one(s) for s in sites))
    await pool.close()

    for site, prev, cur in results:
        name, url = site["name"], site["url"]
        if cur is None:
            skipped.append(name)
            continue
        mode = str(site.get("mode", defaults.get("mode", "auto"))).lower()

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
        real_prev = [u for u, v in prev.get("links", {}).items() if not is_nav_link(u, v.get("title", ""))
                     and not is_action_link(v.get("title", ""))]
        if prev.get("mode") == "links" and mode == "auto" and not cur["links"] and len(real_prev) >= 3:
            st = state[url]
            st["fails"] = st.get("fails", 0) + 1
            st["last_error"] = "no job links found this time (page may not have loaded)"
            print(f"[suspicious {st['fails']}] {name}: 0 links now")
            if st["fails"] == FAIL_ALERT_AFTER:
                messages.append(f"WARNING: <b>{esc(name)}</b> shows 0 job links for {FAIL_ALERT_AFTER} runs "
                                f"(was {len(real_prev)}).\n<a href=\"{esc(url)}\">Open page</a>")
            continue

        if prev.get("fails", 0) >= FAIL_ALERT_AFTER:
            messages.append(f"OK again: <b>{esc(name)}</b> can be read again.")

        msg, new_st, ev = compare(site, prev, cur, mode)
        state[url] = new_st
        events.extend(ev)
        if "line_seen" not in prev and "line_hashes" not in prev:
            baselines.append(f"• {esc(name)}: {new_st['mode']} mode, {len(cur['links'])} links, "
                             f"{'browser' if cur['used_js'] else 'http'}")
        if msg:
            messages.append(msg)
        print(f"[ok] {name}: {new_st['mode']}, {len(cur['links'])} links" + (" -> ALERT" if msg else ""))

    # forget pages that were removed from sites.yaml (or moved to another part)
    wanted = {s["url"] for s in sites}
    state = {u: v for u, v in state.items() if u in wanted}

    if baselines:
        messages.insert(0, f"Started watching {len(baselines)} new page(s) (part {k}/{n}, no alerts on the first run):\n"
                        + "\n".join(baselines))
    if skipped:
        print(f"Time budget used up: {len(skipped)} page(s) skipped this run, they are checked next run.")
    sent_ok = await send_all(args, client, messages)
    stamp = now_iso()
    run_info = {"run_id": os.getenv("GITHUB_RUN_ID", "local"), "at": stamp, "pages": len(sites),
                "alerts": len(messages), "skipped": skipped[:50], "skipped_count": len(skipped),
                "events": [dict(e, at=stamp) for e in events]}
    write_json(shard_file(k), {"n": n, "k": k, "sites": state, "run": run_info})
    return sent_ok


async def report(args, cfg, client, n):
    """Merges all parts: counters, Telegram commands, daily summary. Writes state/meta.json."""
    all_sites = load_sites(cfg)
    meta = read_json(META_FILE, None)
    if meta is None:
        meta = legacy_state().get(META_KEY, {})
    run_id = os.getenv("GITHUB_RUN_ID", "local")
    merged, missing, skipped = {}, [], 0
    for k in range(1, n + 1):
        part = read_json(shard_file(k), {})
        merged.update(part.get("sites", {}))
        info = part.get("run", {})
        if info.get("run_id") != run_id:        # this part crashed / timed out in this run
            missing.append(k)
            continue
        meta["alerts"] = meta.get("alerts", 0) + info.get("alerts", 0)
        meta["events"] = (meta.get("events", []) + info.get("events", []))[-500:]
        skipped += info.get("skipped_count", 0)
    meta["runs"] = meta.get("runs", 0) + 1
    meta["last_run"] = now_iso()
    meta["last_missing_parts"] = missing
    meta["last_skipped"] = skipped
    merged[META_KEY] = meta

    messages = []
    if not args.no_notify:
        messages.extend(await handle_commands(client, cfg, merged, all_sites))
    summary = daily_summary(cfg, merged, all_sites)
    if summary:
        messages.append(summary)
    write_json(META_FILE, merged[META_KEY])
    for p in STATE_DIR.glob("shard_*.json"):    # parts that no longer exist (number of parts was lowered)
        if int(p.stem.split("_")[1]) > n:
            p.unlink()
    return await send_all(args, client, messages)


async def send_all(args, client, messages):
    ok = True
    for m in messages:
        if args.no_notify:
            print("\n[no-notify]\n" + m)
        else:
            ok = await telegram_send(client, m) and ok
    return ok


async def check_mode(args, cfg, client):
    defaults = cfg.get("defaults", {}) or {}
    sites = load_sites(cfg)
    if args.check != "*":
        sites = [s for s in sites if args.check.lower() in s["name"].lower()]
    if not sites:
        sys.exit("No sites found in sites.yaml (or no name matched).")
    sem = asyncio.Semaphore(int(cfg.get("concurrency", 4)))
    pool = BrowserPool()

    async def one(site):
        async with sem:
            return site, await scan_site(site, defaults, client, pool, {})
    results = await asyncio.gather(*(one(s) for s in sites))
    await pool.close()
    for site, cur in results:
        name = site["name"]
        if "error" in cur:
            print(f"\n=== {name}: ERROR - {cur['error']}")
            continue
        print(f"\n=== {name}  [{'browser' if cur['used_js'] else 'http'}] "
              f"{len(cur['links'])} job links, {len(cur['lines'])} text lines")
        for u, t in list(cur["links"].items())[:args.show]:
            print(f"  - {t[:70]!r:72} {u}")
        if not cur["links"]:
            print("  (no job links -> this site will be watched by TEXT changes)")


async def run(args):
    cfg = load_yaml(SITES_FILE)
    async with httpx.AsyncClient(http2=False) as client:
        if args.test_telegram:
            ok = await telegram_send(client, "Career watcher: test message OK.")
            print("Telegram test:", "sent" if ok else "FAILED")
            return
        if args.check is not None:
            return await check_mode(args, cfg, client)
        ok = True
        if args.shard:
            k, n = (int(x) for x in args.shard.split("/"))
            if not 1 <= k <= n:
                sys.exit("--shard must look like 2/4")
            ok = await scan_part(args, cfg, client, k, n)
        elif args.report:
            ok = await report(args, cfg, client, args.report)
        else:                                   # simple local run: whole list in one go
            ok = await scan_part(args, cfg, client, 1, 1)
            ok = await report(args, cfg, client, 1) and ok
        if not ok:
            sys.exit("Some Telegram messages could not be sent (check token / chat ID).")


def main():
    ap = argparse.ArgumentParser(description="Watch careers pages and alert via Telegram.")
    ap.add_argument("--check", nargs="?", const="*", default=None,
                    help="test mode: print what is found per site (optionally filter by name); saves/sends nothing")
    ap.add_argument("--show", type=int, default=10, help="links to print per site in --check mode")
    ap.add_argument("--test-telegram", action="store_true", help="send a test Telegram message")
    ap.add_argument("--no-notify", action="store_true", help="print alerts instead of sending them")
    ap.add_argument("--shard", help="check only one part of the list, e.g. 2/4")
    ap.add_argument("--report", type=int, metavar="N", help="merge N parts, answer commands, daily summary")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                    help="seconds after which no new pages are started (rest is checked next run)")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
