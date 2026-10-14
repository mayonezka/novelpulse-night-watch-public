"""An OS-owned polling lock; process crashes release it without stale lock files."""
import os
from contextlib import contextmanager
from pathlib import Path

@contextmanager
def bot_instance(*, record_pid=True):
    directory = Path(__file__).resolve().parents[1] / 'data'
    directory.mkdir(exist_ok=True)
    stream = (directory / '.polling.lock').open('a+b')
    acquired = False
    try:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        if record_pid:
            (directory / 'polling.pid').write_text(str(os.getpid()), encoding='ascii')
        yield
    except OSError as error:
        if not acquired:
            raise RuntimeError('Бот уже запущен: второй polling-процесс запрещён') from error
        raise
    finally:
        if acquired:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def instance_running():
    """Probe the kernel lock, never trust a PID left behind after a crash."""
    try:
        with bot_instance(record_pid=False):
            return False
    except RuntimeError:
        return True
