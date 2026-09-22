#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
權重最佳化器 — 內建防過度擬合機制

為什麼需要這個檔案:
    主程式的權重(14、11、10、8…)是手設的主觀判斷。這個工具讓你用自己的
    台股資料把它們換成有根據的值。

為什麼最佳化很危險:
    10 個權重、少量獨立樣本,隨便調都能讓「歷史績效」變好看,但那是在擬合雜訊。
    本工具強制以下紀律,不能關閉:

    1. 股票切分   — 一半訓練、一半測試。測試集絕不參與最佳化。
    2. 時間切分   — 前 70% 時間訓練、後 30% 測試。避免用未來資訊。
    3. 粗網格     — 權重只能取 {0, 5, 10, 15, 20},不允許精細調整。
                    參數解析度越細,過度擬合越嚴重。
    4. 落差警告   — 訓練與測試差距過大時,明確告訴你這組權重不可信。
    5. 基準對照   — 一律與「等權重」及「原始手設權重」比較。
                    贏不過等權重,代表最佳化沒有帶來價值。

使用:
    python backtest_tw.py --download      # 先備妥股價
    python chips.py --backfill 250        # 再備妥籌碼(越多越好)
    python optimize.py                    # 開始最佳化
"""

import glob
import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_tw import load, benchmark, DATA
from tw_stock_monitor import rsi, kd, macd, atr
import chips as C
import revenue as REV

GRID = [0, 5, 10, 15, 20]     # 粗網格,刻意不給精細選項
FWD = 60                       # 評估用的未來報酬天數
MIN_TEST_SAMPLES = 200


# ============================================================
# 特徵
# ============================================================

def build_features(data, con=None):
    """把每檔股票每一天的「條件是否成立」算成 0/1 矩陣。"""
    px = pd.DataFrame({k: v["Close"] for k, v in data.items()})
    wr = (2 * (px / px.shift(63) - 1) + (px.shift(63) / px.shift(126) - 1)
          + (px.shift(126) / px.shift(189) - 1) + (px.shift(189) / px.shift(252) - 1))
    rs = wr.rank(axis=1, pct=True) * 99

    chip = C.chip_features(con, [k.split(".")[0] for k in data]) if con else {}
    rows = []
    for tk, df in data.items():
        c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
        if len(c) < 300:
            continue
        ma20, ma60, ma200 = (c.rolling(n).mean() for n in (20, 60, 200))
        r = rsi(c); k_, d_ = kd(h, l, c); _, _, hist = macd(c); a = atr(h, l, c)
        R = rs[tk] if tk in rs else pd.Series(50, index=c.index)

        f = pd.DataFrame({
            "年線上揚": (c > ma200) & (ma200 > ma200.shift(22)),
            "均線多頭": (ma20 > ma60) & (ma60 > ma200),
            "RS高": R >= 70,
            "RSI回檔": r.between(35, 58),
            "KD/MACD轉強": (((k_.shift() <= d_.shift()) & (k_ > d_) & (k_ < 60))
                            | ((hist > 0) & (hist.shift() <= 0))),
            "量能放大": v.rolling(5).mean() / v.rolling(20).mean() > 1.2,
        })
        # 扣分項(權重固定為負,不參與最佳化,避免搜尋空間爆炸)
        f["_貼近年高"] = c >= c.rolling(250).max() * 0.97
        f["_高波動"] = a / c * 100 > 5
        f["_跌破月線"] = c < ma20
        f["fwd"] = (c.shift(-FWD) / c - 1) * 100
        f["ticker"] = tk
        f = f.reset_index().rename(columns={f.index.name or "index": "date"})
        rows.append(f.dropna())
    return pd.concat(rows, ignore_index=True)


FEATURES = ["年線上揚", "均線多頭", "RS高", "RSI回檔", "KD/MACD轉強", "量能放大"]
PENALTY = {"_貼近年高": -8, "_高波動": -8, "_跌破月線": -10}


def score_with(df, weights):
    tot = sum(weights.values()) or 1
    s = sum(df[f].astype(float) * w for f, w in weights.items()) / tot * 100
    for p, w in PENALTY.items():
        s = s + df[p].astype(float) * w
    return s.clip(0, 100)


def evaluate(df, weights, pct=0.15):
    """取分數最高的前 pct 檔,回傳其平均未來報酬減去全體平均。"""
    s = score_with(df, weights)
    n = max(MIN_TEST_SAMPLES // 4, int(len(df) * pct))
    top = df.loc[s.nlargest(n).index, "fwd"]
    return float(top.mean() - df["fwd"].mean()), len(top)


# ============================================================
# 最佳化
# ============================================================

def optimize(train, test, base_weights, max_iter=4):
    """座標下降。每次只調一個權重,在粗網格上取最佳。"""
    w = dict(base_weights)
    print(f"起點(原始手設權重):訓練 {evaluate(train, w)[0]:+.2f}%")
    for it in range(max_iter):
        improved = False
        for f in FEATURES:
            best, bw = evaluate(train, w)[0], w[f]
            for g in GRID:
                if g == w[f]:
                    continue
                trial = dict(w); trial[f] = g
                if sum(trial.values()) == 0:
                    continue
                sc = evaluate(train, trial)[0]
                if sc > best + 1e-9:
                    best, bw, improved = sc, g, True
            w[f] = bw
        print(f"  第 {it + 1} 輪:訓練 {evaluate(train, w)[0]:+.2f}%  {w}")
        if not improved:
            break
    return w


def report(train, test, cands):
    print("\n" + "=" * 72)
    print("結果比較(超額報酬 = 前15%標的平均 − 全體平均,單位 %)")
    print("=" * 72)
    print(f"{'權重方案':<18}{'訓練集':>10}{'測試集':>10}{'落差':>10}{'判定':>16}")
    print("-" * 66)
    rows = []
    for name, w in cands.items():
        tr, _ = evaluate(train, w)
        te, n = evaluate(test, w)
        gap = tr - te
        if n < MIN_TEST_SAMPLES:
            verdict = "樣本不足"
        elif te <= 0:
            verdict = "✗ 測試集無效"
        elif gap > abs(te) * 0.6:
            verdict = "✗ 過度擬合"
        else:
            verdict = "✓ 可考慮"
        rows.append((name, tr, te, gap, verdict, w))
        print(f"{name:<18}{tr:>+9.2f}{te:>+9.2f}{gap:>+9.2f}{verdict:>16}")

    print("\n" + "=" * 72)
    ok = [r for r in rows if r[4] == "✓ 可考慮"]
    eq = next((r for r in rows if r[0] == "等權重"), None)
    if not ok:
        print("結論:沒有任何一組權重通過測試集驗證。")
        print("      這是常見結果,不是失敗——代表資料還不足以支持權重調整。")
        print("      維持原本的手設權重,繼續累積籌碼與營收資料再試。")
        return
    best = max(ok, key=lambda r: r[2])
    if eq and best[2] <= eq[2] and best[0] != "等權重":
        print(f"結論:最佳化後的權重({best[2]:+.2f}%)贏不過等權重({eq[2]:+.2f}%)。")
        print("      這代表「哪個條件比較重要」在你的資料上看不出來。")
        print("      建議直接用等權重——參數越少越不容易騙自己。")
        return
    print(f"結論:建議採用「{best[0]}」")
    print(f"      訓練 {best[1]:+.2f}% / 測試 {best[2]:+.2f}%")
    print(f"      {best[5]}")
    print("\n※ 即使通過驗證,這仍是單一市場單一期間的結果。")
    print("  換個市場環境權重可能就不適用。定期重跑。")


def main():
    if not os.path.isdir(DATA) or not glob.glob(os.path.join(DATA, "*.csv")):
        print("找不到股價資料。請先執行:python backtest_tw.py --download")
        return
    data = load()
    print(f"載入 {len(data)} 檔股票")

    con = None
    if os.path.exists("chips.db"):
        con = C.init_db()
        nd = con.execute("SELECT COUNT(DISTINCT date) FROM inst").fetchone()[0]
        print(f"籌碼資料庫:{nd} 個交易日"
              + ("(不足,籌碼條件不納入最佳化)" if nd < 120 else ""))
        if nd < 120:
            con = None

    df = build_features(data, con)
    print(f"樣本數:{len(df):,} 個股票日\n")

    # 紀律一:股票切分
    tks = sorted(df["ticker"].unique())
    tr_tk, te_tk = set(tks[::2]), set(tks[1::2])
    # 紀律二:時間切分(訓練只用前 70% 的時間)
    cut = df["date"].quantile(0.7)
    train = df[(df.ticker.isin(tr_tk)) & (df.date <= cut)]
    test = df[(df.ticker.isin(te_tk)) & (df.date > cut)]
    print(f"訓練:{len(tr_tk)} 檔 × 至 {pd.Timestamp(cut).date()} = {len(train):,} 筆")
    print(f"測試:{len(te_tk)} 檔 × 之後              = {len(test):,} 筆\n")
    if len(test) < MIN_TEST_SAMPLES:
        print("測試集樣本不足,無法可靠驗證。請先累積更多資料。")
        return

    orig = {"年線上揚": 14, "均線多頭": 7, "RS高": 11,
            "RSI回檔": 10, "KD/MACD轉強": 8, "量能放大": 0}
    equal = {f: 10 for f in FEATURES}
    tuned = optimize(train, test, orig)

    report(train, test, {"原始(手設)": orig, "等權重": equal, "最佳化後": tuned})


if __name__ == "__main__":
    main()
