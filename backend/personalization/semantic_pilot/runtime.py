"""Runs deadline-bound CLI pilots in a disposable spawned worker so blocked native HTTP cannot retain the host indefinitely."""

from __future__ import annotations

import multiprocessing
import time
from typing import Any, Dict

from .runner import PilotConfig, PilotError, RunCancelled, run_pilot

CLEANUP_GRACE_SECONDS = 1.0


def _run_worker(config: PilotConfig, sender) -> None:
    try:
        sender.send(("ok", run_pilot(config)))
    except RunCancelled:
        sender.send(("cancelled", None))
    except (PilotError, ValueError) as exc:
        sender.send((type(exc).__name__, str(exc)))
    except Exception as exc:
        sender.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        sender.close()


def run_pilot_bounded(config: PilotConfig) -> Dict[str, Any]:
    seconds = config.bounds.runtime_deadline_seconds
    if seconds is None:
        return run_pilot(config)
    deadline = time.monotonic() + seconds
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_run_worker, args=(config, sender), daemon=True)
    started = False
    sender_open = True
    try:
        worker.start()
        started = True
        sender.close()
        sender_open = False
        if not receiver.poll(max(0.0, deadline - time.monotonic())):
            raise RunCancelled("pilot host runtime deadline reached")
        try:
            kind, result = receiver.recv()
        except EOFError:
            raise PilotError("pilot worker exited without a report") from None
        if time.monotonic() >= deadline or kind == "cancelled":
            raise RunCancelled("pilot host runtime deadline reached")
        if kind == "PilotError":
            raise PilotError(result)
        if kind == "ValueError":
            raise ValueError(result)
        if kind != "ok":
            raise RuntimeError(result)
        return result
    finally:
        receiver.close()
        if sender_open:
            sender.close()
        if started:
            worker.join(timeout=0)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=CLEANUP_GRACE_SECONDS)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=CLEANUP_GRACE_SECONDS)
            if worker.is_alive():
                raise PilotError("pilot worker could not be stopped")
        worker.close()
