#!/usr/bin/env python3
"""Generate or install a launchd plist for this checkout, without hardcoded paths."""
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'local.qs668.console'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['generate', 'install', 'status', 'restart', 'stop', 'uninstall'])
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('登录常驻服务仅适用于 macOS')
    target = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')
    domain = f'gui/{os.getuid()}'
    if args.action in {'generate', 'install'}:
        python = ROOT / 'funasr-env' / 'bin' / 'python'
        config = {'Label': LABEL, 'ProgramArguments': [str(python if python.is_file() else Path(sys.executable)), '-u', str(ROOT / 'qs668-console.py')],
                  'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True,
                  'ThrottleInterval': 10, 'LimitLoadToSessionType': 'Aqua',
                  'StandardOutPath': str(ROOT / 'console-service.log'),
                  'StandardErrorPath': str(ROOT / 'console-service-error.log')}
        if args.action == 'generate':
            target = ROOT / (LABEL + '.plist')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plistlib.dumps(config))
        print('Generated', target)
        if args.action == 'install':
            subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=True)
    elif args.action == 'status':
        subprocess.run(['launchctl', 'print', domain + '/' + LABEL], check=True)
    elif args.action == 'restart':
        subprocess.run(['launchctl', 'kickstart', '-k', domain + '/' + LABEL], check=True)
    else:
        result = subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL])
        if args.action == 'uninstall':
            target.unlink(missing_ok=True)
        elif result.returncode:
            return result.returncode
    return 0


if __name__ == '__main__':
    sys.exit(main())
