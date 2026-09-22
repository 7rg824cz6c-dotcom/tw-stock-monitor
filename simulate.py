#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
歷史模擬 — 完整策略回測

與前面 backtest_tw.py 的差別很重要:
  backtest_tw.py  只算「訊號出現後的平均報酬」。那不是績效。
  simulate.py     真的買賣:有資金限制、部位大小、停損執行、交易成本。

同一個策略在兩者可能得出完全相反的結論。原因:
  - 訊號可能同時出現 20 檔,但資金只夠買 10 檔
  - 停損會在報酬實現前就出場,砍掉左尾也砍掉部分右尾
  - 成本吃掉的比多數人想像的多

⚠️ 這裡最容易自我欺騙。看完結果回頭改參數再跑一次,就已經在擬合了。
   紙上交易(trading.py 的 paper 模式)才是誠實的驗證。

用法:
    python backtest_tw.py --download
    python simulate.py                # 預設參數
    python simulate.py --compare      # 與買入持有、隨機選股對照
"""

import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tw_stock_monitor import rsi, kd, macd, atr
from trading import (buy_cost, sell_proceeds, SLIPPAGE, round_trip_cost_pct,
                     DEFAULTS)

DATA = "twdata"     # 股價 CSV 存放資料夾


def load(path=DATA):
    """
    讀取 twdata/*.csv。刻意不依賴 backtest_tw,讓本檔可獨立執行。
    支援兩種格式:有 Adj Close 欄(還原權值)或只有 OHLCV。
    """
    import glob
    out = {}
    for f in glob.glob(os.path.join(path, "*.csv")):
        try:
            d = pd.read_csv(f, parse_dates=["Date"]).set_index("Date").sort_index()
        except (ValueError, KeyError):
            continue
        d = d.rename(columns={"Adj Close": "Adj"}).dropna()
        if len(d) < 600 or "Close" not in d:
            continue
        if "Adj" in d:      # 用還原權值價,避免除權息造成假跌破
            ratio = d["Adj"] / d["Close"]
            for c in ("Open", "High", "Low"):
                if c in d:
                    d[c] = d[c] * ratio
            d["Close"] = d["Adj"]
        out[os.path.basename(f)[:-4]] = d[["Open", "High", "Low", "Close", "Volume"]]
    return out


def build_signals(data, rebalance=5):
    """
    逐日計算每檔股票的技術面分數(與主程式同邏輯,無籌碼營收)。
    回傳 {date: DataFrame[ticker, score, close, open, atr, ma20]}
    """
    px = pd.DataFrame({k: v["Close"] for k, v in data.items()})
    wr = (2 * (px / px.shift(63) - 1) + (px.shift(63) / px.shift(126) - 1)
          + (px.shift(126) / px.shift(189) - 1) + (px.shift(189) / px.shift(252) - 1))
    rs = wr.rank(axis=1, pct=True) * 99

    frames = []
    for tk, df in data.items():
        c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
        if len(c) < 300:
            continue
        ma20, ma60, ma200 = (c.rolling(n).mean() for n in (20, 60, 200))
        r = rsi(c); k_, d_ = kd(h, l, c); _, _, hist = macd(c); a = atr(h, l, c)
        R = rs[tk] if tk in rs else pd.Series(50.0, index=c.index)

        s = (14 * ((c > ma200) & (ma200 > ma200.shift(22))).astype(float)
             + 7 * ((ma20 > ma60) & (ma60 > ma200)).astype(float)
             + 11 * (R >= 70).astype(float)
             + 10 * r.between(35, 58).astype(float)
             + 8 * ((((k_.shift() <= d_.shift()) & (k_ > d_) & (k_ < 60))
                     | ((hist > 0) & (hist.shift() <= 0)))).astype(float))
        s = s / 50 * 100
        s -= 8 * (c >= c.rolling(250).max() * 0.97).astype(float)
        s -= 8 * (a / c * 100 > 5).astype(float)
        s -= 10 * (c < ma20).astype(float)

        frames.append(pd.DataFrame({
            "ticker": tk, "score": s.clip(0, 100), "close": c,
            "open": df["Open"], "atr": a, "ma20": ma20,
        }).dropna())
    if not frames:
        return {}
    allf = pd.concat(frames)
    allf.index.name = "date"
    return allf.reset_index()


def run_sim(data, params=None, verbose=True):
    p = {**DEFAULTS, **(params or {})}
    sig = build_signals(data)
    if len(sig) == 0:
        return None
    sig = sig.sort_values("date")
    dates = sorted(sig["date"].unique())
    by_date = {d: g.set_index("ticker") for d, g in sig.groupby("date")}

    cash = float(p["capital"])
    pos = {}          # ticker -> dict
    closed, equity = [], []
    last_px = {}      # 最後已知收盤價 —— 停牌或資料缺漏時沿用,避免市值憑空歸零

    for i, d in enumerate(dates):
        today = by_date[d]

        # ── 1. 先處理出場(停損優先,保守) ──
        for t in list(pos):
            if t not in today.index:
                continue
            row = today.loc[t]
            ps = pos[t]
            px = cl = float(row["close"])
            reason = None
            # 以當日最低價判斷停損是否觸發
            lo = float(data[t]["Low"].get(d, cl))
            if lo <= ps["stop"]:
                reason, px = "停損", ps["stop"]
            elif float(row["score"]) < p["exit_score"]:
                reason = "分數轉弱"
            elif p["exit_below_ma20"] and cl < float(row["ma20"]):
                reason = "跌破月線"
            elif (d - ps["date"]).days >= p["max_hold_days"]:
                reason = "持有到期"
            if reason:
                net, _, _ = sell_proceeds(px * (1 - SLIPPAGE), ps["shares"])
                cash += net
                closed.append({
                    "ticker": t, "entry": ps["date"], "exit": d,
                    "entry_px": ps["px"], "exit_px": px, "reason": reason,
                    "pnl": net - ps["cost"], "pnl_pct": (net / ps["cost"] - 1) * 100,
                    "days": (d - ps["date"]).days})
                del pos[t]

        # ── 2. 再處理進場 ──
        if len(pos) < p["max_positions"]:
            cands = today[(today["score"] >= p["entry_score"])
                          & (~today.index.isin(pos))]
            cands = cands.sort_values("score", ascending=False)
            for t, row in cands.iterrows():
                if len(pos) >= p["max_positions"]:
                    break
                px = float(row["open"]) * (1 + SLIPPAGE)
                budget = (cash + sum(
                    float(by_date[d].loc[x, "close"]) * pos[x]["shares"]
                    for x in pos if x in by_date[d].index)) * p["position_pct"]
                lots = int(min(budget, cash) // (px * 1000))
                if lots < 1:
                    continue
                shares = lots * 1000
                total, _ = buy_cost(px, shares)
                if total > cash:
                    continue
                cash -= total
                pos[t] = {"date": d, "px": px, "shares": shares, "cost": total,
                          "stop": px - p["stop_loss_atr"] * float(row["atr"])}

        # ── 3. 記錄淨值 ──
        # 當日有報價就更新,沒有就沿用最後已知價。
        # 若直接略過無報價的部位,市值會在停牌日憑空消失,製造假回撤。
        for t in today.index:
            last_px[t] = float(today.loc[t, "close"])
        mv = 0.0
        stale = 0
        for t in pos:
            if t in last_px:
                mv += last_px[t] * pos[t]["shares"]
                if t not in today.index:
                    stale += 1
        equity.append({"date": d, "total": cash + mv, "n": len(pos),
                       "stale": stale})

    eq = pd.DataFrame(equity).set_index("date")
    cl = pd.DataFrame(closed)
    return {"equity": eq, "closed": cl, "final": float(eq["total"].iloc[-1]),
            "params": p}


def benchmark_buyhold(data, capital=1_000_000, robust=False):
    """
    等權重買入持有,含一次買賣成本。

    robust=True 改用中位數而非平均數。
    平均數對離群值極度敏感:一檔資料錯誤的股票就能讓基準虛高數倍。
    兩者差距大 = 資料裡有離群股主導,先跑 python audit.py。
    """
    px = pd.DataFrame({k: v["Close"] for k, v in data.items()}).dropna(how="all")
    px = px.loc[:, px.iloc[0].notna()]
    norm_all = px / px.iloc[0]
    norm = norm_all.median(axis=1) if robust else norm_all.mean(axis=1)
    cost = 1 - (round_trip_cost_pct() / 100)
    return norm * capital * cost


def stats(eq, cl, capital):
    e = eq["total"]
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 0.1)
    total = e.iloc[-1] / capital - 1
    cagr = (e.iloc[-1] / capital) ** (1 / yrs) - 1
    dd = (e / e.cummax() - 1).min()
    r = e.pct_change().dropna()
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0
    out = {"總報酬": total * 100, "年化": cagr * 100, "最大回撤": dd * 100,
           "夏普": sharpe, "年數": yrs}
    if cl is not None and len(cl):
        w = cl[cl.pnl > 0]
        out.update({
            "交易數": len(cl),
            "勝率": len(w) / len(cl) * 100,
            "期望值": cl.pnl_pct.mean(),
            "平均持有": cl.days.mean(),
            "獲利因子": (w.pnl.sum() / abs(cl[cl.pnl <= 0].pnl.sum())
                        if (cl.pnl <= 0).any() else float("inf")),
        })
    return out


def sweep(data, cap):
    """
    停損敏感度掃描。

    ⚠️ 這是探索,不是最佳化。看完結果就去改預設值,等於在擬合這段歷史。
       正確用法:看「趨勢是否單調」。若放寬停損一路變好,那可能是
       生存者偏誤(樣本全是贏家,抱著就會漲);若有明確轉折點,
       才比較可能是真的訊號。
    """
    print("\n" + "=" * 68)
    print("停損敏感度掃描")
    print("=" * 68)
    print(f"{'停損倍數':<10}{'年化':>9}{'回撤':>9}{'夏普':>7}"
          f"{'交易':>7}{'停損佔比':>10}{'期望值':>9}")
    print("-" * 61)
    rows = []
    for k in (1.5, 2.0, 3.0, 4.0, 6.0, 99.0):
        r = run_sim(data, {"stop_loss_atr": k})
        if not r:
            continue
        st = stats(r["equity"], r["closed"], cap)
        cl = r["closed"]
        pct = (cl["reason"] == "停損").mean() * 100 if len(cl) else 0
        lbl = "無停損" if k > 50 else f"{k}×ATR"
        rows.append((lbl, st, pct))
        print(f"{lbl:<10}{st['年化']:>+8.2f}%{st['最大回撤']:>8.1f}%"
              f"{st['夏普']:>7.2f}{st.get('交易數', 0):>7}"
              f"{pct:>9.0f}%{st.get('期望值', 0):>+8.2f}%")

    anns = [x[1]["年化"] for x in rows]
    exps = [x[1].get("期望值", 0) for x in rows]
    ann_mono = anns == sorted(anns)
    exp_mono = exps == sorted(exps)

    # ── 先判斷雜訊,再談訊號 ──
    # 「非單調」不等於「有轉折點」。真實的參數效應是平滑的;
    # 相鄰設定之間大幅來回擺盪,代表結果由少數幾筆交易的運氣決定。
    d = np.diff(anns)
    max_step = float(np.abs(d).max()) if len(d) else 0.0
    flips = sum(1 for i in range(len(d) - 1) if d[i] * d[i + 1] < 0)
    span = max(anns) - min(anns)

    print()
    print(f"  參數敏感度:全距 {span:.1f}pp,最大單步跳動 {max_step:.1f}pp,"
          f"方向反轉 {flips}/{max(len(d)-1, 1)} 次")

    noisy = flips >= (len(d) - 1) * 0.6 and max_step > 5
    if noisy:
        print(f"""
  ⚠ 這是雜訊,不是訊號。
    停損倍數只差一級,年化就擺盪 {max_step:.1f} 個百分點,而且方向來回反轉
    {flips} 次。真實的參數效應是平滑的 —— 放寬停損應該讓績效逐步改變,
    不會忽高忽低。

    這代表整體績效由少數幾筆交易的進出時點決定,而不是策略邏輯。
    換一段期間、換一批股票,最佳參數會完全不同。

    → 不要挑「看起來最好」的那一格。那格只是這次運氣好。
    → 也代表策略對基準的超額報酬本身就在雜訊範圍內。""")
    elif ann_mono and exp_mono:
        print("  ⚠ 年化與期望值都隨停損放寬單調變好,沒有轉折點。")
        print("    這在「樣本只含長期存活股」時必然出現 —— 抱著不動最佳,")
        print("    因為樣本裡沒有會一路跌到下市的股票。")
        print("    → 不要據此拿掉停損。真實世界的左尾比這份資料厚得多。")
    elif ann_mono and not exp_mono:
        print("  年化單調變好,但每筆期望值反而變差。")
        print("    代表放寬停損只是讓少數大贏家跑得更遠,")
        print("    多數交易的品質是下降的 —— 報酬集中在極少數部位,")
        print("    實際執行時心理壓力與單一部位風險都會大很多。")
    else:
        best = max(rows, key=lambda x: x[1]["夏普"])
        print(f"  夏普最佳:{best[0]} —— 變化平滑且存在轉折點,")
        print("    比較可能是真實效應。但仍需以紙上交易往前驗證,")
        print("    不要直接改預設值。")
    print("\n  無論哪種結果:這是同一段歷史跑出來的,改參數去迎合它就是擬合。")


def main():
    import glob
    if not os.path.isdir(DATA) or not glob.glob(os.path.join(DATA, "*.csv")):
        print(f"找不到股價資料(預期在 ./{DATA}/ 資料夾)。")
        print("請先執行:python backtest_tw.py --download")
        print("（該指令會下載成交金額前 120 大個股的 10 年資料）")
        return
    data = load()
    if not data:
        print(f"{DATA}/ 裡沒有可用的 CSV(每檔需至少 600 個交易日)。")
        return
    cap = DEFAULTS["capital"]
    print(f"載入 {len(data)} 檔,初始資金 {cap:,} 元")
    print(f"交易成本:每次來回 {round_trip_cost_pct():.3f}%\n")

    r = run_sim(data)
    if not r:
        print("資料不足以模擬。")
        return
    s = stats(r["equity"], r["closed"], cap)

    print("=" * 60)
    print("策略模擬結果")
    print("=" * 60)
    print(f"  期間        {r['equity'].index[0].date()} → "
          f"{r['equity'].index[-1].date()}({s['年數']:.1f} 年)")
    print(f"  最終資產    {r['final']:,.0f} 元")
    print(f"  總報酬      {s['總報酬']:+.2f}%   年化 {s['年化']:+.2f}%")
    print(f"  最大回撤    {s['最大回撤']:.2f}%   夏普 {s['夏普']:.2f}")
    if "交易數" in s:
        print(f"  交易次數    {s['交易數']} 筆,勝率 {s['勝率']:.1f}%,"
              f"期望值 {s['期望值']:+.2f}%/筆")
        print(f"  平均持有    {s['平均持有']:.0f} 天,獲利因子 {s['獲利因子']:.2f}")

    if "--sweep" in sys.argv:
        sweep(data, cap)
        return

    if "--compare" in sys.argv or True:
        bh = benchmark_buyhold(data, cap)
        bs = stats(pd.DataFrame({"total": bh}), None, cap)
        bhm = benchmark_buyhold(data, cap, robust=True)
        bms = stats(pd.DataFrame({"total": bhm}), None, cap)
        print("\n" + "=" * 60)
        print("對照:等權重買入持有")
        print("=" * 60)
        print(f"  最終資產    {bh.iloc[-1]:,.0f} 元")
        print(f"  總報酬      {bs['總報酬']:+.2f}%   年化 {bs['年化']:+.2f}%")
        print(f"  最大回撤    {bs['最大回撤']:.2f}%   夏普 {bs['夏普']:.2f}")
        print(f"\n  中位數版本  年化 {bms['年化']:+.2f}%   回撤 {bms['最大回撤']:.2f}%")
        gap = bs["年化"] - bms["年化"]
        if abs(gap) > 5:
            print(f"  ⚠ 平均數版本比中位數高 {gap:.1f} 個百分點 —— "
                  f"少數離群股主導了基準。")
            print("    請先執行 python audit.py 檢查資料品質。")
        # 生存者偏誤警示 —— 比任何 bug 都影響結論
        px = pd.DataFrame({k: v["Close"] for k, v in data.items()})
        yrs = max((px.index[-1] - px.index[0]).days / 365.25, 0.1)
        rets = (px.ffill().iloc[-1] / px.bfill().iloc[0]) ** (1 / yrs) - 1
        med = float(rets.median() * 100)
        if med > 12:
            print("\n" + "=" * 60)
            print("⚠ 生存者偏誤")
            print("=" * 60)
            print(f"  樣本中位數個股年化 {med:.1f}%,大盤同期約 10-12%。")
            print("  選股池是「今天成交金額前 120 大」——今天大的公司,")
            print("  正是過去十年漲上來的。下市或萎縮的股票不在樣本裡。")
            print("  → 絕對報酬(策略與買入持有)都被高估;")
            print("     兩者「差距」仍可參考,但別拿年化去推估未來。")

        print("\n" + "=" * 60)
        diff = s["年化"] - bs["年化"]
        print(f"判定:策略年化{'贏' if diff > 0 else '輸'} 買入持有 {abs(diff):.2f} 個百分點")
        if diff <= 0:
            print("      策略沒有創造價值。買入持有更省事、成本更低、稅負更少。")
        elif abs(s["最大回撤"]) < abs(bs["最大回撤"]):
            print("      且回撤更小。但這是單一期間單一市場,不要過度推論。")
        else:
            print("      但回撤更大——超額報酬是用更高風險換來的。")
        print("=" * 60)

    if r["closed"] is not None and len(r["closed"]):
        print("\n出場原因分布:")
        g = r["closed"].groupby("reason").agg(n=("pnl_pct", "size"),
                                              avg=("pnl_pct", "mean"))
        for k, v in g.sort_values("n", ascending=False).iterrows():
            print(f"  {k:<10} {int(v['n']):>4} 筆,平均 {v['avg']:+.2f}%")


if __name__ == "__main__":
    main()
