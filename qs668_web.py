"""Local recording library, private model settings, and task helpers."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
AUDIO_EXTS = {'.wav', '.opus', '.ogg', '.mp3', '.m4a', '.flac', '.aac', '.webm', '.mp4'}
DEFAULTS = {
    'asr': {'mode': 'local', 'profile': 'paraformer-zh', 'model': '',
            'base_url': '', 'api_key': '', 'response_format': 'json'},
    'summary': {'mode': 'default', 'model': '', 'base_url': '', 'api_key': '', 'prompt': ''},
}
SETTINGS_LOCK = threading.RLock()


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as f:
            os.chmod(tmp, 0o600)
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path, default=None):
    if not path.is_file():
        return copy.deepcopy(default if default is not None else {})
    return json.loads(path.read_text(encoding='utf-8'))


def validate_endpoint(value):
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('服务地址须为 http(s) URL，凭据请填入 API Key')
    if parsed.query or parsed.fragment:
        raise ValueError('服务地址不可包含查询参数或锚点')
    return value.rstrip('/')


def settings(base=BASE, public=False):
    result = copy.deepcopy(DEFAULTS)
    with SETTINGS_LOCK:
        saved = read_json(base / '.qs668-web-settings.json')
    for section in result:
        result[section].update(saved.get(section, {}))
        if public:
            result[section]['has_key'] = bool(result[section].pop('api_key', ''))
    return result


def save_settings(payload, base=BASE):
    with SETTINGS_LOCK:
        result = settings(base)
        for section in DEFAULTS:
            incoming = payload.get(section, {})
            if not isinstance(incoming, dict):
                raise ValueError('设置格式不正确')
            for key in DEFAULTS[section]:
                if key in incoming:
                    if not isinstance(incoming[key], str):
                        raise ValueError(f'{section}.{key} 必须为文本')
                    # An empty password field keeps the stored credential.
                    if key != 'api_key' or incoming[key].strip():
                        result[section][key] = incoming[key] if key == 'prompt' else incoming[key].strip()
            if incoming.get('clear_key'):
                result[section]['api_key'] = ''
        a, s = result['asr'], result['summary']
        if a['mode'] not in {'local', 'remote'} or s['mode'] not in {'default', 'custom'}:
            raise ValueError('未知模型模式')
        if a['profile'] not in {'paraformer-zh', 'sensevoice', 'custom'}:
            raise ValueError('未知本地模型方案')
        if a['response_format'] not in {'json', 'verbose_json'}:
            raise ValueError('不支持的转写响应格式')
        for section, active in [(a, a['mode'] == 'remote'), (s, s['mode'] == 'custom')]:
            if active:
                section['base_url'] = validate_endpoint(section['base_url'])
                if not section['model']:
                    raise ValueError('请填写模型名称')
        if a['mode'] == 'local' and a['profile'] == 'custom' and not a['model']:
            raise ValueError('请填写 FunASR 模型 ID 或本地模型路径')
        if len(s['prompt']) > 32000:
            raise ValueError('总结提示词过长（最多 32000 字符）')
        private_json(base / '.qs668-web-settings.json', result)
    return settings(base, public=True)


def config_hash(section):
    clean = {k: v for k, v in section.items() if k not in {'api_key', 'has_key'}}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_child(root, name):
    if not isinstance(name, str) or not name or '\x00' in name:
        raise ValueError('无效文件名')
    root = root.resolve()
    candidate = root / name
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError('文件路径超出允许范围')
    return candidate


def validate_stem(stem):
    if not isinstance(stem, str) or not stem or stem in {'.', '..'} or any(c in stem for c in '/\\\x00\n\r'):
        raise ValueError('无效录音标识')
    return stem


def metadata(path):
    if not path.is_file():
        return {}
    text = path.read_text(encoding='utf-8', errors='replace')
    if not text.startswith('---\n'):
        return {}
    end = text.find('\n---', 4)
    if end < 0:
        return {}
    result = {}
    for line in text[4:end].splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            result[k.strip()] = v.strip().strip('"\'')
    return result


class Library:
    def __init__(self, base=BASE):
        self.base = base.resolve()
        self.archive = self.base / 'nextproto' / 'archive'
        self.audio = self.archive / 'audio'
        self.corpus = self.archive / 'corpus'
        self.brief = self.archive / 'brief'
        self.trash = self.archive / '.trash'
        self.lock = threading.RLock()

    def labels(self):
        return read_json(self.base / '.qs668-library.json')

    def files(self):
        labels = self.labels()
        grouped = {}
        for p in sorted(self.audio.glob('*')):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS and p.resolve().is_relative_to(self.audio.resolve()):
                grouped.setdefault(p.stem, []).append(p)
        for folder in [self.corpus, self.brief]:
            for p in folder.glob('*.md'):
                if not p.stem.endswith(('.fsmn', '.webrtc')):
                    grouped.setdefault(p.stem, [])
        result = []
        for stem, paths in grouped.items():
            paths.sort(key=lambda p: (p.suffix.lower() != '.wav', p.name))
            c, b = self.corpus / f'{stem}.md', self.brief / f'{stem}.md'
            meta = metadata(c)
            summary_meta = metadata(b)
            related = paths + [p for p in (c, b) if p.is_file()]
            result.append({'stem': stem, 'title': labels.get(stem, {}).get('title', stem),
                'name': paths[0].name if paths else None, 'variants': [p.name for p in paths],
                'size': sum(p.stat().st_size for p in paths),
                'modified': max((p.stat().st_mtime for p in related), default=0),
                'duration': meta.get('duration', ''), 'recorded_at': meta.get('recorded_at', ''),
                'corpus': c.is_file(), 'brief': b.is_file(), 'audio': bool(paths),
                'asr_model': meta.get('model', meta.get('provider', '')),
                'summary_model': summary_meta.get('model', '')})
        return sorted(result, key=lambda p: p['stem'], reverse=True)

    def resolve(self, source, name, volume=None):
        if source == 'local':
            root = self.audio
        elif source == 'card':
            if not volume or Path(volume).name != volume or volume.lower() in {'macintosh hd', 'recovery'}:
                raise ValueError('无效卡片卷名')
            root = safe_child(Path('/Volumes'), volume)
            if not (root / 'record').is_dir():
                raise ValueError('该挂载卷不是录音卡')
        else:
            raise ValueError('未知文件来源')
        p = safe_child(root, name)
        if p.suffix.lower() not in AUDIO_EXTS or not p.is_file():
            raise FileNotFoundError('音频文件不存在或格式不支持')
        return p

    def content(self, stem, kind):
        validate_stem(stem)
        if kind not in {'corpus', 'brief'}:
            raise ValueError('未知内容类型')
        p = safe_child(getattr(self, kind), stem + '.md')
        if not p.is_file():
            raise FileNotFoundError('尚未生成内容')
        return p

    def title(self, stem, title):
        validate_stem(stem)
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ValueError('标题须为 1–120 个字符')
        if stem not in {f['stem'] for f in self.files()}:
            raise FileNotFoundError('录音不存在')
        with self.lock:
            labels = self.labels()
            labels[stem] = {'title': title.strip()}
            private_json(self.base / '.qs668-library.json', labels)

    def import_file(self, name, stream, size):
        if Path(name).name != name or '\\' in name:
            raise ValueError('无效文件名')
        validate_stem(Path(name).stem)
        if Path(name).suffix.lower() not in AUDIO_EXTS:
            raise ValueError('不支持的音频格式')
        self.audio.mkdir(parents=True, exist_ok=True)
        target = safe_child(self.audio, name)
        # Stems are archive identities: avoid attaching an upload to old results.
        if any((folder / (Path(name).stem + '.md')).exists() for folder in (self.corpus, self.brief)) or any(p.stem == Path(name).stem for p in self.audio.glob('*')):
            raise FileExistsError('已存在同名录音，请先重命名待导入文件')
        tmp = self.audio / ('.upload-' + uuid.uuid4().hex)
        try:
            with tmp.open('xb') as out:
                remaining = size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('上传中断，请重试')
                    out.write(chunk)
                    remaining -= len(chunk)
            # Link without overwriting any concurrent upload.
            os.link(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return target

    def recycle(self, stems):
        batch = self.trash / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        items = []
        for stem in stems:
            validate_stem(stem)
            for folder in [self.audio, self.corpus, self.brief]:
                # Explicit iteration avoids treating user filenames as glob patterns.
                for p in folder.glob('*'):
                    if p.stem == stem and p.is_file():
                        safe_child(folder, p.name)
                        items.append((p, Path(folder.name) / p.name))
            session = safe_child(self.base / 'sessions', stem)
            if session.is_dir() and not session.is_symlink():
                items.append((session, Path('sessions') / stem))
        if not items:
            raise FileNotFoundError('未找到可移入回收站的文件')
        batch.mkdir(parents=True)
        manifest = {'created': time.time(), 'items': [{'source': str(p.relative_to(self.base)), 'stored': str(rel)} for p, rel in items]}
        private_json(batch / 'manifest.json', manifest)
        for p, rel in items:
            dst = batch / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dst))
        return batch.name

    def trash_items(self):
        return [{'id': p.parent.name, **read_json(p)} for p in sorted(self.trash.glob('*/manifest.json'), reverse=True)]

    def restore(self, batch_id):
        batch = safe_child(self.trash, batch_id)
        manifest = read_json(batch / 'manifest.json')
        plan = []
        for item in manifest.get('items', []):
            dst, src = safe_child(self.base, item['source']), safe_child(batch, item['stored'])
            if not src.exists():
                continue
            if dst.exists() or dst.is_symlink():
                raise FileExistsError('原位置已有文件，请先解决冲突')
            plan.append((src, dst))
        if not plan:
            raise FileNotFoundError('回收项目不存在')
        for src, dst in plan:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        shutil.rmtree(batch)


def mime(path):
    return {'.wav': 'audio/wav', '.opus': 'audio/ogg', '.m4a': 'audio/mp4'}.get(path.suffix.lower(), mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
