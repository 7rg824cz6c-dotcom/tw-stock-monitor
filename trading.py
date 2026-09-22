#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模擬交易引擎 — 兩種模式,不要混淆

  paper (紙上交易)  每天執行,記錄「今天的訊號」,隔天用真實開盤價成交。
                    往前累積,無前視偏差。這是唯一誠實的驗證。
                    缺點:要等好幾個月才有樣本。

  sim   (歷史模擬)  用歷史資料快速跑完整策略。
                    快,但只要你看過結果再回頭改參數,就已經污染了。
                    用途是「排除明顯很爛的設定」,不是「證明策略有效」。

真實成本(台股,2026):
  手續費 0.1425%,買賣各一次,多數券商有折扣(預設 6 折)
  證交稅 0.3%,賣出時課徵(當沖減半為 0.15%,本引擎不做當沖)
  滑價   市價單成交價與預期的差距,預設 0.1%

為什麼成本很重要:
  一次完整買賣的成本約 0.47%。若平均持有 60 天、年周轉 4 次,
  年成本就是 1.9%。一個「超額報酬 +0.6%」的策略在成本後是虧的。
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

DB = "trades.db"

# ── 成本參數 ──
FEE_RATE = 0.001425      # 手續費率
FEE_DISCOUNT = 0.6       # 券商折扣(6折)
FEE_MIN = 20             # 單筆最低手續費(元)
TAX_RATE = 0.003         # 證交稅(賣出)
SLIPPAGE = 0.001         # 滑價

DEFAULTS = {
    "capital": 1_000_000,        # 初始資金
    "position_pct": 0.10,        # 每檔投入資金比例
    "max_positions": 10,         # 最大同時持股數
    "stop_loss_atr": 2.0,        # 停損 = 買價 − N×ATR
    "take_profit_pct": None,     # 停利(None = 不設)
    "max_hold_days": 180,        # 最長持有天數
    "entry_score": 65,           # 進場分數門檻
    "exit_score": 30,            # 分數跌破此值出場
    "exit_below_ma20": False,    # 跌破月線出場 —— 預設關閉,見下方說明
}


def buy_cost(price, shares):
    """買進總成本(含手續費)。台股一張 1000 股。"""
    gross = price * shares
    fee = max(gross * FEE_RATE * FEE_DISCOUNT, FEE_MIN)
    return gross + fee, fee


def sell_proceeds(price, shares):
    """賣出淨收入(扣手續費與證交稅)。"""
    gross = price * shares
    fee = max(gross * FEE_RATE * FEE_DISCOUNT, FEE_MIN)
    tax = gross * TAX_RATE
    return gross - fee - tax, fee, tax


def round_trip_cost_pct():
    """一次完整買賣的成本佔比,用於績效歸因。"""
    return (FEE_RATE * FEE_DISCOUNT * 2 + TAX_RATE + SLIPPAGE * 2) * 100


# ============================================================
# 資料庫
# ============================================================

def init_db(path=DB):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT, ticker TEXT, name TEXT,
        entry_date TEXT, entry_price REAL, shares INTEGER,
        entry_score REAL, stop_loss REAL, entry_cost REAL,
        exit_date TEXT, exit_price REAL, exit_reason TEXT,
        pnl REAL, pnl_pct REAL, hold_days INTEGER,
        status TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS signals (
        date TEXT, mode TEXT, ticker TEXT, score REAL,
        action TEXT, executed INTEGER DEFAULT 0,
        PRIMARY KEY (date, mode, ticker, action));
    CREATE TABLE IF NOT EXISTS equity (
        date TEXT, mode TEXT, cash REAL, market_value REAL,
        total REAL, n_positions INTEGER,
        PRIMARY KEY (date, mode));
    CREATE INDEX IF NOT EXISTS ix_pos ON positions(mode, status);
    """)
    con.commit()
    return con


# ============================================================
# 紙上交易:今天記訊號,明天開盤成交
# ============================================================

def record_signals(con, results, cfg, mode="paper"):
    """把今天的掃描結果寫成待執行訊號(隔天開盤成交)。"""
    d = datetime.now().strftime("%Y%m%d")
    p = {**DEFAULTS, **(cfg.get("paper") or {})}
    open_tks = {r[0] for r in con.execute(
        "SELECT ticker FROM positions WHERE mode=? AND status='open'", (mode,))}
    n_open = len(open_tks)
    n = 0

    for r in results:
        t = r["ticker"]
        if t in open_tks:
            continue
        if r["score"] < p["entry_score"]:
            continue
        if n_open + n >= p["max_positions"]:
            break
        con.execute("INSERT OR REPLACE INTO signals VALUES(?,?,?,?,?,0)",
                    (d, mode, t, r["score"], "BUY"))
        n += 1

    # 出場訊號
    score_map = {r["ticker"]: r for r in results}
    for t in open_tks:
        r = score_map.get(t)
        if not r:
            continue
        reason = None
        if r["score"] < p["exit_score"]:
            reason = f"分數跌至 {r['score']}"
        elif p["exit_below_ma20"] and r["price"] < r.get("ma20", 0):
            reason = "跌破月線"
        if reason:
            con.execute("INSERT OR REPLACE INTO signals VALUES(?,?,?,?,?,0)",
                        (d, mode, t, r["score"], f"SELL:{reason}"))
    con.commit()
    return n


def execute_signals(con, hist, cfg, mode="paper"):
    """用最新開盤價執行昨天記錄的訊號。"""
    p = {**DEFAULTS, **(cfg.get("paper") or {})}
    pend = con.execute(
        "SELECT date,ticker,score,action FROM signals "
        "WHERE mode=? AND executed=0 ORDER BY date", (mode,)).fetchall()
    if not pend:
        return 0

    cash = get_cash(con, p["capital"], mode)
    done = 0
    for sd, t, sc, act in pend:
        df = hist.get(t)
        if df is None or df.empty:
            continue
        # 訊號日之後的第一個交易日開盤價
        fut = df[df.index > pd.Timestamp(sd)]
        if fut.empty:
            continue
        row = fut.iloc[0]
        px = float(row["Open"])

        if act == "BUY":
            px *= (1 + SLIPPAGE)
            budget = cash * p["position_pct"]
            lots = int(budget // (px * 1000))
            if lots < 1:
                continue
            shares = lots * 1000
            total, fee = buy_cost(px, shares)
            if total > cash:
                continue
            atr_est = float((df["High"] - df["Low"]).tail(14).mean())
            con.execute(
                "INSERT INTO positions(mode,ticker,name,entry_date,entry_price,"
                "shares,entry_score,stop_loss,entry_cost,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,'open')",
                (mode, t, "", str(fut.index[0].date()), px, shares, sc,
                 px - p["stop_loss_atr"] * atr_est, total))
            cash -= total
            done += 1
        elif act.startswith("SELL"):
            pos = con.execute(
                "SELECT id,entry_price,shares,entry_date,entry_cost FROM positions "
                "WHERE mode=? AND ticker=? AND status='open'", (mode, t)).fetchone()
            if not pos:
                continue
            px *= (1 - SLIPPAGE)
            pid, ep, sh, ed, ec = pos
            net, fee, tax = sell_proceeds(px, sh)
            hold = (fut.index[0] - pd.Timestamp(ed)).days
            con.execute(
                "UPDATE positions SET exit_date=?,exit_price=?,exit_reason=?,"
                "pnl=?,pnl_pct=?,hold_days=?,status='closed' WHERE id=?",
                (str(fut.index[0].date()), px, act.split(":", 1)[-1],
                 net - ec, (net / ec - 1) * 100, hold, pid))
            cash += net
            done += 1
        con.execute("UPDATE signals SET executed=1 WHERE date=? AND mode=? "
                    "AND ticker=? AND action=?", (sd, mode, t, act))
    con.commit()
    return done


def check_stops(con, hist, cfg, mode="paper"):
    """每日檢查停損、停利、持有上限。這是最容易被忽略但最重要的部分。"""
    p = {**DEFAULTS, **(cfg.get("paper") or {})}
    hit = []
    for pid, t, ep, sh, sl, ed, ec in con.execute(
            "SELECT id,ticker,entry_price,shares,stop_loss,entry_date,entry_cost "
            "FROM positions WHERE mode=? AND status='open'", (mode,)):
        df = hist.get(t)
        if df is None or df.empty:
            continue
        row = df.iloc[-1]
        lo, cl = float(row["Low"]), float(row["Close"])
        hold = (df.index[-1] - pd.Timestamp(ed)).days
        reason = px = None
        if lo <= sl:
            reason, px = "停損", sl        # 保守假設:以停損價成交
        elif p["take_profit_pct"] and cl >= ep * (1 + p["take_profit_pct"] / 100):
            reason, px = "停利", cl
        elif hold >= p["max_hold_days"]:
            reason, px = "持有到期", cl
        if reason:
            net, fee, tax = sell_proceeds(px * (1 - SLIPPAGE), sh)
            con.execute(
                "UPDATE positions SET exit_date=?,exit_price=?,exit_reason=?,"
                "pnl=?,pnl_pct=?,hold_days=?,status='closed' WHERE id=?",
                (str(df.index[-1].date()), px, reason, net - ec,
                 (net / ec - 1) * 100, hold, pid))
            hit.append((t, reason, round((net / ec - 1) * 100, 1)))
    con.commit()
    return hit


def get_cash(con, capital, mode="paper"):
    spent = con.execute(
        "SELECT COALESCE(SUM(entry_cost),0) FROM positions WHERE mode=? AND status='open'",
        (mode,)).fetchone()[0]
    realized = con.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM positions WHERE mode=? AND status='closed'",
        (mode,)).fetchone()[0]
    return capital + realized - spent


def snapshot(con, hist, cfg, mode="paper"):
    """記錄當日淨值。"""
    p = {**DEFAULTS, **(cfg.get("paper") or {})}
    cash = get_cash(con, p["capital"], mode)
    mv = 0.0
    n = 0
    for t, sh in con.execute(
            "SELECT ticker,shares FROM positions WHERE mode=? AND status='open'", (mode,)):
        df = hist.get(t)
        if df is not None and not df.empty:
            mv += float(df["Close"].iloc[-1]) * sh
            n += 1
    d = datetime.now().strftime("%Y%m%d")
    con.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?,?,?,?)",
                (d, mode, cash, mv, cash + mv, n))
    con.commit()
    return {"cash": cash, "market_value": mv, "total": cash + mv, "n": n}


# ============================================================
# 績效統計
# ============================================================

def performance(con, mode="paper", capital=None):
    cl = pd.read_sql(
        "SELECT * FROM positions WHERE mode=? AND status='closed'", con, params=(mode,))
    op = pd.read_sql(
        "SELECT * FROM positions WHERE mode=? AND status='open'", con, params=(mode,))
    eq = pd.read_sql("SELECT * FROM equity WHERE mode=? ORDER BY date", con, params=(mode,))
    cap = capital or DEFAULTS["capital"]

    if cl.empty:
        return {"n_closed": 0, "n_open": len(op),
                "note": "尚無已平倉交易。紙上交易需累積數月才有統計意義。"}

    wins = cl[cl.pnl > 0]
    losses = cl[cl.pnl <= 0]
    gross_win = wins.pnl.sum()
    gross_loss = abs(losses.pnl.sum())

    out = {
        "n_closed": len(cl), "n_open": len(op),
        "win_rate": round(len(wins) / len(cl) * 100, 1),
        "avg_win": round(wins.pnl_pct.mean(), 2) if len(wins) else 0,
        "avg_loss": round(losses.pnl_pct.mean(), 2) if len(losses) else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "total_pnl": round(cl.pnl.sum()),
        "total_pnl_pct": round(cl.pnl.sum() / cap * 100, 2),
        "avg_hold": round(cl.hold_days.mean()),
        "best": round(cl.pnl_pct.max(), 1),
        "worst": round(cl.pnl_pct.min(), 1),
        "cost_per_trade_pct": round(round_trip_cost_pct(), 3),
        "total_cost_est": round(len(cl) * cap * DEFAULTS["position_pct"]
                                * round_trip_cost_pct() / 100),
    }
    # 期望值:每次交易的平均損益,這比勝率重要得多
    out["expectancy_pct"] = round(cl.pnl_pct.mean(), 2)
    if len(eq) > 5:
        e = eq["total"]
        out["max_drawdown"] = round((e / e.cummax() - 1).min() * 100, 2)
        r = e.pct_change().dropna()
        if len(r) > 20 and r.std() > 0:
            out["sharpe"] = round(r.mean() / r.std() * np.sqrt(252), 2)
    by = cl.groupby("exit_reason").agg(
        n=("id", "size"), avg=("pnl_pct", "mean")).round(2)
    out["by_reason"] = {k: (int(v["n"]), float(v["avg"])) for k, v in by.iterrows()}
    return out


def format_performance(p, mode="paper"):
    if p.get("n_closed", 0) == 0:
        return f"【{mode} 績效】{p.get('note', '無資料')}(未平倉 {p.get('n_open', 0)} 檔)"
    L = [f"【{mode} 績效】{p['n_closed']} 筆已平倉,{p['n_open']} 檔持有中", ""]
    L.append(f"  總損益      {p['total_pnl']:+,} 元({p['total_pnl_pct']:+.2f}%)")
    L.append(f"  期望值      每筆 {p['expectancy_pct']:+.2f}%   ← 比勝率重要")
    L.append(f"  勝率        {p['win_rate']}%  "
             f"(平均賺 {p['avg_win']:+.2f}% / 平均賠 {p['avg_loss']:+.2f}%)")
    if p.get("profit_factor"):
        L.append(f"  獲利因子    {p['profit_factor']}  (>1.5 才算穩健)")
    if p.get("max_drawdown") is not None:
        L.append(f"  最大回撤    {p['max_drawdown']:.2f}%")
    if p.get("sharpe") is not None:
        L.append(f"  夏普值      {p['sharpe']}")
    L.append(f"  平均持有    {p['avg_hold']} 天 | 最佳 {p['best']:+.1f}% / 最差 {p['worst']:+.1f}%")
    L.append("")
    L.append(f"  交易成本    每次來回 {p['cost_per_trade_pct']}%,"
             f"累計約 {p['total_cost_est']:,} 元")
    if p.get("by_reason"):
        L.append("  出場原因:")
        for k, (n, a) in sorted(p["by_reason"].items(), key=lambda x: -x[1][0]):
            L.append(f"    {k:<12} {n:>3} 筆,平均 {a:+.2f}%")
    return "\n".join(L)
