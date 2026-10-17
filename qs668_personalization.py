"""Tingxu people, voiceprint metadata, vocabulary, and per-recording role maps.

This module uses the standard library. Inference lives in qs668-voice-task.py.
"""
from __future__ import annotations

import copy
import math
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from qs668_web import BASE, Library, config_hash, load_module, private_json, read_json, safe_child, validate_stem

STORE_LOCK = threading.RLock()
VOICE_MODEL = 'cam++'
VOICE_VERSION = 'funasr-campplus-192-v1'


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('无效档案标识')
    return value


def bounded_text(value, label, limit, empty=False):
    if not isinstance(value, str) or len(value.strip()) > limit or (not empty and not value.strip()):
        raise ValueError(f'{label}须为{0 if empty else 1}–{limit}个字符')
    return value.strip()


def finite_number(value, label, low, high):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label}须为数字') from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f'{label}须在 {low}–{high} 之间')
    return number


def default_vocabulary(base=BASE):
    trans = load_module(base / 'qs668-funasr-transcribe.py', 'vocabulary_defaults')
    return {'enabled': True, 'corrections_enabled': False, 'words': [
        {'id': uuid.uuid5(uuid.NAMESPACE_DNS, 'tingxu-hotword:' + word).hex, 'word': word, 'weight': weight, 'group': '现有术语', 'enabled': True}
        for word, weight in trans.HOTWORDS], 'corrections': []}


def vocabulary(base=BASE):
    return read_json(base / '.tingxu-vocabulary.json', None) if (base / '.tingxu-vocabulary.json').is_file() else default_vocabulary(base)


def save_vocabulary(payload, base=BASE):
    if not isinstance(payload, dict):
        raise ValueError('热词设置须为对象')
    if not isinstance(payload.get('enabled'), bool) or not isinstance(payload.get('corrections_enabled'), bool):
        raise ValueError('热词和纠错开关须为布尔值')
    result = {'enabled': payload['enabled'], 'corrections_enabled': payload['corrections_enabled'], 'words': [], 'corrections': []}
    words, corrections = payload.get('words', []), payload.get('corrections', [])
    if not isinstance(words, list) or len(words) > 500 or not isinstance(corrections, list) or len(corrections) > 300:
        raise ValueError('最多 500 个热词和 300 条纠错规则')
    seen = set()
    for item in words:
        if not isinstance(item, dict):
            raise ValueError('热词条目格式无效')
        word = bounded_text(item.get('word'), '热词', 80)
        if '\n' in word or '\r' in word:
            raise ValueError('热词不可包含换行')
        if word in seen:
            raise ValueError('热词重复：' + word)
        seen.add(word)
        if not isinstance(item.get('enabled', True), bool):
            raise ValueError('词条开关须为布尔值')
        result['words'].append({'id': item.get('id') if re.fullmatch(r'[a-f0-9]{32}', str(item.get('id', ''))) else uuid.uuid4().hex,
            'word': word, 'weight': finite_number(item.get('weight', 20), '权重', 1, 100),
            'group': bounded_text(item.get('group', ''), '分组', 40, empty=True), 'enabled': item.get('enabled', True)})
    seen = set()
    for item in corrections:
        if not isinstance(item, dict):
            raise ValueError('纠错条目格式无效')
        source = bounded_text(item.get('from'), '原词', 80)
        target = bounded_text(item.get('to'), '规范词', 80)
        if source == target or source in seen or any(c in source + target for c in '\n\r'):
            raise ValueError('纠错规则重复、相同或包含换行')
        if not isinstance(item.get('enabled', True), bool):
            raise ValueError('规则开关须为布尔值')
        seen.add(source)
        result['corrections'].append({'from': source, 'to': target, 'enabled': item.get('enabled', True)})
    with STORE_LOCK:
        private_json(base / '.tingxu-vocabulary.json', result)
    return result


def hotword_argument(vocab):
    if not vocab.get('enabled'):
        return ''
    return ' '.join(f"{w['word']} {float(w['weight']):g}" for w in vocab.get('words', []) if w.get('enabled', True))


def correct_text(text, vocab):
    if not vocab.get('corrections_enabled'):
        return text
    mapping = {r['from']: r['to'] for r in vocab.get('corrections', []) if r.get('enabled', True)}
    if not mapping:
        return text
    # One literal pass, longest first: no regex input and no cascading replacements.
    pattern = '|'.join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
    return re.sub(pattern, lambda m: mapping[m.group()], text)


class Personalization:
    def __init__(self, base=BASE):
        self.base = Path(base).resolve()
        self.path = self.base / '.tingxu-people.json'

    def data(self):
        return read_json(self.path, {'people': [], 'threshold': 0.65, 'margin': 0.08})

    def public(self):
        data = self.data()
        for person in data['people']:
            for sample in person.get('samples', []):
                sample.pop('embedding', None)
        return data

    def save_person(self, payload):
        name = bounded_text(payload.get('name'), '姓名或角色', 60)
        note = bounded_text(payload.get('note', ''), '备注', 300, empty=True)
        with STORE_LOCK:
            data = self.data()
            person_id = identity(payload['id']) if payload.get('id') else uuid.uuid4().hex
            person = next((p for p in data['people'] if p['id'] == person_id), None)
            if payload.get('id') and person is None:
                raise FileNotFoundError('人物档案不存在')
            if any(p['name'] == name and p['id'] != person_id for p in data['people']):
                raise ValueError('人物名称已存在')
            if person is None:
                person = {'id': person_id, 'samples': [], 'created': time.time()}
                data['people'].append(person)
            person.update(name=name, note=note)
            private_json(self.path, data)
        return self.public()

    def options(self, payload):
        with STORE_LOCK:
            data = self.data()
            data['threshold'] = finite_number(payload.get('threshold'), '相似度阈值', 0.1, 0.99)
            data['margin'] = finite_number(payload.get('margin'), '候选差距', 0, 0.5)
            private_json(self.path, data)
        return self.public()

    def remove(self, person_id, sample_id=None):
        identity(person_id)
        if sample_id is not None:
            identity(sample_id)
        with STORE_LOCK:
            data = self.data()
            person = next((p for p in data['people'] if p['id'] == person_id), None)
            if person is None:
                raise FileNotFoundError('人物档案不存在')
            if sample_id:
                if not any(s['id'] == sample_id for s in person['samples']):
                    raise FileNotFoundError('样本不存在')
                person['samples'] = [s for s in person['samples'] if s['id'] != sample_id]
                source = self.base / '.voiceprints' / person_id / (sample_id + '.wav')
            else:
                data['people'] = [p for p in data['people'] if p['id'] != person_id]
                source = self.base / '.voiceprints' / person_id
            # Retain removed references locally for recovery; never overwrite originals.
            backup = self.base / '.voiceprints' / '.trash' / uuid.uuid4().hex
            backup.mkdir(parents=True)
            private_json(backup / 'people-before.json', self.data())
            if source.exists():
                shutil.move(str(source), str(backup / source.name))
            private_json(self.path, data)
        return self.public()

    def segments_path(self, stem):
        validate_stem(stem)
        return safe_child(self.base / 'sessions', stem) / 'segments.json'

    def roles_path(self, stem):
        validate_stem(stem)
        return safe_child(self.base / 'sessions', stem) / 'tingxu-roles.json'

    def recording(self, stem):
        corpus = Library(self.base).content(stem, 'corpus')
        text = corpus.read_text(encoding='utf-8')
        ids = sorted(set(re.findall(r'^\[[^\]\n]+\]\s*说话人([^：:\s]+)[:：]', text, re.M)))
        raw = read_json(self.segments_path(stem), {})
        wav = Library(self.base).audio / (stem + '.wav')
        # Segments refer to the original WAV; obsolete offsets are never matched.
        segment_valid = bool(raw.get('audio_sha256')) and wav.is_file()
        if segment_valid:
            from qs668_web import metadata
            segment_valid = metadata(corpus).get('audio_sha256') == raw['audio_sha256']
        roles = read_json(self.roles_path(stem), {'roles': {}})
        people = {p['id']: p['name'] for p in self.data()['people']}
        resolved = {}
        for speaker, role in roles.get('roles', {}).items():
            name = people.get(role.get('person_id')) if role.get('person_id') else role.get('name')
            if name:
                resolved[speaker] = name
        return {'stem': stem, 'speakers': ids, 'roles': roles.get('roles', {}), 'resolved': resolved,
                'has_segments': segment_valid, 'segments': raw.get('segments', []) if segment_valid else [],
                'suggestions': read_json(self.roles_path(stem).with_name('voice-suggestions.json'), {}).get('suggestions', [])}

    def save_roles(self, stem, roles):
        available = set(self.recording(stem)['speakers'])
        people = {p['id'] for p in self.data()['people']}
        if not isinstance(roles, dict):
            raise ValueError('角色表格式无效')
        result = {}
        for speaker, value in roles.items():
            if speaker not in available or not isinstance(value, dict):
                raise ValueError('录音中没有该说话人')
            person_id = value.get('person_id')
            if person_id:
                if person_id not in people:
                    raise ValueError('人物档案不存在')
                result[speaker] = {'person_id': person_id}
            elif value.get('name', '').strip():
                result[speaker] = {'name': bounded_text(value['name'], '角色名称', 60)}
        private_json(self.roles_path(stem), {'roles': result, 'confirmed_at': time.time()})
        return self.recording(stem)

    def apply_roles(self, stem, text):
        roles = self.recording(stem)['resolved']
        return re.sub(r'(^\[[^\]\n]+\]\s*)说话人([^：:\s]+)([:：])',
                      lambda m: m[1] + roles.get(m[2], '说话人' + m[2]) + m[3], text, flags=re.M)


def normalize(vector):
    vector = [float(v) for v in vector]
    if len(vector) != 192 or not all(math.isfinite(v) for v in vector):
        raise ValueError('声纹向量无效，期望 192 维')
    norm = math.sqrt(sum(v * v for v in vector))
    if norm < 1e-10:
        raise ValueError('声纹向量为空')
    return [v / norm for v in vector]


def rank_voice(vector, people, threshold, margin):
    vector = normalize(vector)
    ranked = []
    for person in people:
        samples = [s['embedding'] for s in person.get('samples', []) if s.get('version') == VOICE_VERSION]
        if not samples:
            continue
        centroid = normalize([sum(values) / len(samples) for values in zip(*(normalize(s) for s in samples))])
        score = sum(a * b for a, b in zip(vector, centroid))
        ranked.append({'person_id': person['id'], 'name': person['name'], 'score': round(score, 4)})
    ranked.sort(key=lambda r: r['score'], reverse=True)
    top = ranked[0] if ranked else None
    gap = top['score'] - ranked[1]['score'] if len(ranked) > 1 else 1
    accepted = bool(top and top['score'] >= threshold and gap >= margin)
    return {'candidate': top, 'suggested': accepted, 'candidates': ranked[:3],
            'reason': '待人工确认' if accepted else '无匹配档案或相似度/差距不足'}
