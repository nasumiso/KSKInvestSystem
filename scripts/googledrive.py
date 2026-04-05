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
import csv
import threading
import fcntl

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

# Sheets API でセル更新する対象とタブ名の設定
SHEETS_CONFIG = {
    "shintakane_result": {"sheet_name": "shintakane_result"},
    "code_rank": {"sheet_name": "code_rank"},
}


def get_drive_service():
    store = oauth2client.file.Storage(CREDENTIAL_FILE)
    if not store:
        log_warning(" GoogleDrive認証ファイルがありません。", CREDENTIAL_FILE)
        return None
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
    log_print("%sをGoogleDriveに新規アップロードします" % csv_name)
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

    log_print("Upload Complete File ID: %s %s" % (fname, uploaded_file.get("id")))


def upload_csv(csv_name, up_file_name):
    log_print("%sをGoogleDriveに更新アップロードします" % csv_name)
    drive_service = get_drive_service()

    file_id = FILE_DICT[up_file_name]
    try:
        file_metadata = drive_service.files().get(fileId=file_id).execute()
    except httplib2.ResponseNotReady as e:
        log_warning(" GoogleDrive接続エラー", e)
        return
    del file_metadata["id"]
    log_print("meta:", file_metadata)

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

    log_print("Upload Complete File ID: %s" % updated_file.get("id"))


def _col_num_to_letter(n):
    """列番号→列記号変換（1=A, 26=Z, 27=AA, ...）"""
    result = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_sheets_service():
    """OAuth2 認証を行い、Google Sheets サービスオブジェクトを返す"""
    store = oauth2client.file.Storage(CREDENTIAL_FILE)
    if not store:
        log_warning(" GoogleDrive認証ファイルがありません。", CREDENTIAL_FILE)
        return None
    creds = store.get()
    if not creds or creds.invalid:
        flow = oauth2client.client.flow_from_clientsecrets(CLIENT_SECRET_FILE, SCOPES)
        flow.user_agent = APPLICATION_NAME
        creds = oauth2client.tools.run_flow(flow, store)
    return build("sheets", "v4", http=creds.authorize(httplib2.Http()))


def upload_csv_via_sheets(csv_name, up_file_name):
    """Sheets API でセルデータのみ更新する（スプレッドシート設定を保持）"""
    log_print("%sをSheets APIでセル更新します" % csv_name)
    sheets_service = get_sheets_service()
    spreadsheet_id = FILE_DICT[up_file_name]

    # CSV読み込み
    with open(csv_name, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        values = list(reader)

    # SHEETS_CONFIG からタブ名を取得
    sheet_name = SHEETS_CONFIG[up_file_name]["sheet_name"]

    # 対象タブの情報を取得（余剰データクリア用）
    meta = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties"
    ).execute()
    target_sheet = None
    for s in meta["sheets"]:
        if s["properties"]["title"] == sheet_name:
            target_sheet = s
            break
    if target_sheet is None:
        raise ValueError("タブ '%s' が見つかりません: %s" % (sheet_name, up_file_name))
    total_rows = target_sheet["properties"]["gridProperties"]["rowCount"]
    total_cols = target_sheet["properties"]["gridProperties"]["columnCount"]

    # 1) データを上書き（失敗してもシートは空にならない）
    update_range = "'%s'!A1" % sheet_name
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=update_range,
        valueInputOption="USER_ENTERED",
        body={"values": values}
    ).execute()

    # 2) 余剰データをクリア（行数・列数が減った場合に対応）
    new_row_count = len(values)
    new_col_count = max(len(row) for row in values) if values else 0

    # 余剰行をクリア
    if total_rows > new_row_count:
        clear_range = "'%s'!A%d:ZZ%d" % (
            sheet_name, new_row_count + 1, total_rows
        )
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=clear_range,
            body={}
        ).execute()

    # 余剰列をクリア（データ行の範囲内で、新データより右の列）
    if total_cols > new_col_count and new_row_count > 0:
        from_col = _col_num_to_letter(new_col_count + 1)
        clear_col_range = "'%s'!%s1:ZZ%d" % (
            sheet_name, from_col, new_row_count
        )
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=clear_col_range,
            body={}
        ).execute()

    log_print("Sheets API更新完了: %s (%d行)" % (up_file_name, len(values)))


def upload_html(html_path):
    """HTMLファイルをGoogleDriveの「投資データ」フォルダにアップロードする"""
    log_print("%sをGoogleDriveにアップロードします" % html_path)
    drive_service = get_drive_service()
    folder_id = FOLDER_DICT["投資データ"]
    fname = os.path.basename(html_path)

    # 既存ファイルを検索（同名ファイルがあれば上書き更新）
    results = drive_service.files().list(
        q="name='%s' and '%s' in parents and trashed=false" % (fname, folder_id),
        fields="files(id)"
    ).execute()
    files = results.get("files", [])

    media = MediaFileUpload(html_path, mimetype="text/html", resumable=True)

    if files:
        # 既存ファイルを更新
        file_id = files[0]["id"]
        drive_service.files().update(fileId=file_id, media_body=media).execute()
        log_print("Upload(更新) Complete: %s" % fname)
    else:
        # 新規作成
        file_metadata = {"name": fname, "parents": [folder_id]}
        drive_service.files().create(
            body=file_metadata, media_body=media, fields="id"
        ).execute()
        log_print("Upload(新規) Complete: %s" % fname)


# --- 非同期アップロード機構 ---
# スレッド間排他（同一プロセス内）
_upload_lock = threading.Lock()
# 実行中スレッドの追跡
_upload_threads = []
# スレッド内で発生した例外を収集
_upload_errors = []
# プロセス間排他用ロックファイル
_LOCK_FILE = os.path.join(DATA_DIR, "googledrive/.upload_lock")


def _upload_with_lock(func, *args):
    """ファイルロック付きアップロード（プロセス間 + スレッド間排他）"""
    try:
        with _upload_lock:
            with open(_LOCK_FILE, "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    func(*args)
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception as e:
        log_warning("GoogleDriveアップロード失敗: %s" % e)
        _upload_errors.append(e)


def upload_csv_async(csv_name, up_file_name):
    """CSV非同期アップロード。スレッドを起動して即座に返る。"""
    # Sheets API 対象はセル更新、それ以外は従来の Drive API アップロード
    upload_func = upload_csv_via_sheets if up_file_name in SHEETS_CONFIG else upload_csv
    t = threading.Thread(
        target=_upload_with_lock,
        args=(upload_func, csv_name, up_file_name),
        daemon=False,
    )
    _upload_threads.append(t)
    t.start()
    return t


def upload_html_async(html_path):
    """HTML非同期アップロード。スレッドを起動して即座に返る。"""
    t = threading.Thread(
        target=_upload_with_lock,
        args=(upload_html, html_path),
        daemon=False,
    )
    _upload_threads.append(t)
    t.start()
    return t


def wait_all_uploads(timeout=300):
    """全アップロードスレッドの完了を待つ。失敗・タイムアウトがあれば例外を送出。"""
    timed_out = []
    for t in _upload_threads:
        t.join(timeout=timeout)
        if t.is_alive():
            log_warning("アップロードスレッドがタイムアウトしました: %s" % t.name)
            timed_out.append(t.name)
    _upload_threads.clear()
    all_errors = list(_upload_errors)
    _upload_errors.clear()
    if timed_out:
        all_errors.append(
            TimeoutError("タイムアウト: %s" % ", ".join(timed_out))
        )
    if all_errors:
        raise RuntimeError(
            "GoogleDriveアップロードで%d件の失敗: %s" % (len(all_errors), all_errors)
        )


def main():
    # ロガーの初期化
    logger = setup_logger('shintakane')

    # upload_csv('code_rank_data/code_rank.csv', "code_rank")
    upload_csv(os.path.join(DATA_DIR, "code_rank_data/market_data.csv"), "market_data")


if __name__ == "__main__":
    main()
