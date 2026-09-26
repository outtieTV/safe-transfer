#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
PyQt6 front‑end for the CopyMove PowerShell utility.

Features
--------
* Browse source && destination folders
* Choose Copy or Move
* Start, Pause, Resume the operation
* Live view of the PowerShell log file
* Simple progress bar (files processed / total)
* Handles the special exit‑code 999 that signals a pause
"""

import sys
import os
import subprocess
import threading
import time
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton,
    QFileDialog, QVBoxLayout, QHBoxLayout, QTextEdit,
    QProgressBar, QMessageBox, QCheckBox
)
from PyQt6.QtCore import Qt, QTimer

# ----------------------------------------------------------------------
# Configuration (adjust if you move the PowerShell script)
SCRIPT_NAME = "CopyMove.ps1"
SCRIPT_PATH = Path(__file__).with_name(SCRIPT_NAME)

# The PowerShell script writes its history to this file (default from the .ps1)
LOG_FILE = Path.home() / "copy_move_history.txt"

# ----------------------------------------------------------------------
class CopyMoveGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Copy/Move Assistant")
        self.setMinimumSize(600, 400)

        # ----- UI -----
        # Source selector
        self.src_edit = QLineEdit()
        src_btn = QPushButton("Browse…")
        src_btn.clicked.connect(self.browse_src)

        # Destination selector
        self.dst_edit = QLineEdit()
        dst_btn = QPushButton("Browse…")
        dst_btn.clicked.connect(self.browse_dst)

        # Operation checkboxes
        self.move_chk = QCheckBox("Move (delete source after success)")
        self.move_chk.setChecked(False)          # default = copy

        # Control buttons
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_job)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self.pause_job)

        self.resume_btn = QPushButton("Resume")
        self.resume_btn.setEnabled(False)
        self.resume_btn.clicked.connect(self.resume_job)

        # Log view
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)

        # Progress bar
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)

        # Layout assembly
        src_layout = QHBoxLayout()
        src_layout.addWidget(QLabel("Source:"))
        src_layout.addWidget(self.src_edit)
        src_layout.addWidget(src_btn)

        dst_layout = QHBoxLayout()
        dst_layout.addWidget(QLabel("Destination:"))
        dst_layout.addWidget(self.dst_edit)
        dst_layout.addWidget(dst_btn)

        ctrl_layout = QHBoxLayout()
        ctrl_layout.addWidget(self.start_btn)
        ctrl_layout.addWidget(self.pause_btn)
        ctrl_layout.addWidget(self.resume_btn)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.move_chk)

        main_layout = QVBoxLayout()
        main_layout.addLayout(src_layout)
        main_layout.addLayout(dst_layout)
        main_layout.addLayout(ctrl_layout)
        main_layout.addWidget(QLabel("Operation log:"))
        main_layout.addWidget(self.log_view)
        main_layout.addWidget(self.progress)

        self.setLayout(main_layout)

        # ----- State -----
        self.process = None          # subprocess.Popen instance
        self.total_files = 0
        self.processed_files = 0
        self.log_watcher = QTimer()
        self.log_watcher.timeout.connect(self.update_log)
        self.log_watcher.start(1000)   # poll log every second

    # ------------------------------------------------------------------
    # UI helpers
    def browse_src(self):
        path = QFileDialog.getExistingDirectory(self, "Select source folder")
        if path:
            self.src_edit.setText(path)

    def browse_dst(self):
        path = QFileDialog.getExistingDirectory(self, "Select destination folder")
        if path:
            self.dst_edit.setText(path)

    # ------------------------------------------------------------------
    # Core actions
    def start_job(self):
        src = self.src_edit.text().strip()
        dst = self.dst_edit.text().strip()
        if not src or not dst:
            QMessageBox.warning(self, "Missing paths", "Please select both source and destination.")
            return
        if not Path(src).exists():
            QMessageBox.warning(self, "Invalid source", "Source path does not exist.")
            return

        # Build PowerShell command
        args = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT_PATH),
            "-Source", src,
            "-Destination", dst
        ]
        if self.move_chk.isChecked():
            args.append("-Move")

        # Reset counters
        self.processed_files = 0
        self.total_files = self.count_files(src)
        self.progress.setMaximum(self.total_files)
        self.progress.setValue(0)

        # Launch process in a separate thread to keep UI responsive
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
        threading.Thread(target=self.monitor_process, daemon=True).start()

        self.log_view.append("=== Job started ===")
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.resume_btn.setEnabled(False)

    def pause_job(self):
        if not self.process:
            return
        # The PowerShell script checks the JSON state file.
        # We set the "Paused" flag to true.
        state_path = Path(__file__).with_name(".pauseResumeState.json")
        if not state_path.exists():
            QMessageBox.warning(self, "Pause error", "State file not found – cannot pause.")
            return
        try:
            import json
            with state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            data["Paused"] = True
            with state_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.log_view.append("=== Pause requested – will halt after current file ===")
            self.pause_btn.setEnabled(False)
            self.resume_btn.setEnabled(True)
        except Exception as e:
            QMessageBox.critical(self, "Pause failed", str(e))

    def resume_job(self):
        src = self.src_edit.text().strip()
        dst = self.dst_edit.text().strip()
        if not src or not dst:
            QMessageBox.warning(self, "Missing paths", "Source/Destination required for resume.")
            return

        args = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT_PATH),
            "-Resume"
        ]

        # Reset UI state
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.resume_btn.setEnabled(False)

        self.process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
        threading.Thread(target=self.monitor_process, daemon=True).start()
        self.log_view.append("=== Resume started ===")

    # ------------------------------------------------------------------
    # Helper: count total files (used for progress bar)
    def count_files(self, root_path: str) -> int:
        return sum(1 for _ in Path(root_path).rglob("*") if _.is_file())

    # ------------------------------------------------------------------
    # Process monitor thread – reads stdout line‑by‑line, updates UI
    def monitor_process(self):
        if not self.process:
            return
        for line in iter(self.process.stdout.readline, b''):
            txt = line.decode(errors="ignore").rstrip()
            # Simple heuristic: every line that mentions a file copy can increment the counter.
            # Adjust this if your PowerShell script outputs more structured info.
            if "copied" in txt.lower() or "moved" in txt.lower():
                self.processed_files += 1
                self.progress.setValue(self.processed_files)
        self.process.wait()
        exit_code = self.process.returncode
        self.process = None

        # UI updates must happen on the main thread
        def finish():
            self.start_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.resume_btn.setEnabled(False)
            if exit_code == 999:
                self.log_view.append("=== Job paused (PowerShell exited with code 999) ===")
                # keep Pause disabled, allow Resume
                self.resume_btn.setEnabled(True)
            elif exit_code == 0:
                self.log_view.append("=== Job finished successfully ===")
            else:
                self.log_view.append(f"=== Job terminated with error (code {exit_code}) ===")
        QTimer.singleShot(0, finish)

    # ------------------------------------------------------------------
    # Log tailer – periodically reads the log file and appends new lines
    def update_log(self):
        if not LOG_FILE.exists():
            return
        try:
            with LOG_FILE.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return

        # Keep only the last 200 lines to avoid UI overload
        displayed = self.log_view.toPlainText().splitlines()
        new = [ln for ln in lines if ln.strip() not in displayed]
        if new:
            for ln in new[-200:]:
                self.log_view.append(ln.rstrip())
            # Auto‑scroll to bottom
            self.log_view.verticalScrollBar().setValue(
                self.log_view.verticalScrollBar().maximum()
            )
    # ------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    gui = CopyMoveGUI()
    gui.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
