import json
import logging
import os
import queue
import socketserver
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_ALLOWED_ACTIONS = {"document", "batch", "finish", "ignore"}
_DEFAULT_DEBOUNCE_SECONDS = 1.5
_DEFAULT_QUEUE_SIZE = 32
_MAX_EVENT_BYTES = 4096


def parse_target_map(raw_value: str | None) -> dict[str, str]:
    if raw_value is None or raw_value.strip() == "":
        return {}

    result: dict[str, str] = {}
    for raw_entry in raw_value.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if "=" in entry:
            target, action = entry.split("=", 1)
        elif ":" in entry:
            target, action = entry.split(":", 1)
        else:
            raise ValueError(
                "Hardware target map entries must use target=action syntax."
            )

        target = target.strip()
        action = action.strip().lower()
        if not target:
            raise ValueError("Hardware target map contains an empty target.")
        if action not in _ALLOWED_ACTIONS:
            raise ValueError(
                f"Unsupported hardware action '{action}' for target '{target}'."
            )
        if target in result:
            raise ValueError(f"Hardware target '{target}' is configured more than once.")
        result[target] = action

    return result


def resolve_debounce_seconds(raw_value: str | None) -> float:
    if raw_value is None or raw_value.strip() == "":
        return _DEFAULT_DEBOUNCE_SECONDS
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError("Hardware debounce must be a number.") from exc
    if value < 0 or value > 30:
        raise ValueError("Hardware debounce must be between 0 and 30 seconds.")
    return value


@dataclass(frozen=True)
class NormalizedHardwareEvent:
    target: str
    action: str
    source: str
    device: str

    @property
    def duplicate_key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.device)

    def as_dict(self) -> dict[str, str]:
        return {
            "target": self.target,
            "action": self.action,
            "source": self.source,
            "device": self.device,
        }


class HardwareEventDispatcher:
    def __init__(
        self,
        handler: Callable[[dict], object],
        *,
        target_map: dict[str, str],
        debounce_seconds: float = _DEFAULT_DEBOUNCE_SECONDS,
        max_queue_size: int = _DEFAULT_QUEUE_SIZE,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ):
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be greater than zero.")

        invalid_actions = set(target_map.values()) - _ALLOWED_ACTIONS
        if invalid_actions:
            raise ValueError(
                f"Unsupported hardware actions: {', '.join(sorted(invalid_actions))}"
            )

        self._handler = handler
        self._target_map = dict(target_map)
        self._debounce_seconds = debounce_seconds
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._queue: queue.Queue[NormalizedHardwareEvent | None] = queue.Queue(
            maxsize=max_queue_size
        )
        self._gate_lock = threading.Lock()
        self._last_accepted: dict[tuple[str, str, str], float] = {}
        self._inflight: set[tuple[str, str, str]] = set()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="scanexpress-hardware-worker",
            daemon=True,
        )
        self._worker.start()

    def _normalize(self, payload: dict) -> NormalizedHardwareEvent | None:
        raw_target = payload.get("target")
        if raw_target is None:
            raise ValueError("Hardware event target is required.")

        target = str(raw_target).strip()
        if not target:
            raise ValueError("Hardware event target is required.")

        action = self._target_map.get(target)
        if action is None:
            return None

        source = str(payload.get("source") or "scanbd").strip() or "scanbd"
        device = str(payload.get("device") or "default").strip() or "default"
        return NormalizedHardwareEvent(
            target=target,
            action=action,
            source=source,
            device=device,
        )

    def submit(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Hardware event payload must be an object.")

        event = self._normalize(payload)
        if event is None:
            return {
                "status": "ignored",
                "reason": "unmapped-target",
                "target": str(payload.get("target", "")),
            }
        if event.action == "ignore":
            return {
                "status": "ignored",
                "reason": "reserved-target",
                **event.as_dict(),
            }

        now = self._clock()
        key = event.duplicate_key
        with self._gate_lock:
            previous = self._last_accepted.get(key)
            if key in self._inflight:
                return {
                    "status": "ignored",
                    "reason": "duplicate-inflight",
                    **event.as_dict(),
                }
            if previous is not None and now - previous < self._debounce_seconds:
                return {
                    "status": "ignored",
                    "reason": "debounced",
                    **event.as_dict(),
                }

            try:
                self._queue.put_nowait(event)
            except queue.Full:
                return {
                    "status": "busy",
                    "reason": "event-queue-full",
                    **event.as_dict(),
                }

            self._last_accepted[key] = now
            self._inflight.add(key)

        return {
            "status": "accepted",
            **event.as_dict(),
        }

    def _worker_loop(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                self._queue.task_done()
                return

            try:
                self._handler(event.as_dict())
            except Exception:
                self._logger.exception(
                    "Hardware event failed: target=%s action=%s source=%s device=%s",
                    event.target,
                    event.action,
                    event.source,
                    event.device,
                )
            finally:
                with self._gate_lock:
                    self._inflight.discard(event.duplicate_key)
                self._queue.task_done()

    def wait_until_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._gate_lock:
                if not self._inflight and self._queue.unfinished_tasks == 0:
                    return True
            time.sleep(0.01)
        return False

    def stop(self, timeout: float = 2.0) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return
        self._worker.join(timeout=timeout)


class _HardwareUnixRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        raw_line = self.rfile.readline(_MAX_EVENT_BYTES + 1)
        if len(raw_line) > _MAX_EVENT_BYTES:
            response = {"status": "error", "message": "Hardware event is too large."}
        else:
            try:
                payload = json.loads(raw_line.decode("utf-8"))
                response = server.dispatcher.submit(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                response = {"status": "error", "message": str(exc)}

        self.wfile.write((json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))
        self.wfile.flush()


class _HardwareUnixServer(socketserver.UnixStreamServer):
    allow_reuse_address = True

    def __init__(self, socket_path: str, dispatcher: HardwareEventDispatcher):
        self.dispatcher = dispatcher
        super().__init__(socket_path, _HardwareUnixRequestHandler)


class HardwareEventSocketService:
    def __init__(
        self,
        handler: Callable[[dict], object],
        *,
        socket_path: Path,
        target_map: dict[str, str],
        debounce_seconds: float,
        logger: logging.Logger | None = None,
    ):
        self.socket_path = socket_path
        self._logger = logger or logging.getLogger(__name__)
        self._dispatcher = HardwareEventDispatcher(
            handler,
            target_map=target_map,
            debounce_seconds=debounce_seconds,
            logger=self._logger,
        )

        socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass

        self._server = _HardwareUnixServer(str(socket_path), self._dispatcher)
        try:
            socket_path.chmod(0o660)
        except OSError:
            self._server.server_close()
            self._dispatcher.stop()
            raise

        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="scanexpress-hardware-socket",
            daemon=True,
        )
        self._server_thread.start()

    @property
    def dispatcher(self) -> HardwareEventDispatcher:
        return self._dispatcher

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._server_thread.join(timeout=2.0)
        self._dispatcher.stop()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


def start_hardware_event_service(
    handler: Callable[[dict], object],
    *,
    logger: logging.Logger | None = None,
) -> HardwareEventSocketService | None:
    socket_value = os.getenv("SCANEXPRESS_HARDWARE_SOCKET", "").strip()
    if not socket_value:
        return None

    target_map = parse_target_map(os.getenv("SCANEXPRESS_HARDWARE_TARGET_MAP"))
    if not target_map:
        raise RuntimeError(
            "SCANEXPRESS_HARDWARE_TARGET_MAP is required when hardware events are enabled."
        )

    debounce_seconds = resolve_debounce_seconds(
        os.getenv("SCANEXPRESS_HARDWARE_DEBOUNCE_SECONDS")
    )
    return HardwareEventSocketService(
        handler,
        socket_path=Path(socket_value),
        target_map=target_map,
        debounce_seconds=debounce_seconds,
        logger=logger,
    )
