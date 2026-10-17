"""Tingxu vocabulary, speaker roles, enrollment, and BLE regressions."""
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qs668_ble import parse_file_list, stem_from_name
from qs668_web import BASE, Library, private_json
from qs668_personalization import (Personalization, VOICE_VERSION, correct_text, default_vocabulary,
    hotword_argument, normalize, rank_voice, save_vocabulary, vocabulary)


def module(filename, name):
    spec = importlib.util.spec_from_file_location(name, BASE / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def vec(index):
    result = [0.] * 192
    result[index] = 1.
    return result


class VocabularyTests(unittest.TestCase):
    def test_weights_disabled_words_literal_non_cascading_replacements(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {'enabled': True, 'corrections_enabled': True, 'words': [
                {'word': '听序', 'weight': 35, 'group': '产品', 'enabled': True},
                {'word': '关闭词', 'weight': 10, 'enabled': False}], 'corrections': [
                {'from': '听续', 'to': '听序'}, {'from': '听序', 'to': 'Tingxu'},
                {'from': 'a.b', 'to': '术语'}]}
            result = save_vocabulary(payload, Path(tmp))
            self.assertEqual(hotword_argument(result), '听序 35')
            self.assertEqual(correct_text('听续 a.b axb', result), '听序 术语 axb')
            result['enabled'] = False
            self.assertEqual(hotword_argument(result), '')
            result['corrections_enabled'] = False
            self.assertEqual(correct_text('听续', result), '听续')

    def test_invalid_and_duplicate_entries_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {'enabled': True, 'corrections_enabled': False,
                       'words': [{'word': '术语', 'weight': 101}], 'corrections': []}
            with self.assertRaises(ValueError):
                save_vocabulary(payload, Path(tmp))
            payload['words'] = [{'word': '相同', 'weight': 20}] * 2
            with self.assertRaises(ValueError):
                save_vocabulary(payload, Path(tmp))

    def test_defaults_do_not_invalidate_every_task(self):
        self.assertEqual(default_vocabulary(), default_vocabulary())


class VoiceTests(unittest.TestCase):
    def test_rank_ambiguity_and_threshold(self):
        people = [{'id': 'a', 'name': '甲', 'samples': [{'version': VOICE_VERSION, 'embedding': vec(0)}]},
                  {'id': 'b', 'name': '乙', 'samples': [{'version': VOICE_VERSION, 'embedding': vec(1)}]}]
        self.assertTrue(rank_voice(vec(0), people, .65, .08)['suggested'])
        self.assertFalse(rank_voice(normalize([1, 1] + [0] * 190), people, .65, .08)['suggested'])
        self.assertFalse(rank_voice(vec(2), people, .65, .08)['suggested'])
        self.assertIsNone(rank_voice(vec(0), [], .65, .08)['candidate'])
        with self.assertRaises(ValueError):
            normalize([0] * 192)

    def test_person_roles_do_not_rewrite_original_and_hide_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); lib = Library(base); store = Personalization(base)
            lib.corpus.mkdir(parents=True)
            corpus = lib.corpus / 'meeting.md'
            original = '---\naudio_sha256: "abc"\n---\n[10:00:00] 说话人0：开始\n[10:00:02] 说话人1：好的'
            corpus.write_text(original)
            result = store.save_person({'name': '张老师', 'note': '主持人'})
            person_id = result['people'][0]['id']
            data = store.data()
            data['people'][0]['samples'] = [{'id': 'b' * 32, 'version': VOICE_VERSION, 'embedding': vec(0)}]
            private_json(store.path, data)
            self.assertNotIn('embedding', json.dumps(store.public()))
            self.assertIn('embedding', json.dumps(store.data()))
            saved = store.save_roles('meeting', {'0': {'person_id': person_id}, '1': {'name': '客户'}})
            self.assertEqual(saved['resolved'], {'0': '张老师', '1': '客户'})
            self.assertIn('张老师：开始', store.apply_roles('meeting', original))
            self.assertEqual(corpus.read_text(), original)
            store.save_person({'id': person_id, 'name': '李老师'})
            self.assertIn('李老师：开始', store.apply_roles('meeting', original))
            store.remove(person_id)
            self.assertIn('说话人0：开始', store.apply_roles('meeting', original))

    def test_enrollment_persists_embedding_without_network(self):
        worker = module('qs668-voice-task.py', 'test_enrollment')
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); store = Personalization(base)
            person_id = store.save_person({'name': '测试人物'})['people'][0]['id']
            def cut(audio, start, end, target):
                target.write_bytes(b'synthetic-wav')
            with patch.object(worker, 'BASE', base), patch.object(worker, 'Personalization', return_value=store), patch.object(
                    worker, 'audio_for', return_value=base / 'audio.wav'), patch.object(worker, 'cut_audio', side_effect=cut), patch.object(
                    worker, 'validate_clip'), patch.object(worker, 'extract_embedding', return_value=vec(0)):
                worker.enroll({'person_id': person_id, 'stem': 'test', 'start': 0, 'end': 5})
            sample = store.data()['people'][0]['samples'][0]
            self.assertEqual(sample['embedding'], vec(0))
            self.assertTrue((base / '.voiceprints' / person_id / (sample['id'] + '.wav')).is_file())

    def test_exact_segments_required_for_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); lib = Library(base); lib.corpus.mkdir(parents=True)
            (lib.corpus / 'old.md').write_text('[00:00] 说话人0：测试')
            self.assertFalse(Personalization(base).recording('old')['has_segments'])


class BleTests(unittest.TestCase):
    def test_structured_list_and_names(self):
        result = parse_file_list('Using cached address x\n  1. note20261017-120000.  size=123 time=456\n  2. ../../secret.wav  size=100 time=0')
        self.assertEqual(result, [{'name': 'note20261017-120000.', 'size': 123, 'time': 456, 'stem': 'note20261017-120000'}])
        self.assertEqual(stem_from_name('meeting.opus'), 'meeting')

    def test_download_all_reads_list_and_downloads(self):
        collector = module('qs668-collector.py', 'test_collector')
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); audio = base / 'audio'
            def run(cmd, **kwargs):
                if '--list' in cmd:
                    return SimpleNamespace(returncode=0, stdout='  1. meeting.  size=100 time=0\n', stderr='')
                out = Path(cmd[cmd.index('--out') + 1]); (out / 'meeting.wav').write_bytes(b'RIFF')
                return SimpleNamespace(returncode=0)
            with patch.object(collector, 'BASE', base), patch.object(collector, 'AUDIO_DIR', audio), patch.object(
                    collector, 'load_manifest', return_value={'files': {}}), patch.object(collector, 'save_manifest'), patch.object(
                    collector, 'session_flow_event'), patch.object(collector.subprocess, 'run', side_effect=run) as invoke:
                self.assertEqual(collector.collect_ble([]), 0)
                self.assertEqual(invoke.call_count, 2)
                self.assertTrue((audio / 'meeting.wav').is_file())
