#!/usr/bin/env python3
"""Minimalist static-file server for the AuditTrace webui.

Serves `index.html` at `http://localhost:8765/` so the OIDC redirect URI
`http://localhost:8765/*` registered on the `audittrace-webui` Keycloak
client can land back here with the auth code.

Why not python -m http.server? This wrapper sets the correct MIME type
for `.html` (some Python builds default to `application/octet-stream`)
and keeps the listener bound to localhost only — never 0.0.0.0 — to
avoid accidentally exposing the dev UI to the LAN.

Usage:
    ./webui/serve.py            # binds 127.0.0.1:8765
    PORT=8888 ./webui/serve.py  # alt port (must also be in the
                                # Keycloak client's redirectUris)
"""

from __future__ import annotations

import os
import sys
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765"))


def main() -> int:
    handler = partial(SimpleHTTPRequestHandler, directory=str(WEBUI_DIR))
    with HTTPServer((HOST, PORT), handler) as httpd:
        print(
            f"AuditTrace webui — http://{HOST}:{PORT}/  (Ctrl-C to stop)",
            file=sys.stderr,
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
