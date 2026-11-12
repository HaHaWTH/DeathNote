"""Run the managed Windows service controls with the selected Python."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def control_service(root: Path, action: str) -> dict:
    if os.name != 'nt':
        raise RuntimeError('Managed service commands require Windows. Run the foreground server on this platform.')
    powershell = (Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
                  / 'WindowsPowerShell/v1.0/powershell.exe')
    control = root / 'scripts/service-control.ps1'
    arguments = [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                 '-File', str(control), '-Action', action, '-Runtime', sys.executable,
                 '-RuntimeImage', sys._base_executable]
    try:
        completed = subprocess.run(arguments, cwd=root, stdin=subprocess.DEVNULL, capture_output=True,
                                   encoding='utf8', creationflags=subprocess.CREATE_NO_WINDOW,
                                   timeout=20 if action == 'status' else 300)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('The managed service command timed out.') from error
    try:
        result = json.loads(completed.stdout)
    except ValueError as error:
        raise RuntimeError('The Windows service controller did not return a valid result.') from error
    if completed.returncode or not result.get('ok'):
        raise RuntimeError('Managed service ' + action + ' failed (' + result.get('code', 'unknown')
                           + '). See .native-server/service.stderr.log for startup failures.')
    return result
