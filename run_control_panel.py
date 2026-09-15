"""Safe local-only launcher for the Streamlit control panel.

Always binds to 127.0.0.1, regardless of streamlit_config/config.toml
(the separate cloud-deployment config -- never referenced here) or any
STREAMLIT_SERVER_*/STREAMLIT_BROWSER_* environment variable that might
be set. This is the command to run locally, including for approving
real-money Live Execution orders -- see docs/DEPLOYMENT.md's warning
about why that tab must never be approved from a publicly-reachable
instance.

    python run_control_panel.py [--port 8501]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

_UNSAFE_ENV_VARS = (
    "STREAMLIT_SERVER_ADDRESS", "STREAMLIT_SERVER_HEADLESS",
    "STREAMLIT_BROWSER_SERVER_ADDRESS", "STREAMLIT_SERVER_ENABLE_CORS",
    "STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()

    env = dict(os.environ)
    # Explicit CLI flags below already take precedence over these, but
    # stripping them too means nothing about this process's inherited
    # environment can affect binding -- defense in depth, not reliance
    # on flag precedence alone.
    for var in _UNSAFE_ENV_VARS:
        env.pop(var, None)

    cmd = [
        sys.executable, "-m", "streamlit", "run", "src/control_panel.py",
        "--server.address", "127.0.0.1",
        "--server.port", str(args.port),
        "--server.headless", "true",
        "--browser.serverAddress", "127.0.0.1",
        "--server.enableCORS", "true",
        "--server.enableXsrfProtection", "true",
    ]

    print(f"Starting the control panel locally at http://127.0.0.1:{args.port}")
    print("This launcher always binds to 127.0.0.1 and never reads streamlit_config/config.toml "
          "(the separate cloud-deployment config) -- Live Execution approval is only safe from here.")

    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
