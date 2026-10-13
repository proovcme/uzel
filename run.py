"""One entry point for the local MLX toolkit. No model starts on import."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tool', nargs='?', default='manager', choices=('manager','image','check'))
    args = parser.parse_args(sys.argv[1:2])
    rest = sys.argv[2:]
    if args.tool == 'check':
        if rest:
            parser.error('check does not accept additional arguments')
        for directory in ('manager','image-kit'):
            subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],cwd=ROOT/directory,check=True)
        subprocess.run(['node','--check','static/app.js'],cwd=ROOT/'manager',check=True)
        subprocess.run(['node','--check','static/rooms.js'],cwd=ROOT/'manager',check=True)
        for test in sorted((ROOT/'manager/tests').glob('*.js')):
            subprocess.run(['node',str(test)],cwd=ROOT/'manager',check=True)
        return
    if args.tool == 'image':
        cwd = ROOT/'image-kit'
        candidates = [cwd/'.venv/bin/python',ROOT/'.venv/bin/python']
        executable = next((str(p) for p in candidates if p.is_file()),sys.executable)
        command = [executable,str(cwd/'generate.py'),*rest[1:]] if rest and rest[0]=='direct' else [executable,'-m','mlx_image',*rest]
    else:
        if rest:
            parser.error('manager does not accept additional arguments')
        cwd = ROOT/'manager'
        command = [sys.executable,str(cwd/'server.py')]
    os.chdir(cwd)
    os.execv(command[0],command)


if __name__ == '__main__':
    main()
