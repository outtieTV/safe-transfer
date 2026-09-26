#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Safe Transfer - Windows File Copy / Move Assistant
===================================================

PyQt6 application providing:

    • Copy files
    • Move files
    • Find missing files
    • Copy missing files
    • Create destination folder structure before transferring files
    • Sort transfers largest -> smallest
    • Sort transfers smallest -> largest
    • Save file-size scan information to a text document
    • Pause / Resume
    • Skip existing files with the same size
    • Optional free-space pre-flight check
    • Progress reporting
    • Transfer speed
    • Byte-based ETA
    • Estimated completion time using local computer time
    • Source / destination filesystem statistics
    • Persistent logging
    • JSON pause/resume state
    • Per-file error handling
    • Optional file-size verification
    • Background worker threads
    • Windows-friendly filesystem handling
    • Junction/symlink protection
    • Manual folder statistics scan
    • Optional shutdown when transfer completes
    • Large directory support

Requirements:

    Python 3.10+
    PyQt6

Install:

    py -m pip install PyQt6

Run:

    py safe-transfer.pyw

Log:

    %USERPROFILE%\\copy_move_history.txt

State:

    .pauseResumeState.json

File-size scan:

    transfer_file_sizes.txt
"""

import sys
import os
import json
import time
import shutil
import subprocess

from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Tuple

try:
    import humanize
except ImportError:
    humanize = None

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
)


# ======================================================================
# PATHS
# ======================================================================

SCRIPT_DIR = Path(__file__).resolve().parent

LOG_FILE = Path.home() / "copy_move_history.txt"

STATE_FILE = SCRIPT_DIR / ".pauseResumeState.json"

SIZE_LIST_FILE = SCRIPT_DIR / "transfer_file_sizes.txt"


# ======================================================================
# GENERAL UTILITIES
# ======================================================================

def human_bytes(value: int | float) -> str:
    """
    Format bytes using binary units.

    Falls back to an internal formatter if humanize is not installed.
    """

    value = max(0, int(value))

    if humanize:
        return humanize.naturalsize(
            value,
            binary=True,
        )

    units = [
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
        "PiB",
    ]

    size = float(value)

    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"

            return f"{size:.2f} {unit}"

        size /= 1024

    return f"{value} B"


def format_seconds(seconds: float) -> str:
    """Convert seconds to HHh MMm SSs."""

    if seconds <= 0:
        return "—"

    seconds = max(0, int(seconds))

    hours, remainder = divmod(
        seconds,
        3600,
    )

    minutes, seconds = divmod(
        remainder,
        60,
    )

    return (
        f"{hours:02d}h "
        f"{minutes:02d}m "
        f"{seconds:02d}s"
    )


def format_completion_time(seconds: float) -> str:
    """
    Return the estimated completion time using the computer's
    local timezone.
    """

    if seconds <= 0:
        return "—"

    timestamp = (
        time.time()
        + seconds
    )

    return datetime.fromtimestamp(
        timestamp
    ).astimezone().strftime(
        "%Y-%m-%d %I:%M:%S %p"
    )


def free_space(path: Path) -> int:
    """
    Return free space on the filesystem containing path.
    """

    target = (
        path
        if path.exists()
        else path.parent
    )

    return shutil.disk_usage(
        target
    ).free


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
                line + "\n"
            )

    except Exception:
        pass


# ======================================================================
# FOLDER STATISTICS
# ======================================================================

@dataclass
class FolderStats:
    logical_size: int = 0
    file_count: int = 0
    directory_count: int = 0
    errors: int = 0
    reparse_points: int = 0
    largest_file_size: int = 0
    largest_file_path: str = ""


def get_windows_allocated_size(
    path: Path,
) -> int | None:
    """
    Attempt to determine Windows' allocated size for a file.

    This uses GetCompressedFileSizeW through ctypes.

    Returns None if the operation cannot be performed.
    """

    if os.name != "nt":
        return None

    try:
        import ctypes

        GetCompressedFileSizeW = (
            ctypes.windll.kernel32
            .GetCompressedFileSizeW
        )

        GetCompressedFileSizeW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(
                ctypes.c_ulong
            ),
        ]

        GetCompressedFileSizeW.restype = (
            ctypes.c_ulong
        )

        high = ctypes.c_ulong()

        low = GetCompressedFileSizeW(
            str(path),
            ctypes.byref(high),
        )

        invalid = 0xFFFFFFFF

        if (
            low == invalid
            and ctypes.GetLastError() != 0
        ):
            return None

        return (
            (high.value << 32)
            + low
        )

    except Exception:
        return None


def folder_size(
    root: Path,
) -> FolderStats:
    """
    Scan a directory and calculate logical file size plus
    Windows allocated size where available.

    Junctions and symbolic links are not followed.
    """

    stats = FolderStats()

    root = Path(root)

    def walk_error(error):
        stats.errors += 1

        append_log(
            f"SIZE SCAN WARNING: {error}"
        )

    try:

        for current_root, dirs, files in os.walk(
            root,
            topdown=True,
            followlinks=False,
            onerror=walk_error,
        ):

            stats.directory_count += len(
                dirs
            )

            filtered_dirs = []

            for dirname in dirs:

                directory = (
                    Path(current_root)
                    / dirname
                )

                try:

                    if directory.is_symlink():
                        stats.reparse_points += 1
                        continue

                    filtered_dirs.append(
                        dirname
                    )

                except OSError:

                    stats.errors += 1

            dirs[:] = filtered_dirs

            for filename in files:

                file_path = (
                    Path(current_root)
                    / filename
                )

                try:

                    if file_path.is_symlink():
                        stats.reparse_points += 1
                        continue

                    size = (
                        file_path.stat().st_size
                    )

                    stats.logical_size += size
                    stats.file_count += 1

                    if (
                        size
                        > stats.largest_file_size
                    ):
                        stats.largest_file_size = size
                        stats.largest_file_path = (
                            str(file_path)
                        )

                except (
                    OSError,
                    PermissionError,
                ) as e:

                    stats.errors += 1

                    append_log(
                        f"SIZE SCAN WARNING: "
                        f"{file_path}: {e}"
                    )

    except Exception as e:

        stats.errors += 1

        append_log(
            f"SIZE SCAN ERROR: "
            f"{root}: {e}"
        )

    return stats


def filesystem_stats(
    path: Path,
) -> dict:

    usage = shutil.disk_usage(
        path
    )

    return {
        "capacity": usage.total,
        "free": usage.free,
        "used": (
            usage.total
            - usage.free
        ),
        "available": usage.free,
    }


# ======================================================================
# STATE MANAGEMENT
# ======================================================================

def load_state() -> dict | None:

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

    temp_file = (
        STATE_FILE.with_suffix(
            ".tmp"
        )
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
# FILE SIZE LIST
# ======================================================================

def save_file_size_list(
    files: list,
    source: Path,
    sort_mode: str,
):

    try:

        with SIZE_LIST_FILE.open(
            "w",
            encoding="utf-8",
        ) as f:

            f.write(
                "Safe Transfer - File Size List\n"
            )

            f.write(
                "================================\n\n"
            )

            f.write(
                f"Source: {source}\n"
            )

            f.write(
                f"Generated: "
                f"{datetime.now().astimezone()}\n"
            )

            f.write(
                f"Sort order: {sort_mode}\n"
            )

            f.write(
                f"Files: {len(files):,}\n\n"
            )

            f.write(
                "SIZE_BYTES\tSIZE\tPATH\n"
            )

            f.write(
                "----------------------------------------\n"
            )

            for path, size in files:

                try:
                    relative = path.relative_to(
                        source
                    )

                except ValueError:
                    relative = path.name

                f.write(
                    f"{size}\t"
                    f"{human_bytes(size)}\t"
                    f"{relative}\n"
                )

    except Exception as e:

        append_log(
            f"SIZE LIST SAVE ERROR: {e}"
        )


# ======================================================================
# FOLDER STATS WORKER
# ======================================================================

class FolderStatsWorker(QThread):

    result = pyqtSignal(
        object,
        object,
    )

    error = pyqtSignal(str)

    def __init__(
        self,
        source_path,
        destination_path,
    ):

        super().__init__()

        self.source_path = source_path
        self.destination_path = (
            destination_path
        )

    def run(self):

        try:

            source_stats = None

            destination_stats = None

            if (
                self.source_path
                and self.source_path.exists()
                and self.source_path.is_dir()
            ):

                source_stats = folder_size(
                    self.source_path
                )

            if self.destination_path:

                destination_stats = (
                    filesystem_stats(
                        self.destination_path
                    )
                )

            self.result.emit(
                source_stats,
                destination_stats,
            )

        except Exception as e:

            self.error.emit(
                str(e)
            )


# ======================================================================
# SOURCE FILE SCANNER
# ======================================================================

class FileScanner(QThread):

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
        sort_mode: str,
    ):

        super().__init__()

        self.source = source
        self.sort_mode = sort_mode

    def run(self):

        try:

            files = []

            inaccessible = 0

            count = 0

            self.status.emit(
                "Scanning source directory..."
            )

            last_update = (
                time.monotonic()
            )

            def walk_error(error):

                nonlocal inaccessible

                inaccessible += 1

                append_log(
                    f"FILE SCAN WARNING: "
                    f"{error}"
                )

            for current_root, dirs, filenames in os.walk(
                self.source,
                topdown=True,
                followlinks=False,
                onerror=walk_error,
            ):

                filtered_dirs = []

                for dirname in dirs:

                    directory = (
                        Path(current_root)
                        / dirname
                    )

                    try:

                        if directory.is_symlink():
                            continue

                        filtered_dirs.append(
                            dirname
                        )

                    except OSError:

                        inaccessible += 1

                dirs[:] = filtered_dirs

                for filename in filenames:

                    path = (
                        Path(current_root)
                        / filename
                    )

                    count += 1

                    try:

                        if path.is_symlink():
                            continue

                        size = (
                            path.stat().st_size
                        )

                        files.append(
                            (
                                path,
                                size,
                            )
                        )

                    except (
                        OSError,
                        PermissionError,
                    ) as e:

                        inaccessible += 1

                        append_log(
                            f"FILE SCAN WARNING: "
                            f"{path}: {e}"
                        )

                    now = time.monotonic()

                    if (
                        now - last_update
                        > 0.10
                    ):

                        self.status.emit(
                            "Scanning source "
                            f"directory... "
                            f"({count:,} files found)"
                        )

                        last_update = now

            total = len(files)

            self.status.emit(
                f"Sorting {total:,} files..."
            )

            if self.sort_mode == "Largest → Smallest":

                files.sort(
                    key=lambda item: item[1],
                    reverse=True,
                )

            else:

                files.sort(
                    key=lambda item: item[1]
                )

            save_file_size_list(
                files,
                self.source,
                self.sort_mode,
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
                "Indexing source directory..."
            )

            last_update = (
                time.monotonic()
            )

            def walk_error(error):

                nonlocal inaccessible

                inaccessible += 1

                append_log(
                    f"MISSING SCAN WARNING: "
                    f"{error}"
                )

            for current_root, dirs, filenames in os.walk(
                self.source,
                topdown=True,
                followlinks=False,
                onerror=walk_error,
            ):

                filtered_dirs = []

                for dirname in dirs:

                    directory = (
                        Path(current_root)
                        / dirname
                    )

                    try:

                        if directory.is_symlink():
                            continue

                        filtered_dirs.append(
                            dirname
                        )

                    except OSError:

                        inaccessible += 1

                dirs[:] = filtered_dirs

                for filename in filenames:

                    src = (
                        Path(current_root)
                        / filename
                    )

                    try:

                        if src.is_symlink():
                            continue

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
                    ) as e:

                        inaccessible += 1

                        append_log(
                            f"MISSING SCAN WARNING: "
                            f"{src}: {e}"
                        )

                    now = time.monotonic()

                    if (
                        now - last_update
                        > 0.10
                    ):

                        self.status.emit(
                            "Scanning source... "
                            f"({len(all_files):,} "
                            "items indexed)"
                        )

                        last_update = now

            total = len(all_files)

            missing = []

            if total == 0:

                self.result.emit(
                    [],
                    0,
                    inaccessible,
                )

                return

            last_update = (
                time.monotonic()
            )

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

                except (
                    OSError,
                    PermissionError,
                ) as e:

                    inaccessible += 1

                    append_log(
                        f"MISSING CHECK WARNING: "
                        f"{relative}: {e}"
                    )

                now = time.monotonic()

                if (
                    now - last_update
                    > 0.05
                    or index == total
                ):

                    self.status.emit(
                        f"Checking "
                        f"{index:,}/{total:,}: "
                        f"{relative}"
                    )

                    self.progress.emit(
                        int(
                            index
                            / total
                            * 100
                        )
                    )

                    last_update = now

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

    progress = pyqtSignal(int)

    status = pyqtSignal(str)

    statistics = pyqtSignal(str)

    log = pyqtSignal(str)

    finished_result = pyqtSignal(dict)

    error = pyqtSignal(str)

    def __init__(
        self,
        source: Path,
        destination: Path,
        files: list,
        move: bool,
        skip_existing: bool,
        ignore_space: bool,
        verify_files: bool,
        shutdown_on_complete: bool,
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

        self.verify_files = (
            verify_files
        )

        self.shutdown_on_complete = (
            shutdown_on_complete
        )

        self.start_index = start_index

        self.pause_requested = False
        self.stop_requested = False

        self.started = time.monotonic()

        self.bytes_copied = 0

        self.total_bytes = sum(
            size
            for _, size
            in files[
                start_index:
            ]
        )

        self.files_copied = 0

        self.files_skipped = 0

        self.failed = []

        self.successful_source_files = []

    def request_pause(self):
        self.pause_requested = True

    def request_stop(self):
        self.stop_requested = True

    def calculate_required_space(self):

        required = 0

        for src_file, size in self.files[
            self.start_index:
        ]:

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

            if (
                self.skip_existing
                and dst_file.is_file()
            ):

                try:

                    if (
                        dst_file.stat()
                        .st_size
                        == size
                    ):
                        continue

                except (
                    OSError,
                    PermissionError,
                ):
                    pass

            required += size

        return required

    def create_folder_structure(self):

        directories = set()

        for src_file, _ in self.files:

            try:

                relative = (
                    src_file.relative_to(
                        self.source
                    )
                )

                parent = (
                    relative.parent
                )

                if str(parent) != ".":

                    directories.add(
                        parent
                    )

            except ValueError:

                continue

        directories = sorted(
            directories,
            key=lambda p: (
                len(p.parts),
                str(p).lower(),
            )
        )

        for relative_dir in directories:

            if self.stop_requested:
                return

            destination_dir = (
                self.destination
                / relative_dir
            )

            try:

                destination_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            except Exception as e:

                self.log.emit(
                    f"WARNING - could not create "
                    f"directory {relative_dir}: {e}"
                )

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
                    "index": 0,
                    "successful_source_files": [],
                }
            )

            return

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
            "VerifyFiles": (
                self.verify_files
            ),
            "ShutdownOnComplete": (
                self.shutdown_on_complete
            ),
            "Index": self.start_index,
            "Paused": False,
        }

        # --------------------------------------------------------------
        # FREE SPACE CHECK
        # --------------------------------------------------------------

        if not self.ignore_space:

            try:

                required = (
                    self.calculate_required_space()
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
                            "index": self.start_index,
                            "successful_source_files": [],
                        }
                    )

                    return

            except Exception as e:

                self.error.emit(
                    "Could not determine "
                    "destination free space:\n\n"
                    f"{e}"
                )

                return

        # --------------------------------------------------------------
        # CREATE EMPTY FOLDER STRUCTURE
        # --------------------------------------------------------------

        self.status.emit(
            "Creating destination "
            "folder structure..."
        )

        self.create_folder_structure()

        # --------------------------------------------------------------
        # TRANSFER
        # --------------------------------------------------------------

        last_status_update = 0.0

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

            now = time.monotonic()

            if (
                now - last_status_update
                > 0.05
                or index
                == total_files - 1
            ):

                self.status.emit(
                    f"{index + 1:,} / "
                    f"{total_files:,}    "
                    f"{relative}"
                )

                last_status_update = now

            # ----------------------------------------------------------
            # SKIP EXISTING
            # ----------------------------------------------------------

            if (
                self.skip_existing
                and dst_file.is_file()
            ):

                try:

                    if (
                        dst_file.stat()
                        .st_size
                        == size
                    ):

                        self.files_skipped += 1

                        self.successful_source_files.append(
                            src_file
                        )

                        self.log.emit(
                            f"SKIPPED - "
                            f"{relative} "
                            f"(same size)"
                        )

                        self.update_statistics(
                            index + 1,
                            total_files,
                        )

                        if self.pause_requested:

                            state["Paused"] = True

                            state["Index"] = (
                                index + 1
                            )

                            save_state(
                                state
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
                                    "successful_source_files":
                                        self.successful_source_files,
                                }
                            )

                            return

                        continue

                except Exception as e:

                    self.log.emit(
                        f"WARNING - inspect "
                        f"error {relative}: {e}"
                    )

            # ----------------------------------------------------------
            # CREATE PARENT DIRECTORY
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

                self.update_statistics(
                    index + 1,
                    total_files,
                )

                continue

            # ----------------------------------------------------------
            # COPY
            # ----------------------------------------------------------

            try:

                shutil.copy2(
                    src_file,
                    dst_file,
                )

                if self.verify_files:

                    destination_size = (
                        dst_file.stat()
                        .st_size
                    )

                    if (
                        destination_size
                        != size
                    ):

                        raise IOError(
                            "Size mismatch "
                            "after copy. "
                            f"Expected "
                            f"{size} bytes, "
                            f"got "
                            f"{destination_size} "
                            "bytes."
                        )

                self.bytes_copied += size

                self.files_copied += 1

                self.successful_source_files.append(
                    src_file
                )

                operation = (
                    "COPIED FOR MOVE"
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

            # ----------------------------------------------------------
            # SAVE STATE
            # ----------------------------------------------------------

            if (
                index % 50 == 0
                or index
                == total_files - 1
            ):

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

            # ----------------------------------------------------------
            # PAUSE
            # ----------------------------------------------------------

            if self.pause_requested:

                state["Paused"] = True

                state["Index"] = (
                    index + 1
                )

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
                        "successful_source_files":
                            self.successful_source_files,
                    }
                )

                return

        # --------------------------------------------------------------
        # FINISHED
        # --------------------------------------------------------------

        success = (
            len(self.failed) == 0
            and not self.stop_requested
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
                "successful_source_files":
                    self.successful_source_files,
                "shutdown_on_complete":
                    self.shutdown_on_complete,
            }
        )

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

        remaining_bytes = max(
            0,
            self.total_bytes
            - self.bytes_copied,
        )

        if (
            speed > 0
            and remaining_bytes > 0
        ):

            eta = (
                remaining_bytes
                / speed
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

        if speed > 0:

            eta_text = format_seconds(
                eta
            )

            completion_text = (
                format_completion_time(
                    eta
                )
            )

        else:

            eta_text = "—"

            completion_text = "—"

        self.statistics.emit(
            f"Files: "
            f"{completed:,}/{total:,}"
            f"    |    "
            f"Transferred: "
            f"{human_bytes(self.bytes_copied)}"
            f" / "
            f"{human_bytes(self.total_bytes)}"
            f"    |    "
            f"Speed: "
            f"{human_bytes(speed)}/s"
            f"    |    "
            f"ETA: {eta_text}"
            f"    |    "
            f"Done: {completion_text}"
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
            1050,
            850,
        )

        self.worker = None
        self.scan_worker = None
        self.stats_worker = None

        self.source = None
        self.destination = None

        self.file_list = []
        self.missing_files = []

        self.current_operation = None

        # --------------------------------------------------------------
        # SOURCE
        # --------------------------------------------------------------

        self.source_edit = QLineEdit()

        self.source_edit.setPlaceholderText(
            "Source folder..."
        )

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
        # DESTINATION
        # --------------------------------------------------------------

        self.destination_edit = QLineEdit()

        self.destination_edit.setPlaceholderText(
            "Destination folder..."
        )

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
        # STATISTICS BUTTON
        # --------------------------------------------------------------

        self.scan_stats_button = QPushButton(
            "Scan Source and Destination Size"
        )

        self.scan_stats_button.clicked.connect(
            self.manual_folder_statistics
        )

        # --------------------------------------------------------------
        # OPERATION
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
        # SORT ORDER
        # --------------------------------------------------------------

        sort_group = QGroupBox(
            "Transfer Order"
        )

        sort_layout = QHBoxLayout()

        self.sort_combo = QComboBox()

        self.sort_combo.addItems(
            [
                "Largest → Smallest",
                "Smallest → Largest",
            ]
        )

        sort_layout.addWidget(
            QLabel("File order:")
        )

        sort_layout.addWidget(
            self.sort_combo
        )

        sort_layout.addWidget(
            QLabel(
                "File sizes are saved to "
                f"{SIZE_LIST_FILE.name}"
            )
        )

        sort_layout.addStretch()

        sort_group.setLayout(
            sort_layout
        )

        # --------------------------------------------------------------
        # OPTIONS
        # --------------------------------------------------------------

        options_group = QGroupBox(
            "Options"
        )

        options_layout = QVBoxLayout()

        row1 = QHBoxLayout()

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

        row1.addWidget(
            self.skip_existing
        )

        row1.addWidget(
            self.ignore_space
        )

        row1.addWidget(
            self.verify_files
        )

        row2 = QHBoxLayout()

        self.shutdown_on_complete = QCheckBox(
            "Shutdown computer when transfer completes successfully"
        )

        row2.addWidget(
            self.shutdown_on_complete
        )

        row2.addStretch()

        options_layout.addLayout(
            row1
        )

        options_layout.addLayout(
            row2
        )

        options_group.setLayout(
            options_layout
        )

        # --------------------------------------------------------------
        # BUTTONS
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
        # STATISTICS
        # --------------------------------------------------------------

        self.stats_label = QLabel(
            "SOURCE\n"
            "Logical size: —\n"
            "Files: —\n"
            "Directories: —\n\n"
            "DESTINATION VOLUME\n"
            "Capacity: —\n"
            "Used: —\n"
            "Free: —"
        )

        self.stats_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
        )

        self.stats_label.setWordWrap(
            True
        )

        # --------------------------------------------------------------
        # STATUS
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
        # PROGRESS
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
        # LOG
        # --------------------------------------------------------------

        self.log_view = QTextEdit()

        self.log_view.setReadOnly(
            True
        )

        self.log_view.document().setMaximumBlockCount(
            1500
        )

        # --------------------------------------------------------------
        # LAYOUT
        # --------------------------------------------------------------

        main = QVBoxLayout()

        main.addLayout(
            source_layout
        )

        main.addLayout(
            destination_layout
        )

        main.addWidget(
            self.scan_stats_button
        )

        main.addWidget(
            operation_group
        )

        main.addWidget(
            sort_group
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

        self.check_resume_state()

    # ==================================================================
    # FOLDER STATISTICS
    # ==================================================================

    def manual_folder_statistics(self):

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

        if (
            not source.exists()
            or not source.is_dir()
        ):

            QMessageBox.warning(
                self,
                "Invalid Source",
                "The source folder does not exist.",
            )

            return

        try:

            destination.mkdir(
                parents=True,
                exist_ok=True,
            )

        except Exception as e:

            QMessageBox.warning(
                self,
                "Destination Error",
                str(e),
            )

            return

        if (
            self.stats_worker
            and self.stats_worker.isRunning()
        ):

            return

        self.stats_label.setText(
            "Scanning source and "
            "destination..."
        )

        self.scan_stats_button.setEnabled(
            False
        )

        self.stats_worker = FolderStatsWorker(
            source,
            destination,
        )

        self.stats_worker.result.connect(
            self._on_stats_ready
        )

        self.stats_worker.error.connect(
            self._stats_error
        )

        self.stats_worker.finished.connect(
            self._stats_finished
        )

        self.stats_worker.start()

    def _stats_finished(self):

        self.stats_worker = None

        self.scan_stats_button.setEnabled(
            True
        )

    def _stats_error(
        self,
        message,
    ):

        self.stats_label.setText(
            f"Statistics error:\n{message}"
        )

        self.append_log(
            f"STATISTICS ERROR: {message}"
        )

    def _on_stats_ready(
        self,
        source_stats,
        destination_stats,
    ):

        if source_stats:

            logical = (
                source_stats.logical_size
            )

            largest_text = "—"

            if (
                source_stats.largest_file_size
                > 0
            ):

                largest_text = (
                    f"{human_bytes(source_stats.largest_file_size)} "
                    f"— "
                    f"{source_stats.largest_file_path}"
                )

            error_text = ""

            if source_stats.errors:

                error_text = (
                    "\n"
                    f"Scan errors: "
                    f"{source_stats.errors:,}"
                )

            reparse_text = ""

            if source_stats.reparse_points:

                reparse_text = (
                    "\n"
                    f"Skipped reparse points: "
                    f"{source_stats.reparse_points:,}"
                )

            self.stats_label.setText(
                "SOURCE\n"
                f"Logical size: "
                f"{human_bytes(logical)}\n"
                f"Files: "
                f"{source_stats.file_count:,}\n"
                f"Directories: "
                f"{source_stats.directory_count:,}\n"
                f"Largest file: "
                f"{largest_text}"
                f"{error_text}"
                f"{reparse_text}"
                "\n\n"
                "DESTINATION VOLUME\n"
                f"Capacity: "
                f"{human_bytes(destination_stats['capacity'])}\n"
                f"Used: "
                f"{human_bytes(destination_stats['used'])}\n"
                f"Free: "
                f"{human_bytes(destination_stats['free'])}\n"
                f"Available to this process: "
                f"{human_bytes(destination_stats['available'])}"
            )

        elif destination_stats:

            self.stats_label.setText(
                "SOURCE\n"
                "Unavailable\n\n"
                "DESTINATION VOLUME\n"
                f"Capacity: "
                f"{human_bytes(destination_stats['capacity'])}\n"
                f"Used: "
                f"{human_bytes(destination_stats['used'])}\n"
                f"Free: "
                f"{human_bytes(destination_stats['free'])}\n"
                f"Available to this process: "
                f"{human_bytes(destination_stats['available'])}"
            )

    # ==================================================================
    # LOGGING
    # ==================================================================

    def append_log(
        self,
        message: str,
    ):

        self.log_view.append(
            message
        )

        append_log(
            message
        )

    # ==================================================================
    # CONTROLS
    # ==================================================================

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

        self.sort_combo.setEnabled(
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

        self.shutdown_on_complete.setEnabled(
            enabled
        )

        self.start_button.setEnabled(
            enabled
        )

        self.find_button.setEnabled(
            enabled
        )

        self.scan_stats_button.setEnabled(
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

    def browse_destination(self):

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Destination Folder",
        )

        if directory:

            self.destination_edit.setText(
                directory
            )

    # ==================================================================
    # PATH VALIDATION
    # ==================================================================

    def get_paths(self):

        source_text = (
            self.source_edit.text().strip()
        )

        destination_text = (
            self.destination_edit.text().strip()
        )

        if not source_text:

            QMessageBox.warning(
                self,
                "Invalid Source",
                "Please provide a source folder.",
            )

            return None, None

        if not destination_text:

            QMessageBox.warning(
                self,
                "Missing Destination",
                "Please select a destination folder.",
            )

            return None, None

        source = Path(
            source_text
        )

        destination = Path(
            destination_text
        )

        if (
            not source.exists()
            or not source.is_dir()
        ):

            QMessageBox.warning(
                self,
                "Invalid Source",
                "Please provide a valid source folder.",
            )

            return None, None

        try:

            source_resolved = (
                source.resolve()
            )

            destination_resolved = (
                destination.resolve()
            )

            if (
                source_resolved
                == destination_resolved
            ):

                QMessageBox.warning(
                    self,
                    "Invalid Paths",
                    "Source and destination cannot match.",
                )

                return None, None

            try:

                destination_resolved.relative_to(
                    source_resolved
                )

                QMessageBox.warning(
                    self,
                    "Invalid Paths",
                    "The destination cannot be "
                    "inside the source folder.",
                )

                return None, None

            except ValueError:

                pass

            destination.mkdir(
                parents=True,
                exist_ok=True,
            )

        except Exception as e:

            QMessageBox.critical(
                self,
                "Destination Error",
                f"Cannot create destination:\n\n{e}",
            )

            return None, None

        return source, destination

    # ==================================================================
    # FIND MISSING
    # ==================================================================

    def find_missing(self):

        if (
            self.worker
            and self.worker.isRunning()
        ):

            QMessageBox.information(
                self,
                "Busy",
                "An operation is currently running.",
            )

            return

        source, destination = (
            self.get_paths()
        )

        if not source:
            return

        self.source = source

        self.destination = (
            destination
        )

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

        self.scan_worker = (
            MissingFilesScanner(
                source,
                destination,
            )
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

        self.missing_files = missing

        self.progress.setValue(
            100
        )

        if missing:

            self.append_log(
                f"Found {len(missing):,} "
                "missing files."
            )

            self.status_label.setText(
                f"Found {len(missing):,} "
                f"missing files out of "
                f"{total:,}."
            )

            self.copy_missing_button.setEnabled(
                True
            )

            self.log_view.append(
                "\n--- Missing Files ---"
            )

            for path in missing[:500]:

                self.log_view.append(
                    str(path)
                )

            if len(missing) > 500:

                self.log_view.append(
                    f"...and "
                    f"{len(missing) - 500:,} "
                    "more files."
                )

        else:

            self.status_label.setText(
                "No missing files found."
            )

            self.append_log(
                "No missing files found."
            )

        if inaccessible:

            self.append_log(
                f"WARNING: "
                f"{inaccessible:,} "
                "file/directory item(s) "
                "could not be inspected."
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

        if (
            not self.missing_files
            or not self.source
            or not self.destination
        ):
            return

        answer = QMessageBox.question(
            self,
            "Copy Missing Files",
            (
                f"Copy "
                f"{len(self.missing_files):,} "
                "missing file(s) to destination?"
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
        )

        if (
            answer
            != QMessageBox.StandardButton.Yes
        ):
            return

        files = []

        for relative in self.missing_files:

            src = (
                self.source
                / relative
            )

            try:

                files.append(
                    (
                        src,
                        src.stat().st_size,
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
    # START TRANSFER
    # ==================================================================

    def start_transfer(self):

        if (
            self.worker
            and self.worker.isRunning()
        ):

            QMessageBox.information(
                self,
                "Busy",
                "An operation is currently running.",
            )

            return

        source, destination = (
            self.get_paths()
        )

        if not source:
            return

        self.source = source

        self.destination = (
            destination
        )

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
            "Scanning source directory..."
        )

        sort_mode = (
            self.sort_combo.currentText()
        )

        self.scan_worker = FileScanner(
            source,
            sort_mode,
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
            self.scan_finished
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

            self.set_controls_enabled(
                True
            )

            QMessageBox.warning(
                self,
                "No Files",
                "No files found in source folder.",
            )

            return

        if inaccessible:

            self.append_log(
                f"WARNING: "
                f"{inaccessible:,} "
                "inaccessible "
                "file/directory item(s) skipped."
            )

        move = (
            self.operation_combo.currentText()
            == "Move"
        )

        self.start_transfer_with_files(
            files,
            move,
        )

    def start_transfer_with_files(
        self,
        files,
        move=False,
        start_index=0,
    ):

        if not files:

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

        skip = (
            self.skip_existing.isChecked()
        )

        ignore_space = (
            self.ignore_space.isChecked()
        )

        verify = (
            self.verify_files.isChecked()
        )

        shutdown = (
            self.shutdown_on_complete.isChecked()
        )

        self.worker = TransferWorker(
            self.source,
            self.destination,
            files,
            move,
            skip,
            ignore_space,
            verify,
            shutdown,
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

        self.resume_button.setEnabled(
            False
        )

        self.copy_missing_button.setEnabled(
            False
        )

        self.append_log(
            "=== "
            f"{'MOVE' if move else 'COPY'} "
            "started ==="
        )

        self.append_log(
            "Transfer order: "
            f"{self.sort_combo.currentText()}"
        )

        if shutdown:

            self.append_log(
                "Shutdown on successful "
                "completion: ENABLED"
            )

        self.worker.start()

    # ==================================================================
    # PAUSE / RESUME
    # ==================================================================

    def pause_operation(self):

        if (
            self.worker
            and self.worker.isRunning()
        ):

            self.pause_button.setEnabled(
                False
            )

            self.status_label.setText(
                "Pause requested. "
                "Completing current file..."
            )

            self.append_log(
                "=== Pause requested ==="
            )

            self.worker.request_pause()

    def check_resume_state(self):

        state = load_state()

        if (
            state
            and state.get(
                "Paused",
                False,
            )
        ):

            self.resume_button.setEnabled(
                True
            )

            self.append_log(
                "Paused job found: "
                f"{state.get('Source')} "
                "→ "
                f"{state.get('Destination')}"
            )

    def resume_operation(self):

        state = load_state()

        if not state:

            QMessageBox.warning(
                self,
                "No State",
                "No paused job state file found.",
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

            verify = bool(
                state.get(
                    "VerifyFiles",
                    True,
                )
            )

            shutdown = bool(
                state.get(
                    "ShutdownOnComplete",
                    False,
                )
            )

            if not self.source.exists():

                QMessageBox.critical(
                    self,
                    "Resume Error",
                    "Original source path "
                    "no longer exists.",
                )

                return

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

            self.verify_files.setChecked(
                verify
            )

            self.shutdown_on_complete.setChecked(
                shutdown
            )

            self.status_label.setText(
                "Rebuilding file list "
                "for resume..."
            )

            self.set_controls_enabled(
                False
            )

            sort_mode = (
                state.get(
                    "SortMode",
                    "Largest → Smallest",
                )
            )

            index = (
                0
                if sort_mode
                == "Largest → Smallest"
                else 1
            )

            self.sort_combo.setCurrentIndex(
                index
            )

            self.scan_worker = FileScanner(
                self.source,
                sort_mode,
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

                if inaccessible:

                    self.append_log(
                        "WARNING: "
                        f"{inaccessible:,} "
                        "items could not be "
                        "inspected during "
                        "resume scan."
                    )

                if start_index >= len(files):

                    QMessageBox.information(
                        self,
                        "Resume",
                        "Operation already completed.",
                    )

                    delete_state()

                    self.set_controls_enabled(
                        True
                    )

                    self.resume_button.setEnabled(
                        False
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
                self.scan_finished
            )

            self.scan_worker.start()

        except Exception as e:

            QMessageBox.critical(
                self,
                "Resume Error",
                str(e),
            )

            self.set_controls_enabled(
                True
            )

    # ==================================================================
    # TRANSFER COMPLETION
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

        shutdown_requested = result.get(
            "shutdown_on_complete",
            False,
        )

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

        if failed:

            self.append_log(
                "=== Operation completed "
                f"with {len(failed):,} "
                "error(s) ==="
            )

            self.log_view.append(
                "\n--- Failed Files ---"
            )

            for filename, error in failed:

                self.log_view.append(
                    f"{filename}: {error}"
                )

        if (
            self.current_operation
            == "move"
            and success
            and not failed
        ):

            self.status_label.setText(
                "Transfer successful. "
                "Removing source files..."
            )

            self.remove_source_after_move(
                result
            )

        else:

            self.finish_transfer_ui(
                copied,
                skipped,
                failed,
                bytes_copied,
                success,
                shutdown_requested,
            )

    def remove_source_after_move(
        self,
        result,
    ):

        successful_files = result.get(
            "successful_source_files",
            [],
        )

        deleted = 0

        delete_errors = []

        for src in successful_files:

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

        try:

            directories = []

            for current_root, dirs, files in os.walk(
                self.source,
                topdown=False,
                followlinks=False,
            ):

                for dirname in dirs:

                    directory = (
                        Path(current_root)
                        / dirname
                    )

                    if directory.is_symlink():
                        continue

                    directories.append(
                        directory
                    )

            directories.append(
                self.source
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

            self.log_view.append(
                "\n--- Source Cleanup Errors ---"
            )

            for filename, error in delete_errors:

                self.log_view.append(
                    f"{filename}: {error}"
                )

            self.finish_transfer_ui(
                result.get("copied", 0),
                result.get("skipped", 0),
                delete_errors,
                result.get("bytes", 0),
                False,
                False,
            )

        else:

            self.append_log(
                "SUCCESS - MOVE completed. "
                f"Deleted {deleted:,} "
                "source files."
            )

            self.finish_transfer_ui(
                result.get("copied", 0),
                result.get("skipped", 0),
                [],
                result.get("bytes", 0),
                True,
                result.get(
                    "shutdown_on_complete",
                    False,
                ),
            )

    def finish_transfer_ui(
        self,
        copied,
        skipped,
        failed,
        bytes_copied,
        success,
        shutdown_on_complete=False,
    ):

        self.progress.setValue(
            100
        )

        self.status_label.setText(
            "Operation completed."
        )

        if failed:

            self.status_label.setText(
                "Completed with errors: "
                f"{len(failed):,} failed."
            )

            QMessageBox.warning(
                self,
                "Completed With Errors",
                (
                    f"Copied: {copied:,}\n"
                    f"Skipped: {skipped:,}\n"
                    f"Failed: "
                    f"{len(failed):,}\n"
                    f"Transferred: "
                    f"{human_bytes(bytes_copied)}"
                ),
            )

        else:

            self.status_label.setText(
                "Operation completed successfully. "
                f"Copied: {copied:,}, "
                f"Skipped: {skipped:,}"
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

            if shutdown_on_complete:

                self.append_log(
                    "=== Successful transfer "
                    "completed. "
                    "Windows shutdown requested. ==="
                )

                self.status_label.setText(
                    "Transfer complete. "
                    "Windows will shut down..."
                )

                QApplication.processEvents()

                try:

                    # Normal Windows shutdown.
                    # Deliberately do NOT use /f.
                    subprocess.Popen(
                        [
                            "shutdown.exe",
                            "/s",
                            "/t",
                            "30",
                            "/c",
                            "Safe Transfer completed successfully.",
                        ],
                        creationflags=(
                            getattr(
                                subprocess,
                                "CREATE_NO_WINDOW",
                                0,
                            )
                        ),
                    )

                except Exception as e:

                    self.append_log(
                        "SHUTDOWN ERROR: "
                        f"{e}"
                    )

                    QMessageBox.critical(
                        self,
                        "Shutdown Error",
                        (
                            "The transfer completed, "
                            "but Windows shutdown "
                            "could not be requested.\n\n"
                            f"{e}"
                        ),
                    )

        self.current_operation = None

    # ==================================================================
    # ERROR HANDLING
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

    def worker_finished(self):

        self.worker = None

        self.set_controls_enabled(
            True
        )

        self.pause_button.setEnabled(
            False
        )

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

    # ==================================================================
    # WINDOW CLOSE
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
                    "A file operation is actively running.\n\n"
                    "Closing now will terminate the application "
                    "while the transfer is still running.\n\n"
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
# APPLICATION ENTRY POINT
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
