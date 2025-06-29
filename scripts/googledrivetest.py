#!/usr/bin/env python3
# flake8: noqa

from apiclient.discovery import build
from apiclient.http import MediaFileUpload
import oauth2client
from oauth2client import file, tools  # 追加
import httplib2

# 4/0wGiFKpmGzvDx1swYFXflXLcM8xBiFX6ZiTnH6Vvc5KguwQ4BSKalcA

# CLIENT_SECRET_FILE = 'client_secret_152733296438-p7d1thkqdnmh2ip0r9695cdoisigdvjd.apps.googleusercontent.com.json'
CLIENT_SECRET_FILE = "client_secret_152733296438-n9openvtegg2r6ej4mfdn8t4guf77ejs.apps.googleusercontent.com.json"
# CLIENT_SECRET_FILE = 'My Project-d080eb2b84c1.json'
CREDENTIAL_FILE = "drive_credential.json"
APPLICATION_NAME = "CSVUploader"

SCOPES = "https://www.googleapis.com/auth/drive"  # Quickstarts と スコープを変える

store = oauth2client.file.Storage(CREDENTIAL_FILE)
creds = store.get()
if not creds or creds.invalid:
    flow = oauth2client.client.flow_from_clientsecrets(CLIENT_SECRET_FILE, SCOPES)
    flow.user_agent = APPLICATION_NAME
    creds = oauth2client.tools.run_flow(flow, store)
drive_service = build(
    "drive", "v3", http=creds.authorize(httplib2.Http())
)  # Setup the Drive v3 API

file_metadata = {
    "name": "My Report",
    "mimeType": "application/vnd.google-apps.spreadsheet",
}
media = MediaFileUpload("googledrivetest.csv", mimetype="text/csv", resumable=True)


file = (
    drive_service.files()
    .create(body=file_metadata, media_body=media, fields="id")
    .execute()
)
log_print("File ID: %s" % file.get("id"))
