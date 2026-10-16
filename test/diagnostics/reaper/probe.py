"""Run inside a disposable container to check PID 1's orphan reaping."""

import os
import signal
import sys
import time
from pathlib import Path


def state(pid: int) -> str | None:
    """Read process state without spawning another process."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return None


def wait_for(predicate, description: str) -> None:
    """Allow asynchronous reaping, with a bounded deadline."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise RuntimeError(f"Timed out: {description}")


def orphan() -> int:
    """Create a child that reports adoption by PID 1 before exiting."""
    reader, writer = os.pipe()
    parent = os.fork()
    if parent == 0:
        os.close(reader)
        child = os.fork()
        if child != 0:
            os._exit(0)
        deadline = time.monotonic() + 5
        while os.getppid() != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        os.write(writer, f"{os.getpid()} {os.getppid()}".encode())
        os.close(writer)
        os._exit(0)
    os.close(writer)
    os.waitpid(parent, 0)
    with os.fdopen(reader) as stream:
        pid, ppid = map(int, stream.read().split())
    if ppid != 1:
        raise RuntimeError(f"Child {pid} was adopted by {ppid}, not PID 1")
    return pid


def sleep_pid() -> int | None:
    """Find the loop's sleep child, excluding adopted test processes."""
    children = Path("/proc/1/task/1/children").read_text().split()
    for child in children:
        try:
            if Path(f"/proc/{child}/comm").read_text().strip() == "sleep":
                return int(child)
        except FileNotFoundError:
            continue
    return None


def batches(baseline: bool) -> None:
    """Check three batches of 16 confirmed orphans."""
    pids: list[int] = []
    for batch in range(1, 4):
        pids.extend(orphan() for _ in range(16))
        expected = "Z" if baseline else None
        wait_for(
            lambda: all(state(pid) == expected for pid in pids),
            "orphans become zombies" if baseline else "orphans are reaped",
        )
        if baseline:
            time.sleep(0.2)
            if not all(state(pid) == "Z" for pid in pids):
                raise RuntimeError("Baseline unexpectedly reaped an orphan")
        print(
            f"batch={batch} adopted={len(pids)} "
            f"remaining_zombies={sum(state(pid) == 'Z' for pid in pids)}",
            flush=True,
        )


def main() -> None:
    """Validate the baseline, reaping, or signal resilience."""
    mode = sys.argv[1]
    cmdline = Path("/proc/1/cmdline").read_bytes().replace(b"\0", b" ")
    print(f"pid1={cmdline.decode()}", flush=True)
    if mode == "baseline":
        if cmdline.split()[0].rsplit(b"/", 1)[-1] != b"tail":
            raise RuntimeError("Baseline PID 1 is not tail")
        batches(baseline=True)
        return
    if cmdline.split()[0].rsplit(b"/", 1)[-1] not in (b"sh", b"bash"):
        raise RuntimeError("Loop shell is not PID 1 (is Docker init enabled?)")
    wait_for(lambda: sleep_pid() is not None, "initial sleep starts")
    original_sleep = sleep_pid()
    if mode == "signals":
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
            os.kill(1, sig)
            time.sleep(0.1)
            print(f"survived {sig.name}", flush=True)
        assert original_sleep is not None
        os.kill(original_sleep, signal.SIGKILL)
        wait_for(lambda: sleep_pid() not in (None, original_sleep), "sleep is replaced")
        print("sleep child replaced", flush=True)
    batches(baseline=False)
    if mode == "loop" and sleep_pid() != original_sleep:
        raise RuntimeError("Sleep changed: reaping during the original wait not proven")


if __name__ == "__main__":
    main()
