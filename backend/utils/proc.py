"""Subprocess execution for ffmpeg/ffprobe.

Two things this exists to guarantee:

* **stdin is closed.** Given a terminal, ffmpeg will read from it, and a
  malformed file can leave the process waiting on a keypress forever. `-nostdin`
  handles this for *ffmpeg* — but ffprobe rejects that flag outright
  ("Failed to set value '-v' for option 'nostdin': Option not found"), so the
  portable fix is stdin=DEVNULL, applied to every call here.

* **Children are reaped.** Killing on timeout without awaiting the exit status
  leaves zombies, and under supervisord the app is effectively PID 1's child —
  they accumulate for the life of the container.
"""
import asyncio
import contextlib

from backend.common.logging_config import get_logger

logger = get_logger(__name__)


class ProcTimeout(Exception):
    """The process exceeded its deadline and was killed."""


async def run_proc(argv: list[str], timeout: float = 60.0) -> tuple[int, bytes, bytes]:
    """Run a command, returning (returncode, stdout, stderr).

    Raises ProcTimeout on deadline. Propagates CancelledError after killing the
    child, so a cancelled scan doesn't leave ffmpeg running.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out, err
    except asyncio.TimeoutError:
        await _kill(proc)
        raise ProcTimeout(f"{argv[0]} exceeded {timeout}s") from None
    except asyncio.CancelledError:
        await _kill(proc)
        raise


async def _kill(proc) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    # Must await: this is what actually reaps the child.
    with contextlib.suppress(Exception):
        await proc.wait()
