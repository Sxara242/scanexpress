import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import scanbd_event_adapter


class ScanbdAdapterTests(unittest.TestCase):
    def test_build_event_requires_scanbd_target(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                scanbd_event_adapter.build_event_from_environment()

    def test_build_event_forwards_target_and_optional_device(self):
        with patch.dict(
            os.environ,
            {
                "SCANBD_TARGET": "5",
                "SCANBD_DEVICE": "pixma:04A91912_50466B",
            },
            clear=True,
        ):
            payload = scanbd_event_adapter.build_event_from_environment()

        self.assertEqual(
            payload,
            {
                "source": "scanbd",
                "target": "5",
                "device": "pixma:04A91912_50466B",
            },
        )

    def test_send_event_uses_local_unix_socket(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = str(Path(temp_dir) / "adapter.sock")
            ready = threading.Event()

            def server():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(socket_path)
                    listener.listen(1)
                    ready.set()
                    connection, _ = listener.accept()
                    with connection:
                        raw = connection.recv(4096)
                        payload = json.loads(raw.decode("utf-8").strip())
                        response = {
                            "status": "accepted",
                            "target": payload["target"],
                        }
                        connection.sendall(
                            (json.dumps(response) + "\n").encode("utf-8")
                        )

            thread = threading.Thread(target=server, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(timeout=1))

            response = scanbd_event_adapter.send_event(
                {"source": "scanbd", "target": "6"},
                socket_path,
            )
            thread.join(timeout=1)

        self.assertEqual(response["status"], "accepted")
        self.assertEqual(response["target"], "6")


if __name__ == "__main__":
    unittest.main()
