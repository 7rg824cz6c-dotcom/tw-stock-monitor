#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""晚上補當天的籌碼(GitHub Actions 每個交易日 21:45 執行,見 .github/workflows/chips-evening.yml)。
15:30 的每日掃描跑的時候,證交所還沒公布當天的三大法人(約 16 點多)與融資(約 21 點多),
所以籌碼一直慢一個交易日。這支程式只做一件事:把當天(與前幾天漏掉)的法人/融資資料補進 chips.db。"""
import os

import chips as C


def main():
    con = C.init_db()
    try:                                    # 證交所偶爾回空白頁;今天抓不到就交給下面的回補,不讓整個排程失敗
        n, nm = C.update_today(con)
        print(f"今天:法人 {n} 檔 / 融資 {nm} 檔")
    except Exception as e:
        print(f"今天的籌碼抓取失敗(之後回補):{type(e).__name__}: {e}")
    C.backfill(con, days=5, pause=3.0)      # 前幾天若有漏掉的也一起補
    otc_days = int(os.environ.get("OTC_DAYS") or 5)   # 手動執行時可指定一次補多天的上櫃籌碼
    C.backfill_otc(con, days=otc_days)
    fixed = C.repair_dealer(con)
    if fixed:
        print(f"修正自營商欄位 {fixed} 筆")
    last_i = con.execute("SELECT MAX(date) FROM inst").fetchone()[0]
    last_m = con.execute("SELECT MAX(date) FROM margin").fetchone()[0]
    print(f"籌碼資料最新日期:法人 {last_i} / 融資 {last_m}")
    otc = con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM otc_dates").fetchone()
    print(f"上櫃籌碼:{otc[0]} 個交易日({otc[1]} ~ {otc[2]})")
    con.close()


if __name__ == "__main__":
    main()
