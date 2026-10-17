#!/usr/bin/env python3
"""CAMPPlus enrollment and suggested identification, run in the FunASR environment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path

from qs668_web import BASE, Library, load_module, private_json, read_json
from qs668_personalization import Personalization, VOICE_MODEL, VOICE_VERSION, finite_number, identity, normalize, rank_voice

MODEL = None


def extract_embedding(audio):
    global MODEL
    if MODEL is None:
        print('正在加载本地 CAMPPlus 声纹模型…', flush=True)
        from funasr import AutoModel
        MODEL = AutoModel(model=VOICE_MODEL, device='cpu', ncpu=4,
                          disable_update=True, trust_remote_code=False)
    result = MODEL.generate(str(audio))
    if isinstance(result, dict):
        result = [result]
    value = result[0].get('spk_embedding') if result else None
    if value is None:
        raise ValueError('声纹模型没有返回 embedding')
    if hasattr(value, 'detach'):
        value = value.detach().cpu().numpy()
    if hasattr(value, 'reshape'):
        value = value.reshape(-1).tolist()
    elif value and isinstance(value[0], list):
        value = value[0]
    return normalize(value)


def audio_for(stem):
    library = Library()
    entry = next((f for f in library.files() if f['stem'] == stem and f['name']), None)
    if entry is None:
        raise FileNotFoundError('本地音频不存在')
    task = load_module(BASE / 'qs668-web-task.py', 'voice_audio_preparation')
    return task.prepare_audio(library.resolve('local', entry['name']))


def cut_audio(audio, start, end, out):
    start = finite_number(start, '起点秒数', 0, 86400 * 30)
    end = finite_number(end, '终点秒数', start + 3, start + 30)
    with wave.open(str(audio), 'rb') as source:
        duration = source.getnframes() / source.getframerate()
    if end > duration + .01:
        raise ValueError('样本终点超出音频时长')
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-ss', str(start),
                    '-i', str(audio), '-t', str(end - start), '-ar', '16000', '-ac', '1',
                    '-acodec', 'pcm_s16le', str(out)], check=True, timeout=90)
    # Reject silence and sub-three-second output before putting it in a profile.


def validate_clip(out):
    import numpy as np
    with wave.open(str(out), 'rb') as clip:
        if clip.getnframes() / clip.getframerate() < 2.99:
            raise ValueError('有效样本不足 3 秒')
        data = np.frombuffer(clip.readframes(clip.getnframes()), dtype='<i2').astype(float)
    if not len(data) or float(np.sqrt(np.mean(data ** 2))) < 30:
        raise ValueError('样本接近静音，请选择清晰的单人讲话片段')


def enroll(body):
    store = Personalization()
    person_id = identity(body.get('person_id'))
    data = store.data()
    person = next((p for p in data['people'] if p['id'] == person_id), None)
    if person is None:
        raise FileNotFoundError('人物档案不存在')
    if len(person['samples']) >= 5:
        raise ValueError('每个人最多 5 个样本，请先移除低质量样本')
    audio = audio_for(body['stem'])
    sample_id = uuid.uuid4().hex
    folder = BASE / '.voiceprints' / person_id
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    temp = folder / ('.' + sample_id + '.wav')
    out = folder / (sample_id + '.wav')
    try:
        cut_audio(audio, body['start'], body['end'], temp)
        validate_clip(temp)
        embedding = extract_embedding(temp)
        os.chmod(temp, 0o600)
        os.replace(temp, out)
        person['samples'].append({'id': sample_id, 'stem': body['stem'], 'start': float(body['start']),
            'end': float(body['end']), 'created': time.time(), 'version': VOICE_VERSION, 'embedding': embedding})
        private_json(store.path, data)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)
    print(f"已登记 {person['name']} 的声纹样本（仅在本地保存）", flush=True)


def match(body):
    store = Personalization()
    stem = body['stem']
    recording = store.recording(stem)
    if not recording['has_segments']:
        raise ValueError('缺少精确分段，请先用新版执行一次支持说话人分离的转写')
    audio = audio_for(stem)
    raw = read_json(store.segments_path(stem))
    digest = hashlib.sha256()
    with audio.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != raw['audio_sha256']:
        raise ValueError('音频已变化，请重新转写后匹配')
    data = store.data()
    if not any(p['samples'] for p in data['people']):
        raise ValueError('请先登记至少一个人物的声纹样本')
    suggestions = []
    with tempfile.TemporaryDirectory(prefix='tingxu-voice-') as tmp:
        for speaker in recording['speakers']:
            # Use longest clean non-overlapping utterances, never guessed clock offsets.
            candidates = []
            all_segments = raw.get('segments', [])
            for segment in all_segments:
                if str(segment.get('spk')) != speaker:
                    continue
                start, end = float(segment['start']) / 1000, float(segment['end']) / 1000
                if end - start < 3:
                    continue
                overlap = any(str(other.get('spk')) != speaker and
                    float(other['start']) / 1000 < end and float(other['end']) / 1000 > start
                    for other in all_segments if other.get('spk') is not None)
                if not overlap:
                    candidates.append((start, min(end, start + 30)))
            candidates.sort(key=lambda p: p[1] - p[0], reverse=True)
            vectors = []
            for index, (start, end) in enumerate(candidates[:3]):
                out = Path(tmp) / f'{index}.wav'
                cut_audio(audio, start, end, out)
                try:
                    validate_clip(out)
                except ValueError:
                    continue
                vectors.append(extract_embedding(out))
            if not vectors:
                suggestions.append({'speaker': speaker, 'suggested': False, 'candidate': None,
                                     'reason': '没有足够的单人清晰片段（至少 3 秒）'})
                continue
            vector = normalize([sum(v) / len(vectors) for v in zip(*vectors)])
            suggestions.append({'speaker': speaker, **rank_voice(vector, data['people'], data['threshold'], data['margin'])})
            print('已分析说话人' + speaker, flush=True)
    private_json(store.roles_path(stem).with_name('voice-suggestions.json'), {
        'suggestions': suggestions, 'audio_sha256': raw['audio_sha256'], 'created': time.time(),
        'threshold': data['threshold'], 'margin': data['margin']})
    print('声纹建议已生成，请在录音详情中人工确认；没有自动替换姓名。', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['enroll', 'match'])
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    body = json.loads(Path(args.request).read_text())
    (enroll if args.action == 'enroll' else match)(body)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'ERROR {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
