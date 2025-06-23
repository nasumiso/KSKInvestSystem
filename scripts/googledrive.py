#!/usr/bin/env python3

"""
このスクリプトは、Google Drive API を使用して CSV ファイルを Google スプレッドシートとして Google Drive にアップロードするユーティリティ関数を提供します。

モジュールと関数:
- get_drive_service(): OAuth2 認証を行い、Google Drive サービスオブジェクトを返します。
- upload_csv_new(csv_name, up_foler_name): 指定したフォルダに新規で CSV ファイルを Google スプレッドシートとしてアップロードします。
- upload_csv(csv_name, up_file_name): 既存の Google スプレッドシートを新しい CSV ファイルで更新します。
- main(): upload_csv 関数を使った "market_data" ファイルの更新例です。

使い方:
スクリプトを直接実行することで、CSV ファイルを Google Drive に Google スプレッドシートとしてアップロードまたは更新できます。
"""  # noqa: E501
from ks_util import *

# 外部ライブラリ GoogleDriveAPI
from apiclient.discovery import build  # type: ignore Pylanceが認識しない
from apiclient.http import MediaFileUpload  # type: ignore

# 外部ライブラリ 認証用API google-authへ移行すべきらしい
import oauth2client
from oauth2client import file, tools  # noqa: F401 使ってるんだが・・
import httplib2

# https://dev.classmethod.jp/articles/upload-csv-file-to-google-spreadsheet/
# よりGoogleDriveAPIでCSVをアップロード
# ---- 設定ファイル
CLIENT_SECRET_FILE = os.path.join(
    DATA_DIR,
    "googledrive/client_secret_152733296438-n9openvtegg2r6ej4mfdn8t4guf77ejs.apps.googleusercontent.com.json",  # noqa: E501
)
# CLIENT_SECRET_FILE = 'My Project-d080eb2b84c1.json'
CREDENTIAL_FILE = os.path.join(DATA_DIR, "googledrive/drive_credential.json")
APPLICATION_NAME = "CSVUploader"

SCOPES = "https://www.googleapis.com/auth/drive"  # Quickstarts と スコープを変える

FOLDER_DICT = {
    "投資データ": "1CvpiB0bV4mK8DLR_LBQmeCXKgrYHOJZr",
    "新高値": "1_BDjAcRNWRsNPtu2yrJq3jNko2Qk8hQP",
}
FILE_DICT = {
    "shintakane_result": "1KxOFvfgT7o_XGDASGylxA0Rn9yEqPLSv6Yweb_jGsHk",
    "code_rank": "1zto-8-fZ5hTZfXY6k2C49HZHbyA3OE8BgkReAViLSNU",
    "market_data": "1AFzVywuX_iEiPH7XL84USK9i_E0HgBNTqSVy558i3G0",
}


def get_drive_service():
    store = oauth2client.file.Storage(CREDENTIAL_FILE)
    creds = store.get()
    if not creds or creds.invalid:
        flow = oauth2client.client.flow_from_clientsecrets(CLIENT_SECRET_FILE, SCOPES)
        flow.user_agent = APPLICATION_NAME
        creds = oauth2client.tools.run_flow(flow, store)
    drive_service = build(
        "drive", "v3", http=creds.authorize(httplib2.Http())
    )  # Setup the Drive v3 API
    return drive_service


def upload_csv_new(csv_name, up_foler_name):
    print("%sをGoogleDriveに新規アップロードします" % csv_name)
    drive_service = get_drive_service()

    folder_id = FOLDER_DICT[up_foler_name]
    fname = os.path.basename(csv_name).split(".")[0]
    file_metadata_create = {
        "name": fname,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [folder_id],
    }
    media = MediaFileUpload(csv_name, mimetype="text/csv", resumable=True)

    uploaded_file = (
        drive_service.files()
        .create(body=file_metadata_create, media_body=media, fields="id")
        .execute()
    )

    print("Upload Complete File ID: %s %s" % (fname, uploaded_file.get("id")))


def upload_csv(csv_name, up_file_name):
    print("%sをGoogleDriveに更新アップロードします" % csv_name)
    drive_service = get_drive_service()

    file_id = FILE_DICT[up_file_name]
    try:
        file_metadata = drive_service.files().get(fileId=file_id).execute()
    except httplib2.ResponseNotReady as e:
        print("!!! GoogleDrive接続エラー", e)
        return
    del file_metadata["id"]
    print("meta:", file_metadata)

    media = MediaFileUpload(csv_name, mimetype="text/csv", resumable=True)
    updated_file = (
        drive_service.files()
        .update(
            fileId=file_id,
            body=file_metadata,
            media_body=media,
            # fields='id'
        )
        .execute()
    )

    print("Upload Complete File ID: %s" % updated_file.get("id"))


def main():
    # upload_csv('code_rank_data/code_rank.csv', "code_rank")
    upload_csv(os.path.join(DATA_DIR, "code_rank_data/market_data.csv"), "market_data")


if __name__ == "__main__":
    main()
