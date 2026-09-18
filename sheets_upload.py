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

# Google Sheets' hard, non-negotiable cap: a spreadsheet's cell count is its
# allocated grid size (rows x columns, summed across every sheet/tab in it),
# not just the cells that hold data. This is a real platform ceiling — no
# amount of batching or splitting works around it once a workbook is full.
GOOGLE_SHEETS_CELL_LIMIT = 10_000_000


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


def _execute_with_retry(build_request):
    from googleapiclient.errors import HttpError

    for attempt in range(5):
        try:
            return build_request().execute()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 500, 503) and attempt < 4:
                time.sleep(2**attempt)
                continue
            raise SheetsUploadError(f"Google Sheets API error: {e}") from e


def _is_cell_limit_error(exc):
    text = str(exc)
    return "would increase the number of cells" in text or "above the limit of" in text


def _get_workbook_cell_usage(service, spreadsheet_id):
    """Sum each sheet/tab's allocated grid size (rows x columns) — what
    actually counts against the 10,000,000-cell limit, whether or not those
    cells hold data."""
    meta = _execute_with_retry(
        lambda: service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties.gridProperties"
        )
    )
    total = 0
    for sheet in meta.get("sheets", []):
        grid = sheet.get("properties", {}).get("gridProperties", {})
        total += grid.get("rowCount", 0) * grid.get("columnCount", 0)
    return total


def _clear_sheet(service, spreadsheet_id, sheet_name):
    """Wipe a sheet/tab and shrink it back down to reclaim its cell budget.

    Clearing values alone (spreadsheets.values.clear) does NOT free up space
    against the 10,000,000-cell limit — Google Sheets counts a sheet's
    allocated grid dimensions, not just its filled cells, and clearing
    values leaves those dimensions untouched. Deleting the rows outright is
    the only way to actually shrink the grid.
    """
    meta = _execute_with_retry(
        lambda: service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties(sheetId,title,gridProperties)",
        )
    )
    sheet_props = next(
        (s["properties"] for s in meta.get("sheets", []) if s["properties"]["title"] == sheet_name),
        None,
    )
    if sheet_props is None:
        raise SheetsUploadError(f'Sheet/tab "{sheet_name}" was not found in this spreadsheet.')

    _execute_with_retry(
        lambda: service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id, range=sheet_name, body={}
        )
    )

    row_count = sheet_props.get("gridProperties", {}).get("rowCount", 1)
    if row_count > 1:
        _execute_with_retry(
            lambda: service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "deleteDimension": {
                                "range": {
                                    "sheetId": sheet_props["sheetId"],
                                    "dimension": "ROWS",
                                    "startIndex": 1,
                                    "endIndex": row_count,
                                }
                            }
                        }
                    ]
                },
            )
        )


def _append_chunk(service, spreadsheet_id, sheet_name, chunk):
    body = {"values": chunk}
    _execute_with_retry(
        lambda: service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        )
    )


def _append_chunk_tracked(service, spreadsheet_id, sheet_name, chunk, uploaded_so_far, total_rows):
    try:
        _append_chunk(service, spreadsheet_id, sheet_name, chunk)
    except SheetsUploadError as e:
        if _is_cell_limit_error(e):
            raise SheetsUploadError(
                f"Uploaded {uploaded_so_far:,} of {total_rows:,} row(s) before hitting "
                f"Google Sheets' {GOOGLE_SHEETS_CELL_LIMIT:,}-cell-per-spreadsheet limit. "
                "This spreadsheet is full.\n\n"
                'To upload the rest: check "Clear existing sheet contents first" and '
                "re-upload (this deletes this sheet/tab's existing rows to reclaim "
                "space), trim the CSV, or upload the remainder into a separate Google "
                "Sheet."
            ) from e
        raise


def upload_csv_to_sheet(csv_path, spreadsheet_id_or_url, sheet_name, has_header,
                         progress_callback=None, clear_first=False):
    """Stream a CSV's rows into an existing Google Sheet. Returns rows uploaded.

    By default rows are appended after whatever is already in the sheet.
    Pass clear_first=True to wipe the sheet's existing contents first.
    """
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

    if clear_first:
        _clear_sheet(service, spreadsheet_id, sheet_name)

    # Fail fast with a clear, actionable message instead of discovering the
    # workbook is full partway through a long upload.
    try:
        current_cells = _get_workbook_cell_usage(service, spreadsheet_id)
    except SheetsUploadError:
        current_cells = None  # don't block the upload if this check itself fails

    if current_cells is not None and total_rows > 0:
        incoming_cells = total_rows * num_columns
        remaining = GOOGLE_SHEETS_CELL_LIMIT - current_cells
        if incoming_cells > remaining:
            max_rows = max(0, remaining // num_columns)
            raise SheetsUploadError(
                f"This won't fit: the spreadsheet already uses {current_cells:,} of "
                f"{GOOGLE_SHEETS_CELL_LIMIT:,} cells, leaving room for about "
                f"{max_rows:,} more row(s) here, but this CSV has {total_rows:,}.\n\n"
                'Options: check "Clear existing sheet contents first" to reclaim this '
                "sheet/tab's space, trim the CSV, or upload the remainder into a "
                "separate Google Sheet."
            )

    uploaded = 0
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        if has_header:
            next(reader, None)

        chunk = []
        for row in reader:
            chunk.append(row)
            if len(chunk) >= batch_rows:
                _append_chunk_tracked(service, spreadsheet_id, sheet_name, chunk, uploaded, total_rows)
                uploaded += len(chunk)
                if progress_callback:
                    progress_callback(uploaded, total_rows)
                chunk = []
        if chunk:
            _append_chunk_tracked(service, spreadsheet_id, sheet_name, chunk, uploaded, total_rows)
            uploaded += len(chunk)
            if progress_callback:
                progress_callback(uploaded, total_rows)

    return uploaded
