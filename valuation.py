#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股價偏離度 — 標記過高與過低

「折溢價」需要一個基準。個股沒有淨值(那是 ETF 和封閉式基金才有的概念),
所以這裡提供三種基準,各自意義不同:

  1. 乖離率 (BIAS)     — 相對自身均線。純技術,不假設任何合理價。
  2. 乖離分位          — 相對該股「自己的歷史乖離分布」。
                        解決跨股不可比的問題:10% 乖離對台積電是異常,
                        對某些高波動小型股是家常便飯。
  3. 同業估值折溢價    — 本益比相對同產業中位數。這才是最接近
                        「便宜/貴」直覺的一種。

⚠️ 偏離大不代表會回歸。動能因子的存在本身就說明強者恆強。
   高乖離可能是頭部,也可能是趨勢起點。這是「標記」不是「訊號」。
   backtest_deviation() 就是在檢驗它到底能不能預測。
"""

import numpy as np
import pandas as pd

# 分級門檻(以自身歷史乖離分位為準)
BANDS = [
    (0.98, "極度溢價", "🔴"),
    (0.90, "顯著溢價", "🟠"),
    (0.75, "偏高", "🟡"),
    (0.25, "正常", "⚪"),
    (0.10, "偏低", "🟢"),
    (0.02, "顯著折價", "🔵"),
    (0.00, "極度折價", "🟣"),
]


def bias(close, n=60):
    """乖離率 % = (股價 − N日均線) ÷ N日均線 × 100"""
    ma = close.rolling(n).mean()
    return (close / ma - 1) * 100


def deviation(close, n=60, lookback=750):
    """
    回傳該股當前的偏離狀態。

    bias_pct   當前乖離率 %
    pctile     該乖離在自身過去 lookback 日分布中的分位(0-1)
    zscore     標準化偏離倍數
    band       分級文字
    revert_to  回到均線需要的漲跌幅 %
    """
    if len(close) < n + 120:
        return None
    b = bias(close, n).dropna()
    if len(b) < 120:
        return None
    hist = b.tail(lookback)
    cur = float(b.iloc[-1])
    pct = float((hist < cur).mean())
    sd = float(hist.std())
    z = (cur - float(hist.mean())) / sd if sd > 0 else 0.0

    band = icon = ""
    for th, name, ic in BANDS:
        if pct >= th:
            band, icon = name, ic
            break

    ma = float(close.rolling(n).mean().iloc[-1])
    px = float(close.iloc[-1])
    return {
        "ma": round(ma, 2), "window": n,
        "bias_pct": round(cur, 2),
        "pctile": round(pct * 100, 1),
        "zscore": round(z, 2),
        "band": band, "icon": icon,
        "revert_to": round((ma / px - 1) * 100, 2),
        "extreme": pct >= 0.90 or pct <= 0.10,
    }


def multi_horizon(close, windows=(20, 60, 200)):
    """多天期一起看。短中長期同向才是真正的極端。"""
    out = {}
    for n in windows:
        d = deviation(close, n)
        if d:
            out[n] = d
    if not out:
        return None
    ex = [n for n, d in out.items() if d["extreme"]]
    hi = sum(1 for d in out.values() if d["pctile"] >= 90)
    lo = sum(1 for d in out.values() if d["pctile"] <= 10)
    out["_summary"] = {
        "extreme_windows": ex,
        "direction": "溢價" if hi > lo else ("折價" if lo > hi else "混合"),
        "agreement": max(hi, lo),          # 幾個天期同向
        "all_agree": max(hi, lo) == len(windows),
    }
    return out


# ============================================================
# 同業估值折溢價
# ============================================================

def industry_premium(fund_map, rev_map):
    """
    本益比相對同產業中位數的折溢價 %。

    fund_map: {code: {"pe":..., "pb":...}}   來自 TWSE 基本面
    rev_map:  {code: {"industry":...}}       來自月營收(含產業別)
    """
    rows = []
    for code, f in fund_map.items():
        ind = (rev_map.get(code) or {}).get("industry")
        pe = f.get("pe")
        if not ind or pe is None or not np.isfinite(pe) or pe <= 0:
            continue
        rows.append({"code": code, "industry": ind, "pe": float(pe),
                     "pb": f.get("pb")})
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    med = df.groupby("industry")["pe"].transform("median")
    cnt = df.groupby("industry")["pe"].transform("size")
    df["prem"] = (df["pe"] / med - 1) * 100
    df["ind_median_pe"] = med.round(1)
    df["ind_n"] = cnt
    out = {}
    for _, r in df.iterrows():
        if r["ind_n"] < 5:          # 同業樣本太少,中位數沒有代表性
            continue
        out[r["code"]] = {
            "pe": round(r["pe"], 1),
            "industry": r["industry"],
            "ind_median_pe": r["ind_median_pe"],
            "ind_n": int(r["ind_n"]),
            "premium_pct": round(r["prem"], 1),
            "label": ("溢價" if r["prem"] > 20 else
                      "折價" if r["prem"] < -20 else "接近同業"),
        }
    return out


# ============================================================
# 檢驗:偏離到底能不能預測?
# ============================================================

def backtest_deviation(data, n=60, fwd=60, lookback=750):
    """
    對每檔股票每一天算乖離分位,看不同分位的未來報酬。
    使用不重疊樣本,避免重疊視窗高估顯著性。
    """
    rows = []
    for tk, df in data.items():
        c = df["Close"]
        if len(c) < lookback + n + fwd:
            continue
        b = bias(c, n)
        # 滾動分位(僅用過去資料,無前視)
        pct = b.rolling(lookback).apply(
            lambda w: (w[:-1] < w[-1]).mean(), raw=True)
        f = (c.shift(-fwd) / c - 1) * 100
        d = pd.DataFrame({"pct": pct, "fwd": f, "bias": b}).dropna()
        rows.append(d.iloc[::fwd])       # 不重疊
    if not rows:
        return None
    return pd.concat(rows, ignore_index=True)
