# CSV Splitter

A simple Windows desktop app for getting a large CSV into Google Sheets. It
offers two ways to do that:

- **Split into files** — breaks a large CSV into multiple smaller CSVs, each
  small enough to import through Google Sheets' own "Import file" dialog.
  The header row (if present) is repeated in every output file, so each part
  stays valid on its own.
- **Upload to Google Sheet (API)** — pushes the CSV's rows directly into an
  existing Google Sheet through the Sheets API, with no file-size import
  limit and no splitting needed at all.

## Files

| File | Purpose |
|---|---|
| `csv_splitter.py` | The app itself |
| `sheets_upload.py` | Google Sheets API upload logic, used by the "Upload to Google Sheet (API)" tab |
| `csv_splitter_icon.ico` | App icon (must stay in the same folder as the script) |
| `requirements.txt` | Optional dependencies, only needed for the API upload tab |

## Requirements

- Windows 10/11
- [Python 3.9+](https://www.python.org/downloads/) — make sure to check **"Add Python to PATH"** during install. (Not needed if you're using a pre-built `.exe`.)

Splitting into files uses only Python's built-in `tkinter` and `csv`
modules — no extra installation needed. The **Upload to Google Sheet
(API)** tab additionally needs:
```
pip install -r requirements.txt
```

## Running it

1. Put `csv_splitter.py`, `sheets_upload.py`, and `csv_splitter_icon.ico` in the same folder.
2. Double-click `csv_splitter.py`, or open a Command Prompt in that folder and run:
   ```
   python csv_splitter.py
   ```

## Using the app

1. **Input CSV** — click *Browse...* and choose the CSV file you want to work with.
2. **File has a header row** — leave checked if row 1 is column names.
3. Pick a tab:
   - **Split into files** — set an output folder and rows per file (or check
     **Split for importing into an existing Google Sheet**, see below), then
     click **Split CSV**.
   - **Upload to Google Sheet (API)** — enter the destination sheet's URL or
     ID and tab name, then click **Upload to Google Sheet** (see setup steps
     below — this needs a one-time Google Cloud credential first).
4. A progress bar shows status, and a confirmation pops up when done.

Output files are named:
```
yourfile_part1.csv
yourfile_part2.csv
yourfile_part3.csv
...
```

## Splitting for Google Sheets

Google Sheets caps every spreadsheet at 10,000,000 cells total (across all
its sheets). Google's *documented* per-file import limit is around 100MB,
but in practice the "Import file" dialog used to add data to an existing
spreadsheet can reject plain CSVs well below that — with a "This file is
too large to import directly into Google Sheets" error — and exactly how
far below varies. If you're importing several parts into a sheet that
already has data in it, manually guessing a safe "rows per file" number is
error-prone.

Check **Split for importing into an existing Google Sheet** and set a
**Target size (MB)** (25MB by default). The app reads the column count
from the CSV and calculates rows per file to hit that size, cutting a part
early if it would otherwise run over. The "Rows per file" field is
disabled while this is checked, since it's computed automatically, and the
actual rows/file value used is shown in the completion message.

25MB is a reasonable starting point, but Google's real threshold for this
dialog isn't published and seems to depend on the file itself — if a part
still gets rejected, split again with a lower target size; if 25MB parts
import fine, you can raise it to get fewer, larger files.

## Uploading directly via the Google Sheets API

Instead of splitting into files at all, the **Upload to Google Sheet
(API)** tab pushes the CSV's rows straight into an existing sheet through
Google's API, in small batches. There's no file to import and no size
limit to work around — the only constraints left are Google Sheets'
overall 10,000,000-cell-per-spreadsheet limit and the API's own request
rate limits (the app already retries automatically on rate-limit errors).

If **File has a header row** is checked, that header is uploaded as the
sheet's first row when the destination sheet/tab is empty, and skipped
when it already has data — so a header ends up in the sheet exactly once,
whether you're starting fresh or appending to something that already has
one.

**One-time setup**, per Google account:

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and
   create a project (or pick an existing one).
2. Enable the **Google Sheets API**: *APIs & Services → Library*, search
   for "Google Sheets API", click **Enable**.
3. Create credentials: *APIs & Services → Credentials → Create Credentials
   → OAuth client ID*. If prompted, configure the OAuth consent screen
   first (choose **External** and add your own Google account as a test
   user, unless you're on a Google Workspace account). For **Application
   type**, choose **Desktop app**.
4. Download the resulting JSON file and save it as `client_secret.json` in
   the same folder as `csv_splitter.py` (or next to the `.exe`, if you
   built one).
5. Install the extra Python packages this needs: `pip install -r requirements.txt`.

**Using it:**

1. Open the Google Sheet you want to add data to, and copy its URL (or
   just the long ID from the URL) into **Sheet URL or ID**.
2. Enter the name of the sheet/tab to upload to (e.g. `Sheet1`) in
   **Sheet/tab name**.
3. By default, rows are appended after whatever is already in that sheet.
   Check **Clear existing sheet contents first** to delete that sheet/tab's
   existing rows before uploading instead — you'll be asked to confirm,
   since this can't be undone. This actually deletes the rows (not just
   their values) because Google Sheets counts a sheet's allocated size
   toward the cell limit below, regardless of whether it holds data —
   merely clearing values wouldn't free up any room.
4. Click **Upload to Google Sheet**. The first time, a browser window opens
   asking you to sign in and grant access — after that, a cached token
   (stored in `~/.csv_splitter/token.json`) is reused automatically.

**The 10,000,000-cell limit:** this one is a hard Google Sheets platform
limit — it applies to the whole spreadsheet (every tab combined) and
can't be worked around by the API, by splitting files, or by any setting
in this app. If a spreadsheet doesn't have enough room left, the app now
checks before starting the upload and tells you exactly how much room is
left and how many rows will fit, rather than failing partway through.
Your options at that point: clear the target sheet/tab (see above), trim
the CSV, or upload the remainder into a separate Google Sheet (each
spreadsheet gets its own independent 10,000,000-cell budget). If you're
regularly working with datasets too big for a spreadsheet at all, Google's
own suggested path is [Connected Sheets](https://support.google.com/docs/answer/9702507),
which queries BigQuery data from within Sheets instead of loading it all
into cells.

## Building a standalone .exe

If you want to run this on a PC without installing Python, build an
executable using [PyInstaller](https://pyinstaller.org/):

```
pip install pyinstaller
pyinstaller --onefile --windowed --icon=csv_splitter_icon.ico --add-data "csv_splitter_icon.ico;." csv_splitter.py
```

`sheets_upload.py` is picked up automatically since it's imported by
`csv_splitter.py`. If you also want the **Upload to Google Sheet (API)**
tab to work in the built `.exe`, install `requirements.txt` first so
PyInstaller can bundle those packages too:
```
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --icon=csv_splitter_icon.ico ^
    --add-data "csv_splitter_icon.ico;." ^
    --collect-all google_auth_oauthlib --collect-all googleapiclient ^
    csv_splitter.py
```
`client_secret.json` is **not** bundled into the exe (it's read from disk
at runtime) — keep it in the same folder as the built `.exe`.

This must be run **on Windows** — PyInstaller doesn't cross-compile, so
building on macOS/Linux won't produce a Windows `.exe`.

The finished executable will be at `dist\csv_splitter.exe`. You can copy
that single file anywhere and run it — no Python installation required on
the target machine.

## Notes

- Large files are read once to count rows (for the progress bar) and once
  to do the actual splitting, so very large files may take a bit longer.
- The app reads CSVs as UTF-8 (with BOM support) and writes output files as
  UTF-8.
- If the last output file ends up empty, it's automatically removed.

## Troubleshooting

| Problem | Fix |
|---|---|
| Double-clicking the `.py` file does nothing | Make sure Python is installed and `.py` files are associated with it, or run via `python csv_splitter.py` in Command Prompt |
| `tkinter` not found error | Reinstall Python and make sure the "tcl/tk and IDLE" option is checked during setup (it's included by default on the official installer) |
| Icon doesn't show in the window | Confirm `csv_splitter_icon.ico` is in the same folder as `csv_splitter.py` (or, for the `.exe`, that it was built with the `--add-data` flag above) |
| "Missing packages for Google Sheets upload" error | Run `pip install -r requirements.txt` (only needed for the **Upload to Google Sheet (API)** tab) |
| "No Google API credentials found" error | Follow the one-time setup steps above and make sure `client_secret.json` is saved in the exact folder the error message names |
| Browser sign-in page shows "Google hasn't verified this app" | Expected for a personal OAuth client — click **Advanced → Go to (your app name)** to proceed; this only appears because the app isn't published for public use, which is fine for personal/internal use |
| Upload fails with a permission or "not found" error | Make sure the Google account you signed in with has edit access to the destination sheet |
| "This won't fit" / hit the 10,000,000-cell limit | See "The 10,000,000-cell limit" above — clear the sheet/tab, trim the CSV, or use a separate Google Sheet |
