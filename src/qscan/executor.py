"""Serial persisted-job executor; DB state is the only durable queue."""

import sys
import threading
from uuid import UUID

from qscan.application.contracts import NullLock
from qscan.bootstrap import Application
from qscan.domain.models import ErrorCode

__all__ = ["NullLock", "ScanExecutor"]

_MAX_ATTEMPTS = 5
_MAX_BACKOFF_SECONDS = 5.0


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
        attempts: dict[UUID, int] = {}
        while not self._stop.is_set():
            try:
                identity = self.app.repository.next_queued()
            except Exception as exc:
                # Infrastructure hiccup (e.g. SQLITE_BUSY): back off, stay alive.
                self._warn("queue poll failed", exc)
                if self._wakeup.wait(min(self.poll_seconds * 2, _MAX_BACKOFF_SECONDS)):
                    self._wakeup.clear()
                continue
            if identity is None:
                attempts.clear()
                if self._wakeup.wait(self.poll_seconds):
                    self._wakeup.clear()
                continue
            self._current = identity
            try:
                owner = self.app.repository.run_owner(identity)
                scoped = self.app.for_principal(owner or self.app.repository.context.principal)
                scoped.scans.execute_existing(identity, should_stop=self._stop.is_set)
                attempts.pop(identity, None)
            except Exception as exc:
                # Pre-claim errors leave the row QUEUED and would hot-loop; cap
                # consecutive failures per job. Post-claim failures were already
                # persisted FAILED, so the capped fail_queued CAS is a no-op.
                tries = attempts.get(identity, 0) + 1
                attempts[identity] = tries
                self._warn(f"job {identity} failed (attempt {tries})", exc)
                if tries >= _MAX_ATTEMPTS:
                    attempts.pop(identity, None)
                    self.app.repository.fail_queued(
                        identity,
                        ErrorCode.SCAN_FAILED,
                        (f"executor gave up after {tries} attempts: {type(exc).__name__}",),
                    )
                elif self._wakeup.wait(min(self.poll_seconds * 2**tries, _MAX_BACKOFF_SECONDS)):
                    self._wakeup.clear()
            finally:
                self._current = None

    @staticmethod
    def _warn(message: str, exc: Exception) -> None:
        print(
            f"qscan executor: {message}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )

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
