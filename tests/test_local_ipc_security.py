"""Regression tests for authenticated loopback worker IPC."""
import importlib.util
from pathlib import Path
import socket
import threading
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "codec/stbaseline/local_ipc.py"
SPEC = importlib.util.spec_from_file_location("stpyramid_local_ipc_test", MODULE_PATH)
LOCAL_IPC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOCAL_IPC)
authenticate_loopback = LOCAL_IPC.authenticate_loopback
new_ipc_token = LOCAL_IPC.new_ipc_token


class LocalIpcAuthenticationTests(unittest.TestCase):
    def test_rejects_unauthenticated_peer_then_accepts_token_holder(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        token = new_ipc_token()
        address = listener.getsockname()
        client_errors = []

        def client():
            try:
                with socket.create_connection(address, timeout=3) as attacker:
                    attacker.sendall(b"BPC1" + b"0" * 64)
                with socket.create_connection(address, timeout=3) as worker:
                    handshake = b"BPC1" + token.encode("ascii")
                    worker.sendall(handshake[:13])
                    worker.sendall(handshake[13:])
                    if worker.recv(2) != b"OK":
                        raise AssertionError("Authenticated channel response missing")
            except Exception as exc:
                client_errors.append(exc)

        thread = threading.Thread(target=client, daemon=True)
        thread.start()
        try:
            channel = authenticate_loopback(listener, token, timeout=5)
            with channel:
                channel.sendall(b"OK")
        finally:
            listener.close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "IPC client did not finish")
        self.assertEqual(client_errors, [])

    def test_rejects_malformed_token_before_accept(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            with self.assertRaisesRegex(ValueError, "Invalid local IPC token"):
                authenticate_loopback(listener, "not-a-token", timeout=0.1)
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
