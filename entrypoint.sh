#!/bin/bash

set -e

if [ -z "$PORT" ]; then
  PORT=8123
fi

if [ -z "$DISCOVERY_TIMEOUT" ]; then
  DISCOVERY_TIMEOUT=3
fi

if [ -z "$POLLING_INTERVAL" ]; then
  POLLING_INTERVAL=2
fi

if [ -z "$VERBOSE" ]; then
  VERBOSE=""
else
  if [ "$VERBOSE" = "true" ]; then
    VERBOSE=--verbose
  else
    VERBOSE=""
  fi
fi

python3 main.py \
  --port ${PORT} \
  --discovery_timeout ${DISCOVERY_TIMEOUT} \
  --polling_interval ${POLLING_INTERVAL} \
  ${VERBOSE}
