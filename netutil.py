#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共用 HTTPS 連線工具

解決的問題:
    Python 3.13 起,ssl.create_default_context() 預設啟用 VERIFY_X509_STRICT,
    會嚴格檢查 RFC 5280 合規性。證交所(twse.com.tw)的憑證缺少
    Subject Key Identifier 欄位,因此在 Python 3.13+ 會出現:

        ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED]
        certificate verify failed: Missing Subject Key Identifier

    Python 3.12 以前不會有這個問題。

本模組的做法:
    只移除 VERIFY_X509_STRICT 這一個旗標,其餘驗證全部保留 ——
    憑證鏈驗證、主機名稱檢查、有效期限檢查都照常執行。

    ⚠️ 這與 ssl._create_unverified_context() 完全不同。
       後者會關閉所有驗證,讓連線可被中間人攻擊。本模組不這樣做。

    白名單限定:只對明確列出的政府/交易所網域放寬,其他網域維持完整嚴格驗證。
"""

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

def force_utf8_stdio():
    """
    Windows 預設編碼 cp950(Big5)無法輸出 ⚠ ✓ 📊 等符號。
    直接跑通常沒事,但重導向到檔案(排程的 >> run.log)就會
    UnicodeEncodeError 中斷。這裡把 stdout/stderr 轉成 UTF-8,
    無法編碼的字元以替代字元處理而非拋例外。
    """
    for name in ("stdout", "stderr"):
        st = getattr(sys, name, None)
        if st is None:
            continue
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


force_utf8_stdio()

UA = {"User-Agent": "Mozilla/5.0 (compatible; personal-research-script)"}

# 只有這些網域套用放寬(皆為台灣官方金融資訊來源)
RELAXED_HOSTS = ("twse.com.tw", "tpex.org.tw", "tdcc.com.tw", "gov.tw")

_ctx_cache = {}


def strict_flag_is_default():
    """這個 Python 版本是否預設啟用嚴格模式(3.13+)。"""
    try:
        return bool(ssl.create_default_context().verify_flags
                    & ssl.VERIFY_X509_STRICT)
    except AttributeError:
        return False


def make_context(relaxed=False):
    """
    建立 SSL context。

    relaxed=True 時移除 VERIFY_X509_STRICT,但保留:
      - verify_mode = CERT_REQUIRED(仍要求有效憑證)
      - check_hostname = True(仍驗證主機名稱)
      - 完整憑證鏈與有效期限檢查
    """
    key = bool(relaxed)
    if key in _ctx_cache:
        return _ctx_cache[key]
    ctx = ssl.create_default_context()
    if relaxed and hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    _ctx_cache[key] = ctx
    return ctx


def _is_relaxed_host(url):
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in RELAXED_HOSTS)


def urlopen(url, timeout=30, headers=None):
    """
    開啟 URL。對白名單網域,若因嚴格憑證檢查失敗則自動以放寬模式重試一次。
    """
    req = urllib.request.Request(url, headers=headers or UA)
    try:
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=make_context(False))
    except urllib.error.URLError as e:
        msg = str(getattr(e, "reason", e))
        is_strict_err = ("CERTIFICATE_VERIFY_FAILED" in msg
                         and ("Subject Key Identifier" in msg
                              or "Authority Key Identifier" in msg
                              or "strict" in msg.lower()))
        if not (is_strict_err and _is_relaxed_host(url)):
            raise
        # 官方網域 + 已知的嚴格模式相容性問題 → 放寬該項後重試
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=make_context(True))


class EmptyResponse(ValueError):
    """伺服器回 200 但內容為空 —— 通常代表該日資料尚未公布,而非錯誤。"""


def urlopen_post(url, data, timeout=30, headers=None):
    """POST(給 Telegram 等用)。同樣套用白名單放寬邏輯。"""
    req = urllib.request.Request(url, data=data, headers=headers or UA)
    try:
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=make_context(False))
    except urllib.error.URLError as e:
        msg = str(getattr(e, "reason", e))
        if not ("CERTIFICATE_VERIFY_FAILED" in msg
                and ("Subject Key Identifier" in msg
                     or "Authority Key Identifier" in msg)
                and _is_relaxed_host(url)):
            raise
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=make_context(True))


def get_json(url, timeout=30, headers=None):
    """取得 JSON。這是三個資料模組共用的入口。"""
    with urlopen(url, timeout=timeout, headers=headers) as r:
        body = r.read().decode("utf-8").strip()
    if not body:
        raise EmptyResponse(f"回應為空:{url}")
    return json.loads(body)


def diagnose():
    """檢查本機連線環境。安裝後先跑這個。"""
    print(f"Python {sys.version.split()[0]}")
    print(f"OpenSSL {ssl.OPENSSL_VERSION}")
    strict = strict_flag_is_default()
    print(f"預設啟用 VERIFY_X509_STRICT:{'是(3.13+)' if strict else '否'}")
    if strict:
        print("  → 證交所憑證缺少 Subject Key Identifier,本模組會自動處理")
    print()
    from datetime import datetime, timedelta
    d0 = datetime.now().strftime("%Y%m%d")
    targets = [
        ("個股日成交", "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", None),
        ("本益比殖利率", "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", None),
        ("三大法人 T86", "https://openapi.twse.com.tw/v1/fund/T86",
         f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d0}&selectType=ALL&response=json"),
        ("融資融券", "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN",
         f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={d0}&selectType=ALL&response=json"),
        ("月營收(上市)", "https://openapi.twse.com.tw/v1/opendata/t187ap05_L", None),
    ]
    ok = pending = bad = 0
    for name, url, fallback in targets:
        try:
            d = get_json(url, timeout=25)
            n = len(d) if isinstance(d, list) else "?"
            if n == 0:
                raise EmptyResponse("空清單")
            print(f"  ✓ {name}:{n} 筆")
            ok += 1
            continue
        except (EmptyResponse, json.JSONDecodeError):
            reason = "尚未公布"
        except urllib.error.HTTPError as e:
            reason = f"HTTP {e.code}"
        except Exception as e:
            print(f"  ✗ {name}:{type(e).__name__}: "
                  f"{str(getattr(e, 'reason', e))[:80]}")
            bad += 1
            continue

        # OpenAPI 只提供「最新一期」,收盤前或非交易日會是空的。
        # 有備援端點的就實際測一次,確認資料真的拿得到。
        if fallback:
            got = None
            for back in range(0, 6):
                day = (datetime.now() - timedelta(days=back)).strftime("%Y%m%d")
                try:
                    r = get_json(fallback.replace(d0, day), timeout=25)
                    if r.get("stat") == "OK":
                        nn = sum(len(t.get("data", [])) for t in r.get("tables", [])) \
                             or len(r.get("data", []))
                        got = (day, nn)
                        break
                except Exception:
                    continue
            if got:
                print(f"  ✓ {name}:OpenAPI {reason},備援端點正常"
                      f"({got[0]} 共 {got[1]} 筆)")
                ok += 1
            else:
                print(f"  ✗ {name}:OpenAPI 與備援端點皆無資料")
                bad += 1
        else:
            print(f"  ⏳ {name}:{reason}(收盤後或次月才更新,屬正常)")
            pending += 1

    print()
    if bad == 0 and pending == 0:
        print("✓ 全部正常,可以開始使用。")
    elif bad == 0:
        print(f"✓ 可以使用。{pending} 個端點尚未公布當期資料,收盤後會更新。")
    else:
        print(f"⚠ {bad} 個端點異常。檢查網路、防火牆或公司代理伺服器。")
    return ok


if __name__ == "__main__":
    diagnose()
