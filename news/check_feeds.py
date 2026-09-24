#!/usr/bin/env python3
"""診斷 RSS 來源：看看有問題的 feed 到底回了什麼。

用法：
    python check_feeds.py              # 檢查所有來源
    python check_feeds.py ASML DIGITIMES   # 只檢查名稱含這些字的來源
"""

import sys
import requests
import feedparser
import yaml

UA = "TechNewsBot/1.0 (personal research)"


def check(name: str, url: str) -> None:
    print(f"\n{'=' * 60}\n{name}\n{url}")
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    except Exception as e:
        print(f"  連線失敗：{type(e).__name__} {e}")
        return

    print(f"  HTTP {r.status_code} | {len(r.content)} bytes"
          f" | Content-Type: {r.headers.get('Content-Type', '(無)')}")

    if r.status_code != 200:
        print(f"  內容開頭：{r.text[:200]}")
        return

    feed = feedparser.parse(r.content)
    print(f"  項目數：{len(feed.entries)}")
    if getattr(feed, "bozo", 0):
        print(f"  解析警告：{getattr(feed, 'bozo_exception', '')}")

    if feed.entries:
        for e in feed.entries[:3]:
            print(f"    · {getattr(e, 'title', '(無標題)')[:70]}")
    else:
        # 零項目時看原始內容，通常能看出是 HTML 錯誤頁還是空的 feed
        head = r.text[:400].replace("\n", " ")
        print(f"  原始內容開頭：{head}")


def main() -> None:
    with open("sources.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    filters = [a.lower() for a in sys.argv[1:]]
    for src in config["sources"]:
        if filters and not any(f in src["name"].lower() for f in filters):
            continue
        check(src["name"], src["url"])


if __name__ == "__main__":
    main()
