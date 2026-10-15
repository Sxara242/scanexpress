#!/usr/bin/env python3
import json
import os
import socket
import sys


DEFAULT_SOCKET_PATH = "/run/scanexpress/hardware-events.sock"
MAX_RESPONSE_BYTES = 4096


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def build_event_from_environment() -> dict:
    target = os.getenv("SCANBD_TARGET", "").strip()
    if not target:
        raise RuntimeError("SCANBD_TARGET is missing.")

    device = _first_env(
        "SCANBD_DEVICE",
        "SCANBD_DEVNAME",
        "SCANBD_SCANNER",
    )

    payload = {
        "source": "scanbd",
        "target": target,
    }
    if device:
        payload["device"] = device
    return payload


def send_event(payload: dict, socket_path: str, timeout: float = 2.0) -> dict:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall(encoded)
        chunks = []
        remaining = MAX_RESPONSE_BYTES
        while remaining > 0:
            chunk = client.recv(min(remaining, 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
            if b"\n" in chunk:
                break

    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise RuntimeError("Hardware event socket returned no response.")
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict):
        raise RuntimeError("Hardware event socket returned an invalid response.")
    return response


def main() -> int:
    try:
        payload = build_event_from_environment()
        socket_path = os.getenv(
            "SCANEXPRESS_HARDWARE_SOCKET",
            DEFAULT_SOCKET_PATH,
        ).strip() or DEFAULT_SOCKET_PATH
        response = send_event(payload, socket_path)
    except Exception as exc:
        print(f"scanexpress-event: {exc}", file=sys.stderr)
        return 1

    status = response.get("status")
    if status in {"accepted", "ignored"}:
        return 0

    print(
        "scanexpress-event: " + json.dumps(response, sort_keys=True),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
