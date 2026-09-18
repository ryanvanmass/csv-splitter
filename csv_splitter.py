"""
CSV Splitter
------------
A simple graphical app that splits a large CSV file into multiple smaller
CSV files, each with a set number of data rows. The header row (if present)
is repeated in every output file.

Run it with:  python csv_splitter.py
(On Windows, double-clicking works too if .py files are associated with Python.)

Keep csv_splitter_icon.ico in the same folder as this script - it's used as
the window/taskbar icon.

To turn this into a standalone .exe (no Python required on the target PC),
with the icon baked in:
    pip install pyinstaller
    pyinstaller --onefile --windowed --icon=csv_splitter_icon.ico ^
        --add-data "csv_splitter_icon.ico;." csv_splitter.py
The .exe will appear in the "dist" folder.
(The --add-data flag bundles the icon file into the exe itself so it still
shows up in the window titlebar when run as a standalone .exe. On macOS/Linux
replace the ";" with ":" in that flag.)
"""

import csv
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Google Sheets limits (as of 2026): a spreadsheet maxes out at 10,000,000
# cells total across all its sheets, and Google's documented per-file import
# cap is ~100MB. In practice, though, the "Import file" dialog used to add
# data to an *existing* spreadsheet can reject files well below that figure.
# GOOGLE_SHEETS_SAFE_CELLS is a backstop only, kept comfortably under the
# 10,000,000-cell total (with headroom for data already in the destination
# sheet and the other parts being imported alongside it) — it's the target
# file size, set by the user, that normally decides how each part is cut.
GOOGLE_SHEETS_SAFE_CELLS = 2_000_000
GOOGLE_SHEETS_DEFAULT_MB = 25


def count_csv_columns(path):
    """Return the column count of a CSV's first row (min 1)."""
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        first_row = next(csv.reader(f), [])
    return max(len(first_row), 1)


class CSVSplitterApp:
    def __init__(self, root):
        self.root = root
        root.title("CSV Splitter")
        root.geometry("520x360")
        root.resizable(False, False)

        # Set the window/taskbar icon (Windows uses .ico; falls back quietly
        # if the icon file isn't found). Works both run as a .py script and
        # bundled into a PyInstaller exe (which unpacks to sys._MEIPASS).
        try:
            base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            icon_path = os.path.join(base_dir, "csv_splitter_icon.ico")
            root.iconbitmap(icon_path)
        except Exception:
            pass

        self.input_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.rows_per_file = tk.IntVar(value=1000)
        self.has_header = tk.BooleanVar(value=True)
        self.google_sheets_mode = tk.BooleanVar(value=False)
        self.google_sheets_mb = tk.IntVar(value=GOOGLE_SHEETS_DEFAULT_MB)
        self.status = tk.StringVar(value="Choose a CSV file to begin.")

        pad = {"padx": 10, "pady": 6}

        # Input file row
        frame1 = ttk.Frame(root)
        frame1.pack(fill="x", **pad)
        ttk.Label(frame1, text="Input CSV:", width=12).pack(side="left")
        ttk.Entry(frame1, textvariable=self.input_path).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(frame1, text="Browse...", command=self.choose_input).pack(side="left")

        # Output folder row
        frame2 = ttk.Frame(root)
        frame2.pack(fill="x", **pad)
        ttk.Label(frame2, text="Output folder:", width=12).pack(side="left")
        ttk.Entry(frame2, textvariable=self.output_dir).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(frame2, text="Browse...", command=self.choose_output).pack(side="left")

        # Rows per file row
        frame3 = ttk.Frame(root)
        frame3.pack(fill="x", **pad)
        ttk.Label(frame3, text="Rows per file:", width=12).pack(side="left")
        self.rows_spinbox = ttk.Spinbox(frame3, from_=1, to=1_000_000, textvariable=self.rows_per_file, width=10)
        self.rows_spinbox.pack(side="left", padx=5)
        ttk.Checkbutton(frame3, text="File has a header row", variable=self.has_header).pack(side="left", padx=20)

        # Google Sheets auto-split row
        frame3b = ttk.Frame(root)
        frame3b.pack(fill="x", **pad)
        ttk.Checkbutton(
            frame3b,
            text="Split for importing into an existing Google Sheet",
            variable=self.google_sheets_mode,
            command=self.toggle_google_sheets_mode,
        ).pack(side="left")
        ttk.Label(frame3b, text="Target size (MB):").pack(side="left", padx=(20, 5))
        self.google_sheets_mb_spinbox = ttk.Spinbox(
            frame3b, from_=1, to=95, textvariable=self.google_sheets_mb, width=6, state="disabled"
        )
        self.google_sheets_mb_spinbox.pack(side="left")
        ttk.Label(
            root,
            text=(
                "When checked, rows per file is calculated automatically so each part "
                "stays near the target size above. Google's own import dialog can reject "
                "files well under its documented 100MB limit, so if a part still gets "
                "rejected, lower this number and split again."
            ),
            wraplength=480,
            justify="left",
            foreground="#555555",
        ).pack(fill="x", padx=10)

        # Split button
        frame4 = ttk.Frame(root)
        frame4.pack(fill="x", **pad)
        self.split_btn = ttk.Button(frame4, text="Split CSV", command=self.start_split)
        self.split_btn.pack(pady=10)

        # Progress bar
        self.progress = ttk.Progressbar(root, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=6)

        # Status label
        ttk.Label(root, textvariable=self.status, wraplength=480, justify="left").pack(fill="x", padx=10, pady=6)

    def choose_input(self):
        path = filedialog.askopenfilename(
            title="Select CSV file",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.input_path.set(path)
            if not self.output_dir.get():
                self.output_dir.set(os.path.dirname(path))

    def choose_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_dir.set(path)

    def toggle_google_sheets_mode(self):
        enabled = self.google_sheets_mode.get()
        self.rows_spinbox.config(state="disabled" if enabled else "normal")
        self.google_sheets_mb_spinbox.config(state="normal" if enabled else "disabled")

    def start_split(self):
        in_path = self.input_path.get().strip()
        out_dir = self.output_dir.get().strip()
        rows_per_file = self.rows_per_file.get()

        if not in_path or not os.path.isfile(in_path):
            messagebox.showerror("Error", "Please choose a valid input CSV file.")
            return
        if not out_dir:
            messagebox.showerror("Error", "Please choose an output folder.")
            return
        if rows_per_file <= 0:
            messagebox.showerror("Error", "Rows per file must be at least 1.")
            return
        os.makedirs(out_dir, exist_ok=True)

        self.split_btn.config(state="disabled")
        self.status.set("Splitting...")
        self.progress["value"] = 0

        google_sheets_mode = self.google_sheets_mode.get()
        max_file_bytes = 0
        if google_sheets_mode:
            try:
                num_columns = count_csv_columns(in_path)
            except Exception as e:
                messagebox.showerror("Error", f"Could not read the CSV file:\n{e}")
                return
            rows_per_file = max(1, GOOGLE_SHEETS_SAFE_CELLS // num_columns)
            max_file_bytes = max(1, self.google_sheets_mb.get()) * 1024 * 1024

        # Run the split on a background thread so the UI doesn't freeze
        thread = threading.Thread(
            target=self.split_csv,
            args=(in_path, out_dir, rows_per_file, google_sheets_mode, max_file_bytes),
            daemon=True,
        )
        thread.start()

    def split_csv(self, in_path, out_dir, rows_per_file, google_sheets_mode=False, max_file_bytes=0):
        base_name = os.path.splitext(os.path.basename(in_path))[0]
        try:
            # First pass: count total data rows for the progress bar
            with open(in_path, "r", newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                total_rows = sum(1 for _ in reader)
            if self.has_header.get() and total_rows > 0:
                total_rows -= 1
            total_rows = max(total_rows, 1)

            with open(in_path, "r", newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader) if self.has_header.get() else None

                file_index = 1
                row_count = 0
                processed = 0
                writer = None
                out_file = None

                def open_new_file(idx):
                    path = os.path.join(out_dir, f"{base_name}_part{idx}.csv")
                    fh = open(path, "w", newline="", encoding="utf-8")
                    wr = csv.writer(fh)
                    if header:
                        wr.writerow(header)
                    return fh, wr

                out_file, writer = open_new_file(file_index)

                for row in reader:
                    writer.writerow(row)
                    row_count += 1
                    processed += 1

                    if processed % 500 == 0:
                        pct = min(100, int(processed / total_rows * 100))
                        self.root.after(0, self.update_progress, pct)

                    size_limit_hit = google_sheets_mode and out_file.tell() >= max_file_bytes
                    if row_count >= rows_per_file or size_limit_hit:
                        out_file.close()
                        file_index += 1
                        row_count = 0
                        out_file, writer = open_new_file(file_index)

                out_file.close()
                # Remove the last file if it ended up empty (no rows written into it)
                if row_count == 0 and file_index > 1:
                    last_path = os.path.join(out_dir, f"{base_name}_part{file_index}.csv")
                    if os.path.isfile(last_path):
                        os.remove(last_path)
                        file_index -= 1

            self.root.after(0, self.finish, file_index, out_dir, None, rows_per_file if google_sheets_mode else None)

        except Exception as e:
            self.root.after(0, self.finish, 0, out_dir, str(e), None)

    def update_progress(self, pct):
        self.progress["value"] = pct

    def finish(self, file_count, out_dir, error, google_sheets_rows):
        self.split_btn.config(state="normal")
        self.progress["value"] = 100 if not error else 0
        if error:
            self.status.set(f"Error: {error}")
            messagebox.showerror("Error", f"Something went wrong:\n{error}")
        else:
            extra = f" (~{google_sheets_rows} rows/file, sized for Google Sheets)" if google_sheets_rows else ""
            message = f"Done! Created {file_count} file(s){extra} in:\n{out_dir}"
            self.status.set(message)
            messagebox.showinfo("Success", message)


if __name__ == "__main__":
    root = tk.Tk()
    app = CSVSplitterApp(root)
    root.mainloop()
