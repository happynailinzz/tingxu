#!/usr/bin/env python3
"""Isolated model execution for the local WebUI; configuration comes from a private snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from qs668_web import BASE, Library, config_hash, load_module, metadata, validate_stem, private_json


MODEL_CACHE = {}


def progress(message):
    print(message, flush=True)


def annotate(path, fields):
    text = path.read_text(encoding='utf-8')
    end = text.find('\n---', 4)
    front, body = text[4:end], text[end:]
    for key, value in fields.items():
        front = '\n'.join(line for line in front.splitlines() if not line.startswith(key + ':'))
        front += '\n' + key + ': ' + json.dumps(value, ensure_ascii=False)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        tmp.write_text('---\n' + front + body, encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def prepare_audio(audio):
    if audio.suffix.lower() == '.wav':
        return audio
    wav = audio.with_suffix('.wav')
    if wav.is_file() and wav.stat().st_mtime >= audio.stat().st_mtime:
        return wav
    progress('正在准备 16 kHz 音频…')
    temp = wav.with_name('.' + wav.stem + '.' + uuid.uuid4().hex + '.wav')
    try:
        if audio.suffix.lower() == '.opus':
            from qs668_opuslib import opus_to_wav
            opus_to_wav(audio, temp)
        else:
            subprocess.run(['ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-i', str(audio),
                            '-ar', '16000', '-ac', '1', str(temp)], check=True, timeout=1800)
        os.replace(temp, wav)
    finally:
        temp.unlink(missing_ok=True)
    return wav


def remote_transcribe(audio, cfg):
    # Compatible endpoints accept multipart file/model/response_format fields.
    # Limit memory use explicitly; split long files in the UI's local workflow instead.
    if audio.stat().st_size > 100 * 1024 * 1024:
        raise ValueError('云端转写单文件上限 100 MB；请切分音频或使用本地模型（服务商可能有更低限制）')
    boundary = 'qs668-' + uuid.uuid4().hex
    parts = []
    for key, value in {'model': cfg['model'], 'response_format': cfg['response_format']}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    # Stable ASCII upload name; the original filename remains in the local archive.
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
    parts.extend([audio.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
    req = urllib.request.Request(cfg['base_url'].rstrip('/') + '/audio/transcriptions',
          data=b''.join(parts), headers={'Authorization': 'Bearer ' + cfg.get('api_key', ''),
          'Content-Type': 'multipart/form-data; boundary=' + boundary})
    progress('正在等待转写服务返回；此阶段没有可用的逐秒进度…')
    with urllib.request.urlopen(req, timeout=1800) as response:
        result = json.loads(response.read())
    segments = result.get('segments') or []
    if segments:
        return [{'start': float(s.get('start', 0)) * 1000,
                 'end': float(s.get('end', 0)) * 1000, 'spk': s.get('speaker'),
                 'text': s.get('text', '').strip()} for s in segments if s.get('text', '').strip()]
    if result.get('text', '').strip():
        return [{'start': 0, 'end': 0, 'spk': None, 'text': result['text'].strip()}]
    raise ValueError('转写服务返回空文本，未覆盖原转写')


def transcribe(stem, cfg, force=False):
    library = Library()
    candidates = [f for f in library.files() if f['stem'] == stem]
    if not candidates or not candidates[0]['name']:
        raise ValueError('没有可转写的音频')
    audio = prepare_audio(library.resolve('local', candidates[0]['name']))
    trans = load_module(BASE / 'qs668-funasr-transcribe.py', 'web_transcriber')
    from qs668_personalization import correct_text, hotword_argument
    vocab = cfg.get('vocabulary', {})
    trans.NORM_MAP = []  # WebUI corrections are governed only by its explicit switch.
    # Override legacy defaults so disabling vocabulary has an actual effect.
    trans.HOTWORDS = [(w['word'], w['weight']) for w in vocab.get('words', [])
                      if vocab.get('enabled') and w.get('enabled', True)]
    corpus = library.corpus / (stem + '.md')
    fingerprint = config_hash(cfg)
    if not force and trans.should_skip(corpus, audio) and metadata(corpus).get('web_config_sha256') == fingerprint:
        progress('SKIP 转写已匹配当前音频和模型设置')
        return
    if cfg['mode'] == 'remote':
        utterances = remote_transcribe(audio, cfg)
        model_id, provider = cfg['model'], 'compatible-audio-api'
    else:
        progress('正在加载本地模型（首次使用可能下载模型）…')
        model_id = cfg['model'] if cfg['profile'] == 'custom' else cfg['profile']
        cache_key = config_hash(cfg)
        if cache_key not in MODEL_CACHE:
            if cfg['profile'] == 'custom':
                from funasr import AutoModel
                MODEL_CACHE[cache_key] = AutoModel(model=cfg['model'], vad_model='fsmn-vad', device='cpu',
                                  ncpu=4, disable_update=True, trust_remote_code=False)
            else:
                MODEL_CACHE[cache_key] = trans.load_model(cfg['profile'])
        model = MODEL_CACHE[cache_key]
        progress('正在识别音频；模型未提供可靠百分比，请查看日志…')
        generation = {'batch_size_s': 300}
        hotword = hotword_argument(vocab)
        if hotword and cfg['profile'] != 'sensevoice':
            generation['hotword'] = hotword
            progress('已传入自定义热词；实际偏置效果取决于模型能力')
        result = model.generate(str(audio), **generation)
        utterances = trans._extract(result, audio)
        provider = 'funasr-local'
    if not utterances:
        raise ValueError('未识别出有效人声，未覆盖原转写')
    for utterance in utterances:
        utterance['text'] = correct_text(utterance['text'], vocab)
    # Persist exact millisecond boundaries before the display merge mutates segments.
    raw_segments = [{k: u[k] for k in ('start', 'end', 'spk', 'text')} for u in utterances]
    start = trans.parse_recording_start(audio)
    text, has_speaker = trans.build_corpus_lines(utterances, start)
    # Write to a staging folder so a failed inference never truncates the archive.
    staging = BASE / '.web-tasks' / uuid.uuid4().hex
    staging.mkdir(parents=True)
    try:
        out = trans.write_corpus(audio, text, staging, trans._probe_duration(audio), start,
                                 trans.sha256_file(audio), has_speaker)
        annotate(out, {'model': model_id, 'provider': provider, 'web_config_sha256': fingerprint})
        library.corpus.mkdir(parents=True, exist_ok=True)
        os.replace(out, corpus)
        from qs668_personalization import Personalization
        people = Personalization(BASE)
        private_json(people.segments_path(stem), {
            'audio_sha256': trans.sha256_file(audio), 'segments': raw_segments})
        # Speaker indices may change between runs; never silently reuse old identities.
        if people.roles_path(stem).exists():
            previous = people.roles_path(stem).with_name('tingxu-roles-previous-' + uuid.uuid4().hex + '.json')
            os.replace(people.roles_path(stem), previous)
        people.roles_path(stem).with_name('voice-suggestions.json').unlink(missing_ok=True)
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
    progress(f'DONE {stem}：{len(utterances)} 段转写')


def summarize(stem, cfg, force=False):
    library = Library()
    corpus = library.content(stem, 'corpus')
    brief = library.brief / (stem + '.md')
    digest = hashlib.sha256(corpus.read_bytes()).hexdigest()
    from qs668_personalization import Personalization
    people = Personalization(BASE)
    roles = people.recording(stem)['resolved']
    effective_cfg = dict(cfg, confirmed_roles=roles)
    meta = metadata(brief)
    if not force and meta.get('source_corpus_sha256') == digest and meta.get('web_config_sha256') == config_hash(effective_cfg):
        progress('SKIP 纪要已匹配当前转写、模型和提示词')
        return
    summary = load_module(BASE / 'qs668-summarize.py', 'web_summarizer')
    progress('正在生成纪要；服务返回前显示等待状态…')
    summary.summarize_one(corpus, library.brief, False, effective_cfg)
    annotate(brief, {'source_corpus_sha256': digest, 'web_config_sha256': config_hash(effective_cfg)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['transcribe', 'summarize'])
    parser.add_argument('stems', nargs='+')
    parser.add_argument('--settings', required=True)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    for stem in args.stems:
        validate_stem(stem)
    cfg = json.loads(Path(args.settings).read_text())
    for index, stem in enumerate(args.stems):
        print('@qs668-progress ' + json.dumps({'completed': index, 'total': len(args.stems), 'stem': stem}), flush=True)
        if args.action == 'transcribe':
            transcribe(stem, cfg['asr'], args.force)
        else:
            summarize(stem, cfg['summary'], args.force)
        print('@qs668-progress ' + json.dumps({'completed': index + 1, 'total': len(args.stems), 'stem': stem}), flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'ERROR {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
