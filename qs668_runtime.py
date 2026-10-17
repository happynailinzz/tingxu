"""Select a usable BLE interpreter without tying Bluetooth to the ASR environment."""
from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

PROBE = 'from bleak import BleakClient, BleakScanner; print("BLE_IMPORT_OK")'


def probe_ble_python(python):
    try:
        result = subprocess.run([str(python), '-c', PROBE], capture_output=True,
                                text=True, timeout=15)
        if result.returncode == 0 and 'BLE_IMPORT_OK' in result.stdout:
            return True, ''
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, detail[-1] if detail else f'exit={result.returncode}'
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


@lru_cache(maxsize=8)
def _select(base_string, fallback, override):
    base = Path(base_string)
    if override:
        candidates = [Path(override).expanduser()]
    else:
        candidates = [base / 'nextproto/tools/.venv-ble/bin/python',
                      base / '.venv-ble/bin/python', Path(fallback), Path(sys.executable)]
    seen, failures = set(), []
    for candidate in candidates:
        # Keep venv entrypoint paths: resolving a symlink loses the environment.
        path = str(candidate.absolute())
        if path in seen:
            continue
        seen.add(path)
        if not candidate.is_file():
            failures.append(path + ': interpreter not found')
            continue
        ok, detail = probe_ble_python(candidate)
        if ok:
            return path
        failures.append(path + ': ' + detail)
    raise RuntimeError(
        '没有可用的 BLE Python 环境。请安装 requirements-core.txt 中的蓝牙依赖，'
        '或用 QS668_BLE_PYTHON 指向已安装 bleak 的 Python。检测结果：' + '；'.join(failures))


def ble_python(base, fallback):
    return _select(str(Path(base).absolute()), str(fallback), os.environ.get('QS668_BLE_PYTHON', ''))


def failure_detail(lines):
    meaningful = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith(('File "', '^'))]
    detail = meaningful[-1] if meaningful else '进程未返回错误详情，请查看日志'
    lower = detail.lower()
    if ('no module named' in lower and 'bleak' in lower) or 'install ble deps' in lower:
        return '执行环境缺少 bleak 蓝牙依赖：' + detail
    if 'ble is unsupported' in lower:
        return '当前进程无法使用系统 BLE 控制器。请从本机终端启动管理台重试；该错误不能证明录音卡不受支持。'
    if 'not authorized' in lower or 'unauthorized' in lower or 'authorization' in lower:
        return '蓝牙访问未授权，请检查 macOS 系统设置 → 隐私与安全性 → 蓝牙。' + detail
    if 'powered off' in lower or 'turned off' in lower:
        return '系统蓝牙未开启。' + detail
    return detail[:300]
