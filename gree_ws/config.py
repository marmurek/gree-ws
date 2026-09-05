"""Command line and logging setup."""

import argparse
import logging
from typing import Optional, Sequence


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the command line arguments.

    Unknown arguments are ignored so the module can also be imported by a
    server that was started with its own command line, such as
    `uvicorn main:app` or a test runner.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.description = "Gree Climate API - REST and WebSocket API for controlling Gree air conditioners"

    parser.add_argument(
        "--dev_mode", help="Enable development mode with auto-reload", action="store_true", default=False
    )
    parser.add_argument("--port", help="Port to run the server on", type=int, default=8123)
    parser.add_argument("--discovery_timeout", help="Discovery timeout in seconds", type=int, default=3)
    parser.add_argument("--polling_interval", help="Polling interval in seconds", type=int, default=2)
    parser.add_argument(
        "--response_timeout", help="How long to wait for a device to answer, in seconds", type=float, default=5.0
    )
    parser.add_argument("--verbose", help="Enable verbose logging", action="store_true", default=False)

    args, _unknown = parser.parse_known_args(argv)
    return args


def configure_logging(args: argparse.Namespace) -> None:
    """Set the log levels for the application and for greeclimate"""
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().setLevel(logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger("greeclimate").setLevel(logging.DEBUG if args.verbose else logging.WARNING)
