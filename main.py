"""Entry point for the Gree Climate API."""

import uvicorn

from gree_ws.api import create_app
from gree_ws.config import configure_logging, parse_args

cli_args = parse_args()
configure_logging(cli_args)

app = create_app(cli_args)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=cli_args.port,
        reload=cli_args.dev_mode,
        log_level="debug" if cli_args.verbose else "info",
        access_log=cli_args.verbose,
    )
