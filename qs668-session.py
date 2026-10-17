#!/usr/bin/env python3
"""qs668 session 管理：一条录音一个 session 目录 + flow-log 审计留痕（抄 dsh L4/L5）。

目录布局（不动现有 archive/{audio,corpus,brief}，batch-resume/cron 继续可用）：

  ./sessions/<stem>/
  ├── audio            -> ../../nextproto/archive/audio/<stem>.wav   (相对软链)
  ├── corpus.md        -> ../../nextproto/archive/corpus/<stem>.md   (相对软链)
  ├── brief/<title>.md -> 软链 archive/brief 下该 stem 的纪要（一条录音可挂多份）
  └── flow-log.json    审计留痕：这条录音交给过谁、何时、哪个模板/provider/model

flow-log 事件（责任边界四段口径：AI建议 → 人工确认 → 系统执行 → 效果跟踪）：
  transcribed   本地 ASR 完成（provider/model/时长/utterance 数）
  brief         LLM 出纪要（provider/model/模板/耗时）→ AI 建议
  confirm       人工确认纪要可用（--confirm）→ 人工确认
  export        导出/入库（Get笔记 等）→ 系统执行
  followup      效果跟踪备注（--note）

用法：
  funasr-env/bin/python qs668-session.py open <stem>            # 建 session（幂等）
  funasr-env/bin/python qs668-session.py attach-brief <stem>    # 挂当前 brief + 记 flow
  funasr-env/bin/python qs668-session.py confirm <stem> [--note "…"]
  funasr-env/bin/python qs668-session.py followup <stem> --note "客户已按待办执行"
  funasr-env/bin/python qs668-session.py export <stem>          # 记导出事件
  funasr-env/bin/python qs668-session.py show <stem>            # 看 session 全貌
  funasr-env/bin/python qs668-session.py list                   # 全部 session
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
ARCH = BASE / "nextproto" / "archive"
SESSIONS = BASE / "sessions"


def _rel_link(target: Path, link: Path) -> None:
    """相对软链；已存在且指向正确则跳过。"""
    target = target.resolve()
    if not target.exists():
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        if link.is_symlink() and Path(os.path.realpath(link)) == target:
            return
        link.unlink()
    link.symlink_to(Path(os.path.relpath(target, link.parent)))


def flow_path(stem: str) -> Path:
    return SESSIONS / stem / "flow-log.json"


def load_flow(stem: str) -> list[dict]:
    p = flow_path(stem)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_flow(stem: str, events: list[dict]) -> None:
    p = flow_path(stem)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def add_event(stem: str, event: str, **fields) -> dict:
    events = load_flow(stem)
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields}
    events.append(rec)
    save_flow(stem, events)
    return rec


def open_session(stem: str, transcribe_meta: dict | None = None) -> Path:
    """建 session（幂等）。transcribe_meta 非空则同时记 transcribed 事件。"""
    sdir = SESSIONS / stem
    sdir.mkdir(parents=True, exist_ok=True)
    wav = ARCH / "audio" / f"{stem}.wav"
    corpus = ARCH / "corpus" / f"{stem}.md"
    _rel_link(wav, sdir / "audio")
    _rel_link(corpus, sdir / "corpus.md")
    (sdir / "brief").mkdir(exist_ok=True)
    if transcribe_meta:
        add_event(stem, "transcribed", **transcribe_meta)
    return sdir


def attach_brief(stem: str, provider: str = "", model: str = "", template: str = "纪要整理") -> Path | None:
    """把 archive/brief/<stem>.md 软链进 session/brief/ 并记 brief 事件。"""
    sdir = open_session(stem)
    brief = ARCH / "brief" / f"{stem}.md"
    link = sdir / "brief" / f"{stem}.md"
    if brief.is_file():
        _rel_link(brief, link)
        add_event(stem, "brief", provider=provider, model=model, template=template)
        return brief
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="qs668 session/flow-log 管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("open"); p.add_argument("stem")
    p = sub.add_parser("attach-brief"); p.add_argument("stem")
    p.add_argument("--provider", default=""); p.add_argument("--model", default="")
    p.add_argument("--template", default="纪要整理")
    p = sub.add_parser("confirm"); p.add_argument("stem"); p.add_argument("--note", default="")
    p = sub.add_parser("followup"); p.add_argument("stem"); p.add_argument("--note", required=True)
    p = sub.add_parser("export"); p.add_argument("stem")
    p = sub.add_parser("show"); p.add_argument("stem")
    sub.add_parser("list")

    args = ap.parse_args()
    stem = getattr(args, "stem", None)

    if args.cmd == "open":
        sdir = open_session(stem)
        print(f"OK {sdir}")
    elif args.cmd == "attach-brief":
        b = attach_brief(stem, args.provider, args.model, args.template)
        print(f"OK attached {b}" if b else "WARN 无 brief 可挂（先跑 summarize）")
    elif args.cmd == "confirm":
        add_event(stem, "confirm", note=args.note, operator="manual")
        print(f"OK confirmed {stem}")
    elif args.cmd == "followup":
        add_event(stem, "followup", note=args.note)
        print(f"OK followup {stem}")
    elif args.cmd == "export":
        add_event(stem, "export", target="getnote")
        print(f"OK export-event {stem}")
    elif args.cmd == "list":
        rows = sorted(SESSIONS.glob("*/flow-log.json")) if SESSIONS.is_dir() else []
        if not rows:
            print("(无 session)")
        for fp in rows:
            stem2 = fp.parent.name
            events = load_flow(stem2)
            kinds = ",".join(e["event"] for e in events) or "-"
            print(f"{stem2}  events={kinds}")
    elif args.cmd == "show":
        events = load_flow(stem)
        sdir = SESSIONS / stem
        print(f"== {stem} ==  {sdir}")
        for p_ in sorted(sdir.iterdir()) if sdir.is_dir() else []:
            mark = " -> " + p_.resolve().name if p_.is_symlink() else ""
            print(f"  {p_.name}{mark}")
        if events:
            print("-- flow-log --")
            for e in events:
                extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "event"))
                print(f"  [{e['ts']}] {e['event']} {extra}")
        else:
            print("(无 flow-log)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
