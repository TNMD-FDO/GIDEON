"""The one HTTPS connection shape for reaching the ingress on loopback.

Apply, reconcile, and the alert test reach the services the way users do —
through Caddy with the office CA — but dial 127.0.0.1 and present the site
hostname through SNI (Caddy routes by both SNI and ``Host``).  The frontend
client and the Grafana client share this class so the posture cannot drift.
"""

import http.client
import ssl


class SniHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection that dials one host while presenting another SNI name."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        context: ssl.SSLContext,
        server_hostname: str,
        timeout: float,
    ) -> None:
        super().__init__(host, port, context=context, timeout=timeout)
        self._gideon_context = context
        self._gideon_server_hostname = server_hostname

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        if self.sock is None:
            raise OSError("HTTPS socket was not opened")
        self.sock = self._gideon_context.wrap_socket(
            self.sock, server_hostname=self._gideon_server_hostname
        )
