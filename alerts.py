#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日「今天值得留意」提醒(GitHub Actions 每個交易日 21:45 跟著籌碼補齊後執行,見 chips-evening.yml)。

名單來自本機小星同步到 Google Drive 的 alert_watch.json:每個使用者的持股、關注股、提醒規則、敏感度、收件信箱。
檢查五種事件:單日大漲大跌、爆量、外資連續買賣超、融資快速增加、持股跌破停損參考價(收盤 − 2×ATR14)。
有事件才寄信;每個事件附一句白話說明。研究用,不是投資建議。

用法:
    python alerts.py              # 讀 alert_watch.json 與 chips.db,有事件就寄信
    python alerts.py --dry-run    # 只印出來,不寄信
"""
import json
import os
import sqlite3
import sys

# 敏感度 → (單日漲跌 %, 量是 20 日均量幾倍, 外資連續幾天, 融資 5 日增加 %)
LEVELS = {"敏感": (2.0, 1.8, 2, 5.0), "標準": (3.0, 2.5, 3, 10.0), "寬鬆": (5.0, 4.0, 5, 20.0)}
WATCH_FILE = "alert_watch.json"


def _history(code, market=None):
    """近 3 個月日線(知道是上櫃就用 .TWO,不知道就先試上市 .TW)。"""
    import yfinance as yf
    for suf in ((".TWO", ".TW") if market == "TWO" else (".TW", ".TWO")):
        try:
            df = yf.Ticker(code + suf).history(period="3mo", auto_adjust=False)
        except Exception:
            df = None
        if df is not None and len(df) >= 21:
            return df
    return None


def _atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr = (h - l).combine((h - prev).abs(), max).combine((l - prev).abs(), max)
    return tr.rolling(n).mean()


def _chips(con, code, date):
    """回傳 (外資連續天數(正=買超、負=賣超), 融資 5 日增減 %);籌碼日期不是當天就回 None。"""
    streak, margin = None, None
    if con is None:
        return streak, margin
    date = date.replace("-", "")                # chips.db 的日期是 YYYYMMDD
    rows = con.execute("SELECT date, foreign_net FROM inst WHERE code=? ORDER BY date DESC LIMIT 15", (code,)).fetchall()
    if rows and rows[0][0] == date and rows[0][1]:
        sign = 1 if rows[0][1] > 0 else -1
        streak = 0
        for _, v in rows:
            if v and (v > 0) == (sign > 0):
                streak += 1
            else:
                break
        streak *= sign
    rows = con.execute("SELECT date, margin_bal FROM margin WHERE code=? ORDER BY date DESC LIMIT 6", (code,)).fetchall()
    if len(rows) == 6 and rows[0][0] == date and rows[5][1]:
        margin = (rows[0][1] / rows[5][1] - 1) * 100
    return streak, margin


def events_for(code, name, rules, level, holding=None, con=None, df=None, market=None):
    """一檔股票今天的事件清單 [(種類, 白話說明)];另外回傳收盤日期與收盤價。"""
    pct_t, vol_t, inst_t, margin_t = LEVELS.get(level, LEVELS["標準"])
    df = df if df is not None else _history(code, market)
    if df is None:
        return [], None, None
    c = df["Close"]
    date = df.index[-1].strftime("%Y-%m-%d")
    close, prev = float(c.iloc[-1]), float(c.iloc[-2])
    pct = (close / prev - 1) * 100
    out = []
    if rules.get("price", True) and abs(pct) >= pct_t:
        out.append(("漲跌", f"今天{'大漲' if pct > 0 else '大跌'} {pct:+.1f}%(收 {close:g})"))
    vol20 = float(df["Volume"].iloc[-21:-1].mean())
    if rules.get("volume", True) and vol20 > 0:
        ratio = float(df["Volume"].iloc[-1]) / vol20
        if ratio >= vol_t:
            out.append(("爆量", f"成交量是前 20 日均量的 {ratio:.1f} 倍,{'價漲量增,買盤積極' if pct > 0 else '價跌量增,賣壓較重' if pct < 0 else '多空交戰'}"))
    streak, margin = _chips(con, code, date)
    if rules.get("inst", True) and streak is not None and abs(streak) >= inst_t:
        out.append(("外資", f"外資連續 {abs(streak)} 天{'買超' if streak > 0 else '賣超'}"))
    if rules.get("margin", True) and margin is not None and margin >= margin_t:
        out.append(("融資", f"融資 5 天增加 {margin:.0f}%,融資多半是散戶,籌碼較不穩"))
    if rules.get("stop", True) and holding is not None and len(df) >= 16:
        stop_ref = float(c.iloc[-2] - 2 * _atr(df).iloc[-2])
        if close < stop_ref:
            msg = f"收盤 {close:g} 跌破停損參考價 {stop_ref:.1f}(前一天收盤 − 2×ATR)"
            cost = holding.get("cost")
            if cost:
                msg += f";相對你的成本 {cost:g} 是 {(close / float(cost) - 1) * 100:+.1f}%"
            out.append(("停損", msg))
    return out, date, close


def build(user, con=None):
    """一個使用者的提醒內容:[(代號, 名稱, 是否持股, 事件清單)], 資料日期。"""
    rules, level = user.get("rules") or {}, user.get("sensitivity") or "標準"
    holds = {h["code"]: h for h in user.get("holdings") or []}
    codes = list(holds) + [c for c in user.get("watch") or [] if c not in holds]
    names = user.get("names") or {}
    res, date = [], None
    for code in codes:
        ev, d, _ = events_for(code, names.get(code, ""), rules, level, holds.get(code), con,
                              market=(user.get("markets") or {}).get(code))
        date = date or d
        if ev:
            res.append((code, names.get(code, ""), code in holds, ev))
    return res, date


def render(name, res, date):
    lines = [f"{name} 你好,{date} 收盤後,你的持股與關注股裡有 {len(res)} 檔值得留意:", ""]
    html = [f"<div style='font-family:sans-serif;max-width:640px'><h2>⭐ 小星:今天值得留意</h2>"
            f"<p>{name} 你好,{date} 收盤後,你的持股與關注股裡有 <b>{len(res)}</b> 檔值得留意:</p>"]
    for code, nm, held, ev in res:
        tag = "持股" if held else "關注"
        lines.append(f"【{tag}】{code} {nm}")
        lines += [f"  - {msg}" for _, msg in ev]
        html.append(f"<div style='border:1px solid #e3e6ea;border-radius:8px;padding:10px;margin:8px 0'>"
                    f"<b>{code} {nm}</b> <span style='color:#6b7280'>({tag})</span><ul>"
                    + "".join(f"<li>{msg}</li>" for _, msg in ev) + "</ul></div>")
    foot = "想知道原因可以問小星「○○為什麼跌」。這是統計提醒,不是買賣建議;提醒規則與敏感度在小星側欄調整。"
    lines += ["", foot]
    html.append(f"<p style='color:#6b7280;font-size:12px'>{foot}</p></div>")
    return "\n".join(lines), "".join(html)


def main(dry=False):
    if not os.path.exists(WATCH_FILE):
        print(f"沒有 {WATCH_FILE}(小星還沒同步提醒名單),略過")
        return
    data = json.load(open(WATCH_FILE, encoding="utf-8"))
    con = sqlite3.connect("chips.db") if os.path.exists("chips.db") else None
    cfg = None
    if not dry:
        import notify
        import tw_stock_monitor as tw
        cfg = tw.load_config("config.json")
    for u in data.get("users") or []:
        if not u.get("email"):
            continue
        res, date = build(u, con)
        if not res:
            print(f"{u['name']}:今天沒有需要提醒的事件")
            continue
        text, html = render(u["name"], res, date)
        print(text)
        if not dry:
            c = json.loads(json.dumps(cfg))
            em = c.setdefault("notify", {}).setdefault("email", {})
            em["to"], em["enabled"] = u["email"], True
            notify.send_gmail(c, f"⭐ 小星:今天值得留意 {len(res)} 檔({date})", html, text)
    if con is not None:
        con.close()


if __name__ == "__main__":
    main(dry="--dry-run" in sys.argv)
