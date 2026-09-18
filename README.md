# CSV Splitter

A simple Windows desktop app that splits a large CSV file into multiple
smaller CSV files. The header row (if present) is repeated in every output
file, so each split file stays valid on its own.

## Files

| File | Purpose |
|---|---|
| `csv_splitter.py` | The app itself |
| `csv_splitter_icon.ico` | App icon (must stay in the same folder as the script) |

## Requirements

- Windows 10/11
- [Python 3.9+](https://www.python.org/downloads/) — make sure to check **"Add Python to PATH"** during install. (Not needed if you're using a pre-built `.exe`.)

No extra libraries are required — the app uses only Python's built-in `tkinter` and `csv` modules.

## Running it

1. Put `csv_splitter.py` and `csv_splitter_icon.ico` in the same folder.
2. Double-click `csv_splitter.py`, or open a Command Prompt in that folder and run:
   ```
   python csv_splitter.py
   ```

## Using the app

1. **Input CSV** — click *Browse...* and choose the CSV file you want to split.
2. **Output folder** — pick where the split files should be saved (defaults to the input file's folder).
3. **Rows per file** — how many data rows each output file should contain.
4. **File has a header row** — leave checked if row 1 is column names; the header is copied into every output file.
5. **Split for importing into an existing Google Sheet** — check this to have rows per file calculated automatically instead (see below).
6. Click **Split CSV**. A progress bar shows status, and a confirmation pops up when done.

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

## Building a standalone .exe

If you want to run this on a PC without installing Python, build an
executable using [PyInstaller](https://pyinstaller.org/):

```
pip install pyinstaller
pyinstaller --onefile --windowed --icon=csv_splitter_icon.ico --add-data "csv_splitter_icon.ico;." csv_splitter.py
```

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
