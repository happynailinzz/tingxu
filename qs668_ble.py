"""Structured BLE CLI output, shared by the console and collector."""
import re
from pathlib import Path


def parse_file_list(text):
    result = []
    for line in text.splitlines():
        match = re.match(r'^\s*\d+\.\s+(.+?)\s+size=(\d+)\s+time=(\d+)', line)
        if match:
            name = match[1]
            if any(c in name for c in '/\\\x00\n\r,') or name in {'.', '..'}:
                continue
            result.append({'name': name, 'size': int(match[2]), 'time': int(match[3]), 'stem': stem_from_name(name)})
    return result


def stem_from_name(name):
    stem = name.rstrip('.')
    for suffix in ('.wav', '.opus', '.ogg', '.raw', '.bin', '.mp3', '.m4a'):
        if stem.lower().endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    return re.sub(r'[^A-Za-z0-9._-]+', '_', stem).strip('._-') or 'qs668-import'


def valid_address(value):
    return isinstance(value, str) and bool(re.fullmatch(
        r'(?:[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})', value))


def parse_devices(text):
    devices = []
    seen = set()
    for line in text.splitlines():
        match = re.match(r'^\s+(\S+)\s+(.+?)\s*$', line)
        if match and valid_address(match[1]) and match[1] not in seen:
            seen.add(match[1])
            devices.append({'address': match[1], 'name': match[2]})
    return devices
