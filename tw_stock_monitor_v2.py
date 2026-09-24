#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股綜合監控 v2 — 技術面 + 籌碼面 + 交易員模板

v1 的問題:全部分數都押在「趨勢」這一個賭注上。
v2 加入籌碼面(外資/投信/融資),這是與技術面相關性較低的獨立資訊源。

交易員模板(Minervini / Weinstein / O'Neil)以「標籤」呈現而非加分,
因為它們彼此高度相關,全部加分等於把趨勢這個賭注押得更重。
"""

import argparse, json, os, sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tw_stock_monitor import (
    rsi, kd, macd, atr, load_config, log, fetch_history, build_universe,
    send_telegram, save_csv, BENCHMARK, DEFAULT_CONFIG)
import notify as N
from forecast import price_range, structural_levels
from valuation import multi_horizon, industry_premium
import industry as IND
import trading as TR
import chips as C
import revenue as REV


# ============================================================
# 交易員模板 — 只保留可完全量化的規則
# ============================================================

def rs_rating(hist, tickers):
    """
    O'Neil 式相對強弱評等:加權報酬 (2×最近季 + 前三季) 的百分位排名。
    這是唯一有學術支持的「名人指標」——本質就是動能因子。
    """
    scores = {}
    for t in tickers:
        c = hist.get(t, pd.DataFrame()).get("Close")
        if c is None or len(c) < 260:
            continue
        q = [float(c.iloc[-1] / c.iloc[-63] - 1), float(c.iloc[-63] / c.iloc[-126] - 1),
             float(c.iloc[-126] / c.iloc[-189] - 1), float(c.iloc[-189] / c.iloc[-252] - 1)]
        scores[t] = 2 * q[0] + q[1] + q[2] + q[3]
    if not scores:
        return {}
    s = pd.Series(scores)
    return (s.rank(pct=True) * 99).round().astype(int).to_dict()


def minervini_template(c, rs):
    """
    Minervini 趨勢模板 8 條件。全部量化,無主觀判讀。
    回傳 (通過條件數, 是否全過, 明細)
    """
    if len(c) < 260:
        return 0, False, {}
    ma50 = c.rolling(50).mean(); ma150 = c.rolling(150).mean()
    ma200 = c.rolling(200).mean()
    px = float(c.iloc[-1])
    lo52, hi52 = float(c.tail(252).min()), float(c.tail(252).max())
    ck = {
        "股價 > 150MA 且 > 200MA": px > ma150.iloc[-1] and px > ma200.iloc[-1],
        "150MA > 200MA": ma150.iloc[-1] > ma200.iloc[-1],
        "200MA 至少上揚一個月": ma200.iloc[-1] > ma200.iloc[-22],
        "50MA > 150MA 且 > 200MA": ma50.iloc[-1] > ma150.iloc[-1] > ma200.iloc[-1],
        "股價 > 50MA": px > ma50.iloc[-1],
        "距52週低點 ≥30%": px >= lo52 * 1.30,
        "距52週高點 ≤25%": px >= hi52 * 0.75,
        "RS 評等 ≥70": rs >= 70,
    }
    n = sum(ck.values())
    return n, n == 8, ck


def weinstein_stage(c):
    """Stan Weinstein 四階段。30 週線 = 150 日線。"""
    if len(c) < 200:
        return "資料不足"
    ma30w = c.rolling(150).mean()
    px, m, slope = float(c.iloc[-1]), float(ma30w.iloc[-1]), \
        float(ma30w.iloc[-1] - ma30w.iloc[-22])
    if px > m and slope > 0:
        return "第二階段(上升)"
    if px < m and slope < 0:
        return "第四階段(下跌)"
    if px > m and slope <= 0:
        return "第三階段(頭部)"
    return "第一階段(打底)"


# ============================================================
# v2 評分:技術 50 + 籌碼 30 + 月營收 15 + 估值 5
# ============================================================

def analyse_v2(ticker, df, bench_ret6m, fund, rs, chip, rev=None, indprem=None):
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    if len(c) < 260:
        return None

    ma20, ma60, ma200 = (c.rolling(n).mean() for n in (20, 60, 200))
    r = rsi(c); k, d = kd(h, l, c); _, _, hist = macd(c); a = atr(h, l, c)
    px = float(c.iloc[-1])

    earned, avail, penalty, hits, risks = 0, 0, 0, [], []
    missing = []
    pillar = {"tech": [0, 0], "chip": [0, 0], "rev": [0, 0], "val": [0, 0]}
    _cur = ["tech"]

    def add(cond, pts, label, has_data=True):
        """
        has_data=False 表示這項「無資料」,不計入分母。
        否則沒有籌碼/營收資料的股票會被當成表現差,而不是資料缺漏。
        """
        nonlocal earned, avail, penalty
        if pts < 0:
            if cond:
                penalty += pts
                risks.append(label)
            return
        if not has_data:
            return
        avail += pts
        pillar[_cur[0]][1] += pts
        if cond:
            earned += pts
            pillar[_cur[0]][0] += pts
            hits.append(label)

    # ---- 技術面 50 分 ----
    # 年線「上揚」比「站上」更嚴格,這是 v1 漏掉的
    add(px > ma200.iloc[-1] and ma200.iloc[-1] > ma200.iloc[-22], 14,
        "年線上揚且股價在其上")
    add(ma20.iloc[-1] > ma60.iloc[-1] > ma200.iloc[-1], 7, "均線多頭排列")
    add(rs >= 70, 11, f"RS 評等 {rs}")
    add(35 <= float(r.iloc[-1]) <= 58, 10, "RSI 回檔區,非追高")
    add((float(k.iloc[-2]) <= float(d.iloc[-2]) and float(k.iloc[-1]) > float(d.iloc[-1])
         and float(k.iloc[-1]) < 60) or
        (float(hist.iloc[-1]) > 0 >= float(hist.iloc[-2])), 8, "KD/MACD 轉強")

    # ---- 籌碼面 30 分 ----
    _cur[0] = "chip"
    ch = chip or {}
    fs, f5 = ch.get("foreign_streak", 0), ch.get("foreign_net_5", 0)
    ts, t5 = ch.get("trust_streak", 0), ch.get("trust_net_5", 0)
    mc = ch.get("margin_chg_5")
    has_chip = ch.get("days", 0) >= 5
    if not has_chip:
        missing.append("籌碼")
    vol5 = float(v.tail(5).sum()) / 1000.0     # 張

    # ── 配分依據(2026-09 以 657 日台股籌碼實測)──
    # 外資買超佔量 p=0.0023,樣本外兩組皆顯著 → 最高權重
    # 外資連買天數 p=0.0086,樣本外僅一組顯著 → 次之
    # 外資5日買超絕對值 樣本外兩組皆不顯著、且與佔量高度重複 → 移除
    # 投信買超 方向可能相反(p=0.062,樣本外一組 -2.27%)→ 移除,不倒扣
    add(fs >= 3, 9, f"外資連{fs}買", has_chip)
    add(f5 > 0 and vol5 > 0 and f5 / vol5 > 0.05, 14,
        f"外資5日買超佔量 {f5 / vol5 * 100:.1f}%" if vol5 else "外資買超佔量高", has_chip)
    # 投信:實測無正向證據,可能反向。保留顯示但不計分。
    if t5:
        pass
    # 融資:657 日實測無證據(20日 p=0.326 / 60日 p=0.095,樣本外皆不顯著),
    # 且分布呈 U 型而非線性 —— 兩端報酬都好、中間最差。保留顯示,不計分。
    # 注意:實測的是「融資5日變化」單一因子,評分原本用的是
    #       「融資減 >3% 且股價守住月線」這個複合條件,兩者不等價。
    #       複合條件未被單獨檢定,所以這是「證據不足」而非「已證明無效」。

    # ---- 月營收 15 分(台股獨有的高頻基本面) ----
    _cur[0] = "rev"
    rv = rev or {}
    ry, rst = rv.get("yoy"), rv.get("yoy_streak", 0)
    has_rev = ry is not None
    has_trend = rv.get("months", 0) >= 4      # 少於4個月算不出成長趨勢
    if not has_rev:
        missing.append("營收")
    add(ry is not None and ry > 10, 7,
        f"月營收年增 {ry}%" if ry else "營收成長", has_rev)
    add(rst >= 3, 5, f"營收連{rst}個月正成長", has_trend)
    add(rv.get("accel", False) and (ry or 0) > 0, 3, "營收成長加速", has_trend)

    # ---- 估值 5 分 ----
    _cur[0] = "val"
    pe, dy = fund.get("pe", np.nan), fund.get("yield", np.nan)
    add((not np.isnan(pe) and 0 < pe <= 20) or (not np.isnan(dy) and dy >= 3),
        5, "本益比/殖利率合理", not (np.isnan(pe) and np.isnan(dy)))

    # ---- 扣分 ----
    add(ry is not None and ry < -15 and rst <= -3, -10,
        f"營收連{abs(rst)}月衰退({ry}%)")
    add(fs <= -3, -12, f"外資連{abs(fs)}賣")
    add(mc is not None and mc > 15 and px < float(c.tail(60).max()) * 0.95,
        -8, "融資暴增但股價未創高(散戶追高)")
    add(px >= float(c.tail(250).max()) * 0.97, -8, "貼近年高")
    add(float(a.iloc[-1]) / px * 100 > 5, -8, "波動過大")
    add(px < float(ma20.iloc[-1]), -10, "跌破月線")

    # 依「實際有資料的項目」正規化,再扣分。
    # 資料缺漏不該被當成表現差,但覆蓋率低的分數本身較不可靠。
    base = (earned / avail * 100) if avail else 0.0
    score = max(0, min(100, round(base + penalty)))
    coverage = round(avail / 100 * 100)

    # ── 閘門式計分(實驗性,見 README「兩種計分法」)──
    # 加總式的問題:「趨勢爛但籌碼好」與「樣樣普通」可能同分,但意義完全不同。
    # 閘門式讓技術面當作 0.25–1.0 的乘數:趨勢不成立時,籌碼與營收的分數會被打折。
    t_e, t_a = pillar["tech"]
    gate = np.clip(t_e / t_a, 0.25, 1.0) if t_a else 0.25
    o_e = sum(pillar[k][0] for k in ("chip", "rev", "val", "dev") if k in pillar)
    o_a = sum(pillar[k][1] for k in ("chip", "rev", "val", "dev") if k in pillar)
    if t_a and o_a:
        gated_base = (t_e + o_e * gate) / (t_a + o_a) * 100
    else:
        gated_base = base
    gated = max(0, min(100, round(gated_base + penalty)))
    note = ""
    if gated < score - 4:
        note = f"閘門式 {gated} 分(低 {score - gated}):技術面僅 {t_e}/{t_a},籌碼營收被打折 {gate:.2f}"
    elif gated > score + 4:
        note = f"閘門式 {gated} 分(高 {gated - score}):技術面紮實,其他支柱未被折抵"

    # ── 偏離度標記(不計入分數,見 README「為何不併入評分」)──
    dev = multi_horizon(c)
    dev_out = None
    if dev:
        sm = dev.pop("_summary")
        d20, d60 = dev.get(20), dev.get(60)
        dev_out = {
            "direction": sm["direction"], "agreement": sm["agreement"],
            "all_agree": sm["all_agree"],
            "bias20": d20["bias_pct"] if d20 else None,
            "pct20": d20["pctile"] if d20 else None,
            "bias60": d60["bias_pct"] if d60 else None,
            "pct60": d60["pctile"] if d60 else None,
            "band": (d20 or d60 or {}).get("band"),
            "icon": (d20 or d60 or {}).get("icon"),
            "revert": d60["revert_to"] if d60 else None,
            "flag": sm["agreement"] >= 2,
        }

    # ── 偏離度 15 分(本專案證據最強的因子)──
    # 美股 p=0.0020、樣本外雙組複製(+4.11%/+3.68%)。
    # 方向與趨勢因子相反,因此獨立計分而非併入技術面。
    _cur[0] = "dev"
    pillar.setdefault("dev", [0, 0])
    if dev_out and dev_out.get("pct60") is not None:
        p60 = dev_out["pct60"]
        add(p60 <= 25, 15, f"乖離分位 {p60:.0f}(低檔,未過度延伸)")
        add(p60 >= 90, -10, f"乖離分位 {p60:.0f}(過度延伸)")

    # 結構性支撐壓力(呈現用,不計分)。被測試次數多不代表會守住。
    try:
        lv = structural_levels(h, l, c)
    except Exception:
        lv = {"support": [], "resistance": []}

    mn_n, mn_pass, _ = minervini_template(c, rs)
    return {
        "ticker": ticker, "name": fund.get("name", ""),
        "score": score, "gated_score": gated, "gate": round(float(gate), 2),
        "score_note": note,
        "pillars": {k: tuple(v) for k, v in pillar.items()},
        "dev": dev_out, "indprem": (indprem or {}).get(ticker.split(".")[0]),
        "support": lv.get("support", []), "resistance": lv.get("resistance", []),
        "ma60": round(float(ma60.iloc[-1]), 2),
        "coverage": coverage,
        "missing": "/".join(missing), "price": round(px, 2),
        "rs": rs, "minervini": f"{mn_n}/8" + ("  ✅全通過" if mn_pass else ""),
        "stage": weinstein_stage(c),
        "rsi": round(float(r.iloc[-1]), 1),
        "ma20": round(float(ma20.iloc[-1]), 2),
        "foreign_streak": fs, "foreign_net_5": f5,
        "trust_net_5": t5, "margin_chg_5": mc,
        "chip_days": ch.get("days", 0),
        "rev_yoy": ry, "rev_streak": rst, "rev_ym": rv.get("ym"),
        "industry": IND.refine(ticker.split(".")[0], rv.get("industry", "")),
        "ai_role": IND.ai_ecosystem(ticker.split(".")[0]),
        "atr_pct": round(float(a.iloc[-1]) / px * 100, 2),
        "stop_loss": round(px - 2 * float(a.iloc[-1]), 2),
        "pe": None if np.isnan(pe) else round(pe, 1),
        "hits": hits, "risks": risks,
    }


# ============================================================
# 報告
# ============================================================

def _pct(level, price):
    """把價位換算成距現價百分比 —— 絕對數字難比較,百分比一眼就懂。"""
    if not price:
        return "—"
    return f"{(level / price - 1) * 100:+.1f}%"


def _watch_lines(s):
    """
    「接下來看哪裡」:最近的支撐與壓力,含距現價百分比與被測試次數。

    這是對歷史的描述,不是預測 —— 被測試 3 次不代表第 4 次會守住。
    找不到就明講,不硬湊一個數字出來。
    """
    px = s["price"]
    sup = s.get("support") or []
    res = s.get("resistance") or []
    out = ["   接下來看哪裡:"]
    if sup:
        p, n = sup[0]
        out.append(f"     近端支撐 {p}({_pct(p, px)},歷史測試 {n} 次)")
        if len(sup) > 1:
            p2, n2 = sup[1]
            out.append(f"     第二支撐 {p2}({_pct(p2, px)},測試 {n2} 次)")
    else:
        out.append("     近端支撐:一年內無可辨識的轉折低點")
    if res:
        p, n = res[0]
        out.append(f"     近端壓力 {p}({_pct(p, px)},歷史測試 {n} 次)")
        if len(res) > 1:
            p2, n2 = res[1]
            out.append(f"     第二壓力 {p2}({_pct(p2, px)},測試 {n2} 次)")
    else:
        out.append("     近端壓力:已在一年高點附近,上方無參考價位")
    return out


def build_report_v2(buys, threshold, chip_days):
    L = [f"📊 台股綜合篩選 v2  {datetime.now():%Y-%m-%d}",
         f"(籌碼資料庫涵蓋 {chip_days} 個交易日)", ""]
    if not buys:
        L += ["今天沒有標的達到門檻。空手也是一種部位。", ""]
    for i, s in enumerate(buys, 1):
        cov = f"  (資料覆蓋 {s['coverage']}%"
        cov += f",缺{s['missing']})" if s["missing"] else ")"
        L.append(f"{i}. {s['ticker']} {s['name']}  {s['score']} 分"
                 + (cov if s["coverage"] < 100 else ""))
        L.append(f"   收盤 {s['price']} | RS {s['rs']} | {s['stage']}")
        L.append(f"   月線 {s['ma20']}({_pct(s['ma20'], s['price'])})"
                 f" | 季線 {s['ma60']}({_pct(s['ma60'], s['price'])})"
                 if s.get("ma60") else
                 f"   月線 {s['ma20']}({_pct(s['ma20'], s['price'])})")
        L.append(f"   Minervini 模板 {s['minervini']}")
        fs = s["foreign_streak"]
        fst = f"連{abs(fs)}{'買' if fs > 0 else '賣'}" if fs else "無明顯方向"
        L.append(f"   外資 {fst},5日 {s['foreign_net_5']:+.0f} 張 | "
                 f"投信5日 {s['trust_net_5']:+.0f} 張")
        if s["rev_yoy"] is not None:
            ym = s["rev_ym"]
            L.append(f"   {ym // 100}/{ym % 100:02d} 月營收年增 {s['rev_yoy']:+.1f}%"
                     + (f",連{abs(s['rev_streak'])}月"
                        f"{'成長' if s['rev_streak'] > 0 else '衰退'}"
                        if abs(s["rev_streak"]) >= 2 else ""))
        if s["margin_chg_5"] is not None:
            L.append(f"   融資5日變化 {s['margin_chg_5']:+.1f}%")
        L.append(f"   停損參考 {s['stop_loss']}"
                 f"({_pct(s['stop_loss'], s['price'])},2×ATR)")
        L += _watch_lines(s)
        d = s.get("dev")
        if d and d["flag"]:
            L.append(f"   {d['icon']} 股價{d['band']} — 20日乖離 {d['bias20']:+.1f}%"
                     f"(自身歷史第 {d['pct20']:.0f} 分位)"
                     )
            L.append(f"      60日乖離 {d['bias60']:+.1f}%(第 {d['pct60']:.0f} 分位),"
                     f"回到季線需 {d['revert']:+.1f}%"
                     + ("  ※三個天期一致" if d["all_agree"] else ""))
        ip = s.get("indprem")
        if ip and abs(ip["premium_pct"]) > 20:
            L.append(f"   同業{ip['label']} {ip['premium_pct']:+.1f}% — "
                     f"本益比 {ip['pe']} vs {ip['industry']}中位數 "
                     f"{ip['ind_median_pe']}({ip['ind_n']}檔)")
        if s.get("score_note"):
            L.append(f"   ※ {s['score_note']}")
        L.append(f"   ✔ {' / '.join(s['hits'][:4])}")
        if s["risks"]:
            L.append(f"   ⚠ {' / '.join(s['risks'])}")
        L.append("")
    L += ["— 籌碼資料為前一交易日收盤後公布,訊號已有一日延遲。",
          "— 量化篩選結果,僅供研究參考,不構成投資建議。"]
    return "\n".join(L)


def run(cfg, update_chips=True):
    con = C.init_db()
    if update_chips:
        try:
            _ni, _nm = C.update_today(con)
            log(f"更新籌碼資料:法人 {_ni} 檔 / 融資 {_nm} 檔")
        except Exception as e:
            log(f"籌碼更新失敗(將沿用既有資料):{e}")
    REV.init_db()
    nday = con.execute("SELECT COUNT(DISTINCT date) FROM inst").fetchone()[0]
    if nday < 5:
        log(f"⚠ 籌碼資料庫僅 {nday} 天,籌碼分數不可靠。"
            f"請先執行:python chips.py --backfill 90")

    tickers, fdf = build_universe(cfg)
    fund = {}
    if not fdf.empty:
        for t, row in fdf.iterrows():
            fund[t] = {"name": row["name"], "pe": row["pe"], "yield": row["yield"]}

    hist = fetch_history(list(dict.fromkeys(tickers + [BENCHMARK])),
                         cfg["history_days"])
    rs_map = rs_rating(hist, tickers)
    codes = [t.split(".")[0] for t in tickers]
    chip = C.chip_features(con, codes)
    REV.init_db()
    if update_chips:
        try:
            REV.update(con)
        except Exception as e:
            log(f"月營收更新失敗:{e}")
    rev = REV.revenue_features(con, set(codes))
    nrev = con.execute("SELECT COUNT(DISTINCT ym) FROM revenue").fetchone()[0]
    if nrev < 4:
        log(f"⚠ 月營收資料庫僅 {nrev} 個月,成長趨勢分數不可靠(每月執行會自動累積)")
    bret = 0.0
    if BENCHMARK in hist:
        bc = hist[BENCHMARK]["Close"]
        bret = float(bc.iloc[-1]) / float(bc.iloc[-120]) - 1

    indprem = industry_premium(
        {t.split(".")[0]: v for t, v in fund.items()},
        {k: v for k, v in rev.items()})

    res = []
    for t in tickers:
        if t not in hist:
            continue
        try:
            r = analyse_v2(t, hist[t], bret, fund.get(t, {}),
                           rs_map.get(t, 50), chip.get(t.split(".")[0]),
                           rev.get(t.split(".")[0]), indprem)
            if r:
                res.append(r)
        except Exception as e:
            log(f"{t} 失敗:{e}")

    res.sort(key=lambda x: x["score"], reverse=True)

    # ── 產業分析 ──
    agg = IND.industry_summary(res, rev)
    ind_rank = IND.rank_within_industry(res, rev)
    groups = IND.correlation_groups(hist, [r["ticker"] for r in res])
    cands = IND.find_candidates(res, rev, agg, ind_rank,
                                tech_only=cfg.get("tech_only", False))
    for r in res:
        rk = ind_rank.get(r["ticker"])
        if rk:
            r["ind_pct"] = rk["ind_pct"]; r["ind_n"] = rk["ind_n"]

    buys = [r for r in res if r["score"] >= cfg["score_threshold"]][:cfg["max_alerts"]]
    # 為入選標的補上 20 日機率區間
    for b in buys:
        try:
            pr = price_range(hist[b["ticker"]]["Close"], 20)
            if pr:
                b["range20"] = pr["p70"]
        except Exception:
            pass

    # ── 紙上交易(每天執行,隔天開盤成交,無前視偏差)──
    if cfg.get("paper", {}).get("enabled", True):
        try:
            tcon = TR.init_db()
            done = TR.execute_signals(tcon, hist, cfg)      # 先執行昨天的訊號
            stops = TR.check_stops(tcon, hist, cfg)         # 再檢查停損
            newn = TR.record_signals(tcon, res, cfg)        # 最後記錄今天的
            snap = TR.snapshot(tcon, hist, cfg)
            perf = TR.performance(tcon, "paper",
                                  cfg.get("paper", {}).get("capital"))
            paper_rep = ["", "─" * 46, TR.format_performance(perf)]
            if stops:
                paper_rep.append("  今日觸發:" + "、".join(
                    f"{t} {r} ({p:+.1f}%)" for t, r, p in stops))
            paper_rep.append(f"  淨值 {snap['total']:,.0f} 元"
                             f"(現金 {snap['cash']:,.0f} + 持股 {snap['market_value']:,.0f})")
            paper_rep.append(f"  昨日成交 {done} 筆,今日新增訊號 {newn} 筆(明日開盤成交)")
        except Exception as e:
            paper_rep = ["", f"紙上交易模組錯誤:{e}"]
    else:
        paper_rep = []

    rep = build_report_v2(buys, cfg["score_threshold"], nday)
    rep += "\n".join(paper_rep)
    rep += "\n\n" + IND.format_industry_report(agg, groups)
    if cands:
        rep += "\n\n【潛力股】產業熱度≥60 且同業前40% 且乖離未過度延伸 且營收成長\n"
        for c in cands[:12]:
            p60 = f"{c['dev_pct60']:.0f}" if c["dev_pct60"] is not None else "—"
            ry = f"{c['rev_yoy']:+.1f}%" if c.get("rev_yoy") is not None else "—"
            rep += (f"  {c['ticker']} {c['name']}  {c['score']}分 | "
                    f"{c['industry']}(熱度{c['ind_heat']:.0f}) | "
                    f"同業第{c['ind_pct']:.0f}分位/{c['ind_n']}檔 | "
                    f"乖離分位{p60} | 營收{ry}\n")
    else:
        rep += "\n\n【潛力股】今日無標的同時符合四項條件。"
    if cfg["notify"]["console"]:
        print("\n" + rep + "\n")
    if cfg["notify"]["save_csv"]:
        save_csv(res)

    n_hit = len(buys)
    subject = (f"台股篩選 {datetime.now():%m/%d} — {n_hit} 檔達標"
               if n_hit else f"台股篩選 {datetime.now():%m/%d} — 無標的達標")
    N.send_gmail(cfg, subject,
                 N.build_html(buys, cfg["score_threshold"], nday, nrev),
                 N.build_text(buys, cfg["score_threshold"]))
    send_telegram(cfg, rep)      # 若 config 仍啟用 Telegram 則一併發送
    return rep


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.json")
    p.add_argument("--no-chip-update", action="store_true")
    a = p.parse_args()
    run(load_config(a.config), update_chips=not a.no_chip_update)
