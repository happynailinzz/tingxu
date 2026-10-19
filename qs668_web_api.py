"""HTTP routes and serialized workflow jobs for qs668-console."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from qs668_web import Library, AUDIO_EXTS, metadata, private_json, save_settings, settings, validate_stem, mime
from qs668_personalization import Personalization, vocabulary, save_vocabulary, finite_number, identity

TASK_LOCK = threading.Lock()
REQUEST_LOCK = threading.RLock()


def cards(console):
    collector = console._collector_mod()
    entries = []
    for volume in collector.wired_volumes():
        # The physical recorder stores files in record/. Avoid unrelated disks.
        if not (volume / 'record').is_dir():
            continue
        try:
            for p in collector.card_files(volume):
                info = p.stat()
                entries.append({'id': volume.name + ':' + str(p.relative_to(volume)),
                    'volume': volume.name, 'name': str(p.relative_to(volume)), 'stem': p.stem,
                    'size': info.st_size, 'modified': info.st_mtime})
        except OSError:
            # Unplugging during a scan should not hide the local library.
            continue
    return entries


def run_workflow(console, job, body, cfg):
    library = Library(console.BASE)
    snapshot = console.BASE / '.web-tasks' / (job.id + '.json')
    private_json(snapshot, cfg)
    try:
        with TASK_LOCK:
            job.add('任务开始执行')
            action = body.get('action', 'pipeline')
            stems = list(dict.fromkeys(body.get('stems', [])))
            selected = body.get('cards', [])
            stages = []
            if action in {'collect', 'pipeline'} and (selected or not stems):
                stages.append('采集')
            if action in {'transcribe', 'pipeline'}:
                stages.append('转写')
            if action in {'summarize', 'pipeline'}:
                stages.append('总结')
            if action == 'publish' and not cfg['feishu']['enabled']:
                raise ValueError('请先在“模型与提示词”中启用飞书文档')
            if action == 'publish' or (action == 'pipeline' and cfg['feishu']['enabled'] and cfg['feishu']['auto_upload']):
                stages.append('上传飞书')
            job.stages = [{'label': s, 'status': 'pending', 'completed': 0, 'total': 0} for s in stages]
            for index, stage in enumerate(job.stages):
                job.current_stage = index
                stage['status'] = 'running'
                job.message = stage['label'] + '准备中'
                if stage['label'] == '采集':
                    available = {c['id']: c for c in cards(console)}
                    chosen = [available[c] for c in selected if c in available] if selected else list(available.values())
                    if selected and len(chosen) != len(selected):
                        raise ValueError('部分卡片文件已不可用，请刷新后重试')
                    stage['total'] = len(chosen)
                    if not chosen:
                        raise ValueError('未发现已挂载录音卡，请使用蓝牙操作或导入本地音频')
                    collector = console._collector_mod()
                    for item in chosen:
                        job.message = '采集 ' + item['name']
                        source = library.resolve('card', item['name'], item['volume'])
                        dest = library.audio / source.name
                        library.audio.mkdir(parents=True, exist_ok=True)
                        # Do not replace a same-stem recording with unrelated data.
                        if dest.exists():
                            if collector.sha256_file(source) != collector.sha256_file(dest):
                                raise ValueError(f'{source.name} 与本地同名音频不同，请先处理本地副本')
                            job.add('SKIP 已采集 ' + source.name)
                        else:
                            with source.open('rb') as stream:
                                library.import_file(source.name, stream, source.stat().st_size)
                            try:
                                if collector.sha256_file(source) != collector.sha256_file(dest):
                                    raise ValueError('复制校验失败，请重新插卡后再试')
                            except Exception:
                                dest.unlink(missing_ok=True)
                                raise
                            job.add('DONE 采集 ' + source.name)
                        digest = collector.sha256_file(dest)
                        probe = collector.probe_opus(dest) if dest.suffix.lower() == '.opus' else 'n/a'
                        mf_path = library.base / 'collected-manifest.json'
                        from qs668_web import read_json
                        manifest = read_json(mf_path, {'files': {}})
                        manifest.setdefault('files', {})[source.stem] = {
                            'status': 'done' if probe in {'n/a', 'clean', 'contaminated'} else 'suspect',
                            'source': 'wired', 'volume': item['volume'],
                            'path': str(dest.relative_to(library.base)), 'size': dest.stat().st_size,
                            'sha256': digest, 'opus_probe': probe,
                            'collected_at': time.strftime('%Y-%m-%d %H:%M:%S')}
                        manifest['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
                        private_json(mf_path, manifest)
                        if probe not in {'n/a', 'clean', 'contaminated'}:
                            raise ValueError('已保留采集文件，但音频完整性可疑：' + probe)
                        if source.stem not in stems:
                            stems.append(source.stem)
                        stage['completed'] += 1
                else:
                    if not stems:
                        raise ValueError('请先选择本地录音')
                    stage['total'] = len(stems)
                    job.message = stage['label'] + ' ' + stems[0]
                    if stage['label'] == '上传飞书':
                        if not cfg['feishu']['enabled']:
                            raise ValueError('请先在“模型与提示词”中启用飞书文档')
                        if not stems:
                            raise ValueError('请先选择本地录音')
                        stage['total'] = len(stems)
                        links = []
                        job.result = {'stems': stems, 'feishu': links}
                        for stem in stems:
                            brief = library.content(stem, 'brief')
                            title = library.labels().get(stem, {}).get('title', stem)
                            link = upload_feishu(cfg['feishu']['cli'], brief, title, console.BASE)
                            library.save_feishu_link(stem, link)
                            links.append({'stem': stem, 'url': link})
                            job.add(f'DONE 飞书文档 {stem}: {link}')
                            stage['completed'] += 1
                        job.result = {'stems': stems, 'feishu': links}
                        stage['status'] = 'done'
                        continue
                    task = 'transcribe' if stage['label'] == '转写' else 'summarize'
                    cmd = [console.PY, '-u', str(console.BASE / 'qs668-web-task.py'), task,
                           *stems, '--settings', str(snapshot)]
                    if body.get('force'):
                        cmd.append('--force')
                    rc = console._stream_step(job, job.message, cmd, str(console.BASE))
                    if rc:
                        raise RuntimeError(f"{stage['label']}失败（exit={rc}），查看日志后可重试")
                    stage['completed'] = len(stems)
                    # Preserve the existing per-recording session links and audit log.
                    try:
                        from qs668_web import load_module
                        sessions = load_module(console.BASE / 'qs668-session.py', 'web_sessions')
                        for stem in stems:
                            if task == 'transcribe':
                                sessions.open_session(stem, transcribe_meta={
                                    'source': 'webui', 'model': metadata(library.corpus / (stem + '.md')).get('model', '')})
                            else:
                                meta = metadata(library.brief / (stem + '.md'))
                                sessions.attach_brief(stem, meta.get('provider', ''), meta.get('model', ''),
                                                      'custom' if cfg['summary']['prompt'] else '智能总结')
                    except Exception as exc:
                        job.add('会话关联未更新：' + str(exc))
                stage['status'] = 'done'
            job.status = 'done'
            job.message = '全部阶段完成'
            if not job.result or 'feishu' not in job.result:
                job.result = {'stems': stems}
    except Exception:
        for stage in getattr(job, 'stages', []):
            if stage['status'] == 'running':
                stage['status'] = 'failed'
        raise
    finally:
        snapshot.unlink(missing_ok=True)


def upload_feishu(cli, markdown_path, title, cwd):
    """Publish only the generated Markdown brief through the official Lark CLI."""
    executable = shutil.which(cli) if os.path.sep not in cli else cli
    if not executable or not Path(executable).is_file():
        raise RuntimeError(f'找不到飞书 CLI：{cli}。请安装 larksuite/cli 并完成登录授权')
    if not markdown_path.is_file():
        raise ValueError(f'纪要文件不存在：{markdown_path.name}')
    markdown_path = markdown_path.resolve()
    cwd = Path(cwd).resolve()
    if not markdown_path.is_relative_to(cwd):
        raise ValueError('纪要文件路径不在工作目录内')
    content = markdown_path.read_text(encoding='utf-8')
    # The archive frontmatter tracks models and fingerprints, not document text.
    content = re.sub(r'\A\ufeff?---[^\S\n]*\n.*?\n---[^\S\n]*(?:\n|\Z)',
                     '', content, count=1, flags=re.DOTALL).lstrip('\n')
    command = [executable, 'docs', '+create', '--as', 'user', '--title', title,
               '--doc-format', 'markdown', '--content', '-']
    # npm's lark-cli uses /usr/bin/env node. launchd does not inherit the
    # interactive shell's nvm PATH, even when the CLI has an absolute path.
    env = os.environ.copy()
    env['PATH'] = str(Path(executable).absolute().parent) + os.pathsep + env.get('PATH', os.defpath)
    env['LARKSUITE_CLI_NO_UPDATE_NOTIFIER'] = '1'
    env['LARKSUITE_CLI_NO_SKILLS_NOTIFIER'] = '1'
    result = subprocess.run(command, cwd=cwd, input=content, capture_output=True,
                            env=env, text=True, timeout=120, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise RuntimeError('飞书文档创建失败：' + (detail or f'exit={result.returncode}'))
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError('飞书 CLI 未返回有效 JSON，请检查 CLI 版本') from exc
    def find_url(value):
        if isinstance(value, dict):
            if value.get('ok') is False:
                return None
            for key in ('url', 'document_url', 'doc_url'):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.startswith(('https://', 'http://')):
                    return candidate
            for child in value.values():
                found = find_url(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find_url(child)
                if found:
                    return found
        return None
    url = find_url(payload)
    if not url:
        raise RuntimeError('文档可能已创建，但 CLI 响应中没有文档链接')
    return url


def active(console):
    with console.JOBS_LOCK:
        return any(j.status == 'running' for j in console.JOBS.values())


def require_idle(console):
    if active(console):
        raise ValueError('有任务正在运行，请完成后再进行文件修改或启动新任务')


def send_file(handler, path, ranged=False, download=False):
    size = path.stat().st_size
    start, end = 0, size - 1
    partial = False
    if ranged and handler.headers.get('Range'):
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', handler.headers['Range'])
        if not match or not size:
            handler.send_response(416); handler.send_header('Content-Range', f'bytes */{size}'); handler.end_headers(); return
        a, b = match.groups()
        if not a and not b:
            handler.send_response(416); handler.end_headers(); return
        start = int(a) if a else max(0, size - int(b))
        end = min(int(b), size - 1) if a and b else size - 1
        if start >= size or start > end:
            handler.send_response(416); handler.send_header('Content-Range', f'bytes */{size}'); handler.end_headers(); return
        partial = True
    handler.send_response(206 if partial else 200)
    handler.send_header('Content-Type', mime(path) if ranged else 'text/markdown; charset=utf-8')
    handler.send_header('Content-Length', str(max(0, end - start + 1)))
    handler.send_header('Accept-Ranges', 'bytes')
    handler.send_header('Cache-Control', 'no-store')
    if partial:
        handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    if download:
        handler.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(path.name))
    handler.end_headers()
    try:
        with path.open('rb') as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(left, 262144))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                left -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass


def get(handler, console):
    query = urlparse(handler.path)
    route = query.path
    q = {k: v[0] for k, v in parse_qs(query.query).items()}
    library = Library(console.BASE)
    try:
        if route in {'/', '/app.js', '/style.css', '/personalization.js', '/ble-flow.js', '/feishu-docs.jpg'}:
            name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/personalization.js': 'personalization.js', '/ble-flow.js': 'ble-flow.js', '/feishu-docs.jpg': 'feishu-docs.jpg'}[route]
            ctype = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8', '/style.css': 'text/css; charset=utf-8', '/personalization.js': 'text/javascript; charset=utf-8', '/ble-flow.js': 'text/javascript; charset=utf-8', '/feishu-docs.jpg': 'image/jpeg'}[route]
            handler._send(200, (console.BASE / 'webui' / name).read_bytes(), ctype)
        elif route == '/api/library':
            handler._json({'local': library.files(), 'card': cards(console), 'trash': library.trash_items()})
        elif route == '/api/content':
            path = library.content(q.get('stem'), q.get('kind'))
            if q.get('download'):
                send_file(handler, path, download=True)
            else:
                text = path.read_text(encoding='utf-8')
                if q.get('kind') == 'corpus':
                    text = Personalization(console.BASE).apply_roles(q.get('stem'), text)
                handler._json({'text': text, 'meta': metadata(path)})
        elif route == '/api/audio':
            send_file(handler, library.resolve(q.get('source'), q.get('name'), q.get('volume')), ranged=True, download=q.get('download') == '1')
        elif route == '/api/people':
            handler._json(Personalization(console.BASE).public())
        elif route == '/api/vocabulary':
            handler._json(vocabulary(console.BASE))
        elif route == '/api/roles':
            handler._json(Personalization(console.BASE).recording(q.get('stem')))
        elif route == '/api/voice-sample':
            person_id, sample_id = identity(q.get('person')), identity(q.get('sample'))
            data = Personalization(console.BASE).public()
            if not any(p['id'] == person_id and any(s['id'] == sample_id for s in p['samples']) for p in data['people']):
                raise FileNotFoundError('样本不存在')
            send_file(handler, console.BASE / '.voiceprints' / person_id / (sample_id + '.wav'), ranged=True)
        elif route == '/api/models':
            handler._json(settings(console.BASE, public=True))
        elif route == '/api/tasks':
            with console.JOBS_LOCK:
                tasks = [j.snapshot(0) for j in reversed(list(console.JOBS.values()))]
            handler._json({'tasks': tasks})
        elif route == '/api/default-prompt':
            from qs668_web import load_module
            handler._json({'prompt': load_module(console.SUMMARIZE, 'web_prompt').PROMPT_SYS})
        else:
            return False
    except (ValueError, KeyError, TypeError) as exc:
        handler._json({'error': str(exc)}, 400)
    except FileNotFoundError as exc:
        handler._json({'error': str(exc)}, 404)
    except Exception as exc:
        handler._json({'error': type(exc).__name__ + ': ' + str(exc)}, 500)
    return True


def post(handler, console, body):
    route = urlparse(handler.path).path
    library = Library(console.BASE)
    try:
        if route == '/api/people':
            require_idle(console)
            handler._json(Personalization(console.BASE).save_person(body))
        elif route == '/api/people/options':
            require_idle(console)
            handler._json(Personalization(console.BASE).options(body))
        elif route == '/api/people/remove':
            require_idle(console)
            handler._json(Personalization(console.BASE).remove(body.get('id'), body.get('sample')))
        elif route == '/api/vocabulary':
            handler._json(save_vocabulary(body, console.BASE))
        elif route == '/api/roles':
            require_idle(console)
            handler._json(Personalization(console.BASE).save_roles(body.get('stem'), body.get('roles')))
        elif route == '/api/voice-task':
            require_idle(console)
            if body.get('action') not in {'enroll', 'match'}:
                raise ValueError('未知声纹操作')
            validate_stem(body.get('stem'))
            Library(console.BASE).content(body['stem'], 'corpus') if body['action'] == 'match' else None
            if body['action'] == 'enroll':
                identity(body.get('person_id'))
                start = finite_number(body.get('start'), '起点', 0, 86400 * 30)
                finite_number(body.get('end'), '终点', start + 3, start + 30)
            job = console.new_job('voice')
            threading.Thread(target=console._job_wrap, args=(job, run_voice_task, (console, job, body)), daemon=True).start()
            handler._json({'job_id': job.id})
        elif route == '/api/models':
            handler._json(save_settings(body, console.BASE))
        elif route == '/api/workflow':
            if body.get('action') not in {'collect', 'transcribe', 'summarize', 'pipeline', 'publish'}:
                raise ValueError('未知任务类型')
            if not isinstance(body.get('stems', []), list) or not isinstance(body.get('cards', []), list):
                raise ValueError('文件列表格式不正确')
            for stem in body.get('stems', []):
                validate_stem(stem)
            require_idle(console)
            cfg = settings(console.BASE)
            cfg['asr']['vocabulary'] = vocabulary(console.BASE)
            job = console.new_job(body['action'])
            job.stages = []
            job.message = '等待执行'
            threading.Thread(target=console._job_wrap, args=(job, run_workflow, (console, job, body, cfg)), daemon=True).start()
            handler._json({'job_id': job.id})
        elif route == '/api/library/title':
            require_idle(console)
            library.title(body.get('stem'), body.get('title'))
            handler._json({'ok': True})
        elif route == '/api/library/recycle':
            require_idle(console)
            stems = body.get('stems', [])
            if not isinstance(stems, list) or not stems:
                raise ValueError('请选择录音')
            handler._json({'ok': True, 'batch': library.recycle(list(dict.fromkeys(stems)))})
        elif route == '/api/library/restore':
            require_idle(console)
            library.restore(body.get('id'))
            handler._json({'ok': True})
        else:
            return False
    except (ValueError, KeyError, TypeError) as exc:
        handler._json({'error': str(exc)}, 400)
    except FileNotFoundError as exc:
        handler._json({'error': str(exc)}, 404)
    except FileExistsError as exc:
        handler._json({'error': str(exc)}, 409)
    except Exception as exc:
        handler._json({'error': type(exc).__name__ + ': ' + str(exc)}, 500)
    return True


def run_voice_task(console, job, body):
    request = console.BASE / '.web-tasks' / (job.id + '.voice.json')
    private_json(request, body)
    try:
        label = '登记声纹' if body['action'] == 'enroll' else '匹配声纹'
        job.stages = [{'label': label, 'status': 'running', 'completed': 0, 'total': 1}]
        job.message = label
        rc = console._stream_step(job, label, [console.PY, '-u', str(console.BASE / 'qs668-voice-task.py'),
                                  body['action'], '--request', str(request)], str(console.BASE))
        job.stages[0].update(status='done' if rc == 0 else 'failed', completed=1 if rc == 0 else 0)
        if rc:
            raise RuntimeError(label + '失败，请查看任务日志')
        job.message = label + '完成'
    finally:
        request.unlink(missing_ok=True)
