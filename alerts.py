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


def _streak(vals):
    """從最新一天往回數同方向的天數;正=連買、負=連賣、0=最新一天沒有買賣。"""
    if not vals or not vals[0]:
        return 0
    sign = 1 if vals[0] > 0 else -1
    n = 0
    for v in vals:
        if v and (v > 0) == (sign > 0):
            n += 1
        else:
            break
    return n * sign


def _chips(con, code, date):
    """回傳 {streak 今天的外資連續天數, prev 前一天為止的連續天數, margin 融資 5 日增減 %, margin_prev 前一天的同一個數字};
    籌碼日期不是當天就是 None。"""
    out = {"streak": None, "prev": None, "margin": None, "margin_prev": None}
    if con is None:
        return out
    date = date.replace("-", "")                # chips.db 的日期是 YYYYMMDD
    rows = con.execute("SELECT date, foreign_net FROM inst WHERE code=? ORDER BY date DESC LIMIT 30", (code,)).fetchall()
    if rows and rows[0][0] == date and rows[0][1]:
        vals = [v for _, v in rows]
        out["streak"], out["prev"] = _streak(vals), _streak(vals[1:])
    rows = con.execute("SELECT date, margin_bal FROM margin WHERE code=? ORDER BY date DESC LIMIT 7", (code,)).fetchall()
    if len(rows) >= 6 and rows[0][0] == date and rows[5][1]:
        out["margin"] = (rows[0][1] / rows[5][1] - 1) * 100
        if len(rows) == 7 and rows[6][1]:
            out["margin_prev"] = (rows[1][1] / rows[6][1] - 1) * 100
    return out


# ---------- 大盤與族群:讓提醒分得出「跟著大家跌」還是「自己跌」 ----------
IND_CODE = {"01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維", "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷",
            "09": "造紙工業", "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造", "15": "航運業", "16": "觀光餐旅",
            "17": "金融保險業", "18": "貿易百貨", "20": "其他", "21": "化學工業", "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業",
            "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業",
            "31": "其他電子業", "33": "農業科技", "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活"}
UA = {"User-Agent": "Mozilla/5.0"}


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def market_context(date):
    """date='YYYY-MM-DD' 那天的 {taiex, ind_avg: {產業: 平均漲跌%}, ind_of: {代號: 產業}};抓不到就回 None。"""
    import re
    import netutil as N
    d = date.replace("-", "")
    pct, taiex = {}, None
    try:
        r = N.get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d}&type=ALLBUT0999&response=json",
                       timeout=60, headers=UA)
        sign = lambda cell: -1 if "-" in re.sub(r"<[^>]+>", "", str(cell)).strip()[:2] else 1
        for t in r.get("tables") or []:
            f = t.get("fields") or []
            if "指數" in f and "漲跌百分比(%)" in f:
                for row in t.get("data") or []:
                    if row[0] == "發行量加權股價指數":
                        taiex = abs(_num(row[f.index("漲跌百分比(%)")]) or 0) * sign(row[f.index("漲跌(+/-)")])
            if "證券代號" in f and "收盤價" in f:
                ic, icl, isg, idf = f.index("證券代號"), f.index("收盤價"), f.index("漲跌(+/-)"), f.index("漲跌價差")
                for row in t.get("data") or []:
                    close, diff = _num(row[icl]), _num(row[idf])
                    if close and diff is not None and close - diff * sign(row[isg]) > 0:
                        chg = diff * sign(row[isg])
                        pct[str(row[ic]).strip()] = chg / (close - chg) * 100
        rows = N.get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=60, headers=UA)
        rd = str(rows[0].get("Date", "")) if rows else ""
        if rd and f"{int(rd[:-4]) + 1911}{rd[-4:]}" == d:      # 櫃買中心的資料要是同一天才用
            for x in rows:
                close, chg = _num(x.get("Close")), _num(x.get("Change"))
                if close and chg is not None and close - chg > 0:
                    pct[str(x.get("SecuritiesCompanyCode", "")).strip()] = chg / (close - chg) * 100
        ind_of = {}
        for x in N.get_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", timeout=60, headers=UA):
            ind_of[str(x.get("公司代號", "")).strip()] = IND_CODE.get(str(x.get("產業別", "")).strip().zfill(2), "")
        for x in N.get_json("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", timeout=60, headers=UA):
            ind_of[str(x.get("SecuritiesCompanyCode", "")).strip()] = IND_CODE.get(
                str(x.get("SecuritiesIndustryCode", "")).strip().zfill(2), "")
    except Exception as e:
        print(f"大盤與族群資料抓不到,提醒信不附族群比較:{type(e).__name__}: {e}")
        return None
    groups = {}
    for c, p in pct.items():
        if ind_of.get(c):
            groups.setdefault(ind_of[c], []).append(p)
    ind_avg = {g: sum(v) / len(v) for g, v in groups.items() if len(v) >= 5}
    return {"taiex": taiex, "ind_avg": ind_avg, "ind_of": ind_of}


_CTX = {}


def context_for(date):
    """同一天的大盤與族群資料只抓一次(所有使用者共用)。"""
    if date not in _CTX:
        _CTX[date] = market_context(date)
    return _CTX[date]


def _why(pct, code, ctx):
    """單日大漲大跌時補一句:跟著族群,還是個股自己的原因。"""
    if not ctx:
        return ""
    ind = ctx["ind_of"].get(code)
    avg = ctx["ind_avg"].get(ind)
    if avg is None:
        return ""
    diff, verb = pct - avg, "漲" if avg > 0 else "跌"
    if (pct > 0) == (avg > 0) and abs(avg) >= 1:      # 族群大漲大跌的日子,差距的容忍度跟著放大
        if abs(diff) < max(1.5, 0.35 * abs(avg)):
            return f";主要是跟著{ind}一起{verb}(族群平均 {avg:+.1f}%)"
        if abs(pct) > abs(avg) and abs(diff) < max(3.0, 0.8 * abs(avg)):
            return f";跟著{ind}一起{verb},但{verb}得比族群多(族群平均 {avg:+.1f}%)"
    return f";比{ind}平均({avg:+.1f}%){'強' if pct > avg else '弱'} {abs(diff):.1f} 個百分點,是個股自己的原因"


def events_for(code, name, rules, level, holding=None, con=None, df=None, market=None, ctx=None):
    """一檔股票今天的事件清單 [(種類, 白話說明)];另外回傳收盤日期與收盤價。
    持續中的狀況(外資連賣、融資偏高、已經在停損線下)只在剛發生那天提醒,不會每天重複寄。"""
    pct_t, vol_t, inst_t, margin_t = LEVELS.get(level, LEVELS["標準"])
    df = df if df is not None else _history(code, market)
    if df is None:
        return [], None, None
    c = df["Close"]
    date = df.index[-1].strftime("%Y-%m-%d")
    if ctx == "auto":
        ctx = context_for(date)
    close, prev = float(c.iloc[-1]), float(c.iloc[-2])
    pct = (close / prev - 1) * 100
    out = []
    if rules.get("price", True) and abs(pct) >= pct_t:
        out.append(("漲跌", f"今天{'大漲' if pct > 0 else '大跌'} {pct:+.1f}%(收 {close:g}){_why(pct, code, ctx)}"))
    vol20 = float(df["Volume"].iloc[-21:-1].mean())
    if rules.get("volume", True) and vol20 > 0:
        ratio = float(df["Volume"].iloc[-1]) / vol20
        if ratio >= vol_t:
            out.append(("爆量", f"成交量是前 20 日均量的 {ratio:.1f} 倍,{'價漲量增,買盤積極' if pct > 0 else '價跌量增,賣壓較重' if pct < 0 else '多空交戰'}"))
    ch = _chips(con, code, date)
    s, p = ch["streak"], ch["prev"]
    if rules.get("inst", True) and s is not None:
        side = lambda x: "買超" if x > 0 else "賣超"
        if abs(s) in (inst_t, inst_t * 2):           # 剛達到門檻,或連續天數又翻倍,才提醒
            out.append(("外資", f"外資連續 {abs(s)} 天{side(s)}" + ("(天數還在拉長)" if abs(s) == inst_t * 2 else "")))
        elif s and p and (s > 0) != (p > 0) and abs(p) >= inst_t:
            out.append(("外資", f"外資連續 {abs(p)} 天{side(p)}後,今天轉為{side(s)}"))
    m, mp = ch["margin"], ch["margin_prev"]
    if rules.get("margin", True) and m is not None and m >= margin_t and (mp is None or mp < margin_t):
        out.append(("融資", f"融資 5 天增加 {m:.0f}%,融資多半是散戶,籌碼較不穩"))
    if rules.get("stop", True) and holding is not None and len(df) >= 17:
        atr = _atr(df)
        stop_ref = float(c.iloc[-2] - 2 * atr.iloc[-2])
        was_below = float(c.iloc[-2]) < float(c.iloc[-3] - 2 * atr.iloc[-3])     # 昨天就已經跌破的不再重複提醒
        if close < stop_ref and not was_below:
            msg = f"收盤 {close:g} 跌破停損參考價 {stop_ref:.1f}(前一天收盤 − 2×ATR)"
            cost = holding.get("cost")
            if cost:
                msg += f";相對你的成本 {cost:g} 是 {(close / float(cost) - 1) * 100:+.1f}%"
            out.append(("停損", msg))
    return out, date, close


def build(user, con=None, ctx="auto"):
    """一個使用者的提醒內容:[(代號, 名稱, 是否持股, 事件清單)], 資料日期。"""
    rules, level = user.get("rules") or {}, user.get("sensitivity") or "標準"
    holds = {h["code"]: h for h in user.get("holdings") or []}
    codes = list(holds) + [c for c in user.get("watch") or [] if c not in holds]
    names = user.get("names") or {}
    res, date = [], None
    for code in codes:
        ev, d, _ = events_for(code, names.get(code, ""), rules, level, holds.get(code), con,
                              market=(user.get("markets") or {}).get(code), ctx=ctx)
        date = date or d
        if ev:
            res.append((code, names.get(code, ""), code in holds, ev))
    return res, date


def market_line(res, date):
    """信的第一段:今天大盤怎麼走、名單裡的大漲大跌有幾檔只是跟著族群。"""
    ctx = _CTX.get(date)
    if not ctx or ctx.get("taiex") is None:
        return ""
    moves = [m for _, _, _, ev in res for k, m in ev if k == "漲跌"]
    follow = sum("跟著" in m for m in moves)
    line = f"今天加權指數 {ctx['taiex']:+.2f}%。"
    if moves:
        line += f"名單裡單日大漲大跌的有 {len(moves)} 檔,其中 {follow} 檔主要是跟著族群一起動" + (
            ",比較需要注意的是其他標「個股自己的原因」的。" if follow < len(moves) else ",沒有特別的個股因素。")
    return line


def render(name, res, date):
    mk = market_line(res, date)
    lines = [f"{name} 你好,{date} 收盤後,你的持股與關注股裡有 {len(res)} 檔值得留意:"] + ([mk] if mk else []) + [""]
    html = [f"<div style='font-family:sans-serif;max-width:640px'><h2>⭐ 小星:今天值得留意</h2>"
            f"<p>{name} 你好,{date} 收盤後,你的持股與關注股裡有 <b>{len(res)}</b> 檔值得留意:</p>"
            + (f"<p style='background:#f8fafc;border:1px solid #e3e6ea;border-radius:6px;padding:8px'>{mk}</p>" if mk else "")]
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
