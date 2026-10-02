"""Worker stand-ins for SlideValidator(target=...). Importable under 'spawn'."""

import os
import signal
import time

from surf_transfer.models import VERIFIED, CheckResult, JobResult


def ok(job, conn, progress):
    conn.send(
        JobResult(
            slide_key=job.slide_key,
            kind=job.kind,
            status=VERIFIED,
            checks=[CheckResult("stub", True)],
        )
    )
    conn.close()


def segfault(job, conn, progress):
    progress[0], progress[1], progress[2] = 0, 1024, 2048
    os.kill(os.getpid(), signal.SIGSEGV)


def hang(job, conn, progress):
    progress[0], progress[1], progress[2] = 1, 10, 20
    time.sleep(120)


def exits_silently(job, conn, progress):
    os._exit(3)


def python_error(job, conn, progress):
    try:
        raise RuntimeError("boom in validator")
    except RuntimeError:
        import traceback

        conn.send(("error", traceback.format_exc()))
        conn.close()
