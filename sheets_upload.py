"""
Direct upload to an existing Google Sheet via the Sheets API.

This sidesteps Google Sheets' file-import size limits entirely: instead of
writing a CSV and having the browser upload it, rows are pushed straight
into the sheet through the API, in small batches.

Requires (not needed for CSV splitting, only for this upload path):
    pip install google-auth google-auth-oauthlib google-api-python-client

One-time setup, per Google account:
1. Go to https://console.cloud.google.com/ and create a project (or pick
   an existing one).
2. Enable the "Google Sheets API" for it: APIs & Services > Library.
3. Create credentials: APIs & Services > Credentials > Create Credentials
   > OAuth client ID > Application type "Desktop app".
4. Download the resulting JSON and save it as "client_secret.json" in the
   same folder as csv_splitter.py (or next to the .exe, if using the
   built app).
5. The first upload opens a browser to sign in and grant access. After
   that, a cached token is reused automatically and no browser prompt is
   needed again (unless access is revoked).
"""

import csv
import os
import re
import sys
import time

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Target cell count per API request: small enough to keep each call fast
# and comfortably under the API's request size limits, regardless of how
# wide the CSV is.
BATCH_CELLS_TARGET = 500_000
MIN_BATCH_ROWS = 100
MAX_BATCH_ROWS = 10_000


class SheetsUploadError(Exception):
    pass


def _missing_packages_error(original=None):
    return SheetsUploadError(
        "Missing packages for Google Sheets upload. Install them with:\n\n"
        "pip install google-auth google-auth-oauthlib google-api-python-client"
    )


def get_app_dir():
    """Directory to look for client_secret.json in (next to the script or .exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_token_path():
    """Where the cached OAuth token is stored (persists across exe relaunches)."""
    config_dir = os.path.join(os.path.expanduser("~"), ".csv_splitter")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "token.json")


def extract_spreadsheet_id(text):
    """Accept either a raw spreadsheet ID or a full Google Sheets URL."""
    text = text.strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", text)
    return match.group(1) if match else text


def get_credentials():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise _missing_packages_error(e) from e

    client_secret_path = os.path.join(get_app_dir(), "client_secret.json")
    token_path = get_token_path()

    creds = None
    if os.path.isfile(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.isfile(client_secret_path):
                raise SheetsUploadError(
                    "No Google API credentials found.\n\n"
                    f"Save your OAuth client JSON as:\n{client_secret_path}\n\n"
                    "See the README for setup steps (one-time, per Google account)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def count_csv_columns(path):
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        first_row = next(csv.reader(f), [])
    return max(len(first_row), 1)


def _batch_size_for_columns(num_columns):
    size = BATCH_CELLS_TARGET // max(num_columns, 1)
    return max(MIN_BATCH_ROWS, min(MAX_BATCH_ROWS, size))


def _append_chunk(service, spreadsheet_id, sheet_name, chunk):
    from googleapiclient.errors import HttpError

    body = {"values": chunk}
    for attempt in range(5):
        try:
            service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"{sheet_name}!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body=body,
            ).execute()
            return
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 500, 503) and attempt < 4:
                time.sleep(2**attempt)
                continue
            raise SheetsUploadError(f"Google Sheets API error: {e}") from e


def upload_csv_to_sheet(csv_path, spreadsheet_id_or_url, sheet_name, has_header, progress_callback=None):
    """Stream a CSV's rows into an existing Google Sheet. Returns rows uploaded."""
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise _missing_packages_error(e) from e

    spreadsheet_id = extract_spreadsheet_id(spreadsheet_id_or_url)
    if not spreadsheet_id:
        raise SheetsUploadError("Please enter a Google Sheet URL or ID.")

    num_columns = count_csv_columns(csv_path)
    batch_rows = _batch_size_for_columns(num_columns)

    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        total_rows = sum(1 for _ in csv.reader(f))
    if has_header and total_rows > 0:
        total_rows -= 1
    total_rows = max(total_rows, 0)

    creds = get_credentials()
    service = build("sheets", "v4", credentials=creds)

    uploaded = 0
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        if has_header:
            next(reader, None)

        chunk = []
        for row in reader:
            chunk.append(row)
            if len(chunk) >= batch_rows:
                _append_chunk(service, spreadsheet_id, sheet_name, chunk)
                uploaded += len(chunk)
                if progress_callback:
                    progress_callback(uploaded, total_rows)
                chunk = []
        if chunk:
            _append_chunk(service, spreadsheet_id, sheet_name, chunk)
            uploaded += len(chunk)
            if progress_callback:
                progress_callback(uploaded, total_rows)

    return uploaded
