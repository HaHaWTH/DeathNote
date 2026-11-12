"""Process locks released by the OS even when a download is forcibly stopped."""
from contextlib import contextmanager
import os


@contextmanager
def workspace_lock(root, filename='write.lock'):
    directory = root / '.native-server'
    directory.mkdir(exist_ok=True)
    stream = (directory / filename).open('a+b')
    if stream.tell() == 0:
        stream.write(b'\0')
        stream.flush()
    stream.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        stream.close()
        raise RuntimeError('Another DeathNote process is using ' + filename + '.') from error
    try:
        yield
    finally:
        stream.close()
