"""Launch the existing UZEL MCP bridge with a private per-user connection."""
import json
import os
from pathlib import Path
import runpy
import sys


def main():
    if not os.environ.get('UZEL_ROOM_TOKEN'):
        path = Path(os.environ.get('UZEL_ROOMS_PLUGIN_CONFIG', str(Path.home()/'.local/share/uzel-rooms-plugin/connection.json'))).expanduser()
        try:
            connection = json.loads(path.read_text(encoding='utf-8'))
            token = connection['room_token']
            base = connection['manager_url']
            if not isinstance(token, str) or not 20 <= len(token) <= 100 or not isinstance(base, str):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError):
            raise SystemExit('Configure a private UZEL room invitation before using this plugin.') from None
        os.environ['UZEL_ROOM_TOKEN'] = token
        os.environ['UZEL_ROOMS_URL'] = base
    # Stdout belongs exclusively to JSON-RPC. No model or Codex process is started.
    runpy.run_path(str(Path(__file__).with_name('rooms_mcp.py')), run_name='__main__')


if __name__ == '__main__':
    main()
