"""One JSON request per connection, one JSON reply: the same shape omen-fxd uses."""

from __future__ import annotations

import json
import socket

from . import paths


class HubError(RuntimeError):
    pass


def send(request: dict, path: str | None = None, timeout: float = 5.0,
         name: str = "omen-hubd") -> dict:
    path = path or paths.SOCKET
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((json.dumps(request) + "\n").encode())
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
    except FileNotFoundError:
        raise HubError(f"{name} non è in esecuzione (socket assente)") from None
    except ConnectionRefusedError:
        raise HubError(f"{name} non risponde") from None
    except PermissionError:
        raise HubError(f"permesso negato su {path}") from None
    except socket.timeout:
        raise HubError(f"{name}: timeout") from None
    except OSError as exc:
        raise HubError(f"{path}: {exc}") from None
    raw = b"".join(chunks).decode("utf-8", "replace").strip()
    if not raw:
        raise HubError(f"risposta vuota da {name}")
    try:
        reply = json.loads(raw)
    except json.JSONDecodeError:
        raise HubError(f"risposta non valida: {raw[:200]}") from None
    if reply.get("error"):
        raise HubError(reply["error"])
    return reply


def available(path: str | None = None) -> bool:
    try:
        send({"cmd": "ping"}, path, timeout=1.0)
        return True
    except HubError:
        return False
