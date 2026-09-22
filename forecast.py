#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
價格區間估計 — 不做預測,只做機率測量

為什麼不給「目標價」:
    從技術指標推導單一目標價,準確度極低。任何宣稱「目標價 285 元」的東西
    都是把一個寬到沒有意義的分配壓縮成一個點,丟掉了最重要的資訊——不確定性。

這裡改用「過濾式歷史模擬」(Filtered Historical Simulation):
    1. 取該股過去 2 年的 N 日報酬分配(非參數,自動處理厚尾)
    2. 用「當前波動率 ÷ 歷史平均波動率」縮放,反映當下的波動狀態
    3. 取分位數當作機率區間

這種區間可以被驗證:如果說「70% 機率」,就該有 70% 的時候價格真的落在裡面。
calibration_test() 就是在做這件事。
"""

import numpy as np
import pandas as pd


def ewma_vol(ret, lam=0.94):
    """RiskMetrics 式指數加權波動率,對近期變化較敏感。"""
    v = ret.ewm(alpha=1 - lam, adjust=False).var()
    return np.sqrt(v)


# 膨脹係數:在 43 檔訓練股票上擬合,42 檔測試股票上驗證。
# 原因:2 年歷史只含約 8 個獨立的 60 日期間,經驗分位數會系統性低估尾部。
# 120 日以上就算調到搜尋上限也無法校準(最佳仍差 29pp),故不提供。
INFLATION = {20: 1.10, 60: 1.25}
MAX_HORIZON = 60


def price_range(close, horizon=20, lookback=504, levels=(0.5, 0.7, 0.9)):
    """
    回傳指定天數後的價格機率區間。

    close    : 收盤價 Series
    horizon  : 預測天數(交易日)
    lookback : 用多久的歷史建立分配(504 ≈ 2 年)
    levels   : 要哪些機率水準

    回傳 dict: {"p50": (low, high), "p70": (...), "p90": (...), ...}
    """
    if horizon > MAX_HORIZON:
        raise ValueError(
            f"不提供 {horizon} 日區間。實測顯示超過 {MAX_HORIZON} 日的區間"
            f"無法校準(宣稱90%實際僅約78%),給出來會誤導。")
    if len(close) < lookback // 2 + horizon:
        return None

    lr = np.log(close / close.shift(1)).dropna()
    hist = lr.tail(lookback)
    if len(hist) < 120:
        return None

    # 波動率縮放:當前波動 vs 歷史平均波動
    cur_vol = float(ewma_vol(hist).iloc[-1])
    avg_vol = float(hist.std())
    if not np.isfinite(cur_vol) or avg_vol <= 0:
        return None
    scale = np.clip(cur_vol / avg_vol, 0.5, 2.5)

    # N 日累積報酬的經驗分配(重疊視窗,樣本較多但相關)
    cum = hist.rolling(horizon).sum().dropna()
    if len(cum) < 60:
        return None
    # 依天期套用經驗校準係數(最近的已驗證天期)
    infl = INFLATION.get(horizon) or INFLATION[
        min(INFLATION, key=lambda h: abs(h - horizon))]
    cum_scaled = cum * scale * infl

    px = float(close.iloc[-1])
    out = {"price": round(px, 2), "horizon": horizon,
           "vol_ratio": round(scale, 2), "inflation": infl,
           "ann_vol": round(cur_vol * np.sqrt(252) * 100, 1)}
    for lv in levels:
        lo_q, hi_q = (1 - lv) / 2, 1 - (1 - lv) / 2
        lo = px * np.exp(np.quantile(cum_scaled, lo_q))
        hi = px * np.exp(np.quantile(cum_scaled, hi_q))
        out[f"p{int(lv * 100)}"] = (round(lo, 2), round(hi, 2))
    out["median"] = round(px * np.exp(float(np.median(cum_scaled))), 2)
    return out


def structural_levels(high, low, close, window=10, lookback=250, tol=0.02):
    """
    結構性支撐/壓力 — 這是「描述」不是「預測」。

    找出過去的轉折高低點,把價格相近的聚成一群。
    被測試越多次的價位,參考價值越高(但不保證會守住)。
    """
    h, l = high.tail(lookback), low.tail(lookback)
    px = float(close.iloc[-1])

    piv_h = h[(h == h.rolling(window * 2 + 1, center=True).max())].dropna()
    piv_l = l[(l == l.rolling(window * 2 + 1, center=True).min())].dropna()

    def cluster(s):
        if s.empty:
            return []
        vals = sorted(s.tolist())
        groups, cur = [], [vals[0]]
        for v in vals[1:]:
            if v <= cur[-1] * (1 + tol):
                cur.append(v)
            else:
                groups.append(cur); cur = [v]
        groups.append(cur)
        return [(round(float(np.mean(g)), 2), len(g)) for g in groups]

    res = [(p, n) for p, n in cluster(piv_h) if p > px]
    sup = [(p, n) for p, n in cluster(piv_l) if p < px]
    return {
        "resistance": sorted(res, key=lambda x: x[0])[:3],
        "support": sorted(sup, key=lambda x: -x[0])[:3],
    }


def confidence(close, horizon=20):
    """
    區間可信度 0-100。低分代表這個區間本身就不該太當真。

    三個成分:
      波動穩定度 — 當前波動偏離歷史越多,分配越不適用
      樣本充足度 — 歷史資料夠不夠長
      分配穩定度 — 近一年與近兩年的分配差異多大
    """
    lr = np.log(close / close.shift(1)).dropna()
    if len(lr) < 300:
        return 0, ["歷史資料不足"]
    notes, sc = [], 100

    cur, avg = float(ewma_vol(lr.tail(504)).iloc[-1]), float(lr.tail(504).std())
    ratio = cur / avg if avg > 0 else 1
    if not 0.7 <= ratio <= 1.4:
        sc -= 25
        notes.append(f"波動偏離常態({ratio:.1f}倍)")

    if len(lr) < 504:
        sc -= 20
        notes.append(f"歷史僅 {len(lr)} 日")

    c1 = lr.tail(252).rolling(horizon).sum().dropna()
    c2 = lr.tail(504).rolling(horizon).sum().dropna()
    if len(c1) > 30 and len(c2) > 60:
        d = abs(c1.std() - c2.std()) / (c2.std() + 1e-9)
        if d > 0.35:
            sc -= 25
            notes.append("近期分配與長期差異大")

    # 近期是否出現極端跳動
    if float(lr.tail(60).abs().max()) > 5 * float(lr.tail(504).std()):
        sc -= 15
        notes.append("近期有極端跳動")

    return max(0, sc), notes


def calibration_test(price_dict, horizon=20, lookback=504, levels=(0.5, 0.7, 0.9)):
    """
    校準檢定:宣稱 70% 的區間,實際包含價格的比例是多少?

    這是這個模組唯一真正重要的函式。區間如果沒校準過,就只是好看的數字。
    """
    rec = {lv: [0, 0] for lv in levels}          # [命中, 總數]
    for tk, df in price_dict.items():
        c = df["Close"]
        if len(c) < lookback + horizon + 60:
            continue
        # 每 horizon 天取一次,避免樣本重疊
        for i in range(lookback, len(c) - horizon, horizon):
            r = price_range(c.iloc[:i], horizon, lookback, levels)
            if not r:
                continue
            future = float(c.iloc[i + horizon - 1])
            for lv in levels:
                lo, hi = r[f"p{int(lv * 100)}"]
                rec[lv][1] += 1
                if lo <= future <= hi:
                    rec[lv][0] += 1
    return {lv: (h / n if n else 0, n) for lv, (h, n) in rec.items()}
