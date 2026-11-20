"""One-time authentication for loopback worker IPC.

The token is delivered over the child's stdin pipe, not in its command line or
the listening TCP port. Invalid local clients are rejected before a worker
channel is accepted.
"""
import hmac
import secrets
import socket
import time

MAGIC = b"BPC1"
TOKEN_HEX_LENGTH = 64
MAX_HANDSHAKE_ATTEMPTS = 16


def new_ipc_token():
    return secrets.token_hex(32)


def _receive_exact(connection, size):
    data = bytearray(size)
    view = memoryview(data)
    offset = 0
    while offset < size:
        count = connection.recv_into(view[offset:])
        if count == 0:
            raise ConnectionError("Local worker closed during authentication")
        offset += count
    return bytes(data)


def authenticate_loopback(listener, token, *, timeout=30):
    """Accept only a peer that proves possession of the one-time pipe token."""
    if (not isinstance(token, str) or len(token) != TOKEN_HEX_LENGTH
            or any(ch not in "0123456789abcdef" for ch in token)):
        raise ValueError("Invalid local IPC token")
    expected = token.encode("ascii")
    deadline = time.monotonic() + timeout
    attempts = 0
    while attempts < MAX_HANDSHAKE_ATTEMPTS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for authenticated local worker")
        listener.settimeout(remaining)
        connection, _ = listener.accept()
        attempts += 1
        try:
            connection.settimeout(min(5.0, remaining))
            received = _receive_exact(connection, len(MAGIC) + TOKEN_HEX_LENGTH)
            if (received[:len(MAGIC)] == MAGIC
                    and hmac.compare_digest(received[len(MAGIC):], expected)):
                connection.settimeout(None)
                return connection
        except (OSError, TimeoutError, ConnectionError):
            pass
        connection.close()
    raise PermissionError("Too many unauthenticated local IPC connections")
