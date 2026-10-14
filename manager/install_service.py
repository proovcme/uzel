"""Install a per-user LaunchAgent using the current interpreter and checkout."""
import argparse
import os
import plistlib
import subprocess
import sys
import socket
from pathlib import Path
import config


# Credentials stay in the ignored configuration, not copied indiscriminately.
ENVIRONMENT_KEYS = ('PATH','HF_HOME','HF_HUB_CACHE','HUGGINGFACE_HUB_CACHE','XDG_CACHE_HOME',
    'XDG_DATA_HOME','OMLX_BASE_PATH','OMLX_MODEL_DIR','UV_TOOL_DIR')
SETTING_KEYS = ('PORT','DATA_ROOT','MODEL_ROOTS','IMAGE_ROOT','IMAGE_PYTHON','IMAGE_CACHE',
    'OMLX_EXECUTABLE','MLX_EXECUTABLE','WEB_PYTHON','WEB_BACKEND','IMAGE_ACCELERATION',
    'EXTERNAL_ENABLED','EXTERNAL_LABEL','EXTERNAL_PLIST','EXTERNAL_MODEL','EXTERNAL_LOG',
    'EXTERNAL_MODEL_ID','EXTERNAL_ENDPOINT','PROXY_ENDPOINT','PROXY_LABEL','PROXY_PLIST',
    'SESSION_PATH','SESSION_HEADER','STATS_PATH','CODEX_BINARY','CODEX_MODEL')

def service_environment(environ=None):
    env = os.environ if environ is None else environ
    keys = (*ENVIRONMENT_KEYS, *(prefix+key for prefix in ('UZEL_', 'MLX_MANAGER_') for key in SETTING_KEYS))
    return {key:env[key] for key in keys if key in env}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label',default='org.uzel.service')
    args=parser.parse_args()
    if not all(c.isalnum() or c in '.-' for c in args.label):
        parser.error('Invalid LaunchAgent label')
    try:
        with socket.create_connection((config.MANAGER_HOST,config.MANAGER_PORT),timeout=1):
            parser.error(f'A service is already listening on port {config.MANAGER_PORT}; stop it before installing another LaunchAgent')
    except OSError:
        pass
    config.DATA_ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    agents=Path.home()/'Library/LaunchAgents';agents.mkdir(parents=True,exist_ok=True)
    destination=agents/(args.label+'.plist')
    if destination.exists():
        parser.error('LaunchAgent already exists; stop and edit it explicitly before reinstalling')
    content=dict(Label=args.label,ProgramArguments=[sys.executable,str(config.ROOT/'server.py')],WorkingDirectory=str(config.ROOT),StandardOutPath=str(config.DATA_ROOT/'stdout.log'),StandardErrorPath=str(config.DATA_ROOT/'stderr.log'),RunAtLoad=True,KeepAlive=True,ProcessType='Background',EnvironmentVariables=service_environment())
    destination.write_bytes(plistlib.dumps(content));destination.chmod(0o600)
    import os
    subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(destination)],check=True)
    print(f'Installed {destination}. Open http://127.0.0.1:{config.MANAGER_PORT}/')

if __name__=='__main__':main()
