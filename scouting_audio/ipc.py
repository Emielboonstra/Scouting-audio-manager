"""One-request JSON protocol over a private Unix socket."""

import json
import socket

from .config import runtime_dir

MAX_REQUEST = 4 * 1024 * 1024


def request(action, **arguments):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(12)
        connection.connect(str(runtime_dir() / "engine.sock"))
        connection.sendall(json.dumps({"action": action, **arguments}).encode() + b"\n")
        with connection.makefile("rb") as stream:
            response = json.loads(stream.readline(MAX_REQUEST))
    if not response["ok"]:
        raise RuntimeError(response["error"])
    return response["data"]
