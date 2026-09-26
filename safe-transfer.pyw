"""
Safe Transfer - Windows File Copy / Move Assistant

Features:
    - Copy or move files
    - Creates the complete destination folder structure first
    - Scans files and builds a persistent transfer manifest
    - Transfer order:
        * Largest -> Smallest
        * Smallest -> Largest
    - Pause / resume
    - Skip existing files
    - Optional verification
    - Missing-file scan
    - Windows "Size on Disk" statistics
    - Destination volume statistics
    - Explicit "Scan Source & Destination Size" button
    - Detailed logging
    - JSON pause/resume state
    - PyQt6 GUI

Python:
    3.10+

Dependencies:
    PyQt6
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import time
import traceback

from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QObject,
    QThread,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)

from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QPlainTextEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)


# ============================================================
# Configuration
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

LOG_FILE = Path.home() / "copy_move_history.txt"
STATE_FILE = SCRIPT_DIR / ".pauseResumeState.json"

TRANSFER_MANIFEST = SCRIPT_DIR / "transfer_file_list.txt"

BUFFER_SIZE = 1024 * 1024

STATS_DEBOUNCE_MS = 500

FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

INVALID_FILE_SIZE = 0xFFFFFFFF

ERROR_SUCCESS = 0


# ============================================================
# Windows API
# ============================================================

if os.name == "nt":

    kernel32 = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    )

    kernel32.GetCompressedFileSizeW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]

    kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD

    kernel32.GetDiskFreeSpaceExW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    ]

    kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL


# ============================================================
# Formatting
# ============================================================

def human_bytes(value: int | float) -> str:
    """
    Format bytes using IEC units.
    """

    value = float(value)

    units = (
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
        "PiB",
        "EiB",
    )

    index = 0

    while abs(value) >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1

    if index == 0:
        return f"{int(value):,} {units[index]}"

    return f"{value:,.2f} {units[index]}"


def format_seconds(seconds: float) -> str:
    if seconds < 0:
        seconds = 0

    seconds = int(seconds)

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m {seconds}s"

    if minutes:
        return f"{minutes}m {seconds}s"

    return f"{seconds}s"


# ============================================================
# Logging
# ============================================================

def write_log(message: str) -> None:

    timestamp = time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    line = f"[{timestamp}] {message}"

    try:
        with LOG_FILE.open(
            "a",
            encoding="utf-8",
        ) as handle:

            handle.write(line + "\n")

    except Exception:
        pass


# ============================================================
# Windows Size on Disk
# ============================================================

def get_file_size_on_disk(path: Path) -> int:
    """
    Get the Windows allocated size of a file.

    Uses GetCompressedFileSizeW(), which handles sparse and
    compressed files better than simply using st_size.

    Falls back to st_blocks * 512 when available.
    """

    if os.name == "nt":

        high = wintypes.DWORD(0)

        ctypes.set_last_error(ERROR_SUCCESS)

        low = kernel32.GetCompressedFileSizeW(
            str(path),
            ctypes.byref(high),
        )

        if low != INVALID_FILE_SIZE:
            return (
                (int(high.value) << 32)
                | int(low)
            )

        error = ctypes.get_last_error()

        if error == ERROR_SUCCESS:
            return int(low)

    try:

        stat = path.stat()

        blocks = getattr(
            stat,
            "st_blocks",
            0,
        )

        if blocks:
            return int(blocks) * 512

        return int(stat.st_size)

    except Exception:

        return 0


# ============================================================
# Windows Volume Statistics
# ============================================================

def get_volume_statistics(
    path: Path,
) -> tuple[int, int, int]:
    """
    Returns:

        available_to_caller
        total_capacity
        total_free
    """

    if os.name != "nt":

        usage = shutil.disk_usage(path)

        return (
            usage.free,
            usage.total,
            usage.free,
        )

    available = ctypes.c_ulonglong(0)
    capacity = ctypes.c_ulonglong(0)
    free = ctypes.c_ulonglong(0)

    success = kernel32.GetDiskFreeSpaceExW(
        str(path),
        ctypes.byref(available),
        ctypes.byref(capacity),
        ctypes.byref(free),
    )

    if not success:

        error = ctypes.get_last_error()

        raise OSError(
            error,
            f"GetDiskFreeSpaceExW failed for {path}"
        )

    return (
        int(available.value),
        int(capacity.value),
        int(free.value),
    )


# ============================================================
# File Information
# ============================================================

@dataclass
class TransferFile:
    source: str
    relative_path: str
    size: int


# ============================================================
# Folder Statistics
# ============================================================

@dataclass
class FolderStatistics:

    logical_size: int = 0

    allocated_size: int = 0

    file_count: int = 0

    directory_count: int = 0

    scan_errors: int = 0

    skipped_reparse_points: int = 0

    largest_file_size: int = 0

    largest_file_path: str = ""

    allocation_difference: int = 0


# ============================================================
# Reparse Point Detection
# ============================================================

def is_reparse_point(path: Path) -> bool:

    try:

        attributes = ctypes.windll.kernel32.GetFileAttributesW(
            str(path)
        )

        if attributes == 0xFFFFFFFF:
            return False

        return bool(
            attributes
            & FILE_ATTRIBUTE_REPARSE_POINT
        )

    except Exception:

        try:
            return path.is_symlink()

        except Exception:
            return False


# ============================================================
# Folder Statistics Scanner
# ============================================================

def scan_folder_statistics(
    root: Path,
) -> FolderStatistics:

    stats = FolderStatistics()

    if not root.exists():
        return stats

    stack = [root]

    while stack:

        current = stack.pop()

        try:

            with os.scandir(current) as entries:

                for entry in entries:

                    try:

                        entry_path = Path(entry.path)

                        if entry.is_dir(
                            follow_symlinks=False
                        ):

                            if is_reparse_point(
                                entry_path
                            ):

                                stats.skipped_reparse_points += 1

                                continue

                            stats.directory_count += 1

                            stack.append(
                                entry_path
                            )

                            continue

                        if not entry.is_file(
                            follow_symlinks=False
                        ):
                            continue

                        if is_reparse_point(
                            entry_path
                        ):

                            stats.skipped_reparse_points += 1

                            continue

                        file_stat = entry.stat(
                            follow_symlinks=False
                        )

                        logical = int(
                            file_stat.st_size
                        )

                        allocated = (
                            get_file_size_on_disk(
                                entry_path
                            )
                        )

                        stats.logical_size += logical

                        stats.allocated_size += allocated

                        stats.file_count += 1

                        if logical > stats.largest_file_size:

                            stats.largest_file_size = logical

                            stats.largest_file_path = (
                                str(entry_path)
                            )

                    except OSError:

                        stats.scan_errors += 1

        except OSError:

            stats.scan_errors += 1

    stats.allocation_difference = (
        stats.allocated_size
        - stats.logical_size
    )

    return stats


# ============================================================
# Transfer File Scanner
# ============================================================

def scan_transfer_files(
    source: Path,
) -> tuple[list[TransferFile], int]:

    files: list[TransferFile] = []

    errors = 0

    stack = [source]

    while stack:

        current = stack.pop()

        try:

            with os.scandir(current) as entries:

                for entry in entries:

                    try:

                        path = Path(entry.path)

                        if entry.is_dir(
                            follow_symlinks=False
                        ):

                            if is_reparse_point(path):
                                continue

                            stack.append(path)

                            continue

                        if not entry.is_file(
                            follow_symlinks=False
                        ):
                            continue

                        if is_reparse_point(path):
                            continue

                        size = int(
                            entry.stat(
                                follow_symlinks=False
                            ).st_size
                        )

                        relative = os.path.relpath(
                            path,
                            source,
                        )

                        files.append(
                            TransferFile(
                                source=str(path),
                                relative_path=relative,
                                size=size,
                            )
                        )

                    except OSError:

                        errors += 1

        except OSError:

            errors += 1

    return files, errors


# ============================================================
# Manifest
# ============================================================

def save_manifest(
    files: list[TransferFile],
    order_name: str,
) -> None:

    with TRANSFER_MANIFEST.open(
        "w",
        encoding="utf-8",
    ) as handle:

        handle.write(
            "# Safe Transfer file manifest\n"
        )

        handle.write(
            f"# Created: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

        handle.write(
            f"# Order: {order_name}\n"
        )

        handle.write(
            f"# Files: {len(files):,}\n"
        )

        handle.write(
            "# Format: SIZE_BYTES<TAB>RELATIVE_PATH\n"
        )

        handle.write("\n")

        for item in files:

            handle.write(
                f"{item.size}\t{item.relative_path}\n"
            )


# ============================================================
# Pause / Resume State
# ============================================================

def save_pause_state(
    source: str,
    destination: str,
    current_index: int,
    files: list[TransferFile],
) -> None:

    state = {
        "source": source,
        "destination": destination,
        "current_index": current_index,
        "files": [
            {
                "source": item.source,
                "relative_path": item.relative_path,
                "size": item.size,
            }
            for item in files
        ],
    }

    try:

        with STATE_FILE.open(
            "w",
            encoding="utf-8",
        ) as handle:

            json.dump(
                state,
                handle,
                indent=2,
            )

    except Exception as exc:

        write_log(
            f"Failed saving pause state: {exc}"
        )


def load_pause_state() -> Optional[dict]:

    if not STATE_FILE.exists():
        return None

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as handle:

            return json.load(handle)

    except Exception as exc:

        write_log(
            f"Failed loading pause state: {exc}"
        )

        return None


def clear_pause_state() -> None:

    try:

        if STATE_FILE.exists():
            STATE_FILE.unlink()

    except Exception as exc:

        write_log(
            f"Failed clearing pause state: {exc}"
        )


# ============================================================
# Statistics Worker
# ============================================================

class FolderStatsWorker(QObject):

    finished = pyqtSignal(
        object,
        object,
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

    @pyqtSlot()
    def run(self):

        try:

            write_log(
                f"Scanning source statistics: {self.source}"
            )

            source_stats = scan_folder_statistics(
                self.source
            )

            write_log(
                "FOLDER STATISTICS: "
                f"Logical={source_stats.logical_size:,} "
                f"OnDisk={source_stats.allocated_size:,} "
                f"Files={source_stats.file_count:,} "
                f"Directories={source_stats.directory_count:,} "
                f"Errors={source_stats.scan_errors:,} "
                f"ReparseSkipped={source_stats.skipped_reparse_points:,}"
            )

            (
                available,
                capacity,
                free,
            ) = get_volume_statistics(
                self.destination
            )

            destination_stats = {
                "available": available,
                "capacity": capacity,
                "free": free,
            }

            write_log(
                "DESTINATION VOLUME: "
                f"Capacity={capacity:,} "
                f"Free={free:,} "
                f"Available={available:,}"
            )

            self.finished.emit(
                source_stats,
                destination_stats,
            )

        except Exception as exc:

            write_log(
                f"Statistics scan failed: {exc}"
            )

            self.error.emit(
                f"{type(exc).__name__}: {exc}"
            )


# ============================================================
# Transfer Worker
# ============================================================

class TransferWorker(QObject):

    progress = pyqtSignal(
        int,
        int,
        int,
        str,
    )

    status = pyqtSignal(str)

    finished = pyqtSignal()

    paused = pyqtSignal()

    error = pyqtSignal(str)

    def __init__(
        self,
        source: Path,
        destination: Path,
        files: list[TransferFile],
        move_files: bool,
        skip_existing: bool,
        verify: bool,
        start_index: int = 0,
    ):

        super().__init__()

        self.source = source

        self.destination = destination

        self.files = files

        self.move_files = move_files

        self.skip_existing = skip_existing

        self.verify = verify

        self.start_index = start_index

        self.pause_requested = False

        self.cancel_requested = False

    def request_pause(self):

        self.pause_requested = True

    def request_cancel(self):

        self.cancel_requested = True

    def check_pause(
        self,
        index: int,
    ):

        if self.cancel_requested:

            raise RuntimeError(
                "Transfer cancelled."
            )

        if self.pause_requested:

            save_pause_state(
                str(self.source),
                str(self.destination),
                index,
                self.files,
            )

            raise RuntimeError(
                "Transfer paused."
            )

    def create_folder_structure(self):

        self.status.emit(
            "Creating destination folder structure..."
        )

        write_log(
            "Creating destination folder structure."
        )

        directories: set[Path] = set()

        for item in self.files:

            relative = Path(
                item.relative_path
            )

            parent = relative.parent

            if str(parent) not in ("", "."):

                directories.add(
                    self.destination / parent
                )

        directories = sorted(
            directories,
            key=lambda p: len(p.parts),
        )

        for directory in directories:

            if self.cancel_requested:

                raise RuntimeError(
                    "Transfer cancelled."
                )

            directory.mkdir(
                parents=True,
                exist_ok=True,
            )

    def copy_file(
        self,
        source: Path,
        destination: Path,
    ):

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with source.open(
            "rb"
        ) as src:

            with destination.open(
                "wb"
            ) as dst:

                while True:

                    if self.cancel_requested:

                        raise RuntimeError(
                            "Transfer cancelled."
                        )

                    if self.pause_requested:

                        raise RuntimeError(
                            "Transfer paused."
                        )

                    data = src.read(
                        BUFFER_SIZE
                    )

                    if not data:
                        break

                    dst.write(data)

        try:

            shutil.copystat(
                source,
                destination,
            )

        except Exception:

            pass

    def verify_file(
        self,
        source: Path,
        destination: Path,
    ) -> bool:

        try:

            source_size = source.stat().st_size

            destination_size = (
                destination.stat().st_size
            )

            return (
                source_size
                == destination_size
            )

        except Exception:

            return False

    def run(self):

        try:

            if not self.files:

                self.finished.emit()

                return

            # ------------------------------------------------
            # STEP 1:
            # Create ALL folders first.
            # ------------------------------------------------

            self.create_folder_structure()

            total_bytes = sum(
                item.size
                for item in self.files
            )

            transferred_bytes = sum(
                item.size
                for item in self.files[
                    :self.start_index
                ]
            )

            total_files = len(self.files)

            # ------------------------------------------------
            # STEP 2:
            # Transfer files in selected order.
            # ------------------------------------------------

            for index in range(
                self.start_index,
                total_files,
            ):

                self.check_pause(index)

                item = self.files[index]

                source = Path(
                    item.source
                )

                destination = (
                    self.destination
                    / Path(item.relative_path)
                )

                self.status.emit(
                    f"{index + 1:,} / "
                    f"{total_files:,}: "
                    f"{item.relative_path}"
                )

                if not source.exists():

                    write_log(
                        f"Source missing: {source}"
                    )

                    transferred_bytes += item.size

                    self.progress.emit(
                        transferred_bytes,
                        total_bytes,
                        index + 1,
                        item.relative_path,
                    )

                    continue

                # --------------------------------------------
                # Skip existing files
                # --------------------------------------------

                if (
                    self.skip_existing
                    and destination.exists()
                ):

                    try:

                        destination_size = (
                            destination.stat().st_size
                        )

                    except OSError:

                        destination_size = -1

                    if (
                        destination_size
                        == item.size
                    ):

                        write_log(
                            f"Skipped existing: "
                            f"{destination}"
                        )

                        transferred_bytes += (
                            item.size
                        )

                        self.progress.emit(
                            transferred_bytes,
                            total_bytes,
                            index + 1,
                            item.relative_path,
                        )

                        continue

                # --------------------------------------------
                # Transfer
                # --------------------------------------------

                write_log(
                    f"{'MOVE' if self.move_files else 'COPY'}: "
                    f"{source} -> {destination} "
                    f"({item.size:,} bytes)"
                )

                self.copy_file(
                    source,
                    destination,
                )

                # --------------------------------------------
                # Verification
                # --------------------------------------------

                if self.verify:

                    if not self.verify_file(
                        source,
                        destination,
                    ):

                        raise RuntimeError(
                            "Verification failed for "
                            f"{item.relative_path}"
                        )

                # --------------------------------------------
                # Move removes source AFTER successful copy
                # --------------------------------------------

                if self.move_files:

                    try:

                        source.unlink()

                        write_log(
                            f"Deleted source after move: "
                            f"{source}"
                        )

                    except Exception as exc:

                        raise RuntimeError(
                            "Copied file successfully, "
                            "but could not remove source: "
                            f"{source} ({exc})"
                        )

                transferred_bytes += item.size

                elapsed_start = time.monotonic()

                percent = 0

                if total_bytes > 0:

                    percent = int(
                        (
                            transferred_bytes
                            / total_bytes
                        )
                        * 100
                    )

                self.progress.emit(
                    transferred_bytes,
                    total_bytes,
                    index + 1,
                    item.relative_path,
                )

            # ------------------------------------------------
            # Done
            # ------------------------------------------------

            clear_pause_state()

            self.status.emit(
                "Transfer complete."
            )

            write_log(
                "Transfer completed successfully."
            )

            self.finished.emit()

        except Exception as exc:

            error_text = (
                f"{type(exc).__name__}: {exc}"
            )

            write_log(
                f"Transfer stopped: {error_text}"
            )

            if "Transfer paused." in str(exc):

                self.paused.emit()

            else:

                self.error.emit(
                    error_text
                )


# ============================================================
# Main Window
# ============================================================

class MainWindow(QMainWindow):

    def __init__(self):

        super().__init__()

        self.setWindowTitle(
            "Safe Transfer - Windows File Copy / Move Assistant"
        )

        self.resize(
            1050,
            850,
        )

        self.stats_thread: Optional[QThread] = None

        self.stats_worker: Optional[
            FolderStatsWorker
        ] = None

        self.transfer_thread: Optional[
            QThread
        ] = None

        self.transfer_worker: Optional[
            TransferWorker
        ] = None

        self.current_files: list[
            TransferFile
        ] = []

        self.transfer_start_time = 0.0

        self.build_ui()

    # ========================================================
    # UI
    # ========================================================

    def build_ui(self):

        central = QWidget()

        self.setCentralWidget(
            central
        )

        layout = QVBoxLayout(
            central
        )

        # ----------------------------------------------------
        # Source / Destination
        # ----------------------------------------------------

        paths_group = QGroupBox(
            "Source / Destination"
        )

        paths_layout = QGridLayout(
            paths_group
        )

        self.source_edit = QLineEdit()

        self.destination_edit = QLineEdit()

        source_button = QPushButton(
            "Browse..."
        )

        destination_button = QPushButton(
            "Browse..."
        )

        source_button.clicked.connect(
            self.choose_source
        )

        destination_button.clicked.connect(
            self.choose_destination
        )

        paths_layout.addWidget(
            QLabel("Source:"),
            0,
            0,
        )

        paths_layout.addWidget(
            self.source_edit,
            0,
            1,
        )

        paths_layout.addWidget(
            source_button,
            0,
            2,
        )

        paths_layout.addWidget(
            QLabel("Destination:"),
            1,
            0,
        )

        paths_layout.addWidget(
            self.destination_edit,
            1,
            1,
        )

        paths_layout.addWidget(
            destination_button,
            1,
            2,
        )

        layout.addWidget(
            paths_group
        )

        # ----------------------------------------------------
        # Transfer Mode
        # ----------------------------------------------------

        options_group = QGroupBox(
            "Transfer Options"
        )

        options_layout = QGridLayout(
            options_group
        )

        self.copy_radio = QRadioButton(
            "Copy"
        )

        self.move_radio = QRadioButton(
            "Move"
        )

        self.copy_radio.setChecked(
            True
        )

        self.skip_existing_checkbox = (
            QCheckBox(
                "Skip existing files with matching size"
            )
        )

        self.verify_checkbox = (
            QCheckBox(
                "Verify destination file size"
            )
        )

        self.order_combo = QComboBox()

        self.order_combo.addItem(
            "Largest → Smallest",
            "largest",
        )

        self.order_combo.addItem(
            "Smallest → Largest",
            "smallest",
        )

        options_layout.addWidget(
            self.copy_radio,
            0,
            0,
        )

        options_layout.addWidget(
            self.move_radio,
            0,
            1,
        )

        options_layout.addWidget(
            QLabel("File transfer order:"),
            1,
            0,
        )

        options_layout.addWidget(
            self.order_combo,
            1,
            1,
        )

        options_layout.addWidget(
            self.skip_existing_checkbox,
            2,
            0,
            1,
            2,
        )

        options_layout.addWidget(
            self.verify_checkbox,
            3,
            0,
            1,
            2,
        )

        layout.addWidget(
            options_group
        )

        # ----------------------------------------------------
        # Scan
        # ----------------------------------------------------

        scan_group = QGroupBox(
            "Folder Statistics"
        )

        scan_layout = QVBoxLayout(
            scan_group
        )

        self.scan_button = QPushButton(
            "Scan Source & Destination Size"
        )

        self.scan_button.clicked.connect(
            self.scan_statistics
        )

        self.stats_label = QLabel(
            "Statistics have not been scanned."
        )

        self.stats_label.setWordWrap(
            True
        )

        scan_layout.addWidget(
            self.scan_button
        )

        scan_layout.addWidget(
            self.stats_label
        )

        layout.addWidget(
            scan_group
        )

        # ----------------------------------------------------
        # Transfer Buttons
        # ----------------------------------------------------

        buttons_layout = QHBoxLayout()

        self.start_button = QPushButton(
            "Start Transfer"
        )

        self.pause_button = QPushButton(
            "Pause"
        )

        self.resume_button = QPushButton(
            "Resume Saved Transfer"
        )

        self.cancel_button = QPushButton(
            "Cancel"
        )

        self.start_button.clicked.connect(
            self.start_transfer
        )

        self.pause_button.clicked.connect(
            self.pause_transfer
        )

        self.resume_button.clicked.connect(
            self.resume_transfer
        )

        self.cancel_button.clicked.connect(
            self.cancel_transfer
        )

        self.pause_button.setEnabled(
            False
        )

        self.cancel_button.setEnabled(
            False
        )

        buttons_layout.addWidget(
            self.start_button
        )

        buttons_layout.addWidget(
            self.pause_button
        )

        buttons_layout.addWidget(
            self.resume_button
        )

        buttons_layout.addWidget(
            self.cancel_button
        )

        layout.addLayout(
            buttons_layout
        )

        # ----------------------------------------------------
        # Progress
        # ----------------------------------------------------

        self.progress_bar = QProgressBar()

        self.progress_bar.setRange(
            0,
            100,
        )

        self.progress_bar.setValue(
            0
        )

        layout.addWidget(
            self.progress_bar
        )

        self.progress_label = QLabel(
            "Ready."
        )

        layout.addWidget(
            self.progress_label
        )

        # ----------------------------------------------------
        # Log
        # ----------------------------------------------------

        log_group = QGroupBox(
            "Activity Log"
        )

        log_layout = QVBoxLayout(
            log_group
        )

        self.log_output = QPlainTextEdit()

        self.log_output.setReadOnly(
            True
        )

        log_layout.addWidget(
            self.log_output
        )

        layout.addWidget(
            log_group,
            1,
        )

        self.append_log(
            "Safe Transfer started."
        )

        self.append_log(
            f"Log file: {LOG_FILE}"
        )

        self.append_log(
            f"Transfer manifest: {TRANSFER_MANIFEST}"
        )

    # ========================================================
    # Logging
    # ========================================================

    def append_log(
        self,
        message: str,
    ):

        timestamp = time.strftime(
            "%H:%M:%S"
        )

        self.log_output.appendPlainText(
            f"[{timestamp}] {message}"
        )

    # ========================================================
    # Browse
    # ========================================================

    def choose_source(self):

        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Source Folder",
        )

        if folder:

            self.source_edit.setText(
                folder
            )

            self.append_log(
                f"Source selected: {folder}"
            )

            # IMPORTANT:
            # Statistics are NOT automatically scanned.

    def choose_destination(self):

        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Destination Folder",
        )

        if folder:

            self.destination_edit.setText(
                folder
            )

            self.append_log(
                f"Destination selected: {folder}"
            )

            # IMPORTANT:
            # Statistics are NOT automatically scanned.

    # ========================================================
    # Validation
    # ========================================================

    def get_paths(
        self,
    ) -> Optional[
        tuple[Path, Path]
    ]:

        source_text = (
            self.source_edit.text().strip()
        )

        destination_text = (
            self.destination_edit.text().strip()
        )

        if not source_text:

            QMessageBox.warning(
                self,
                "Missing Source",
                "Please select a source folder.",
            )

            return None

        if not destination_text:

            QMessageBox.warning(
                self,
                "Missing Destination",
                "Please select a destination folder.",
            )

            return None

        source = Path(
            source_text
        )

        destination = Path(
            destination_text
        )

        if not source.exists():
            QMessageBox.warning(
                self,
                "Invalid Source",
                "The source folder does not exist.",
            )

            return None

        if not source.is_dir():
            QMessageBox.warning(
                self,
                "Invalid Source",
                "The source path is not a folder.",
            )

            return None

        try:

            source_resolved = (
                source.resolve()
            )

            destination_resolved = (
                destination.resolve()
            )

            if source_resolved == destination_resolved:

                QMessageBox.warning(
                    self,
                    "Invalid Paths",
                    "Source and destination cannot be the same folder.",
                )

                return None

        except Exception:
            pass

        return (
            source,
            destination,
        )

    # ========================================================
    # Statistics Scan
    # ========================================================

    def scan_statistics(self):

        paths = self.get_paths()

        if not paths:
            return

        source, destination = paths

        self.scan_button.setEnabled(
            False
        )

        self.stats_label.setText(
            "Scanning source folder and destination volume..."
        )

        self.append_log(
            "Starting manual statistics scan."
        )

        self.stats_thread = QThread()

        self.stats_worker = (
            FolderStatsWorker(
                source,
                destination,
            )
        )

        self.stats_worker.moveToThread(
            self.stats_thread
        )

        self.stats_thread.started.connect(
            self.stats_worker.run
        )

        self.stats_worker.finished.connect(
            self._on_stats_ready
        )

        self.stats_worker.error.connect(
            self._on_stats_error
        )

        self.stats_worker.finished.connect(
            self.stats_thread.quit
        )

        self.stats_worker.error.connect(
            self.stats_thread.quit
        )

        self.stats_thread.finished.connect(
            self._stats_thread_finished
        )

        self.stats_thread.start()

    @pyqtSlot(object, object)
    def _on_stats_ready(
        self,
        source_stats: FolderStatistics,
        destination_stats: dict,
    ):

        logical = (
            source_stats.logical_size
        )

        allocated = (
            source_stats.allocated_size
        )

        difference = (
            source_stats.allocation_difference
        )

        if difference >= 0:

            allocation_difference_text = (
                f"+{human_bytes(difference)}"
            )

        else:

            allocation_difference_text = (
                human_bytes(difference)
            )

        if source_stats.largest_file_path:

            largest_text = (
                f"{human_bytes(source_stats.largest_file_size)}\n"
                f"{source_stats.largest_file_path}"
            )

        else:

            largest_text = "None"

        error_text = ""

        if source_stats.scan_errors:

            error_text = (
                "\n"
                f"Scan errors: "
                f"{source_stats.scan_errors:,}"
            )

        reparse_text = ""

        if source_stats.skipped_reparse_points:

            reparse_text = (
                "\n"
                f"Skipped reparse points: "
                f"{source_stats.skipped_reparse_points:,}"
            )

        used = (
            destination_stats["capacity"]
            - destination_stats["free"]
        )

        self.stats_label.setText(
            "SOURCE\n"
            f"Logical size: {human_bytes(logical)}\n"
            f"Size on disk: {human_bytes(allocated)}\n"
            f"Allocation difference: "
            f"{allocation_difference_text}\n"
            f"Files: {source_stats.file_count:,}\n"
            f"Directories: {source_stats.directory_count:,}\n"
            f"Largest file: {largest_text}"
            f"{error_text}"
            f"{reparse_text}"
            "\n\n"
            "DESTINATION VOLUME\n"
            f"Capacity: "
            f"{human_bytes(destination_stats['capacity'])}\n"
            f"Used: "
            f"{human_bytes(used)}\n"
            f"Free: "
            f"{human_bytes(destination_stats['free'])}\n"
            f"Available to this process: "
            f"{human_bytes(destination_stats['available'])}"
        )

        self.append_log(
            "Statistics scan complete."
        )

        self.append_log(
            f"Source logical size: "
            f"{logical:,} bytes"
        )

        self.append_log(
            f"Source size on disk: "
            f"{allocated:,} bytes"
        )

        self.append_log(
            f"Destination capacity: "
            f"{destination_stats['capacity']:,} bytes"
        )

        self.append_log(
            f"Destination free: "
            f"{destination_stats['free']:,} bytes"
        )

    @pyqtSlot(str)
    def _on_stats_error(
        self,
        error: str,
    ):

        self.stats_label.setText(
            f"Statistics scan failed:\n{error}"
        )

        self.append_log(
            f"Statistics error: {error}"
        )

        QMessageBox.warning(
            self,
            "Statistics Scan Failed",
            error,
        )

    def _stats_thread_finished(self):

        self.scan_button.setEnabled(
            True
        )

        self.stats_thread = None

        self.stats_worker = None

    # ========================================================
    # Build Transfer Manifest
    # ========================================================

    def build_transfer_manifest(
        self,
        source: Path,
        order: str,
    ) -> list[TransferFile]:

        self.append_log(
            "Scanning source files..."
        )

        files, errors = (
            scan_transfer_files(
                source
            )
        )

        self.append_log(
            f"Found {len(files):,} files."
        )

        if errors:

            self.append_log(
                f"File scan errors: {errors:,}"
            )

        if order == "largest":

            files.sort(
                key=lambda item: item.size,
                reverse=True,
            )

            order_name = (
                "Largest -> Smallest"
            )

        else:

            files.sort(
                key=lambda item: item.size
            )

            order_name = (
                "Smallest -> Largest"
            )

        save_manifest(
            files,
            order_name,
        )

        self.append_log(
            f"Transfer order: {order_name}"
        )

        self.append_log(
            f"Manifest saved: {TRANSFER_MANIFEST}"
        )

        return files

    # ========================================================
    # Start Transfer
    # ========================================================

    def start_transfer(self):

        paths = self.get_paths()

        if not paths:
            return

        source, destination = paths

        order = (
            self.order_combo.currentData()
        )

        move_files = (
            self.move_radio.isChecked()
        )

        skip_existing = (
            self.skip_existing_checkbox.isChecked()
        )

        verify = (
            self.verify_checkbox.isChecked()
        )

        if destination.exists():

            try:

                destination.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            except Exception as exc:

                QMessageBox.critical(
                    self,
                    "Destination Error",
                    str(exc),
                )

                return

        else:

            try:

                destination.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            except Exception as exc:

                QMessageBox.critical(
                    self,
                    "Destination Error",
                    str(exc),
                )

                return

        # ----------------------------------------------------
        # Scan files
        # ----------------------------------------------------

        try:

            files = (
                self.build_transfer_manifest(
                    source,
                    order,
                )
            )

        except Exception as exc:

            QMessageBox.critical(
                self,
                "File Scan Failed",
                str(exc),
            )

            self.append_log(
                f"File scan failed: {exc}"
            )

            return

        if not files:

            QMessageBox.information(
                self,
                "Nothing to Transfer",
                "No files were found in the source folder.",
            )

            return

        # ----------------------------------------------------
        # Destination free-space check
        # ----------------------------------------------------

        try:

            (
                available,
                capacity,
                free,
            ) = get_volume_statistics(
                destination
            )

            total_size = sum(
                item.size
                for item in files
            )

            self.append_log(
                f"Transfer logical size: "
                f"{total_size:,} bytes"
            )

            self.append_log(
                f"Destination available: "
                f"{available:,} bytes"
            )

            if available < total_size:

                result = QMessageBox.warning(
                    self,
                    "Insufficient Free Space",
                    (
                        "The destination does not appear "
                        "to have enough available space.\n\n"
                        f"Required: {human_bytes(total_size)}\n"
                        f"Available: {human_bytes(available)}\n\n"
                        "Continue anyway?"
                    ),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )

                if (
                    result
                    != QMessageBox.StandardButton.Yes
                ):

                    return

        except Exception as exc:

            self.append_log(
                f"Could not determine destination free space: {exc}"
            )

            result = QMessageBox.warning(
                self,
                "Free Space Check Failed",
                (
                    "Windows could not determine "
                    "destination free space.\n\n"
                    f"{exc}\n\n"
                    "Continue anyway?"
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if (
                result
                != QMessageBox.StandardButton.Yes
            ):

                return

        # ----------------------------------------------------
        # Confirmation
        # ----------------------------------------------------

        action = (
            "MOVE"
            if move_files
            else "COPY"
        )

        order_name = (
            "Largest -> Smallest"
            if order == "largest"
            else "Smallest -> Largest"
        )

        total_size = sum(
            item.size
            for item in files
        )

        message = (
            f"Operation: {action}\n\n"
            f"Source:\n{source}\n\n"
            f"Destination:\n{destination}\n\n"
            f"Files: {len(files):,}\n"
            f"Logical size: {human_bytes(total_size)}\n"
            f"Order: {order_name}\n\n"
            "The complete folder structure will be "
            "created before files are transferred."
        )

        if move_files:

            message += (
                "\n\n"
                "MOVE mode will delete each source file "
                "only after its destination copy succeeds."
            )

        result = QMessageBox.question(
            self,
            "Start Transfer",
            message,
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if (
            result
            != QMessageBox.StandardButton.Yes
        ):

            return

        self.current_files = files

        self.launch_transfer(
            source,
            destination,
            files,
            move_files,
            skip_existing,
            verify,
            0,
        )

    # ========================================================
    # Launch Transfer
    # ========================================================

    def launch_transfer(
        self,
        source: Path,
        destination: Path,
        files: list[TransferFile],
        move_files: bool,
        skip_existing: bool,
        verify: bool,
        start_index: int,
    ):

        self.transfer_thread = QThread()

        self.transfer_worker = (
            TransferWorker(
                source=source,
                destination=destination,
                files=files,
                move_files=move_files,
                skip_existing=skip_existing,
                verify=verify,
                start_index=start_index,
            )
        )

        self.transfer_worker.moveToThread(
            self.transfer_thread
        )

        self.transfer_thread.started.connect(
            self.transfer_worker.run
        )

        self.transfer_worker.progress.connect(
            self._on_transfer_progress
        )

        self.transfer_worker.status.connect(
            self._on_transfer_status
        )

        self.transfer_worker.finished.connect(
            self._on_transfer_finished
        )

        self.transfer_worker.paused.connect(
            self._on_transfer_paused
        )

        self.transfer_worker.error.connect(
            self._on_transfer_error
        )

        self.transfer_worker.finished.connect(
            self.transfer_thread.quit
        )

        self.transfer_worker.paused.connect(
            self.transfer_thread.quit
        )

        self.transfer_worker.error.connect(
            self.transfer_thread.quit
        )

        self.transfer_thread.finished.connect(
            self._transfer_thread_finished
        )

        self.set_transfer_running(
            True
        )

        self.transfer_start_time = (
            time.monotonic()
        )

        self.append_log(
            f"Starting transfer at file "
            f"{start_index + 1:,}."
        )

        self.transfer_thread.start()

    # ========================================================
    # Pause
    # ========================================================

    def pause_transfer(self):

        if self.transfer_worker:

            self.append_log(
                "Pause requested..."
            )

            self.transfer_worker.request_pause()

    # ========================================================
    # Resume
    # ========================================================

    def resume_transfer(self):

        state = load_pause_state()

        if not state:

            QMessageBox.information(
                self,
                "No Saved Transfer",
                "There is no saved paused transfer.",
            )

            return

        source = Path(
            state["source"]
        )

        destination = Path(
            state["destination"]
        )

        files = [
            TransferFile(
                source=item["source"],
                relative_path=item["relative_path"],
                size=int(item["size"]),
            )
            for item in state["files"]
        ]

        current_index = int(
            state.get(
                "current_index",
                0,
            )
        )

        if not source.exists():

            QMessageBox.warning(
                self,
                "Source Missing",
                f"Source folder no longer exists:\n{source}",
            )

            return

        try:

            destination.mkdir(
                parents=True,
                exist_ok=True,
            )

        except Exception as exc:

            QMessageBox.critical(
                self,
                "Destination Error",
                str(exc),
            )

            return

        self.source_edit.setText(
            str(source)
        )

        self.destination_edit.setText(
            str(destination)
        )

        self.current_files = files

        self.append_log(
            f"Resuming saved transfer at file "
            f"{current_index + 1:,}."
        )

        self.launch_transfer(
            source,
            destination,
            files,
            self.move_radio.isChecked(),
            self.skip_existing_checkbox.isChecked(),
            self.verify_checkbox.isChecked(),
            current_index,
        )

    # ========================================================
    # Cancel
    # ========================================================

    def cancel_transfer(self):

        if self.transfer_worker:

            self.append_log(
                "Cancel requested..."
            )

            self.transfer_worker.request_cancel()

    # ========================================================
    # Transfer Progress
    # ========================================================

    @pyqtSlot(
        int,
        int,
        int,
        str,
    )
    def _on_transfer_progress(
        self,
        transferred: int,
        total: int,
        file_number: int,
        filename: str,
    ):

        if total > 0:

            percent = int(
                (
                    transferred
                    / total
                )
                * 100
            )

        else:

            percent = 0

        self.progress_bar.setValue(
            max(
                0,
                min(
                    100,
                    percent,
                ),
            )
        )

        elapsed = (
            time.monotonic()
            - self.transfer_start_time
        )

        if elapsed > 0:

            speed = (
                transferred
                / elapsed
            )

        else:

            speed = 0

        self.progress_label.setText(
            f"{percent}% | "
            f"{human_bytes(transferred)} / "
            f"{human_bytes(total)} | "
            f"{human_bytes(speed)}/s | "
            f"File {file_number:,}"
        )

        self.statusBar().showMessage(
            filename
        )

    @pyqtSlot(str)
    def _on_transfer_status(
        self,
        message: str,
    ):

        self.append_log(
            message
        )

    # ========================================================
    # Transfer Finished
    # ========================================================

    def _on_transfer_finished(self):

        self.progress_bar.setValue(
            100
        )

        self.progress_label.setText(
            "Transfer complete."
        )

        self.append_log(
            "Transfer complete."
        )

        self.set_transfer_running(
            False
        )

        QMessageBox.information(
            self,
            "Transfer Complete",
            "The transfer completed successfully.",
        )

    # ========================================================
    # Transfer Paused
    # ========================================================

    def _on_transfer_paused(self):

        self.progress_label.setText(
            "Transfer paused."
        )

        self.append_log(
            "Transfer paused. Resume state saved."
        )

        self.set_transfer_running(
            False
        )

        QMessageBox.information(
            self,
            "Transfer Paused",
            (
                "The transfer has been paused.\n\n"
                "Your progress was saved and can be "
                "resumed with 'Resume Saved Transfer'."
            ),
        )

    # ========================================================
    # Transfer Error
    # ========================================================

    def _on_transfer_error(
        self,
        error: str,
    ):

        self.progress_label.setText(
            "Transfer stopped."
        )

        self.append_log(
            f"Transfer error: {error}"
        )

        self.set_transfer_running(
            False
        )

        QMessageBox.critical(
            self,
            "Transfer Error",
            error,
        )

    # ========================================================
    # Thread Cleanup
    # ========================================================

    def _transfer_thread_finished(self):

        self.transfer_thread = None

        self.transfer_worker = None

    # ========================================================
    # UI Transfer State
    # ========================================================

    def set_transfer_running(
        self,
        running: bool,
    ):

        self.start_button.setEnabled(
            not running
        )

        self.resume_button.setEnabled(
            not running
        )

        self.pause_button.setEnabled(
            running
        )

        self.cancel_button.setEnabled(
            running
        )

        self.scan_button.setEnabled(
            not running
        )

        self.source_edit.setEnabled(
            not running
        )

        self.destination_edit.setEnabled(
            not running
        )

        self.copy_radio.setEnabled(
            not running
        )

        self.move_radio.setEnabled(
            not running
        )

        self.order_combo.setEnabled(
            not running
        )

        self.skip_existing_checkbox.setEnabled(
            not running
        )

        self.verify_checkbox.setEnabled(
            not running
        )

    # ========================================================
    # Close Event
    # ========================================================

    def closeEvent(self, event):

        if self.transfer_worker:

            result = QMessageBox.question(
                self,
                "Transfer Running",
                (
                    "A transfer is currently running.\n\n"
                    "Do you want to pause it before closing?"
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )

            if (
                result
                == QMessageBox.StandardButton.Cancel
            ):

                event.ignore()

                return

            if (
                result
                == QMessageBox.StandardButton.Yes
            ):

                self.transfer_worker.request_pause()

                # Give the worker a moment to save state.
                QTimer.singleShot(
                    500,
                    self.close,
                )

                event.ignore()

                return

            if (
                result
                == QMessageBox.StandardButton.No
            ):

                self.transfer_worker.request_cancel()

        event.accept()


# ============================================================
# Application
# ============================================================

def main():

    app = QApplication(
        sys.argv
    )

    app.setApplicationName(
        "Safe Transfer"
    )

    window = MainWindow()

    window.show()

    sys.exit(
        app.exec()
    )


if __name__ == "__main__":
    main()
