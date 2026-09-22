#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Drive 同步:在 GitHub Actions 這種每次都是全新環境的地方,
把 chips.db / trades.db 從指定的 Drive 資料夾下載下來(跑之前)、
跑完再上傳回去,讓資料可以跨執行累積。

需要的環境變數:
    GDRIVE_SA_KEY    服務帳號金鑰 JSON(整份內容,不是檔案路徑)
    GDRIVE_FOLDER_ID 已分享給該服務帳號的 Drive 資料夾 ID

用法:
    python gdrive_sync.py pull   # 下載 chips.db / trades.db(不存在就略過)
    python gdrive_sync.py push   # 上傳 chips.db / trades.db(不存在就略過)
"""

import io
import json
import os
import sys

FILES = ["chips.db", "trades.db"]

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    key_json = os.environ.get("GDRIVE_SA_KEY")
    if not key_json:
        print("錯誤:未設定 GDRIVE_SA_KEY 環境變數", file=sys.stderr)
        sys.exit(1)
    info = json.loads(key_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _folder_id():
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    if not folder_id:
        print("錯誤:未設定 GDRIVE_FOLDER_ID 環境變數", file=sys.stderr)
        sys.exit(1)
    return folder_id


def _find_file(svc, name, folder_id):
    q = f"'{folder_id}' in parents and name = '{name}' and trashed = false"
    res = svc.files().list(q=q, spaces="drive", fields="files(id, name)").execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def pull():
    from googleapiclient.http import MediaIoBaseDownload

    svc = _service()
    folder_id = _folder_id()
    for name in FILES:
        file_id = _find_file(svc, name, folder_id)
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
    folder_id = _folder_id()
    for name in FILES:
        if not os.path.exists(name):
            print(f"[push] {name} 不存在,略過")
            continue
        file_id = _find_file(svc, name, folder_id)
        media = MediaFileUpload(name, resumable=True)
        if file_id:
            svc.files().update(fileId=file_id, media_body=media).execute()
        else:
            metadata = {"name": name, "parents": [folder_id]}
            svc.files().create(body=metadata, media_body=media, fields="id").execute()
        print(f"[push] {name} 已上傳 ({os.path.getsize(name)} bytes)")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("pull", "push"):
        print("用法: python gdrive_sync.py [pull|push]", file=sys.stderr)
        sys.exit(1)
    {"pull": pull, "push": push}[sys.argv[1]]()
