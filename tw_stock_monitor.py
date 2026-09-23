#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股綜合評分監控 (TW Stock Composite Screener)
------------------------------------------------
把「趨勢 / 動能 / 回檔時機 / 量能 / 基本面 / 風險」量化成 0-100 分,
每天排程跑一次,把達標的股票用 Telegram 或 Email 推給你。

這是一個「縮小研究範圍」的篩選工具,不是買賣訊號,不構成投資建議。

用法:
    python tw_stock_monitor.py                # 跑一次
    python tw_stock_monitor.py --demo         # 用假資料試跑(不連網)
    python tw_stock_monitor.py --config my.json
    python tw_stock_monitor.py --serve 14:00  # 常駐,每天 14:00 自動跑
"""

import argparse
import json
import os
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import Header

import numpy as np
import pandas as pd

import netutil as N

# ============================================================
# 預設設定
# ============================================================

DEFAULT_CONFIG = {
    "universe": {
        "auto_scan": True,          # True = 掃全上市;False = 只看 watchlist
        "max_candidates": 150,      # 基本面預篩後最多保留幾檔(控制下載量)
        "min_price": 10.0,
        "max_price": 3000.0,
        "min_turnover_ntd": 30_000_000,   # 當日成交金額下限,濾掉冷門股
    },
    "watchlist": [],                # 例: ["2330.TW", "2454.TW", "6488.TWO"]
    "holdings": {},                 # 例: {"REDACTED.TW": 0.0} → 成本價,用於出場提醒
    "fundamentals": {
        "pe_min": 0.0,
        "pe_max": 30.0,
        "pb_max": 5.0,
        "yield_min": 0.0,
    },
    "score_threshold": 65,          # 幾分以上才通知
    "max_alerts": 15,               # 一次最多推幾檔
    "history_days": 400,
    "notify": {
        "console": True,
        "save_csv": True,
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "email": {
            "enabled": False,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "username": "",
            "password": "",
            "to": "",
        },
    },
}

TWSE_BWIBBU = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TWSE_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
BENCHMARK = "0050.TW"


N.force_utf8_stdio()


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    cfg = DEFAULT_CONFIG
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            cfg = deep_merge(DEFAULT_CONFIG, json.load(f))
        log(f"已載入設定檔 {path}")
    else:
        log("找不到設定檔,使用預設值")

    # config.local.json 不進 git(見 .gitignore),放個人資料:
    # holdings、watchlist、email 收件人等不想公開的內容,會覆蓋 config.json 同名欄位
    local_path = os.path.join(os.path.dirname(os.path.abspath(path)), "config.local.json") \
        if path else "config.local.json"
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8-sig") as f:
            cfg = deep_merge(cfg, json.load(f))
        log(f"已載入個人設定檔 {local_path}")
    return cfg


# ============================================================
# 技術指標
# ============================================================

def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def kd(high, low, close, n=9, m1=3, m2=3):
    """台股慣用的 KD:RSV 用 9 日,K/D 用 1/3 平滑。"""
    low_n = low.rolling(n).min()
    high_n = high.rolling(n).max()
    rng = (high_n - low_n).replace(0, np.nan)
    rsv = ((close - low_n) / rng * 100).fillna(50)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    return k, d


def macd(close, fast=12, slow=26, signal=9):
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea, (dif - dea) * 2


def atr(high, low, close, period=14):
    prev = close.shift()
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# ============================================================
# 資料來源
# ============================================================

def http_json(url, timeout=20):
    # 走 netutil:處理 Python 3.13+ 對證交所憑證的嚴格驗證問題
    # (證交所憑證缺 Subject Key Identifier,3.13 起預設會拒絕)
    return N.get_json(url, timeout=timeout,
                      headers={"User-Agent": "Mozilla/5.0"})


def _num(row, *keys):
    """TWSE 欄位名稱偶有變動,逐一嘗試。"""
    for k in keys:
        v = row.get(k)
        if v in (None, "", "-", "N/A"):
            continue
        try:
            return float(str(v).replace(",", ""))
        except ValueError:
            continue
    return np.nan


def fetch_twse_fundamentals():
    """全上市個股的本益比 / 殖利率 / 股價淨值比。"""
    rows = http_json(TWSE_BWIBBU)
    recs = []
    for r in rows:
        code = str(r.get("Code", "")).strip()
        if not code.isdigit() or len(code) != 4:
            continue
        recs.append({
            "code": code,
            "name": str(r.get("Name", "")).strip(),
            "pe": _num(r, "PEratio", "PERatio"),
            "yield": _num(r, "DividendYield", "YieldRatio"),
            "pb": _num(r, "PBratio", "PBRatio"),
        })
    return pd.DataFrame(recs)


def fetch_twse_daily():
    """全上市個股當日收盤與成交金額。"""
    rows = http_json(TWSE_DAY_ALL)
    recs = []
    for r in rows:
        code = str(r.get("Code", "")).strip()
        if not code.isdigit() or len(code) != 4:
            continue
        recs.append({
            "code": code,
            "close": _num(r, "ClosingPrice", "Close"),
            "turnover": _num(r, "TradeValue", "Value"),
        })
    return pd.DataFrame(recs)


def build_universe(cfg):
    """決定要分析哪些股票,回傳 yfinance 代號清單 + 基本面表。"""
    watch = cfg.get("watchlist") or []
    if watch and not cfg["universe"]["auto_scan"]:
        log(f"使用自訂觀察清單,共 {len(watch)} 檔")
        return watch, pd.DataFrame()

    u, f = cfg["universe"], cfg["fundamentals"]
    log("下載 TWSE 全市場基本面資料…")
    fund = fetch_twse_fundamentals()
    daily = fetch_twse_daily()
    df = fund.merge(daily, on="code", how="inner")
    log(f"全市場 {len(df)} 檔,開始預篩")

    m = (
        df["close"].between(u["min_price"], u["max_price"])
        & (df["turnover"] >= u["min_turnover_ntd"])
        & (df["pe"].fillna(999) <= f["pe_max"])
        & (df["pe"].fillna(-1) > f["pe_min"])
        & (df["pb"].fillna(999) <= f["pb_max"])
        & (df["yield"].fillna(0) >= f["yield_min"])
    )
    df = df[m].copy()
    # 成交金額由大到小,優先分析流動性好的
    df = df.sort_values("turnover", ascending=False).head(u["max_candidates"])
    log(f"基本面預篩後剩 {len(df)} 檔")

    df["ticker"] = df["code"] + ".TW"
    tickers = df["ticker"].tolist() + [t for t in watch if t not in set(df["ticker"])]
    return tickers, df.set_index("ticker")


def fetch_history(tickers, days, demo=False):
    """回傳 {ticker: DataFrame(Open/High/Low/Close/Volume)}"""
    if demo:
        return _demo_history(tickers, days)

    try:
        import yfinance as yf
    except ImportError:
        log("錯誤:未安裝 yfinance,請執行 pip install yfinance")
        sys.exit(1)

    end = datetime.now()
    start = end - timedelta(days=days)
    out = {}
    chunk = 40
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        log(f"下載歷史股價 {i + 1}-{i + len(batch)} / {len(tickers)}")
        data = yf.download(batch, start=start, end=end, group_by="ticker",
                           auto_adjust=True, progress=False, threads=True)
        for t in batch:
            try:
                d = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                d = d.dropna()
                if len(d) >= 210:
                    out[t] = d
            except (KeyError, TypeError):
                continue
        time.sleep(1)   # 對資料源客氣一點
    log(f"取得 {len(out)} 檔有效歷史資料")
    return out


def _demo_history(tickers, days):
    """離線測試用的合成資料。"""
    rng = np.random.default_rng(42)
    idx = pd.bdate_range(end=datetime.now(), periods=days)
    out = {}
    for t in tickers:
        drift = rng.normal(0.0004, 0.0004)
        ret = rng.normal(drift, 0.018, len(idx))
        close = 100 * np.exp(np.cumsum(ret))
        high = close * (1 + np.abs(rng.normal(0, 0.008, len(idx))))
        low = close * (1 - np.abs(rng.normal(0, 0.008, len(idx))))
        out[t] = pd.DataFrame({
            "Open": close * (1 + rng.normal(0, 0.004, len(idx))),
            "High": high, "Low": low, "Close": close,
            "Volume": rng.integers(2_000_000, 30_000_000, len(idx)),
        }, index=idx)
    return out


# ============================================================
# 評分核心
# ============================================================

SCORE_RULES = [
    # (代號, 說明, 滿分)
    ("trend_long",  "股價站上年線(200MA),長期趨勢向上", 20),
    ("trend_mid",   "月線 > 季線,中期多頭排列", 12),
    ("momentum",    "半年報酬贏過大盤(0050)", 15),
    ("pullback",    "RSI 落在 35-58,是回檔而非追高", 15),
    ("kd_cross",    "KD 低檔黃金交叉", 10),
    ("macd_turn",   "MACD 柱狀體由負轉正", 10),
    ("volume",      "近 5 日均量放大", 8),
    ("value",       "本益比 / 殖利率 落在合理區間", 10),
]
PENALTY_RULES = [
    ("chase_high",  "距離一年高點不到 3%,追高風險", -10),
    ("high_vol",    "波動度過大(ATR > 5%)", -8),
    ("below_ma20",  "跌破月線,短線轉弱", -10),
]


def analyse(ticker, df, bench_ret_6m, fundamentals):
    """對單檔股票計算指標並評分。"""
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    if len(c) < 210:
        return None

    ma5, ma20, ma60, ma200 = (c.rolling(n).mean() for n in (5, 20, 60, 200))
    r = rsi(c)
    k, d = kd(h, l, c)
    _, _, hist = macd(c)
    a = atr(h, l, c)

    px = float(c.iloc[-1])
    hits, misses, score = [], [], 0

    def award(key, cond, pts):
        nonlocal score
        label = next(x[1] for x in SCORE_RULES + PENALTY_RULES if x[0] == key)
        if cond:
            score += pts
            (hits if pts > 0 else misses).append(label)
        elif pts > 0:
            misses.append(label)

    # --- 加分項 ---
    award("trend_long", px > ma200.iloc[-1], 20)
    award("trend_mid", ma20.iloc[-1] > ma60.iloc[-1], 12)

    ret_6m = px / float(c.iloc[-120]) - 1 if len(c) > 120 else 0.0
    award("momentum", ret_6m > bench_ret_6m, 15)

    rsi_now = float(r.iloc[-1])
    award("pullback", 35 <= rsi_now <= 58, 15)

    k_now, k_prev, d_prev = float(k.iloc[-1]), float(k.iloc[-2]), float(d.iloc[-2])
    award("kd_cross", (k_prev <= d_prev) and (k_now > float(d.iloc[-1])) and k_now < 60, 10)

    award("macd_turn", float(hist.iloc[-1]) > 0 >= float(hist.iloc[-2]), 10)

    vol_ratio = float(v.tail(5).mean() / v.tail(20).mean()) if float(v.tail(20).mean()) else 0
    award("volume", vol_ratio > 1.2, 8)

    fund = fundamentals.get(ticker, {})
    pe, dy = fund.get("pe", np.nan), fund.get("yield", np.nan)
    value_ok = (not np.isnan(pe) and 0 < pe <= 20) or (not np.isnan(dy) and dy >= 3)
    award("value", value_ok, 10)

    # --- 扣分項 ---
    year_high = float(c.tail(250).max())
    award("chase_high", px >= year_high * 0.97, -10)
    atr_pct = float(a.iloc[-1]) / px * 100
    award("high_vol", atr_pct > 5, -8)
    award("below_ma20", px < ma20.iloc[-1], -10)

    score = max(0, min(100, score))
    return {
        "ticker": ticker,
        "name": fund.get("name", ""),
        "score": score,
        "price": round(px, 2),
        "ma20": round(float(ma20.iloc[-1]), 2),
        "ma60": round(float(ma60.iloc[-1]), 2),
        "ma200": round(float(ma200.iloc[-1]), 2),
        "rsi": round(rsi_now, 1),
        "k": round(k_now, 1),
        "ret_6m": round(ret_6m * 100, 1),
        "vol_ratio": round(vol_ratio, 2),
        "atr_pct": round(atr_pct, 2),
        "pe": None if np.isnan(pe) else round(pe, 1),
        "yield": None if np.isnan(dy) else round(dy, 2),
        "stop_loss": round(px - 2 * float(a.iloc[-1]), 2),   # 2 倍 ATR 停損參考
        "hits": hits,
        "misses": misses,
    }


def check_exits(holdings, results_all, hist):
    """對持股做出場提醒。"""
    alerts = []
    for ticker, cost in (holdings or {}).items():
        df = hist.get(ticker)
        if df is None or len(df) < 60:
            continue
        c = df["Close"]
        px = float(c.iloc[-1])
        ma20 = float(c.rolling(20).mean().iloc[-1])
        ma60 = float(c.rolling(60).mean().iloc[-1])
        pnl = (px / float(cost) - 1) * 100 if cost else 0
        reasons = []
        if px < ma20:
            reasons.append("跌破月線")
        if px < ma60:
            reasons.append("跌破季線")
        if cost and pnl <= -10:
            reasons.append(f"虧損已達 {pnl:.1f}%")
        if reasons:
            alerts.append({"ticker": ticker, "price": round(px, 2),
                           "pnl": round(pnl, 1), "reasons": reasons})
    return alerts


# ============================================================
# 通知
# ============================================================

def build_report(buys, exits, threshold):
    today = f"{datetime.now():%Y-%m-%d}"
    lines = [f"📊 台股篩選報告 {today}", ""]

    if buys:
        lines.append(f"✅ 通過篩選(≥{threshold} 分)共 {len(buys)} 檔:")
        for i, s in enumerate(buys, 1):
            head = f"{i}. {s['ticker']} {s['name']}  {s['score']} 分"
            lines.append(head)
            lines.append(f"   收盤 {s['price']} | 月線 {s['ma20']} | 年線 {s['ma200']}")
            lines.append(f"   RSI {s['rsi']} | K {s['k']} | 半年報酬 {s['ret_6m']}% | 量比 {s['vol_ratio']}")
            pe = s['pe'] if s['pe'] is not None else "—"
            dy = s['yield'] if s['yield'] is not None else "—"
            lines.append(f"   本益比 {pe} | 殖利率 {dy}% | 波動 {s['atr_pct']}%")
            lines.append(f"   停損參考(2×ATR) {s['stop_loss']}")
            lines.append(f"   ✔ {' / '.join(s['hits'][:4])}")
            if s["misses"]:
                lines.append(f"   ✘ {' / '.join(s['misses'][:3])}")
            lines.append("")
    else:
        lines.append("今天沒有股票達到門檻。空手也是一種部位。")
        lines.append("")

    if exits:
        lines.append("⚠️ 持股警示:")
        for e in exits:
            lines.append(f"• {e['ticker']} 現價 {e['price']}(損益 {e['pnl']}%)"
                         f" → {'、'.join(e['reasons'])}")
        lines.append("")

    lines.append("— 本報告為量化篩選結果,僅供研究參考,不構成投資建議。")
    return "\n".join(lines)


def send_telegram(cfg, text):
    tg = cfg["notify"]["telegram"]
    if not tg.get("enabled"):
        return
    url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
    # Telegram 單則上限 4096 字,超過就分段
    for i in range(0, len(text), 3800):
        payload = urllib.parse.urlencode({
            "chat_id": tg["chat_id"], "text": text[i:i + 3800]
        }).encode()
        try:
            N.urlopen_post(url, payload, timeout=20)
        except Exception as e:
            log(f"Telegram 發送失敗:{e}")
            return
    log("已發送 Telegram 通知")


def send_email(cfg, text):
    em = cfg["notify"]["email"]
    if not em.get("enabled"):
        return
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(f"台股篩選報告 {datetime.now():%Y-%m-%d}", "utf-8")
    msg["From"] = em["username"]
    msg["To"] = em["to"]
    try:
        with smtplib.SMTP(em["smtp_host"], em["smtp_port"], timeout=30) as s:
            s.starttls()
            s.login(em["username"], em["password"])
            s.send_message(msg)
        log("已發送 Email 通知")
    except Exception as e:
        log(f"Email 發送失敗:{e}")


def save_csv(results):
    if not results:
        return None
    os.makedirs("reports", exist_ok=True)
    path = f"reports/scan_{datetime.now():%Y%m%d}.csv"
    df = pd.DataFrame(results).drop(columns=["hits", "misses"], errors="ignore")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    log(f"完整結果已存至 {path}")
    return path


# ============================================================
# 主流程
# ============================================================

def run_once(cfg, demo=False):
    tickers, fund_df = build_universe(cfg) if not demo else (
        ["2330.TW", "2454.TW", "2603.TW", "1101.TW", "2882.TW"], pd.DataFrame())

    fundamentals = {}
    if not fund_df.empty:
        for t, row in fund_df.iterrows():
            fundamentals[t] = {"name": row["name"], "pe": row["pe"],
                               "yield": row["yield"], "pb": row["pb"]}

    holdings = cfg.get("holdings") or {}
    all_tickers = list(dict.fromkeys(tickers + list(holdings) + [BENCHMARK]))
    hist = fetch_history(all_tickers, cfg["history_days"], demo=demo)

    # 大盤半年報酬,作為動能比較基準
    bench_ret = 0.0
    if BENCHMARK in hist and len(hist[BENCHMARK]) > 120:
        bc = hist[BENCHMARK]["Close"]
        bench_ret = float(bc.iloc[-1]) / float(bc.iloc[-120]) - 1
    log(f"大盤(0050)半年報酬:{bench_ret * 100:.1f}%")

    results = []
    for t in tickers:
        if t not in hist:
            continue
        try:
            r = analyse(t, hist[t], bench_ret, fundamentals)
            if r:
                results.append(r)
        except Exception as e:
            log(f"{t} 分析失敗:{e}")

    results.sort(key=lambda x: x["score"], reverse=True)
    buys = [r for r in results if r["score"] >= cfg["score_threshold"]][:cfg["max_alerts"]]
    exits = check_exits(holdings, results, hist)

    report = build_report(buys, exits, cfg["score_threshold"])
    if cfg["notify"]["console"]:
        print("\n" + report + "\n")
    if cfg["notify"]["save_csv"]:
        save_csv(results)
    send_telegram(cfg, report)
    send_email(cfg, report)
    return report


def serve(cfg, hhmm):
    hour, minute = (int(x) for x in hhmm.split(":"))
    log(f"常駐模式啟動,每個交易日 {hour:02d}:{minute:02d} 執行。Ctrl+C 結束。")
    last = None
    while True:
        now = datetime.now()
        if (now.weekday() < 5 and now.hour == hour
                and now.minute == minute and last != now.date()):
            last = now.date()
            try:
                run_once(cfg)
            except Exception as e:
                log(f"執行失敗:{e}")
        time.sleep(30)


def main():
    p = argparse.ArgumentParser(description="台股綜合評分監控")
    p.add_argument("--config", default="config.json")
    p.add_argument("--demo", action="store_true", help="用合成資料離線試跑")
    p.add_argument("--serve", metavar="HH:MM", help="常駐並每日定時執行")
    args = p.parse_args()

    cfg = load_config(args.config)
    if args.serve:
        serve(cfg, args.serve)
    else:
        run_once(cfg, demo=args.demo)


if __name__ == "__main__":
    main()
