"""
Safe Transfer - Windows File Copy / Move Assistant

Python 3.10+
Requires:
    pip install PyQt6

Windows-specific folder statistics:
    - Logical file size
    - Size on disk / allocated size
    - File count
    - Directory count
    - Scan errors
    - Volume capacity
    - Volume used space
    - Volume free space

Transfer features:
    - Copy / Move
    - Move = Copy -> Verify -> Delete Source
    - Pause / Resume
    - Skip existing files when destination size matches
    - Optional free-space pre-flight check
    - Find missing files
    - Copy missing files
    - Progress bar
    - Transfer speed
    - ETA
    - Persistent transfer log
    - Pause/resume state saved to JSON
    - Per-file error reporting
    - Optional file-size verification
    - Windows filesystem handling
    - Junction / symlink / reparse-point protection
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

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
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
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


# ============================================================
# Configuration
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

LOG_FILE = Path.home() / "copy_move_history.txt"
STATE_FILE = SCRIPT_DIR / ".pauseResumeState.json"

BUFFER_SIZE = 1024 * 1024
STATS_DEBOUNCE_MS = 500

# Windows constants.
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

    # --------------------------------------------------------
    # GetCompressedFileSizeW
    #
    # Returns the actual number of bytes allocated on disk for
    # a file, taking Windows compression/sparse allocation into
    # account.
    # --------------------------------------------------------

    kernel32.GetCompressedFileSizeW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]

    kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD

    # --------------------------------------------------------
    # GetDiskFreeSpaceExW
    #
    # Retrieves:
    #   free bytes available to the caller
    #   total bytes on volume
    #   total free bytes on volume
    # --------------------------------------------------------

    kernel32.GetDiskFreeSpaceExW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    ]

    kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL


# ============================================================
# Byte formatting
# ============================================================

def human_bytes(value: int | float) -> str:
    """
    Format bytes using IEC binary units.

    Examples:

        1024              -> 1.00 KiB
        1048576           -> 1.00 MiB
        1073741824        -> 1.00 GiB
        1099511627776     -> 1.00 TiB
    """

    try:
        value = max(0, int(value))
    except (TypeError, ValueError):
        return "0 B"

    units = (
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
        "PiB",
        "EiB",
    )

    size = float(value)
    unit_index = 0

    while (
        size >= 1024.0
        and unit_index < len(units) - 1
    ):
        size /= 1024.0
        unit_index += 1

    if unit_index == 0:
        return f"{value:,} B"

    return f"{size:,.2f} {units[unit_index]}"


# ============================================================
# Logging
# ============================================================

def append_log(message: str) -> None:
    timestamp = time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    try:
        with LOG_FILE.open(
            "a",
            encoding="utf-8",
        ) as f:
            f.write(
                f"[{timestamp}] {message}\n"
            )
    except Exception:
        pass


# ============================================================
# Windows filesystem helpers
# ============================================================

def is_reparse_point(path: Path) -> bool:
    """
    Return True when a Windows filesystem entry is a
    reparse point.

    Junctions, symlinks, mount points and other special
    Windows filesystem objects can be represented as
    reparse points.

    We do not follow them during folder scanning.
    """

    try:
        stat_result = os.lstat(path)

        attributes = getattr(
            stat_result,
            "st_file_attributes",
            0,
        )

        return bool(
            attributes
            & FILE_ATTRIBUTE_REPARSE_POINT
        )

    except (
        OSError,
        PermissionError,
    ):
        return False


def get_file_size_on_disk(path: Path) -> int:
    """
    Get Windows' allocated / compressed file size.

    This is much closer to the Windows Explorer
    "Size on disk" value than st_size.

    For a normal uncompressed file:

        logical size ~= size on disk

    For sparse/compressed files:

        logical size can be much larger than
        size on disk.
    """

    if os.name != "nt":
        # Fallback for non-Windows systems.
        try:
            return int(path.stat().st_blocks * 512)
        except (OSError, AttributeError):
            try:
                return int(path.stat().st_size)
            except OSError:
                return 0

    high = wintypes.DWORD(0)

    ctypes.set_last_error(ERROR_SUCCESS)

    low = kernel32.GetCompressedFileSizeW(
        str(path),
        ctypes.byref(high),
    )

    if low == INVALID_FILE_SIZE:
        error = ctypes.get_last_error()

        # INVALID_FILE_SIZE can technically be a valid
        # result when the actual size is exactly 0xFFFFFFFF,
        # so only treat it as an error when Windows reports
        # an actual error.
        if error != ERROR_SUCCESS:
            raise OSError(
                error,
                f"GetCompressedFileSizeW failed: {path}",
            )

    return (
        (int(high.value) << 32)
        | int(low)
    )


def get_volume_statistics(
    path: Path,
) -> tuple[int, int, int]:
    """
    Return:

        available_to_caller
        total_capacity
        total_free

    Uses Windows GetDiskFreeSpaceExW.
    """

    if os.name != "nt":
        usage = shutil.disk_usage(path)

        return (
            int(usage.free),
            int(usage.total),
            int(usage.free),
        )

    path = Path(path)

    # GetDiskFreeSpaceExW accepts a directory path.
    # If the target does not exist, use its nearest existing
    # parent.
    if not path.exists():
        current = path

        while (
            not current.exists()
            and current != current.parent
        ):
            current = current.parent

        path = current

    available = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    free = ctypes.c_ulonglong(0)

    result = kernel32.GetDiskFreeSpaceExW(
        str(path),
        ctypes.byref(available),
        ctypes.byref(total),
        ctypes.byref(free),
    )

    if not result:
        error = ctypes.get_last_error()

        raise OSError(
            error,
            f"GetDiskFreeSpaceExW failed: {path}",
        )

    return (
        int(available.value),
        int(total.value),
        int(free.value),
    )


# ============================================================
# Folder statistics
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

    # This is the total difference between logical size
    # and allocated size.
    allocation_difference: int = 0


def scan_folder_statistics(
    root: Path,
) -> FolderStatistics:
    """
    Recursively scan a Windows folder.

    Produces both:

        logical_size
            Sum of normal file sizes.

        allocated_size
            Sum of Windows Size-on-Disk values.

    Directories that are junctions/reparse points are not
    followed.
    """

    stats = FolderStatistics()

    root = Path(root)

    if not root.exists():
        stats.scan_errors = 1
        return stats

    pending = [root]

    while pending:
        current = pending.pop()

        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        entry_path = Path(
                            entry.path
                        )

                        # Never follow symbolic links.
                        if entry.is_symlink():
                            stats.skipped_reparse_points += 1
                            continue

                        # ------------------------------------------------
                        # Directory
                        # ------------------------------------------------

                        if entry.is_dir(
                            follow_symlinks=False
                        ):
                            stats.directory_count += 1

                            if is_reparse_point(
                                entry_path
                            ):
                                stats.skipped_reparse_points += 1
                                continue

                            pending.append(
                                entry_path
                            )

                            continue

                        # ------------------------------------------------
                        # File
                        # ------------------------------------------------

                        if not entry.is_file(
                            follow_symlinks=False
                        ):
                            continue

                        # Logical size.
                        try:
                            logical_size = int(
                                entry.stat(
                                    follow_symlinks=False
                                ).st_size
                            )
                        except (
                            OSError,
                            PermissionError,
                        ):
                            stats.scan_errors += 1
                            continue

                        # Windows allocated size / Size on disk.
                        try:
                            allocated_size = (
                                get_file_size_on_disk(
                                    entry_path
                                )
                            )
                        except (
                            OSError,
                            PermissionError,
                        ):
                            # If Windows refuses the native
                            # allocation query, still retain
                            # the logical file size.
                            allocated_size = logical_size
                            stats.scan_errors += 1

                        stats.logical_size += logical_size
                        stats.allocated_size += allocated_size
                        stats.file_count += 1

                        if (
                            logical_size
                            > stats.largest_file_size
                        ):
                            stats.largest_file_size = (
                                logical_size
                            )

                            stats.largest_file_path = (
                                str(entry_path)
                            )

                    except (
                        OSError,
                        PermissionError,
                    ):
                        stats.scan_errors += 1

        except (
            OSError,
            PermissionError,
        ):
            stats.scan_errors += 1

    stats.allocation_difference = (
        stats.allocated_size
        - stats.logical_size
    )

    return stats


# ============================================================
# Compatibility folder-size function
# ============================================================

def folder_size(
    root: Path,
) -> tuple[int, int, int]:
    """
    Compatibility helper.

    Returns:

        allocated_size
        file_count
        scan_errors

    This is intentionally now based on Windows
    "Size on disk", rather than logical file size.
    """

    stats = scan_folder_statistics(root)

    return (
        stats.allocated_size,
        stats.file_count,
        stats.scan_errors,
    )


# ============================================================
# File collection
# ============================================================

def collect_files(
    root: Path,
) -> list[Path]:
    """
    Recursively collect normal files.

    Junctions, symlinks and reparse-point directories
    are not followed.
    """

    files: list[Path] = []
    pending = [Path(root)]

    while pending:
        current = pending.pop()

        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        entry_path = Path(
                            entry.path
                        )

                        if entry.is_symlink():
                            continue

                        if entry.is_dir(
                            follow_symlinks=False
                        ):
                            if is_reparse_point(
                                entry_path
                            ):
                                continue

                            pending.append(
                                entry_path
                            )

                        elif entry.is_file(
                            follow_symlinks=False
                        ):
                            files.append(
                                entry_path
                            )

                    except (
                        OSError,
                        PermissionError,
                    ):
                        continue

        except (
            OSError,
            PermissionError,
        ):
            continue

    return files


# ============================================================
# Statistics worker
# ============================================================

class FolderStatsWorker(QObject):
    finished = pyqtSignal(object, object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        source: Path,
        destination: Path,
    ):
        super().__init__()

        self.source = source
        self.destination = destination

    def run(self) -> None:
        try:
            source_stats = scan_folder_statistics(
                self.source
            )

            (
                destination_available,
                destination_capacity,
                destination_free,
            ) = get_volume_statistics(
                self.destination
            )

            append_log(
                "FOLDER STATISTICS: "
                f"{self.source} | "
                f"Logical={source_stats.logical_size:,} bytes "
                f"({human_bytes(source_stats.logical_size)}) | "
                f"OnDisk={source_stats.allocated_size:,} bytes "
                f"({human_bytes(source_stats.allocated_size)}) | "
                f"Files={source_stats.file_count:,} | "
                f"Directories={source_stats.directory_count:,} | "
                f"Errors={source_stats.scan_errors:,} | "
                f"ReparseSkipped={source_stats.skipped_reparse_points:,}"
            )

            append_log(
                "DESTINATION VOLUME: "
                f"{self.destination} | "
                f"Capacity={destination_capacity:,} bytes "
                f"({human_bytes(destination_capacity)}) | "
                f"Free={destination_free:,} bytes "
                f"({human_bytes(destination_free)}) | "
                f"Available={destination_available:,} bytes "
                f"({human_bytes(destination_available)})"
            )

            self.finished.emit(
                source_stats,
                {
                    "available": destination_available,
                    "capacity": destination_capacity,
                    "free": destination_free,
                },
            )

        except Exception as exc:
            append_log(
                f"FOLDER STATISTICS FAILED: "
                f"{self.source} | {exc}"
            )

            self.failed.emit(
                str(exc)
            )


# ============================================================
# Transfer options
# ============================================================

@dataclass
class TransferOptions:
    operation: str
    skip_existing: bool
    ignore_space: bool
    verify: bool


# ============================================================
# Transfer worker
# ============================================================

class TransferWorker(QObject):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    log = pyqtSignal(str)

    finished = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(
        self,
        source: Path,
        destination: Path,
        options: TransferOptions,
        start_index: int = 0,
    ):
        super().__init__()

        self.source = Path(source)
        self.destination = Path(destination)
        self.options = options

        self.start_index = start_index

        self._pause_requested = False
        self._cancel_requested = False

        self.current_index = start_index

        self.total_files = 0
        self.total_bytes = 0
        self.completed_bytes = 0

        self.start_time = 0.0

    # --------------------------------------------------------
    # Control
    # --------------------------------------------------------

    def request_pause(self) -> None:
        self._pause_requested = True

    def request_cancel(self) -> None:
        self._cancel_requested = True

    # --------------------------------------------------------
    # State
    # --------------------------------------------------------

    def save_state(self) -> None:
        state = {
            "source": str(self.source),
            "destination": str(self.destination),
            "operation": self.options.operation,
            "skip_existing": self.options.skip_existing,
            "ignore_space": self.options.ignore_space,
            "verify": self.options.verify,
            "file_index": self.current_index,
        }

        try:
            with STATE_FILE.open(
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    state,
                    f,
                    indent=2,
                )

        except Exception as exc:
            self.log.emit(
                f"WARNING: Could not save pause state: {exc}"
            )

    # --------------------------------------------------------
    # Main
    # --------------------------------------------------------

    def run(self) -> None:
        try:
            self._run()

        except Exception as exc:
            append_log(
                "TRANSFER FAILED: "
                f"{exc}\n"
                f"{traceback.format_exc()}"
            )

            self.failed.emit(
                str(exc)
            )

    def _run(self) -> None:
        self.status.emit(
            "Scanning files..."
        )

        files = collect_files(
            self.source
        )

        self.total_files = len(files)

        if self.total_files == 0:
            raise RuntimeError(
                "No files were found in the source folder."
            )

        self.total_bytes = 0

        for path in files:
            try:
                self.total_bytes += int(
                    path.stat().st_size
                )
            except OSError:
                pass

        self.start_time = time.monotonic()

        append_log(
            "TRANSFER START: "
            f"{self.options.operation.upper()} | "
            f"{self.source} -> {self.destination} | "
            f"{self.total_files:,} files | "
            f"{self.total_bytes:,} logical bytes | "
            f"{human_bytes(self.total_bytes)}"
        )

        for index in range(
            self.start_index,
            len(files),
        ):
            self.current_index = index

            if self._cancel_requested:
                self.status.emit(
                    "Cancelled."
                )

                self.log.emit(
                    "Transfer cancelled by user."
                )

                return

            if self._pause_requested:
                self.save_state()

                self.status.emit(
                    "Paused."
                )

                self.log.emit(
                    f"Paused at file "
                    f"{index + 1:,} of "
                    f"{self.total_files:,}."
                )

                return

            source_file = files[index]

            relative = source_file.relative_to(
                self.source
            )

            destination_file = (
                self.destination / relative
            )

            try:
                file_size = int(
                    source_file.stat().st_size
                )
            except OSError as exc:
                self.log.emit(
                    f"ERROR: Could not stat "
                    f"{source_file}: {exc}"
                )

                continue

            # ------------------------------------------------
            # Existing file
            # ------------------------------------------------

            if destination_file.exists():
                try:
                    destination_size = int(
                        destination_file.stat().st_size
                    )
                except OSError:
                    destination_size = -1

                if (
                    self.options.skip_existing
                    and destination_size == file_size
                ):
                    self.completed_bytes += (
                        file_size
                    )

                    self.log.emit(
                        f"SKIP: {relative} "
                        f"({human_bytes(file_size)})"
                    )

                    self._update_progress()

                    continue

            # ------------------------------------------------
            # Create destination directory
            # ------------------------------------------------

            destination_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            # ------------------------------------------------
            # Free-space check
            # ------------------------------------------------

            if not self.options.ignore_space:
                try:
                    (
                        available,
                        _capacity,
                        _free,
                    ) = get_volume_statistics(
                        destination_file.parent
                    )

                except OSError as exc:
                    raise RuntimeError(
                        "Could not determine destination "
                        f"free space:\n{exc}"
                    )

                if available < file_size:
                    raise RuntimeError(
                        "Not enough free space.\n\n"
                        f"Required: "
                        f"{human_bytes(file_size)}\n"
                        f"Available: "
                        f"{human_bytes(available)}\n\n"
                        f"File:\n{source_file}"
                    )

            # ------------------------------------------------
            # Copy
            # ------------------------------------------------

            self.status.emit(
                f"Transferring: {relative}"
            )

            self.log.emit(
                f"{self.options.operation.upper()}: "
                f"{relative} "
                f"({human_bytes(file_size)})"
            )

            self._copy_file(
                source_file,
                destination_file,
            )

            # ------------------------------------------------
            # Verify
            # ------------------------------------------------

            if self.options.verify:
                self.status.emit(
                    f"Verifying: {relative}"
                )

                if not self._verify_file(
                    source_file,
                    destination_file,
                ):
                    raise RuntimeError(
                        f"Verification failed: "
                        f"{relative}"
                    )

            # ------------------------------------------------
            # Move cleanup
            # ------------------------------------------------

            if (
                self.options.operation
                == "move"
            ):
                try:
                    source_file.unlink()

                except OSError as exc:
                    raise RuntimeError(
                        "Copied successfully but could "
                        "not delete source file:\n"
                        f"{source_file}\n\n"
                        f"{exc}"
                    )

            self.completed_bytes += file_size

            self._update_progress()

        # ----------------------------------------------------
        # Complete
        # ----------------------------------------------------

        try:
            if STATE_FILE.exists():
                STATE_FILE.unlink()

        except OSError:
            pass

        elapsed = max(
            time.monotonic()
            - self.start_time,
            0.001,
        )

        speed = (
            self.completed_bytes
            / elapsed
        )

        self.log.emit(
            "TRANSFER COMPLETE: "
            f"{self.total_files:,} files | "
            f"{human_bytes(self.completed_bytes)} | "
            f"{human_bytes(speed)}/s"
        )

        self.status.emit(
            "Transfer complete."
        )

        self.progress.emit(
            100
        )

        self.finished.emit()

    # --------------------------------------------------------
    # Copy
    # --------------------------------------------------------

    def _copy_file(
        self,
        source: Path,
        destination: Path,
    ) -> None:

        try:
            source_size = int(
                source.stat().st_size
            )

        except OSError as exc:
            raise RuntimeError(
                f"Could not read source file:\n"
                f"{source}\n\n{exc}"
            )

        copied = 0

        with (
            source.open("rb") as src,
            destination.open("wb") as dst,
        ):
            while True:

                if self._cancel_requested:
                    raise RuntimeError(
                        "Transfer cancelled."
                    )

                if self._pause_requested:
                    self.save_state()

                    raise RuntimeError(
                        "Transfer paused."
                    )

                chunk = src.read(
                    BUFFER_SIZE
                )

                if not chunk:
                    break

                dst.write(chunk)

                copied += len(chunk)

        try:
            shutil.copystat(
                source,
                destination,
                follow_symlinks=False,
            )

        except OSError:
            pass

        if copied != source_size:
            raise RuntimeError(
                "Copy size mismatch:\n"
                f"Source: {source_size:,} bytes\n"
                f"Copied: {copied:,} bytes\n"
                f"File: {source}"
            )

    # --------------------------------------------------------
    # Verification
    # --------------------------------------------------------

    def _verify_file(
        self,
        source: Path,
        destination: Path,
    ) -> bool:

        try:
            source_size = int(
                source.stat().st_size
            )

            destination_size = int(
                destination.stat().st_size
            )

            return (
                source_size
                == destination_size
            )

        except OSError:
            return False

    # --------------------------------------------------------
    # Progress
    # --------------------------------------------------------

    def _update_progress(self) -> None:

        if self.total_bytes <= 0:
            percent = 100

        else:
            percent = int(
                (
                    self.completed_bytes
                    / self.total_bytes
                )
                * 100
            )

        percent = max(
            0,
            min(100, percent),
        )

        self.progress.emit(
            percent
        )

        elapsed = max(
            time.monotonic()
            - self.start_time,
            0.001,
        )

        speed = (
            self.completed_bytes
            / elapsed
        )

        if speed > 0:
            remaining = max(
                self.total_bytes
                - self.completed_bytes,
                0,
            )

            eta_seconds = (
                remaining / speed
            )

            if eta_seconds >= 3600:
                eta = (
                    f"{eta_seconds / 3600:.1f}h"
                )

            elif eta_seconds >= 60:
                eta = (
                    f"{eta_seconds / 60:.1f}m"
                )

            else:
                eta = (
                    f"{eta_seconds:.0f}s"
                )

        else:
            eta = "--"

        self.status.emit(
            f"{percent}% | "
            f"{human_bytes(speed)}/s | "
            f"ETA {eta}"
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
            1100,
            850,
        )

        self.worker_thread: Optional[QThread] = None
        self.worker: Optional[TransferWorker] = None

        self.stats_thread: Optional[QThread] = None
        self.stats_worker: Optional[FolderStatsWorker] = None

        self.current_source_stats = (
            FolderStatistics()
        )

        self.current_destination_stats = {
            "available": 0,
            "capacity": 0,
            "free": 0,
        }

        self.stats_timer = QTimer(
            self
        )

        self.stats_timer.setSingleShot(
            True
        )

        self.stats_timer.setInterval(
            STATS_DEBOUNCE_MS
        )

        self.stats_timer.timeout.connect(
            self.update_folder_statistics
        )

        self._build_ui()

        self.load_pause_state()

    # ========================================================
    # UI
    # ========================================================

    def _build_ui(self) -> None:

        central = QWidget()

        self.setCentralWidget(
            central
        )

        main_layout = QVBoxLayout(
            central
        )

        # ----------------------------------------------------
        # Paths
        # ----------------------------------------------------

        paths_group = QGroupBox(
            "Source / Destination"
        )

        paths_layout = QGridLayout(
            paths_group
        )

        paths_layout.addWidget(
            QLabel("Source:"),
            0,
            0,
        )

        self.source_edit = QLineEdit()

        source_button = QPushButton(
            "Browse..."
        )

        source_button.clicked.connect(
            self.browse_source
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

        self.destination_edit = QLineEdit()

        destination_button = QPushButton(
            "Browse..."
        )

        destination_button.clicked.connect(
            self.browse_destination
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

        main_layout.addWidget(
            paths_group
        )

        # ----------------------------------------------------
        # Options
        # ----------------------------------------------------

        options_group = QGroupBox(
            "Transfer Options"
        )

        options_layout = QGridLayout(
            options_group
        )

        options_layout.addWidget(
            QLabel("Operation:"),
            0,
            0,
        )

        self.operation_combo = QComboBox()

        self.operation_combo.addItems(
            [
                "Copy",
                "Move",
            ]
        )

        options_layout.addWidget(
            self.operation_combo,
            0,
            1,
        )

        self.skip_existing_check = QCheckBox(
            "Skip existing files when destination size matches"
        )

        self.ignore_space_check = QCheckBox(
            "Ignore free-space pre-flight check"
        )

        self.verify_check = QCheckBox(
            "Verify destination file size after copying"
        )

        options_layout.addWidget(
            self.skip_existing_check,
            1,
            0,
            1,
            2,
        )

        options_layout.addWidget(
            self.ignore_space_check,
            2,
            0,
            1,
            2,
        )

        options_layout.addWidget(
            self.verify_check,
            3,
            0,
            1,
            2,
        )

        main_layout.addWidget(
            options_group
        )

        # ----------------------------------------------------
        # Folder statistics
        # ----------------------------------------------------

        stats_group = QGroupBox(
            "Folder / Volume Statistics"
        )

        stats_layout = QVBoxLayout(
            stats_group
        )

        self.stats_label = QLabel(
            "Source statistics: --\n"
            "Destination volume: --"
        )

        self.stats_label.setWordWrap(
            True
        )

        stats_layout.addWidget(
            self.stats_label
        )

        main_layout.addWidget(
            stats_group
        )

        # ----------------------------------------------------
        # Status
        # ----------------------------------------------------

        self.status_label = QLabel(
            "Ready."
        )

        self.status_label.setFont(
            QFont(
                "Segoe UI",
                10,
            )
        )

        main_layout.addWidget(
            self.status_label
        )

        self.progress_bar = QProgressBar()

        self.progress_bar.setRange(
            0,
            100,
        )

        main_layout.addWidget(
            self.progress_bar
        )

        # ----------------------------------------------------
        # Buttons
        # ----------------------------------------------------

        button_layout = QHBoxLayout()

        self.start_button = QPushButton(
            "Start Transfer"
        )

        self.start_button.clicked.connect(
            self.start_transfer
        )

        self.pause_button = QPushButton(
            "Pause"
        )

        self.pause_button.clicked.connect(
            self.pause_transfer
        )

        self.cancel_button = QPushButton(
            "Cancel"
        )

        self.cancel_button.clicked.connect(
            self.cancel_transfer
        )

        self.find_missing_button = QPushButton(
            "Find Missing Files"
        )

        self.find_missing_button.clicked.connect(
            self.find_missing
        )

        self.copy_missing_button = QPushButton(
            "Copy Missing Files"
        )

        self.copy_missing_button.clicked.connect(
            self.copy_missing
        )

        button_layout.addWidget(
            self.start_button
        )

        button_layout.addWidget(
            self.pause_button
        )

        button_layout.addWidget(
            self.cancel_button
        )

        button_layout.addWidget(
            self.find_missing_button
        )

        button_layout.addWidget(
            self.copy_missing_button
        )

        main_layout.addLayout(
            button_layout
        )

        # ----------------------------------------------------
        # Log
        # ----------------------------------------------------

        log_group = QGroupBox(
            "Log"
        )

        log_layout = QVBoxLayout(
            log_group
        )

        self.log_edit = QPlainTextEdit()

        self.log_edit.setReadOnly(
            True
        )

        log_layout.addWidget(
            self.log_edit
        )

        main_layout.addWidget(
            log_group
        )

        # ----------------------------------------------------
        # Statistics triggers
        # ----------------------------------------------------

        self.source_edit.textChanged.connect(
            self.schedule_folder_statistics
        )

        self.destination_edit.textChanged.connect(
            self.schedule_folder_statistics
        )

        self.pause_button.setEnabled(
            False
        )

        self.cancel_button.setEnabled(
            False
        )

    # ========================================================
    # Browse
    # ========================================================

    def browse_source(self) -> None:

        path = QFileDialog.getExistingDirectory(
            self,
            "Select Source Folder",
        )

        if path:
            self.source_edit.setText(
                path
            )

    def browse_destination(self) -> None:

        path = QFileDialog.getExistingDirectory(
            self,
            "Select Destination Folder",
        )

        if path:
            self.destination_edit.setText(
                path
            )

    # ========================================================
    # Logging
    # ========================================================

    def add_log(
        self,
        message: str,
    ) -> None:

        self.log_edit.appendPlainText(
            message
        )

        append_log(
            message
        )

    # ========================================================
    # Statistics
    # ========================================================

    def schedule_folder_statistics(self) -> None:
        self.stats_timer.start()

    def update_folder_statistics(self) -> None:

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

        if not source_text:
            self.stats_label.setText(
                "Source statistics: --\n"
                "Destination volume: --"
            )

            return

        source = Path(
            source_text
        )

        if not source.exists():
            self.stats_label.setText(
                "Source statistics: "
                "folder does not exist.\n"
                "Destination volume: --"
            )

            return

        destination = (
            Path(destination_text)
            if destination_text
            else source.parent
        )

        if (
            self.stats_thread is not None
            and self.stats_thread.isRunning()
        ):
            return

        self.stats_label.setText(
            "Scanning source folder...\n"
            "Calculating Windows Size on Disk..."
        )

        self.stats_thread = QThread()

        self.stats_worker = FolderStatsWorker(
            source,
            destination,
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

        self.stats_worker.failed.connect(
            self._on_stats_failed
        )

        self.stats_worker.finished.connect(
            self.stats_thread.quit
        )

        self.stats_worker.failed.connect(
            self.stats_thread.quit
        )

        self.stats_thread.finished.connect(
            self._stats_thread_finished
        )

        self.stats_thread.start()

    def _on_stats_ready(
        self,
        source_stats: FolderStatistics,
        destination_stats: dict,
    ) -> None:

        self.current_source_stats = (
            source_stats
        )

        self.current_destination_stats = (
            destination_stats
        )

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
                f"-{human_bytes(abs(difference))}"
            )

        largest_text = "--"

        if source_stats.largest_file_size:
            largest_text = (
                f"{human_bytes(source_stats.largest_file_size)}"
            )

        error_text = ""

        if source_stats.scan_errors:
            error_text = (
                f"\nScan errors: "
                f"{source_stats.scan_errors:,}"
            )

        reparse_text = ""

        if source_stats.skipped_reparse_points:
            reparse_text = (
                f"\nReparse points skipped: "
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
            f"Allocation difference: {allocation_difference_text}\n"
            f"Files: {source_stats.file_count:,}\n"
            f"Directories: {source_stats.directory_count:,}\n"
            f"Largest file: {largest_text}"
            f"{error_text}"
            f"{reparse_text}"
            "\n\n"
            "DESTINATION VOLUME\n"
            f"Capacity: {human_bytes(destination_stats['capacity'])}\n"
            f"Used: {human_bytes(used)}\n"
            f"Free: {human_bytes(destination_stats['free'])}\n"
            f"Available to this process: "
            f"{human_bytes(destination_stats['available'])}"
        )

        # Add raw byte values to the log.
        self.add_log(
            "STATISTICS RAW: "
            f"SourceLogical={logical:,} bytes | "
            f"SourceOnDisk={allocated:,} bytes | "
            f"Files={source_stats.file_count:,}"
        )

        self.add_log(
            "VOLUME RAW: "
            f"Capacity="
            f"{destination_stats['capacity']:,} bytes | "
            f"Used="
            f"{destination_stats['capacity'] - destination_stats['free']:,} bytes | "
            f"Free="
            f"{destination_stats['free']:,} bytes | "
            f"Available="
            f"{destination_stats['available']:,} bytes"
        )

    def _on_stats_failed(
        self,
        error: str,
    ) -> None:

        self.stats_label.setText(
            "Folder statistics failed:\n"
            f"{error}"
        )

        self.add_log(
            f"STATISTICS ERROR: {error}"
        )

    def _stats_thread_finished(self) -> None:

        if self.stats_thread is not None:
            self.stats_thread.deleteLater()

        self.stats_thread = None
        self.stats_worker = None

    # ========================================================
    # Start transfer
    # ========================================================

    def start_transfer(self) -> None:

        if (
            self.worker_thread is not None
            and self.worker_thread.isRunning()
        ):
            QMessageBox.warning(
                self,
                "Transfer Running",
                "A transfer is already running.",
            )

            return

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

        if not source_text:
            QMessageBox.warning(
                self,
                "Missing Source",
                "Please select a source folder.",
            )

            return

        if not destination_text:
            QMessageBox.warning(
                self,
                "Missing Destination",
                "Please select a destination folder.",
            )

            return

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

            return

        try:
            if (
                source.resolve()
                == destination.resolve()
            ):
                QMessageBox.warning(
                    self,
                    "Invalid Paths",
                    "Source and destination "
                    "cannot be the same folder.",
                )

                return

        except OSError:
            pass

        options = TransferOptions(
            operation=(
                self.operation_combo
                .currentText()
                .lower()
            ),
            skip_existing=(
                self.skip_existing_check
                .isChecked()
            ),
            ignore_space=(
                self.ignore_space_check
                .isChecked()
            ),
            verify=(
                self.verify_check
                .isChecked()
            ),
        )

        self.progress_bar.setValue(
            0
        )

        self.worker_thread = QThread()

        self.worker = TransferWorker(
            source,
            destination,
            options,
        )

        self.worker.moveToThread(
            self.worker_thread
        )

        self.worker_thread.started.connect(
            self.worker.run
        )

        self.worker.progress.connect(
            self.progress_bar.setValue
        )

        self.worker.status.connect(
            self.status_label.setText
        )

        self.worker.log.connect(
            self.add_log
        )

        self.worker.finished.connect(
            self.transfer_finished
        )

        self.worker.failed.connect(
            self.transfer_failed
        )

        self.worker.finished.connect(
            self.worker_thread.quit
        )

        self.worker.failed.connect(
            self.worker_thread.quit
        )

        self.worker_thread.finished.connect(
            self.transfer_thread_finished
        )

        self.start_button.setEnabled(
            False
        )

        self.pause_button.setEnabled(
            True
        )

        self.cancel_button.setEnabled(
            True
        )

        self.worker_thread.start()

    # ========================================================
    # Pause
    # ========================================================

    def pause_transfer(self) -> None:

        if self.worker is None:
            return

        self.worker.request_pause()

        self.pause_button.setEnabled(
            False
        )

        self.status_label.setText(
            "Pausing..."
        )

    # ========================================================
    # Cancel
    # ========================================================

    def cancel_transfer(self) -> None:

        if self.worker is None:
            return

        self.worker.request_cancel()

        self.cancel_button.setEnabled(
            False
        )

        self.status_label.setText(
            "Cancelling..."
        )

    # ========================================================
    # Transfer callbacks
    # ========================================================

    def transfer_finished(self) -> None:

        self.status_label.setText(
            "Transfer complete."
        )

        self.progress_bar.setValue(
            100
        )

        QMessageBox.information(
            self,
            "Transfer Complete",
            "The transfer completed successfully.",
        )

    def transfer_failed(
        self,
        error: str,
    ) -> None:

        if (
            STATE_FILE.exists()
            and "paused"
            in error.lower()
        ):
            self.status_label.setText(
                "Paused."
            )

            return

        self.status_label.setText(
            "Transfer stopped."
        )

        QMessageBox.critical(
            self,
            "Transfer Error",
            error,
        )

    def transfer_thread_finished(self) -> None:

        if self.worker_thread is not None:
            self.worker_thread.deleteLater()

        self.worker_thread = None
        self.worker = None

        self.start_button.setEnabled(
            True
        )

        self.pause_button.setEnabled(
            False
        )

        self.cancel_button.setEnabled(
            False
        )

    # ========================================================
    # Pause-state loading
    # ========================================================

    def load_pause_state(self) -> None:

        if not STATE_FILE.exists():
            return

        try:
            with STATE_FILE.open(
                "r",
                encoding="utf-8",
            ) as f:
                state = json.load(f)

            source = state.get(
                "source"
            )

            destination = state.get(
                "destination"
            )

            if not source or not destination:
                return

            reply = QMessageBox.question(
                self,
                "Resume Transfer",
                (
                    "A paused transfer was found.\n\n"
                    f"Source:\n{source}\n\n"
                    f"Destination:\n{destination}\n\n"
                    "Resume it?"
                ),
            )

            if (
                reply
                != QMessageBox.StandardButton.Yes
            ):
                return

            self.source_edit.setText(
                source
            )

            self.destination_edit.setText(
                destination
            )

            operation = state.get(
                "operation",
                "copy",
            )

            index = (
                self.operation_combo.findText(
                    operation.capitalize()
                )
            )

            if index >= 0:
                self.operation_combo.setCurrentIndex(
                    index
                )

            self.skip_existing_check.setChecked(
                bool(
                    state.get(
                        "skip_existing",
                        False,
                    )
                )
            )

            self.ignore_space_check.setChecked(
                bool(
                    state.get(
                        "ignore_space",
                        False,
                    )
                )
            )

            self.verify_check.setChecked(
                bool(
                    state.get(
                        "verify",
                        False,
                    )
                )
            )

            self.add_log(
                "Paused transfer state loaded."
            )

        except Exception as exc:
            self.add_log(
                "WARNING: Could not load pause state: "
                f"{exc}"
            )

    # ========================================================
    # Find missing
    # ========================================================

    def find_missing(self) -> None:

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

        if (
            not source_text
            or not destination_text
        ):
            QMessageBox.warning(
                self,
                "Missing Paths",
                "Please select both source "
                "and destination.",
            )

            return

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

            return

        self.add_log(
            "Scanning for missing files..."
        )

        source_files = collect_files(
            source
        )

        missing: list[Path] = []

        for source_file in source_files:

            relative = (
                source_file.relative_to(
                    source
                )
            )

            destination_file = (
                destination / relative
            )

            if not destination_file.exists():
                missing.append(
                    relative
                )

        self.log_edit.appendPlainText(
            ""
        )

        self.log_edit.appendPlainText(
            f"Missing files: "
            f"{len(missing):,}"
        )

        for relative in missing:
            self.log_edit.appendPlainText(
                str(relative)
            )

        self.add_log(
            "Missing-file scan complete: "
            f"{len(missing):,} missing files."
        )

    # ========================================================
    # Copy missing
    # ========================================================

    def copy_missing(self) -> None:

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

        if (
            not source_text
            or not destination_text
        ):
            QMessageBox.warning(
                self,
                "Missing Paths",
                "Please select both source "
                "and destination.",
            )

            return

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

            return

        source_files = collect_files(
            source
        )

        missing: list[Path] = []

        for source_file in source_files:

            relative = (
                source_file.relative_to(
                    source
                )
            )

            destination_file = (
                destination / relative
            )

            if not destination_file.exists():
                missing.append(
                    source_file
                )

        if not missing:
            QMessageBox.information(
                self,
                "No Missing Files",
                "No missing files were found.",
            )

            return

        reply = QMessageBox.question(
            self,
            "Copy Missing Files",
            (
                f"{len(missing):,} "
                "missing files were found.\n\n"
                "Copy them to the destination?"
            ),
        )

        if (
            reply
            != QMessageBox.StandardButton.Yes
        ):
            return

        self.skip_existing_check.setChecked(
            True
        )

        self.operation_combo.setCurrentText(
            "Copy"
        )

        self.start_transfer()


# ============================================================
# Main
# ============================================================

def main() -> None:

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
