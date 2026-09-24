#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股監控 — 網頁介面

啟動:
    pip install streamlit plotly
    streamlit run app.py

會在瀏覽器開啟 http://localhost:8501

注意:這是給你自己用的本機工具。不要公開部署後對外提供選股建議,
     那會踩到證券投資信託及顧問法第 107 條。
"""

import io
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chips as C
import revenue as REV
from forecast import price_range, structural_levels, confidence, MAX_HORIZON
from tw_stock_monitor import load_config, fetch_history, build_universe, BENCHMARK
from tw_stock_monitor_v2 import analyse_v2, rs_rating, minervini_template, weinstein_stage

st.set_page_config(page_title="台股監控", page_icon="📊", layout="wide")

CACHE = "last_scan.json"


# ============================================================
# 資料
# ============================================================

@st.cache_data(ttl=1800, show_spinner=False)
def run_scan(cfg_json):
    cfg = json.loads(cfg_json)
    con = C.init_db(); REV.init_db()
    try:
        C.update_today(con)
    except Exception:
        pass
    try:
        REV.update(con)
    except Exception:
        pass

    tickers, fdf = build_universe(cfg)
    fund = {}
    if not fdf.empty:
        for t, row in fdf.iterrows():
            fund[t] = {"name": row["name"], "pe": row["pe"], "yield": row["yield"]}

    hist = fetch_history(list(dict.fromkeys(tickers + [BENCHMARK])), cfg["history_days"])
    rs_map = rs_rating(hist, tickers)
    codes = [t.split(".")[0] for t in tickers]
    chip = C.chip_features(con, codes)
    rev = REV.revenue_features(con, set(codes))
    bret = 0.0
    if BENCHMARK in hist and len(hist[BENCHMARK]) > 120:
        bc = hist[BENCHMARK]["Close"]
        bret = float(bc.iloc[-1]) / float(bc.iloc[-120]) - 1

    res = []
    for t in tickers:
        if t not in hist:
            continue
        try:
            r = analyse_v2(t, hist[t], bret, fund.get(t, {}), rs_map.get(t, 50),
                           chip.get(t.split(".")[0]), rev.get(t.split(".")[0]))
            if r:
                res.append(r)
        except Exception:
            continue
    res.sort(key=lambda x: x["score"], reverse=True)
    nday = con.execute("SELECT COUNT(DISTINCT date) FROM inst").fetchone()[0]
    nrev = con.execute("SELECT COUNT(DISTINCT ym) FROM revenue").fetchone()[0]
    px = {t: hist[t][["Open", "High", "Low", "Close", "Volume"]].to_json(date_format="iso")
          for t in hist if t in {r["ticker"] for r in res}}
    return {"results": res, "prices": px, "chip_days": nday, "rev_months": nrev,
            "ts": datetime.now().isoformat(timespec="seconds")}


def load_prices(blob, ticker):
    if ticker not in blob:
        return None
    # 新版 pandas 會把字面 JSON 字串當成檔案路徑,必須包成 StringIO
    d = pd.read_json(io.StringIO(blob[ticker]))
    d.index = pd.to_datetime(d.index)
    return d.sort_index()


# ============================================================
# 介面
# ============================================================

st.title("📊 台股綜合監控")
st.caption("量化篩選工具,僅供個人研究。不構成投資建議。")

cfg = load_config("config.json")

with st.sidebar:
    st.header("設定")
    cfg["score_threshold"] = st.slider("分數門檻", 0, 100, cfg["score_threshold"], 5)
    cfg["universe"]["max_candidates"] = st.slider(
        "掃描檔數", 30, 400, cfg["universe"]["max_candidates"], 10)
    cfg["fundamentals"]["pe_max"] = st.number_input(
        "本益比上限", 5.0, 100.0, float(cfg["fundamentals"]["pe_max"]))
    min_cov = st.slider("最低資料覆蓋率 %", 0, 100, 0, 5,
                        help="覆蓋率低的分數是從較少證據推出來的,可靠度較低")
    st.divider()
    go = st.button("🔄 執行掃描", type="primary", width="stretch")
    st.caption("掃描約需 2-4 分鐘,結果快取 30 分鐘")

if go:
    st.cache_data.clear()

with st.spinner("下載資料並計算中…"):
    try:
        data = run_scan(json.dumps(cfg))
    except Exception as e:
        st.error(f"掃描失敗:{e}")
        st.info("請先確認:`python chips.py --check` 與 `python revenue.py --check`")
        st.stop()

res = [r for r in data["results"] if r["coverage"] >= min_cov]
if not res:
    st.warning("沒有符合條件的結果。試著降低覆蓋率要求或分數門檻。")
    st.stop()

# ---- 產業目錄:依公司類別篩選/搜尋 ----
all_industries = sorted({r.get("industry") or "未分類" for r in res})
picked_industries = st.sidebar.multiselect(
    "產業目錄", all_industries, default=all_industries,
    help="依公司類別篩選,取消勾選可縮小選取/搜尋範圍")
res = [r for r in res if (r.get("industry") or "未分類") in picked_industries]
if not res:
    st.warning("篩選後沒有符合條件的股票,試著勾選更多產業。")
    st.stop()

# ---- 資料健康度 ----
c1, c2, c3, c4 = st.columns(4)
c1.metric("掃描檔數", len(data["results"]))
c2.metric("達門檻", sum(r["score"] >= cfg["score_threshold"] for r in res))
c3.metric("籌碼資料", f"{data['chip_days']} 日",
          delta=None if data["chip_days"] >= 20 else "不足",
          delta_color="off" if data["chip_days"] >= 20 else "inverse")
c4.metric("營收資料", f"{data['rev_months']} 月",
          delta=None if data["rev_months"] >= 4 else "不足",
          delta_color="off" if data["rev_months"] >= 4 else "inverse")

if data["chip_days"] < 20:
    st.warning("籌碼資料不足 20 日,籌碼分數不可靠。請執行 `python chips.py --backfill 90`")
if data["rev_months"] < 4:
    st.warning("營收資料不足 4 個月,成長趨勢分數會被略過(不計入分母)。每月執行會自動累積。")

tab1, tab2 = st.tabs(["📋 掃描結果", "🔍 個股分析"])

with tab1:
    df = pd.DataFrame([{
        "代號": r["ticker"], "名稱": r["name"],
        "產業": r.get("industry") or "未分類", "分數": r["score"],
        "覆蓋%": r["coverage"], "收盤": r["price"],
        "停損距%": round((r["stop_loss"] / r["price"] - 1) * 100, 1)
                   if r.get("price") else None,
        "RS": r["rs"],
        "階段": r["stage"].split("(")[0],
        "乖離分位": (r.get("dev") or {}).get("pct60"),
        "外資連買": r["foreign_streak"],
        "營收年增%": r["rev_yoy"],
    } for r in res])
    # 用 Streamlit 原生欄位格式,不依賴 matplotlib
    st.caption("點一列可在「🔍 個股分析」分頁直接看到該檔的細節")
    event = st.dataframe(
        df, width="stretch", height=520, hide_index=True,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "分數": st.column_config.ProgressColumn(
                "分數", min_value=0, max_value=100, format="%d"),
            "覆蓋%": st.column_config.NumberColumn(format="%d%%"),
            "收盤": st.column_config.NumberColumn(format="%.2f"),
            "停損距%": st.column_config.NumberColumn(
                format="%+.1f%%", help="停損參考價距現價的百分比(2×ATR)"),
            "乖離分位": st.column_config.NumberColumn(
                format="%d", help="≥90 過度延伸,≤25 未延伸。本專案證據最強的因子"),
            "營收年增%": st.column_config.NumberColumn(format="%+.1f%%"),
        })
    if event.selection.rows:
        st.session_state["picked_ticker"] = res[event.selection.rows[0]]["ticker"]
    st.download_button("下載 CSV", df.to_csv(index=False).encode("utf-8-sig"),
                       f"scan_{datetime.now():%Y%m%d}.csv", "text/csv")

with tab2:
    # 依產業分組排序,選單標籤把公司類別標在名稱後面,可直接輸入搜尋
    ordered = sorted(res, key=lambda x: (x.get("industry") or "未分類", -x["score"]))
    tickers_in_order = [r["ticker"] for r in ordered]
    labels = [f"{r['ticker']} {r['name']}({r.get('industry') or '未分類'})  {r['score']}分"
              for r in ordered]
    default_idx = 0
    picked_ticker = st.session_state.get("picked_ticker")
    if picked_ticker in tickers_in_order:
        default_idx = tickers_in_order.index(picked_ticker)
    pick = st.selectbox("選擇個股(依產業分組,可輸入代號/名稱搜尋)",
                        labels, index=default_idx)
    r = ordered[labels.index(pick)]
    d = load_prices(data["prices"], r["ticker"])

    a, b, c = st.columns([1, 1, 2])
    a.metric("分數", r["score"], f"覆蓋 {r['coverage']}%")
    b.metric("收盤", r["price"], f"RS {r['rs']}")
    c.metric("Weinstein 階段", r["stage"])

    st.subheader("評分明細")
    x, y = st.columns(2)
    with x:
        st.markdown("**✔ 加分項**")
        for h in r["hits"]:
            st.markdown(f"- {h}")
    with y:
        st.markdown("**⚠ 扣分項**")
        if r["risks"]:
            for k in r["risks"]:
                st.markdown(f"- {k}")
        else:
            st.caption("無")
    if r["missing"]:
        st.info(f"缺少 {r['missing']} 資料,該部分未計入分母。")

    if d is not None and len(d) > 300:
        st.subheader("價格機率區間")
        hz = st.radio("天期", [20, 60], horizontal=True,
                      format_func=lambda h: f"{h} 個交易日(約 {h // 20} 個月)")
        pr = price_range(d["Close"], hz)
        cf, notes = confidence(d["Close"], hz)
        lv = structural_levels(d["High"], d["Low"], d["Close"])

        if pr:
            m1, m2, m3 = st.columns(3)
            m1.metric("50% 機率區間", f"{pr['p50'][0]} ~ {pr['p50'][1]}")
            m2.metric("70% 機率區間", f"{pr['p70'][0]} ~ {pr['p70'][1]}")
            m3.metric("90% 機率區間", f"{pr['p90'][0]} ~ {pr['p90'][1]}")

            n1, n2 = st.columns(2)
            n1.metric("區間可信度", f"{cf}/100")
            n2.metric("年化波動率", f"{pr['ann_vol']}%",
                      f"當前為常態的 {pr['vol_ratio']}倍")
            if notes:
                
                st.caption("⚠ " + " / ".join(notes))

            try:
                import plotly.graph_objects as go
                recent = d.tail(120)
                fig = go.Figure()
                fig.add_trace(go.Candlestick(
                    x=recent.index, open=recent["Open"], high=recent["High"],
                    low=recent["Low"], close=recent["Close"], name="股價"))
                fut = pd.bdate_range(d.index[-1], periods=hz + 1)[1:]
                for lvl, col in (("p90", "rgba(200,200,200,.20)"),
                                 ("p70", "rgba(120,170,255,.25)"),
                                 ("p50", "rgba(80,140,255,.35)")):
                    lo, hi = pr[lvl]
                    fig.add_trace(go.Scatter(
                        x=list(fut) + list(fut[::-1]),
                        y=[hi] * len(fut) + [lo] * len(fut),
                        fill="toself", fillcolor=col, line=dict(width=0),
                        name=f"{lvl[1:]}% 區間", hoverinfo="skip"))
                for p, n in lv["resistance"]:
                    fig.add_hline(y=p, line=dict(color="tomato", dash="dot", width=1),
                                  annotation_text=f"壓力 {p} (測試{n}次)")
                for p, n in lv["support"]:
                    fig.add_hline(y=p, line=dict(color="seagreen", dash="dot", width=1),
                                  annotation_text=f"支撐 {p} (測試{n}次)")
                fig.add_hline(y=r["stop_loss"], line=dict(color="orange", width=1.5),
                              annotation_text=f"停損參考 {r['stop_loss']}")
                fig.update_layout(height=520, xaxis_rangeslider_visible=False,
                                  margin=dict(t=20, b=20), hovermode="x unified")
                st.plotly_chart(fig, width="stretch")
            except ImportError:
                st.caption("安裝 plotly 可看圖表:pip install plotly")

            st.caption(
                f"區間由過去 2 年 {hz} 日報酬分配、依當前波動率縮放並套用經驗校準係數"
                f" ×{pr['inflation']} 得出。這是**機率測量,不是預測**——"
                f"沒有任何一個價位是「目標價」。實測校準:70% 區間實際命中約 70%。")
        else:
            st.info("歷史資料不足,無法估計區間。")

    with st.expander("Minervini 趨勢模板明細"):
        if d is not None:
            n, ok, ck = minervini_template(d["Close"], r["rs"])
            st.markdown(f"**{n}/8** {'✅ 全部通過' if ok else ''}")
            for kk, vv in ck.items():
                st.markdown(f"{'✅' if vv else '❌'} {kk}")
            st.caption("此模板僅供參考,不計入分數——它與其他趨勢條件高度重疊。")

st.divider()
st.caption(f"最後掃描 {data['ts']} · 資料來源:證交所 OpenAPI"
           "(政府資料開放授權條款)、Yahoo Finance(僅限個人使用)")
