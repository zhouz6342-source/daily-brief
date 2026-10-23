#!/usr/bin/env python3
"""Offline smoke test: render a page without touching the network.

Used by CI, and useful locally before a commit.

    python tests/smoke.py --lang en
    python tests/smoke.py --lang zh-CN
"""
import argparse
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="en", help="locale to render in")
    args = ap.parse_args()

    # BRIEF_LANG must be set before importing, because the module resolves its
    # language at import time.
    os.environ["BRIEF_LANG"] = args.lang
    sys.path.insert(0, ROOT)

    import digest  # noqa: E402

    now = datetime.datetime.now().astimezone()
    item = {
        "title": "Example headline about a model release",
        "link": "https://example.com/story",
        "source": "OpenAI",
        "summary": "A short standfirst describing the story in one sentence.",
        "published": now,
    }
    page = digest.render([dict(item)] * 5, [dict(item)] * 5, now, 20, False)

    checks = {
        "doctype present": page.lstrip().lower().startswith("<!doctype html"),
        "html closed": page.rstrip().endswith("</html>"),
        "source count embedded": 'data-sources="20"' in page,
        "date embedded": 'data-date="' + now.strftime("%Y-%m-%d") + '"' in page,
        "five cards per column": page.count('class="card"') == 10,
        "links rendered": page.count('href="https://example.com/story"') >= 5,
        "self-contained (no external css/js)": "<link" not in page
                                              and "cdn" not in page.lower(),
    }
    # locale-specific expectations
    if args.lang.startswith("zh"):
        checks["chinese html lang"] = 'lang="zh-CN"' in page
        checks["chinese section title"] = "AI 前沿" in page
    else:
        checks["english html lang"] = 'lang="en"' in page
        checks["english section title"] = "AI Frontier" in page

    failed = [k for k, ok in checks.items() if not ok]
    for name, ok in checks.items():
        print(("PASS  " if ok else "FAIL  ") + name)
    if failed:
        print("\n%d check(s) failed" % len(failed), file=sys.stderr)
        return 1
    print("\nall checks passed (%s)" % args.lang)
    return 0


if __name__ == "__main__":
    sys.exit(main())
