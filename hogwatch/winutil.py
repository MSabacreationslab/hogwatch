"""Small Windows helpers shared by the launcher and the dashboard server."""

from __future__ import annotations

import ctypes
import subprocess
import urllib.request
import webbrowser


def is_admin() -> bool:
    """True when running elevated (needed for the per-program breakdown)."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def open_dashboard(url: str) -> None:
    """Open the dashboard in the default browser, never as administrator.

    HogWatch itself runs elevated, and anything it launches directly inherits that.
    Handing the URL to explorer.exe opens it at the user's normal level instead.
    """
    if is_admin():
        subprocess.Popen(["explorer.exe", url])
    else:
        webbrowser.open(url)


def server_up(port: int, timeout: float = 1.0) -> bool:
    """True if a HogWatch dashboard is already answering on this port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/now", timeout=timeout) as r:
            return r.status == 200
    except OSError:
        return False


def run_elevated(exe: str, params: str, cwd: str) -> bool:
    """Start a program as administrator (shows the Windows prompt). False if the user said no."""
    # ShellExecute returns a value > 32 on success; SW_HIDE (0) keeps a console from flashing.
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, cwd, 0) > 32
