"""P1-1 regression: large local-stage results and timeouts must not wedge the queue.

Old behaviour: the parent called proc.join() before reading the mp.Queue, so a
child holding a multi-MB result could never exit; after the timeout kill, the
drain loop blocked forever on a half-written message and the local slot was
never released (production stall, Oct 10).
"""

from __future__ import annotations

import multiprocessing as mp
import threading
import time


def _child_big_result(q, n):
    q.put(("ok", {"status": "local_done", "blob": [("x" * 24000) + str(i) for i in range(n)]}))


def _child_sleep_forever(q, big):
    if big:
        # Start writing a large message, then hang: the classic half-written pipe.
        q.put(("ok", {"blob": [("y" * 24000) + str(i) for i in range(200)]}))
    time.sleep(3600)


def _child_exit_silently(q, _unused):
    return


def _run(target, arg):
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=target, args=(q, arg), daemon=True)
    proc.start()
    return proc, q


def test_large_result_is_received_without_deadlock():
    from app.scan_executor import _await_child_result, _discard_queue

    proc, q = _run(_child_big_result, 220)  # ~5 MB, far over the pipe buffer
    started = time.monotonic()
    message, timed_out = _await_child_result(proc, q, timeout=30)
    elapsed = time.monotonic() - started
    proc.join(timeout=10)
    _discard_queue(q)
    assert timed_out is False
    assert message and message[0] == "ok"
    assert len(message[1]["blob"]) == 220
    assert not proc.is_alive()
    assert elapsed < 20


def test_timeout_kill_and_cleanup_never_block():
    from app.scan_executor import _await_child_result, _discard_queue, _terminate

    proc, q = _run(_child_sleep_forever, True)
    message, timed_out = _await_child_result(proc, q, timeout=3)
    # A large half-read message may arrive before the deadline; either way the
    # cleanup below must return promptly.
    done = threading.Event()

    def cleanup():
        _terminate(proc, "test")
        _discard_queue(q)
        done.set()

    threading.Thread(target=cleanup, daemon=True).start()
    assert done.wait(20), "cleanup blocked after killing the child"
    assert not proc.is_alive()
    assert timed_out or message is not None


def test_pure_hang_times_out():
    from app.scan_executor import _await_child_result, _discard_queue, _terminate

    proc, q = _run(_child_sleep_forever, False)
    started = time.monotonic()
    message, timed_out = _await_child_result(proc, q, timeout=2)
    assert timed_out is True and message is None
    assert time.monotonic() - started < 10
    _terminate(proc, "test")
    _discard_queue(q)
    assert not proc.is_alive()


def test_child_exit_without_result_is_reported():
    from app.scan_executor import _await_child_result, _discard_queue

    proc, q = _run(_child_exit_silently, None)
    message, timed_out = _await_child_result(proc, q, timeout=20)
    _discard_queue(q)
    assert timed_out is False and message is None


def test_runner_result_does_not_echo_payload():
    """The child's return value must stay small (no prior_snapshots echo)."""
    import inspect

    from app import scan_job_runner

    src = inspect.getsource(scan_job_runner.execute_ephemeral_job)
    tail = src[src.rindex("return {") :]
    assert '"payload"' not in tail
