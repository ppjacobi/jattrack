#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cross‑platform Time Tracker
===========================

Single‑file Python app with a Tkinter GUI that runs on Windows and Linux.

Features
--------
- Start/Stop tracking with live timer
- Projects dropdown (auto‑complete), task title, optional notes
- Today overview with total time
- History table with filter by date range & project
- Edit/delete entries
- CSV export
- SQLite persistence in user data folder

Dependencies
------------
- Python 3.9+
- Tkinter (bundled with Python on Windows/macOS; on some Linux distros: install package `python3-tk`)

Usage
-----
python timetracker.py

Packaging (optional)
--------------------
PyInstaller (creates a single executable):
  pyinstaller -F -w timetracker.py

Author: ChatGPT (GPT‑5 Thinking)
License: MIT
"""
from __future__ import annotations

import csv
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional, List, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = "TimeTracker"
DB_NAME = "timetracker.sqlite3"

# ----------------------------
# Utility helpers
# ----------------------------

def user_data_dir() -> Path:
    """Return a per‑user data directory suitable for the platform."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return Path(base) / APP_NAME
    else:
        # Linux and others
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        return Path(base) / APP_NAME


def pretty_duration(seconds: int) -> str:
    neg = seconds < 0
    seconds = abs(int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    sign = "-" if neg else ""
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


def parse_hhmm(value: str) -> Optional[int]:
    """Parse HH:MM or H:MM into seconds. Return None if invalid."""
    try:
        parts = value.strip().split(":")
        if len(parts) not in (2, 3):
            return None
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2]) if len(parts) == 3 else 0
        if not (0 <= m < 60 and 0 <= s < 60):
            return None
        return h * 3600 + m * 60 + s
    except Exception:
        return None


# ----------------------------
# Data layer
# ----------------------------

class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS projects(
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS entries(
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                task TEXT NOT NULL,
                notes TEXT,
                start_ts TEXT NOT NULL,
                end_ts TEXT,
                duration_s INTEGER, -- cached finalized duration
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_start ON entries(start_ts);
            """
        )
        self.conn.commit()

    # --- project ops ---
    def upsert_project(self, name: str) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Project name cannot be empty")
        cur = self.conn.cursor()
        try:
            cur.execute("INSERT INTO projects(name) VALUES(?)", (name,))
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass
        cur.execute("SELECT id FROM projects WHERE name=?", (name,))
        row = cur.fetchone()
        return int(row[0])

    def projects(self) -> List[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT name FROM projects ORDER BY name COLLATE NOCASE")
        return [r[0] for r in cur.fetchall()]

    # --- entry ops ---
    def start_entry(self, project_name: str, task: str, notes: str) -> int:
        # stop any running entry first
        running = self.get_running_entry()
        if running:
            self.stop_entry(running["id"])  # auto‑stop previous
        pid = self.upsert_project(project_name)
        now = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO entries(project_id, task, notes, start_ts) VALUES(?,?,?,?)",
            (pid, task.strip(), notes.strip(), now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def stop_entry(self, entry_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("SELECT start_ts FROM entries WHERE id=?", (entry_id,))
        row = cur.fetchone()
        if not row:
            return
        start = datetime.fromisoformat(row[0])
        end = datetime.now()
        dur = int((end - start).total_seconds())
        cur.execute(
            "UPDATE entries SET end_ts=?, duration_s=? WHERE id=?",
            (end.isoformat(timespec="seconds"), dur, entry_id),
        )
        self.conn.commit()

    def finalize_running_if_any(self) -> None:
        r = self.get_running_entry()
        if r:
            self.stop_entry(int(r["id"]))

    def get_running_entry(self) -> Optional[sqlite3.Row]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT e.*, p.name as project
            FROM entries e JOIN projects p ON e.project_id = p.id
            WHERE e.end_ts IS NULL
            ORDER BY e.start_ts DESC
            LIMIT 1
            """
        )
        return cur.fetchone()

    def delete_entry(self, entry_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        self.conn.commit()

    def update_entry(self, entry_id: int, project_name: str, task: str, notes: str, start_ts: str, end_ts: Optional[str]) -> None:
        pid = self.upsert_project(project_name)
        cur = self.conn.cursor()
        duration_s = None
        if end_ts:
            start = datetime.fromisoformat(start_ts)
            end = datetime.fromisoformat(end_ts)
            duration_s = int((end - start).total_seconds())
        cur.execute(
            "UPDATE entries SET project_id=?, task=?, notes=?, start_ts=?, end_ts=?, duration_s=? WHERE id=?",
            (pid, task.strip(), notes.strip(), start_ts, end_ts, duration_s, entry_id),
        )
        self.conn.commit()

    def query_entries(self, start_date: date, end_date: date, project: Optional[str]) -> List[sqlite3.Row]:
        cur = self.conn.cursor()
        start_ts = datetime.combine(start_date, datetime.min.time()).isoformat()
        end_ts = datetime.combine(end_date, datetime.max.time()).isoformat()
        sql = (
            "SELECT e.id, p.name AS project, e.task, e.notes, e.start_ts, e.end_ts, e.duration_s "
            "FROM entries e JOIN projects p ON e.project_id=p.id "
            "WHERE e.start_ts BETWEEN ? AND ?"
        )
        params: List[object] = [start_ts, end_ts]
        if project:
            sql += " AND p.name=?"
            params.append(project)
        sql += " ORDER BY e.start_ts DESC"
        cur.execute(sql, params)
        return list(cur.fetchall())

    def sum_today(self) -> int:
        today = date.today()
        # Define today's interval
        start_of_day = datetime.combine(today, datetime.min.time())
        end_of_day = datetime.combine(today, datetime.max.time())

        cur = self.conn.cursor()
        # Fetch entries that overlap with today:
        # start <= end_of_day AND (end IS NULL OR end >= start_of_day)
        cur.execute(
            """
            SELECT e.start_ts, e.end_ts, e.duration_s
            FROM entries e
            WHERE e.start_ts <= ? AND (e.end_ts IS NULL OR e.end_ts >= ?)
            """,
            (end_of_day.isoformat(), start_of_day.isoformat()),
        )
        rows = cur.fetchall()

        total = 0
        now = datetime.now()

        for r in rows:
            start = datetime.fromisoformat(r["start_ts"])
            end = datetime.fromisoformat(r["end_ts"]) if r["end_ts"] else now
            # Clamp to today's window
            eff_start = max(start, start_of_day)
            eff_end = min(end, end_of_day)
            if eff_end > eff_start:
                total += int((eff_end - eff_start).total_seconds())

        return total

    def export_csv(self, rows: List[sqlite3.Row], dest: Path) -> None:
        with dest.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ID", "Project", "Task", "Notes", "Start", "End", "Duration (h:mm:ss)"])
            for r in rows:
                dur = r["duration_s"]
                if dur is None and r["end_ts"] and r["start_ts"]:
                    start = datetime.fromisoformat(r["start_ts"])  # safety
                    end = datetime.fromisoformat(r["end_ts"]) if r["end_ts"] else datetime.now()
                    dur = int((end - start).total_seconds())
                writer.writerow([
                    r["id"], r["project"], r["task"], r["notes"], r["start_ts"], r["end_ts"], pretty_duration(int(dur or 0))
                ])


# ----------------------------
# GUI layer
# ----------------------------

@dataclass
class FormState:
    project: tk.StringVar
    task: tk.StringVar
    notes: tk.StringVar


class TimeTrackerApp(ttk.Frame):
    def __init__(self, master: tk.Tk, store: Store):
        super().__init__(master)
        self.master = master
        self.store = store
        self.running_id: Optional[int] = None
        self.timer_job: Optional[str] = None

        self.state = FormState(
            project=tk.StringVar(),
            task=tk.StringVar(),
            notes=tk.StringVar(),
        )

        self._build_ui()
        self._load_projects()
        self._load_running()
        self._refresh_table()
        self._tick()

    # --- UI builders ---
    def _build_ui(self):
        self.master.title(APP_NAME)
        self.master.geometry("980x600")
        self.master.minsize(820, 520)

        # top menu
        menubar = tk.Menu(self.master)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Export CSV…", command=self.on_export_csv)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.master.destroy)
        menubar.add_cascade(label="File", menu=filemenu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About", command=self.on_about)
        menubar.add_cascade(label="Help", menu=helpmenu)
        self.master.config(menu=menubar)

        # main layout
        container = ttk.Frame(self.master, padding=10)
        container.pack(fill=tk.BOTH, expand=True)

        form = ttk.LabelFrame(container, text="New Entry")
        form.pack(fill=tk.X)

        ttk.Label(form, text="Project").grid(row=0, column=0, sticky=tk.W, padx=6, pady=6)
        self.project_cb = ttk.Combobox(form, textvariable=self.state.project)
        self.project_cb.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=6)

        ttk.Label(form, text="Task").grid(row=0, column=2, sticky=tk.W, padx=6, pady=6)
        self.task_entry = ttk.Entry(form, textvariable=self.state.task)
        self.task_entry.grid(row=0, column=3, sticky=tk.EW, padx=6, pady=6)

        ttk.Label(form, text="Notes").grid(row=0, column=4, sticky=tk.W, padx=6, pady=6)
        self.notes_entry = ttk.Entry(form, textvariable=self.state.notes)
        self.notes_entry.grid(row=0, column=5, sticky=tk.EW, padx=6, pady=6)

        form.columnconfigure(1, weight=2)
        form.columnconfigure(3, weight=3)
        form.columnconfigure(5, weight=3)

        buttons = ttk.Frame(container)
        buttons.pack(fill=tk.X, pady=(6, 2))

        self.start_btn = tk.Button(
            buttons,
            text="Start",
            command=self.on_start,
            bg="#2e7d32",
            fg="white",
            activebackground="#388e3c",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            padx=16,
            pady=6,
        )
        self.stop_btn = tk.Button(
            buttons,
            text="Stop",
            command=self.on_stop,
            state=tk.DISABLED,
            bg="#c62828",
            fg="white",
            activebackground="#e53935",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            padx=16,
            pady=6,
        )
        self.delete_btn = ttk.Button(buttons, text="Delete Selected", command=self.on_delete)
        self.edit_btn = ttk.Button(buttons, text="Edit Selected", command=self.on_edit_selected)

        self.start_btn.pack(side=tk.LEFT, padx=4)
        self.stop_btn.pack(side=tk.LEFT, padx=4)
        self.edit_btn.pack(side=tk.LEFT, padx=4)
        self.delete_btn.pack(side=tk.LEFT, padx=4)

        stats = ttk.Frame(container)
        stats.pack(fill=tk.X)
        self.live_label = ttk.Label(stats, text="Not running", font=("Segoe UI", 11, "bold"))
        self.today_total = ttk.Label(stats, text="Today: 00:00:00")
        self.live_label.pack(side=tk.LEFT, padx=4, pady=6)
        self.today_total.pack(side=tk.RIGHT, padx=4, pady=6)

        # Filters
        filters = ttk.LabelFrame(container, text="Filter")
        filters.pack(fill=tk.X, pady=(4, 0))

        ttk.Label(filters, text="From").grid(row=0, column=0, sticky=tk.W, padx=6, pady=6)
        ttk.Label(filters, text="To").grid(row=0, column=2, sticky=tk.W, padx=6, pady=6)
        ttk.Label(filters, text="Project").grid(row=0, column=4, sticky=tk.W, padx=6, pady=6)

        self.from_entry = ttk.Entry(filters)
        self.to_entry = ttk.Entry(filters)
        self.filter_project_cb = ttk.Combobox(filters, values=["(any)"])
        self.filter_project_cb.set("(any)")

        today = date.today()
        first_of_month = today.replace(day=1)
        self.from_entry.insert(0, first_of_month.isoformat())
        self.to_entry.insert(0, today.isoformat())

        self.from_entry.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=6)
        self.to_entry.grid(row=0, column=3, sticky=tk.EW, padx=6, pady=6)
        self.filter_project_cb.grid(row=0, column=5, sticky=tk.EW, padx=6, pady=6)

        filters.columnconfigure(1, weight=1)
        filters.columnconfigure(3, weight=1)
        filters.columnconfigure(5, weight=1)

        self.apply_filter_btn = ttk.Button(filters, text="Apply", command=self._refresh_table)
        self.apply_filter_btn.grid(row=0, column=6, padx=6)

        # Table
        table_frame = ttk.Frame(container)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=6)

        cols = ("id", "project", "task", "notes", "start", "end", "duration")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        for c, w in zip(cols, (60, 140, 220, 240, 140, 140, 110)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # bindings
        self.tree.bind("<Double-1>", lambda e: self.on_edit_selected())
        self.master.bind("<Control-Return>", lambda e: self.on_start())
        self.master.bind("<Escape>", lambda e: self.on_stop())

    # --- actions ---
    def _load_projects(self):
        names = self.store.projects()
        self.project_cb["values"] = names
        self.filter_project_cb["values"] = ["(any)"] + names

    def _load_running(self):
        running = self.store.get_running_entry()
        if running:
            self.running_id = int(running["id"])
            self.start_btn.configure(state=tk.DISABLED)
            self.stop_btn.configure(state=tk.NORMAL)
            self.live_label.configure(text=f"Running: {running['project']} — {running['task']}")
        else:
            self.running_id = None
            self.start_btn.configure(state=tk.NORMAL)
            self.stop_btn.configure(state=tk.DISABLED)
            self.live_label.configure(text="Not running")
        self._update_today_total()

    def _tick(self):
        # update live timer + today total every 1s
        self._update_today_total()
        self.timer_job = self.after(1000, self._tick)

    def _update_today_total(self):
        total = self.store.sum_today()
        self.today_total.configure(text=f"Today: {pretty_duration(total)}")

        if self.running_id:
            r = self.store.get_running_entry()
            if r:
                start = datetime.fromisoformat(r["start_ts"])
                elapsed = int((datetime.now() - start).total_seconds())
                self.live_label.configure(text=f"Running: {r['project']} — {r['task']} ({pretty_duration(elapsed)})")

    def _refresh_table(self):
        # get filters
        try:
            start_date = date.fromisoformat(self.from_entry.get().strip())
            end_date = date.fromisoformat(self.to_entry.get().strip())
            if end_date < start_date:
                raise ValueError
        except Exception:
            messagebox.showerror("Invalid dates", "Enter valid ISO dates YYYY-MM-DD.")
            return

        project = self.filter_project_cb.get().strip()
        if project == "(any)":
            project = None

        rows = self.store.query_entries(start_date, end_date, project)

        # clear
        for iid in self.tree.get_children():
            self.tree.delete(iid)

        for r in rows:
            dur = r["duration_s"]
            if dur is None and r["start_ts"] and r["end_ts"]:
                start = datetime.fromisoformat(r["start_ts"])
                end = datetime.fromisoformat(r["end_ts"]) if r["end_ts"] else datetime.now()
                dur = int((end - start).total_seconds())
            self.tree.insert("", tk.END, values=(
                r["id"], r["project"], r["task"], r["notes"], r["start_ts"], r["end_ts"], pretty_duration(int(dur or 0))
            ))

    def on_start(self):
        project = self.state.project.get().strip()
        task = self.state.task.get().strip()
        notes = self.state.notes.get().strip()
        if not project:
            messagebox.showwarning("Missing project", "Please enter a project name.")
            return
        if not task:
            task = "(untitled)"
        try:
            self.running_id = self.store.start_entry(project, task, notes)
        except Exception as ex:
            messagebox.showerror("Cannot start", str(ex))
            return
        self.state.notes.set("")
        self._load_projects()
        self._load_running()
        self._refresh_table()

    def on_stop(self):
        if not self.running_id:
            return
        try:
            self.store.stop_entry(self.running_id)
        except Exception as ex:
            messagebox.showerror("Cannot stop", str(ex))
            return
        finally:
            self.running_id = None
        self._load_running()
        self._refresh_table()

    def on_delete(self):
        sel = self._selected_id()
        if not sel:
            return
        if messagebox.askyesno("Delete", "Delete the selected entry?"):
            self.store.delete_entry(sel)
            self._refresh_table()
            self._update_today_total()

    def _selected_id(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        vals = self.tree.item(iid, "values")
        return int(vals[0]) if vals else None

    def on_edit_selected(self):
        sel_id = self._selected_id()
        if not sel_id:
            return
        EditDialog(self.master, self.store, sel_id, on_saved=self._after_edit)

    def _after_edit(self):
        self._refresh_table()
        self._update_today_total()
        self._load_projects()

    def on_export_csv(self):
        # export filtered rows
        try:
            start_date = date.fromisoformat(self.from_entry.get().strip())
            end_date = date.fromisoformat(self.to_entry.get().strip())
        except Exception:
            messagebox.showerror("Invalid dates", "Enter valid ISO dates YYYY-MM-DD.")
            return
        project = self.filter_project_cb.get().strip()
        if project == "(any)":
            project = None
        rows = self.store.query_entries(start_date, end_date, project)
        if not rows:
            messagebox.showinfo("Nothing to export", "No rows for selected filter.")
            return
        dest = filedialog.asksaveasfilename(
            title="Export CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            initialfile=f"timetracker_{start_date}_{end_date}.csv",
        )
        if not dest:
            return
        try:
            self.store.export_csv(rows, Path(dest))
            messagebox.showinfo("Exported", f"Saved to {dest}")
        except Exception as ex:
            messagebox.showerror("Export failed", str(ex))

    def on_about(self):
        messagebox.showinfo(
            "About",
            f"{APP_NAME}\nSimple cross‑platform time tracking.\nDatabase: {self.store.path}",
        )


class EditDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, store: Store, entry_id: int, on_saved):
        super().__init__(master)
        self.title("Edit Entry")
        self.resizable(False, False)
        self.store = store
        self.entry_id = entry_id
        self.on_saved = on_saved

        self.vars = {
            "project": tk.StringVar(),
            "task": tk.StringVar(),
            "notes": tk.StringVar(),
            "start": tk.StringVar(),
            "end": tk.StringVar(),
            "duration": tk.StringVar(),
        }

        cur = store.conn.cursor()
        cur.execute(
            "SELECT e.*, p.name as project FROM entries e JOIN projects p ON e.project_id=p.id WHERE e.id=?",
            (entry_id,),
        )
        row = cur.fetchone()
        if not row:
            self.destroy()
            return

        self.vars["project"].set(row["project"])
        self.vars["task"].set(row["task"])
        self.vars["notes"].set(row["notes"] or "")
        self.vars["start"].set(row["start_ts"])
        self.vars["end"].set(row["end_ts"] or "")
        dur = row["duration_s"]
        if not dur and row["end_ts"]:
            start = datetime.fromisoformat(row["start_ts"]) ; end = datetime.fromisoformat(row["end_ts"]) ; dur = int((end-start).total_seconds())
        self.vars["duration"].set(pretty_duration(int(dur or 0)))

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text="Project").grid(row=0, column=0, sticky=tk.W, padx=6, pady=6)
        self.project_cb = ttk.Combobox(frm, textvariable=self.vars["project"], values=self.store.projects())
        self.project_cb.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=6)

        ttk.Label(frm, text="Task").grid(row=1, column=0, sticky=tk.W, padx=6, pady=6)
        ttk.Entry(frm, textvariable=self.vars["task"]).grid(row=1, column=1, sticky=tk.EW, padx=6, pady=6)

        ttk.Label(frm, text="Notes").grid(row=2, column=0, sticky=tk.W, padx=6, pady=6)
        ttk.Entry(frm, textvariable=self.vars["notes"]).grid(row=2, column=1, sticky=tk.EW, padx=6, pady=6)

        ttk.Label(frm, text="Start (ISO)").grid(row=3, column=0, sticky=tk.W, padx=6, pady=6)
        ttk.Entry(frm, textvariable=self.vars["start"]).grid(row=3, column=1, sticky=tk.EW, padx=6, pady=6)

        ttk.Label(frm, text="End (ISO or empty)").grid(row=4, column=0, sticky=tk.W, padx=6, pady=6)
        ttk.Entry(frm, textvariable=self.vars["end"]).grid(row=4, column=1, sticky=tk.EW, padx=6, pady=6)

        self.error_lbl = ttk.Label(frm, text="", foreground="red")
        self.error_lbl.grid(row=5, column=0, columnspan=2, sticky=tk.W, padx=6)

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky=tk.E, pady=(8, 0))
        ttk.Button(btns, text="Save", command=self.on_save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=4)

        frm.columnconfigure(1, weight=1)

    def on_save(self):
        proj = self.vars["project"].get().strip()
        task = self.vars["task"].get().strip() or "(untitled)"
        notes = self.vars["notes"].get().strip()
        start_txt = self.vars["start"].get().strip()
        end_txt = self.vars["end"].get().strip()
        try:
            # basic validation
            _ = datetime.fromisoformat(start_txt)
            end_iso = None
            if end_txt:
                _ = datetime.fromisoformat(end_txt)
                end_iso = end_txt
            self.store.update_entry(self.entry_id, proj, task, notes, start_txt, end_iso)
            self.on_saved()
            self.destroy()
        except Exception as ex:
            self.error_lbl.configure(text=str(ex))


# ----------------------------
# Entry point
# ----------------------------

def main():
    db_path = user_data_dir() / DB_NAME
    store = Store(db_path)

    root = tk.Tk()
    # platform‑aware ttk styling
    try:
        from tkinter import font as tkfont
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(size=10)
    except Exception:
        pass

    style = ttk.Style()
    # Use 'clam' for better cross‑platform look
    try:
        style.theme_use('clam')
    except Exception:
        pass

    app = TimeTrackerApp(root, store)
    app.pack(fill=tk.BOTH, expand=True)

    def on_close():
        # ensure running entry is left as is; do not auto‑stop
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
