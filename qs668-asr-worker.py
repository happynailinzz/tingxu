#!/usr/bin/env python3
"""qs668 常驻 ASR worker：模型只加载一次，stdio JSON-lines 批量服务。

抄 dsh-ai-recorder 的 funasr_worker（常驻 + 模型单例 + JSON-lines）+ lomehong/record
的模型单例。解决逐条 `python transcribe.py` 每条子进程重复 load_model 的浪费：
批量 N 条时，N-1 次加载（paraformer+VAD+punc+cam++，秒级~几十秒/次）全省掉。

协议（stdin/stdout，每行一个 JSON；stdout 只走协议，日志走 stderr）：
  请求  {"id":1,"cmd":"transcribe","audio":"...","force":false,"start_time":null,"out_dir":null}
        {"id":2,"cmd":"status"}
        {"id":3,"cmd":"quit"}
  响应  {"id":1,"ok":true,"skipped":false,"lines":92,"speaker":true,"corpus":"..."}
        {"id":1,"ok":true,"skipped":true,"corpus":"..."}        # 幂等命中跳过
        {"id":1,"ok":false,"error":"..."}
        {"id":2,"ok":true,"model":"paraformer-zh","hotwords":12,"loaded":true}
        {"id":3,"ok":true}
  事件  {"event":"ready","model":"paraformer-zh","hotwords":12,"load_ms":...}  (启动推一条，无 id)

转写逻辑与 qs668-funasr-transcribe.py 完全一致（复用其函数），输出 corpus 契约不变：
  archive/corpus/<stem>.md（frontmatter + [墙钟] 说话人N：文本）。
首次运行会从 ModelScope 下载模型（paraformer-zh ~800MB），之后离线。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TRANS = _load(BASE / "qs668-funasr-transcribe.py", "transcribe_mod")

_MODEL = None
_LOAD_MS = 0


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(*a) -> None:
    print("[worker]", *a, file=sys.stderr, flush=True)


def ensure_model():
    global _MODEL, _LOAD_MS
    if _MODEL is None:
        _log("loading FunASR (paraformer-zh + fsmn-vad + ct-punc-c + cam++) ...")
        t0 = time.time()
        _MODEL = TRANS.load_model()
        _LOAD_MS = int((time.time() - t0) * 1000)
        _log(f"model ready in {_LOAD_MS}ms")
    return _MODEL


def do_transcribe(p: dict) -> dict:
    audio = Path(p["audio"]).expanduser()
    if not audio.is_file():
        return {"ok": False, "error": f"audio not found: {audio}"}
    out_dir = Path(p.get("out_dir") or TRANS.DEFAULT_CORPUS_DIR)
    corpus = out_dir / f"{audio.stem}.md"
    digest = TRANS.sha256_file(audio)

    # 幂等跳过（与 CLI 同规则：corpus 非空正文 + audio_sha256 一致才跳）
    if not p.get("force") and TRANS.should_skip(corpus, audio):
        return {"ok": True, "skipped": True, "corpus": str(corpus)}

    model = ensure_model()
    _log(f"transcribe {audio.name} ...")
    result = model.generate(str(audio), batch_size_s=300)
    utterances = TRANS._extract(result, audio)
    if not utterances:
        return {"ok": False, "error": "FunASR returned no utterances; 检查音频是否有人声"}
    base = TRANS.parse_recording_start(audio, p.get("start_time"))
    text, has_spk = TRANS.build_corpus_lines(utterances, base)
    dur = TRANS._probe_duration(audio)
    out_path = TRANS.write_corpus(audio, text, out_dir, dur, base, digest, has_spk)
    return {"ok": True, "skipped": False, "lines": len(utterances),
            "speaker": has_spk, "corpus": str(out_path)}


def main() -> int:
    ensure_model()
    _emit({"event": "ready", "model": "paraformer-zh",
           "hotwords": len(TRANS.HOTWORDS), "load_ms": _LOAD_MS})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _emit({"ok": False, "error": f"bad json: {e}"})
            continue
        cmd = req.get("cmd")
        rid = req.get("id")
        try:
            if cmd == "transcribe":
                r = do_transcribe(req)
            elif cmd == "status":
                r = {"ok": True, "loaded": _MODEL is not None,
                     "model": "paraformer-zh", "hotwords": len(TRANS.HOTWORDS)}
            elif cmd == "quit":
                r = {"ok": True}
            else:
                r = {"ok": False, "error": f"unknown cmd: {cmd}"}
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": repr(e)[:200]}
        if rid is not None:
            r["id"] = rid
        _emit(r)
        if cmd == "quit":
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
