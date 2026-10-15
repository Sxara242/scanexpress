import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hardware_events import (
    HardwareEventDispatcher,
    HardwareEventSocketService,
    parse_target_map,
    resolve_debounce_seconds,
)


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += seconds


class HardwareTargetMapTests(unittest.TestCase):
    def test_parse_target_map_supports_reference_lide_mapping(self):
        mapping = parse_target_map(
            "5=document,6=finish,2=batch,1=ignore,3=ignore"
        )
        self.assertEqual(
            mapping,
            {
                "5": "document",
                "6": "finish",
                "2": "batch",
                "1": "ignore",
                "3": "ignore",
            },
        )

    def test_parse_target_map_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            parse_target_map("5=explode")

    def test_debounce_validation(self):
        self.assertEqual(resolve_debounce_seconds(None), 1.5)
        self.assertEqual(resolve_debounce_seconds("2.25"), 2.25)
        with self.assertRaises(ValueError):
            resolve_debounce_seconds("-1")


class HardwareDispatcherTests(unittest.TestCase):
    def test_unmapped_and_reserved_targets_do_not_reach_handler(self):
        calls = []
        dispatcher = HardwareEventDispatcher(
            calls.append,
            target_map={"1": "ignore", "5": "document"},
        )
        self.addCleanup(dispatcher.stop)

        unmapped = dispatcher.submit({"target": "99"})
        reserved = dispatcher.submit({"target": "1"})

        self.assertEqual(unmapped["status"], "ignored")
        self.assertEqual(unmapped["reason"], "unmapped-target")
        self.assertEqual(reserved["reason"], "reserved-target")
        self.assertTrue(dispatcher.wait_until_idle())
        self.assertEqual(calls, [])

    def test_duplicate_is_suppressed_while_first_event_is_inflight(self):
        handler_started = threading.Event()
        release_handler = threading.Event()
        calls = []

        def handler(event):
            calls.append(event)
            handler_started.set()
            release_handler.wait(timeout=2)

        dispatcher = HardwareEventDispatcher(
            handler,
            target_map={"5": "document"},
            debounce_seconds=0,
        )
        self.addCleanup(dispatcher.stop)

        first = dispatcher.submit({"target": "5", "source": "scanbd", "device": "lide"})
        self.assertEqual(first["status"], "accepted")
        self.assertTrue(handler_started.wait(timeout=1))

        duplicate = dispatcher.submit(
            {"target": "5", "source": "scanbd", "device": "lide"}
        )
        self.assertEqual(duplicate["status"], "ignored")
        self.assertEqual(duplicate["reason"], "duplicate-inflight")

        release_handler.set()
        self.assertTrue(dispatcher.wait_until_idle())
        self.assertEqual(len(calls), 1)

    def test_debounce_suppresses_fast_repeat_after_handler_finishes(self):
        calls = []
        clock = FakeClock()
        dispatcher = HardwareEventDispatcher(
            calls.append,
            target_map={"6": "finish"},
            debounce_seconds=1.5,
            clock=clock,
        )
        self.addCleanup(dispatcher.stop)

        first = dispatcher.submit({"target": "6"})
        self.assertEqual(first["status"], "accepted")
        self.assertTrue(dispatcher.wait_until_idle())

        clock.advance(0.2)
        duplicate = dispatcher.submit({"target": "6"})
        self.assertEqual(duplicate["reason"], "debounced")

        clock.advance(1.5)
        next_press = dispatcher.submit({"target": "6"})
        self.assertEqual(next_press["status"], "accepted")
        self.assertTrue(dispatcher.wait_until_idle())
        self.assertEqual(len(calls), 2)

    def test_different_actions_are_serialized_in_acceptance_order(self):
        calls = []
        dispatcher = HardwareEventDispatcher(
            lambda event: calls.append(event["action"]),
            target_map={"5": "document", "6": "finish"},
            debounce_seconds=0,
        )
        self.addCleanup(dispatcher.stop)

        self.assertEqual(dispatcher.submit({"target": "5"})["status"], "accepted")
        self.assertEqual(dispatcher.submit({"target": "6"})["status"], "accepted")
        self.assertTrue(dispatcher.wait_until_idle())
        self.assertEqual(calls, ["document", "finish"])


class HardwareSocketTests(unittest.TestCase):
    def test_unix_socket_acknowledges_without_waiting_for_scan_handler(self):
        handler_started = threading.Event()
        release_handler = threading.Event()

        def handler(_event):
            handler_started.set()
            release_handler.wait(timeout=2)

        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "hardware.sock"
            service = HardwareEventSocketService(
                handler,
                socket_path=socket_path,
                target_map={"5": "document"},
                debounce_seconds=1.5,
            )
            self.addCleanup(service.stop)

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(str(socket_path))
                client.sendall(b'{"source":"scanbd","target":"5"}\n')
                response = client.recv(4096)

            payload = json.loads(response.decode("utf-8"))
            self.assertEqual(payload["status"], "accepted")
            self.assertTrue(handler_started.wait(timeout=1))
            release_handler.set()
            self.assertTrue(service.dispatcher.wait_until_idle())


if __name__ == "__main__":
    unittest.main()
