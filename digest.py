# -*- coding: utf-8 -*-
"""
Daily Brief generator -- N AI frontier items + N business knowledge items.

Fetches a curated set of public RSS/Atom/JSON feeds, ranks them by relevance and
freshness, optionally translates them, and renders a self-contained HTML page at
<HOME>/index.html plus a dated archive copy.

Stdlib only -- no pip install, no required API key. Safe to run repeatedly.
"""
from __future__ import annotations

import concurrent.futures as cf
import gzip
import html
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HOME = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HOME, "data")
ARCHIVE = os.path.join(HOME, "archive")
INDEX = os.path.join(HOME, "index.html")
STATE = os.path.join(HOME, "data", "last_good.json")
LOCK = os.path.join(HOME, "data", ".lock")
LOCALES = os.path.join(HOME, "locales")
CONFIG_PATH = os.path.join(HOME, "config.json")

QUIET = "--quiet" in sys.argv
FORCE = "--force" in sys.argv
TRANSLATE = "--no-translate" not in sys.argv
IF_STALE = "--if-stale" in sys.argv


def _arg(name: str):
    """Read `--name value` from argv, or None."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


# ---------------------------------------------------------------- config / i18n
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


CONFIG = load_config()


def detect_lang() -> str:
    """Pick a UI language: --lang, then config.json, then the system locale."""
    for candidate in (_arg("--lang"), CONFIG.get("lang"),
                      os.environ.get("BRIEF_LANG")):
        if candidate:
            return str(candidate)

    # Honour a POSIX locale variable before the OS preference: on Windows the
    # system locale wins otherwise, which makes LANG=... useless for testing
    # and for anyone who deliberately runs a different locale.
    for env in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(env)
        if not val or val in ("C", "POSIX"):
            continue
        code = val.split(".")[0].replace("-", "_").split("_")[0].lower()
        if code == "zh":
            return "zh-CN"
        if code == "en":
            return "en"
    try:
        import locale as _locale
        code = (_locale.getdefaultlocale() or (None,))[0] or ""
        code = code.lower()
        if code.startswith("zh") or "chinese" in code:
            return "zh-CN"
    except Exception:
        pass
    return "en"


LANG = detect_lang()

_FALLBACK_STRINGS = {
    "html_lang": "en", "page_title": "Daily Brief", "kicker": "DAILY BRIEF",
    "section_ai": "AI Frontier", "section_biz": "Business Knowledge",
    "subtitle": "{ai} AI items · {biz} business pieces · built {time} · {sources} sources polled",
    "footer_archive": "Previous days:", "footer_refresh": "Rebuilt daily at 08:00.",
    "no_archive": "none yet", "updating": "Updating…",
    "fetching": "Fetching (attempt {n}/{max})…", "gave_up": "Showing the brief from {date}.",
    "banner_degraded": "Some sources were unreachable just now.",
    "evergreen": "evergreen", "undated": "undated",
    "date_format": "{year}-{month}-{day}", "weekdays": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
    "month_names": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "ago_min": "{n} min ago", "ago_hour": "{n} hour ago", "ago_hours": "{n} hours ago",
    "ago_day": "{n} day ago", "ago_days": "{n} days ago",
    "ago_month": "{n} month ago", "ago_months": "{n} months ago",
    "source_names": {}, "evergreen_concepts": [],
}


def load_strings(lang: str) -> dict:
    data = dict(_FALLBACK_STRINGS)
    for candidate in (lang, "en"):
        path = os.path.join(LOCALES, candidate + ".json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data.update({k: v for k, v in loaded.items() if v not in (None, "", [], {})})
                break
        except Exception:
            continue
    return data


S = load_strings(LANG)

# Every feed is English, so an English page needs no translation at all.
SOURCE_LANG = "en"
TARGET_LANG = "en" if LANG.lower().startswith("en") else LANG

# counts can be overridden from config.json without touching the source
AI_COUNT = int(CONFIG.get("ai_count", 5))
BIZ_COUNT = int(CONFIG.get("biz_count", 5))
WINDOW_HOURS = 96          # prefer items newer than this


def _proxy_url() -> str:
    """The proxy urllib will actually use, if any (VPN clients set one)."""
    try:
        p = urllib.request.getproxies()
    except Exception:
        return ""
    return p.get("https") or p.get("http") or ""


def _using_proxy() -> bool:
    return bool(_proxy_url())


# A VPN routes traffic through a local proxy: requests get slower (a feed that
# answers in 2s directly can take 20s through a tunnel) but far more sites are
# reachable. Scale the deadlines to the network instead of tuning for one.
TUNNELED = _using_proxy()
FETCH_TIMEOUT = 22 if TUNNELED else 6
PROBE_DEADLINE = 6.0 if TUNNELED else 5.0
MAX_PER_SOURCE = 1         # diversity cap: one story per outlet in the first pass

# Structural nudge per outlet: analysis sources up, rolling news down. Kept
# deliberately modest -- freshness is decided by the age decay in score(), and a
# big bonus here is what used to let week-old newsletters beat today's news.
SOURCE_BONUS = {
    "McKinsey": 1.3, "Lenny's Newsletter": 1.1, "MIT Sloan Mgmt Review": 1.1,
    "Not Boring": 1.0, "Economist Business": 0.9,
    "Fortune": -0.8, "Forbes Business": -0.9, "Inc.com": -0.9,
    "MarketWatch": -1.1, "Entrepreneur": -1.0, "CNBC Business": -1.2,
    "OpenAI": 0.8, "Google DeepMind": 0.7, "AI News": 0.5, "Interconnects": 0.4,
    "Hugging Face": 0.3,
    "MarkTechPost": -0.6, "TechCrunch AI": -0.4, "The Verge AI": -0.4, "Wired AI": -0.3,
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# ---------------------------------------------------------------- feed table
# (display name, url, weight)  -- weight nudges ranking, all verified reachable
AI_FEEDS = [
    ("OpenAI",              "https://openai.com/news/rss.xml", 1.50),
    ("Google DeepMind",     "https://deepmind.google/blog/rss.xml", 1.45),
    ("AI News",             "https://buttondown.com/ainews/rss", 1.35),
    ("Interconnects",       "https://www.interconnects.ai/feed", 1.30),
    ("Simon Willison",      "https://simonwillison.net/atom/everything/", 1.20),
    ("BAIR Berkeley",       "https://bair.berkeley.edu/blog/feed.xml", 1.15),
    ("MIT Tech Review",     "https://www.technologyreview.com/topic/artificial-intelligence/feed", 1.10),
    ("arXiv cs.AI",         "http://export.arxiv.org/rss/cs.AI", 1.00),
    ("Microsoft Research",  "https://www.microsoft.com/en-us/research/feed/", 1.00),
    ("Ars Technica",        "https://feeds.arstechnica.com/arstechnica/technology-lab", 0.95),
    ("The Verge AI",        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 0.90),
    ("Wired AI",            "https://www.wired.com/feed/tag/ai/latest/rss", 0.90),
    ("TechCrunch AI",       "https://techcrunch.com/category/artificial-intelligence/feed/", 0.85),
    ("MarkTechPost",        "https://www.marktechpost.com/feed/", 0.70),
    ("Hugging Face",        "https://huggingface.co/blog/feed.xml", 1.15),
]

BIZ_FEEDS = [
    ("McKinsey",            "https://www.mckinsey.com/insights/rss", 1.35),
    ("MIT Sloan Mgmt Review", "https://sloanreview.mit.edu/feed/", 1.25),
    ("Economist Business",  "https://www.economist.com/business/rss.xml", 1.25),
    ("Lenny's Newsletter",  "https://www.lennysnewsletter.com/feed", 1.30),
    ("Not Boring",          "https://www.notboring.co/feed", 1.20),
    ("Fortune",             "https://fortune.com/feed/fortune-feeds/?id=3230629", 0.95),
    ("Forbes Business",     "https://www.forbes.com/business/feed/", 0.85),
    ("Inc.com",             "https://www.inc.com/rss", 0.85),
    ("MarketWatch",         "https://feeds.marketwatch.com/marketwatch/topstories/", 0.80),
    ("Entrepreneur",        "https://www.entrepreneur.com/latest.rss", 0.75),
    ("CNBC Business",       "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147", 0.75),
]

HN_URL = ("https://hn.algolia.com/api/v1/search_by_date"
          "?query=AI%20OR%20LLM%20OR%20%22machine%20learning%22&tags=story&hitsPerPage=40")

AI_KEYWORDS = [
    "release", "launch", "unveil", "announc", "model", "llm", "gpt", "claude", "gemini",
    "llama", "mistral", "deepseek", "qwen", "open-source", "open source", "benchmark",
    "state-of-the-art", "sota", "agent", "reasoning", "inference", "training", "dataset",
    "paper", "research", "breakthrough", "robot", "multimodal", "context window", "fine-tun",
    "chip", "gpu", "scaling", "alignment", "safety", "eval",
]
BIZ_KEYWORDS = [
    "strategy", "growth", "margin", "moat", "pricing", "customer", "market", "revenue",
    "unit economics", "leadership", "negotiat", "decision", "mental model", "hiring",
    "culture", "product", "positioning", "competit", "capital", "investor", "founder",
    "how to", "why ", "framework", "principle", "playbook", "lessons", "case study",
]

# Titles that read like an explainer rather than a headline -- the "knowledge" half.
KNOWLEDGE_HINTS = [
    "how to", "why ", "what ", "the case for", "the case against", "a guide", "guide to",
    "lessons", "principles", "principle", "framework", "mental model", "playbook",
    "mistake", "myth", "truth about", "anatomy of", "economics of", "art of", "science of",
    "less is", "should you", "when to", "the problem with", "rethinking", "beyond ",
    "notes on", "thoughts on", "on strategy", "the power of", "how ", "why ",
]

# Evergreen business knowledge, used only to backfill a short day.
# These come straight from the locale file, so they are already in the page's
# language and never go through the translator.
EVERGREEN = [tuple(x) for x in S.get("evergreen_concepts", [])]

# Link roundups and community digests: they translate into noise and carry no
# transferable lesson, so they should lose to a real article every time.
NOISE_PATTERNS = [
    re.compile(r"weekly dose", re.I),
    re.compile(r"community wisdom", re.I),
    re.compile(r"top contributors", re.I),
    re.compile(r"\bround-?up\b", re.I),
    re.compile(r"\blinks? (for|of) the\b", re.I),
    re.compile(r"what i'?ve been reading", re.I),
    re.compile(r"^\W*[#\d]"),
]

# ---------------------------------------------------------------- utilities
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")


def log(msg: str) -> None:
    if not QUIET:
        try:
            print(msg, flush=True)
        except Exception:
            pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return str(pid) in (out.stdout or "")
        except Exception:
            return True      # cannot tell -> assume alive, fall back to age
    try:
        os.kill(pid, 0)      # POSIX: signal 0 only tests for existence
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists but owned by someone else
    except Exception:
        return True


def take_lock(max_age: int = 900) -> bool:
    """Single-instance guard so a manual open cannot race the 08:00 job.

    The holder's PID is written into the lock, so a lock left behind by a
    crashed run is reclaimed immediately instead of blocking every later run
    until it ages out.
    """
    if FORCE:
        return True
    os.makedirs(DATA, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            holder, age = 0, 0.0
            try:
                with open(LOCK, "r", encoding="utf-8", errors="replace") as f:
                    holder = int((f.read() or "0").strip() or 0)
            except Exception:
                holder = 0
            try:
                age = time.time() - os.path.getmtime(LOCK)
            except OSError:
                return True
            if age < max_age and _pid_alive(holder):
                return False                 # a live run really is in progress
            try:
                os.remove(LOCK)              # crashed or wedged: reclaim it
            except OSError:
                return False
        except OSError:
            return True                      # never block generation over lock trouble
    return False


def release_lock() -> None:
    try:
        os.remove(LOCK)
    except OSError:
        pass


def note_run(text: str) -> None:
    """Append a one-line record so an unattended 08:00 run is diagnosable."""
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "run.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
        # keep the log from growing without bound
        path = os.path.join(DATA, "run.log")
        if os.path.getsize(path) > 200_000:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-300:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(tail)
    except Exception:
        pass


def clean_text(raw: str, limit: int = 240) -> str:
    if not raw:
        return ""
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    txt = re.sub(r"(?i)<br\s*/?>", " ", txt)
    txt = re.sub(r"(?i)</p>", " ", txt)
    txt = _TAG_RE.sub(" ", txt)
    txt = html.unescape(txt)
    txt = _WS_RE.sub(" ", txt).strip()
    if len(txt) > limit:
        cut = txt[:limit]
        sp = cut.rfind(" ")
        if sp > limit * 0.6:
            cut = cut[:sp]
        txt = cut.rstrip(" ,;:-") + "..."
    return txt


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def first_text(node, names) -> str:
    for child in node:
        if local_name(child.tag) in names:
            if child.text and child.text.strip():
                return child.text
            # <content type="html"> may hold text in a child or tail
            if len(child):
                inner = "".join(child.itertext())
                if inner.strip():
                    return inner
    return ""


def extract_link(node) -> str:
    # RSS <link>text</link>
    for child in node:
        if local_name(child.tag) == "link":
            href = child.get("href")
            if href:
                rel = (child.get("rel") or "alternate").lower()
                if rel == "alternate":
                    return href.strip()
            if child.text and child.text.strip():
                return child.text.strip()
    # Atom: pick first alternate href if the loop above only saw non-alternate
    for child in node:
        if local_name(child.tag) == "link" and child.get("href"):
            return child.get("href").strip()
    # guid fallback
    for child in node:
        if local_name(child.tag) in ("guid", "id"):
            t = (child.text or "").strip()
            if t.startswith("http"):
                return t
    return ""


def parse_date(node):
    raw = first_text(node, {"pubdate", "published", "updated", "date", "dc:date", "issued"})
    if not raw:
        return None
    raw = raw.strip()
    # Feeds like McKinsey publish "Thu, 22 Oct 2026" -- no time, no timezone --
    # which parsedate_to_datetime rejects outright. Without this the item looks
    # undated and escapes every freshness rule, so it shows up as "常青内容"
    # no matter how old it actually is.
    candidates = [raw]
    if ":" not in raw:
        candidates.append(raw + " 00:00:00 GMT")
        candidates.append(raw + " 00:00:00 +0000")
    for candidate in candidates:
        for parser in (parsedate_to_datetime, _parse_iso):
            try:
                dt = parser(candidate)
                if dt is None:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone()
            except Exception:
                continue
    return None


def _parse_iso(raw: str):
    s = raw.strip().replace("Z", "+00:00")
    s = re.sub(r"\.(\d{3})\d*", r".\1", s)
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", s)
    if m:
        y, mo, d, h, mi, se = (int(x) for x in m.groups())
        return datetime(y, mo, d, h, mi, se, tzinfo=timezone.utc)
    return None


def _read_url(req, timeout: float, limit: int = 3_000_000) -> bytes:
    """GET with a hard deadline; a stalled socket cannot outlive `timeout`."""
    box: dict = {}

    def worker():
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                box["raw"] = r.read(limit)
                box["enc"] = (r.headers.get("Content-Encoding") or "").lower()
        except Exception as e:                      # noqa: BLE001
            box["error"] = e

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(timeout + 3.0)
    if th.is_alive():
        raise TimeoutError(f"no response within {timeout + 3.0:.0f}s")
    if "error" in box:
        raise box["error"]
    if "raw" not in box:
        raise RuntimeError("empty response")
    return box["raw"], box.get("enc", "")


def fetch(url: str, tries: int = 1) -> bytes:
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": ("application/rss+xml, application/atom+xml, application/xml, "
                       "application/json, text/xml;q=0.9, */*;q=0.8"),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        })
        try:
            raw, enc = _read_url(req, FETCH_TIMEOUT)
            # some CDNs gzip the body even when identity is requested
            if raw[:2] == b"\x1f\x8b" or "gzip" in enc:
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    raw = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
            elif raw[:1] == b"\x78" or "deflate" in enc:
                try:
                    raw = zlib.decompress(raw)
                except Exception:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw
        except Exception as e:                      # noqa: BLE001
            last = e
            if attempt + 1 < tries:
                time.sleep(0.8)
    raise last  # type: ignore[misc]


def parse_feed(name: str, weight: float, raw: bytes) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        text = raw.decode("utf-8", "replace")
        root = ET.fromstring(re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text))
    nodes = [n for n in root.iter() if local_name(n.tag) in ("item", "entry")]
    for node in nodes:
        title = clean_text(first_text(node, {"title"}), 300)
        link = extract_link(node)
        if not title or not link:
            continue
        summary = clean_text(
            first_text(node, {"description", "summary", "content", "encoded", "subtitle"}), 260)
        items.append({
            "title": title,
            "link": link,
            "source": name,
            "weight": weight,
            "summary": summary,
            "published": parse_date(node),
        })
    return items


def parse_hn(raw: bytes) -> list[dict]:
    items = []
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return items
    for hit in data.get("hits", []):
        title = (hit.get("title") or "").strip()
        link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        if not title:
            continue
        pts = hit.get("points") or 0
        if pts < 60:
            continue
        pub = _parse_iso(hit.get("created_at") or "")
        items.append({
            "title": clean_text(title, 300),
            "link": link,
            "source": "Hacker News",
            "weight": min(1.25, 0.9 + pts / 800.0),
            "summary": f"{pts} points - {hit.get('num_comments') or 0} comments on Hacker News.",
            "published": pub.astimezone() if pub else None,
        })
    return items


# ---------------------------------------------------------------- ranking
def norm_key(title: str) -> frozenset:
    stop = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "is", "are", "with",
            "how", "why", "what", "new", "your", "you", "it", "its", "as", "at", "by",
            "from", "that", "this", "be", "can", "will", "s", "t"}
    return frozenset(w for w in _WORD_RE.findall(title.lower()) if w not in stop and len(w) > 2)


# Outlets that publish lots of general material: their stories only qualify when
# the headline itself carries the category signal (no "Apple smart home" in the
# AI column, no "oil theft" in the business column).
STRICT_SOURCES = {
    "TechCrunch AI", "The Verge AI", "Wired AI", "Ars Technica", "MarkTechPost",
    "Microsoft Research", "Simon Willison", "Fortune", "CNBC Business", "Entrepreneur",
    "Forbes Business", "Inc.com", "MarketWatch",
}


def kw_match(text: str, k: str) -> bool:
    return re.search(r"\b" + re.escape(k), text) is not None


def score(item: dict, keywords: list[str], now: datetime, hints: tuple = (),
          window: float = WINDOW_HOURS, age_scale: float = 40.0) -> float:
    title = item["title"].lower()
    body = item.get("summary", "").lower()
    title_hits = [k for k in keywords if kw_match(title, k)]
    if item["source"] in STRICT_SOURCES and not title_hits:
        return -999.0

    s = float(item.get("weight", 1.0)) + SOURCE_BONUS.get(item["source"], 0.0)
    if any(p.search(item["title"]) for p in NOISE_PATTERNS):
        s -= 3.5
    pub = item.get("published")
    if pub:
        hours = max(0.0, (now - pub).total_seconds() / 3600.0)
        s += 2.0 * max(0.0, 1.0 - hours / 48.0)    # modest reward for < 2 days
        s -= hours / age_scale                      # age cost, never stops
        if hours > window:
            s -= 4.0
    else:
        # undated is not trustworthy: it cannot be checked for staleness
        s -= 2.0
    # signal in the headline counts far more than signal buried in the blurb
    s += min(2.5, len(title_hits) * 0.45)
    s += min(1.0, sum(0.15 for k in keywords if kw_match(body, k)))
    if hints:
        s += min(1.6, sum(0.55 for h in hints if kw_match(title, h)))
    return s


def select(items: list[dict], keywords: list[str], count: int, now: datetime,
           hints: tuple = (), window: float = WINDOW_HOURS,
           age_scale: float = 40.0) -> list[dict]:
    ranked = sorted(items, key=lambda it: score(it, keywords, now, hints, window, age_scale),
                    reverse=True)
    picked: list[dict] = []
    seen_keys: list[frozenset] = []
    seen_links: set[str] = set()
    taken: set[int] = set()

    # Pass 1 gives every outlet one slot; later passes relax the cap only as far
    # as needed, so a short day degrades to a second story rather than a blank.
    for cap in (MAX_PER_SOURCE, MAX_PER_SOURCE + 1, 99):
        if len(picked) >= count:
            break
        per_source: dict[str, int] = {}
        for it in picked:
            per_source[it["source"]] = per_source.get(it["source"], 0) + 1
        for it in ranked:
            if len(picked) >= count:
                break
            if id(it) in taken:
                continue
            if score(it, keywords, now, hints, window, age_scale) < -500:
                continue
            link = it["link"].split("?")[0].rstrip("/")
            if link in seen_links:
                continue
            k = norm_key(it["title"])
            if k and any(len(k & o) / max(1, len(k | o)) > 0.55 for o in seen_keys):
                continue
            if per_source.get(it["source"], 0) >= cap:
                continue
            seen_links.add(link)
            seen_keys.append(k)
            per_source[it["source"]] = per_source.get(it["source"], 0) + 1
            taken.add(id(it))
            picked.append(it)
    return picked


# ---------------------------------------------------------------- translation
T_CACHE_PATH = os.path.join(DATA, "translate_cache.json")
T_ORDER_PATH = os.path.join(DATA, "provider_order.json")
T_MAIN = "https://clients5.google.com/translate_a/t"          # free, undocumented
T_FALLBACK = "https://api.mymemory.translated.net/get"        # free, capped
T_DEEPL = "https://api-free.deepl.com/v2/translate"           # keyed
T_DEEPL_PRO = "https://api.deepl.com/v2/translate"            # keyed, paid plans
T_GOOGLE_API = "https://translation.googleapis.com/language/translate/v2"  # keyed
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# How many strings a provider takes per request. Free Google accepts a batch;
# the keyed APIs do too; MyMemory is one string at a time and rate limited.
BATCH_SIZE = {"google": 12, "mymemory": 4, "deepl": 20, "google_official": 20}

# Ceiling on the whole translation phase. Past this the page renders with
# whatever was translated and keeps English for the rest -- a stale brief is
# worse than a partly-English one, but a hung run is worse than both.
TRANSLATE_BUDGET = 150 if TUNNELED else 60

# Which provider to try first. Google batches many strings per request and is
# far better, but it is blocked outright on some networks and then swallows the
# connection instead of refusing it, so the free order is decided by a probe.
# A keyed provider is always tried before the free ones when its key is set.
PROBE_TEXT = "hello"


def _provider_keys() -> dict:
    """API keys, from the environment or config.json (env wins)."""
    cfg = CONFIG.get("translate") or {}
    return {
        "deepl": os.environ.get("DEEPL_API_KEY") or cfg.get("deepl_api_key") or "",
        "google_official": (os.environ.get("GOOGLE_TRANSLATE_API_KEY")
                            or cfg.get("google_api_key") or ""),
    }


def _lang_code(provider: str) -> str:
    """Target-language code in the dialect each provider expects."""
    if TARGET_LANG.lower().startswith("zh"):
        return {"deepl": "ZH", "google_official": "zh-CN"}.get(provider, "zh-CN")
    return {"deepl": TARGET_LANG.split("-")[0].upper(),
            "google_official": TARGET_LANG}.get(provider, TARGET_LANG)


def _tcp_reachable(url: str, timeout: float = 4.0) -> bool:
    """Cheap reachability test for a URL's host.

    A blocked host swallows the SYN instead of refusing it, so a connect test
    with a short deadline separates 'blocked' from 'slow' far faster than a full
    request. Crucially, when a proxy is configured the connection that matters is
    the one to the PROXY -- the proxy resolves and fetches remotely, so testing
    the target directly would wrongly report a totally usable endpoint as dead.
    """
    proxy = _proxy_url()
    if proxy:
        parts = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        host = parts.hostname or ""
        port = parts.port or 8080
    else:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
    box: dict = {}

    def worker():
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except Exception:
            box["ok"] = False
            return
        for family, socktype, proto, _, sockaddr in infos[:3]:
            s = socket.socket(family, socktype, proto)
            s.settimeout(timeout)
            try:
                s.connect(sockaddr)
                box["ok"] = True
                return
            except Exception:
                continue
            finally:
                s.close()
        box["ok"] = False

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(timeout * 4 + 1.0)
    return bool(box.get("ok"))


def has_cjk(s: str) -> bool:
    return bool(s) and CJK_RE.search(s) is not None


# Names that must survive translation untouched. Google happily renders
# "Claude Duet" as a person called 克劳德·杜埃, which is worse than useless, so
# these are swapped for {{n}} guards before the request and put back after.
PROTECTED_TERMS = [
    "Claude Duet", "Claude", "ChatGPT", "GPT-6.1", "GPT-5", "GPT-4", "GPT",
    "Gemini", "DeepMind", "Google Cloud", "Model Garden", "Cloud Marketplace",
    "Codex", "Copilot", "Muse", "Dots", "Flint", "Recursive", "Voxtral",
    "Mistral", "Qwen", "Llama", "DeepSeek", "Granite", "SWE-bench",
    "MAMath-Bench", "Hugging Face", "MacBook", "Sora", "Grok", "Midjourney",
    "Stable Diffusion", "Anthropic", "OpenAI", "NVIDIA", "PyTorch",
    "TensorFlow", "arXiv", "Apache", "Linux", "Kubernetes",
]
_PROTECT_RE = None


def _protect(text: str):
    """Swap protected names for {{n}} guards; return (guarded_text, mapping)."""
    global _PROTECT_RE
    if _PROTECT_RE is None:
        terms = sorted(PROTECTED_TERMS, key=len, reverse=True)
        _PROTECT_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b",
                                 re.IGNORECASE)
    mapping: dict[str, str] = {}

    def repl(m):
        key = "{{%d}}" % len(mapping)
        mapping[key] = m.group(0)
        return key

    return _PROTECT_RE.sub(repl, text), mapping


def _restore(text: str, mapping: dict) -> str:
    for key, original in mapping.items():
        n = key[2:-2]
        text = re.sub(r"\{\{\s*" + n + r"\s*\}\}", original.replace("\\", "\\\\"), text)
    # A bare classifier straight after a restored brand name is an artifact of
    # the guard: "{{0}} models" comes back as "OpenAI 个模型" instead of
    # "OpenAI 的模型". Only fires directly after the name, so "有 3 个模型"
    # (a real classifier) is left alone.
    for original in set(mapping.values()):
        text = re.sub(r"(" + re.escape(original) + r")\s*个(?=[\u4e00-\u9fff])",
                      r"\1 的", text)
    return text


class Translator:
    """English -> Chinese with an on-disk cache and two providers.

    The daily job runs unattended, so this must never raise: anything it cannot
    translate stays in English rather than leaving a hole in the page.
    """

    def __init__(self):
        self._order = None
        self.cache: dict[str, str] = {}
        try:
            with open(T_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.cache = {k: v for k, v in data.items() if isinstance(v, str)}
        except Exception:
            self.cache = {}

    # -- providers -------------------------------------------------------
    def _provider_order(self) -> list:
        """Keep the reachable endpoint first.

        A blocked host does not refuse the connection, it swallows it, so
        without this the dead provider eats the whole budget before the working
        one is ever tried. Both probes run at once and the verdict is cached,
        because which endpoints a network blocks changes slowly.
        """
        if self._order is not None:
            return self._order

        keys = _provider_keys()
        # keyed providers cost nothing to "probe" -- they are tried first when a
        # key is present, because they are stable and have no daily cap.
        keyed = []
        if keys["deepl"]:
            keyed.append(("deepl", T_DEEPL))
        if keys["google_official"]:
            keyed.append(("google_official", T_GOOGLE_API))
        if keyed and not CONFIG.get("translate", {}).get("also_try_free", True):
            self._order = [n for n, _ in keyed]
            log(f"  translator: using keyed provider(s) only: {self._order}")
            return self._order

        cached = self._load_order()
        if cached and not keyed:
            self._order = cached
            log(f"  translator order (cached): {cached}")
            return cached

        checks = [
            ("google", T_MAIN),
            ("mymemory", T_FALLBACK),
        ]
        results: dict[str, bool] = {}

        def run(name, url):
            results[name] = _tcp_reachable(url)

        threads = [threading.Thread(target=run, args=(n, u), daemon=True)
                   for n, u in checks]
        for th in threads:
            th.start()
        for th in threads:
            th.join(PROBE_DEADLINE + 4.0)
        for n, _ in checks:
            results.setdefault(n, False)

        healthy = [n for n, _ in checks if results.get(n)]
        other = [n for n, _ in checks if not results.get(n)]
        self._order = [n for n, _ in keyed] + healthy + other
        log(f"  translator reachability: ok={healthy or 'none'} "
            f"blocked={other or 'none'}"
            + (f" keyed={[n for n, _ in keyed]}" if keyed else ""))
        # only persist a verdict that found something usable, so a transient
        # outage does not get baked in for the next few days
        if healthy and not keyed:
            self._save_order(self._order)
        return self._order

    @staticmethod
    def _load_order(max_age: int = 72 * 3600):
        try:
            with open(T_ORDER_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            # a saved verdict only holds for the same kind of network: turning a
            # VPN on or off changes which endpoints are reachable
            if bool(d.get("proxy")) != TUNNELED:
                return None
            if time.time() - d.get("at", 0) < max_age and d.get("order"):
                return [str(x) for x in d["order"]]
        except Exception:
            pass
        return None

    @staticmethod
    def _save_order(order) -> None:
        try:
            os.makedirs(DATA, exist_ok=True)
            tmp = T_ORDER_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"at": time.time(), "proxy": TUNNELED, "order": list(order)}, f)
            os.replace(tmp, T_ORDER_PATH)
        except Exception:
            pass

    @staticmethod
    def _http(url: str, timeout: float = 8.0, method: str = "GET",
              data: bytes | None = None, headers: dict | None = None) -> str:
        """Request with a HARD wall-clock deadline.

        urlopen(timeout=...) only bounds individual socket operations, so a
        connection that stalls in SYN_SENT (common when a host resolves to
        several unreachable IPs) can block far past it and hang the whole run.
        Running the request on a daemon thread lets us abandon it outright.
        """
        box: dict = {}

        def worker():
            try:
                hdrs = {
                    "User-Agent": UA,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Referer": "https://translate.google.com/",
                }
                hdrs.update(headers or {})
                req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
                with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                    raw = r.read(500000)
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                box["body"] = raw.decode("utf-8", "replace")
            except Exception as e:                  # noqa: BLE001
                box["error"] = e

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        th.join(timeout + 3.0)
        if th.is_alive():
            # abandoned on purpose: daemon thread dies with the process
            raise TimeoutError(f"request exceeded {timeout + 3.0:.0f}s deadline")
        if "error" in box:
            raise box["error"]
        if "body" not in box:
            raise RuntimeError("empty response")
        return box["body"]

    def _google(self, texts: list[str]) -> list[str]:
        params = [("client", "dict-chrome-ex"), ("sl", SOURCE_LANG),
                  ("tl", _lang_code("google")), ]
        params += [("q", t) for t in texts]
        body = self._http(T_MAIN + "?" + urllib.parse.urlencode(params))
        data = json.loads(body)
        if isinstance(data, str):
            data = [data]
        out = []
        for item in data:
            if isinstance(item, list):
                item = "".join(str(x) for x in item if isinstance(x, str))
            out.append((item or "").strip() if isinstance(item, str) else "")
        return out

    def _deepl(self, texts: list[str]) -> list[str]:
        key = _provider_keys()["deepl"]
        if not key:
            raise RuntimeError("no DEEPL_API_KEY")
        endpoint = T_DEEPL if key.strip().endswith(":fx") else T_DEEPL_PRO
        params = [("source_lang", SOURCE_LANG.upper()),
                  ("target_lang", _lang_code("deepl"))]
        params += [("text", t) for t in texts]
        body = self._http(endpoint + "?" + urllib.parse.urlencode(params),
                          headers={"Authorization": "DeepL-Auth-Key " + key})
        data = json.loads(body)
        return [tr.get("text", "").strip() for tr in data.get("translations", [])]

    def _google_official(self, texts: list[str]) -> list[str]:
        key = _provider_keys()["google_official"]
        if not key:
            raise RuntimeError("no GOOGLE_TRANSLATE_API_KEY")
        payload = json.dumps({"q": texts, "source": SOURCE_LANG,
                              "target": _lang_code("google_official"),
                              "format": "text"}).encode()
        body = self._http(T_GOOGLE_API + "?key=" + urllib.parse.quote(key),
                          method="POST", data=payload,
                          headers={"Content-Type": "application/json"})
        data = json.loads(body)
        out = []
        for tr in data.get("data", {}).get("translations", []):
            text = tr.get("translatedText", "")
            out.append(html.unescape(text).strip())
        return out

    def _mymemory(self, text: str) -> str:
        # anonymous tier caps a query at ~500 bytes; split on sentence edges
        chunks, buf = [], ""
        for piece in re.split(r"(?<=[.!?])\s+", text):
            if buf and len(buf.encode("utf-8")) + len(piece.encode("utf-8")) > 450:
                chunks.append(buf)
                buf = piece
            else:
                buf = (buf + " " + piece).strip() if buf else piece
        if buf:
            chunks.append(buf)
        parts = []
        for c in chunks[:6]:
            url = T_FALLBACK + "?" + urllib.parse.urlencode(
                {"q": c, "langpair": "%s|%s" % (SOURCE_LANG, _lang_code("mymemory"))})
            j = json.loads(self._http(url, timeout=10))
            if str(j.get("responseStatus")) != "200":
                raise RuntimeError("mymemory status %s" % j.get("responseStatus"))
            parts.append(j["responseData"]["translatedText"])
        return " ".join(p.strip() for p in parts if p).strip()

    def _via(self, name: str, texts: list[str]) -> list[str]:
        """Translate a batch through one named provider."""
        if name == "google":
            return self._google(texts)
        if name == "deepl":
            return self._deepl(texts)
        if name == "google_official":
            return self._google_official(texts)
        return [self._mymemory(t) for t in texts]

    # -- batching --------------------------------------------------------
    @staticmethod
    def _chunks(texts: list[str], max_len: int = 3000, max_items: int = 12):
        batch, size = [], 0
        for t in texts:
            n = len(urllib.parse.quote(t))
            if batch and (size + n > max_len or len(batch) >= max_items):
                yield batch
                batch, size = [], 0
            batch.append(t)
            size += n
        if batch:
            yield batch

    # -- public ----------------------------------------------------------
    def translate_many(self, texts: list[str]) -> dict[str, str]:
        wanted = [t for t in dict.fromkeys(texts) if t and t.strip() and not has_cjk(t)]
        done = {t: self.cache[t] for t in wanted if has_cjk(self.cache.get(t, ""))}
        todo = [t for t in wanted if t not in done]
        if not todo:
            return done

        deadline = time.time() + TRANSLATE_BUDGET
        new: dict[str, str] = {}

        def out_of_time() -> bool:
            return time.time() >= deadline

        # hide brand/product names behind {{n}} guards and remember how to undo it
        guarded: dict[str, tuple] = {}          # guarded text -> (original, mapping)
        for t in todo:
            g, mapping = _protect(t)
            guarded.setdefault(g, (t, mapping))
        work = list(guarded)

        order = self._provider_order()
        delivered = None
        for name in order:                          # reachable provider first
            if out_of_time():
                break
            remaining = [g for g in work if guarded[g][0] not in new]
            if not remaining:
                break
            # batch size varies by provider: Google and the keyed APIs accept
            # many strings per request; MyMemory is one at a time.
            for batch in self._chunks(remaining, max_items=BATCH_SIZE.get(name, 4)):
                if out_of_time():
                    break
                try:
                    results = self._via(name, batch)
                except Exception as e:              # noqa: BLE001
                    log(f"  ! translate via {name}: {type(e).__name__}: {str(e)[:60]}")
                    break                           # provider is unhappy: next one
                got = 0
                for src, dst in zip(batch, results):
                    original, mapping = guarded[src]
                    restored = _restore(dst, mapping)
                    if has_cjk(restored):
                        new[original] = restored
                        got += 1
                if got:
                    delivered = name

        # remember who actually worked, so the next run skips the dead one
        if delivered and order and order[0] != delivered:
            self._order = [delivered] + [n for n in order if n != delivered]
            self._save_order(self._order)

        self.cache.update(new)
        self.save()
        missed = len(todo) - len(new)
        log(f"  translated {len(new)}/{len(todo)} new strings "
            f"({len(wanted) - len(todo)} cached)"
            + (f" -- {missed} left in English" if missed else ""))
        return {**done, **new}

    def save(self) -> None:
        try:
            if len(self.cache) > 4000:              # keep the cache bounded
                self.cache = dict(list(self.cache.items())[-2500:])
            os.makedirs(DATA, exist_ok=True)
            tmp = T_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False)
            os.replace(tmp, T_CACHE_PATH)
        except Exception:
            pass


def localize(items: list[dict]) -> None:
    """Attach translated title/summary in place; the original stays for hover."""
    if not TRANSLATE or SOURCE_LANG == TARGET_LANG:
        return
    tr = Translator()
    strings: list[str] = []
    for it in items:
        if it.get("pre_localized"):
            it["title_zh"] = it["title"]
            it["summary_zh"] = it.get("summary", "")
            continue
        strings.append(it["title"])
        if it.get("summary"):
            strings.append(it["summary"])
    if not strings:
        return
    mapping = tr.translate_many(strings)
    for it in items:
        if it.get("pre_localized"):
            continue
        it["title_zh"] = mapping.get(it["title"]) or it["title"]
        if it.get("summary"):
            it["summary_zh"] = mapping.get(it["summary"]) or it["summary"]


# ---------------------------------------------------------------- rendering
def ago(pub) -> str:
    if not pub:
        return ""
    now = datetime.now(pub.tzinfo or None)
    secs = max(0, int((now - pub).total_seconds()))
    if secs < 3600:
        return S["ago_min"].format(n=max(1, secs // 60))
    if secs < 86400:
        h = secs // 3600
        return S["ago_hour" if h == 1 else "ago_hours"].format(n=h)
    d = secs // 86400
    if d < 31:
        return S["ago_day" if d == 1 else "ago_days"].format(n=d)
    m = max(1, d // 30)
    return S["ago_month" if m == 1 else "ago_months"].format(n=m)


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def src_name(name: str) -> str:
    return (S.get("source_names") or {}).get(name, name)


def render_date(now: datetime) -> str:
    return S["date_format"].format(
        year=now.year, month=now.month, day=now.day,
        weekday=S["weekdays"][(now.weekday() + 1) % 7],   # locale lists start Sunday
        month_name=(S.get("month_names") or ["--"] * 12)[now.month - 1],
    )


def render_card(i: int, it: dict, extra_source: str | None = None) -> str:
    name = extra_source or it["source"]
    src = src_name(name)
    title = it.get("title_zh") or it["title"]
    summ = it.get("summary_zh") or it.get("summary") or ""
    summ_html = f'<p class="sum">{esc(summ)}</p>' if summ else ""
    pub = it.get("published")
    when = ago(pub) if pub else (it.get("meta") or S["evergreen"])
    time_title = pub.strftime("%Y-%m-%d %H:%M") if pub else ""
    if it.get("link"):
        head = (f'<a href="{esc(it["link"])}" target="_blank" rel="noopener" '
                f'title="{esc(it["title"])}">{esc(title)}</a>')
    else:
        head = esc(title)
    return f"""      <article class="card">
        <div class="rank">{i:02d}</div>
        <div class="body">
          <h3>{head}</h3>
          {summ_html}
          <div class="meta"><span class="src">{esc(src)}</span><span class="dot">&middot;</span><span title="{esc(time_title)}">{esc(when)}</span></div>
        </div>
      </article>"""


def render(ai_items, biz_items, generated: datetime, total_sources: int, degraded: bool) -> str:
    now = generated
    date_line = render_date(now)
    stamp = now.strftime("%Y-%m-%d %H:%M")

    ai_html = "\n".join(render_card(i + 1, it) for i, it in enumerate(ai_items))
    biz_html = "\n".join(render_card(i + 1, it) for i, it in enumerate(biz_items))

    arch = []
    if os.path.isdir(ARCHIVE):
        files = sorted((f for f in os.listdir(ARCHIVE) if f.endswith(".html")), reverse=True)[:8]
        for f in files:
            day = f[:-5]
            arch.append(f'<a href="archive/{esc(f)}">{esc(day)}</a>')
    arch_html = " ".join(arch) if arch else f"<span>{esc(S['no_archive'])}</span>"

    banner = ""
    if degraded:
        banner = f'<div class="banner">{esc(S["banner_degraded"])}</div>'

    sub_line = S["subtitle"].format(ai=len(ai_items), biz=len(biz_items),
                                    time=stamp, sources=total_sources)

    return f"""<!doctype html>
<html lang="{esc(S['html_lang'])}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(S['page_title'])} - {esc(date_line)}</title>
<style>
  :root {{
    --bg: #f5f6f8; --panel: #ffffff; --ink: #14181f; --muted: #6b7280;
    --line: #e5e7eb; --accent: #2563eb; --accent2: #b45309; --chip: #f3f4f6;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f1216; --panel: #171b21; --ink: #e8eaed; --muted: #9aa3af;
      --line: #262c35; --accent: #6ea8fe; --accent2: #f0b429; --chip: #20262e;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.7 "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
          "Noto Sans CJK SC", "Source Han Sans SC", "Segoe UI", sans-serif;
  }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 28px 20px 56px; }}
  header.top {{ border-bottom: 2px solid var(--ink); padding-bottom: 14px; margin-bottom: 26px; }}
  .kicker {{ font-size: 12px; letter-spacing: .34em; color: var(--muted); }}
  h1 {{ margin: 6px 0 4px; font-size: 28px; letter-spacing: .01em; }}
  .sub {{ color: var(--muted); font-size: 13px; }}
  .banner {{ margin: 14px 0 0; padding: 10px 14px; border-left: 3px solid var(--accent2);
             background: var(--chip); font-size: 13px; color: var(--muted); border-radius: 0 6px 6px 0; }}
  .cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 26px; }}
  @media (max-width: 900px) {{ .cols {{ grid-template-columns: 1fr; }} h1 {{ font-size: 22px; }} }}
  section h2 {{ font-size: 13px; letter-spacing: .22em;
                margin: 0 0 12px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }}
  section.ai h2 {{ color: var(--accent); }}
  section.biz h2 {{ color: var(--accent2); }}
  .card {{
    display: flex; gap: 14px; background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 14px 16px; margin-bottom: 12px;
    transition: box-shadow .15s ease, transform .15s ease;
  }}
  .card:hover {{ box-shadow: 0 6px 20px rgba(0,0,0,.10); transform: translateY(-1px); }}
  .rank {{ font: 600 20px/1 "Microsoft YaHei", "Segoe UI", sans-serif; color: var(--muted); opacity: .55;
           min-width: 30px; padding-top: 2px; }}
  .body {{ min-width: 0; }}
  .card h3 {{ margin: 0 0 6px; font-size: 16px; line-height: 1.45; font-weight: 600; }}
  .card h3 a {{ color: inherit; text-decoration: none; }}
  .card h3 a:hover {{ color: var(--accent); text-decoration: underline; }}
  .sum {{ margin: 0 0 8px; color: var(--muted); font-size: 13.5px; line-height: 1.65; }}
  .meta {{ font-size: 12px; color: var(--muted); letter-spacing: .02em; }}
  .src {{ background: var(--chip); border-radius: 4px; padding: 2px 7px; }}
  .dot {{ margin: 0 7px; }}
  footer {{ margin-top: 34px; padding-top: 16px; border-top: 1px solid var(--line);
            color: var(--muted); font-size: 12.5px; display: flex; flex-wrap: wrap; gap: 8px 18px; }}
  footer a {{ color: var(--accent); text-decoration: none; margin-right: 10px; }}
  footer a:hover {{ text-decoration: underline; }}
  #updating {{ display: none; margin: 14px 0 0; padding: 12px 16px; border-radius: 8px;
               background: var(--chip); border: 1px solid var(--line); font-size: 13.5px; }}
  #updating.on {{ display: block; }}
  #updating .spin {{ display: inline-block; width: 12px; height: 12px; margin-right: 8px;
                     border: 2px solid var(--line); border-top-color: var(--accent);
                     border-radius: 50%; animation: sp .8s linear infinite; vertical-align: -2px; }}
  @keyframes sp {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body data-date="{esc(now.strftime('%Y-%m-%d'))}" data-sources="{total_sources}">
<div class="wrap">
  <header class="top">
    <div class="kicker">{esc(S['kicker'])}</div>
    <h1>{esc(date_line)}</h1>
    <div class="sub">{esc(sub_line)}</div>
    {banner}
    <div id="updating"><span class="spin"></span><span id="uptext">{esc(S['updating'])}</span></div>
  </header>

  <div class="cols">
    <section class="ai">
      <h2>{esc(S['section_ai'])}</h2>
{ai_html}
    </section>
    <section class="biz">
      <h2>{esc(S['section_biz'])}</h2>
{biz_html}
    </section>
  </div>

  <footer>
    <div><strong>{esc(S['footer_archive'])}</strong> {arch_html}</div>
    <div>{esc(S['footer_refresh'])}</div>
  </footer>
</div>
<script>
(function () {{
  var pageDate = document.body.getAttribute('data-date');
  var d = new Date();
  var today = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  var KEY = 'dailyBriefReloads';
  if (pageDate === today) {{ try {{ sessionStorage.removeItem(KEY); }} catch (e) {{}} return; }}
  var n = 0;
  try {{ n = parseInt(sessionStorage.getItem(KEY) || '0', 10) || 0; }} catch (e) {{}}
  var box = document.getElementById('updating');
  var txt = document.getElementById('uptext');
  box.className = 'on';
  if (n >= 8) {{
    box.querySelector('.spin').style.display = 'none';
    txt.textContent = {json.dumps(S["gave_up"], ensure_ascii=False)}.replace('{{date}}', pageDate);
    return;
  }}
  try {{ sessionStorage.setItem(KEY, String(n + 1)); }} catch (e) {{}}
  txt.textContent = {json.dumps(S["fetching"], ensure_ascii=False)}
    .replace('{{n}}', String(n + 1)).replace('{{max}}', '8');
  setTimeout(function () {{ location.reload(); }}, 5000);
}})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- main
# Hard wall-clock budget for the whole fetch phase. Stragglers are abandoned
# rather than allowed to stretch the refresh into a minute on a flaky network.
FEED_BUDGET = 45 if TUNNELED else 16

KNOWLEDGE_SOURCES = {"Not Boring", "McKinsey", "Lenny's Newsletter",
                     "MIT Sloan Mgmt Review", "Economist Business"}


def gather(feeds, keywords, now):
    results: list[dict] = []
    failed: list[str] = []
    # every feed starts at once: 23 outlets should be one wave, not two, or the
    # slow ones double the wall-clock cost of the whole refresh
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(fetch, url): (name, weight) for name, url, weight in feeds}
        done, pending = cf.wait(futs, timeout=FEED_BUDGET)
        for fut in done:
            name, weight = futs[fut]
            try:
                raw = fut.result()
                got = parse_feed(name, weight, raw)
                if not got:
                    failed.append(name)
                results.extend(got)
            except Exception as e:                  # noqa: BLE001
                failed.append(name)
                log(f"  ! {name}: {type(e).__name__}: {str(e)[:70]}")
        for fut in pending:
            name, _ = futs[fut]
            failed.append(name)
            fut.cancel()
            log(f"  ! {name}: exceeded {FEED_BUDGET}s budget")
    return results, failed


def build_evergreen(count: int, now: datetime) -> list[dict]:
    """Deterministic rotating pick from the evergreen pool."""
    if count <= 0 or not EVERGREEN:
        return []
    start = (now.timetuple().tm_yday * 3) % len(EVERGREEN)
    out = []
    for i in range(count):
        title, body = EVERGREEN[(start + i) % len(EVERGREEN)]
        # already written in the page's language: skip the translator
        out.append({"title": title, "link": "", "source": "Evergreen", "summary": body,
                    "published": None, "meta": S["evergreen"], "pre_localized": True})
    return out


MIN_HEALTHY_SOURCES = 8


def today_page_is_current() -> bool:
    """True when index.html is today's AND was built from a real network.

    A run that happens while the VPN is still coming up can finish with almost
    every source failing. That page is dated today, so a date-only check would
    treat it as done and never retry -- the stale-looking brief would sit there
    until tomorrow. Requiring a plausible source count makes it self-heal.
    """
    try:
        with open(INDEX, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return False
    m = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', head)
    if not m or m.group(1) != f"{datetime.now():%Y-%m-%d}":
        return False
    s = re.search(r'data-sources="(\d+)"', head)
    if s and int(s.group(1)) < MIN_HEALTHY_SOURCES:
        log(f"today's page only had {s.group(1)} sources -- rebuilding")
        return False
    return True


def main() -> int:
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(ARCHIVE, exist_ok=True)
    if IF_STALE and today_page_is_current():
        log("today's brief already exists -- nothing to do")
        return 0
    if not take_lock():
        log("another run is already in progress -- exiting")
        return 0
    try:
        return _run()
    finally:
        release_lock()


def _run() -> int:
    now = datetime.now().astimezone()
    log(f"[{now:%Y-%m-%d %H:%M}] generating daily brief ...")

    ai_raw, ai_fail = gather(AI_FEEDS, AI_KEYWORDS, now)
    log(f"  AI: {len(ai_raw)} items from {len(AI_FEEDS) - len(ai_fail)}/{len(AI_FEEDS)} feeds")

    try:
        hn = parse_hn(fetch(HN_URL))
        ai_raw.extend(hn)
        log(f"  AI: +{len(hn)} from Hacker News")
    except Exception as e:                          # noqa: BLE001
        ai_fail.append("Hacker News")
        log(f"  ! Hacker News: {type(e).__name__}: {str(e)[:70]}")

    biz_raw, biz_fail = gather(BIZ_FEEDS, BIZ_KEYWORDS, now)
    log(f"  BIZ: {len(biz_raw)} items from {len(BIZ_FEEDS) - len(biz_fail)}/{len(BIZ_FEEDS)} feeds")

    # AI news ages fast; business analysis ages more slowly but still ages.
    ai_window = 96
    biz_window = 24 * 14
    # hours of age that cost one point of score. Business gets a longer leash
    # because analysis stays relevant for days, but it is no longer a free pass:
    # a week-old newsletter now loses to today's story.
    ai_age_scale = 40.0
    biz_age_scale = 90.0

    def fresh(items, hours):
        return [i for i in items
                if not i["published"] or (now - i["published"]).total_seconds() < hours * 3600]

    fresh_ai = fresh(ai_raw, ai_window)
    fresh_biz = fresh(biz_raw, biz_window)
    pool_ai = fresh_ai if len(fresh_ai) >= AI_COUNT * 3 else ai_raw
    pool_biz = fresh_biz if len(fresh_biz) >= BIZ_COUNT * 3 else biz_raw

    ai_items = select(pool_ai, AI_KEYWORDS, AI_COUNT, now, (), ai_window, ai_age_scale)
    # One pass over every business outlet. Analysis still gets a head start from
    # SOURCE_BONUS and KNOWLEDGE_HINTS, but it has to be reasonably fresh to win
    # -- ranking knowledge sources separately is what kept showing week-old posts.
    biz_items = select(pool_biz, BIZ_KEYWORDS, BIZ_COUNT, now,
                       KNOWLEDGE_HINTS, biz_window, biz_age_scale)

    degraded = False
    if len(biz_items) < BIZ_COUNT:
        degraded = True
        biz_items += build_evergreen(BIZ_COUNT - len(biz_items), now)
    if len(ai_items) < AI_COUNT:
        degraded = True
        prev = load_previous()
        for it in prev.get("ai", []):
            if len(ai_items) >= AI_COUNT:
                break
            if all(x["title"] != it["title"] for x in ai_items):
                it = dict(it)
                it["published"] = None
                it["meta"] = "来自上次抓取"
                ai_items.append(it)

    localize(ai_items + biz_items)

    total_sources = (len(AI_FEEDS) + len(BIZ_FEEDS) + 1) - len(set(ai_fail) | set(biz_fail))
    page = render(ai_items, biz_items, now, total_sources, degraded)

    # atomic writes so an open tab never reloads into a half-written page
    for path in (INDEX, os.path.join(ARCHIVE, f"{now:%Y-%m-%d}.html")):
        tmp_page = path + ".tmp"
        with open(tmp_page, "w", encoding="utf-8") as f:
            f.write(page)
        os.replace(tmp_page, path)
    snapshot = {
        "generated": now.isoformat(timespec="seconds"),
        "ai": [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in it.items()}
               for it in ai_items],
        "biz": [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in it.items()}
                for it in biz_items],
    }
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)
    with open(os.path.join(DATA, f"{now:%Y-%m-%d}.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)

    log(f"  wrote {INDEX}")
    note_run(f"ok  ai={len(ai_items)} biz={len(biz_items)} sources={total_sources} "
             f"degraded={degraded} failed={','.join(sorted(set(ai_fail) | set(biz_fail))) or 'none'}")
    for i, it in enumerate(ai_items, 1):
        log(f"    AI{i}: {it['title'][:80]}  [{it['source']}]")
    for i, it in enumerate(biz_items, 1):
        log(f"    BIZ{i}: {it['title'][:80]}  [{it['source']}]")
    return 0


def load_previous() -> dict:
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                        # noqa: BLE001
        log(f"FATAL: {type(exc).__name__}: {exc}")
        note_run(f"FAIL {type(exc).__name__}: {exc}")
        # never leave the user with no page at all
        if not os.path.exists(INDEX):
            os.makedirs(ARCHIVE, exist_ok=True)
            fallback = render([], build_evergreen(BIZ_COUNT, datetime.now()), datetime.now(), 0, True)
            with open(INDEX, "w", encoding="utf-8") as f:
                f.write(fallback)
        sys.exit(1)
