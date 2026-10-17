"""WebUI regression tests; fixtures stay in temporary directories."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import qs668_web as web
import qs668_web_api as api

BASE = Path(__file__).resolve().parents[1]


def module(filename, name):
    spec = importlib.util.spec_from_file_location(name, BASE / filename)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.lib = web.Library(self.base)

    def test_import_title_recycle_restore(self):
        p = self.lib.import_file('note20261017-120000.wav', io.BytesIO(b'audio'), 5)
        stem = p.stem
        self.lib.corpus.mkdir(parents=True)
        (self.lib.corpus / (stem + '.md')).write_text('---\nduration: "00:10"\n---\n[00:00] example')
        session = self.base / 'sessions' / stem
        session.mkdir(parents=True)
        (session / 'audio').symlink_to(p)
        self.lib.title(stem, '会议测试')
        self.assertEqual(self.lib.files()[0]['title'], '会议测试')
        batch = self.lib.recycle([stem])
        self.assertFalse(p.exists())
        self.assertFalse(session.exists())
        self.assertEqual(len(self.lib.trash_items()), 1)
        self.lib.restore(batch)
        self.assertEqual(p.read_bytes(), b'audio')
        self.assertTrue((session / 'audio').exists())
        self.assertEqual(self.lib.files()[0]['duration'], '00:10')

    def test_import_refuses_existing_identity(self):
        self.lib.import_file('meeting.wav', io.BytesIO(b'audio'), 5)
        with self.assertRaises(FileExistsError):
            self.lib.import_file('meeting.mp3', io.BytesIO(b'other'), 5)

    def test_interrupted_upload_has_no_partial_audio(self):
        with self.assertRaises(ValueError):
            self.lib.import_file('meeting.wav', io.BytesIO(b'a'), 5)
        self.assertEqual(list(self.lib.audio.iterdir()), [])

    def test_traversal_and_symlink_escape_blocked(self):
        self.lib.audio.mkdir(parents=True)
        outside = self.base / 'private.wav'
        outside.write_bytes(b'private')
        (self.lib.audio / 'escape.wav').symlink_to(outside)
        for name in ['../../private.wav', 'escape.wav', str(outside)]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.lib.resolve('local', name)

    def test_restore_never_overwrites(self):
        p = self.lib.import_file('meeting.wav', io.BytesIO(b'old'), 3)
        batch = self.lib.recycle(['meeting'])
        p.write_bytes(b'new')
        with self.assertRaises(FileExistsError):
            self.lib.restore(batch)
        self.assertEqual(p.read_bytes(), b'new')

    def test_secret_redaction_preservation_clear(self):
        cfg = {'summary': {'mode': 'custom', 'model': 'test-model',
                'base_url': 'http://127.0.0.1:9000/v1', 'api_key': 'test-secret', 'prompt': '只列行动项'}}
        public = web.save_settings(cfg, self.base)
        self.assertNotIn('test-secret', json.dumps(public))
        self.assertTrue(public['summary']['has_key'])
        self.assertEqual(os.stat(self.base / '.qs668-web-settings.json').st_mode & 0o777, 0o600)
        web.save_settings({'summary': {'api_key': '', 'prompt': '新提示词'}}, self.base)
        self.assertEqual(web.settings(self.base)['summary']['api_key'], 'test-secret')
        web.save_settings({'summary': {'clear_key': True}}, self.base)
        self.assertEqual(web.settings(self.base)['summary']['api_key'], '')

    def test_invalid_model_settings_do_not_save(self):
        with self.assertRaises(ValueError):
            web.save_settings({'asr': {'mode': 'remote', 'base_url': 'file:///etc/passwd', 'model': 'x'}}, self.base)
        self.assertFalse((self.base / '.qs668-web-settings.json').exists())


class TaskTests(unittest.TestCase):
    def test_custom_summary_model_prompt_are_sent(self):
        m = module('qs668-summarize.py', 'test_summary')
        response = Mock()
        response.read.return_value = json.dumps({'choices': [{'message': {'content': '# 行动项\n\n- 完成复核。'}, 'finish_reason': 'stop'}]}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        cfg = {'mode': 'custom', 'base_url': 'http://example.test/v1',
               'api_key': 'test-key', 'model': 'chosen-model', 'prompt': '只输出行动项'}
        with tempfile.TemporaryDirectory() as d, patch.object(m.urllib.request, 'urlopen', return_value=response) as request:
            root = Path(d)
            corpus = root / 'meeting.md'
            corpus.write_text('---\nduration: "00:10"\n---\n[00:01] 明天完成复核。')
            out = root / 'brief'
            self.assertEqual(m.summarize_one(corpus, out, False, cfg), 0)
            sent = json.loads(request.call_args.args[0].data)
            self.assertEqual(sent['model'], 'chosen-model')
            self.assertEqual(sent['messages'][0]['content'], cfg['prompt'])
            self.assertIn('# 行动项', (out / 'meeting.md').read_text())

    def test_remote_asr_multipart_and_segments(self):
        m = module('qs668-web-task.py', 'test_task')
        response = Mock()
        response.read.return_value = json.dumps({'segments': [{'start': 1.2, 'end': 3.4, 'text': '测试'}]}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as d, patch.object(m.urllib.request, 'urlopen', return_value=response) as request:
            audio = Path(d) / 'audio.wav'; audio.write_bytes(b'RIFF-test')
            result = m.remote_transcribe(audio, {'base_url': 'http://example.test/v1',
                 'model': 'chosen-asr', 'api_key': 'test-key', 'response_format': 'verbose_json'})
            self.assertEqual(result[0]['start'], 1200)
            req = request.call_args.args[0]
            self.assertEqual(req.full_url, 'http://example.test/v1/audio/transcriptions')
            self.assertIn(b'chosen-asr', req.data)
            self.assertIn(b'verbose_json', req.data)

    def test_failed_job_is_not_done(self):
        m = module('qs668-console.py', 'test_console')
        job = m.Job('test', 'pipeline')
        def fail():
            raise RuntimeError('expected failure')
        m._job_wrap(job, fail, ())
        self.assertEqual(job.status, 'failed')
        self.assertIn('expected failure', job.message)

    def test_failed_transcription_stops_summary_and_cleans_snapshot(self):
        m = module('qs668-console.py', 'test_console_2')
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            job = m.Job('test', 'pipeline')
            console = SimpleNamespace(BASE=base, PY='python', _stream_step=Mock(return_value=1))
            body = {'action': 'pipeline', 'stems': ['meeting']}
            with self.assertRaises(RuntimeError):
                api.run_workflow(console, job, body, web.DEFAULTS)
            self.assertEqual(console._stream_step.call_count, 1)
            self.assertEqual(job.stages[0]['status'], 'failed')
            self.assertEqual(job.stages[1]['status'], 'pending')
            self.assertEqual(list((base / '.web-tasks').glob('*.json')), [])

    def test_range_playback_and_unsatisfied_range(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'test.wav'; p.write_bytes(b'0123456789')
            handler = SimpleNamespace(headers={'Range': 'bytes=2-5'}, wfile=io.BytesIO(),
                        send_response=Mock(), send_header=Mock(), end_headers=Mock())
            api.send_file(handler, p, ranged=True)
            handler.send_response.assert_called_once_with(206)
            self.assertEqual(handler.wfile.getvalue(), b'2345')
            handler.headers['Range'] = 'bytes=200-'
            handler.send_response.reset_mock()
            api.send_file(handler, p, ranged=True)
            handler.send_response.assert_called_once_with(416)


if __name__ == '__main__':
    unittest.main()

class HTTPTests(unittest.TestCase):
    def test_library_routes_upload_read_recycle_restore_and_origin(self):
        import sys
        m = module('qs668-console.py', 'test_http_console')
        with patch.dict(sys.modules, {m.__name__: m}), tempfile.TemporaryDirectory() as d:
            m.BASE = Path(d)
            (m.BASE / 'webui').symlink_to(BASE / 'webui', target_is_directory=True)
            m.Handler.port = 8765
            class Connection:
                def __init__(self, raw):
                    self.input = io.BytesIO(raw)
                    self.output = io.BytesIO()
                def makefile(self, *args, **kwargs):
                    return self.input
                def sendall(self, data):
                    self.output.write(data)
            def request(method, path, body=b'', content_type='application/json', extra=''):
                raw = (f'{method} {path} HTTP/1.0\r\nHost: 127.0.0.1:8765\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n{extra}\r\n').encode() + body
                conn = Connection(raw)
                m.Handler(conn, ('127.0.0.1', 12345), SimpleNamespace())
                wire = conn.output.getvalue()
                head, data = wire.split(b'\r\n\r\n', 1)
                return int(head.split(b' ')[1]), data
            code, html = request('GET', '/')
            self.assertEqual(code, 200)
            self.assertIn('模型与提示词'.encode(), html)
            code, data = request('POST', '/api/upload?name=meeting.wav', b'RIFF-test', 'application/octet-stream')
            self.assertEqual(code, 200, data)
            code, data = request('GET', '/api/library')
            self.assertEqual(json.loads(data)['local'][0]['stem'], 'meeting')
            code, data = request('GET', '/api/audio?source=local&name=meeting.wav', extra='Range: bytes=0-3\r\n')
            self.assertEqual((code, data), (206, b'RIFF'))
            code, data = request('POST', '/api/library/title', json.dumps({'stem': 'meeting', 'title': '测试录音'}).encode())
            self.assertEqual(code, 200, data)
            code, data = request('POST', '/api/library/recycle', b'{"stems":["meeting"]}')
            self.assertEqual(code, 200, data)
            batch = json.loads(data)['batch']
            code, data = request('POST', '/api/library/restore', json.dumps({'id': batch}).encode())
            self.assertEqual(code, 200, data)
            code, data = request('POST', '/api/models', b'{}', extra='Origin: https://other.example\r\n')
            self.assertEqual(code, 403)
            code, data = request('GET', '/api/audio?source=local&name=../../private.wav')
            self.assertEqual(code, 400)

class LocalModelTests(unittest.TestCase):
    def test_profile_forwarding_reuses_model_and_records_config(self):
        task = module('qs668-web-task.py', 'test_local_model_task')
        trans = module('qs668-funasr-transcribe.py', 'test_transcriber')
        model = Mock()
        model.generate.return_value = [{'text': '测试内容', 'timestamp': [[0, 3000]]}]
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            library = web.Library(base)
            for name in ['first.wav', 'second.wav']:
                library.import_file(name, io.BytesIO(b'audio'), 5)
            cfg = dict(web.DEFAULTS['asr'], profile='sensevoice')
            with patch.object(task, 'BASE', base), patch.object(task, 'Library', return_value=library), patch.object(
                    task, 'load_module', return_value=trans), patch.object(trans, 'load_model', return_value=model) as loader, patch.object(
                    trans, '_probe_duration', return_value=3):
                task.transcribe('first', cfg)
                task.transcribe('second', cfg)
                loader.assert_called_once_with('sensevoice')
                self.assertEqual(model.generate.call_count, 2)
                task.transcribe('first', cfg)
                self.assertEqual(model.generate.call_count, 2)
            self.assertEqual(web.metadata(library.corpus / 'first.md')['model'], 'sensevoice')
