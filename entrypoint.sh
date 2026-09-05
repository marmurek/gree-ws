#!/bin/bash

set -e

# Every setting lives in the YAML configuration file and can be overridden by an
# environment variable, which the application reads directly. Nothing to
# translate here any more.
exec python3 main.py --config "${CONFIG_FILE:-config.yaml}"
