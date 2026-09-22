#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
評分邏輯回測 — 全部指標皆為回看式(backward-looking),無前視偏差。
問題:分數高的股票,未來報酬真的比較好嗎?
"""
import glob, os, sys
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tw_stock_monitor import rsi, kd, macd, atr   # 直接沿用主程式的指標函式

DATA = "twdata"   # 台股 CSV 存放處,執行 --download 會自動建立
FWD = [20, 60]        # 未來 20 / 60 個交易日


# 備援清單:證交所 API 無法連線時使用(台股主要權值股與熱門股)
FALLBACK_CODES = [
    "2330", "2317", "2454", "2308", "2382", "2412", "2881", "2882", "2891",
    "3711", "2303", "1301", "1303", "2002", "1216", "2886", "2884", "2885",
    "2892", "5880", "2357", "2379", "3034", "2409", "3008", "2207", "2301",
    "2327", "2345", "2395", "2408", "2474", "3231", "3037", "4938", "6505",
    "2603", "2609", "2615", "1101", "1102", "2105", "9910", "1326", "2201",
    "2227", "6415", "6669", "8046", "3045", "4904", "3481", "2618", "2610",
    "1402", "9904", "2801", "2809", "2812", "2823", "2834", "2880", "2887",
]


def download_tw(n=120):
    """
    從 yfinance 下載台股歷史資料存成 CSV(僅供個人研究使用)。
    可安全中斷並重跑:已存在且資料足夠的檔案會自動略過。
    """
    try:
        import yfinance as yf
    except ImportError:
        print("✗ 未安裝 yfinance。請執行:pip install yfinance")
        return
    import time
    import netutil as N

    os.makedirs(DATA, exist_ok=True)

    # ── 取得選股清單 ──
    codes = []
    try:
        rows = N.get_json(
            "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
            timeout=30)
        cand = []
        for r in rows:
            code = str(r.get("Code", "")).strip()
            if code.isdigit() and len(code) == 4:
                try:
                    cand.append((float(str(r.get("TradeValue", 0)).replace(",", "")), code))
                except ValueError:
                    pass
        cand.sort(reverse=True)
        codes = [c for _, c in cand[:n]]
        print(f"從證交所取得成交金額前 {len(codes)} 大個股")
    except Exception as e:
        print(f"⚠ 證交所 API 無法連線({type(e).__name__}: "
              f"{str(getattr(e, 'reason', e))[:70]})")
        print("  改用內建備援清單。若要診斷連線問題,執行:python netutil.py")
        codes = FALLBACK_CODES[:n]

    if not codes:
        print("✗ 沒有可下載的股票代號。")
        return

    # ── 略過已下載的 ──
    todo = []
    for c in codes:
        f = os.path.join(DATA, c + ".csv")
        if os.path.exists(f) and os.path.getsize(f) > 40000:
            continue
        todo.append(c)
    skipped = len(codes) - len(todo)
    if skipped:
        print(f"略過 {skipped} 檔已下載的資料")
    if not todo:
        print("全部已下載完成。")
        verify_data()
        return

    print(f"開始下載 {len(todo)} 檔的 10 年資料(可中斷,重跑會接續)…")
    ok = fail = 0
    for i in range(0, len(todo), 20):
        batch = [c + ".TW" for c in todo[i:i + 20]]
        try:
            d = yf.download(batch, period="10y", group_by="ticker",
                            auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            print(f"  批次 {i // 20 + 1} 下載失敗:{type(e).__name__}: {e}")
            fail += len(batch)
            time.sleep(5)
            continue
        if d is None or len(d) == 0:
            print(f"  批次 {i // 20 + 1} 回傳空資料(可能是速率限制,稍候重試)")
            fail += len(batch)
            time.sleep(10)
            continue
        for t in batch:
            try:
                x = d[t] if isinstance(d.columns, pd.MultiIndex) else d
                x = x.dropna()
                if len(x) > 600:
                    x.to_csv(os.path.join(DATA, t.replace(".TW", "") + ".csv"))
                    ok += 1
                else:
                    fail += 1
            except (KeyError, TypeError):
                fail += 1
        print(f"  進度 {min(i + 20, len(todo))}/{len(todo)}  成功 {ok} 失敗 {fail}")
        time.sleep(2)

    print(f"\n下載結束:成功 {ok} 檔,失敗 {fail} 檔,存於 {DATA}/")
    if ok == 0:
        print("✗ 一檔都沒成功。可能原因:網路問題、yfinance 版本過舊、")
        print("  或 Yahoo 端暫時限流。請稍後重試,或 pip install -U yfinance")
    elif fail > ok:
        print("⚠ 失敗多於成功。建議稍後重跑一次(會自動接續未完成的)。")
    verify_data()


def verify_data():
    """檢查已下載的資料是否可用。跑模擬前務必先確認。"""
    import glob
    fs = glob.glob(os.path.join(DATA, "*.csv"))
    if not fs:
        print(f"✗ {DATA}/ 裡沒有任何 CSV。")
        return False
    good = bad = 0
    spans = []
    for f in fs:
        try:
            d = pd.read_csv(f, parse_dates=["Date"])
            if len(d) >= 600 and {"Open", "High", "Low", "Close"} <= set(d.columns):
                good += 1
                spans.append((d["Date"].min(), d["Date"].max()))
            else:
                bad += 1
        except Exception:
            bad += 1
    print(f"\n資料檢查:{good} 檔可用" + (f",{bad} 檔不完整(會被略過)" if bad else ""))
    if spans:
        print(f"  期間 {min(s[0] for s in spans).date()} → "
              f"{max(s[1] for s in spans).date()}")
    if good < 30:
        print(f"  ⚠ 只有 {good} 檔可用,模擬結果的統計意義有限(建議 ≥50 檔)")
    else:
        print("  ✓ 可以執行:python simulate.py")
    return good > 0


def load():
    out = {}
    for f in glob.glob(os.path.join(DATA, "*.csv")):
        d = pd.read_csv(f, parse_dates=["Date"]).set_index("Date").sort_index()
        d = d.rename(columns={"Adj Close": "Adj"}).dropna()
        if len(d) < 600:
            continue
        # 用還原權值價,避免除權息造成假跌破
        adj = d["Adj"] / d["Close"]
        for col in ("Open", "High", "Low"):
            d[col] *= adj
        d["Close"] = d["Adj"]
        out[os.path.basename(f)[:-4]] = d[["Open", "High", "Low", "Close", "Volume"]]
    return out


def benchmark(data):
    """等權重指數,作為動能比較基準。"""
    px = pd.DataFrame({k: v["Close"] for k, v in data.items()}).dropna(how="all")
    norm = px / px.bfill().iloc[0]
    return norm.mean(axis=1)


def score_series(df, bench_ret6m):
    """把 analyse() 的規則向量化,一次算出每一天的分數。"""
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    ma20, ma60, ma200 = (c.rolling(n).mean() for n in (20, 60, 200))
    r = rsi(c); k, d = kd(h, l, c); _, _, hist = macd(c); a = atr(h, l, c)

    ret6m = c / c.shift(120) - 1
    bench = bench_ret6m.reindex(c.index).ffill()
    volr = v.rolling(5).mean() / v.rolling(20).mean()
    yhigh = c.rolling(250).max()
    atrp = a / c * 100

    s = pd.Series(0.0, index=c.index)
    s += 20 * (c > ma200)                                        # 長期趨勢
    s += 12 * (ma20 > ma60)                                      # 中期排列
    s += 15 * (ret6m > bench)                                    # 動能
    s += 15 * r.between(35, 58)                                  # 回檔非追高
    s += 10 * ((k.shift() <= d.shift()) & (k > d) & (k < 60))    # KD 交叉
    s += 10 * ((hist > 0) & (hist.shift() <= 0))                 # MACD 翻正
    s += 8 * (volr > 1.2)                                        # 量能
    # 基本面 +10:此資料集無 PE/殖利率,故本次無法驗證(滿分因此為 90)
    s -= 10 * (c >= yhigh * 0.97)                                # 追高
    s -= 8 * (atrp > 5)                                          # 高波動
    s -= 10 * (c < ma20)                                         # 跌破月線
    return s.clip(0, 100).where(ma200.notna())


def main():
    data = load()
    print(f"載入 {len(data)} 檔,期間 "
          f"{min(v.index[0] for v in data.values()).date()} → "
          f"{max(v.index[-1] for v in data.values()).date()}\n")

    bench = benchmark(data)
    bret6m = bench / bench.shift(120) - 1

    rows = []
    for tk, df in data.items():
        s = score_series(df, bret6m)
        c = df["Close"]
        rec = {"ticker": tk, "score": s}
        for f in FWD:
            rec[f"fwd{f}"] = (c.shift(-f) / c - 1) * 100
        rows.append(pd.DataFrame(rec).dropna(subset=["score"]))

    all_ = pd.concat(rows, ignore_index=True).dropna()
    print(f"總樣本數(股票日):{len(all_):,}\n")

    # === 基準:所有股票日的無條件平均報酬 ===
    print("=" * 62)
    print("基準線(隨機挑一天買進,即買入持有的平均結果)")
    print("=" * 62)
    base = {}
    for f in FWD:
        m, w = all_[f"fwd{f}"].mean(), (all_[f"fwd{f}"] > 0).mean() * 100
        base[f] = (m, w)
        print(f"  未來 {f:2d} 日:平均報酬 {m:+.2f}%   勝率 {w:.1f}%")

    # === 分數分組 ===
    print("\n" + "=" * 62)
    print("依分數分組的未來報酬(對照基準)")
    print("=" * 62)
    bins = [-1, 29, 44, 59, 69, 79, 100]
    labels = ["0-29", "30-44", "45-59", "60-69", "70-79", "80+"]
    all_["bucket"] = pd.cut(all_["score"], bins=bins, labels=labels)

    print(f"{'分數':<8}{'樣本數':>9}{'20日報酬':>10}{'vs基準':>9}"
          f"{'20日勝率':>10}{'60日報酬':>10}{'vs基準':>9}{'60日勝率':>10}")
    print("-" * 78)
    for lb in labels:
        g = all_[all_["bucket"] == lb]
        if len(g) < 100:
            continue
        m20, w20 = g["fwd20"].mean(), (g["fwd20"] > 0).mean() * 100
        m60, w60 = g["fwd60"].mean(), (g["fwd60"] > 0).mean() * 100
        print(f"{lb:<8}{len(g):>9,}{m20:>9.2f}%{m20-base[20][0]:>+8.2f}%"
              f"{w20:>9.1f}%{m60:>9.2f}%{m60-base[60][0]:>+8.2f}%{w60:>9.1f}%")

    # === 門檻檢定 ===
    print("\n" + "=" * 62)
    print("門檻效果與統計顯著性 (Welch t-test)")
    print("=" * 62)
    from scipy import stats
    for th in (55, 60, 65, 70):
        hit = all_[all_["score"] >= th]
        rest = all_[all_["score"] < th]
        if len(hit) < 100:
            continue
        t, p = stats.ttest_ind(hit["fwd60"], rest["fwd60"], equal_var=False)
        pct = len(hit) / len(all_) * 100
        print(f"  ≥{th} 分:觸發 {len(hit):>6,} 次 ({pct:4.1f}% 的日子) | "
              f"60日平均 {hit['fwd60'].mean():+.2f}% vs 其餘 {rest['fwd60'].mean():+.2f}% "
              f"| p={p:.4f} {'✓顯著' if p < 0.05 else '✗不顯著'}")

    # === 單一條件貢獻度 ===
    print("\n" + "=" * 62)
    print("拆解:每個條件單獨的 60 日報酬貢獻")
    print("=" * 62)
    conds = {}
    for tk, df in data.items():
        c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
        ma20, ma60, ma200 = (c.rolling(n).mean() for n in (20, 60, 200))
        r = rsi(c); k, d = kd(h, l, c); _, _, hist = macd(c); a = atr(h, l, c)
        f60 = (c.shift(-60) / c - 1) * 100
        m = {
            "站上年線(200MA)": c > ma200,
            "月線>季線": ma20 > ma60,
            "RSI 35-58": r.between(35, 58),
            "KD低檔交叉": (k.shift() <= d.shift()) & (k > d) & (k < 60),
            "MACD翻正": (hist > 0) & (hist.shift() <= 0),
            "量能放大": v.rolling(5).mean() / v.rolling(20).mean() > 1.2,
            "距年高<3%(扣分項)": c >= c.rolling(250).max() * 0.97,
            "跌破月線(扣分項)": c < ma20,
        }
        for name, mask in m.items():
            ok = mask & ma200.notna() & f60.notna()
            conds.setdefault(name, []).append(pd.DataFrame(
                {"hit": ok, "f60": f60}).dropna())
    for name, lst in conds.items():
        g = pd.concat(lst)
        yes, no = g[g["hit"]]["f60"], g[~g["hit"]]["f60"]
        if len(yes) < 100:
            continue
        print(f"  {name:<22} 成立時 {yes.mean():+6.2f}%  |  不成立 {no.mean():+6.2f}%  "
              f"|  差距 {yes.mean()-no.mean():+6.2f}%")


if __name__ == "__main__":
    if "--download" in sys.argv:
        i = sys.argv.index("--download")
        n = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 120
        download_tw(n)
    elif "--verify" in sys.argv:
        verify_data()
    else:
        main()
