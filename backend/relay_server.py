"""Uvicorn entry point with cooperative relay shutdown."""

from __future__ import annotations

import argparse
import logging
import signal

import uvicorn

from backend.app import app, app_subs, plugin_subs


class RelayServer(uvicorn.Server):
    """Notify ASGI streams before Uvicorn begins draining connections."""

    def handle_exit(self, sig: int, frame) -> None:
        state = getattr(self.config.app, "state", None)
        shutdown_event = getattr(state, "shutdown_requested", None)
        if shutdown_event is not None:
            shutdown_event.set()
            signal_name = signal.Signals(sig).name
            logging.getLogger("uvicorn.error").info(
                "relay shutdown signal=%s app_sse=%d plugin_sse=%d",
                signal_name,
                len(app_subs),
                len(plugin_subs),
            )
        super().handle_exit(sig, frame)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Valelin relay")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()

    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        timeout_graceful_shutdown=5,
    )
    RelayServer(config).run()


if __name__ == "__main__":
    main()
