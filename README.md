# safe-transfer
Safe-transfer is a python script for transferring files more safely.
<br />
I was transferring 900 gigabytes of files and Windows crashed! So I wrote this script to allow for resume-on-crash and a few other features.
<br /><br />
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
