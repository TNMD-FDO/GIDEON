"""Run the corpus egress door until the container receives a stop signal."""

import signal
import socket
import sys
import threading
from collections.abc import Mapping

from .proxy import ProxyServer
from .settings import EgressSettingsError, load_settings


def install_stop_handlers(stop: threading.Event) -> None:
    """Let the first process stop accepting connections on SIGTERM or SIGINT."""

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def main(environ: Mapping[str, str] | None = None) -> int:
    """Validate settings, bind every interface, and serve until stopped."""

    try:
        settings = load_settings(environ)
        stop = threading.Event()
        install_stop_handlers(stop)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listening:
            listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listening.bind(("0.0.0.0", settings.port))
            listening.listen()
            ProxyServer(settings).serve(listening, stop)
    except EgressSettingsError as exc:
        print(f"egress failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - startup failures must be content-free.
        print(
            f"egress failed: {type(exc).__name__}. "
            "Fix: check the egress listener and rerun sudo python3 -m gideon apply.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
