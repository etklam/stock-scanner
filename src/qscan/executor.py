"""Serial persisted-job executor; DB state is the only durable queue."""

import threading
from uuid import UUID

from qscan.application.contracts import NullLock
from qscan.bootstrap import Application

__all__ = ["NullLock", "ScanExecutor"]


class ScanExecutor:
    """One worker thread claiming QUEUED runs FIFO until cooperatively stopped."""

    def __init__(self, app: Application, *, poll_seconds: float = 0.2, stop_grace: float = 30.0):
        self.app = app
        self.poll_seconds = poll_seconds
        self.stop_grace = stop_grace
        self.recovered = 0
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: UUID | None = None

    @property
    def busy(self) -> bool:
        return self._current is not None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> int:
        """Run startup recovery and start the worker; ownership lock must be held."""
        self.recovered = self.app.repository.recover_interrupted()
        self._thread = threading.Thread(target=self._loop, name="qscan-executor")
        self._thread.start()
        return self.recovered

    def poke(self) -> None:
        self._wakeup.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            identity = self.app.repository.next_queued()
            if identity is None:
                if self._wakeup.wait(self.poll_seconds):
                    self._wakeup.clear()
                continue
            self._current = identity
            try:
                owner = self.app.repository.run_owner(identity)
                scoped = self.app.for_principal(owner or self.app.repository.context.principal)
                scoped.scans.execute_existing(identity, should_stop=self._stop.is_set)
            except Exception:
                # execute_existing already persisted the failure; the queue keeps serving.
                pass
            finally:
                self._current = None

    def stop(self) -> bool:
        """Cooperative stop. True means the thread finished; only then release
        the ownership lock or dispose the engine. A stuck worker is left to
        process exit and the next startup recovery."""
        self._stop.set()
        self._wakeup.set()
        if self._thread is None:
            return True
        self._thread.join(self.stop_grace)
        return not self._thread.is_alive()
