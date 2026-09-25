"""Keeps one pcbnew worker process alive (under KiCad's Python) and talks JSON lines to it."""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
from pathlib import Path

from .kicad import kicad_python

WORKER = Path(__file__).parent / "worker" / "board_worker.py"


class WorkerError(RuntimeError):
    pass


class Worker:
    def __init__(self, timeout: float = 120.0):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.timeout = timeout
        self._id = 0

    def _start(self) -> None:
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [kicad_python(), "-u", str(WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # wx asserts and KiCad chatter
            text=True,
            env=env,
        )

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None

    def call(self, method: str, **params):
        with self.lock:
            if not self._alive():
                self._start()
            assert self.proc and self.proc.stdin and self.proc.stdout
            self._id += 1
            req = {"id": self._id, "method": method, "params": params}
            try:
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self.stop()
                raise WorkerError(f"pcbnew worker died: {e}")
            ready, _, _ = select.select([self.proc.stdout], [], [], self.timeout)
            if not ready:
                self.stop()
                raise WorkerError(f"pcbnew worker timed out after {self.timeout}s on {method}")
            line = self.proc.stdout.readline()
            if not line:
                self.stop()
                raise WorkerError("pcbnew worker exited unexpectedly (is pcbnew importable by KICAD_PYTHON?)")
            resp = json.loads(line)
            if "error" in resp:
                if os.environ.get("KICAD_MCP_DEBUG"):
                    print(resp.get("trace", ""), file=sys.stderr)
                raise WorkerError(resp["error"])
            return resp["result"]


_worker: Worker | None = None


def worker() -> Worker:
    global _worker
    if _worker is None:
        _worker = Worker()
    return _worker
