# Daily Brief

A desktop shortcut that opens today's brief: **5 AI frontier items + 5 business knowledge pieces**, pulled from ~26 public RSS feeds, ranked by relevance and freshness, optionally translated, and rendered as one self-contained HTML page.

No account, no subscription, no required API key. Python standard library only — nothing to `pip install`.

![Daily Brief](assets/screenshot.png)

[![CI](https://github.com/zhouz6342-source/daily-brief/actions/workflows/ci.yml/badge.svg)](https://github.com/zhouz6342-source/daily-brief/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#requirements)

```
┌──────────────────────────────────────────────────────────────┐
│  每日简报 / DAILY BRIEF                                        │
│  2026年10月23日 星期五          Friday, October 23, 2026       │
│  AI 前沿 5 条 · 商业知识 5 条 · 已轮询 26 个源                  │
├───────────────────────────┬──────────────────────────────────┤
│  AI 前沿                   │  商业知识                         │
│  01 …                      │  01 …                            │
│  02 …                      │  02 …                            │
│  …                         │  …                               │
└───────────────────────────┴──────────────────────────────────┘
```

## Why it's built the way it is

Most "news digest" scripts break in the same three ways. This one handles them:

- **Feeds rot.** A source that has not published in three years still returns HTTP 200. Age decay in the ranking pushes stale items out instead of trusting the feed to be alive.
- **The network lies.** A blocked host does not refuse the connection, it swallows it, so `urlopen(timeout=…)` can hang forever. Every request runs on a worker thread with a hard wall-clock deadline.
- **A wasted run looks like success.** A run that happens while your VPN is still connecting can finish with every source failing. The page records how many sources it actually reached, and a low count marks it for rebuild rather than letting it serve for a whole day.

## Requirements

- Python **3.9+** (tested on 3.10 and 3.12). No third-party packages.
- Any OS for the generator. The installers cover Windows, macOS and Linux.

## Install

Clone or download, then run the installer for your platform.

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Creates a desktop shortcut, a scheduled task at 08:00, and a logon catch-up entry.
Registering the scheduled task may require an elevated shell; if it is denied the
script says so and still installs the shortcut and catch-up.

### macOS

```sh
./install.sh
```

Creates `~/Desktop/Daily Brief.command` and a launchd agent at 08:00
(`~/Library/LaunchAgents/com.dailybrief.refresh.plist`).

### Linux

```sh
./install.sh
```

Creates a `.desktop` entry and either a systemd user timer (`daily-brief.timer`,
persistent across downtime) or a crontab entry if systemd is unavailable.

### Manual

```sh
python3 digest.py            # build index.html and archive/YYYY-MM-DD.html
python3 digest.py --force    # ignore the single-instance lock
python3 digest.py --if-stale # exit immediately when today's page is already current
```

Open `index.html` in any browser — it is standalone, no server needed.

## Configure

Copy `config.example.json` to `config.json` and edit it. If `config.json` is
absent, the defaults apply and the UI language is auto-detected from your system
locale (English if unrecognised):

```json
{
  "lang": "zh-CN",
  "ai_count": 5,
  "biz_count": 5,
  "translate": {
    "deepl_api_key": "",
    "google_api_key": "",
    "also_try_free": true
  }
}
```

| Key | Meaning |
| --- | --- |
| `lang` | UI language: `en`, `zh-CN` (see `locales/`). Also settable with `--lang`. |
| `ai_count` / `biz_count` | Items per column. |
| `translate.deepl_api_key` | With a key, DeepL is tried first. Free-tier keys end in `:fx`. |
| `translate.google_api_key` | Cloud Translation v2 key; tried first when set. |
| `translate.also_try_free` | Keep the free endpoints as fallback after a keyed provider. |

Environment variables `DEEPL_API_KEY` / `GOOGLE_TRANSLATE_API_KEY` take priority
over `config.json`, so you can keep keys out of the file entirely.

**Never commit `config.json`** — it is already in `.gitignore`.

### Adding a language

Copy `locales/en.json` to `locales/<code>.json`, translate the values, and set
`lang`. `evergreen_concepts` and `source_names` live there too.

## How it works

1. **Fetch** ~26 feeds in parallel (24 workers) with a hard budget. Stragglers are
   abandoned instead of stretching the run.
2. **Parse** RSS 2.0, Atom and one JSON API. Dates are the usual trap: McKinsey
   writes `Thu, 22 Oct 2026` with no time and no zone, which the standard parser
   rejects — such items would otherwise look undated and dodge every freshness rule.
3. **Rank** by `source_weight + keyword hits + knowledge hints − age_cost`.
   Age cost is continuous (`hours / age_scale`), so 20 days old scores far worse
   than 2 days old. One slot per outlet first, relaxed only if a column is short.
4. **Translate** if the page language is not English. Brand and product names are
   swapped for `{{0}}` guards first, so "Claude Duet" does not become 克劳德·杜埃.
5. **Render** a single self-contained HTML file, plus a dated copy in `archive/`.

Freshness detail: AI news uses a 40-hour decay scale and a 4-day candidate window;
business analysis uses 90 hours and 14 days, because analysis stays useful longer
but should still lose to today's story.

## Translation providers

| Provider | Cost | Notes |
| --- | --- | --- |
| `google` (default) | free | Undocumented Chrome-dictionary endpoint. Batches many strings per request. Fast, but Google can change or block it without notice. |
| `mymemory` | free | Fallback. Anonymous tier caps queries at ~500 bytes and roughly 5k characters/day. |
| `deepl` | keyed | Best quality. Used first when a key is present. |
| `google_official` | keyed | Cloud Translation v2. Used first when a key is present. |

The free endpoints are probed for reachability at run time (through your proxy, if
you have one) and the working one is tried first. **The default free endpoint is
not a public API** — if you need long-term reliability, use a key.

Translations are cached in `data/translate_cache.json`, so each string is only
translated once.

## Files

```
digest.py                  the generator
config.example.json        settings template
locales/en.json            UI strings (English)
locales/zh-CN.json         UI strings (Chinese)
install.ps1                Windows installer
install.sh                 macOS / Linux installer
launch.sh                  macOS / Linux launcher (generates then opens)
Launch Daily Brief.vbs     Windows launcher (windowless)
brief.ico                  icon
make_icon.py               regenerates the icon
index.html                 generated — the page you open
archive/                   generated — one snapshot per day
data/                      generated — cache, run log, last good result
```

## Troubleshooting

Check `data/run.log` first — every run appends one line:

```
2026-10-23 09:49:56  ok  ai=5 biz=5 sources=24 degraded=False failed=arXiv cs.AI
```

- `degraded=True` — too few fresh items; built-in evergreen concepts filled the gap.
- `sources=` low — most feeds failed, usually network/proxy. The page self-rebuilds next run.
- `failed=` — per-outlet failures. Harmless while others succeed.
- `translated 0/N` — every translation provider failed; the page falls back to English.

Behind a proxy/VPN the generator detects it automatically and relaxes its timeouts
(tunnelled requests are much slower but reach far more sites).

## Tests

The render pipeline has an offline smoke test: it builds a page from fake items and
checks the structure, so it needs no network and is safe in CI.

```sh
python tests/smoke.py --lang en
python tests/smoke.py --lang zh-CN
```

CI runs those on Ubuntu, macOS and Windows across Python 3.9 and 3.12, plus
`sh -n` on the shell scripts and a PowerShell parse check on `install.ps1`.

## Notes on sources

RSS exists to be read, and this only reads feeds. It does not scrape HTML, bypass
paywalls, or redistribute article text — the page shows a short excerpt and links out.

Some outlets may block your region regardless of the tool; `data/run.log` will show
which. Edit `AI_FEEDS` / `BIZ_FEEDS` in `digest.py` to swap them.

## License

MIT — see [LICENSE](LICENSE).

---

[中文说明](README.zh-CN.md)
