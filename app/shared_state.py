import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


class FileMutex:
    def __init__(self, lock_path: Path, timeout_sec: float = 10.0, stale_sec: float = 60.0):
        self._lock_path = lock_path
        self._timeout_sec = timeout_sec
        self._stale_sec = stale_sec
        self._acquired = False

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        while True:
            try:
                fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"{os.getpid()}:{time.time()}")
                self._acquired = True
                return
            except FileExistsError:
                if self._is_stale():
                    try:
                        self._lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue

                if time.monotonic() - started >= self._timeout_sec:
                    raise TimeoutError(f"Timed out waiting for lock: {self._lock_path}")
                time.sleep(0.05)

    def release(self) -> None:
        if not self._acquired:
            return
        self._lock_path.unlink(missing_ok=True)
        self._acquired = False

    def _is_stale(self) -> bool:
        try:
            mtime = self._lock_path.stat().st_mtime
        except FileNotFoundError:
            return False
        return (time.time() - mtime) > self._stale_sec

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


@contextmanager
def locked_json_state(path: Path, default_factory: Callable[[], dict] | None = None):
    default_factory = default_factory or dict
    lock = FileMutex(path.with_suffix(path.suffix + ".lock"))
    with lock:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = default_factory()
        else:
            data = default_factory()

        if not isinstance(data, dict):
            data = default_factory()

        yield data

        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as tmp:
            json.dump(data, tmp, ensure_ascii=True, indent=2)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
