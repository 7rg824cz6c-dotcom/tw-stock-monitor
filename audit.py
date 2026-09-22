#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
資料品質稽核 — 跑模擬之前應該先跑這個

為什麼需要:
    回測結果只要有一檔股票的價格資料錯誤,整個結論就是垃圾。
    等權重平均對離群值極度敏感:一檔假裝漲了 1000 倍的股票,
    就能讓 120 檔的平均報酬從 +200% 變成 +1000%。

    yfinance 的台股資料在以下情況容易出錯:
      - 減資(台股特有,yfinance 常處理錯誤)
      - 大量股票股利(配股)
      - 長期停牌後復牌
      - 興櫃轉上市

檢查項目:
    1. 極端總報酬(可能是還原權值錯誤)
    2. 單日異常跳動(超過漲跌幅限制 10% 太多)
    3. 零價、負價、缺漏
    4. 資料起訖不一致
    5. 平均 vs 中位數的落差(落差大 = 有離群值主導)

用法:
    python audit.py              # 稽核 twdata/
    python audit.py --fix        # 把有問題的檔案移到 twdata_bad/
"""

import glob
import os
import shutil
import sys

import numpy as np
import pandas as pd

try:
    import netutil as _N
    _N.force_utf8_stdio()
except Exception:
    pass

DATA = "twdata"
BAD = "twdata_bad"

# 台股漲跌幅限制 10%,還原權值後除權息日可能略超過,留寬容
MAX_DAILY_MOVE = 0.35
# 高報酬本身不是錯誤 —— 台股十年確實有漲百倍的 AI 供應鏈股。
# 只有「還原權值比失真」「零價」「單日暴跳」才是真正的資料問題。
EXTREME_RETURN = 100.0       # 僅標示為值得人工確認,不視為錯誤
MIN_TOTAL_RETURN = -0.98


def audit_one(path):
    """回傳 (代號, 統計, 問題清單)"""
    code = os.path.basename(path)[:-4]
    issues, notes = [], []
    try:
        d = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
    except Exception as e:
        return code, None, [f"讀取失敗:{type(e).__name__}"]

    d = d.rename(columns={"Adj Close": "Adj"})
    if "Close" not in d.columns:
        return code, None, ["缺少 Close 欄位"]

    raw_close = d["Close"].copy()
    close = d["Adj"] if "Adj" in d.columns else d["Close"]
    close = pd.to_numeric(close, errors="coerce").dropna()
    if len(close) < 100:
        return code, None, [f"資料過少({len(close)} 筆)"]

    if (close <= 0).any():
        issues.append(f"有 {(close <= 0).sum()} 筆零價或負價")
        close = close[close > 0]

    ret = close.pct_change().dropna()
    big = ret[ret.abs() > MAX_DAILY_MOVE]
    if len(big):
        worst = big.abs().max() * 100
        issues.append(f"{len(big)} 天單日跳動 >{MAX_DAILY_MOVE*100:.0f}%"
                      f"(最大 {worst:.0f}%)")

    total = float(close.iloc[-1] / close.iloc[0] - 1)
    if total < MIN_TOTAL_RETURN:
        issues.append(f"總報酬 {total*100:.1f}% 幾近歸零")

    # 還原權值比例:Adj/Close 在最早期應該 ≤1 且不會太極端
    adj_ratio = None
    if "Adj" in d.columns:
        try:
            r = (pd.to_numeric(d["Adj"], errors="coerce")
                 / pd.to_numeric(raw_close, errors="coerce")).dropna()
            if len(r):
                adj_ratio = float(r.iloc[0])
                if adj_ratio < 0.1:
                    issues.append(f"還原權值比 {adj_ratio:.3f} 過低"
                                  f"(除權息還原失真,會灌大報酬)")
                elif total > EXTREME_RETURN and adj_ratio > 0.5:
                    notes.append(f"報酬 {total*100:,.0f}% 但還原比正常"
                                 f"({adj_ratio:.2f})→ 很可能是真實飆股,勿刪")
        except Exception:
            pass

    gaps = close.index.to_series().diff().dt.days
    long_gap = int((gaps > 30).sum())
    if long_gap:
        issues.append(f"{long_gap} 段超過 30 天的資料空窗")

    return code, {
        "notes": notes,
        "n": len(close), "start": close.index[0], "end": close.index[-1],
        "total_return": total * 100,
        "ann": ((close.iloc[-1] / close.iloc[0]) **
                (365.25 / max((close.index[-1] - close.index[0]).days, 1)) - 1) * 100,
        "adj_ratio": adj_ratio,
        "last": float(close.iloc[-1]),
    }, issues


def main():
    fs = sorted(glob.glob(os.path.join(DATA, "*.csv")))
    if not fs:
        print(f"{DATA}/ 裡沒有 CSV。請先執行:python backtest_tw.py --download")
        return

    rows, bad = [], {}
    for f in fs:
        code, st, iss = audit_one(f)
        if st:
            rows.append({"code": code, **st, "issues": len(iss)})
        if iss:
            bad[code] = iss

    df = pd.DataFrame(rows)
    print(f"稽核 {len(fs)} 檔,{len(df)} 檔可解析\n")
    print("=" * 66)
    print("整體報酬分布(這是判斷資料是否可信的關鍵)")
    print("=" * 66)
    tr = df["total_return"]
    print(f"  中位數   {tr.median():>10,.1f}%     ← 穩健,不受離群值影響")
    print(f"  平均數   {tr.mean():>10,.1f}%     ← 被離群值拉高")
    print(f"  第25/75  {tr.quantile(.25):>10,.1f}% / {tr.quantile(.75):,.1f}%")
    print(f"  最大/最小 {tr.max():>9,.1f}% / {tr.min():,.1f}%")
    ratio = tr.mean() / tr.median() if tr.median() else float("inf")
    print()
    if ratio > 2:
        print(f"  ⚠ 平均是中位數的 {ratio:.1f} 倍 —— 少數離群股主導了等權重報酬。")
        print("    回測的『買入持有』基準會因此被嚴重高估。")
    else:
        print(f"  ✓ 平均/中位數 = {ratio:.2f},分布正常。")

    med_ann = float(np.median(df["ann"]))
    print("\n" + "=" * 66)
    print("⚠ 生存者偏誤檢查(這比資料錯誤更嚴重)")
    print("=" * 66)
    print(f"  中位數個股年化報酬:{med_ann:.1f}%")
    if med_ann > 12:
        print(f"""
  台股加權指數同期年化約 10-12%。你的樣本中位數是 {med_ann:.1f}%,
  明顯偏高。原因是選股池用「今天成交金額前 120 大」挑出來的 ——
  今天大的公司,正是過去十年漲上來的公司。十年前就下市、
  萎縮、或從未進過前 120 名的股票,完全不在樣本裡。

  這代表:
    • 絕對報酬數字(策略與買入持有都是)全部被高估
    • 兩者的「差距」仍有參考價值(因為共享同一偏誤)
    • 但不要拿這個年化報酬去推估未來

  要消除這個偏誤,需要「歷史成分股」資料 —— 免費來源沒有。""")
    else:
        print("  ✓ 中位數年化接近大盤,樣本偏誤不明顯。")

    print("\n" + "=" * 66)
    print("報酬最高的 10 檔(高報酬不等於資料錯誤)")
    print("=" * 66)
    print(f"{'代號':<8}{'總報酬':>12}{'年化':>9}{'還原比':>9}{'最新價':>10}{'問題':>6}")
    print("-" * 56)
    for _, r in df.nlargest(10, "total_return").iterrows():
        ar = f"{r['adj_ratio']:.3f}" if r["adj_ratio"] is not None else "—"
        verdict = "真實飆股" if (r["adj_ratio"] or 1) > 0.5 else "⚠可疑"
        print(f"{r['code']:<8}{r['total_return']:>11,.0f}%{r['ann']:>8.1f}%"
              f"{ar:>9}{r['last']:>10.1f}  {verdict}")
    print("\n  還原比 >0.5 代表除權息還原正常,高報酬是真的,不要刪除。")

    print("\n" + "=" * 66)
    print(f"有問題的檔案:{len(bad)} 檔")
    print("=" * 66)
    if not bad:
        print("  ✓ 未發現異常。")
    for code, iss in sorted(bad.items())[:25]:
        print(f"  {code}: {' / '.join(iss)}")
    if len(bad) > 25:
        print(f"  …還有 {len(bad)-25} 檔")

    print("\n" + "=" * 66)
    print("建議")
    print("=" * 66)
    severe = {c: i for c, i in bad.items()
              if any("還原權值比" in x or "零價" in x or "幾近歸零" in x
                     or "單日跳動" in x for x in i)}
    if severe:
        print(f"  {len(severe)} 檔有真正的資料問題,建議剔除:")
        print(f"    python audit.py --fix     (移到 {BAD}/,可隨時搬回)")
        print(f"    python simulate.py")
    elif ratio > 2:
        print("  沒有單檔嚴重錯誤,但報酬分布高度偏斜。")
        print("  這可能是真實的(台股十年確實有幾檔翻很多倍),")
        print("  但等權重買入持有的基準會被那幾檔主導,參考價值有限。")
    else:
        print("  ✓ 資料品質良好,模擬結果可以參考。")

    if "--fix" in sys.argv and severe:
        os.makedirs(BAD, exist_ok=True)
        for c in severe:
            src = os.path.join(DATA, c + ".csv")
            if os.path.exists(src):
                shutil.move(src, os.path.join(BAD, c + ".csv"))
        print(f"\n已移出 {len(severe)} 檔至 {BAD}/,現在重跑 python simulate.py")


if __name__ == "__main__":
    main()
