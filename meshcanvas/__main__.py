"""Entry point: python -m meshcanvas"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="meshcanvas",
        description="Meshtastic position injector for owned-mesh research",
    )
    # Localhost by default: this service can key a transmitter and has no auth.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {args.host} exposes an unauthenticated service "
            "that can transmit on real spectrum."
        )

    uvicorn.run(
        "meshcanvas.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
