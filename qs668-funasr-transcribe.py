#!/usr/bin/env python3
"""qs668 本地 ASR provider：FunASR（paraformer-zh + fsmn-vad + cam++ 说话人分离）。

与 qs668-transcribe.py（doubao/openai provider）对齐的输出契约：
- 音频输入：archive/audio/<stem>.wav（或任意路径）
- 语料输出：archive/corpus/<stem>.md，YAML front matter 字段与上游一致
- 正文行：[HH:MM:SS] 说话人N：文本  （墙钟时间从文件名 noteYYYYMMDD-HHMMSS 推算）
- 同一套跳过规则：corpus 非空正文 + audio_sha256 一致才跳过，否则重跑

用法：
  ./funasr-env/bin/python qs668-funasr-transcribe.py archive/audio/note....wav
  热词：编辑下方 HOTWORDS（格式：(词, 权重1-100)）
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE / "nextproto"
DEFAULT_AUDIO_DIR = ROOT / "archive" / "audio"
DEFAULT_CORPUS_DIR = ROOT / "archive" / "corpus"

# Public starter vocabulary. Customize it in the WebUI.
HOTWORDS: list[tuple[str, int]] = [("听序", 30), ("会议纪要", 20)]
NORM_MAP: list[tuple[str, str]] = []


def apply_norm(text: str) -> str:
    for pat, repl in NORM_MAP:
        text = re.sub(pat, repl, text)
    return text

NOTE_TIME_RE = re.compile(r"(?P<date>\d{8})[-_](?P<clock>\d{6})", re.IGNORECASE)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_recording_start(audio_path: Path, explicit: str | None = None) -> datetime | None:
    if explicit:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d-%H%M%S", "%Y%m%d_%H%M%S"):
            try:
                return datetime.strptime(explicit.strip(), fmt)
            except ValueError:
                continue
    match = NOTE_TIME_RE.search(audio_path.stem)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("date") + match.group("clock"), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def format_wall_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def yaml_escape(value: str) -> str:
    return f'"{value}"'


def load_model(profile: str = "paraformer-zh"):
    """按 catalog profile 构造 FunASR AutoModel。

    默认 paraformer-zh（= 现状行为）。profile 由 qs668_catalog 提供：
    - paraformer-zh          显式 repo id + fsmn-vad + ct-punc-c + cam++
    - paraformer-zh+webrtc   显式 repo id，但不加载 funasr vad（切段由 qs668_vad 做）
    - sensevoice             多语种快速
    防漂移边界：spk 用注册短名 cam++，ASR/punc/vad 用显式 repo id（见 catalog 注释）。
    """
    from funasr import AutoModel

    if profile in ("paraformer-zh", "sensevoice"):
        # 传统 profile：fsmn-vad 走 funasr
        model_map = {
            "paraformer-zh": ("paraformer-zh", "fsmn-vad", "ct-punc-c", "cam++"),
            "sensevoice": ("SenseVoiceSmall", "fsmn-vad", None, "cam++"),
        }
        m, vad, punc, spk = model_map[profile]
        kwargs = dict(model=m, device="cpu", ncpu=4, disable_update=True)
        if profile == "paraformer-zh":
            kwargs["vad_model"] = vad
            kwargs["punc_model"] = punc
            kwargs["spk_model"] = spk
            if HOTWORDS:
                kwargs["hotwords"] = " ".join(f"{w} {weight}" for w, weight in HOTWORDS)
        else:  # sensevoice：自带标点，无 hotwords
            kwargs["vad_model"] = vad
            kwargs["spk_model"] = spk
        return AutoModel(**kwargs)

    if profile == "paraformer-zh+webrtc":
        # WebRTC VAD 自切段：ASR 模型不带 funasr vad，切段由 qs668_vad.webrtc_segments 做
        kwargs = dict(model="paraformer-zh", device="cpu", ncpu=4, disable_update=True)
        kwargs["punc_model"] = "ct-punc-c"
        kwargs["spk_model"] = "cam++"
        if HOTWORDS:
            kwargs["hotwords"] = " ".join(f"{w} {weight}" for w, weight in HOTWORDS)
        return AutoModel(**kwargs)

    raise SystemExit(f"未知 profile {profile}；可选 paraformer-zh / paraformer-zh+webrtc / sensevoice")


def _extract(result, wav_path: Path):
    """把 FunASR 结果解析为 utterance 列表：{text,start(ms),end(ms),spk}。

    paraformer-zh + cam++ 的典型返回：list[dict]，每个元素含 'text' 与
    'sentence_info'（说话人分段，每项 {spk,start,end,text}）。

    质量防线（2026-10-17 note20261017-124438 复盘加入）：
    - 丢弃无实词段：VAD 在静音/噪声上的误触发会产出纯标点行（"，"/"。"），
      还会被 cam++ 随机挂上说话人号（制造出根本不存在的"说话人N"）——无汉字/
      字母/数字的段一律丢。
    """
    items = result["data"] if isinstance(result, dict) and "data" in result else result
    if isinstance(items, dict):
        items = [items]
    out: list[dict] = []

    def clean(text: str) -> str:
        # 中文逐字带空格（"王 总 您 好"），去掉汉字间空格；西文保留。
        # 注意：必须用 CJK 区间 [\\u4e00-\\u9fff]，写成 (?<=\\u4e00) 只会匹配"一"这个字。
        return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text.strip())

    def _drop_nonsense(text: str) -> bool:
        return not re.search(r"[\u4e00-\u9fff0-9A-Za-z]", text)

    for item in items:
        if not isinstance(item, dict):
            continue
        sentences = item.get("sentence_info")
        if sentences:
            for s in sentences:
                text = clean(str(s.get("text") or ""))
                if not text or _drop_nonsense(text):
                    continue
                out.append({
                    "text": text,
                    "start": int(s.get("start", 0)),
                    "end": int(s.get("end", 0)),
                    "spk": s.get("spk"),
                })
            continue
        text = clean(str(item.get("text") or ""))
        if text and not _drop_nonsense(text):
            ts = item.get("timestamp") or []
            start = int(ts[0][0]) if ts and ts[0] else 0
            end = int(ts[-1][1]) if ts and ts[-1] else 0
            out.append({"text": text, "start": start, "end": end, "spk": None})
    return out


def _merge_fragments(utterances: list[dict]) -> list[dict]:
    """同说话人短碎片合并（kardo 显示层规则收敛版）：
    相邻两段同 spk、间隔 ≤2s、且任一段 <10 字 → 并成一段（kardo 实测：
    长段不拆不并，避免同时间多窗口重复；这里保守，只在明显碎片时合并）。
    """
    out: list[dict] = []
    for u in utterances:
        if out and u["spk"] is not None and u["spk"] == out[-1]["spk"]:
            prev = out[-1]
            gap_s = (u["start"] - prev["end"]) / 1000.0
            if 0 <= gap_s <= 2 and (len(prev["text"]) < 10 or len(u["text"]) < 10):
                prev["text"] = prev["text"] + u["text"]
                prev["end"] = u["end"]
                continue
        out.append(dict(u))
    return out


def build_corpus_lines(utterances: list[dict], base: datetime | None):
    utterances = _merge_fragments(utterances)
    for u in utterances:  # 专有名词归一（离线 paraformer 不支持热词偏置，事后归一是唯一可靠防线）
        u["text"] = apply_norm(u["text"])
    lines = []
    for u in utterances:
        if base is not None:
            moment = base + timedelta(milliseconds=u["start"])
            stamp = moment.strftime("%H:%M:%S") if moment.date() == base.date() else moment.strftime("%m-%d %H:%M:%S")
        else:
            stamp = format_timestamp(u["start"] / 1000.0)
        spk = u["spk"]
        if spk not in (None, ""):
            lines.append(f"[{stamp}] 说话人{spk}：{u['text']}")
        else:
            lines.append(f"[{stamp}] {u['text']}")
    return "\n".join(lines), any(u["spk"] is not None for u in utterances)


def write_corpus(audio_path, text, out_dir, duration_s, recording_start, digest, has_speaker) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{audio_path.stem}.md"
    lines = [
        "---",
        f"title: {yaml_escape(audio_path.stem)}",
        f"audio: {yaml_escape(audio_path.name)}",
        f"audio_sha256: {yaml_escape(digest)}",
    ]
    if recording_start:
        lines.append(f"recorded_at: {yaml_escape(format_wall_time(recording_start))}")
    lines += [
        f"duration: {yaml_escape(format_timestamp(duration_s))}",
        "language: zh",
        "sync_status: local",
        "provider: funasr-local",
        "transcribed_on: pc",
        f"timeline: {'wall-clock' if recording_start else 'relative'}",
        "---",
        "",
        f"# {audio_path.stem}",
        "",
        text,
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def should_skip(corpus_path: Path, audio_path: Path) -> bool:
    if not corpus_path.is_file():
        return False
    text = corpus_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end < 0:
        return False
    front = text[3:end]
    body = text[end + 4:].lstrip("\n")
    if not any(l.strip() and not l.strip().startswith("#") for l in body.splitlines()):
        return False
    m = re.search(r"^audio_sha256:\s*\"?([0-9a-f]{64})\"?", front, re.MULTILINE)
    return bool(m) and m.group(1) == sha256_file(audio_path)


def _probe_duration(audio_path: Path) -> float:
    """用 ffprobe 读真实时长（秒）；失败回退到 utterance 跨度。"""
    import subprocess, shutil
    if shutil.which("ffprobe"):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(audio_path)],
                check=True, capture_output=True, text=True, timeout=60,
            ).stdout.strip()
            if out:
                return float(out)
        except Exception:
            pass
    return 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="QS668 archive audio via local FunASR")
    parser.add_argument("audio", nargs="?", help="Audio path. Default: newest under archive/audio/")
    parser.add_argument("--out-dir", default=str(DEFAULT_CORPUS_DIR))
    parser.add_argument("--start-time", default=None, help="e.g. '2026-10-11 16:18:58'")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.audio:
        audio_path = Path(args.audio)
    else:
        candidates = [p for p in DEFAULT_AUDIO_DIR.glob("*") if p.suffix.lower() in {".wav", ".mp3", ".m4a", ".ogg", ".opus"}]
        if not candidates:
            raise SystemExit(f"No audio under {DEFAULT_AUDIO_DIR}")
        audio_path = max(candidates, key=lambda p: p.stat().st_mtime)
    if not audio_path.is_file():
        raise SystemExit(f"Audio not found: {audio_path}")

    out_dir = Path(args.out_dir)
    corpus_path = out_dir / f"{audio_path.stem}.md"
    digest = sha256_file(audio_path)
    print(f"ASR audio_sha256 {digest}")
    if not args.force and should_skip(corpus_path, audio_path):
        print(f"SKIP corpus reusable: {corpus_path} (pass --force to re-run)")
        print(f"DONE {corpus_path} skipped=1")
        return 0

    print("loading FunASR (paraformer-zh + fsmn-vad + cam++) ... 首次会下载模型约 800MB")
    model = load_model()
    utterances = _extract(model.generate(str(audio_path), batch_size_s=300), audio_path)
    if not utterances:
        raise SystemExit("ERR FunASR returned no utterances; 检查音频是否有人声")
    base = parse_recording_start(audio_path, args.start_time)
    text, has_speaker = build_corpus_lines(utterances, base)
    duration_s = _probe_duration(audio_path)
    out_path = write_corpus(audio_path, text, out_dir, duration_s, base, digest, has_speaker)
    print(f"DONE {out_path} provider=funasr-local lines={len(utterances)} speaker={'yes' if has_speaker else 'no'}")
    print(text[:800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
