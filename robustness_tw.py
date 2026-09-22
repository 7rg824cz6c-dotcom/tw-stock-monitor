#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第二輪:修正重疊樣本造成的統計膨脹,並檢查年度穩定性。
"""
import glob, os, sys
import numpy as np, pandas as pd
from scipy import stats
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_tw import load, benchmark, score_series, DATA

try:
    import netutil as _N
    _N.force_utf8_stdio()
except Exception:
    pass


def main():
    if not os.path.isdir(DATA) or not glob.glob(os.path.join(DATA, "*.csv")):
        print(f"找不到台股資料。請先執行:python backtest_tw.py --download")
        return

    data = load()
    bench = benchmark(data)
    bret6m = bench / bench.shift(120) - 1

    rows = []
    for tk, df in data.items():
        s = score_series(df, bret6m)
        c = df["Close"]
        rows.append(pd.DataFrame({
            "date": c.index, "ticker": tk, "score": s.values,
            "fwd60": ((c.shift(-60) / c - 1) * 100).values}).dropna())
    all_ = pd.concat(rows, ignore_index=True)

    print("=" * 66)
    print("問題:60 日報酬視窗互相重疊,樣本不獨立,t 檢定會嚴重高估顯著性")
    print("=" * 66)
    n_raw = len(all_)
    n_eff = len(data) * (all_.groupby("ticker").size().mean() / 60)
    print(f"  名目樣本數 {n_raw:,}  →  有效獨立樣本數約 {n_eff:,.0f}")
    print(f"  t 值被高估約 {np.sqrt(n_raw/n_eff):.1f} 倍\n")

    print("改用不重疊樣本(每 60 個交易日只取一次)重做檢定:")
    print("-" * 66)
    ni = all_.groupby("ticker", group_keys=False).apply(
        lambda g: g.iloc[::60], include_groups=False)
    for th in (55, 60, 65):
        hit, rest = ni[ni.score >= th]["fwd60"], ni[ni.score < th]["fwd60"]
        if len(hit) < 20:
            print(f"  ≥{th} 分:僅 {len(hit)} 個獨立樣本,不足以檢定")
            continue
        t, p = stats.ttest_ind(hit, rest, equal_var=False)
        print(f"  ≥{th} 分:{len(hit):>4} 個獨立樣本 | 達標 {hit.mean():+.2f}% "
              f"vs 其餘 {rest.mean():+.2f}% | p={p:.3f} "
              f"{'✓顯著' if p < 0.05 else '✗不顯著'}")

    print("\n" + "=" * 66)
    print("年度穩定性:優勢是持續存在,還是只有某幾年?")
    print("=" * 66)
    all_["year"] = pd.to_datetime(all_["date"]).dt.year
    print(f"{'年份':<8}{'達標次數':>10}{'達標報酬':>11}{'全體報酬':>11}{'超額':>10}")
    print("-" * 52)
    wins = 0; yrs = 0
    for y, g in all_.groupby("year"):
        hit = g[g.score >= 60]["fwd60"]
        if len(hit) < 30:
            continue
        ex = hit.mean() - g["fwd60"].mean()
        yrs += 1; wins += ex > 0
        print(f"{y:<8}{len(hit):>10,}{hit.mean():>10.2f}%{g['fwd60'].mean():>10.2f}%"
              f"{ex:>+9.2f}%")
    print(f"\n  {yrs} 個年度中,有 {wins} 年策略贏過大盤平均 ({wins/yrs*100:.0f}%)")

    print("\n" + "=" * 66)
    print("最重要的檢查:最大回撤與尾部風險")
    print("=" * 66)
    hit = all_[all_.score >= 60]["fwd60"]
    alln = all_["fwd60"]
    for nm, s in (("達標股票", hit), ("全體平均", alln)):
        print(f"  {nm}:最差 {s.min():.1f}% | 第5百分位 {s.quantile(.05):.1f}% | "
              f"中位數 {s.median():+.1f}% | 第95百分位 {s.quantile(.95):.1f}%")
    print(f"\n  達標後仍虧損超過 10% 的機率:{(hit < -10).mean()*100:.1f}%")
    print(f"  達標後仍虧損超過 20% 的機率:{(hit < -20).mean()*100:.1f}%")


if __name__ == "__main__":
    main()
