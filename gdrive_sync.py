#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Drive 同步:在 GitHub Actions 這種每次都是全新環境的地方,
把 chips.db / trades.db 從你自己的 Google Drive 下載下來(跑之前)、
跑完再上傳回去,讓資料可以跨執行累積。

用你「本人帳號」的 OAuth 授權(不是 service account —— service account
沒有自己的儲存空間配額,沒辦法建立新檔案)。範圍限定在 drive.file,
也就是這個 App 只能看到「自己建立的檔案」,不會碰到你 Drive 裡其他東西。

一次性設定(在你自己電腦上跑,不是在 CI 裡):
    python gdrive_authorize.py path/to/client_secret.json
會印出 GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET / GDRIVE_REFRESH_TOKEN,
把這三個值存成 GitHub Secrets。

用法(CI 裡用):
    python gdrive_sync.py pull   # 下載 chips.db / trades.db(不存在就略過)
    python gdrive_sync.py push   # 上傳 chips.db / trades.db(不存在就略過)
"""

import io
import os
import sys

FILES = ["chips.db", "trades.db"]

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    client_id = os.environ.get("GDRIVE_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET")
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN")
    missing = [
        n
        for n, v in [
            ("GDRIVE_CLIENT_ID", client_id),
            ("GDRIVE_CLIENT_SECRET", client_secret),
            ("GDRIVE_REFRESH_TOKEN", refresh_token),
        ]
        if not v
    ]
    if missing:
        print(f"錯誤:未設定環境變數 {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_file(svc, name):
    q = f"name = '{name}' and trashed = false"
    res = svc.files().list(q=q, spaces="drive", fields="files(id, name)").execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def pull():
    from googleapiclient.http import MediaIoBaseDownload

    svc = _service()
    for name in FILES:
        file_id = _find_file(svc, name)
        if not file_id:
            print(f"[pull] {name} 在 Drive 上不存在,略過(第一次執行會這樣,正常)")
            continue
        request = svc.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        with open(name, "wb") as f:
            f.write(buf.getvalue())
        print(f"[pull] {name} 已下載 ({buf.getbuffer().nbytes} bytes)")


def push():
    from googleapiclient.http import MediaFileUpload

    svc = _service()
    for name in FILES:
        if not os.path.exists(name):
            print(f"[push] {name} 不存在,略過")
            continue
        file_id = _find_file(svc, name)
        media = MediaFileUpload(name, resumable=True)
        if file_id:
            svc.files().update(fileId=file_id, media_body=media).execute()
        else:
            metadata = {"name": name}
            svc.files().create(body=metadata, media_body=media, fields="id").execute()
        print(f"[push] {name} 已上傳 ({os.path.getsize(name)} bytes)")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("pull", "push"):
        print("用法: python gdrive_sync.py [pull|push]", file=sys.stderr)
        sys.exit(1)
    {"pull": pull, "push": push}[sys.argv[1]]()
