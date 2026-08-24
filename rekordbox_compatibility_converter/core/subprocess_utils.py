"""Safe external-process launching for GUI and worker-thread code."""

import os
import shutil
import subprocess
import sys
from typing import Any, Sequence


def run_external(command: Sequence[Any], **kwargs: Any) -> subprocess.CompletedProcess:
    """Run an external tool without a post-CoreFoundation fork on macOS.

    Python 3.9-3.11 only selects its ``posix_spawn`` path when the executable
    has a directory component and ``close_fds`` is false. File descriptors
    created by Python are non-inheritable by default, so this is safe for the
    FFmpeg and macOS utility calls made by this application.
    """
    resolved_command = list(command)
    if sys.platform == "darwin" and resolved_command:
        executable = os.fspath(resolved_command[0])
        if not os.path.dirname(executable):
            executable = shutil.which(executable) or executable
            resolved_command[0] = executable

        spawn_eligible = (
            bool(os.path.dirname(executable))
            and kwargs.get("cwd") is None
            and kwargs.get("preexec_fn") is None
            and not kwargs.get("pass_fds")
            and not kwargs.get("start_new_session", False)
        )
        if spawn_eligible:
            kwargs.setdefault("close_fds", False)

    return subprocess.run(resolved_command, **kwargs)
