"""ProcTap worker entry point — spawned as a reparented subprocess.

This script is launched by SubprocessProcTap via CreateProcessW with
PROC_THREAD_ATTRIBUTE_PARENT_PROCESS set to os.getppid(), so this process
becomes a sibling of the Qt UI process rather than its child. That allows
Windows WASAPI INCLUDE_TARGET_PROCESS_TREE to capture audio from the Qt UI.
"""

from __future__ import annotations

import argparse
import multiprocessing.connection
import pathlib
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="ProcTap worker")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--system-loopback", action="store_true")
    args = parser.parse_args()

    project_root = str(pathlib.Path(__file__).resolve().parent.parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Dial back to the launcher BEFORE any heavy import. The launcher only
    # waits a bounded time for this connection; the sentry + audio import
    # chain can exceed 10s on a loaded machine (measured 13.8s, 2026-07-01),
    # and running it first made every launch time out — the hub then
    # broadcast silence to all spokes while retrying forever.
    conn = multiprocessing.connection.Client(("127.0.0.1", args.port))

    from audio_core.capture._proctap_subprocess import (
        _start_sentry_init_background,
        _worker_main,
    )

    # Crash reporting initializes on a daemon side thread — NEVER
    # synchronously on the readiness path. A wedged local Sentry stack made a
    # synchronous init_sentry block 22.4s here (2026-07-02), blowing the
    # launcher's 15s ready window on every attempt: capture never started and
    # spokes got silence-flagged frames forever. Same disease as the 2026-07-01
    # import-before-dial-back bug, one stage later.
    _start_sentry_init_background("audio_core.capture._proctap_worker_entry")

    _worker_main(conn, args.pid, args.system_loopback)


if __name__ == "__main__":
    main()
