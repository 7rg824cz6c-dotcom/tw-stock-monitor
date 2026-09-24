# 科技產業情報蒐集器

抓取國內外科技新聞、訂單動能、技術創新、地緣事件，用 LLM 擷取重點與分類，
把國外消息反推到相關台廠，每日彙整成 Gmail 摘要。

## 檔案清單

| 檔案 | 用途 | 要不要改 |
|---|---|---|
| `crawler.py` | 主程式 | 不用 |
| `sources.yaml` | 31 個來源、關鍵字、垃圾稿過濾樣式 | feed 失效時改 |
| `supply_chain_map.yaml` | 國外廠商與地緣事件 → 台廠對照表 | **建議每季維護** |
| `check_feeds.py` | 來源診斷工具 | 不用 |
| `.env.example` | 設定範本 | 複製成 `.env` 後填 |
| `requirements.txt` | 套件清單 | 不用 |
| `run.bat` | 排程用啟動檔 | 路徑要對 |
| `.gitignore` | 排除 `.env` 等 | 不用 |

程式自動產生：`.venv/`（虛擬環境）、`news.db`（資料庫）、`run.log`（執行記錄）。

## 安裝

```powershell
cd C:\dev\news_crawler
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

VS Code：`Ctrl+Shift+P` → `Python: Create Environment` → Venv → 勾 requirements.txt。

## 設定

`.env.example` 複製成 `.env` 填入金鑰。

**LLM（免費選項）**

| 供應商 | 取得方式 | 備註 |
|---|---|---|
| `gemini` | aistudio.google.com | 免信用卡，中文品質最佳，**推薦** |
| `groq` | console.groq.com | 免信用卡，塞車時的備援 |
| `ollama` | 本機安裝 | 無額度限制，吃顯卡、較慢 |
| `none` | 不需金鑰 | 只分類不摘要，功能減半 |

型號可用逗號列多個，額度滿或塞車時自動輪替：

```
GEMINI_MODEL=gemini-3.5-flash,gemini-3.1-flash-lite,gemini-flash-latest
```

不要在 Google Cloud 專案啟用帳單，免費額度會消失。

**Gmail**：Google 帳戶 → 安全性 → 開兩步驟驗證 → 申請「應用程式密碼」，
16 碼填進 `GMAIL_APP_PASSWORD`（不是登入密碼）。

## 執行

```powershell
python crawler.py                  # 抓取 + 分析 + 寄信
python crawler.py --no-mail        # 結果印在畫面
python crawler.py --analyze-only   # 不重抓，只分析待處理的
python crawler.py --report         # 只用既有結果重寄摘要（不耗額度）
python check_feeds.py              # 檢查所有來源是否存活
python check_feeds.py ASML MOPS    # 只檢查特定來源
```

## 排程

工作排程器 → 建立基本工作 → 每天 → 啟動程式 → 選 `run.bat`。
早上 08:00（趕在台股開盤前收到美股盤後消息）與晚上 20:00 各一次。

必要設定：
- 「一般」分頁勾「不論使用者是否登入均執行」
- 「條件」分頁取消「只有在使用 AC 電源時才啟動」（想用電池時也跑的話）
- 「設定」分頁勾「錯過排定的啟動後盡快執行」

`run.bat` 已含 `chcp 65001` 與 `PYTHONIOENCODING`，缺少會因 cp950 編碼崩潰。

## 來源分級

| 分級 | 標記 | importance 上限 | 進台廠彙總 |
|---|---|---|---|
| official / media | — | 5 | 是 |
| statement 人物發言 | 🗣 | 4 | 否 |
| social 社群討論 | 💬 | 3 | 否 |

發言是意向、社群是傳聞，都不該與官方公告等量齊觀，因此不計入彙總。

## 設計重點

- 只讀 RSS 與官方 API，付費牆站不抓正文，請求間隔 3 秒
- 三層去重：URL 正規化、標題詞彙交集、摘要階段以公司名稱合併同事件
- 批次分析：一次送 6 則，大幅減少 API 呼叫次數
- 地區分流：台灣新聞不附供應鏈對照表，省 token
- 36 條垃圾稿樣式在進資料庫前攔截市調報告與券商噪音
- 地緣事件（戰爭、天災、原料管制）獨立追蹤，並附傳導路徑對照

## 維護

- `supply_chain_map.yaml` 是準確度的天花板，每季法說會後校對
- 每月跑一次 `python check_feeds.py` 檢查失效來源
- 免費 LLM 型號代碼常改，404 時到後台確認現行名稱
- 摘要太吵就把 `crawler.py` 裡 `build_report` 的 `min_importance` 調到 4

## 注意

自用性質。公開發布摘要、商業使用或對外提供個股分析，涉及的法律問題不同，
需另行評估。社群與發言類來源為未經查證的資訊，不應直接作為決策依據。
