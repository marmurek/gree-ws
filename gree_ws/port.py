"""Print the port the application will listen on.

The Docker health check needs the port before it can probe anything, and the
port can come from the configuration file as easily as from the environment.
Resolving it through the same loader keeps the probe and the application from
disagreeing.
"""

import os

from gree_ws.config import DEFAULT_CONFIG_PATH, load_settings

if __name__ == "__main__":
    print(load_settings(os.environ.get("CONFIG_FILE", DEFAULT_CONFIG_PATH)).port)
