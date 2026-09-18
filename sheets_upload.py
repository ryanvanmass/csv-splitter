"""
Direct upload to an existing Google Sheet via the Sheets API.

This sidesteps Google Sheets' file-import size limits entirely: instead of
writing a CSV and having the browser upload it, rows are pushed straight
into the sheet through the API, in small batches.

Google Sheets' hard, non-negotiable cap remains, though: a spreadsheet's
cell count is its allocated grid size (rows x columns, summed across every
sheet/tab in it, not just cells holding data) and can't exceed 10,000,000
— tabs within one spreadsheet share that same budget, they don't each get
their own. So when it fills up mid-upload, this module first adds a new
tab to the SAME spreadsheet and keeps going there; only once the whole
file is genuinely full does it create a new spreadsheet instead, which
does have its own independent budget. See upload_csv_to_sheet.

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


class _CellLimitHit(Exception):
    """Internal signal: this API call would exceed the 10,000,000-cell limit."""


def _is_cell_limit_error(exc):
    text = str(exc)
    return "would increase the number of cells" in text or "above the limit of" in text


def _execute_with_retry(build_request):
    from googleapiclient.errors import HttpError

    for attempt in range(5):
        try:
            return build_request().execute()
        except HttpError as e:
            if _is_cell_limit_error(e):
                raise _CellLimitHit() from e
            status = getattr(e.resp, "status", None)
            if status in (429, 500, 502, 503, 504):
                if attempt < 4:
                    time.sleep(2**attempt)
                    continue
                raise SheetsUploadError(
                    f"Google's servers returned a temporary error (HTTP {status}) and kept "
                    "failing after several retries. This is usually transient — wait a "
                    "minute and try the upload again."
                ) from e
            raise SheetsUploadError(f"Google Sheets API error: {e}") from e


def _column_letter(n):
    """Convert a 1-indexed column number to A1-style letters (1->A, 26->Z, 27->AA)."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _find_empty_suffix_boundary(is_empty_from, total):
    """Find the smallest 0-indexed k such that is_empty_from(k) is True —
    i.e. everything from position k to the end is verified empty.

    is_empty_from(k) must directly query whether that range holds any data
    (never assumed/inferred), so the result is safe to act on: no risk of
    treating a sheet with real, sparse data as trimmable.

    Searches by galloping outward from the end (checking window sizes
    1, 2, 4, 8, ...) before doing a bounded binary search in between, so
    a *small* empty suffix — the common case; someone scrolled or pasted
    a few thousand rows past their real data — costs a handful of cheap
    probes (an empty API response is cheap regardless of range size),
    rather than the first few probes of a naive top-down binary search
    landing in the middle of millions of real data rows and pulling all
    of it back over the network just to check.
    """
    if total <= 0:
        return 0

    window = 1
    checked_boundary = total  # is_empty_from(total) is trivially true (empty range)
    lo = max(0, total - window)
    while lo > 0 and is_empty_from(lo):
        checked_boundary = lo
        window *= 2
        lo = max(0, total - window)

    if lo == 0:
        if is_empty_from(0):
            return 0
        lo_bound, hi_bound = 0, checked_boundary
    else:
        lo_bound, hi_bound = lo, checked_boundary

    while lo_bound < hi_bound:
        mid = (lo_bound + hi_bound) // 2
        if is_empty_from(mid):
            hi_bound = mid
        else:
            lo_bound = mid + 1
    return lo_bound


def _trim_empty_space(service, spreadsheet_id):
    """Shrink every sheet/tab in a spreadsheet down to its actual used
    range, reclaiming cell budget from trailing empty rows/columns that
    don't hold any data (a sheet's allocated grid size counts against the
    10,000,000-cell limit regardless of whether cells are filled — see
    _clear_sheet). Always keeps at least 1 row and 1 column per sheet.

    Returns True if anything was actually trimmed.
    """
    meta = _execute_with_retry(
        lambda: service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties(sheetId,title,gridProperties)",
        )
    )

    requests = []
    for sheet in meta.get("sheets", []):
        props = sheet["properties"]
        sheet_id = props["sheetId"]
        title = props["title"]
        grid = props.get("gridProperties", {})
        row_count = grid.get("rowCount", 0)
        col_count = grid.get("columnCount", 0)

        def row_is_empty_from(k, title=title, row_count=row_count):
            if k >= row_count:
                return True
            return not bool(_execute_with_retry(
                lambda: service.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id, range=f"{title}!{k + 1}:{row_count}"
                )
            ).get("values"))

        def col_is_empty_from(k, title=title, col_count=col_count):
            if k >= col_count:
                return True
            start_letter = _column_letter(k + 1)
            end_letter = _column_letter(col_count)
            return not bool(_execute_with_retry(
                lambda: service.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id, range=f"{title}!{start_letter}:{end_letter}"
                )
            ).get("values"))

        last_row = max(1, _find_empty_suffix_boundary(row_is_empty_from, row_count))
        if last_row < row_count:
            requests.append({
                "deleteDimension": {
                    "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": last_row, "endIndex": row_count}
                }
            })

        last_col = max(1, _find_empty_suffix_boundary(col_is_empty_from, col_count))
        if last_col < col_count:
            requests.append({
                "deleteDimension": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": last_col, "endIndex": col_count}
                }
            })

    if not requests:
        return False

    _execute_with_retry(
        lambda: service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        )
    )
    return True


def _sheet_has_data(service, spreadsheet_id, sheet_name):
    """Cheap check: does this sheet/tab's first row already have anything in it?

    Only reads row 1 (not the whole sheet, which could be huge) — good enough
    to tell whether a header is already there.
    """
    result = _execute_with_retry(
        lambda: service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{sheet_name}!1:1"
        )
    )
    return bool(result.get("values"))


def _get_spreadsheet_title(service, spreadsheet_id):
    meta = _execute_with_retry(
        lambda: service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="properties.title")
    )
    return meta.get("properties", {}).get("title", "Untitled spreadsheet")


def _add_sheet(service, spreadsheet_id, title, num_columns):
    """Add a new tab to an existing spreadsheet, sized to just fit the
    CSV's columns rather than accepting Google's default 1000x26 grid —
    when a spreadsheet is already tight on its cell budget, a wide default
    tab can itself burn through most of what little room is left before a
    single row of real data goes in. Raises _CellLimitHit if that
    spreadsheet has no room left even for a minimal, empty tab.
    """
    result = _execute_with_retry(
        lambda: service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": title,
                                "gridProperties": {"rowCount": 1, "columnCount": max(num_columns, 1)},
                            }
                        }
                    }
                ]
            },
            fields="replies.addSheet.properties.title",
        )
    )
    return result["replies"][0]["addSheet"]["properties"]["title"]


def _create_continuation_spreadsheet(service, base_title, part_number, sheet_name):
    """Create a new spreadsheet to hold the overflow once the original
    spreadsheet is full — tabs within one spreadsheet share its cell
    budget, so a new tab can't gain more room once the whole file is
    exhausted; only a separate spreadsheet has its own independent budget.

    Creating a spreadsheet is covered by the same "spreadsheets" OAuth scope
    already used for reading/writing, so this needs no extra permission.
    """
    result = _execute_with_retry(
        lambda: service.spreadsheets().create(
            body={
                "properties": {"title": f"{base_title} (continued {part_number})"},
                "sheets": [{"properties": {"title": sheet_name}}],
            },
            fields="spreadsheetId,spreadsheetUrl,sheets.properties.title",
        )
    )
    sheet_title = result["sheets"][0]["properties"]["title"]
    return result["spreadsheetId"], result["spreadsheetUrl"], sheet_title


def _clear_sheet(service, spreadsheet_id, sheet_name):
    """Wipe a sheet/tab and shrink it back down to reclaim its cell budget.

    Clearing values alone (spreadsheets.values.clear) does NOT free up space
    against the 10,000,000-cell limit — Google Sheets counts a sheet's
    allocated grid dimensions, not just its filled cells, and clearing
    values leaves those dimensions untouched. Deleting the rows outright is
    the only way to actually shrink the grid.

    Grid-shrinking (deleteDimension, by row index) runs before the values
    clear, and the clear afterward targets only the single row left behind
    (an explicit "1:1" range) rather than the whole sheet by name — passing
    a bare sheet name to values.clear() can make the API mis-derive an
    invalid range (start row past its own end row) on some sheets and fail
    with a confusing "exceeds grid limits" error.
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

    _execute_with_retry(
        lambda: service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id, range=f"{sheet_name}!1:1", body={}
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


def upload_csv_to_sheet(csv_path, spreadsheet_id_or_url, sheet_name, has_header,
                         progress_callback=None, clear_first=False):
    """Stream a CSV's rows into an existing Google Sheet via the API.

    By default rows are appended after whatever is already in the sheet.
    Pass clear_first=True to wipe the sheet's existing contents first.
    If has_header is True, the CSV's header row is uploaded whenever the
    current destination sheet/tab is empty (so it ends up with column
    headers) and skipped when it already has data (to avoid a duplicate).

    If the destination spreadsheet fills up — Google Sheets' hard
    10,000,000-cell-per-spreadsheet limit, which no amount of batching can
    get around — a new tab named "<original tab> (continued N)" is added
    to the SAME spreadsheet and the rest of the CSV flows into that. Tabs
    share their spreadsheet's cell budget, though, so once the whole file
    is genuinely full, a new spreadsheet ("<original title> (continued N)")
    is created instead, with its own independent budget. This can repeat
    multiple times for very large CSVs, mixing new tabs and new
    spreadsheets as needed.

    Returns a list of dicts, one per (spreadsheet, tab) actually written
    to: {"spreadsheet_id", "url", "title", "sheet_name", "rows"}
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

    creds = get_credentials()
    service = build("sheets", "v4", credentials=creds)

    if clear_first:
        _clear_sheet(service, spreadsheet_id, sheet_name)

    # Reclaim any cell budget tied up in trailing empty rows/columns across
    # every sheet/tab in the destination — common after a stray scroll or
    # paste inflates a sheet's grid far past its real data — before
    # deciding whether the CSV needs to spill into new tabs or sheets.
    try:
        _trim_empty_space(service, spreadsheet_id)
    except SheetsUploadError:
        pass  # not worth failing the upload over; overflow handling below still works

    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        raw_rows = sum(1 for _ in csv.reader(f))
    total_data_rows = max(raw_rows - (1 if has_header else 0), 0)

    base_title = _get_spreadsheet_title(service, spreadsheet_id)
    try:
        original_is_empty = not _sheet_has_data(service, spreadsheet_id, sheet_name)
    except SheetsUploadError:
        original_is_empty = False  # if unsure, don't risk duplicating an existing header

    state = {
        "dest": {
            "spreadsheet_id": spreadsheet_id,
            "sheet_name": sheet_name,
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
            "title": base_title,
            "rows": 0,
            "is_empty": original_is_empty,
        },
        "destinations": None,  # set below, same list object as dest's container
        "uploaded_total": 0,
        "part_number": 1,
        "header_row": None,
    }
    state["destinations"] = [state["dest"]]

    def send(chunk):
        d = state["dest"]
        rows_to_send = chunk
        if d["is_empty"] and d["rows"] == 0 and state["header_row"] is not None:
            rows_to_send = [state["header_row"]] + chunk

        spillovers = 0
        tried_tab_for = set()  # spreadsheet_ids where a new tab has already been tried and failed
        while True:
            try:
                _append_chunk(service, d["spreadsheet_id"], d["sheet_name"], rows_to_send)
                d["rows"] += len(rows_to_send)
                state["uploaded_total"] += len(chunk)
                return
            except _CellLimitHit:
                spillovers += 1
                if spillovers > 6:
                    raise SheetsUploadError(
                        f"Even a brand-new, empty Google Sheet can't fit a single batch of "
                        f"{len(rows_to_send):,} row(s) x {num_columns} column(s) — this CSV "
                        "has far too many columns for Google Sheets to hold. Try a CSV with "
                        "fewer columns."
                    )
                state["part_number"] += 1
                continuation_name = f"{sheet_name} (continued {state['part_number']})"

                # Prefer a new tab in the SAME spreadsheet, so everything
                # stays in one file as long as that file still has room.
                # Tabs share their spreadsheet's cell budget, though, so
                # this can fail the same way the append just did — but only
                # try it once per spreadsheet: if a fresh tab there *also*
                # can't take the batch, that file is genuinely exhausted,
                # and trying yet another tab there would just fail again.
                new_dest = None
                if d["spreadsheet_id"] not in tried_tab_for:
                    tried_tab_for.add(d["spreadsheet_id"])
                    try:
                        new_sheet_name = _add_sheet(service, d["spreadsheet_id"], continuation_name, num_columns)
                        new_dest = {
                            "spreadsheet_id": d["spreadsheet_id"],
                            "sheet_name": new_sheet_name,
                            "url": d["url"],
                            "title": d["title"],
                            "rows": 0,
                            "is_empty": True,
                        }
                    except _CellLimitHit:
                        pass  # fall through to a brand-new spreadsheet below

                if new_dest is None:
                    new_id, new_url, new_sheet_name = _create_continuation_spreadsheet(
                        service, base_title, state["part_number"], sheet_name
                    )
                    new_dest = {
                        "spreadsheet_id": new_id,
                        "sheet_name": new_sheet_name,
                        "url": new_url,
                        "title": f"{base_title} (continued {state['part_number']})",
                        "rows": 0,
                        "is_empty": True,
                    }

                d = new_dest
                state["dest"] = d
                state["destinations"].append(d)
                rows_to_send = [state["header_row"]] + chunk if state["header_row"] is not None else chunk

    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        if has_header:
            state["header_row"] = next(reader, None)

        chunk = []
        for row in reader:
            chunk.append(row)
            if len(chunk) >= batch_rows:
                send(chunk)
                if progress_callback:
                    progress_callback(state["uploaded_total"], total_data_rows)
                chunk = []
        if chunk:
            send(chunk)
            if progress_callback:
                progress_callback(state["uploaded_total"], total_data_rows)

    return [d for d in state["destinations"] if d["rows"] > 0] or state["destinations"][:1]
