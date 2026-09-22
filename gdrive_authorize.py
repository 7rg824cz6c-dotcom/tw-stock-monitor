#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性設定用。在你自己電腦上跑(不是 CI),用來取得長期有效的
refresh token,讓 GitHub Actions 可以用「你本人帳號」存取 Google Drive
(而不是沒有儲存空間配額的 service account)。

前置作業(Google Cloud Console,跟建立 service account 用同一個專案即可):
  1. 「API 和服務」→ OAuth consent screen → 設定 User Type: External,
     填 App name / 你的信箱,Scopes 加入 .../auth/drive.file,
     Test users 加你自己的信箱 → 儲存後點「PUBLISH APP」發布成 Production
     (drive.file 不是敏感範圍,不需要 Google 審核,但不發布的話 refresh
     token 只活 7 天,發布後才會長期有效)。
  2. 「憑證」→ 建立憑證 → OAuth 用戶端 ID → 應用程式類型選「電腦版應用程式」
     → 建立 → 下載那份 JSON。

用法:
    python gdrive_authorize.py path/to/client_secret.json

執行後會開瀏覽器要你登入 Google 帳號並同意授權,完成後終端機會印出
三個值,複製貼到 GitHub Secrets:GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET
/ GDRIVE_REFRESH_TOKEN。
"""

import sys

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main():
    if len(sys.argv) != 2:
        print("用法: python gdrive_authorize.py path/to/client_secret.json", file=sys.stderr)
        sys.exit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n設定完成,請把以下 3 個值貼到 GitHub Secrets:\n")
    print(f"GDRIVE_CLIENT_ID={creds.client_id}")
    print(f"GDRIVE_CLIENT_SECRET={creds.client_secret}")
    print(f"GDRIVE_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()
