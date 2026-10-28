"""
Send everything the app prints to a size-rotated log file.

stdout/stderr are replaced with line-buffered tees: each complete line goes to
the log (stderr lines at ERROR, so uncaught thread tracebacks are kept too) and
still reaches the console when there is one. The windowless build has no
console (sys.stdout is None), so there it is log file only.
"""
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

LOG_NAME = 'isp_switcher.log'


class _Tee:
    def __init__(self, logger, level, stream):
        self._logger = logger
        self._level = level
        self._stream = stream
        self._buf = ''
        self._lock = threading.Lock()

    def write(self, text):
        if self._stream is not None:
            try:
                self._stream.write(text)
            except Exception:
                pass
        with self._lock:
            self._buf += text
            *lines, self._buf = self._buf.split('\n')
        for line in lines:
            if line.strip():
                self._logger.log(self._level, line.rstrip())
        return len(text)

    def flush(self):
        if self._stream is not None:
            try:
                self._stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def setup(log_dir, max_bytes, backups):
    """Returns the log file path."""
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, LOG_NAME)

    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups,
                                  encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-5s %(message)s'))
    logger = logging.getLogger('isp_switcher')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers[:] = [handler]

    for name, level in (('stdout', logging.INFO), ('stderr', logging.ERROR)):
        stream = getattr(sys, name)
        if stream is not None:
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass
        setattr(sys, name, _Tee(logger, level, stream))
    return path
