#!/usr/bin/env python3
"""Tingxu summary generation with configurable model services and prompts."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "nextproto"
CORPUS_DIR = ROOT / "archive" / "corpus"
BRIEF_DIR = ROOT / "archive" / "brief"
FUNASR_DIR = Path(__file__).resolve().parent

# Optional legacy Hermes providers, explicitly configured through the environment.
# Fresh installations should configure a custom model in the WebUI.
import os
SUMMARIZE_CHAIN: list[tuple[str, str]] = json.loads(os.environ.get("TINGXU_SUMMARY_PROVIDERS", "[]"))
# 单请求超时（秒）+ 同 provider 内重试次数 + 指数退避基数（秒：2,4）
CALL_TIMEOUT = 180
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2
MAX_TOKENS_BASE = 1800  # 长录音的纪要可能超 1800 token；finish_reason=length 时翻倍升档重试

# ============ Get笔记「智能总结」格式模板 ============
# 结构：智能总结(录音信息+录音总结分主题块) / 章节概要 / 金句精选 / 待办事项
# 本地版说明：Get笔记平台的 [MM:SS:SS](getnotes.seek:N) 是可点击的播放跳转链接，
# 本地 md 打不开，故保留 [MM:SS:SS] 章节锚点但去掉 seek 外链；其余层级与措辞对齐。
PROMPT_SYS = """你是专业会议纪要助手。基于给定的「语音转写稿」（每行形如 [MM:SS:SS] 说话人N：原文），
产出一份「智能总结」，严格使用下面的 Markdown 模板与层级，不增删章节，模板外不输出多余内容：

### 📑 智能总结

#### 录音信息
- **录音时间**：<从时间戳推 start ~ end>
- **时长**：约 <X> 分钟
- **参与人数**：约 <N> 人（按出现过的说话人数）
- **内容类型**：<会议 / 访谈 / 通话 / 汇报，自行判断>

#### 录音总结
<3-5 句总括：什么场景、核心结论、后续方向。>

**<主题块一>**
* **<小标题>**：<要点>
* **<小标题>**：<要点>

**<主题块二>**
* **<小标题>**：<要点>
（按内容拆 2-5 个「**主题块**」，每块下 2-4 个「* **小标题**：说明」要点；信息不足就少写，不硬凑）

### 📅 章节概要
[MM:SS:SS] **<章节标题>**
<本章 1-2 句概括>
（按时间戳切 2-5 章；转写很短就 1-2 章）

### ✨ 金句精选
- “<原话>”（<类型：执行策略 / 战略洞见 / 思考启发>）
（挑 3-6 句有信息量的原话，保留原话；不足 3 句则按实写）

### 📋 待办事项
- <负责人/相关方>：<事项>（<时间，若原话有>）
（从内容提取；没有则写「本段未明确待办」）

硬性要求：
1. 转写稿存在同音字/专有名词错误，必须结合上下文纠正：公司名、集团名、云厂商名、行业术语要纠对
   对不确定的专有名词保留原文，不自行猜测真实名称。
2. 除多角色需区分外，正文不要保留「说话人N」字样。
3. 不虚构原文没有的信息；拿不准就基于现有文本归纳，不要编造。
4. 只输出模板内容本身，不要解释、不要复述本 prompt。
5. 【绝对禁止】输出任何思考/推理/计划性独白，例如「We need…」「我们需要回答…」「需要判断…」「让我…」「Key facts…」「Note…」。
   你的最终回复从 `### 📑 智能总结` 开始、到此结束；在此之前和章节之间不得有任何其他文字。"""


# 思考独白泄漏标记：模型偶尔把推理过程拼进 content（前缀或章节间穿插）。
# 只取「绝不会出现在干净成品里的 planning 短语」，避免误伤正常中文总结（如"可能是"）。
MONOLOGUE_MARKERS = [
    # 英文 planning 短语（干净中文正文绝不会出现）
    "We need", "Need ", "Key facts", "Let's", "Let me", "Maybe ", "Better ", "Hmm.",
    # 中文 planning 短语（成品总结不会出现的指令式口吻）
    "我们需要回答", "需要输出模板", "让我推断", "需要判断专名", "需要输出模板",
]


def strip_monologue_prefix(content: str) -> str:
    """剥掉模板标记之前的独白前缀（正常输出第一个 ### 就是智能总结头）。"""
    marker = "### 📑 智能总结"
    if content.startswith("### "):
        return content
    idx = content.find(marker)
    if idx > 0:
        return content[idx:].strip()
    return content


def has_monologue_leak(content: str) -> bool:
    """检查（剥前缀后）的正文里是否残留独白标记（含章节间穿插）。"""
    body = strip_monologue_prefix(content)
    for mk in MONOLOGUE_MARKERS:
        if mk in body:
            return True
    return False


def _last_section_present(body: str) -> bool:
    """模板最后一节「待办事项」必须出现（缺了 = 被截断或跳章）。"""
    return "待办事项" in body


def _ends_cleanly(body: str) -> tuple[bool, str]:
    """末尾不能停在半句：末节最后一个非空行应是列表项/章节头/完整要点。

    判据（宽松但能拦住典型断尾）：
      * 末行是 "- " 或 "* " 或 "#### " 或 "### " 开头 → 视为列表项/头（允许，正常）
      * 末行是 "- <完整句>（含中文句号/引号/括号收尾）" → 允许
      * 末行是 "- <未收尾>"（如"- 业主"、"* 授权" 这种半句）→ 判截断
    简化：末行去掉列表符后，若不含中文标点收尾（。！？）」〕）且长度 < 8 字 → 视为截断。
    """
    lines = [l for l in body.rstrip().splitlines() if l.strip()]
    if not lines:
        return True, ""
    last = lines[-1].strip()
    # 章节头 / 纯标题行不算截断
    if last.startswith("#"):
        return True, ""
    # 去列表符
    core = re.sub(r"^[-*•\d\.\s]+", "", last).strip()
    if not core:
        return True, ""
    # 收尾标点
    tail_ok = bool(re.search(r"[。！？）」】\"'\.\?\!]\s*$", core))
    if tail_ok:
        return True, ""
    # 短且无收尾标点 = 半句（如"- 业主"→ core="业主" 长度 2）
    if len(core) < 12:
        return False, f"末尾停在半句: {last!r}"
    return True, ""


def _todo_tail_clean(body: str) -> tuple[bool, str]:
    """「待办事项」是模板最后一节，其标题行之后只允许 "- " 列表项（独白残留/格式跑偏 = 散段）。

    注意按「标题行」定位（含 待办事项 且以 # 开头的行），不要用 rfind 落在标题中间，
    否则 tail 会从 '待办事项' 残片起算、把标题本身误判成散段。
    """
    lines = body.splitlines()
    hdr = None
    for i, l in enumerate(lines):
        if "待办事项" in l and l.lstrip().startswith("#"):
            hdr = i
    if hdr is None:
        return True, ""  # 没找到待办事项节标题（_last_section_present 已拦，这里兜底放行）
    bad = []
    for l in lines[hdr + 1:]:
        s = l.strip()
        if not s:
            continue
        if s.startswith("#") or s[:1] in ("-", "*", "•"):
            continue
        bad.append(s)
    if bad:
        return False, f"待办事项后出现非列表散段（疑似独白残留）: {bad[-1][:30]!r}"
    return True, ""


def content_ok(content: str) -> tuple[bool, str]:
    """接受判据：剥前缀后必须以 ### 开头、无独白残留、含章节结构、末节完整、末行不半句、待办后无非列表散段。"""
    body = strip_monologue_prefix(content)
    if not body or not body.startswith("### "):
        return False, "content empty or not starting with a section"
    if has_monologue_leak(body):
        return False, "thinking-monologue leaked into content"
    if "###" not in body or "####" not in body:
        return False, "missing required section structure"
    if not _last_section_present(body):
        return False, "末节「待办事项」缺失（疑似被截断或跳章）"
    ok, reason = _ends_cleanly(body)
    if not ok:
        return False, reason
    ok, reason = _todo_tail_clean(body)
    if not ok:
        return False, reason
    return True, ""


def load_credentials(provider: str) -> tuple[str, str, dict]:
    import yaml
    cfg = yaml.safe_load(open(Path.home() / ".hermes" / "config.yaml"))

    def find(o, k):
        if isinstance(o, dict):
            if k in o and isinstance(o[k], dict):
                return o[k]
            for v in o.values():
                r = find(v, k)
                if r:
                    return r
        return None

    providers = find(cfg, "providers")
    if providers is None or provider not in providers:
        raise SystemExit(f"config.yaml 缺少 provider {provider}")
    spec = providers[provider]
    return spec["base_url"].rstrip("/"), spec["api_key"], spec.get("extra_headers", {})


def parse_front_matter(text: str) -> dict:
    m = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    m[k.strip()] = v.strip().strip('"\'')
    return m


def body_lines(text: str) -> list[str]:
    return [l for l in text.splitlines() if l.startswith("[")]


def transcribe_if_audio(audio: Path) -> Path:
    """给定 audio，跑本地 FunASR 转写，返回 corpus md 路径。"""
    env_py = FUNASR_DIR / "funasr-env" / "bin" / "python"
    script = FUNASR_DIR / "qs668-funasr-transcribe.py"
    print(f"[transcribe] {audio.name} -> corpus")
    subprocess.run([str(env_py), str(script), str(audio), "--force"], check=True)
    return CORPUS_DIR / f"{audio.stem}.md"


def summarize_one(corpus_path: Path, out_dir: Path, suspect: bool, settings: dict | None = None) -> int:
    """对单条 corpus 出 Get笔记 纪要，写 out_dir/<stem>.md。成功 0，失败 SystemExit。"""
    if not corpus_path.is_file():
        raise SystemExit(f"corpus not found: {corpus_path}")

    settings = settings or {}
    custom_prompt = settings.get("prompt", "").strip()
    chain = [("custom", settings["model"])] if settings.get("mode") == "custom" else SUMMARIZE_CHAIN
    if not chain:
        raise ValueError("请在模型与提示词页面配置自定义总结服务")
    text = corpus_path.read_text(encoding="utf-8")
    meta = parse_front_matter(text)
    lines = body_lines(text)
    speakers = len(set(re.findall(r"说话人(\d+)", "\n".join(lines))))
    transcript = "\n".join(lines)
    roles = settings.get("confirmed_roles", {})
    if roles:
        transcript = re.sub(r'(^\[[^\]\n]+\]\s*)说话人([^：:\s]+)([:：])',
            lambda m: m[1] + roles.get(m[2], "说话人" + m[2]) + m[3], transcript, flags=re.M)

    messages = [
        {"role": "system", "content": custom_prompt or PROMPT_SYS},
        {
            "role": "user",
            "content": (
                f"录音元信息：记录时刻={meta.get('recorded_at','未知')}；"
                f"时长={meta.get('duration','未知')}；语言={meta.get('language','zh')}。\n\n"
                f"转写稿如下：\n{transcript}"
            ),
        },
    ]

    print(f"[summarize] {corpus_path.name}  lines={len(lines)} speakers={speakers} chain="
          f"{[(p, m) for p, m in chain]}")

    t0 = time.time()
    content, used_provider, used_model, last_err = "", None, None, None
    for provider, model in chain:
        try:
            if provider == "custom":
                base, key, extra_hdr = settings["base_url"].rstrip("/"), settings.get("api_key", ""), {}
            else:
                base, key, extra_hdr = load_credentials(provider)
        except (Exception, SystemExit):
            last_err = f"{provider} 配置不可用"
            print(last_err, flush=True)
            continue
        max_tokens = MAX_TOKENS_BASE
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # 每次请求前同步 payload（max_tokens 可能在上次被截断后升档）
                payload["max_tokens"] = max_tokens
                req = urllib.request.Request(
                    base + "/chat/completions",
                    data=json.dumps(payload).encode(),
                    headers={**{"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"}, **extra_hdr},
                )
                with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as r:
                    d = json.loads(r.read())
                ch = d["choices"][0]
                raw = (ch.get("message", {}).get("content") or "").strip()
                clean = raw if custom_prompt else strip_monologue_prefix(raw)
                ok, reason = (bool(clean), "empty response") if custom_prompt else content_ok(clean)
                if ok and ch.get("finish_reason") != "length":
                    content, used_provider, used_model = clean, provider, model
                    break
                # finish_reason=length → 被 max_tokens 截断：翻倍升档再试（不重试同额度）
                if ch.get("finish_reason") == "length":
                    old = max_tokens
                    max_tokens = min(max_tokens * 2, 8000)
                    last_err = f"被 max_tokens={old} 截断（finish_reason=length），升档到 {max_tokens}"
                else:
                    last_err = reason or "empty or missing section in response"
            except Exception as e:
                last_err = repr(e)[:160]
            print(f"[{provider}/{model}] 第{attempt}次失败（{last_err}），"
                  f"{RETRY_BACKOFF_BASE ** attempt}s 后重试")
            time.sleep(RETRY_BACKOFF_BASE ** attempt)
        if used_provider:
            break
    dt = time.time() - t0

    if not used_provider:
        raise SystemExit(
            f"summarize 失败：整条 provider 链 {chain} 均未得到有效纪要"
            f"（最后错误：{last_err}），不落空 brief")

    out_path = out_dir / f"{corpus_path.stem}.md"
    fm = [
        "---",
        f'title: "{corpus_path.stem}"',
        f'audio: "{meta.get("audio", "")}"',
        f'source_corpus: "{corpus_path.name}"',
        f"provider: {used_provider}",
        f"model: {used_model}",
        f"asr_quality: {'suspect' if suspect else 'ok'}",
        f"format: {'custom' if custom_prompt else 'getnote-smart-summary'}",
        f"summarized_on: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "---",
        "",
    ]
    import os, uuid
    out_dir.mkdir(parents=True, exist_ok=True)
    temp = out_path.with_name("." + out_path.name + "." + uuid.uuid4().hex)
    try:
        temp.write_text("\n".join(fm) + content + "\n", encoding="utf-8")
        os.replace(temp, out_path)
    finally:
        temp.unlink(missing_ok=True)
    print(f"[done] {out_path}  provider={used_provider} model={used_model} ({dt:.1f}s)")
    print("\n" + "=" * 50 + "\n")
    print(content)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Tingxu configurable summary generation")
    ap.add_argument("stem", nargs="?", help="corpus stem, e.g. note20261014-100100")
    ap.add_argument("--audio", help="audio path: run FunASR transcribe then summarize")
    ap.add_argument("--out-dir", default=str(BRIEF_DIR))
    ap.add_argument("--suspect", action="store_true",
                    help="上游 gate 判定 corpus 可疑，frontmatter 写 asr_quality=suspect")
    ap.add_argument("--pending", action="store_true",
                    help="幂等批量：扫 corpus 里还没有 brief 的，逐条出纪要（console 闭环第③步用）")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # pending 幂等批量：corpus 有、brief 没有（或为空）的逐条出纪要
    if args.pending:
        todo = []
        for c in sorted(CORPUS_DIR.glob("*.md")):
            b = out_dir / f"{c.stem}.md"
            if not b.is_file() or b.stat().st_size < 200:
                todo.append(c)
        if not todo:
            print("[pending] corpus 里 0 条缺 brief，无需出纪要")
            return 0
        print(f"[pending] 待出纪要 {len(todo)} 条：{', '.join(c.stem for c in todo)}")
        failed = 0
        for c in todo:
            # 单条失败不拖垮整批（继续下一条），最后按失败数返回非 0
            try:
                summarize_one(c, out_dir, args.suspect)
            except (Exception, SystemExit) as e:
                failed += 1
                print(f"[pending] ✗ {c.stem}: {e}")
                if str(e).startswith("summarize 失败") or "not found" in str(e):
                    print(f"  （{c.stem} 记为未出，下轮再补）")
        return 1 if failed else 0

    if args.audio:
        corpus_path = transcribe_if_audio(Path(args.audio))
    elif args.stem:
        corpus_path = CORPUS_DIR / f"{args.stem}.md"
    else:
        candidates = [p for p in CORPUS_DIR.glob("*.md") if p.is_file()]
        if not candidates:
            raise SystemExit(f"No corpus under {CORPUS_DIR}; 先跑 qs668-funasr-transcribe.py")
        corpus_path = max(candidates, key=lambda p: p.stat().st_mtime)

    return summarize_one(corpus_path, out_dir, args.suspect)


if __name__ == "__main__":
    raise SystemExit(main())
