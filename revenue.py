#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
月營收模組 — 台股獨有的高頻基本面資料

台灣上市櫃公司依法須在次月 10 日前公告上月營收。
多數市場只有季報,台灣有月報 —— 這是結構性的資訊優勢。

資料來源:證交所 OpenAPI /opendata/t187ap05_L
        (即公開資訊觀測站 MOPS 的每月營收彙總表,政府資料開放授權條款第1版)
不爬 mops.twse.com.tw 網頁,走官方開放資料管道。

注意:此端點只提供「最新一期快照」,沒有歷史查詢。
     所以本模組每月抓一次、存進本地 SQLite,自行累積歷史。
"""

import json
import sqlite3
import urllib.request
from datetime import datetime

import pandas as pd

import netutil as N

DB = "chips.db"          # 與籌碼共用同一個資料庫
UA = {"User-Agent": "Mozilla/5.0 (compatible; personal-research-script)"}

API = {
    "上市": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    "上櫃": "https://openapi.twse.com.tw/v1/opendata/t187ap05_O",
}

# t187ap05_L 的欄位是繁體中文,必須逐字對應,不能自行翻譯
F_CODE = "公司代號"
F_NAME = "公司名稱"
F_IND = "產業別"
F_YM = "資料年月"
F_MON = "營業收入-當月營收"
F_YOY = "營業收入-去年同月增減(%)"
F_MOM = "營業收入-上月比較增減(%)"
F_CUM_YOY = "累計營業收入-前期比較增減(%)"


def _f(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (ValueError, AttributeError):
        return float("nan")


def roc_to_ym(v):
    """民國年月 11411 → 202511。"""
    s = str(v).strip()
    if len(s) >= 5:
        return int(s[:-2]) + 1911, int(s[-2:])
    return None, None


def init_db(path=DB):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS revenue (
        ym INTEGER, code TEXT, name TEXT, industry TEXT,
        rev REAL,          -- 當月營收(千元)
        yoy REAL,          -- 去年同月增減 %
        mom REAL,          -- 上月增減 %
        cum_yoy REAL,      -- 累計年增 %
        PRIMARY KEY (ym, code));
    CREATE INDEX IF NOT EXISTS ix_rev ON revenue(code, ym);
    """)
    con.commit()
    return con


def fetch_revenue(market="上市"):
    rows = N.get_json(API[market], timeout=30, headers=UA)
    recs = []
    for r in rows:
        code = str(r.get(F_CODE, "")).strip()
        if not (code.isdigit() and len(code) == 4):
            continue
        y, m = roc_to_ym(r.get(F_YM))
        if not y:
            continue
        recs.append({
            "ym": y * 100 + m, "code": code,
            "name": str(r.get(F_NAME, "")).strip(),
            "industry": str(r.get(F_IND, "")).strip(),
            "rev": _f(r.get(F_MON)), "yoy": _f(r.get(F_YOY)),
            "mom": _f(r.get(F_MOM)), "cum_yoy": _f(r.get(F_CUM_YOY)),
        })
    return pd.DataFrame(recs)


def update(con, markets=("上市",)):
    total = 0
    for mk in markets:
        try:
            df = fetch_revenue(mk)
        except Exception as e:
            print(f"  {mk}營收抓取失敗:{e}")
            continue
        if df.empty:
            continue
        df.to_sql("tmp_r", con, if_exists="replace", index=False)
        con.execute("INSERT OR REPLACE INTO revenue SELECT * FROM tmp_r")
        con.execute("DROP TABLE tmp_r")
        total += len(df)
        ym = int(df["ym"].max())
        print(f"  {mk}:{len(df)} 檔,資料年月 {ym // 100}/{ym % 100:02d}")
    con.commit()
    return total


def revenue_features(con, codes, months=13):
    """
    回傳 {code: {...}}

    yoy            最新月營收年增率 %
    yoy_streak     連續正/負年增的月數(正=成長)
    yoy_avg3       近3個月年增率平均
    cum_yoy        累計營收年增率 %
    accel          年增率是否加速(最新 > 前3月平均)
    ym             資料年月
    """
    df = pd.read_sql("SELECT * FROM revenue ORDER BY ym", con)
    if df.empty:
        return {}
    out = {}
    for code, g in df.groupby("code"):
        if code not in codes or len(g) < 1:
            continue
        g = g.tail(months)
        yoy = g["yoy"].tolist()
        n, sign = 0, 0
        for v in reversed(yoy):
            if pd.isna(v):
                break
            if v > 0 and sign >= 0:
                sign, n = 1, n + 1
            elif v < 0 and sign <= 0:
                sign, n = -1, n + 1
            else:
                break
        last = g.iloc[-1]
        avg3 = pd.Series(yoy[-3:]).mean()
        prev3 = pd.Series(yoy[-4:-1]).mean() if len(yoy) >= 4 else float("nan")
        out[code] = {
            "yoy": None if pd.isna(last["yoy"]) else round(float(last["yoy"]), 1),
            "yoy_streak": n * sign,
            "yoy_avg3": None if pd.isna(avg3) else round(float(avg3), 1),
            "cum_yoy": None if pd.isna(last["cum_yoy"]) else round(float(last["cum_yoy"]), 1),
            "accel": bool(not pd.isna(prev3) and not pd.isna(avg3) and avg3 > prev3),
            "ym": int(last["ym"]), "months": len(g),
            "industry": last["industry"],
        }
    return out


if __name__ == "__main__":
    import sys
    con = init_db()
    if "--check" in sys.argv:
        for mk, url in API.items():
            try:
                d = N.get_json(url, timeout=30, headers=UA)
                s = d[0] if d else {}
                y, m = roc_to_ym(s.get(F_YM))
                print(f"  ✓ {mk}每月營收:{len(d)} 筆,最新 {y}/{m:02d}")
                print(f"    欄位範例:{list(s.keys())[:6]}")
            except Exception as e:
                print(f"  ✗ {mk}:{e}")
    else:
        print(f"更新月營收:{update(con)} 筆")
        n = con.execute("SELECT COUNT(DISTINCT ym) FROM revenue").fetchone()[0]
        print(f"資料庫現有 {n} 個月份"
              + ("(需累積 ≥4 個月才能算成長趨勢)" if n < 4 else ""))
