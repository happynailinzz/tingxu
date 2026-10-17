#!/usr/bin/env python3
"""qs668 批量 ASR driver：起一次常驻 worker，一次喂 N 条转写（模型只加载 1 次）。

逐条 `qs668-funasr-transcribe.py` 每条都 load_model（paraformer+VAD+punc+cam++，
实测本机 ~12s/次）；批量 8 条 = 8×12s ≈ 99s 纯加载浪费。本 driver 起一个常驻
worker（qs668-asr-worker.py），N 条只付 1 次加载，省 N-1 次。

用法：
  funasr-env/bin/python qs668-batch-asr.py <stem 或 音频路径>...   # 指定，全 force 重转写
  funasr-env/bin/python qs668-batch-asr.py --pending              # 只补缺/指纹不符的 corpus（幂等，生产推荐）
  funasr-env/bin/python qs668-batch-asr.py --audio-dir DIR       # 扫整个目录（裸 .opus 先裁前导污染）

幂等口径（--pending）：corpus 已有正文 + audio_sha256 一致 → skip，不重跑 ASR（零费用）。
裸 .opus 由 qs668_opuslib 先裁前导污染转 16k wav 再喂 worker（worker 只收 wav）。
日志走 stderr，协议响应走 stdout。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
WORKER = BASE / "qs668-asr-worker.py"
ENV_PY = BASE / "funasr-env" / "bin" / "python"
AUDIO_DIR = BASE / "nextproto" / "archive" / "audio"
CORPUS_DIR = BASE / "nextproto" / "archive" / "corpus"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def prep_wavs(stems: list[str], force: bool) -> list[dict]:
    """stem → wav 就绪（裸 opus 先裁前导污染）。返回 [{stem, wav, force}]。"""
    opuslib = _load(BASE / "qs668_opuslib.py", "qs668_opuslib")
    plan = []
    for stem in stems:
        wav = AUDIO_DIR / f"{stem}.wav"
        if not wav.is_file():
            opus = AUDIO_DIR / f"{stem}.opus"
            if opus.is_file():
                opuslib.opus_to_wav(opus, wav)
                print(f"[opus] {stem}.opus -> {stem}.wav（前导污染裁切）", file=sys.stderr)
        if not wav.is_file():
            print(f"[warn] 无音频 {stem}，跳过", file=sys.stderr)
            continue
        plan.append({"stem": stem, "wav": wav, "force": force})
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description="qs668 批量 ASR（常驻 worker，1 次加载）")
    ap.add_argument("stems", nargs="*", help="stem 列表（= 音频文件名去掉扩展名）")
    ap.add_argument("--pending", action="store_true",
                    help="幂等模式：只补缺/指纹不符的 corpus，已有命中 skip")
    ap.add_argument("--audio-dir", help="扫整个目录（文件名 stem）")
    ap.add_argument("--force", action="store_true", help="忽略幂等，全部重转写")
    args = ap.parse_args()

    # 定 stems + 每 stem 是否 force
    if args.stems:
        force = args.force
        plan = prep_wavs(args.stems, force)
    elif args.audio_dir:
        d = Path(args.audio_dir).expanduser()
        stems = sorted(
            p.stem for p in d.glob("*")
            if p.suffix.lower() in {".wav", ".opus", ".mp3", ".m4a"}
        )
        plan = prep_wavs(stems, args.force)
    elif args.pending:
        # 幂等补差：corpus 缺正文或 sha 不符 → force；已命中 → 交给 worker skip
        trans = _load(BASE / "qs668-funasr-transcribe.py", "transcribe_mod")
        stems = sorted(p.stem for p in AUDIO_DIR.glob("*.wav"))
        plan = []
        for stem in stems:
            wav = AUDIO_DIR / f"{stem}.wav"
            need = not trans.should_skip(CORPUS_DIR / f"{stem}.md", wav)
            plan.append({"stem": stem, "wav": wav, "force": need})
        print(f"[pending] {sum(p['force'] for p in plan)}/{len(plan)} 条需转写，"
              f"其余幂等命中 skip", file=sys.stderr)
    else:
        ap.error("需要 stems / --audio-dir / --pending 之一")

    if not plan:
        print("（无音频可转写）", file=sys.stderr)
        return 0

    t0 = time.time()
    proc = subprocess.Popen(
        [str(ENV_PY), str(WORKER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, cwd=str(BASE))

    def send(o):
        proc.stdin.write(json.dumps(o) + "\n")
        proc.stdin.flush()

    def read_json(timeout_note: str = "") -> dict:
        """读一行协议 JSON；跳过 funasr 打到 stdout 的杂行（如 'funasr version: x'）。"""
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError(f"worker 提前退出{timeout_note}")
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except Exception:
                continue  # 非 JSON 杂行（funasr 的 stdout print），跳过

    send({"id": 0, "cmd": "status"})  # 触发首次模型加载
    # worker 先推 ready 事件（无 id）再回 status；按内容匹配，跳过杂行保持对齐
    d = read_json()
    if d.get("event") == "ready":
        d = read_json()  # ready 之后是 status 响应
    if not d.get("ok"):
        print(f"[fatal] worker status 异常: {json.dumps(d, ensure_ascii=False)[:120]}", file=sys.stderr)
        return 1
    n_trans = n_skip = n_fail = 0
    for i, item in enumerate(plan, 1):
        send({"id": i, "cmd": "transcribe", "audio": str(item["wav"]),
              "force": item["force"]})
        try:
            d = read_json()
        except RuntimeError as e:
            print(f"[{item['stem']}] {e}", file=sys.stderr)
            n_fail += 1
            continue
        if d.get("skipped"):
            n_skip += 1
            print(f"[{item['stem']}] skip（幂等命中）", file=sys.stderr)
        elif d.get("ok"):
            n_trans += 1
            print(f"[{item['stem']}] transcribed lines={d.get('lines')} "
                  f"speaker={d.get('speaker')}", file=sys.stderr)
        else:
            n_fail += 1
            print(f"[{item['stem']}] FAIL {d.get('error')}", file=sys.stderr)
    send({"id": 999, "cmd": "quit"})
    proc.wait(timeout=60)

    dt = time.time() - t0
    print(f"\n=== 批量完成 {dt:.1f}s ===", file=sys.stderr)
    print(f"转写 {n_trans} / 跳过 {n_skip} / 失败 {n_fail}（共 {len(plan)} 条）", file=sys.stderr)
    print(f"模型只加载 1 次；逐条跑将付 {len(plan)} 次 ≈ 省 {max(0, len(plan)-1)} 次",
          file=sys.stderr)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
