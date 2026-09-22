#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
籌碼面模組 — 三大法人買賣超 + 融資融券餘額

資料來源皆為證交所官方管道:
  1. openapi.twse.com.tw      政府資料開放授權條款第1版(優先)
  2. www.twse.com.tw/rwd/...  證交所網站自身的 JSON 端點(個股層級法人資料
                              僅此處提供;非爬 HTML 頁面)

資料每天收盤後才公布,所以任何法人訊號最快只能在「隔天」執行。
本模組把每天抓到的資料存進本地 SQLite,自行累積歷史,避免重複請求。
"""

import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta

import pandas as pd

import netutil as N

DB = "chips.db"
UA = {"User-Agent": "Mozilla/5.0 (compatible; personal-research-script)"}

OPENAPI_T86 = "https://openapi.twse.com.tw/v1/fund/T86"
OPENAPI_MARGIN = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"
WEB_T86 = ("https://www.twse.com.tw/rwd/zh/fund/T86"
           "?date={d}&selectType=ALL&response=json")
WEB_MARGIN = ("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
              "?date={d}&selectType=ALL&response=json")


def _get(url, timeout=25):
    # 走 netutil:處理 Python 3.13+ 對證交所憑證的嚴格驗證問題
    return N.get_json(url, timeout=timeout, headers=UA)


def _f(x):
    """把 '1,234' / '--' / '' 轉成 float。"""
    try:
        s = str(x).replace(",", "").strip()
        return float(s) if s not in ("", "-", "--", "N/A") else 0.0
    except (ValueError, AttributeError):
        return 0.0


# ============================================================
# 建立本地資料庫
# ============================================================

def init_db(path=DB):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS inst (
        date TEXT, code TEXT,
        foreign_net REAL,   -- 外資買賣超(股)
        trust_net   REAL,   -- 投信買賣超(股)
        dealer_net  REAL,   -- 自營商買賣超(股)
        total_net   REAL,
        PRIMARY KEY (date, code));
    CREATE TABLE IF NOT EXISTS margin (
        date TEXT, code TEXT,
        margin_bal REAL,    -- 融資餘額(張)
        short_bal  REAL,    -- 融券餘額(張)
        PRIMARY KEY (date, code));
    CREATE INDEX IF NOT EXISTS ix_inst_code ON inst(code, date);
    CREATE INDEX IF NOT EXISTS ix_mgn_code  ON margin(code, date);
    """)
    con.commit()
    return con


# ============================================================
# 抓取
# ============================================================

def fetch_institutional(date=None):
    """
    三大法人買賣超(個股)。date=None 抓最新;給 'YYYYMMDD' 抓指定日。
    回傳 DataFrame[code, foreign_net, trust_net, dealer_net, total_net]
    """
    rows, src = None, ""
    if date is None:
        try:
            rows, src = _get(OPENAPI_T86), "openapi"
        except Exception:
            date = datetime.now().strftime("%Y%m%d")
    if rows is None:
        d = _get(WEB_T86.format(d=date))
        if d.get("stat") != "OK":
            return pd.DataFrame()
        fields = d.get("fields", [])
        rows = [dict(zip(fields, r)) for r in d.get("data", [])]
        src = "web"

    def pick(r, *names):
        for n in names:
            for k in r:
                if n in str(k):
                    return _f(r[k])
        return 0.0

    recs = []
    for r in rows:
        code = str(r.get("證券代號") or r.get("Code") or "").strip()
        if not (code.isdigit() and len(code) == 4):
            continue
        fn = pick(r, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數", "ForeignInvestors")
        tn = pick(r, "投信買賣超股數", "SecuritiesInvestmentTrust")
        dn = pick(r, "自營商買賣超股數", "Dealer")
        tot = pick(r, "三大法人買賣超股數", "TotalInstitutional")
        recs.append({"code": code, "foreign_net": fn, "trust_net": tn,
                     "dealer_net": dn, "total_net": tot or (fn + tn + dn)})
    df = pd.DataFrame(recs)
    if not df.empty:
        df.attrs["source"] = src
    return df


def fetch_margin(date=None):
    """
    融資融券餘額(個股),單位:張。

    ⚠️ 證交所這個端點的欄位名稱有重複:
       ['股票代號','股票名稱','融資買進','融資賣出','現金償還','前日餘額','今日餘額','限額',
        '融券買進','融券賣出','現券償還','前日餘額','今日餘額','限額','資券互抵','註記']
       「前日餘額」「今日餘額」「限額」各出現兩次(融資一次、融券一次)。
       用 dict(zip(fields, row)) 轉換會讓後者覆蓋前者,且欄位數從 16 縮成 13,
       位置索引也跟著錯位 —— 這會讓融資餘額變成融券餘額。
       所以這裡一律以「原始 list + 欄位位置」處理,不轉 dict。
    """
    rows = None
    if date is None:
        try:
            rows = _get(OPENAPI_MARGIN)
        except Exception:
            date = datetime.now().strftime("%Y%m%d")

    recs = []
    if rows is not None:
        # OpenAPI 回傳的是 dict 且欄位名不重複
        for r in rows:
            code = str(r.get("Code", "")).strip()
            if code.isdigit() and len(code) == 4:
                recs.append({"code": code,
                             "margin_bal": _f(r.get("MarginBalance")
                                              or r.get("MarginPurchaseTodayBalance")),
                             "short_bal": _f(r.get("ShortBalance")
                                             or r.get("ShortSaleTodayBalance"))})
        return pd.DataFrame(recs)

    d = _get(WEB_MARGIN.format(d=date))
    if d.get("stat") != "OK":
        return pd.DataFrame()

    # 找出含「股票代號」的那張表(該端點會回多張表)
    blk = None
    for t in d.get("tables", []):
        f = t.get("fields", [])
        if f and any("代號" in str(x) for x in f) and len(t.get("data", [])) > 50:
            blk = t
            break
    if blk is None:
        return pd.DataFrame()

    fields = blk.get("fields", [])
    # 依「第 N 次出現」定位,避開重複名稱
    def nth_index(name, n=1):
        cnt = 0
        for i, f in enumerate(fields):
            if str(f).strip() == name:
                cnt += 1
                if cnt == n:
                    return i
        return -1

    i_code = next((i for i, f in enumerate(fields) if "代號" in str(f)), 0)
    i_mgn = nth_index("今日餘額", 1)      # 第一個 = 融資
    i_sht = nth_index("今日餘額", 2)      # 第二個 = 融券
    if i_mgn < 0:
        i_mgn, i_sht = 6, 12              # 退回已知的標準位置

    for row in blk.get("data", []):
        if not isinstance(row, (list, tuple)) or len(row) <= i_mgn:
            continue
        code = str(row[i_code]).strip()
        if not (code.isdigit() and len(code) == 4):
            continue
        recs.append({
            "code": code,
            "margin_bal": _f(row[i_mgn]),
            "short_bal": _f(row[i_sht]) if 0 <= i_sht < len(row) else 0.0,
        })
    return pd.DataFrame(recs)


def update_today(con, date=None, only=None):
    """
    抓當日籌碼並寫進資料庫(重複執行不會重複寫)。
    only="inst" 或 "margin" 可只補其中一張表。
    回傳 (法人筆數, 融資筆數)。
    """
    d = date or datetime.now().strftime("%Y%m%d")
    n = nm = 0
    inst = fetch_institutional(date) if only != "margin" else pd.DataFrame()
    if not inst.empty:
        inst["date"] = d
        inst[["date", "code", "foreign_net", "trust_net", "dealer_net",
              "total_net"]].to_sql("tmp_i", con, if_exists="replace", index=False)
        con.execute("INSERT OR REPLACE INTO inst SELECT * FROM tmp_i")
        con.execute("DROP TABLE tmp_i")
        n += len(inst)
    mgn = fetch_margin(date) if only != "inst" else pd.DataFrame()
    if not mgn.empty:
        mgn["date"] = d
        mgn[["date", "code", "margin_bal", "short_bal"]].to_sql(
            "tmp_m", con, if_exists="replace", index=False)
        con.execute("INSERT OR REPLACE INTO margin SELECT * FROM tmp_m")
        con.execute("DROP TABLE tmp_m")
        nm = len(mgn)
    con.commit()
    return n, nm


def backfill(con, days=90, pause=3.0, start=None):
    """
    回補歷史籌碼資料。每個交易日一次請求,請保持 pause≥3 秒。
    這是給回測用的;日常監控只需要 update_today()。
    已抓過的日期會自動跳過,中斷後重跑可接續。
    T86 資料自民國101年5月2日(2012-05-02)起提供,可回補十三年以上。
    """
    # 兩張表分開檢查 —— 只看 inst 的話,法人補完後融資就永遠補不到了
    have_i = {r[0] for r in con.execute("SELECT DISTINCT date FROM inst")}
    have_m = {r[0] for r in con.execute("SELECT DISTINCT date FROM margin")}
    have = have_i & have_m
    day = datetime.now()
    stop = datetime.strptime(start, "%Y%m%d") if start else None
    if stop and stop < datetime(2012, 5, 2):
        print("注意:T86 資料自民國101年5月2日(2012-05-02)起才提供")
        stop = datetime(2012, 5, 2)
    limit = days * 2 if not stop else (datetime.now() - stop).days + 10
    done = 0
    for _ in range(limit):
        if not stop and done >= days:
            break
        if stop and day <= stop:
            break
        day -= timedelta(days=1)
        if day.weekday() >= 5:
            continue
        d = day.strftime("%Y%m%d")
        if d in have:
            done += 1
            continue
        need = "margin" if (d in have_i and d not in have_m) else None
        try:
            n, nm = update_today(con, d, only=need)
            if n or nm:
                done += 1
                parts = []
                if n:
                    parts.append(f"法人 {n}")
                if nm:
                    parts.append(f"融資 {nm}")
                print(f"  回補 {d}:{' / '.join(parts)} 檔")
        except Exception as e:
            print(f"  {d} 失敗:{e}")
        time.sleep(pause)
    ni = con.execute("SELECT COUNT(DISTINCT date) FROM inst").fetchone()[0]
    nm = con.execute("SELECT COUNT(DISTINCT date) FROM margin").fetchone()[0]
    print(f"完成:法人 {ni} 個交易日 / 融資 {nm} 個交易日")
    if nm < ni * 0.8:
        print(f"  ⚠ 融資涵蓋僅 {nm/max(ni,1)*100:.0f}%。診斷:python chips.py --check")


# ============================================================
# 特徵計算
# ============================================================

def chip_features(con, codes, lookback=20):
    """
    回傳 {code: {...籌碼特徵...}}

    foreign_net_5    外資近5日累計買賣超(張)
    foreign_streak   外資連續買超(正)/賣超(負)天數
    foreign_pct      外資5日累計買超 ÷ 5日累計成交量,需外部傳入成交量才算
    trust_net_5      投信近5日累計(張)
    trust_streak     投信連續買超天數
    margin_chg_5     融資餘額5日變化率(%)
    """
    q = "SELECT date, code, foreign_net, trust_net, dealer_net FROM inst ORDER BY date"
    inst = pd.read_sql(q, con)
    mgn = pd.read_sql("SELECT date, code, margin_bal FROM margin ORDER BY date", con)
    if inst.empty:
        return {}

    out = {}
    inst_g = {c: g for c, g in inst.groupby("code")}
    mgn_g = {c: g for c, g in mgn.groupby("code")} if not mgn.empty else {}

    for code in codes:
        g = inst_g.get(code)
        if g is None or len(g) < 3:
            continue
        g = g.tail(lookback)
        f = g["foreign_net"] / 1000.0     # 股 → 張
        t = g["trust_net"] / 1000.0

        def streak(s):
            n, sign = 0, 0
            for v in reversed(s.tolist()):
                if v > 0 and sign >= 0:
                    sign, n = 1, n + 1
                elif v < 0 and sign <= 0:
                    sign, n = -1, n + 1
                else:
                    break
            return n * sign

        rec = {
            "foreign_net_5": round(f.tail(5).sum(), 1),
            "foreign_net_20": round(f.sum(), 1),
            "foreign_streak": streak(f),
            "trust_net_5": round(t.tail(5).sum(), 1),
            "trust_streak": streak(t),
            "days": len(g),
        }
        m = mgn_g.get(code)
        if m is not None and len(m) >= 6:
            mb = m["margin_bal"].tail(6).tolist()
            rec["margin_chg_5"] = round(
                (mb[-1] / mb[0] - 1) * 100, 1) if mb[0] else 0.0
        out[code] = rec
    return out


if __name__ == "__main__":
    import sys
    con = init_db()
    if "--backfill" in sys.argv:
        i = sys.argv.index("--backfill")
        arg = sys.argv[i + 1] if len(sys.argv) > i + 1 else "90"
        if len(arg) == 8 and arg.isdigit():      # --backfill 20240101
            print(f"回補至 {arg}(可中斷,重跑會自動接續)")
            backfill(con, start=arg)
        else:
            backfill(con, days=int(arg))
    elif "--check" in sys.argv:
        print("測試 OpenAPI 端點…")
        for nm, url in (("三大法人 T86", OPENAPI_T86), ("融資融券", OPENAPI_MARGIN)):
            try:
                d = _get(url)
                print(f"  ✓ {nm}:{len(d)} 筆")
            except Exception as e:
                print(f"  ✗ {nm}:{e} → 將自動改用證交所網站 JSON 端點")
        print("\n測試網站端點(往前找最近有資料的交易日)…")
        for nm, fn, url in (("三大法人", fetch_institutional, WEB_T86),
                            ("融資融券", fetch_margin, WEB_MARGIN)):
            got = False
            for back in range(0, 7):
                d = (datetime.now() - timedelta(days=back)).strftime("%Y%m%d")
                try:
                    df = fn(d)
                    if len(df):
                        print(f"  ✓ {nm}:{d} 共 {len(df)} 檔")
                        got = True
                        break
                except Exception:
                    continue
            if not got:
                print(f"  ✗ {nm}:近 7 天都抓不到。原始欄位如下,供比對:")
                try:
                    raw = _get(url.format(d=datetime.now().strftime("%Y%m%d")))
                    if isinstance(raw, dict):
                        print(f"      stat={raw.get('stat')}")
                        for t in raw.get("tables", [])[:3]:
                            print(f"      表『{t.get('title','')[:20]}』"
                                  f"欄位:{t.get('fields', [])[:8]}")
                        if raw.get("fields"):
                            print(f"      欄位:{raw['fields'][:8]}")
                except Exception as e:
                    print(f"      無法取得:{type(e).__name__}: {e}")

        con2 = init_db()
        ni = con2.execute("SELECT COUNT(*) FROM inst").fetchone()[0]
        nm2 = con2.execute("SELECT COUNT(*) FROM margin").fetchone()[0]
        print(f"\n本地資料庫:法人 {ni:,} 筆 / 融資 {nm2:,} 筆")
        if nm2 == 0 and ni > 0:
            print("  → 融資從未成功寫入。回補時該端點一直失敗。")
    else:
        n, nm = update_today(con)
        print(f"更新今日籌碼:法人 {n} 檔 / 融資 {nm} 檔")
