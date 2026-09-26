#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Unified Windows File Copy / Move / Missing Files Utility
=========================================================

PyQt6 application providing:

    • Copy files
    • Move files
    • Find missing files
    • Copy missing files
    • Pause / Resume
    • Skip existing files with the same size
    • Optional free-space pre-flight check
    • Progress reporting
    • Transfer speed
    • ETA
    • Source size / destination free-space information
    • Persistent logging
    • JSON pause/resume state
    • Per-file error handling
    • File verification
    • Background worker threads
    • Windows-friendly filesystem handling

Requirements:

    Python 3.10+
    PyQt6

Install PyQt6:

    py -m pip install PyQt6

Run:

    py unified_file_manager.py

Log:

    %USERPROFILE%\\copy_move_history.txt

State:

    .pauseResumeState.json

The state file is stored beside this Python script.
"""


# ======================================================================
# IMPORTS
# ======================================================================

import sys
import os
import json
import time
import shutil
import threading
from pathlib import Path
from datetime import datetime, timedelta

from PyQt6.QtCore import (
    Qt,
    QThread,
    pyqtSignal,
)

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTextEdit,
    QProgressBar,
    QMessageBox,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QFormLayout,
)


# ======================================================================
# PATHS
# ======================================================================

SCRIPT_DIR = Path(__file__).resolve().parent

LOG_FILE = (
    Path.home()
    / "copy_move_history.txt"
)

STATE_FILE = (
    SCRIPT_DIR
    / ".pauseResumeState.json"
)


# ======================================================================
# GENERAL UTILITIES
# ======================================================================

def human_bytes(value: int | float) -> str:
    """Convert bytes to a human-readable value."""

    value = float(value)

    units = (
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
        "PiB",
    )

    for unit in units:

        if value < 1024.0:

            return f"{value:.2f} {unit}"

        value /= 1024.0

    return f"{value:.2f} PiB"


def format_seconds(seconds: float) -> str:
    """Convert seconds to a readable duration."""

    if seconds <= 0:
        return "—"

    seconds = int(seconds)

    hours, remainder = divmod(
        seconds,
        3600,
    )

    minutes, seconds = divmod(
        remainder,
        60,
    )

    if hours:
        return f"{hours}h {minutes}m {seconds}s"

    if minutes:
        return f"{minutes}m {seconds}s"

    return f"{seconds}s"


def free_space(path: Path) -> int:
    """
    Return free space on the filesystem containing path.

    shutil.disk_usage works on Windows, Linux and macOS.
    """

    return shutil.disk_usage(
        path
    ).free


def folder_size(root: Path) -> int:
    """
    Calculate the total size of all files under root.

    Inaccessible files are ignored.
    """

    total = 0

    for current_root, dirs, files in os.walk(root):

        for name in files:

            file_path = (
                Path(current_root)
                / name
            )

            try:

                total += (
                    file_path.stat().st_size
                )

            except (
                OSError,
                PermissionError,
            ):

                pass

    return total


def count_files(root: Path) -> int:
    """Count files under root."""

    count = 0

    for current_root, dirs, files in os.walk(root):

        count += len(files)

    return count


def append_log(message: str):
    """Append a timestamped message to the persistent log."""

    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    line = (
        f"{timestamp}\t{message}"
    )

    try:

        with LOG_FILE.open(
            "a",
            encoding="utf-8",
        ) as f:

            f.write(
                line
                + "\n"
            )

    except Exception:
        # Logging should never crash the application.
        pass


# ======================================================================
# STATE MANAGEMENT
# ======================================================================

def load_state() -> dict | None:
    """Load pause/resume state."""

    if not STATE_FILE.exists():
        return None

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as f:

            return json.load(f)

    except Exception:

        return None


def save_state(data: dict):
    """Safely save pause/resume state."""

    temp_file = STATE_FILE.with_suffix(
        ".tmp"
    )

    try:

        with temp_file.open(
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
                indent=4,
            )

        temp_file.replace(
            STATE_FILE
        )

    except Exception as e:

        append_log(
            f"STATE SAVE ERROR: {e}"
        )


def delete_state():

    try:

        if STATE_FILE.exists():

            STATE_FILE.unlink()

    except Exception as e:

        append_log(
            f"STATE DELETE ERROR: {e}"
        )


# ======================================================================
# FILE SCANNING
# ======================================================================

class FileScanner(QThread):
    """
    Scans a source directory and builds a file list.

    Files are returned sorted largest -> smallest.
    """

    progress = pyqtSignal(int)

    status = pyqtSignal(str)

    result = pyqtSignal(
        list,
        int,
        int,
    )

    error = pyqtSignal(str)

    def __init__(
        self,
        source: Path,
    ):

        super().__init__()

        self.source = source

    def run(self):

        try:

            files = []

            inaccessible = 0

            self.status.emit(
                "Scanning source directory..."
            )

            for current_root, dirs, filenames in os.walk(
                self.source
            ):

                for filename in filenames:

                    path = (
                        Path(current_root)
                        / filename
                    )

                    try:

                        size = path.stat().st_size

                        files.append(
                            (
                                path,
                                size,
                            )
                        )

                    except (
                        OSError,
                        PermissionError,
                    ):

                        inaccessible += 1

            total = len(files)

            self.status.emit(
                f"Found {total:,} files."
            )

            # Largest files first.
            files.sort(
                key=lambda x: x[1],
                reverse=True,
            )

            self.progress.emit(
                100
            )

            self.result.emit(
                files,
                total,
                inaccessible,
            )

        except Exception as e:

            self.error.emit(
                f"Source scan failed:\n\n{e}"
            )


# ======================================================================
# MISSING FILE SCANNER
# ======================================================================

class MissingFilesScanner(QThread):
    """
    Finds files present in source but missing from destination.

    A destination file is considered present if it exists.

    If Skip Existing is enabled, matching-size files are also skipped
    when later performing the copy.
    """

    progress = pyqtSignal(int)

    status = pyqtSignal(str)

    result = pyqtSignal(
        list,
        int,
        int,
    )

    error = pyqtSignal(str)

    def __init__(
        self,
        source: Path,
        destination: Path,
    ):

        super().__init__()

        self.source = source
        self.destination = destination

    def run(self):

        try:

            all_files = []

            inaccessible = 0

            self.status.emit(
                "Finding missing files..."
            )

            for current_root, dirs, filenames in os.walk(
                self.source
            ):

                for filename in filenames:

                    src = (
                        Path(current_root)
                        / filename
                    )

                    try:

                        relative = src.relative_to(
                            self.source
                        )

                        all_files.append(
                            (
                                src,
                                relative,
                            )
                        )

                    except (
                        OSError,
                        PermissionError,
                        ValueError,
                    ):

                        inaccessible += 1

            total = len(all_files)

            missing = []

            if total == 0:

                self.result.emit(
                    [],
                    0,
                    inaccessible,
                )

                return

            for index, (
                src,
                relative,
            ) in enumerate(
                all_files,
                start=1,
            ):

                try:

                    destination_file = (
                        self.destination
                        / relative
                    )

                    if not destination_file.is_file():

                        missing.append(
                            relative
                        )

                    self.status.emit(
                        f"Checking "
                        f"{index:,}/{total:,}: "
                        f"{relative}"
                    )

                except (
                    OSError,
                    PermissionError,
                ):

                    inaccessible += 1

                self.progress.emit(
                    int(
                        index
                        / total
                        * 100
                    )
                )

            self.result.emit(
                missing,
                total,
                inaccessible,
            )

        except Exception as e:

            self.error.emit(
                f"Missing-file scan failed:\n\n{e}"
            )


# ======================================================================
# TRANSFER WORKER
# ======================================================================

class TransferWorker(QThread):
    """
    Handles copy or move operations.

    This worker supports:

        • pause
        • resume
        • skip existing
        • verification
        • logging
        • progress
        • speed
        • ETA
        • per-file error handling
    """

    progress = pyqtSignal(int)

    status = pyqtSignal(str)

    statistics = pyqtSignal(
        str
    )

    log = pyqtSignal(
        str
    )

    finished_result = pyqtSignal(
        dict
    )

    error = pyqtSignal(
        str
    )

    def __init__(
        self,
        source: Path,
        destination: Path,
        files: list,
        move: bool,
        skip_existing: bool,
        ignore_space: bool,
        start_index: int = 0,
    ):

        super().__init__()

        self.source = source
        self.destination = destination

        self.files = files

        self.move = move

        self.skip_existing = (
            skip_existing
        )

        self.ignore_space = (
            ignore_space
        )

        self.start_index = (
            start_index
        )

        self.pause_requested = False

        self.stop_requested = False

        self.started = time.monotonic()

        self.bytes_copied = 0

        self.files_copied = 0

        self.files_skipped = 0

        self.failed = []

    # ------------------------------------------------------------------
    # External controls
    # ------------------------------------------------------------------

    def request_pause(self):

        self.pause_requested = True

    def request_stop(self):

        self.stop_requested = True

    # ------------------------------------------------------------------
    # Main worker
    # ------------------------------------------------------------------

    def run(self):

        total_files = len(
            self.files
        )

        if total_files == 0:

            self.finished_result.emit(
                {
                    "success": True,
                    "paused": False,
                    "copied": 0,
                    "skipped": 0,
                    "failed": [],
                    "bytes": 0,
                }
            )

            return

        # --------------------------------------------------------------
        # Create state
        # --------------------------------------------------------------

        state = {
            "Source": str(
                self.source
            ),
            "Destination": str(
                self.destination
            ),
            "Move": self.move,
            "SkipExisting": (
                self.skip_existing
            ),
            "IgnoreSpace": (
                self.ignore_space
            ),
            "Index": self.start_index,
            "Paused": False,
        }

        # --------------------------------------------------------------
        # Calculate required space
        # --------------------------------------------------------------

        if not self.ignore_space:

            try:

                required = sum(
                    size
                    for path, size
                    in self.files[
                        self.start_index:
                    ]
                )

                available = free_space(
                    self.destination
                )

                if required > available:

                    message = (
                        "Insufficient free space.\n\n"
                        f"Needed: "
                        f"{human_bytes(required)}\n"
                        f"Available: "
                        f"{human_bytes(available)}"
                    )

                    self.log.emit(
                        "FAILED - insufficient "
                        "free space."
                    )

                    self.finished_result.emit(
                        {
                            "success": False,
                            "paused": False,
                            "copied": 0,
                            "skipped": 0,
                            "failed": [],
                            "bytes": 0,
                            "message": message,
                        }
                    )

                    return

            except Exception as e:

                self.error.emit(
                    f"Could not determine "
                    f"destination free space:\n\n{e}"
                )

                return

        # --------------------------------------------------------------
        # Transfer loop
        # --------------------------------------------------------------

        for index in range(
            self.start_index,
            total_files,
        ):

            if self.stop_requested:

                break

            src_file, size = (
                self.files[index]
            )

            try:

                relative = (
                    src_file.relative_to(
                        self.source
                    )
                )

            except ValueError:

                relative = Path(
                    src_file.name
                )

            dst_file = (
                self.destination
                / relative
            )

            self.status.emit(
                f"{index + 1:,} / "
                f"{total_files:,}    "
                f"{relative}"
            )

            # ----------------------------------------------------------
            # Skip existing
            # ----------------------------------------------------------

            if (
                self.skip_existing
                and dst_file.is_file()
            ):

                try:

                    destination_size = (
                        dst_file.stat().st_size
                    )

                    if (
                        destination_size
                        == size
                    ):

                        self.files_skipped += 1

                        self.log.emit(
                            f"SKIPPED - "
                            f"{relative} "
                            f"(same size)"
                        )

                        state["Index"] = (
                            index + 1
                        )

                        save_state(
                            state
                        )

                        self.update_statistics(
                            index + 1,
                            total_files,
                        )

                        # For Move mode, a same-size destination
                        # is considered successfully synchronized.
                        #
                        # The source is NOT deleted immediately.
                        # It will only be deleted after the entire
                        # operation completes successfully.

                        continue

                except Exception as e:

                    self.log.emit(
                        f"WARNING - could not "
                        f"inspect destination "
                        f"{relative}: {e}"
                    )

            # ----------------------------------------------------------
            # Ensure destination directory exists
            # ----------------------------------------------------------

            try:

                dst_file.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            except Exception as e:

                self.failed.append(
                    (
                        str(relative),
                        str(e),
                    )
                )

                self.log.emit(
                    f"FAILED - mkdir - "
                    f"{relative}: {e}"
                )

                state["Index"] = (
                    index + 1
                )

                save_state(
                    state
                )

                continue

            # ----------------------------------------------------------
            # Copy
            # ----------------------------------------------------------

            try:

                shutil.copy2(
                    src_file,
                    dst_file,
                )

                # ------------------------------------------------------
                # Verify destination size
                # ------------------------------------------------------

                destination_size = (
                    dst_file.stat().st_size
                )

                if (
                    destination_size
                    != size
                ):

                    raise IOError(
                        "Size mismatch "
                        "after copy."
                    )

                self.bytes_copied += (
                    size
                )

                self.files_copied += 1

                operation = (
                    "MOVED"
                    if self.move
                    else "COPIED"
                )

                self.log.emit(
                    f"{operation} - "
                    f"{relative} "
                    f"({human_bytes(size)})"
                )

            except Exception as e:

                self.failed.append(
                    (
                        str(relative),
                        str(e),
                    )
                )

                self.log.emit(
                    f"FAILED - "
                    f"{relative}: {e}"
                )

                # Do not stop the entire job for one bad file.
                #
                # This is more robust than the original PowerShell
                # script, which stopped at the first failure.

            # ----------------------------------------------------------
            # Save resume state
            # ----------------------------------------------------------

            state["Index"] = (
                index + 1
            )

            save_state(
                state
            )

            # ----------------------------------------------------------
            # Progress
            # ----------------------------------------------------------

            self.update_statistics(
                index + 1,
                total_files,
            )

            # ----------------------------------------------------------
            # Pause
            # ----------------------------------------------------------

            if self.pause_requested:

                state["Paused"] = True

                save_state(
                    state
                )

                self.log.emit(
                    f"PAUSED at file "
                    f"{index + 1:,}."
                )

                self.finished_result.emit(
                    {
                        "success": False,
                        "paused": True,
                        "copied": self.files_copied,
                        "skipped": self.files_skipped,
                        "failed": self.failed,
                        "bytes": self.bytes_copied,
                        "index": index + 1,
                    }
                )

                return

        # --------------------------------------------------------------
        # Final result
        # --------------------------------------------------------------

        success = (
            len(self.failed)
            == 0
        )

        self.finished_result.emit(
            {
                "success": success,
                "paused": False,
                "copied": self.files_copied,
                "skipped": self.files_skipped,
                "failed": self.failed,
                "bytes": self.bytes_copied,
                "index": total_files,
            }
        )

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def update_statistics(
        self,
        completed,
        total,
    ):

        elapsed = (
            time.monotonic()
            - self.started
        )

        speed = (
            self.bytes_copied
            / elapsed
            if elapsed > 0
            else 0
        )

        remaining_files = (
            total - completed
        )

        if (
            completed > 0
            and elapsed > 0
        ):

            average_per_file = (
                elapsed
                / completed
            )

            eta = (
                average_per_file
                * remaining_files
            )

        else:

            eta = 0

        percent = int(
            completed
            / total
            * 100
        )

        self.progress.emit(
            percent
        )

        self.statistics.emit(
            (
                f"Files: "
                f"{completed:,}/{total:,}"
                f"    |    "
                f"Copied: "
                f"{human_bytes(self.bytes_copied)}"
                f"    |    "
                f"Skipped: "
                f"{self.files_skipped:,}"
                f"    |    "
                f"Speed: "
                f"{human_bytes(speed)}/s"
                f"    |    "
                f"ETA: "
                f"{format_seconds(eta)}"
            )
        )


# ======================================================================
# MAIN GUI
# ======================================================================

class FileManagerGUI(QWidget):

    def __init__(self):

        super().__init__()

        self.setWindowTitle(
            "Windows File Copy / Move Assistant"
        )

        self.resize(
            900,
            720,
        )

        # --------------------------------------------------------------
        # State
        # --------------------------------------------------------------

        self.worker = None

        self.scan_worker = None

        self.source = None

        self.destination = None

        self.file_list = []

        self.missing_files = []

        self.current_operation = None

        # --------------------------------------------------------------
        # Source
        # --------------------------------------------------------------

        self.source_edit = QLineEdit()

        self.source_button = QPushButton(
            "Browse..."
        )

        self.source_button.clicked.connect(
            self.browse_source
        )

        source_layout = QHBoxLayout()

        source_layout.addWidget(
            QLabel("Source:")
        )

        source_layout.addWidget(
            self.source_edit
        )

        source_layout.addWidget(
            self.source_button
        )

        # --------------------------------------------------------------
        # Destination
        # --------------------------------------------------------------

        self.destination_edit = QLineEdit()

        self.destination_button = QPushButton(
            "Browse..."
        )

        self.destination_button.clicked.connect(
            self.browse_destination
        )

        destination_layout = QHBoxLayout()

        destination_layout.addWidget(
            QLabel("Destination:")
        )

        destination_layout.addWidget(
            self.destination_edit
        )

        destination_layout.addWidget(
            self.destination_button
        )

        # --------------------------------------------------------------
        # Operation
        # --------------------------------------------------------------

        operation_group = QGroupBox(
            "Operation"
        )

        operation_layout = QHBoxLayout()

        self.operation_combo = QComboBox()

        self.operation_combo.addItems(
            [
                "Copy",
                "Move",
            ]
        )

        operation_layout.addWidget(
            QLabel("Mode:")
        )

        operation_layout.addWidget(
            self.operation_combo
        )

        operation_layout.addStretch()

        operation_group.setLayout(
            operation_layout
        )

        # --------------------------------------------------------------
        # Options
        # --------------------------------------------------------------

        options_group = QGroupBox(
            "Options"
        )

        options_layout = QHBoxLayout()

        self.skip_existing = QCheckBox(
            "Skip existing files with same size"
        )

        self.ignore_space = QCheckBox(
            "Ignore free-space check"
        )

        self.verify_files = QCheckBox(
            "Verify copied file size"
        )

        self.verify_files.setChecked(
            True
        )

        options_layout.addWidget(
            self.skip_existing
        )

        options_layout.addWidget(
            self.ignore_space
        )

        options_layout.addWidget(
            self.verify_files
        )

        options_group.setLayout(
            options_layout
        )

        # --------------------------------------------------------------
        # Main buttons
        # --------------------------------------------------------------

        self.start_button = QPushButton(
            "Start Copy / Move"
        )

        self.start_button.clicked.connect(
            self.start_transfer
        )

        self.find_button = QPushButton(
            "Find Missing"
        )

        self.find_button.clicked.connect(
            self.find_missing
        )

        self.copy_missing_button = QPushButton(
            "Copy Missing Files"
        )

        self.copy_missing_button.setEnabled(
            False
        )

        self.copy_missing_button.clicked.connect(
            self.copy_missing
        )

        self.pause_button = QPushButton(
            "Pause"
        )

        self.pause_button.setEnabled(
            False
        )

        self.pause_button.clicked.connect(
            self.pause_operation
        )

        self.resume_button = QPushButton(
            "Resume"
        )

        self.resume_button.setEnabled(
            False
        )

        self.resume_button.clicked.connect(
            self.resume_operation
        )

        button_layout = QHBoxLayout()

        button_layout.addWidget(
            self.start_button
        )

        button_layout.addWidget(
            self.find_button
        )

        button_layout.addWidget(
            self.copy_missing_button
        )

        button_layout.addWidget(
            self.pause_button
        )

        button_layout.addWidget(
            self.resume_button
        )

        # --------------------------------------------------------------
        # Statistics
        # --------------------------------------------------------------

        self.stats_label = QLabel(
            "Source size: —    |    "
            "Destination free: —"
        )

        self.stats_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        # --------------------------------------------------------------
        # Status
        # --------------------------------------------------------------

        self.status_label = QLabel(
            "Ready."
        )

        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        self.status_label.setWordWrap(
            True
        )

        # --------------------------------------------------------------
        # Progress
        # --------------------------------------------------------------

        self.progress = QProgressBar()

        self.progress.setRange(
            0,
            100,
        )

        self.progress.setValue(
            0
        )

        # --------------------------------------------------------------
        # Log
        # --------------------------------------------------------------

        self.log_view = QTextEdit()

        self.log_view.setReadOnly(
            True
        )

        # --------------------------------------------------------------
        # Layout
        # --------------------------------------------------------------

        main = QVBoxLayout()

        main.addLayout(
            source_layout
        )

        main.addLayout(
            destination_layout
        )

        main.addWidget(
            operation_group
        )

        main.addWidget(
            options_group
        )

        main.addWidget(
            self.stats_label
        )

        main.addWidget(
            self.status_label
        )

        main.addWidget(
            self.progress
        )

        main.addLayout(
            button_layout
        )

        main.addWidget(
            QLabel("Operation log:")
        )

        main.addWidget(
            self.log_view
        )

        self.setLayout(
            main
        )

        # --------------------------------------------------------------
        # Restore available resume state
        # --------------------------------------------------------------

        self.check_resume_state()

    # ==================================================================
    # UI UTILITIES
    # ==================================================================

    def append_log(
        self,
        message: str,
    ):

        self.log_view.append(
            message
        )

        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum()
        )

        append_log(
            message
        )

    def update_folder_statistics(self):

        source_text = (
            self.source_edit
            .text()
            .strip()
        )

        destination_text = (
            self.destination_edit
            .text()
            .strip()
        )

        source_size = None
        destination_free = None

        if source_text:

            try:

                source_size = folder_size(
                    Path(source_text)
                )

            except Exception:
                pass

        if destination_text:

            try:

                destination_free = free_space(
                    Path(destination_text)
                )

            except Exception:
                pass

        parts = []

        if source_size is not None:

            parts.append(
                f"Source size: "
                f"{human_bytes(source_size)}"
            )

        if destination_free is not None:

            parts.append(
                f"Destination free: "
                f"{human_bytes(destination_free)}"
            )

        self.stats_label.setText(
            "    |    ".join(parts)
            if parts
            else "Source size: —    |    Destination free: —"
        )

    def set_controls_enabled(
        self,
        enabled: bool,
    ):

        self.source_button.setEnabled(
            enabled
        )

        self.destination_button.setEnabled(
            enabled
        )

        self.source_edit.setEnabled(
            enabled
        )

        self.destination_edit.setEnabled(
            enabled
        )

        self.operation_combo.setEnabled(
            enabled
        )

        self.skip_existing.setEnabled(
            enabled
        )

        self.ignore_space.setEnabled(
            enabled
        )

        self.verify_files.setEnabled(
            enabled
        )

        self.start_button.setEnabled(
            enabled
        )

        self.find_button.setEnabled(
            enabled
        )

    # ==================================================================
    # BROWSING
    # ==================================================================

    def browse_source(self):

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Source Folder",
        )

        if directory:

            self.source_edit.setText(
                directory
            )

            self.update_folder_statistics()

    def browse_destination(self):

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Destination Folder",
        )

        if directory:

            self.destination_edit.setText(
                directory
            )

            self.update_folder_statistics()

    # ==================================================================
    # VALIDATION
    # ==================================================================

    def get_paths(self):

        source = Path(
            self.source_edit
            .text()
            .strip()
        )

        destination = Path(
            self.destination_edit
            .text()
            .strip()
        )

        if not source:

            QMessageBox.warning(
                self,
                "Missing Source",
                "Please select a source folder.",
            )

            return None, None

        if not source.exists():

            QMessageBox.warning(
                self,
                "Invalid Source",
                "The source folder does not exist.",
            )

            return None, None

        if not source.is_dir():

            QMessageBox.warning(
                self,
                "Invalid Source",
                "The source path is not a folder.",
            )

            return None, None

        if not destination:

            QMessageBox.warning(
                self,
                "Missing Destination",
                "Please select a destination folder.",
            )

            return None, None

        try:

            if source.resolve() == destination.resolve():

                QMessageBox.warning(
                    self,
                    "Invalid Paths",
                    "Source and destination cannot be the same folder.",
                )

                return None, None

        except Exception:
            pass

        try:

            destination.mkdir(
                parents=True,
                exist_ok=True,
            )

        except Exception as e:

            QMessageBox.critical(
                self,
                "Destination Error",
                f"Could not create destination folder:\n\n{e}",
            )

            return None, None

        return (
            source,
            destination,
        )

    # ==================================================================
    # FIND MISSING
    # ==================================================================

    def find_missing(self):

        if self.worker and self.worker.isRunning():

            QMessageBox.information(
                self,
                "Busy",
                "An operation is already running.",
            )

            return

        source, destination = (
            self.get_paths()
        )

        if not source:
            return

        self.source = source
        self.destination = destination

        self.missing_files.clear()

        self.copy_missing_button.setEnabled(
            False
        )

        self.progress.setValue(
            0
        )

        self.status_label.setText(
            "Finding missing files..."
        )

        self.set_controls_enabled(
            False
        )

        self.append_log(
            "=== Missing-file scan started ==="
        )

        self.scan_worker = MissingFilesScanner(
            source,
            destination,
        )

        self.scan_worker.progress.connect(
            self.progress.setValue
        )

        self.scan_worker.status.connect(
            self.status_label.setText
        )

        self.scan_worker.result.connect(
            self.missing_scan_finished
        )

        self.scan_worker.error.connect(
            self.operation_error
        )

        self.scan_worker.finished.connect(
            self.scan_finished
        )

        self.scan_worker.start()

    def missing_scan_finished(
        self,
        missing,
        total,
        inaccessible,
    ):

        self.missing_files = (
            missing
        )

        self.progress.setValue(
            100
        )

        if missing:

            self.append_log(
                f"Found "
                f"{len(missing):,} missing files."
            )

            self.status_label.setText(
                f"Found "
                f"{len(missing):,} missing "
                f"files out of "
                f"{total:,}."
            )

            self.copy_missing_button.setEnabled(
                True
            )

            # Replace log with missing file list.
            self.log_view.append(
                "\n--- Missing Files ---"
            )

            for path in missing:

                self.log_view.append(
                    str(path)
                )

        else:

            self.status_label.setText(
                "No missing files found."
            )

            self.append_log(
                "No missing files found."
            )

        if inaccessible:

            self.log_view.append(
                "\n--- Inaccessible Files ---"
            )

            self.log_view.append(
                f"{inaccessible:,} "
                f"file(s) could not be inspected."
            )

    def scan_finished(self):

        self.set_controls_enabled(
            True
        )

        self.scan_worker = None

    # ==================================================================
    # COPY MISSING
    # ==================================================================

    def copy_missing(self):

        if not self.missing_files:

            return

        if not self.source or not self.destination:

            QMessageBox.warning(
                self,
                "Missing Paths",
                "Source and destination are required.",
            )

            return

        answer = QMessageBox.question(
            self,
            "Copy Missing Files",
            (
                f"Copy "
                f"{len(self.missing_files):,} "
                f"missing file(s) "
                f"to the destination?"
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:

            return

        # Build file list with sizes.
        files = []

        for relative in self.missing_files:

            src = (
                self.source
                / relative
            )

            try:

                size = (
                    src.stat().st_size
                )

                files.append(
                    (
                        src,
                        size,
                    )
                )

            except Exception as e:

                self.append_log(
                    f"FAILED TO STAT "
                    f"{relative}: {e}"
                )

        self.start_transfer_with_files(
            files,
            move=False,
        )

    # ==================================================================
    # START NORMAL TRANSFER
    # ==================================================================

    def start_transfer(self):

        if self.worker and self.worker.isRunning():

            QMessageBox.information(
                self,
                "Busy",
                "An operation is already running.",
            )

            return

        source, destination = (
            self.get_paths()
        )

        if not source:
            return

        self.source = source
        self.destination = destination

        self.append_log(
            "=== Building file list ==="
        )

        self.set_controls_enabled(
            False
        )

        self.progress.setValue(
            0
        )

        self.status_label.setText(
            "Scanning source..."
        )

        self.scan_worker = FileScanner(
            source
        )

        self.scan_worker.progress.connect(
            self.progress.setValue
        )

        self.scan_worker.status.connect(
            self.status_label.setText
        )

        self.scan_worker.result.connect(
            self.file_list_ready
        )

        self.scan_worker.error.connect(
            self.operation_error
        )

        self.scan_worker.finished.connect(
            self.scan_finished_for_transfer
        )

        self.scan_worker.start()

    def file_list_ready(
        self,
        files,
        total,
        inaccessible,
    ):

        self.file_list = files

        if not files:

            QMessageBox.warning(
                self,
                "No Files",
                "No files were found in the source.",
            )

            return

        if inaccessible:

            self.append_log(
                f"WARNING: "
                f"{inaccessible:,} inaccessible "
                f"file(s) were skipped."
            )

        total_bytes = sum(
            size
            for path, size
            in files
        )

        # --------------------------------------------------------------
        # Time estimate
        # --------------------------------------------------------------

        estimated_seconds = (
            total_bytes
            / (50 * 1024 * 1024)
        )

        if estimated_seconds >= 3600:

            answer = QMessageBox.question(
                self,
                "Long Operation",
                (
                    f"Estimated transfer time "
                    f"at 50 MiB/s: "
                    f"{format_seconds(estimated_seconds)}.\n\n"
                    "Continue?"
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
            )

            if (
                answer
                != QMessageBox.StandardButton.Yes
            ):

                self.set_controls_enabled(
                    True
                )

                return

        move = (
            self.operation_combo.currentText()
            == "Move"
        )

        self.start_transfer_with_files(
            files,
            move,
        )

    def scan_finished_for_transfer(
        self
    ):

        self.scan_worker = None

    # ==================================================================
    # START WORKER
    # ==================================================================

    def start_transfer_with_files(
        self,
        files,
        move=False,
        start_index=0,
    ):

        if not files:

            QMessageBox.warning(
                self,
                "No Files",
                "There are no files to transfer.",
            )

            self.set_controls_enabled(
                True
            )

            return

        self.current_operation = (
            "move"
            if move
            else "copy"
        )

        self.progress.setValue(
            0
        )

        self.status_label.setText(
            "Starting transfer..."
        )

        self.pause_button.setEnabled(
            True
        )

        self.resume_button.setEnabled(
            False
        )

        self.copy_missing_button.setEnabled(
            False
        )

        skip = (
            self.skip_existing.isChecked()
        )

        ignore_space = (
            self.ignore_space.isChecked()
        )

        self.worker = TransferWorker(
            self.source,
            self.destination,
            files,
            move,
            skip,
            ignore_space,
            start_index,
        )

        self.worker.progress.connect(
            self.progress.setValue
        )

        self.worker.status.connect(
            self.status_label.setText
        )

        self.worker.statistics.connect(
            self.status_label.setText
        )

        self.worker.log.connect(
            self.append_log
        )

        self.worker.finished_result.connect(
            self.transfer_finished
        )

        self.worker.error.connect(
            self.operation_error
        )

        self.worker.finished.connect(
            self.worker_finished
        )

        self.set_controls_enabled(
            False
        )

        self.pause_button.setEnabled(
            True
        )

        self.append_log(
            (
                "=== "
                + (
                    "MOVE"
                    if move
                    else "COPY"
                )
                + " started ==="
            )
        )

        self.worker.start()

    # ==================================================================
    # PAUSE
    # ==================================================================

    def pause_operation(self):

        if not self.worker:
            return

        if not self.worker.isRunning():
            return

        self.pause_button.setEnabled(
            False
        )

        self.status_label.setText(
            "Pause requested. Finishing current file..."
        )

        self.append_log(
            "=== Pause requested ==="
        )

        self.worker.request_pause()

    # ==================================================================
    # RESUME
    # ==================================================================

    def check_resume_state(self):

        state = load_state()

        if not state:
            return

        if not state.get(
            "Paused",
            False,
        ):

            return

        self.resume_button.setEnabled(
            True
        )

        self.append_log(
            (
                "Paused job found: "
                f"{state.get('Source', '')}"
                " → "
                f"{state.get('Destination', '')}"
            )
        )

    def resume_operation(self):

        state = load_state()

        if not state:

            QMessageBox.warning(
                self,
                "No Resume State",
                "No paused operation was found.",
            )

            self.resume_button.setEnabled(
                False
            )

            return

        try:

            self.source = Path(
                state["Source"]
            )

            self.destination = Path(
                state["Destination"]
            )

            start_index = int(
                state.get(
                    "Index",
                    0,
                )
            )

            move = bool(
                state.get(
                    "Move",
                    False,
                )
            )

            skip = bool(
                state.get(
                    "SkipExisting",
                    False,
                )
            )

            ignore_space = bool(
                state.get(
                    "IgnoreSpace",
                    False,
                )
            )

            if not self.source.exists():

                QMessageBox.critical(
                    self,
                    "Resume Error",
                    "The original source no longer exists.",
                )

                return

            if not self.destination.exists():

                self.destination.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            self.source_edit.setText(
                str(self.source)
            )

            self.destination_edit.setText(
                str(self.destination)
            )

            self.skip_existing.setChecked(
                skip
            )

            self.ignore_space.setChecked(
                ignore_space
            )

            # Rebuild the file list so the resume index
            # corresponds to the current filesystem.
            self.status_label.setText(
                "Rebuilding file list for resume..."
            )

            self.set_controls_enabled(
                False
            )

            self.scan_worker = FileScanner(
                self.source
            )

            self.scan_worker.progress.connect(
                self.progress.setValue
            )

            self.scan_worker.status.connect(
                self.status_label.setText
            )

            def resume_files(
                files,
                total,
                inaccessible,
            ):

                self.file_list = files

                if start_index >= len(files):

                    QMessageBox.information(
                        self,
                        "Resume",
                        "The previous operation was already complete.",
                    )

                    delete_state()

                    self.set_controls_enabled(
                        True
                    )

                    return

                self.start_transfer_with_files(
                    files,
                    move=move,
                    start_index=start_index,
                )

            self.scan_worker.result.connect(
                resume_files
            )

            self.scan_worker.error.connect(
                self.operation_error
            )

            self.scan_worker.finished.connect(
                self.scan_finished_for_transfer
            )

            self.scan_worker.start()

        except Exception as e:

            QMessageBox.critical(
                self,
                "Resume Error",
                str(e),
            )

    # ==================================================================
    # WORKER COMPLETION
    # ==================================================================

    def transfer_finished(
        self,
        result,
    ):

        if result.get(
            "paused",
            False,
        ):

            self.status_label.setText(
                "Operation paused."
            )

            self.append_log(
                "=== Operation PAUSED ==="
            )

            self.resume_button.setEnabled(
                True
            )

            self.pause_button.setEnabled(
                False
            )

            return

        copied = result.get(
            "copied",
            0,
        )

        skipped = result.get(
            "skipped",
            0,
        )

        failed = result.get(
            "failed",
            [],
        )

        bytes_copied = result.get(
            "bytes",
            0,
        )

        success = result.get(
            "success",
            False,
        )

        # --------------------------------------------------------------
        # Failure during pre-flight
        # --------------------------------------------------------------

        if "message" in result:

            QMessageBox.critical(
                self,
                "Transfer Failed",
                result["message"],
            )

            self.append_log(
                result["message"]
            )

            return

        # --------------------------------------------------------------
        # Failed files
        # --------------------------------------------------------------

        if failed:

            self.append_log(
                (
                    f"=== Operation completed with "
                    f"{len(failed):,} error(s) ==="
                )
            )

            self.log_view.append(
                "\n--- Failed Files ---"
            )

            for filename, error in failed:

                self.log_view.append(
                    f"{filename}: {error}"
                )

        # --------------------------------------------------------------
        # Move deletion
        # --------------------------------------------------------------

        if (
            self.current_operation
            == "move"
            and not failed
        ):

            self.status_label.setText(
                "Transfer successful. Removing source files..."
            )

            self.remove_source_after_move()

        # --------------------------------------------------------------
        # Normal completion
        # --------------------------------------------------------------

        else:

            self.finish_transfer_ui(
                copied,
                skipped,
                failed,
                bytes_copied,
                success,
            )

    def remove_source_after_move(self):

        """
        Remove source files only after the entire move operation
        completed without errors.

        The destination files have already been verified.
        """

        deleted = 0

        delete_errors = []

        for src, size in self.file_list:

            try:

                if src.exists():

                    src.unlink()

                    deleted += 1

            except Exception as e:

                delete_errors.append(
                    (
                        str(src),
                        str(e),
                    )
                )

        # Remove empty directories.
        try:

            directories = []

            for current_root, dirs, files in os.walk(
                self.source,
                topdown=False,
            ):

                for dirname in dirs:

                    directory = (
                        Path(current_root)
                        / dirname
                    )

                    directories.append(
                        directory
                    )

            for directory in directories:

                try:

                    directory.rmdir()

                except OSError:

                    pass

        except Exception as e:

            delete_errors.append(
                (
                    str(self.source),
                    str(e),
                )
            )

        if delete_errors:

            self.append_log(
                "PARTIAL - move completed "
                "but source cleanup had errors."
            )

            self.finish_transfer_ui(
                0,
                0,
                delete_errors,
                0,
                False,
            )

        else:

            self.append_log(
                (
                    f"SUCCESS - MOVE completed. "
                    f"Deleted {deleted:,} source files."
                )
            )

            self.finish_transfer_ui(
                0,
                0,
                [],
                0,
                True,
            )

    def finish_transfer_ui(
        self,
        copied,
        skipped,
        failed,
        bytes_copied,
        success,
    ):

        self.progress.setValue(
            100
        )

        self.update_folder_statistics()

        if failed:

            self.status_label.setText(
                (
                    "Completed with errors: "
                    f"{len(failed):,} failed."
                )
            )

            QMessageBox.warning(
                self,
                "Completed With Errors",
                (
                    f"Copied: {copied:,}\n"
                    f"Skipped: {skipped:,}\n"
                    f"Failed: {len(failed):,}\n"
                    f"Transferred: "
                    f"{human_bytes(bytes_copied)}"
                ),
            )

        else:

            self.status_label.setText(
                (
                    "Operation completed successfully. "
                    f"Copied: {copied:,}, "
                    f"Skipped: {skipped:,}"
                )
            )

            QMessageBox.information(
                self,
                "Operation Complete",
                (
                    f"Copied: {copied:,}\n"
                    f"Skipped: {skipped:,}\n"
                    f"Transferred: "
                    f"{human_bytes(bytes_copied)}"
                ),
            )

        if success:

            delete_state()

        self.current_operation = None

    # ==================================================================
    # GENERIC ERRORS
    # ==================================================================

    def operation_error(
        self,
        message,
    ):

        self.append_log(
            f"ERROR: {message}"
        )

        self.status_label.setText(
            "Operation failed."
        )

        QMessageBox.critical(
            self,
            "Operation Error",
            message,
        )

    # ==================================================================
    # WORKER FINISHED
    # ==================================================================

    def worker_finished(self):

        self.worker = None

        self.set_controls_enabled(
            True
        )

        self.pause_button.setEnabled(
            False
        )

        # Keep resume available if a paused state exists.
        state = load_state()

        self.resume_button.setEnabled(
            bool(
                state
                and state.get(
                    "Paused",
                    False,
                )
            )
        )

        self.update_folder_statistics()

    # ==================================================================
    # CLOSE EVENT
    # ==================================================================

    def closeEvent(
        self,
        event,
    ):

        if (
            self.worker
            and self.worker.isRunning()
        ):

            answer = QMessageBox.question(
                self,
                "Operation Running",
                (
                    "A file operation is still running.\n\n"
                    "Closing now will NOT safely pause it.\n\n"
                    "Are you sure you want to exit?"
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
            )

            if (
                answer
                != QMessageBox.StandardButton.Yes
            ):

                event.ignore()

                return

        event.accept()


# ======================================================================
# APPLICATION
# ======================================================================

def main():

    app = QApplication(
        sys.argv
    )

    app.setApplicationName(
        "Windows File Copy Move Assistant"
    )

    window = FileManagerGUI()

    window.show()

    sys.exit(
        app.exec()
    )


if __name__ == "__main__":

    main()
