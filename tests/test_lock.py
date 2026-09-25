"""A second process against the same database fails before it serves."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cat_fleet_chat.db import ProcessLock


def test_second_process_fails(tmp_path: Path):
    db_path = tmp_path / "hub.sqlite"
    lock = ProcessLock(str(db_path))
    lock.acquire()
    try:
        root = Path(__file__).resolve().parents[1]
        script = (
            "from cat_fleet_chat.db import ProcessLock\n"
            f"ProcessLock({str(db_path)!r}).acquire()\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode != 0
        assert "already holds" in result.stderr
    finally:
        lock.release()
