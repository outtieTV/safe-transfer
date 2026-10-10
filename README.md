# Safe Transfer — Windows File Copy / Move Assistant

A Windows file transfer utility built with Python and PyQt6. Safe Transfer provides a graphical interface for copying, moving, and verifying files, with support for Windows Robocopy, sequential task queues, transfer progress, speed and ETA estimates, logging, and configurable shutdown behavior.

Designed for large directory transfers, backups, archives, and moving files between local drives and network storage.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Getting Started](#getting-started)
- [Transfer Operations](#transfer-operations)
- [Task Queue](#task-queue)
- [Robocopy Integration](#robocopy-integration)
- [Transfer Verification and Safety](#transfer-verification-and-safety)
- [Shutdown Options](#shutdown-options)
- [Logs and State Files](#logs-and-state-files)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)
- [License](#license)

---

## Overview

Safe Transfer is designed to simplify file operations that would otherwise require manually managing multiple transfers or working directly with command-line utilities.

The application supports standard Python-based file transfers and Windows Robocopy operations. Users can monitor progress, inspect logs, identify missing files, and queue multiple jobs to run sequentially.

The task queue is especially useful for preparing batches of transfers, such as backing up game installations, archiving downloads, copying documents, or moving project directories to another drive.

## Features

### File Operations

- Copy files and directories.
- Move files and directories.
- Robocopy Copy and Robocopy Move modes.
- Find files missing from the destination.
- Copy missing files.
- Create destination directory structures.
- Sort transfers from largest to smallest or smallest to largest.
- Optionally skip existing files with matching sizes.
- Optional file-size verification.
- Optional destination free-space preflight checks.

### Transfer Monitoring

- Graphical progress bar.
- File counts and transferred data statistics.
- Transfer speed reporting.
- Estimated time remaining (ETA).
- Estimated completion time based on the computer's local time.
- Per-file error handling.
- Background worker threads to keep the interface responsive.

### Task Queue

- Add multiple copy and move jobs.
- List and refresh queued tasks.
- Remove pending tasks.
- Start and stop queue processing.
- Execute tasks sequentially.
- Retain failed tasks and remaining jobs for review and retry.
- Optionally shut down Windows after every queued task succeeds.

### Additional Features

- Pause and resume support for supported Python-based transfers.
- Persistent activity logging.
- Saved pause/resume state.
- Saved file-size scan information.
- Source and destination filesystem statistics.
- Windows-friendly path handling.
- Junction and symbolic-link protections.
- Optional shutdown after a successful standalone transfer.

## Requirements

- Windows
- Python 3.10 or newer
- PyQt6
- Windows `robocopy.exe` for Robocopy operations

`robocopy.exe` is normally included with supported Windows installations.

The optional `humanize` Python package provides additional human-readable formatting. The application includes a fallback formatter if it is unavailable.

## Installation

### Install Python

Install Python 3.10 or newer if necessary.

Verify the installation in Windows Terminal or Command Prompt:

```powershell
py --version
```

### Install dependencies

Install PyQt6:

```powershell
py -m pip install PyQt6
```

Optionally install `humanize`:

```powershell
py -m pip install humanize
```

### Download the application

Save the script as:

```text
safe-transfer-robocopy.pyw
```

The `.pyw` extension allows the application to launch without opening a separate console window when using the normal Windows Python file association.

Run it by double-clicking the file or launching it from Windows Terminal:

```powershell
pyw .\safe-transfer-robocopy.pyw
```

If `pyw` is unavailable, try:

```powershell
py .\safe-transfer-robocopy.pyw
```

## Getting Started

1. Launch Safe Transfer.
2. Select the source directory.
3. Select the destination directory.
4. Choose the transfer operation.
5. Configure the available transfer options.
6. Start the operation, or add it to the task queue.
7. Monitor progress and review the log for errors.

Before using the application with important files, perform a test transfer using disposable directories.

### Path selection

- Confirm that the source directory exists.
- Ensure the destination has sufficient free space.
- Do not select the same directory as both source and destination.
- Avoid placing the destination inside the source directory.
- Ensure external drives and network shares are connected before starting.
- Verify that the Windows account running the application has the necessary permissions.

---

## Transfer Operations

### Copy

Copies files from the source directory to the destination without intentionally deleting the originals.

**Recommended for:** backups, duplicate copies, and transfers where the original data must remain intact.

### Move

Moves files from the source to the destination using the application's Python-based transfer process.

**Recommended for:** relocating files when they no longer need to remain in the original location.

Because a move operation can remove source files, confirm the paths before starting and maintain an independent backup of important data.

### Robocopy Copy

Uses Windows Robocopy to copy the source directory tree to the destination.

Robocopy is particularly useful for large directory structures and Windows file transfers.

### Robocopy Move

Uses a copy-first process designed to avoid deleting source files until the destination has passed the application's verification checks.

The process is:

1. Run Robocopy to copy the directory tree.
2. Check that each scanned source file exists at the destination.
3. Compare source and destination file sizes.
4. If all verification checks pass, delete the scanned source files.
5. Attempt to remove empty source directories.

If copying fails or verification fails, the application does not proceed with the source-file deletion phase.

**Important:** File-size verification does not guarantee that file contents are identical. Files with different contents can have the same size. For irreplaceable data, use an independent backup and consider hash-based verification.

### Find Missing Files

Scans for files that exist in the source but are missing from the destination.

This can help identify gaps in an existing backup or copy.

### Copy Missing Files

Copies files identified as missing from the destination.

Review the selected paths and operation options before proceeding.

---

## Task Queue

The task queue lets you configure multiple transfer jobs and execute them one at a time.

Each queued job stores its source directory, destination directory, operation, sorting preference, and applicable transfer options.

### Task Queue Add

1. Select the source directory.
2. Select the destination directory.
3. Choose the desired operation.
4. Configure the available options.
5. Click **Task Queue Add**.

Repeat these steps to prepare additional jobs.

### Task Queue List / Refresh

Click **Task Queue List / Refresh** to refresh the displayed task list.

The list shows the queued jobs, their status, operation, and source-to-destination paths.

### Task Queue Cancel / Remove

Select a pending task and click **Task Queue Cancel / Remove** to remove it from the queue.

The active task cannot be removed while its transfer is running. Stop the queue to prevent additional jobs from starting, then allow the active task to finish.

### Task Queue Start

Click **Task Queue Start** to begin processing pending jobs.

The application executes tasks sequentially, in queue order.

After a task succeeds, it is removed from the pending queue and the next task begins.

If a task fails:

- Queue processing stops.
- The failed task remains queued.
- Remaining tasks stay queued.
- You can inspect the error, correct the problem, and start the queue again.

### Task Queue Stop

Click **Task Queue Stop** to stop scheduling additional jobs.

The active transfer is allowed to finish. This is not an immediate cancellation of the current file operation.

Remaining tasks stay queued and can be resumed by clicking **Task Queue Start**.

### Example queue

| Order | Operation | Source | Destination |
|---|---|---|---|
| 1 | Robocopy Copy | `D:\Games` | `E:\Backup\Games` |
| 2 | Robocopy Move | `D:\Downloads` | `E:\Archive\Downloads` |
| 3 | Copy | `C:\Users\YourName\Documents` | `E:\Backup\Documents` |
| 4 | Copy | `D:\Projects` | `E:\Backup\Projects` |

Add each job, review the queue, and click **Task Queue Start**. The application processes each job before starting the next one.

### Queue persistence

The task queue is stored in memory and is not saved as a persistent job list.

Closing the application or restarting Windows loses pending queue entries. Record your planned jobs separately if you need to recreate them later.

---

## Robocopy Integration

Robocopy is a Windows command-line utility designed for copying files and directory trees.

Safe Transfer invokes Robocopy with the following options:

| Option | Purpose |
|---|---|
| `/E` | Copy subdirectories, including empty directories. |
| `/COPY:DAT` | Copy file data, attributes, and timestamps. |
| `/DCOPY:DAT` | Copy directory data, attributes, and timestamps. |
| `/R:2` | Retry failed copies twice. |
| `/W:2` | Wait two seconds between retries. |
| `/XJ` | Exclude junction points. |
| `/NP` | Suppress per-file percentage progress in Robocopy output. |

### Robocopy exit codes

Robocopy uses exit codes differently from many other Windows programs.

Safe Transfer treats exit codes from `0` through `7` as non-failure outcomes and exit codes of `8` or higher as failures.

### Robocopy limitations

- The application's pause functionality cannot safely pause a running Robocopy operation.
- Robocopy operates on the selected directory tree, not only on individual files identified by the application's scanner.
- Robocopy Move verifies the scanned files using existence and file-size checks before deleting them.
- Network permissions, file locks, and storage failures can still cause errors.

See Microsoft's [Robocopy documentation](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/robocopy) for additional information.

---

## Transfer Verification and Safety

### Skip existing files

When enabled, the application can skip existing files with matching sizes.

This can reduce unnecessary copying, but matching file sizes do not prove that the files have identical contents.

### File verification

Use the verification option when you want additional checks on transferred files supported by the selected operation.

Verification behavior differs between Python-based transfers and Robocopy operations.

### Free-space preflight

The optional free-space check helps identify insufficient destination capacity before a transfer starts.

Available space can change during a transfer, especially when multiple users or applications are writing to the same storage.

### Move operations

Move operations can delete source files. Always verify both paths and maintain an independent backup of important data.

### Pause and resume

Pause/resume is intended for supported Python-based transfers. Robocopy operations cannot be paused using the application's pause button.

Saved pause/resume state is separate from the task queue and does not preserve the full queue.

---

## Shutdown Options

Safe Transfer provides two shutdown options with different purposes.

### Shutdown after current task finishes

Requests Windows shutdown after a successful standalone transfer.

The application requests a normal shutdown with a 30-second countdown. It does not use the forced-shutdown option.

This option is intended for a single transfer rather than a complete task queue.

### Shutdown after all tasks finish successfully

Located in the Task Queue section.

When enabled before starting the queue, the application requests Windows shutdown only after every queued task has completed successfully.

Expected behavior:

| Situation | Shutdown requested? |
|---|---|
| Standalone transfer succeeds with its shutdown option enabled | Yes |
| Every queued task succeeds and queue-wide shutdown is enabled | Yes |
| A queued task fails | No |
| The queue is stopped before all tasks complete | No |
| Queue-wide shutdown is disabled | No |

The queue-wide shutdown setting is captured when the queue starts. Changing the checkbox during a running batch does not change that batch's setting.

Queued jobs suppress individual shutdown requests to prevent Windows from shutting down before the remaining jobs run.

**Save your work before enabling shutdown.** A normal Windows shutdown can still close other applications, and unsaved work may be lost.

---

## Logs and State Files

Safe Transfer uses files to record transfer activity and support certain operations.

| File | Purpose |
|---|---|
| `%USERPROFILE%\copy_move_history.txt` | Persistent transfer log and activity history. |
| `.pauseResumeState.json` | Saved state for supported pause/resume operations. |
| `transfer_file_sizes.txt` | Saved file-size scan information. |

The pause/resume and scan files are stored alongside the script.

The task queue is not saved in these files.

Logs are useful for diagnosing failed transfers, reviewing Robocopy output, and determining where an operation encountered an error.

---

## Troubleshooting

### PyQt6 is missing

Install PyQt6:

```powershell
py -m pip install PyQt6
```

### Robocopy is unavailable

Confirm that `robocopy.exe` is available on the system. It is normally included with supported Windows installations.

### A transfer fails

Review the application log and check:

- Source and destination paths.
- Available disk space.
- File and directory permissions.
- Whether the source or destination is disconnected.
- Whether another application is locking the files.

### A queued task fails

The queue stops and retains the failed task and remaining jobs.

Review the error, correct the underlying problem, and click **Task Queue Start** to retry.

### The queue disappears after restarting the application

This is expected. The queue is currently held in memory and is not persisted between application sessions.

### Windows does not shut down after the queue finishes

Check that **Shutdown after all tasks finish successfully** was enabled before starting the queue and that all queued tasks completed successfully.

The application does not request queue-wide shutdown when a task fails or the queue is stopped.

### Robocopy cannot be paused

This is expected. The application's pause/resume controls do not support pausing Robocopy operations.

---

## Known Limitations

- Designed primarily for Windows.
- Robocopy features require Windows and `robocopy.exe`.
- The task queue does not persist across application restarts.
- Stopping the queue does not forcibly interrupt an active transfer.
- A failed task stops queue processing until the user reviews and restarts it.
- Robocopy operations cannot be paused through the GUI.
- Robocopy Move uses existence and size checks rather than cryptographic hashes.
- Shutdown is requested only under the applicable successful-completion conditions.
- Network shares, permissions, file locks, and storage failures may affect results.

Test new operations with disposable data before relying on the application for important backups or file migrations.

---

## License

This project is licensed under the **GNU General Public License v3.0 (GPL-3.0-only)**.

You may use, study, modify, and redistribute this software under the terms of the GNU GPL version 3.

A copy of the full license should be included in the repository as a file named `LICENSE`.

The official license text is available from the [GNU Project — GNU General Public License, version 3](https://www.gnu.org/licenses/gpl-3.0.html).

Copyright and license notices in the source code should be preserved when redistributing the project.
