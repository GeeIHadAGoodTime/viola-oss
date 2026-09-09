/*
 * viola_launcher.c — tiny native launch shim for the Viola desktop app.
 *
 * WHY THIS EXISTS
 * ---------------
 * Velopack runs its install/update/uninstall lifecycle hooks by launching the
 * package's `--mainExe` with a `--veloapp-*` argument and waiting a HARDCODED
 * 30 seconds for that process to exit (vpk: run_hook(..., 30) — not configurable
 * at any vpk version). Our real desktop app is an ~82 MB PyInstaller onedir exe
 * plus a large Python runtime. On a cold, slow, freshly-installed machine the OS
 * + Windows Defender must memory-map and real-time-scan that whole image on first
 * access, which can take longer than 30 s just to reach the app's earliest code.
 * When the 30 s timer fires, Velopack TerminateProcess-kills the hook process —
 * but it is wedged mid-DLL-load under the AV scan and cannot be terminated
 * ("Access is denied"). Velopack's post-hook force_stop_package then blocks on
 * that un-killable process and Setup.exe hangs indefinitely. Result: the install
 * never completes on a clean machine.
 *
 * THE FIX
 * -------
 * Make the package's `--mainExe` this tiny launcher instead of the heavy frozen
 * app. It links only against always-resident system DLLs (kernel32/user32/
 * shell32), so it is never AV-wedged and exits in milliseconds.
 *
 *   - On ANY `--veloapp-*` argument: exit(0) immediately. We register no Velopack
 *     lifecycle callbacks (velopack.App().run() is a no-op for us), so doing
 *     nothing and exiting instantly is entirely correct and safe. Velopack sees a
 *     fast, clean exit, never times out, never kills, never hangs.
 *
 *   - On a normal launch (shortcut double-click, first-run, self-update relaunch):
 *     start the real frozen app (ViolaApp.exe, sitting next to this launcher),
 *     forwarding the full command line, the inherited environment, and the
 *     inherited working directory, then EXIT IMMEDIATELY with 0 (Plan B).
 *
 *     We deliberately do NOT WaitForSingleObject on the child. On a normal
 *     first-run launch the frozen app calls velopack.App().run() (viola_qt.py) as
 *     its REQUIRED startup call, and Velopack's documented behavior is that run()
 *     "may terminate or restart the process" — it RESTARTS the app into a fresh
 *     detached ViolaApp.exe. So the launcher's direct child exits almost at once
 *     while the Velopack-restarted detached ViolaApp.exe becomes the real,
 *     persistent app (it serves /health and owns the top-level window). Waiting
 *     on the direct child was therefore meaningless: the launcher's child exits 0
 *     the instant Velopack detaches, the launcher returned 0, and the gate that
 *     tracked the launcher saw "launched process exited early" even though the app
 *     was up. Plan B makes that explicit: the launcher spawns ViolaApp.exe and
 *     returns 0 immediately; ViolaApp.exe (+ Velopack's restart of it) owns the
 *     process lifetime. A normal CreateProcessW child (no job object, not
 *     CREATE_SUSPENDED, handles closed) outlives its parent, so the app survives
 *     the launcher's exit.
 *
 * WHY NOT A KILL-ON-JOB-CLOSE JOB OBJECT (the abandoned orphan-prevention idea)
 * ---------------------------------------------------------------------------
 * A sibling branch (43123463, 2026-07-02, never merged) tried to stop a
 * force-killed launcher from orphaning ViolaApp.exe (which then keeps the
 * single-instance lock in core/single_instance.py and blocks the next launch) by
 * putting the child in a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and
 * AssignProcessToJobObject, so killing the launcher tears the app down too. That
 * is fundamentally incompatible with Plan B and must NOT be re-applied here:
 *   1. KILL_ON_JOB_CLOSE terminates the job the instant the launcher's LAST job
 *      handle closes. Under Plan B the launcher exits immediately, so the job
 *      would kill ViolaApp.exe moments after launch. "Job object without the
 *      infinite wait" therefore does NOT work — it kills the app.
 *   2. velopack.App().run() restarts the app into a fresh DETACHED ViolaApp.exe
 *      (a different pid, not this launcher's direct child), which a
 *      launcher-owned job cannot govern anyway.
 *   3. Keeping the launcher alive to hold the job open is exactly the blocking
 *      WaitForSingleObject(INFINITE) that Plan B (and the clean-VM gate's
 *      "launcher exits within a short bound" leg) removed.
 * The real orphaned-lock fix lives in the app's single-instance guard
 * (core/single_instance.py: verify the lock holder is a live, window-bearing
 * instance and reclaim a zombie lock), not in this transient launch shim. This
 * is locked by scripts/check_velopack_launcher_hook_target.py Leg 6.
 *
 * This is bundle-size-independent and permanent: no matter how large the frozen
 * app grows, the Velopack hook target stays tiny.
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <wchar.h>

/*
 * The real frozen application, shipped next to this launcher in the same dir.
 * The name is supplied at compile time (-DVIOLA_FROZEN_APP_NAME=L"...") by
 * scripts/build_launcher.ps1 so it can never silently drift from the PyInstaller
 * EXE(name=...) in viola.spec. A default keeps a bare `zig cc` build working.
 */
#ifndef VIOLA_FROZEN_APP_NAME
#define VIOLA_FROZEN_APP_NAME L"ViolaApp.exe"
#endif
static const wchar_t *FROZEN_APP_NAME = VIOLA_FROZEN_APP_NAME;
static const wchar_t *VELOAPP_PREFIX = L"--veloapp-";

/*
 * Return non-zero if any argument after argv[0] begins with "--veloapp-".
 * These are Velopack's lifecycle hook invocations; we must do nothing and exit.
 */
static int is_veloapp_invocation(void)
{
    int argc = 0;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (argv == NULL) {
        return 0;
    }
    int found = 0;
    size_t prefix_len = wcslen(VELOAPP_PREFIX);
    for (int i = 1; i < argc; i++) {
        if (wcsncmp(argv[i], VELOAPP_PREFIX, prefix_len) == 0) {
            found = 1;
            break;
        }
    }
    LocalFree(argv);
    return found;
}

/*
 * Return a pointer INTO the process command-line buffer positioned just after
 * the first token (argv[0]) and any following whitespace — i.e. the tail of
 * arguments to forward verbatim to the child. Do not free the returned pointer;
 * it aliases the buffer owned by GetCommandLineW().
 */
static LPWSTR command_line_tail(void)
{
    LPWSTR p = GetCommandLineW();
    while (*p == L' ' || *p == L'\t') {
        p++;
    }
    if (*p == L'"') {
        /* Quoted program path: skip to the closing quote. */
        p++;
        while (*p != L'\0' && *p != L'"') {
            p++;
        }
        if (*p == L'"') {
            p++;
        }
    } else {
        while (*p != L'\0' && *p != L' ' && *p != L'\t') {
            p++;
        }
    }
    while (*p == L' ' || *p == L'\t') {
        p++;
    }
    return p;
}

/*
 * Resolve the absolute path of the frozen app that lives beside this launcher.
 * Writes into out (out_cap wide chars). Returns non-zero on success.
 */
static int resolve_frozen_app_path(wchar_t *out, size_t out_cap)
{
    wchar_t self[4096];
    DWORD n = GetModuleFileNameW(NULL, self, (DWORD)(sizeof(self) / sizeof(self[0])));
    if (n == 0 || n >= (sizeof(self) / sizeof(self[0]))) {
        return 0;
    }
    wchar_t *slash = wcsrchr(self, L'\\');
    if (slash != NULL) {
        *(slash + 1) = L'\0';
    } else {
        self[0] = L'\0';
    }
    /* self now holds the directory (with trailing backslash) or empty. */
    if (wcslen(self) + wcslen(FROZEN_APP_NAME) + 1 >= out_cap) {
        return 0;
    }
    wcscpy(out, self);
    wcscat(out, FROZEN_APP_NAME);
    return 1;
}

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPWSTR lpCmdLine, int nCmdShow)
{
    (void)hInstance;
    (void)hPrevInstance;
    (void)lpCmdLine;
    (void)nCmdShow;

    /*
     * Velopack lifecycle hook: do nothing, exit instantly. This is the whole
     * point of the launcher — a fast, un-wedgeable hook target.
     */
    if (is_veloapp_invocation()) {
        return 0;
    }

    wchar_t frozen_path[4096];
    if (!resolve_frozen_app_path(frozen_path, sizeof(frozen_path) / sizeof(frozen_path[0]))) {
        MessageBoxW(NULL, L"Viola could not locate its application component (ViolaApp.exe).",
                    L"Viola Launcher", MB_OK | MB_ICONERROR);
        return 1;
    }

    /*
     * Build the child command line: "<frozen_path>" <forwarded args>.
     * The first token becomes the child's argv[0]; the rest is the verbatim
     * tail of our own command line so every user/OS-supplied argument (including
     * the Velopack self-update relaunch args) is passed through untouched.
     */
    LPWSTR tail = command_line_tail();
    size_t cmd_cap = wcslen(frozen_path) + wcslen(tail) + 8;
    LPWSTR cmd = (LPWSTR)HeapAlloc(GetProcessHeap(), 0, cmd_cap * sizeof(wchar_t));
    if (cmd == NULL) {
        return 1;
    }
    cmd[0] = L'"';
    cmd[1] = L'\0';
    wcscat(cmd, frozen_path);
    wcscat(cmd, L"\"");
    if (tail[0] != L'\0') {
        wcscat(cmd, L" ");
        wcscat(cmd, tail);
    }

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    /*
     * lpApplicationName = frozen_path (exact binary, no PATH search).
     * lpEnvironment = NULL  -> child inherits our environment (firstrun env from
     *                          Velopack start_package flows through).
     * lpCurrentDirectory = NULL -> child inherits our working directory.
     * bInheritHandles = FALSE -> GUI app, nothing to inherit.
     * dwCreationFlags = 0 -> NO job object, NOT CREATE_SUSPENDED. A plain child
     *                        is independent of this parent and keeps running after
     *                        the launcher exits (Plan B relies on this).
     */
    BOOL ok = CreateProcessW(
        frozen_path,
        cmd,
        NULL,
        NULL,
        FALSE,
        0,
        NULL,
        NULL,
        &si,
        &pi);

    HeapFree(GetProcessHeap(), 0, cmd);

    if (!ok) {
        MessageBoxW(NULL, L"Viola failed to start its application component.",
                    L"Viola Launcher", MB_OK | MB_ICONERROR);
        return 1;
    }

    /*
     * Plan B: the frozen app is launched; exit immediately with 0. We do NOT wait
     * on the child. Closing both handles only drops OUR references — the child
     * process keeps running independently and, once Velopack's App().run()
     * restarts it, the detached ViolaApp.exe is the real persistent app. See the
     * file header for the full rationale.
     */
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
