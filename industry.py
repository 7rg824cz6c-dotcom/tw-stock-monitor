#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
產業分析 — 分類、族群發現、同業排名

三個層次:

  1. 官方分類   證交所 33 種產業別,來自月營收資料的「產業別」欄位。
                權威、不會錯,但粒度粗:「半導體業」裡有台積電也有小型 IC 設計。

  2. 相關族群   用價格相關性從資料裡發現實際共同波動的族群。
                刻意不寫死供應鏈關係——那是我的記憶,可能過時,
                而且台股供應鏈重組很快。資料說了算。

  3. 同業排名   個股分數在自己產業內的百分位。跨產業比分數沒有意義:
                景氣循環股與成長股的合理分數區間本來就不同。

「潛力股」的定義(明確寫出來,不藏在黑盒子裡):
    產業動能為正 AND 同業排名前 40% AND 乖離未過度延伸 AND 營收成長
    其中「乖離未過度延伸」用的是本專案唯一通過樣本外檢定的發現。
    這仍然是篩選,不是推薦。
"""

import os
import re

import numpy as np
import pandas as pd

# 證交所官方產業別(取自 TWSE 三大法人查詢頁的分類選單)
TECH_SECTORS = [
    "半導體業", "電腦及週邊設備業", "光電業", "通信網路業",
    "電子零組件業", "電子通路業", "資訊服務業", "其他電子業",
    "電子工業", "數位雲端",
]
TECH_ADJACENT = ["電機機械", "電器電纜", "綠能環保"]   # 泛科技,受同樣景氣循環影響

ALL_SECTORS = TECH_SECTORS + TECH_ADJACENT + [
    "水泥工業", "食品工業", "塑膠工業", "紡織纖維", "化學生技醫療",
    "化學工業", "生技醫療業", "玻璃陶瓷", "造紙工業", "鋼鐵工業",
    "橡膠工業", "汽車工業", "建材營造", "航運業", "觀光餐旅",
    "金融保險", "貿易百貨", "油電燃氣業", "存託憑證", "綜合",
    "運動休閒", "居家生活", "其他",
]


def classify(industry):
    """回傳 ('科技'|'泛科技'|'非科技', 產業別)"""
    if not industry:
        return "未分類", ""
    if industry in TECH_SECTORS:
        return "科技", industry
    if industry in TECH_ADJACENT:
        return "泛科技", industry
    return "非科技", industry


# ============================================================
# 半導體業細分
# ============================================================
# 證交所「半導體業」是單一分類,裡面同時有台積電跟小型 IC 設計,
# 拿分數互相比較沒有意義。這裡手動對照到子產業,只收案子比較確定、
# 主業單一的公司;結構複雜(IDM、跨足多段)的公司寧可不分,維持「半導體業」。
# 這份對照表跟 supply_chain_map.yaml 一樣是我的記憶,非官方資料,會過時,
# 建議每季順手校對一次。代號用 4 碼(不含 .TW/.TWO)。
SEMI_SUBSECTOR = {
    # 晶圓代工
    "2330": "晶圓代工", "2303": "晶圓代工", "5347": "晶圓代工", "6770": "晶圓代工",
    "3707": "晶圓代工", "3105": "晶圓代工",
    # IC 設計
    "2454": "IC設計", "3034": "IC設計", "2379": "IC設計", "6415": "IC設計",
    "3443": "IC設計", "3661": "IC設計", "3035": "IC設計", "6643": "IC設計",
    "3227": "IC設計", "2458": "IC設計", "8016": "IC設計", "4966": "IC設計",
    "5269": "IC設計", "3545": "IC設計", "4961": "IC設計", "6288": "IC設計",
    "8081": "IC設計", "6202": "IC設計", "3529": "IC設計", "6533": "IC設計",
    # 封測
    "3711": "封測", "6239": "封測", "2449": "封測", "6257": "封測",
    "6147": "封測", "8150": "封測", "2369": "封測",
    # 記憶體
    "2408": "記憶體", "2344": "記憶體", "2337": "記憶體", "8299": "記憶體",
    "3260": "記憶體", "4967": "記憶體", "8271": "記憶體",
    # 設備儀器
    "3680": "半導體設備", "3413": "半導體設備", "3131": "半導體設備",
    "6196": "半導體設備", "2404": "半導體設備", "6139": "半導體設備",
    "6510": "半導體設備", "3583": "半導體設備",
    # 矽晶圓/材料
    "6488": "矽材料", "5483": "矽材料", "5434": "矽材料", "3532": "矽材料",
}


def refine(code, industry):
    """半導體業再細分;其他產業或查無對照的半導體股原樣傳回。"""
    if industry == "半導體業":
        return SEMI_SUBSECTOR.get(code, industry)
    return industry


# ============================================================
# AI 生態系角色
# ============================================================
# 借用 news/supply_chain_map.yaml(新聞爬蟲那份對照表)裡跟 AI 直接
# 相關的幾個區塊,算出每檔股票在 AI 供應鏈裡扮演的角色。
# 「特別標註」不是我主觀判斷護城河,是客觀算出來的:
# 同時被 3 個以上不同 AI 需求來源(NVIDIA/AMD/Broadcom ASIC/CSP資本支出/
# 記憶體)引用,代表這家公司不是繫在單一客戶身上,跨多個 AI 需求來源都
# 找得到它,汰換難度自然比只服務一個客戶的公司高。
AI_SECTIONS = ["NVIDIA", "AMD", "Broadcom_ASIC", "CSP資本支出", "記憶體"]
NOTABLE_MIN_SECTIONS = 3

_AI_ROLE_CACHE = None


def _parse_ai_roles():
    """回傳 {code: {"name":.., "roles": {role,...}, "sections": {section,...}}}。
    supply_chain_map.yaml 讀不到就回傳空字典 —— 這是加分資訊,不是必要資料。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "news", "supply_chain_map.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return {}

    out = {}
    for section in AI_SECTIONS:
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for role, entries in block.items():
            if role == "說明" or not isinstance(entries, list):
                continue
            for entry in entries:
                m = re.match(r"^(.*\S)\s+(\d{4,6})$", str(entry).strip())
                if not m:
                    continue
                name, code = m.group(1), m.group(2)
                rec = out.setdefault(code, {"name": name, "roles": set(), "sections": set()})
                rec["roles"].add(role)
                rec["sections"].add(section)
    return out


def ai_ecosystem(code):
    """回傳這檔股票的 AI 供應鏈角色描述,查無資料回傳 None。
    格式例:"★ 代工(AMD/Broadcom_ASIC/NVIDIA)" —— ★ 代表同時被 3 個以上
    不同 AI 需求來源引用。"""
    global _AI_ROLE_CACHE
    if _AI_ROLE_CACHE is None:
        _AI_ROLE_CACHE = _parse_ai_roles()
    rec = _AI_ROLE_CACHE.get(code)
    if not rec:
        return None
    roles = "/".join(sorted(rec["roles"]))
    star = "★ " if len(rec["sections"]) >= NOTABLE_MIN_SECTIONS else ""
    return f"{star}{roles}({'/'.join(sorted(rec['sections']))})"


# ============================================================
# 產業總覽
# ============================================================

def industry_summary(results, rev_map, min_n=3):
    """
    每個產業的整體狀態。

    breadth   該產業有多少比例的股票站上季線(市場廣度)
    momentum  該產業成分股的中位數 6 個月報酬
    rev_yoy   中位數月營收年增
    foreign   外資 5 日淨買超合計(張)
    """
    rows = []
    for r in results:
        code = r["ticker"].split(".")[0]
        ind = (rev_map.get(code) or {}).get("industry") or r.get("industry") or ""
        if not ind:
            continue
        d = r.get("dev") or {}
        rows.append({
            "industry": ind, "ticker": r["ticker"], "name": r["name"],
            "score": r["score"], "rs": r["rs"],
            "rev_yoy": r.get("rev_yoy"),
            "above_ma60": r["price"] > r.get("ma60", r["price"]),
            "foreign_5": r.get("foreign_net_5", 0) or 0,
            "pct60": d.get("pct60"),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)

    agg = df.groupby("industry").agg(
        n=("ticker", "size"),
        score_med=("score", "median"),
        rs_med=("rs", "median"),
        rev_yoy_med=("rev_yoy", "median"),
        breadth=("above_ma60", "mean"),
        foreign_sum=("foreign_5", "sum"),
    ).reset_index()
    agg = agg[agg["n"] >= min_n]
    agg["breadth"] = (agg["breadth"] * 100).round(0)
    agg["類型"] = agg["industry"].map(lambda x: classify(x)[0])

    # 產業動能綜合分:RS 中位數、廣度、營收成長的橫斷面排名平均
    for c in ("rs_med", "breadth", "rev_yoy_med"):
        agg[c + "_r"] = agg[c].rank(pct=True)
    agg["heat"] = (agg[["rs_med_r", "breadth_r", "rev_yoy_med_r"]]
                   .mean(axis=1) * 100).round(0)
    return agg.sort_values("heat", ascending=False)


def rank_within_industry(results, rev_map):
    """個股分數在自己產業內的百分位。跨產業比分數沒有意義。"""
    rows = []
    for r in results:
        code = r["ticker"].split(".")[0]
        ind = (rev_map.get(code) or {}).get("industry")
        if ind:
            rows.append({"ticker": r["ticker"], "industry": ind,
                         "score": r["score"]})
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    df["pct"] = df.groupby("industry")["score"].rank(pct=True) * 100
    df["n"] = df.groupby("industry")["score"].transform("size")
    return {r["ticker"]: {"ind_pct": round(r["pct"]), "ind_n": int(r["n"]),
                          "industry": r["industry"]}
            for _, r in df.iterrows() if r["n"] >= 3}


# ============================================================
# 相關族群(資料驅動,不寫死供應鏈)
# ============================================================

def correlation_groups(hist, tickers, window=250, threshold=0.65, min_size=3):
    """
    用日報酬相關性把股票分群。

    刻意不使用寫死的供應鏈對照表——那是靜態知識,會過時。
    共同波動是市場對「這些公司受同一因素影響」的實際定價。
    """
    px = {}
    for t in tickers:
        d = hist.get(t)
        if d is not None and len(d) >= window:
            px[t] = d["Close"].tail(window)
    if len(px) < min_size:
        return []
    ret = pd.DataFrame(px).pct_change().dropna(how="all")
    if len(ret) < 100:
        return []
    corr = ret.corr()

    # 簡單階層式聚合:反覆把相關性最高的成員併入群組
    unassigned = set(corr.columns)
    groups = []
    while unassigned:
        seed = max(unassigned,
                   key=lambda t: corr.loc[t, list(unassigned)].sum())
        members = {seed}
        for t in list(unassigned - {seed}):
            if corr.loc[seed, t] >= threshold:
                members.add(t)
        unassigned -= members
        if len(members) >= min_size:
            sub = corr.loc[list(members), list(members)]
            avg = (sub.values.sum() - len(members)) / (len(members) ** 2 - len(members))
            groups.append({"members": sorted(members),
                           "avg_corr": round(float(avg), 2)})
    return sorted(groups, key=lambda g: -len(g["members"]))


# ============================================================
# 潛力股篩選
# ============================================================

def find_candidates(results, rev_map, ind_summary, ind_rank,
                    heat_min=60, rank_min=60, pct_max=75, tech_only=False):
    """
    定義寫在這裡,不藏起來:

      1. 產業熱度 ≥ heat_min          — 產業本身有動能
      2. 同業排名 ≥ rank_min 百分位   — 在自己產業裡是前段班
      3. 乖離分位 ≤ pct_max           — 尚未過度延伸(唯一經檢定的條件)
      4. 營收年增 > 0                 — 基本面不是衰退中

    第 3 條是關鍵:它排除已經噴出的標的。實測顯示乖離分位高的
    未來報酬明顯較差,這是本專案唯一通過樣本外顯著性檢定的發現。
    """
    if ind_summary.empty:
        return []
    heat = dict(zip(ind_summary["industry"], ind_summary["heat"]))
    out = []
    for r in results:
        t = r["ticker"]
        rk = ind_rank.get(t)
        if not rk:
            continue
        ind = rk["industry"]
        cat = classify(ind)[0]
        if tech_only and cat == "非科技":
            continue
        h = heat.get(ind)
        if h is None or h < heat_min:
            continue
        if rk["ind_pct"] < rank_min:
            continue
        d = r.get("dev") or {}
        p60 = d.get("pct60")
        if p60 is not None and p60 > pct_max:
            continue
        ry = r.get("rev_yoy")
        if ry is not None and ry <= 0:
            continue
        out.append({**r, "industry": ind, "category": cat,
                    "ind_heat": h, "ind_pct": rk["ind_pct"],
                    "ind_n": rk["ind_n"], "dev_pct60": p60})
    return sorted(out, key=lambda x: (-x["ind_heat"], -x["ind_pct"]))


def format_industry_report(agg, groups=None, top=12):
    if agg.empty:
        return "產業資料不足(需要月營收資料提供產業別)。"
    L = ["【產業熱度】熱度 = RS中位數 / 市場廣度 / 營收成長 三項排名平均", ""]
    L.append(f"{'產業':<14}{'類型':<6}{'檔數':>4}{'熱度':>5}{'RS':>5}"
             f"{'廣度':>6}{'營收年增':>9}{'外資5日':>10}")
    L.append("-" * 62)
    for _, r in agg.head(top).iterrows():
        ry = f"{r['rev_yoy_med']:+.1f}%" if pd.notna(r["rev_yoy_med"]) else "—"
        L.append(f"{r['industry']:<14}{r['類型']:<6}{r['n']:>4.0f}{r['heat']:>5.0f}"
                 f"{r['rs_med']:>5.0f}{r['breadth']:>5.0f}%{ry:>9}"
                 f"{r['foreign_sum']:>+10.0f}")
    if groups:
        L += ["", "【相關族群】由日報酬相關性發現,非預設供應鏈對照表", ""]
        for i, g in enumerate(groups[:5], 1):
            L.append(f"  族群{i}(平均相關 {g['avg_corr']}):"
                     f"{'、'.join(m.split('.')[0] for m in g['members'][:10])}"
                     + ("…" if len(g["members"]) > 10 else ""))
    return "\n".join(L)
