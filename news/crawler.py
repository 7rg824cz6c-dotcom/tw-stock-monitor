#!/usr/bin/env python3
"""
科技新聞 / 訂單動能 / 技術創新 情報蒐集器

流程： 來源(RSS/API) -> 抓取 -> 正文擷取 -> 去重 -> LLM 批次擷取分類 -> SQLite -> Gmail 摘要

用法：
    python crawler.py                  # 抓取 + 分析 + 寄信
    python crawler.py --no-mail        # 抓取 + 分析，結果印在畫面
    python crawler.py --analyze-only   # 不抓新資料，只分析資料庫裡待處理的
    python crawler.py --report         # 只用既有分析結果重寄摘要
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import smtplib
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import requests
import yaml

try:                                    # 有 .env 就自動載入
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_PATH = os.getenv("NEWS_DB", "news.db")
UA = os.getenv("CRAWLER_UA", "TechNewsBot/1.0 (personal research; contact: your@email.com)")
REQUEST_GAP = 3.0                       # 對新聞站的請求間隔（秒）
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "6"))   # 每次 LLM 呼叫分析幾則
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                   "utm_content", "fbclid", "gclid", "ch", "from"}
TZ = timezone(timedelta(hours=8))

CATEGORIES = ["訂單動能", "產能擴廠", "技術創新", "政策法規", "供應鏈風險",
              "地緣總經", "法人動向", "其他"]
REGION_LABEL = {"tw": "台灣", "us": "美國", "eu": "歐洲", "asia": "亞洲其他地區"}

# 供應商：gemini / groq（皆免費免信用卡）、anthropic（付費）、ollama（本機）、none（純規則）
PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()


# ================================================================ 資料庫

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY,
    url_hash      TEXT UNIQUE,
    url           TEXT,
    source        TEXT,
    region        TEXT,
    tier          TEXT,
    title         TEXT,
    published_at  TEXT,
    fetched_at    TEXT,
    raw_text      TEXT,
    category      TEXT,
    summary       TEXT,
    companies     TEXT,
    tw_tickers    TEXT,
    supply_chain  TEXT,
    numbers       TEXT,
    impact        TEXT,
    importance    INTEGER,
    beneficiaries TEXT,
    analyzed      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fetched  ON articles(fetched_at);
CREATE INDEX IF NOT EXISTS idx_category ON articles(category);
"""

MIGRATIONS = [("region", "TEXT"), ("beneficiaries", "TEXT"), ("tier", "TEXT")]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for col, typ in MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


# ================================================================ 工具

def canonical_url(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k not in TRACKING_PARAMS]
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", urlencode(q), ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:32]


def _tokens(text: str) -> set[str]:
    """英文取單字、中文取雙字組，用來比對兩則標題講的是不是同一件事。"""
    low = re.sub(r"[^\w\u4e00-\u9fff ]", " ", text.lower())
    words = {w for w in low.split() if len(w) > 2 and not w.isdigit()}
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", low)
    for seg in cjk:
        words |= {seg[i:i + 2] for i in range(len(seg) - 1)}
    return words


def similar(a: str, b: str) -> float:
    """字面相似度與詞彙交集取大者。同一則新聞被不同媒體改寫時，
    字面差很多但詞彙高度重疊，光看字面會漏掉。"""
    lexical = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = _tokens(a), _tokens(b)
    jaccard = len(ta & tb) / len(ta | tb) if (ta and tb) else 0.0
    return max(lexical, jaccard)


def is_duplicate(conn, title: str, days: int = 3, threshold: float = 0.62) -> bool:
    since = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT title FROM articles WHERE fetched_at > ?", (since,)).fetchall()
    return any(similar(title, r["title"]) >= threshold for r in rows)


def matches_keywords(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k.lower() in low for k in keywords)


def is_spam(title: str, patterns: list[str]) -> bool:
    """市調公司發的 SEO 新聞稿標題高度樣板化，用樣式擋掉最省事。"""
    low = title.lower()
    return any(p.lower() in low for p in patterns)


_last_request = 0.0


def polite_get(url: str, timeout: int = 20) -> requests.Response | None:
    """節流，避免對來源站造成負擔。"""
    global _last_request
    wait = REQUEST_GAP - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()
    try:
        return requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    except requests.RequestException as e:
        print(f"  ! 抓取失敗 {url}: {type(e).__name__}")
        return None


def extract_fulltext(url: str) -> str:
    try:
        import trafilatura
    except ImportError:
        return ""
    resp = polite_get(url)
    if not resp or resp.status_code != 200:
        return ""
    return (trafilatura.extract(resp.text, include_comments=False,
                                include_tables=False) or "")[:4000]


# ================================================================ 抓取

def fetch_rss(src: dict, keywords: list[str]) -> list[dict]:
    print(f"[RSS] {src['name']}")
    resp = polite_get(src["url"])
    if not resp:
        return []
    if resp.status_code != 200:
        print(f"  ! HTTP {resp.status_code} — 網址可能已失效，請用 check_feeds.py 確認")
        return []
    ctype = resp.headers.get("Content-Type", "")
    if "html" in ctype.lower():
        print(f"  ! 回傳的是網頁而非 feed（{ctype}），網址可能已失效")
        return []
    feed = feedparser.parse(resp.content)
    if not feed.entries:
        print("  ! feed 解析不出任何項目")
        return []
    items = []
    for e in feed.entries[:int(src.get("limit", 40))]:
        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "")
        summary = re.sub(r"<[^>]+>", "", getattr(e, "summary", ""))[:800]
        if not title or not link:
            continue
        if not src.get("always") and not matches_keywords(title + summary, keywords):
            continue
        published = ""
        if getattr(e, "published_parsed", None):
            published = datetime(*e.published_parsed[:6],
                                 tzinfo=timezone.utc).astimezone(TZ).isoformat()
        items.append({"title": title, "url": link, "source": src["name"],
                      "region": src.get("region", "tw"),
                      "tier": src.get("tier", "media"), "published_at": published,
                      "summary": summary, "fulltext": src.get("fulltext", False)})
    print(f"  → 命中 {len(items)} 則")
    return items


def fetch_hn(src: dict, keywords: list[str]) -> list[dict]:
    """Hacker News 官方 Algolia 搜尋 API，免金鑰。工程師的技術討論常早於媒體。"""
    print(f"[HN ] {src['name']}")
    min_points = src.get("min_points", 30)      # 門檻過濾掉沒人理的貼文
    items = []
    for term in src.get("terms", []):
        resp = polite_get("https://hn.algolia.com/api/v1/search_by_date"
                          f"?query={term}&tags=story&numericFilters=points>{min_points}")
        if not resp or resp.status_code != 200:
            continue
        try:
            hits = resp.json().get("hits", [])
        except ValueError:
            continue
        for h in hits[:15]:
            title = (h.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "title": title,
                "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                "source": f"Hacker News（{term}）", "region": "us", "tier": "social",
                "published_at": h.get("created_at", ""),
                "summary": f"HN 討論，{h.get('points', 0)} 分 / {h.get('num_comments', 0)} 則留言",
                "fulltext": False})
    print(f"  → 命中 {len(items)} 則")
    return items


def fetch_people(src: dict, keywords: list[str]) -> list[dict]:
    """追蹤特定人物的公開發言。政策表態與高層談話常早於正式公告，
    但屬於意向而非既成事實，另外分級處理。"""
    from urllib.parse import quote
    print(f"[人物] {src['name']}")
    topics = " OR ".join(src.get("topics", []))
    window = src.get("window", "2d")
    items = []
    for person in src.get("people", []):
        q = quote(f'"{person}" ({topics}) when:{window}')
        lang = src.get("lang", "en")
        locale = ("hl=zh-TW&gl=TW&ceid=TW:zh-Hant" if lang == "zh"
                  else "hl=en-US&gl=US&ceid=US:en")
        resp = polite_get(f"https://news.google.com/rss/search?q={q}&{locale}")
        if not resp or resp.status_code != 200:
            continue
        feed = feedparser.parse(resp.content)
        for e in feed.entries[:int(src.get("per_person", 5))]:
            title = getattr(e, "title", "").strip()
            link = getattr(e, "link", "")
            if not title or not link:
                continue
            published = ""
            if getattr(e, "published_parsed", None):
                published = datetime(*e.published_parsed[:6],
                                     tzinfo=timezone.utc).astimezone(TZ).isoformat()
            items.append({
                "title": title, "url": link,
                "source": f"人物相關：{person}",
                "region": src.get("region", "us"), "tier": "statement",
                "published_at": published,
                "summary": re.sub(r"<[^>]+>", "", getattr(e, "summary", ""))[:600],
                "fulltext": False})
    print(f"  → 命中 {len(items)} 則")
    return items


def fetch_mops(src: dict, keywords: list[str]) -> list[dict]:
    """公開資訊觀測站重大訊息：官方開放資料，訂單與擴廠的一手來源。"""
    print(f"[API] {src['name']}")
    resp = polite_get(src["url"])
    if not resp:
        return []
    try:
        data = resp.json()
    except ValueError:
        print("  ! 回應非 JSON")
        return []
    items = []
    for row in data[:200]:
        subject = row.get("主旨", "") or row.get("subject", "")
        company = row.get("公司名稱", "") or row.get("Company", "")
        code = row.get("公司代號", "") or row.get("Code", "")
        if not subject or not matches_keywords(subject, keywords):
            continue
        items.append({
            "title": f"[{code} {company}] {subject}",
            "url": f"https://mops.twse.com.tw/mops/web/t05st01?co_id={code}",
            "source": "MOPS 重大訊息", "region": "tw", "tier": "official",
            "published_at": datetime.now(TZ).isoformat(),
            "summary": (row.get("說明", "") or "")[:800], "fulltext": False})
    print(f"  → 命中 {len(items)} 則")
    return items


def collect(config: dict, conn: sqlite3.Connection) -> int:
    keywords = config["keywords"]
    spam = config.get("spam_patterns", [])
    new_count = spam_count = 0
    for src in config["sources"]:
        fetcher = {"mops_api": fetch_mops, "hn": fetch_hn,
                   "people": fetch_people}.get(src["type"], fetch_rss)
        items = fetcher(src, keywords)
        for it in items:
            if is_spam(it["title"], spam):
                spam_count += 1
                continue
            h = url_hash(it["url"])
            if conn.execute("SELECT 1 FROM articles WHERE url_hash=?", (h,)).fetchone():
                continue
            if is_duplicate(conn, it["title"]):
                continue
            raw = extract_fulltext(it["url"]) if it["fulltext"] else ""
            conn.execute(
                """INSERT OR IGNORE INTO articles
                   (url_hash, url, source, region, tier, title, published_at,
                    fetched_at, raw_text)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (h, it["url"], it["source"], it["region"], it.get("tier", "media"),
                 it["title"],
                 it["published_at"], datetime.now(TZ).isoformat(), raw or it["summary"]))
            conn.commit()
            new_count += 1
    print(f"\n收錄新文章 {new_count} 則（過濾市調垃圾稿 {spam_count} 則）")
    return new_count


# ================================================================ LLM 供應商

_llm_last_call = 0.0


def _throttle(min_gap: float) -> None:
    global _llm_last_call
    wait = min_gap - (time.time() - _llm_last_call)
    if wait > 0:
        time.sleep(wait)
    _llm_last_call = time.time()


_gemini_models: list[str] = []
_gemini_idx = 0


def _model_list() -> list[str]:
    global _gemini_models
    if not _gemini_models:
        raw = os.getenv("GEMINI_MODEL", "gemini-3.5-flash,gemini-3.1-flash-lite,gemini-flash-latest")
        _gemini_models = [m.strip() for m in raw.split(",") if m.strip()]
    return _gemini_models


def call_gemini(prompt: str, max_tokens: int, attempt: int = 1) -> str:
    """Google AI Studio 免費額度。可在 GEMINI_MODEL 用逗號列多個型號，塞車時自動輪替。"""
    global _gemini_idx
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("未設定 GEMINI_API_KEY")
    models = _model_list()
    model = models[_gemini_idx % len(models)]
    _throttle(float(os.getenv("GEMINI_GAP", "6.5")))

    gen_cfg = {"temperature": 0.2, "maxOutputTokens": max_tokens,
               "responseMimeType": "application/json"}   # 直接要求 JSON，解析穩定得多
    budget = os.getenv("GEMINI_THINKING_BUDGET", "0")
    if budget != "off":
        # 新世代模型預設會內部推理，會吃掉輸出額度導致 JSON 被截斷
        gen_cfg["thinkingConfig"] = {"thinkingBudget": int(budget)}

    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg},
        timeout=180)

    # 舊型號不認得 thinkingConfig，拿掉重試一次
    if r.status_code == 400 and "thinkingConfig" in gen_cfg:
        gen_cfg.pop("thinkingConfig")
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg},
            timeout=180)

    if r.status_code == 429:
        # 額度是按型號分開算的，換型號比等待有效
        if len(models) > 1 and attempt <= len(models):
            _gemini_idx += 1
            nxt = models[_gemini_idx % len(models)]
            print(f"  ! {model} 額度已滿，改用 {nxt}")
            time.sleep(3)
            return call_gemini(prompt, max_tokens, attempt + 1)
        print("  ! 所有型號額度皆滿，等 60 秒後重試")
        time.sleep(60)
        return call_gemini(prompt, max_tokens, 1)

    if r.status_code in (500, 502, 503, 504):
        if attempt <= 2:                     # 同型號先退避重試
            wait = 10 * attempt
            print(f"  ! {model} 忙碌（{r.status_code}），等 {wait} 秒重試")
            time.sleep(wait)
            return call_gemini(prompt, max_tokens, attempt + 1)
        if len(models) > 1 and attempt <= 2 + len(models):   # 再不行就換型號
            _gemini_idx += 1
            nxt = models[_gemini_idx % len(models)]
            print(f"  ! {model} 持續忙碌，改用 {nxt}")
            time.sleep(5)
            return call_gemini(prompt, max_tokens, attempt + 1)

    if r.status_code == 404:
        print(f"  ! 型號 {model} 不存在或不開放，請確認 GEMINI_MODEL")
    r.raise_for_status()

    data = r.json()
    cands = data.get("candidates", [])
    if not cands:
        raise ValueError(f"無回應內容（{data.get('promptFeedback', '')}）")
    reason = cands[0].get("finishReason", "")
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        raise ValueError(f"回應為空，finishReason={reason}"
                         + "（推理吃掉額度，請調高 TOKENS_PER_ITEM）"
                         * (reason == "MAX_TOKENS"))
    if reason == "MAX_TOKENS":
        print(f"  ! 回應被截斷（{len(text)} 字元），將嘗試逐筆救援")
    return text


def call_groq(prompt: str, max_tokens: int) -> str:
    """Groq 免費額度：速度快，免信用卡。"""
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("未設定 GROQ_API_KEY")
    _throttle(2.5)
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.2, "max_tokens": max_tokens},
        timeout=120)
    if r.status_code == 429:
        print("  ! 觸及頻率上限，等 60 秒後重試")
        time.sleep(60)
        return call_groq(prompt, max_tokens)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def call_anthropic(prompt: str, max_tokens: int) -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("未設定 ANTHROPIC_API_KEY")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
              "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
        timeout=120)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []))


def call_ollama(prompt: str, max_tokens: int) -> str:
    """本機模型：免費無額度限制，但吃顯卡且較慢。"""
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    r = requests.post(f"{host}/api/generate",
                      json={"model": os.getenv("OLLAMA_MODEL", "qwen3:8b"),
                            "prompt": prompt, "stream": False,
                            "options": {"temperature": 0.2, "num_predict": max_tokens}},
                      timeout=600)
    r.raise_for_status()
    return r.json()["response"]


CALLERS = {"gemini": call_gemini, "groq": call_groq,
           "anthropic": call_anthropic, "ollama": call_ollama}


# ================================================================ 批次分析

BATCH_PROMPT = """你是專門追蹤全球科技供應鏈的台股情報分析師。
以下有 {n} 則新聞（來自{region_label}），請逐一分析。

{chain_section}

只輸出一個 JSON 陣列，不要有任何說明文字或 markdown 標記。
陣列長度必須正好是 {n}，順序對應輸入編號，每個元素格式如下：
{{
  "id": 對應的新聞編號,
  "category": "從 {cats} 擇一",
  "summary": "三句以內的繁體中文重點：誰、做了什麼、規模多大、影響誰（英文新聞也用中文寫）",
  "companies": ["新聞提到的公司"],
  "tw_tickers": ["新聞直接提到的台股代號，沒有就空陣列"],
  "beneficiaries": [{{"ticker": "2330", "name": "台積電", "reason": "一句話依據", "strength": "高/中/低"}}],
  "supply_chain": "供應鏈環節",
  "numbers": {{"金額": "", "數量": "", "時程": ""}},
  "impact": "正面/負面/中性",
  "importance": 1到5的整數
}}

beneficiaries 判斷原則：strength 高 = 明確涉及該台廠產品線或客戶關係且規模具體；
低 = 只是同題材沾邊。關聯不明確時回空陣列，錯誤的連結比沒有連結更糟。

以下情況一律 category 填「其他」、importance 填 1、beneficiaries 回空陣列：
市調公司的市場規模報告、券商比較性文章、消費性產品開箱、遊戲或顯卡零售消息、
單純股價漲跌評論。這些沒有訂單或產能的實質資訊，不要為了填滿欄位而硬做供應鏈推論。

importance 5 只給：具體金額或數量的重大訂單、確定的擴廠投資、量產時程確認、
重大製程突破。市場預測與分析師觀點最多給 3。

特別注意台積電（2330）：不要因為新聞提到 AI 晶片就把台積電列進去。
只有在新聞直接涉及其製程、產能、客戶訂單、資本支出或競爭對手搶單時才列，
且要在 reason 說明是哪一項。同理，伺服器 ODM（廣達、緯創、緯穎）只在新聞
涉及資料中心硬體建置或伺服器出貨時才列，不要見到 AI 就全部掛上。
消費性產品、遊戲主機、顯示卡零售、企業併購財務新聞，一律不做供應鏈推論。

若新聞是地緣政治、天災、戰爭、航運或原物料事件（歸類為「地緣總經」），
不要因為它跟半導體無關就略過。請判斷傳導路徑：是影響原料供應、能源成本、
運輸、終端需求，還是政策風險？在 summary 說明傳導鏈，並只在傳導關係明確時
才列 beneficiaries，同時在 reason 寫出中間環節。
傳導路徑說不清楚就回空陣列——猜測的關聯比沒有關聯更糟。

若新聞內容是人物的公開發言（政治人物的政策表態、企業高層的展望談話），
summary 必須寫明「誰說的」以及這是表態或意向，不要寫成已發生的事實。
例如寫「某官員表示考慮對某類產品課徵關稅」，不要寫成「將課徵關稅」。
政治性議題只做事實陳述，不要加入立場評價。

新聞：
{articles}
"""

CHAIN_SECTION = """這批是國外新聞，你最重要的工作是往上下游推論到台灣廠商——
國外客戶的訂單、財測、資本支出、新產品，往往先於台灣媒體反映在台廠營收上。
以下對照表僅供參考，仍須依新聞實際內容判斷關聯強弱，內容不符就不要硬套：

{chain_map}"""


def build_batch_prompt(rows: list[sqlite3.Row], region: str, chain_map: str) -> str:
    blocks = []
    body_len = int(os.getenv("BODY_CHARS", "1200"))   # 前段通常就含足夠資訊
    for i, r in enumerate(rows, 1):
        body = (r["raw_text"] or "")[:body_len]
        blocks.append(f"--- 編號 {i} ---\n來源：{r['source']}\n標題：{r['title']}\n內容：{body}")
    return BATCH_PROMPT.format(
        n=len(rows), cats="/".join(CATEGORIES),
        region_label=REGION_LABEL.get(region, "未知地區"),
        chain_section=CHAIN_SECTION.format(chain_map=chain_map) if region != "tw" else "",
        articles="\n\n".join(blocks))


def parse_json_array(text: str, expected: int) -> list[dict]:
    """先照正常方式解析；失敗就逐個物件救回，避免一顆老鼠屎壞掉整批。"""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\[.*\]", text, re.S)      # 模型偶爾在前後多講話
    candidate = m.group(0) if m else text
    try:
        data = json.loads(candidate)
        if isinstance(data, list):
            if len(data) != expected:
                print(f"  ! 回傳 {len(data)} 筆，預期 {expected} 筆，以 id 對應")
            return data
    except json.JSONDecodeError:
        pass

    # 救援模式：抓出每個最外層 {...} 分別解析，壞掉的那筆跳過就好
    salvaged, depth, start = [], 0, None
    for i, ch in enumerate(candidate):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(candidate[start:i + 1])
                    if isinstance(obj, dict) and "id" in obj:
                        salvaged.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None
    if not salvaged:
        raise ValueError(f"無法解析回應，開頭：{candidate[:150]!r}")
    print(f"  ! JSON 格式有誤，救回 {len(salvaged)}/{expected} 筆")
    return salvaged


def rules_only(row: sqlite3.Row) -> dict:
    """不呼叫模型的保底方案：關鍵字分類，不做摘要與供應鏈推論。"""
    text = (row["title"] + " " + (row["raw_text"] or ""))[:2000].lower()
    rules = [("訂單動能", ["訂單", "接單", "拉貨", "出貨", "order", "backlog", "bookings"]),
             ("產能擴廠", ["擴廠", "產能", "投片", "資本支出", "capex", "fab", "capacity"]),
             ("技術創新", ["專利", "製程", "研發", "新品", "發表", "launch", "unveil"]),
             ("政策法規", ["管制", "關稅", "制裁", "tariff", "export control"]),
             ("供應鏈風險", ["缺料", "停工", "地震", "延遲", "shortage", "disruption"])]
    cat = next((c for c, ks in rules if any(k.lower() in text for k in ks)), "其他")
    return {"category": cat, "summary": row["title"], "companies": [], "tw_tickers": [],
            "beneficiaries": [], "supply_chain": "", "numbers": {}, "impact": "中性",
            "importance": 3 if cat in ("訂單動能", "產能擴廠") else 2}


def save_result(conn, row_id: int, res: dict) -> None:
    conn.execute(
        """UPDATE articles SET category=?, summary=?, companies=?, tw_tickers=?,
           supply_chain=?, numbers=?, impact=?, importance=?, beneficiaries=?,
           analyzed=1 WHERE id=?""",
        (res.get("category"), res.get("summary"),
         json.dumps(res.get("companies", []), ensure_ascii=False),
         json.dumps(res.get("tw_tickers", []), ensure_ascii=False),
         res.get("supply_chain"),
         json.dumps(res.get("numbers", {}), ensure_ascii=False),
         res.get("impact"), int(res.get("importance", 1) or 1),
         json.dumps(res.get("beneficiaries", []), ensure_ascii=False), row_id))
    conn.commit()


def normalize(res: dict) -> dict:
    """模型偶爾回英文分類或超出範圍的分數，統一收斂。"""
    cat = str(res.get("category", "")).strip()
    if cat not in CATEGORIES:
        cat = {"other": "其他", "orders": "訂單動能",
               "technology": "技術創新"}.get(cat.lower(), "其他")
    res["category"] = cat
    try:
        res["importance"] = max(1, min(5, int(res.get("importance", 1))))
    except (TypeError, ValueError):
        res["importance"] = 1
    if res.get("impact") not in ("正面", "負面", "中性"):
        res["impact"] = "中性"
    return res


def analyze_pending(conn: sqlite3.Connection, chain_map: str, limit: int = 300) -> None:
    rows = conn.execute(
        "SELECT * FROM articles WHERE analyzed=0 ORDER BY region, id DESC LIMIT ?",
        (limit,)).fetchall()
    if not rows:
        print("沒有待分析的文章")
        return

    # 先用規則清掉垃圾稿，不浪費 LLM 額度（涵蓋過濾器上線前收進來的舊資料）
    try:
        with open("sources.yaml", encoding="utf-8") as f:
            spam = yaml.safe_load(f).get("spam_patterns", [])
    except FileNotFoundError:
        spam = []
    if spam:
        kept = []
        for r in rows:
            if is_spam(r["title"], spam):
                save_result(conn, r["id"], {**rules_only(r), "category": "其他",
                                            "importance": 1})
            else:
                kept.append(r)
        if len(kept) < len(rows):
            print(f"規則過濾掉 {len(rows) - len(kept)} 則低價值文章")
        rows = kept
        if not rows:
            return

    if PROVIDER == "none":
        for r in rows:
            save_result(conn, r["id"], rules_only(r))
        print(f"以規則模式處理 {len(rows)} 則")
        return

    if PROVIDER not in CALLERS:
        print(f"未知的 LLM_PROVIDER: {PROVIDER}")
        return

    # 依地區分組，台灣新聞不用附供應鏈對照表，可省不少 token
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["region"] or "tw", []).append(r)

    batches = [(region, chunk[i:i + BATCH_SIZE])
               for region, chunk in groups.items()
               for i in range(0, len(chunk), BATCH_SIZE)]
    print(f"待分析 {len(rows)} 則，分成 {len(batches)} 批（供應商：{PROVIDER}）")

    done = fails = 0
    for n, (region, batch) in enumerate(batches, 1):
        prompt = build_batch_prompt(batch, region, chain_map)
        try:
            text = CALLERS[PROVIDER](prompt, max_tokens=int(os.getenv("TOKENS_PER_ITEM", "1100")) * len(batch))
            results = parse_json_array(text, len(batch))
        except Exception as e:
            fails += 1
            print(f"  ! 第 {n} 批失敗：{type(e).__name__} {str(e)[:120]}")
            if fails >= 3:
                print("  ! 連續失敗過多，已中止。請檢查金鑰、額度或模型名稱")
                return
            continue
        fails = 0

        by_id = {int(r.get("id", 0)): r for r in results if isinstance(r, dict)}
        for i, row in enumerate(batch, 1):
            res = by_id.get(i)
            if not res:
                continue
            res = normalize(res)
            tier = row["tier"] or "media"
            if tier == "social":
                res["importance"] = min(res["importance"], 3)   # 傳聞不給高分
            elif tier == "statement":
                res["importance"] = min(res["importance"], 4)   # 表態非既成事實
            save_result(conn, row["id"], res)
            done += 1
            hits = "、".join(b.get("ticker", "") for b in res.get("beneficiaries", [])[:4])
            print(f"  ✓ [{res.get('category')}] {row['title'][:34]}"
                  + (f"  → {hits}" if hits else ""))
        print(f"  ── 第 {n}/{len(batches)} 批完成")
    print(f"\n分析完成 {done} 則")


def load_chain_map(path: str = "supply_chain_map.yaml") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print("找不到 supply_chain_map.yaml，國外新聞將只靠模型自身知識推論")
        return "（無對照表）"


# ================================================================ 摘要與寄信

def build_report(conn: sqlite3.Connection, hours: int = 24, min_importance: int = 3) -> str:
    since = (datetime.now(TZ) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        """SELECT * FROM articles WHERE analyzed=1 AND fetched_at > ? AND importance >= ?
           ORDER BY importance DESC, category""", (since, min_importance)).fetchall()

    # 「其他」多半是併購財務、ETF、消費性產品，除非特別重要否則不進摘要
    rows = [r for r in rows if r["category"] != "其他" or (r["importance"] or 0) >= 5]

    # 摘要階段再去重：同一事件經多家外電改寫，標題差很多但講的是同一批公司同一件事
    def companies_of(row) -> set[str]:
        try:
            return {c.strip().lower() for c in json.loads(row["companies"] or "[]") if c}
        except json.JSONDecodeError:
            return set()

    def same_event(a, b) -> bool:
        if a["category"] != b["category"]:
            return False
        if similar(a["summary"] or "", b["summary"] or "") >= 0.6:
            return True
        ca, cb = companies_of(a), companies_of(b)
        if not ca or not cb:
            return False
        # 用聯集當分母：{NVIDIA} 與 {NVIDIA, 聯發科} 只有 0.5，不會被誤併
        return len(ca & cb) / len(ca | cb) >= 0.6

    deduped = []
    for r in rows:                       # rows 已依 importance 排序，保留最重要的那則
        if not any(same_event(r, d) for d in deduped):
            deduped.append(r)
    if len(deduped) < len(rows):
        print(f"摘要階段合併重複事件 {len(rows) - len(deduped)} 則")
    rows = deduped

    # 同一起事件常被十幾家外電報導，措辭與受惠股各異而躲過去重。
    # 每個分類與每個來源各設上限，只留最重要的幾則。
    max_cat = int(os.getenv("MAX_PER_CATEGORY", "8"))
    max_src = int(os.getenv("MAX_PER_SOURCE", "4"))
    cat_n: dict[str, int] = {}
    src_n: dict[str, int] = {}
    capped = []
    for r in rows:
        c, s = r["category"] or "其他", r["source"] or ""
        if cat_n.get(c, 0) >= max_cat or src_n.get(s, 0) >= max_src:
            continue
        cat_n[c] = cat_n.get(c, 0) + 1
        src_n[s] = src_n.get(s, 0) + 1
        capped.append(r)
    if len(capped) < len(rows):
        print(f"套用分類/來源上限，略過 {len(rows) - len(capped)} 則同質消息")
    rows = capped

    if not rows:
        return ""

    html = ["<h2>科技產業情報摘要</h2>",
            f"<p>{datetime.now(TZ):%Y-%m-%d %H:%M} ／ 共 {len(rows)} 則</p>"]

    # 台廠關聯彙總：被多則消息同時指到的標的最值得看
    tally: dict[str, dict] = {}
    for r in rows:
        if (r["tier"] or "media") in ("social", "statement"):
            continue                      # 彙總只採信官方與媒體，傳聞與口頭表態不計入
        for b in json.loads(r["beneficiaries"] or "[]"):
            key = f"{b.get('ticker', '')} {b.get('name', '')}".strip()
            if not key:
                continue
            t = tally.setdefault(key, {"events": set(), "reasons": [], "strength": []})
            # 用「來源＋分類」當事件識別：同一起航運事件被十家外電報導只算一次
            t["events"].add(f"{r['source']}|{r['category']}")
            t["strength"].append(b.get("strength", "低"))
            if b.get("reason") and len(t["reasons"]) < 3:
                t["reasons"].append(f"{b['reason']}（{r['source']}）")
    ranked = sorted(tally.items(),
                    key=lambda kv: (kv[1]["strength"].count("高"), len(kv[1]["events"])),
                    reverse=True)[:10]
    if ranked:
        html.append("<h3>台廠關聯彙總</h3><ul>")
        for name, t in ranked:
            html.append(f"<li><b>{name}</b>（{len(t['events'])} 類消息，"
                        f"最高關聯度 {'高' if '高' in t['strength'] else '中'}）"
                        f"<br><small>{'；'.join(t['reasons'])}</small></li>")
        html.append("</ul><hr>")

    for cat in CATEGORIES:
        group = [r for r in rows if r["category"] == cat]
        if not group:
            continue
        html.append(f"<h3>{cat}</h3><ul>")
        for r in group:
            tickers = json.loads(r["tw_tickers"] or "[]")
            bene = [b.get("ticker", "") for b in json.loads(r["beneficiaries"] or "[]")
                    if b.get("strength") in ("高", "中")]
            tag = f" <b>[{'、'.join(tickers or bene)}]</b>" if (tickers or bene) else ""
            flag = "🌐 " if (r["region"] or "tw") != "tw" else ""
            tier = r["tier"] or "media"
            if tier == "social":
                flag += "💬 "             # 社群討論，未經查證
            elif tier == "statement":
                flag += "🗣 "             # 公開發言，屬意向非既成事實
            html.append(f"<li>{flag}{'★' * (r['importance'] or 1)}{tag} {r['summary']}<br>"
                        f"<small>{r['source']}｜{r['impact']}｜"
                        f"<a href='{r['url']}'>原文</a></small></li>")
        html.append("</ul>")
    return "\n".join(html)


def send_gmail(html: str) -> None:
    user, app_pw = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    to = os.getenv("MAIL_TO", user)
    if not (user and app_pw):
        print("未設定 GMAIL_USER / GMAIL_APP_PASSWORD，略過寄信")
        return
    msg = EmailMessage()
    msg["Subject"] = f"科技產業情報 {datetime.now(TZ):%m/%d}"
    msg["From"], msg["To"] = user, to
    msg.set_content("請以 HTML 檢視")
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, app_pw.replace(" ", ""))
            s.send_message(msg)
        print("摘要已寄出")
    except Exception as e:
        print(f"寄信失敗：{type(e).__name__} {e}")


# ================================================================ 主流程

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mail", action="store_true", help="不寄信，結果印在畫面")
    ap.add_argument("--analyze-only", action="store_true", help="不抓新資料，只分析待處理的")
    ap.add_argument("--report", action="store_true", help="只用既有分析結果重寄摘要")
    args = ap.parse_args()

    conn = db()
    if not args.report:
        if not args.analyze_only:
            with open("sources.yaml", encoding="utf-8") as f:
                collect(yaml.safe_load(f), conn)
        analyze_pending(conn, load_chain_map())

    html = build_report(conn)
    if not html:
        print("本次無達標情報")
        return
    print(html) if args.no_mail else send_gmail(html)


if __name__ == "__main__":
    main()
