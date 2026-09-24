#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gmail 通知模組

Gmail 有兩個一定會踩到的坑:
  1. 一般登入密碼不能用。必須先開啟兩步驟驗證,再產生「應用程式密碼」。
  2. 應用程式密碼顯示成 4 組 4 字元(abcd efgh ijkl mnop),
     貼進設定檔時空格要不要留?本模組會自動去除,兩種都可以。

密碼建議放環境變數而非 config.json,避免不小心提交到 git 或分享資料夾外洩:
    export GMAIL_APP_PASSWORD="abcdefghijklmnop"

測試設定:
    python notify.py --test
"""

import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    import netutil as _N
    _N.force_utf8_stdio()
except Exception:
    pass

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587          # STARTTLS。若被防火牆擋,可改 465 + SSL
SMTP_PORT_SSL = 465


def get_password(cfg_email):
    """優先讀環境變數,其次讀設定檔。自動去除應用程式密碼的空格。"""
    pw = os.environ.get("GMAIL_APP_PASSWORD") or cfg_email.get("password", "")
    return pw.replace(" ", "").strip()


# ============================================================
# HTML 報告
# ============================================================

CSS = """
body{font-family:-apple-system,"Noto Sans TC","Microsoft JhengHei",sans-serif;
     color:#1a1a1a;line-height:1.6;margin:0;padding:16px;background:#f6f7f9}
.wrap{max-width:680px;margin:0 auto;background:#fff;border-radius:10px;
      padding:24px;border:1px solid #e3e6ea}
h1{font-size:19px;margin:0 0 4px}
.sub{color:#6b7280;font-size:13px;margin-bottom:18px}
.card{border:1px solid #e3e6ea;border-radius:8px;padding:14px;margin-bottom:12px}
.hd{display:flex;justify-content:space-between;align-items:baseline;
    border-bottom:1px solid #eef0f3;padding-bottom:8px;margin-bottom:10px}
.tk{font-weight:700;font-size:16px}
.sc{font-weight:700;font-size:20px;padding:2px 10px;border-radius:6px;color:#fff}
.g{background:#16a34a}.y{background:#ca8a04}.r{background:#dc2626}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:3px 0;vertical-align:top}
td.k{color:#6b7280;width:34%}
ul{margin:8px 0 0;padding-left:18px;font-size:13px}
li{margin:2px 0}
.hit{color:#15803d}.risk{color:#b91c1c}
.warn{background:#fef3c7;border:1px solid #fcd34d;border-radius:6px;
      padding:10px;font-size:13px;margin-bottom:14px}
.foot{color:#6b7280;font-size:12px;border-top:1px solid #eef0f3;
      padding-top:12px;margin-top:16px}
.cov{color:#6b7280;font-size:12px;font-weight:400}
.watch{background:#f8fafc;border:1px solid #e3e6ea;border-radius:6px;
       padding:10px 12px;margin-top:10px;font-size:13px}
.note{color:#6b7280;font-size:11px;margin-top:6px}
"""


def _pct(level, price):
    if not price:
        return ""
    return f"{(level / price - 1) * 100:+.1f}%"


def _color(score):
    return "g" if score >= 70 else ("y" if score >= 55 else "r")


def build_html(buys, threshold, chip_days=0, rev_months=0, exits=None):
    today = f"{datetime.now():%Y-%m-%d}"
    h = [f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>",
         "<div class='wrap'>",
         f"<h1>📊 台股綜合篩選 v3</h1>",
         f"<div class='sub'>{today} · 門檻 {threshold} 分 · "
         f"籌碼 {chip_days} 日 / 營收 {rev_months} 月</div>",
         "<div style='margin:10px 0 4px'>"
         "<a href='https://tw-stock-monitor.streamlit.app/' "
         "style='display:inline-block;padding:8px 16px;background:#ff4b4b;"
         "color:#fff;text-decoration:none;border-radius:6px;font-size:13px'>"
         "🔍 開啟互動網頁(調整篩選條件 / 個股機率區間)</a></div>"]

    if chip_days < 20 or rev_months < 4:
        miss = []
        if chip_days < 20:
            miss.append(f"籌碼僅 {chip_days} 日")
        if rev_months < 4:
            miss.append(f"營收僅 {rev_months} 月")
        h.append(f"<div class='warn'>⚠ 資料累積不足({'、'.join(miss)}),"
                 f"相關分數已排除於分母外,但可靠度較低。</div>")

    if not buys:
        h.append("<div class='card'>今天沒有標的達到門檻。空手也是一種部位。</div>")

    for s in buys:
        cov = (f" <span class='cov'>覆蓋 {s['coverage']}%"
               + (f",缺{s['missing']}" if s.get("missing") else "") + "</span>") \
            if s.get("coverage", 100) < 100 else ""
        h.append("<div class='card'>")
        h.append(f"<div class='hd'><span class='tk'>{s['ticker']} "
                 f"{s['name']}{cov}</span>"
                 f"<span class='sc {_color(s['score'])}'>{s['score']}</span></div>")
        rows = [("收盤", f"{s['price']}"),
                ("停損參考", f"{s['stop_loss']}"
                             f" <span class='cov'>({_pct(s['stop_loss'], s['price'])},"
                             f"2×ATR)</span>"),
                ("RS 評等", s["rs"]),
                ("Weinstein", s["stage"]),
                ("Minervini", s["minervini"])]
        fs = s.get("foreign_streak", 0)
        rows.append(("外資", f"連{abs(fs)}{'買' if fs > 0 else '賣'}"
                     f",5日 {s.get('foreign_net_5', 0):+.0f} 張" if fs
                     else f"5日 {s.get('foreign_net_5', 0):+.0f} 張"))
        if s.get("trust_net_5"):
            rows.append(("投信", f"5日 {s['trust_net_5']:+.0f} 張"))
        if s.get("rev_yoy") is not None:
            ym = s.get("rev_ym") or 0
            rows.append((f"月營收 {ym // 100}/{ym % 100:02d}",
                         f"年增 {s['rev_yoy']:+.1f}%"))
        if s.get("margin_chg_5") is not None:
            rows.append(("融資 5 日", f"{s['margin_chg_5']:+.1f}%"))
        if s.get("range20"):
            lo, hi = s["range20"]
            rows.append(("20日70%區間", f"{lo} ~ {hi}"))
        if s.get("ai_role"):
            rows.append(("AI供應鏈", s["ai_role"] +
                         " <span class='cov'>(僅供參考,非計分項目)</span>"))
        h.append("<table>" + "".join(
            f"<tr><td class='k'>{k}</td><td>{v}</td></tr>" for k, v in rows) + "</table>")

        # 「接下來看哪裡」—— 對歷史的描述,不是預測
        sup, res = s.get("support") or [], s.get("resistance") or []
        if sup or res or s.get("price"):
            h.append("<div class='watch'><b>接下來看哪裡</b><table>")
            if res:
                for lb, (p, n) in zip(("近端壓力", "第二壓力"), res[:2]):
                    h.append(f"<tr><td class='k'>{lb}</td><td>{p} "
                             f"<span class='cov'>({_pct(p, s['price'])},"
                             f"測試 {n} 次)</span></td></tr>")
            else:
                h.append("<tr><td class='k'>近端壓力</td>"
                         "<td class='cov'>已在一年高點附近,上方無參考價位</td></tr>")
            if sup:
                for lb, (p, n) in zip(("近端支撐", "第二支撐"), sup[:2]):
                    h.append(f"<tr><td class='k'>{lb}</td><td>{p} "
                             f"<span class='cov'>({_pct(p, s['price'])},"
                             f"測試 {n} 次)</span></td></tr>")
            else:
                h.append("<tr><td class='k'>近端支撐</td>"
                         "<td class='cov'>一年內無可辨識的轉折低點</td></tr>")
            h.append("</table><div class='note'>被測試 N 次是歷史事實,"
                     "不保證下次會守住。</div></div>")
        if s.get("hits"):
            h.append("<ul>" + "".join(
                f"<li class='hit'>✔ {x}</li>" for x in s["hits"][:5]) + "</ul>")
        if s.get("risks"):
            h.append("<ul>" + "".join(
                f"<li class='risk'>⚠ {x}</li>" for x in s["risks"]) + "</ul>")
        h.append("</div>")

    if exits:
        h.append("<h1 style='font-size:16px;margin-top:22px'>⚠️ 持股警示</h1>")
        for e in exits:
            h.append(f"<div class='card'><b>{e['ticker']}</b> 現價 {e['price']}"
                     f"(損益 {e['pnl']:+.1f}%)<br>"
                     f"<span class='risk'>{'、'.join(e['reasons'])}</span></div>")

    h.append("<div class='foot'>籌碼資料為前一交易日收盤後公布,訊號有一日延遲。<br>"
             "機率區間為波動率測量結果,非目標價預測。<br>"
             "本信件為量化篩選結果,僅供個人研究,不構成投資建議。</div>")
    h.append("</div></body></html>")
    return "".join(h)


def build_text(buys, threshold):
    """純文字備援,給不顯示 HTML 的信箱。"""
    L = [f"台股綜合篩選 {datetime.now():%Y-%m-%d}(門檻 {threshold} 分)",
         "互動網頁:https://tw-stock-monitor.streamlit.app/", ""]
    if not buys:
        L.append("今天沒有標的達到門檻。")
    for i, s in enumerate(buys, 1):
        L.append(f"{i}. {s['ticker']} {s['name']}  {s['score']} 分")
        L.append(f"   收盤 {s['price']} | 停損 {s['stop_loss']} | RS {s['rs']}")
        if s.get("rev_yoy") is not None:
            L.append(f"   月營收年增 {s['rev_yoy']:+.1f}%")
        L.append(f"   ✔ {' / '.join(s.get('hits', [])[:4])}")
        if s.get("risks"):
            L.append(f"   ⚠ {' / '.join(s['risks'])}")
        L.append("")
    L.append("僅供個人研究,不構成投資建議。")
    return "\n".join(L)


# ============================================================
# 寄送
# ============================================================

def send_gmail(cfg, subject, html, text=None, force=False):
    em = cfg.get("notify", {}).get("email", cfg)
    if not em.get("enabled", True) and not force:
        print("✗ config.json 中 notify.email.enabled 為 false,已略過寄送。")
        return False
    user, to = em.get("username", ""), em.get("to", "") or em.get("username", "")
    pw = get_password(em)

    if not user or not pw:
        print("✗ Email 未設定:缺少帳號或應用程式密碼")
        print("  設定方式:export GMAIL_APP_PASSWORD='你的16位應用程式密碼'")
        return False
    if len(pw) != 16:
        print(f"⚠ 應用程式密碼長度為 {len(pw)},Gmail 應用程式密碼應為 16 字元。")
        print("  如果你填的是一般登入密碼,Gmail 會拒絕連線。")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(text or "請以 HTML 檢視", "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    try:
        port = int(em.get("smtp_port", SMTP_PORT))
        if port == SMTP_PORT_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, port, context=ctx, timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, port, timeout=30) as s:
                s.ehlo(); s.starttls(context=ctx); s.ehlo()
                s.login(user, pw)
                s.send_message(msg)
        print(f"✓ 已寄至 {to}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        print(f"✗ 認證失敗:{e.smtp_code}")
        print("  最常見原因,依機率排序:")
        print("   1. 用了一般登入密碼 → 必須用「應用程式密碼」")
        print("   2. 沒開兩步驟驗證 → 沒開就無法產生應用程式密碼")
        print("   3. 應用程式密碼已被撤銷 → 重新產生一組")
        print("  產生網址:https://myaccount.google.com/apppasswords")
    except smtplib.SMTPServerDisconnected:
        print("✗ 連線被中斷。若在公司或學校網路,587 埠可能被擋,"
              "把 smtp_port 改成 465 試試。")
    except (TimeoutError, OSError) as e:
        print(f"✗ 連線失敗:{e}。檢查網路,或改用 465 埠。")
    except Exception as e:
        print(f"✗ 寄送失敗:{type(e).__name__}: {e}")
    return False


if __name__ == "__main__":
    import json
    cfg = json.load(open("config.json", encoding="utf-8")) \
        if os.path.exists("config.json") else {}
    if "--test" in sys.argv:
        demo = [{
            "ticker": "2330.TW", "name": "台積電", "score": 72, "coverage": 100,
            "missing": "", "price": 1085.0, "stop_loss": 1032.5, "rs": 88,
            "stage": "第二階段(上升)", "minervini": "8/8  ✅全通過",
            "foreign_streak": 5, "foreign_net_5": 12400, "trust_net_5": 3100,
            "rev_yoy": 28.4, "rev_ym": 202606, "margin_chg_5": -6.2,
            "range20": (1042.0, 1131.0),
            "support": [(1032.0, 3), (968.0, 2)],
            "resistance": [(1145.0, 4), (1210.0, 2)],
            "hits": ["年線上揚且股價在其上", "RS 評等 88", "外資連5買", "月營收年增 28.4%"],
            "risks": ["貼近年高"],
        }]
        html = build_html(demo, 65, chip_days=90, rev_months=6)
        with open("email_preview.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("已產生預覽:email_preview.html(可用瀏覽器開啟確認排版)")
        print("正在寄送測試信…")
        send_gmail(cfg, "【測試】台股監控設定成功", html, build_text(demo, 65), force=True)
    else:
        print(__doc__)
