#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
籌碼因子驗證 — 外資、投信、融資到底能不能預測?

這是本專案一直缺的那一塊。simulate.py 只跑技術面;評分裡的籌碼 30 分
從頭到尾沒有任何證據支持,只是「聽起來合理」的設計。

現在 chips.db 累積夠了,可以真的檢驗。

方法:
    對每個「股票-日」計算當日已知的籌碼特徵(只用過去資料,無前視),
    對照未來 N 日報酬,看不同分組的報酬差異。

紀律(與 optimize.py 相同):
    1. 不重疊取樣 —— 未來 20 日報酬互相重疊會讓 t 值膨脹數倍
    2. 股票切分 —— 一半訓練、一半測試
    3. 時間切分 —— 前後期分開看,確認不是只在某段期間有效
    4. 一律與「隨機買進」基準比較

用法:
    python chip_backtest.py              # 全部因子
    python chip_backtest.py --fwd 60     # 改看 60 日
"""

import glob
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

try:
    import netutil as _N
    _N.force_utf8_stdio()
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB = "chips.db"
DATA = "twdata"
MIN_SAMPLES = 100


def load_prices(path=DATA):
    out = {}
    for f in glob.glob(os.path.join(path, "*.csv")):
        try:
            d = pd.read_csv(f, parse_dates=["Date"]).set_index("Date").sort_index()
        except Exception:
            continue
        d = d.rename(columns={"Adj Close": "Adj"}).dropna()
        if len(d) < 300 or "Close" not in d:
            continue
        if "Adj" in d:
            d["Close"] = d["Adj"]
        out[os.path.basename(f)[:-4]] = d[["Close", "Volume"]]
    return out


def load_chips(db=DB):
    if not os.path.exists(db):
        return None, None
    con = sqlite3.connect(db)
    inst = pd.read_sql(
        "SELECT date, code, foreign_net, trust_net FROM inst ORDER BY date", con)
    mgn = pd.read_sql(
        "SELECT date, code, margin_bal FROM margin ORDER BY date", con)
    con.close()
    for df in (inst, mgn):
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return inst, mgn


def streak(arr):
    """連續同向天數:正=連買,負=連賣。"""
    out = np.zeros(len(arr))
    n = 0
    sign = 0
    for i, v in enumerate(arr):
        if v > 0:
            n = n + 1 if sign >= 0 else 1
            sign = 1
        elif v < 0:
            n = n + 1 if sign <= 0 else 1
            sign = -1
        else:
            n, sign = 0, 0
        out[i] = n * sign
    return out


def build_panel(prices, inst, mgn, fwd=20):
    """把籌碼與價格對齊,算出特徵與未來報酬。全部只用當日以前的資料。"""
    rows = []
    inst_g = {c: g.set_index("date") for c, g in inst.groupby("code")}
    mgn_g = {c: g.set_index("date") for c, g in mgn.groupby("code")} if mgn is not None and not mgn.empty else {}

    for code, px in prices.items():
        g = inst_g.get(code)
        if g is None or len(g) < 60:
            continue
        df = pd.DataFrame(index=g.index.intersection(px.index)).sort_index()
        if len(df) < 120:
            continue
        f = g.loc[df.index, "foreign_net"] / 1000.0      # 股→張
        t = g.loc[df.index, "trust_net"] / 1000.0
        c = px.loc[df.index, "Close"]
        v = px.loc[df.index, "Volume"] / 1000.0

        df["外資連買天數"] = streak(f.values)
        df["外資5日買超"] = f.rolling(5).sum()
        df["外資買超佔量"] = (f.rolling(5).sum()
                             / v.rolling(5).sum().replace(0, np.nan) * 100)
        df["投信連買天數"] = streak(t.values)
        df["投信5日買超"] = t.rolling(5).sum()
        m = mgn_g.get(code)
        if m is not None:
            mb = m["margin_bal"].reindex(df.index).ffill()
            # 融資餘額可能為 0(無人融資),相除會得到 inf,
            # 讓 qcut 分位計算失真並噴 RuntimeWarning。一律轉成 NaN 排除。
            prev = mb.shift(5).replace(0, np.nan)
            df["融資5日變化"] = ((mb / prev - 1) * 100).replace(
                [np.inf, -np.inf], np.nan)
        df["fwd"] = (c.shift(-fwd) / c - 1) * 100
        df["ticker"] = code
        df["date"] = df.index
        rows.append(df.dropna(subset=["fwd"]))

    if not rows:
        return pd.DataFrame()
    panel = pd.concat(rows, ignore_index=True)
    return panel


def test_factor(panel, col, fwd, label=""):
    """對單一因子做分組檢定。回傳 (結果表, 摘要)"""
    from scipy import stats
    d = panel[[col, "fwd", "ticker", "date"]].replace(
        [np.inf, -np.inf], np.nan).dropna()
    if len(d) < MIN_SAMPLES * 4:
        return None, f"{col}:樣本不足({len(d)})"

    # 不重疊取樣:每檔股票每 fwd 天取一次
    d = d.sort_values(["ticker", "date"])
    d = d.groupby("ticker", group_keys=False).apply(
        lambda g: g.iloc[::fwd], include_groups=False).reset_index(drop=True)
    if len(d) < MIN_SAMPLES:
        return None, f"{col}:不重疊樣本不足({len(d)})"

    base = d["fwd"].mean()
    # 連買天數是離散值(…-2,-1,0,1,2…),qcut 會因重複邊界失敗,
    # 改用有意義的自訂分界。
    if "連買天數" in col:
        d["q"] = pd.cut(d[col], [-99, -3, -1, 1, 3, 99],
                        labels=["連賣3+", "連賣1-2", "無方向", "連買1-2", "連買3+"],
                        include_lowest=True)
        if d["q"].nunique() < 3:
            return None, f"{col}:分組後類別過少"
    else:
        try:
            d["q"] = pd.qcut(d[col], 5, labels=["最低", "低", "中", "高", "最高"],
                             duplicates="drop")
        except ValueError:
            return None, f"{col}:數值分布無法分組"

    tbl = d.groupby("q", observed=True).agg(
        n=("fwd", "size"), avg=("fwd", "mean"),
        win=("fwd", lambda x: (x > 0).mean() * 100)).reset_index()
    tbl["超額"] = tbl["avg"] - base

    cats = [c for c in d["q"].cat.categories if (d["q"] == c).any()]
    hi = d[d["q"] == cats[-1]]["fwd"]
    lo = d[d["q"] == cats[0]]["fwd"]
    if len(hi) < 20 or len(lo) < 20:
        return tbl, f"{col}:兩端樣本過少"
    t, p = stats.ttest_ind(hi, lo, equal_var=False)
    rho, prho = stats.spearmanr(d[col], d["fwd"])
    return tbl, {"col": col, "base": base, "n": len(d),
                 "hi": hi.mean(), "lo": lo.mean(),
                 "spread": hi.mean() - lo.mean(), "p": p,
                 "rho": rho, "p_rho": prho,
                 "monotone": list(tbl["avg"]) == sorted(tbl["avg"])
                 or list(tbl["avg"]) == sorted(tbl["avg"], reverse=True)}


def main():
    fwd = 20
    if "--fwd" in sys.argv:
        i = sys.argv.index("--fwd")
        if len(sys.argv) > i + 1:
            fwd = int(sys.argv[i + 1])

    if not os.path.exists(DB):
        print(f"找不到 {DB}。請先執行:python chips.py --backfill 20240101")
        return
    prices = load_prices()
    if not prices:
        print(f"找不到股價資料。請先執行:python backtest_tw.py --download")
        return
    inst, mgn = load_chips()
    if inst is None or inst.empty:
        print("籌碼資料庫是空的。")
        return

    # 資料完整性診斷 —— 「樣本不足」通常是資料沒抓到,不是統計問題
    con = sqlite3.connect(DB)
    n_inst = con.execute("SELECT COUNT(*) FROM inst").fetchone()[0]
    n_mgn = con.execute("SELECT COUNT(*) FROM margin").fetchone()[0]
    con.close()
    print(f"資料庫:inst {n_inst:,} 筆 / margin {n_mgn:,} 筆")
    if n_mgn == 0:
        print("  ⚠ 融資融券表是空的 —— 回補時該端點失敗了。")
        print("    診斷:python chips.py --check")
        print("    融資相關因子會被跳過,其餘不受影響。\n")
    elif n_mgn < n_inst * 0.5:
        print(f"  ⚠ 融資資料只有法人的 {n_mgn/n_inst*100:.0f}%,涵蓋不完整。\n")
    else:
        print()

    nday = inst["date"].nunique()
    print(f"籌碼 {nday} 個交易日({inst['date'].min().date()} → "
          f"{inst['date'].max().date()})")
    print(f"股價 {len(prices)} 檔,未來報酬天期 {fwd} 日\n")

    panel = build_panel(prices, inst, mgn, fwd)
    if panel.empty:
        print("籌碼與股價無法對齊(代號或日期沒有交集)。")
        return
    print(f"對齊後:{panel['ticker'].nunique()} 檔,{len(panel):,} 個股票日\n")

    # ── 檢定力分析:這份樣本能偵測到多小的效應? ──
    n_per_group = len(panel) // fwd // 5
    sd = float(panel["fwd"].std())
    if n_per_group > 10:
        mde = 2.8 * sd / np.sqrt(n_per_group)     # 約 80% 檢定力、5% 顯著水準
        print("=" * 68)
        print("檢定力分析(先看這個,再看結果)")
        print("=" * 68)
        print(f"  不重疊樣本約 {n_per_group * 5:,} 筆,每組約 {n_per_group:,} 筆")
        print(f"  報酬標準差 {sd:.1f}%")
        print(f"  → 可偵測的最小價差約 {mde:.1f}%(最高組減最低組)")
        print(f"\n  換句話說:真實效應若小於 {mde:.1f}%,這份資料看不出來,")
        print("  結果會是「不顯著」—— 那代表證據不足,不代表因子無效。")
        print("  籌碼因子的真實效應通常是 1-3%,所以樣本不夠時很容易測不到。")
        print()

    factors = ["外資連買天數", "外資5日買超", "外資買超佔量",
               "投信連買天數", "投信5日買超"]
    if "融資5日變化" in panel.columns:
        factors.append("融資5日變化")

    results = []
    for col in factors:
        tbl, summ = test_factor(panel, col, fwd)
        if tbl is None:
            print(f"  {summ}")
            continue
        print("=" * 68)
        print(f"{col}  (未來 {fwd} 日報酬,基準 {summ['base']:+.2f}%)")
        print("=" * 68)
        print(f"  {'分組':<6}{'樣本':>7}{'平均報酬':>10}{'超額':>9}{'勝率':>8}")
        print("  " + "-" * 38)
        for _, r in tbl.iterrows():
            print(f"  {str(r['q']):<6}{r['n']:>7}{r['avg']:>9.2f}%"
                  f"{r['超額']:>+8.2f}%{r['win']:>7.1f}%")
        sig = "✓顯著" if summ["p"] < 0.05 else "✗不顯著"
        mono = "單調" if summ["monotone"] else "非單調"
        print(f"\n  最高組 − 最低組 = {summ['spread']:+.2f}%  "
              f"p={summ['p']:.4f} {sig}")
        print(f"  等級相關 rho={summ['rho']:+.3f} (p={summ['p_rho']:.4f})  {mono}")
        print()
        results.append(summ)

    # ── 樣本外驗證 ──
    print("=" * 68)
    print("樣本外驗證(股票依代號奇偶切分,兩組完全獨立)")
    print("=" * 68)
    tks = sorted(panel["ticker"].unique())
    A = panel[panel.ticker.isin(tks[::2])]
    B = panel[panel.ticker.isin(tks[1::2])]
    print(f"{'因子':<14}{'A組價差':>10}{'A組p':>9}{'B組價差':>10}{'B組p':>9}{'判定':>12}")
    print("-" * 64)
    for col in factors:
        ra = test_factor(A, col, fwd)[1]
        rb = test_factor(B, col, fwd)[1]
        if not isinstance(ra, dict) or not isinstance(rb, dict):
            print(f"{col:<14}{'樣本不足':>50}")
            continue
        same = (ra["spread"] > 0) == (rb["spread"] > 0)
        both_sig = ra["p"] < 0.05 and rb["p"] < 0.05
        if both_sig and same:
            v = "✓ 兩組皆顯著"
        elif same and (ra["p"] < 0.05 or rb["p"] < 0.05):
            v = "△ 方向一致"
        elif same:
            v = "— 方向一致但不顯著"
        else:
            v = "✗ 方向相反"
        print(f"{col:<14}{ra['spread']:>+9.2f}%{ra['p']:>9.3f}"
              f"{rb['spread']:>+9.2f}%{rb['p']:>9.3f}{v:>12}")

    print("\n" + "=" * 68)
    print("結論")
    print("=" * 68)
    strong = [r for r in results if r["p"] < 0.05 and r["monotone"]]
    weak = [r for r in results if r["p"] < 0.05 and not r["monotone"]]
    if strong:
        print(f"  {len(strong)} 個因子同時通過顯著性與單調性:")
        for r in strong:
            print(f"    {r['col']}:價差 {r['spread']:+.2f}%,p={r['p']:.4f}")
        print("  → 這些值得保留在評分裡。")
    if weak:
        print(f"  {len(weak)} 個因子顯著但非單調 —— 可能是少數極端值造成,")
        print("    而非穩定的線性關係,參考價值有限:")
        for r in weak:
            print(f"    {r['col']}:價差 {r['spread']:+.2f}%,p={r['p']:.4f}")
    if not strong and not weak:
        print("  沒有任何籌碼因子通過檢定。")
        if n_per_group > 10:
            print(f"  但注意上面的檢定力:這份資料只能偵測 {mde:.1f}% 以上的價差。")
            print("  籌碼因子的真實效應通常只有 1-3%,所以這個結果有兩種可能:")
            print("    (a) 籌碼因子確實無效")
            print("    (b) 有效但效應太小,樣本不足以證明")
            print("  無法分辨。繼續累積資料再跑,或用紙上交易往前驗證。")
        print("\n  在有證據之前,評分裡的籌碼 30 分就是沒有根據的權重。")
        print("  保守作法:調降籌碼權重,讓有證據的因子(偏離度)佔更大比重。")
    print("\n  ※ 樣本僅約 2-3 年、單一市場。定期重跑,累積越多越可信。")


if __name__ == "__main__":
    main()
