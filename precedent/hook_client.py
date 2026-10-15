"""What Claude Code runs for every hook event. Standard library only, so it starts fast: it forwards the event to
the local daemon (`python -m precedent serve`), which already holds the graph connection, and starts that daemon
in the background the first time. If the daemon can't be reached, the hook runs in-process (same code, slower)."""

import os
import socket
import sys

PORT = int(os.getenv("PRECEDENT_DAEMON_PORT", "7341"))


def via_daemon(raw):
    """A bare HTTP/1.0 POST over a socket (http.client alone costs ~60 ms of imports per hook)."""
    body = raw.encode()
    with socket.create_connection(("127.0.0.1", PORT), timeout=10) as s:
        s.sendall(b"POST /hook HTTP/1.0\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        chunks = []
        while chunk := s.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks).partition(b"\r\n\r\n")[2].decode()


def start_daemon():
    import subprocess
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, "-m", "precedent", "serve", "--port", str(PORT)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
                     start_new_session=os.name != "nt")


def main():
    raw = sys.stdin.read()
    try:
        body = via_daemon(raw)
    except OSError:
        if os.getenv("PRECEDENT_NO_DAEMON") != "1":
            start_daemon()  # ready for the next event; this one runs in-process
        import json
        from precedent import hook
        o = hook.handle(json.loads(raw))
        body = json.dumps(o) if o else ""
    if body:
        print(body)


if __name__ == "__main__":
    main()
