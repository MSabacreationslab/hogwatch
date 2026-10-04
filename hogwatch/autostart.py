"""Start HogWatch automatically at login, via a Windows scheduled task.

The task runs with highest privileges, so there's no admin prompt at login and
per-program tracking works. Creating or removing it needs admin itself, which
HogWatch has when it was started the normal way.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config import FROZEN, ROOT

TASK_NAME = "HogWatch"


class AutostartError(Exception):
    """The task couldn't be created or removed (usually: not running as administrator)."""


def _ps(script: str) -> subprocess.CompletedProcess:
    """Run a PowerShell snippet without flashing a window."""
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _q(value: str) -> str:
    """Quote a value for a PowerShell single-quoted string."""
    return "'" + value.replace("'", "''") + "'"


def command() -> tuple[str, str, str]:
    """(program, arguments, working folder) that starts HogWatch in the background."""
    if FROZEN:
        exe = Path(sys.executable)
        return str(exe), "run --no-browser", str(exe.parent)
    pyw = Path(sys.executable).with_name("pythonw.exe")  # no console window
    return str(pyw if pyw.exists() else sys.executable), "-m hogwatch run --no-browser", str(ROOT)


def installed() -> bool:
    """True if the login task exists."""
    r = _ps(f"if (Get-ScheduledTask -TaskName {_q(TASK_NAME)} -ErrorAction SilentlyContinue) {{ 'yes' }}")
    return "yes" in r.stdout


def install_script() -> str:
    """The PowerShell that registers the task (separate so it can be inspected and tested)."""
    exe, args, cwd = command()
    return (
        "$ErrorActionPreference = 'Stop'; "
        "$user = \"$env:USERDOMAIN\\$env:USERNAME\"; "
        f"$action = New-ScheduledTaskAction -Execute {_q(exe)} -Argument {_q(args)} -WorkingDirectory {_q(cwd)}; "
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user; "
        "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest; "
        # No time limit, keep running on battery, restart if it ever crashes.
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) "
        "-MultipleInstances IgnoreNew; "
        f"Register-ScheduledTask -TaskName {_q(TASK_NAME)} -Action $action -Trigger $trigger -Principal $principal "
        "-Settings $settings -Description 'HogWatch home network monitor' -Force | Out-Null"
    )


def install() -> None:
    """Create (or replace) the login task."""
    r = _ps(install_script())
    if r.returncode != 0:
        raise AutostartError("Windows wouldn't create the startup task. HogWatch needs to be running as "
                             "administrator for this: close it, start it again, and click Yes on the Windows prompt.")


def uninstall() -> None:
    """Remove the login task (history is kept)."""
    r = _ps(f"$ErrorActionPreference = 'Stop'; if (Get-ScheduledTask -TaskName {_q(TASK_NAME)} -ErrorAction "
            f"SilentlyContinue) {{ Unregister-ScheduledTask -TaskName {_q(TASK_NAME)} -Confirm:$false }}")
    if r.returncode != 0:
        raise AutostartError("Windows wouldn't remove the startup task. HogWatch needs to be running as administrator.")
