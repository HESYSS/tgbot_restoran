"""Process lock held by the OS; automatically released after a crash."""
import hashlib
import os
import tempfile
from pathlib import Path


class SingleInstance:
    def __init__(self, token):
        key = hashlib.sha256(token.encode()).hexdigest()[:24]
        self.path = Path(tempfile.gettempdir()) / f'booking-panel-{key}.lock'
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open('a+b')
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b'0')
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError('Панель уже запущена. Остановите существующий экземпляр перед повторным запуском.') from None
        return self

    def __exit__(self, *args):
        self.handle.close()
