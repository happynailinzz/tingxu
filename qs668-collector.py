#!/usr/bin/env python3
"""qs668 采集控制器（P2.10 方向）：录音卡音频进 archive/audio 的「可控 + 可观测」层。

两条采集通道（互斥、不混用——设备单通道语义，群规"导入和实时不要同时跑"）：
  wired（TypeC）  USB mass-storage 挂载 /Volumes/<卷> → 整文件拷入（MB/s 级，完整性 100%）
  ble（蓝牙）     复用 nextproto/tools/qs668-bringup.py：scan→connect→list→import（~2KB/s）

通道优先级（默认 auto）：
  有线挂载可读 → 一律走 wired（快+完整）；未挂载/读取失败 → 回退 BLE 补传
  同一文件两通道不混：manifest 是唯一事实源，wired 已 done+sha 的文件 BLE 自动跳过
  --mode wired|ble|auto 可手动钉死通道（"手动优先有线" = --mode wired，未挂载直接报错）

完整性判据（wired 拷贝后）：
  .opus 裸流大小 % 40B == 0 且 opus_probe 可解码 → done
  否则 → suspect（拷贝中途拔线/未写完），保留文件不删，manifest 标记，下轮重拷
  拔线中途：source-dir 消失 → 该文件记 incomplete，watch 不崩，等重插

可观测：
  manifest  ./nextproto/collected-manifest.json（per-file：status/source/sha256/opus_probe/collected_at）
  status    一条命令看 挂载状态/BLE 可达性/各文件状态/完成率/下一步动作（--json 可机读）
  --watch   轮询挂载（插 TypeC 线即自动采集），全 done 后退出；进度按 文件拷完率 报
  session   每次 collected 事件进 sessions/<stem>/flow-log.json（哪条通道、何时、多大、校验过没有）

用法：
  funasr-env/bin/python qs668-collector.py status
  funasr-env/bin/python qs668-collector.py collect                    # auto：wired 优先
  funasr-env/bin/python qs668-collector.py collect --mode ble         # 手动激活蓝牙采集
  funasr-env/bin/python qs668-collector.py collect --mode wired       # 手动优先有线（没插报错）
  funasr-env/bin/python qs668-collector.py collect --watch            # 插线自动采，采完退出
  funasr-env/bin/python qs668-collector.py collect --realtime 300     # BLE 实时推流（特殊场景）
  funasr-env/bin/python qs668-collector.py collect --source-dir DIR   # 测试/自定义挂载点
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
ARCH = BASE / "nextproto" / "archive"
AUDIO_DIR = ARCH / "audio"
MANIFEST = BASE / "collected-manifest.json"
BRINGUP = BASE / "nextproto" / "tools" / "qs668-bringup.py"
ENV_PY = BASE / "funasr-env" / "bin" / "python"
if not ENV_PY.is_file():
    ENV_PY = Path(sys.executable)
PACKET_SIZE = 40  # 设备裸 Opus：40B/包（kardo/dsh 口径）
VOLUME_HINTS = ("qs668", "cb08", "recorder", "rekord", "voice")


# ── manifest ──────────────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST.is_file():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"updated": None, "files": {}}


def save_manifest(m: dict) -> None:
    m["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def session_flow_event(stem: str, event: str, **fields) -> None:
    """collected/collected-fail 事件记进 session flow-log（审计留痕，抄 P0.2）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("qs668_session", str(BASE / "qs668-session.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    try:
        m.open_session(stem)
        m.add_event(stem, event, **fields)
    except Exception:
        pass  # 审计失败不阻断采集


# ── 通道探测 ──────────────────────────────────────────────────────

def wired_volumes() -> list[Path]:
    """/Volumes 下非内置的可疑卡片卷。

    注意（真机踩出）：FAT32 可移动卷的目录 st_size=0（APFS 卷才有非零值），
    不能用 st_size 判"非内置"，按保留卷名排除即可。
    """
    exclude = {"macintosh hd", "recovery", "nocomputer", "nosim",
               "system volume", "preboot", "vm", "efi"}
    out = []
    vols = Path("/Volumes")
    for p in sorted(vols.iterdir()) if vols.is_dir() else []:
        if not p.is_dir() or p.name.lower() in exclude:
            continue
        out.append(p)
    return out


def find_wired(volume_name: str | None, source_dir: str | None) -> Path | None:
    if source_dir:
        p = Path(source_dir).expanduser()
        return p if p.is_dir() else None
    vols = wired_volumes()
    if volume_name:
        for v in vols:
            if v.name.lower() == volume_name.lower():
                return v
        return None
    return vols[0] if vols else None


def ble_reachable(seconds: int = 2) -> bool:
    """快速扫描判断设备在场（不连接，只探测）。"""
    code = (
        "import asyncio,bleak,sys\n"
        "async def s():\n"
        "    found=[]\n"
        "    def cb(d,a):\n"
        "        if (d.name or '').strip(): found.append(d.name)\n"
        "    sc=bleak.BleakScanner(discovery_callback=cb); await sc.start()\n"
        "    await asyncio.sleep(float(sys.argv[1])); await sc.stop()\n"
        "    print('1' if found else '0')\n"
        "asyncio.run(s())\n"
    )
    try:
        r = subprocess.run([str(ENV_PY), "-c", code, str(seconds)],
                           capture_output=True, text=True, timeout=seconds + 15)
        return r.stdout.strip() == "1"
    except Exception:
        return False


# ── 文件处理 ──────────────────────────────────────────────────────

RECORDED_NAMES = ("note", "call", "rec", "audio", "voice", "oppo")


def card_files(vol: Path) -> list[Path]:
    """卷上的候选录音文件（递归，按扩展名 + 命名启发式）。

    真机形态（FAT32 卡）：录音在 record/ 子目录，且 opus + wav 同 stem 成对
    （设备端已把 opus 转好 wav）。按 stem 去重，同 stem 优先 .wav（完整、免
    opus2wav 前导污染路径），回退 .opus/.ogg/.mp3/.m4a。
    """
    ext_rank = {".wav": 0, ".opus": 1, ".ogg": 2, ".mp3": 3, ".m4a": 4}
    best: dict[str, Path] = {}
    for p in vol.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in ext_rank:
            continue
        if p.name.lower().startswith("."):
            continue
        cur = best.get(p.stem)
        if cur is None or ext_rank[p.suffix.lower()] < ext_rank[cur.suffix.lower()]:
            best[p.stem] = p
    cands = sorted(best.values(), key=lambda p: p.name)
    if not cands:
        return cands
    # 命名带 note/call 前缀优先
    named = [p for p in cands
             if any(p.stem.lower().startswith(h) for h in RECORDED_NAMES)]
    return named or cands


def probe_opus(path: Path) -> str:
    """裸 opus 完整性快检（比全量 opus_probe 快）：40B 对齐 + 首包可封装。"""
    size = path.stat().st_size
    if size < PACKET_SIZE * 10:
        return "too-small"
    if size % PACKET_SIZE:
        return "not-40b-aligned"
    import importlib.util
    spec = importlib.util.spec_from_file_location("qs668_opuslib", str(BASE / "qs668_opuslib.py"))
    lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lib)
    try:
        raw = path.read_bytes()
        info = lib.probe(raw, max_skip=65536, min_ratio=0.9)
        return info["status"]  # clean / contaminated / undecodable
    except Exception:
        return "probe-failed"


def opus_to_wav(path: Path) -> Path | None:
    """设备裸 opus → 16k wav（前导污染裁切，P0.1）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("qs668_opuslib", str(BASE / "qs668_opuslib.py"))
    lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lib)
    wav = path.with_suffix(".wav")
    try:
        lib.opus_to_wav(path, wav)
        return wav
    except Exception as e:
        print(f"    [opus2wav 失败] {e}", file=sys.stderr)
        return None


def collect_wired(vol: Path) -> int:
    """wired：拷文件 → 完整性 → sha → manifest → opus 转 wav。返回剩余未完成数。"""
    mf = load_manifest()
    files = card_files(vol)
    if not files:
        print(f"[wired] {vol} 上未见录音文件", file=sys.stderr)
        return 0
    pending = 0
    for src in files:
        stem = src.stem
        dst = AUDIO_DIR / src.name
        cur = mf["files"].get(stem, {})
        if cur.get("status") == "done" and cur.get("sha256") and dst.is_file() \
                and sha256_file(dst) == cur["sha256"]:
            print(f"  [skip] {stem} 已 done 且 sha 一致（{vol.name}）", file=sys.stderr)
            continue
        total = src.stat().st_size
        dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"  [copy] {src.name} ({total/1e6:.1f}MB) ...", file=sys.stderr)
        try:
            shutil.copy2(src, dst)
            ok = dst.stat().st_size == total
        except Exception as e:
            ok = False
            print(f"    拷贝中断（可能拔线）: {e}", file=sys.stderr)
        if not ok:
            mf["files"][stem] = {"status": "incomplete", "source": "wired",
                                 "volume": vol.name, "collected_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_manifest(mf)
            session_flow_event(stem, "collect-fail", channel="wired", reason="incomplete-copy")
            pending += 1
            continue

        # 完整性：裸 opus 走 40B 对齐 + probe；其他格式过 size 校验即可
        if dst.suffix.lower() == ".opus":
            probe = probe_opus(dst)
            status = "done" if probe in ("clean", "contaminated") else "suspect"
        else:
            probe = "n/a"
            status = "done"
        sha = sha256_file(dst)
        rec = {"status": status, "source": "wired", "volume": vol.name,
               "path": str(dst.relative_to(BASE)), "size": dst.stat().st_size,
               "sha256": sha, "opus_probe": probe,
               "collected_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        # opus 转 wav（转了记路径；转失败不影响 done，下轮 pipeline 再补）
        if dst.suffix.lower() == ".opus":
            wav = opus_to_wav(dst)
            if wav:
                rec["wav_path"] = str(wav.relative_to(BASE))
        mf["files"][stem] = rec
        save_manifest(mf)
        session_flow_event(stem, "collected", channel="wired", volume=vol.name,
                           bytes=rec["size"], opus_probe=probe, verified=(status == "done"))
        mark = "" if status == "done" else f"  ⚠ {status} ({probe})"
        print(f"  [ok] {stem} -> {dst.name}{mark}", file=sys.stderr)
        if status != "done":
            pending += 1
    return pending


def collect_ble(names: list[str], force: bool = False) -> int:
    """BLE：复用 bringup。names 为空 = import-all；指定 = 逐个 import。"""
    from qs668_ble import parse_file_list, stem_from_name
    mf = load_manifest()
    if not names:
        listing = subprocess.run([str(ENV_PY), '-u', str(BRINGUP), '--list'],
                                 cwd=str(BASE), capture_output=True, text=True, timeout=90)
        print(listing.stdout, end='', flush=True)
        if listing.returncode:
            print(listing.stderr, file=sys.stderr)
            return 1
        names = [item['name'] for item in parse_file_list(listing.stdout)]
        if not names:
            print('[ble] 卡上文件列表为空', file=sys.stderr)
            return 0
    to_import = []
    for name in names:
        stem = stem_from_name(name)
        cur = mf['files'].get(stem, {})
        existing = AUDIO_DIR / Path(cur.get('path', '_missing_')).name
        if not force and cur.get('status') == 'done' and cur.get('sha256') and existing.is_file() and sha256_file(existing) == cur['sha256']:
            print('[ble] skip ' + stem, file=sys.stderr)
        else:
            to_import.append(name)
    if not to_import:
        print('[ble] 无待采文件', file=sys.stderr)
        return 0
    # Download into staging: the upstream tool may overwrite, the archive must not.
    import tempfile
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ble-', dir=str(BASE)) as tmp:
        base_cmd = [str(ENV_PY), '-u', str(BRINGUP), '--out', tmp, '--filename', ','.join(to_import)]
        result = subprocess.run(base_cmd, cwd=str(BASE), text=True)
        for source in Path(tmp).glob('*'):
            if not source.is_file():
                continue
            target = AUDIO_DIR / source.name
            if target.exists():
                if sha256_file(source) != sha256_file(target):
                    print('[ble] 同名文件不同，拒绝覆盖：' + source.name, file=sys.stderr)
                    return 1
            else:
                shutil.copy2(source, target)
    download_failed = result.returncode != 0
    # import 后落 manifest（BLE 下载件可能带前导污染，probe 标出来）
    pending = 0
    for list_name in to_import:
        n = stem_from_name(list_name)
        dst = AUDIO_DIR / f"{n}.opus"
        if not dst.is_file():
            dst = AUDIO_DIR / f"{n}.wav"
        if not dst.is_file():
            mf["files"][n] = {"status": "failed", "source": "ble",
                              "collected_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            session_flow_event(n, "collect-fail", channel="ble", reason="not-imported")
            pending += 1
            continue
        probe = probe_opus(dst) if dst.suffix.lower() == ".opus" else "n/a"
        status = "done" if dst.suffix.lower() != ".opus" or probe in ("clean", "contaminated") else "suspect"
        rec = {"status": status, "source": "ble", "path": str(dst.relative_to(BASE)),
               "size": dst.stat().st_size, "sha256": sha256_file(dst),
               "opus_probe": probe, "collected_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if dst.suffix.lower() == ".opus":
            wav = opus_to_wav(dst)
            if wav:
                rec["wav_path"] = str(wav.relative_to(BASE))
        mf["files"][n] = rec
        save_manifest(mf)
        session_flow_event(n, "collected", channel="ble", bytes=rec["size"],
                           opus_probe=probe, verified=(status == "done"))
        print(f"  [ok] {n} ({rec['size']//1024}KB) probe={probe}{'' if status=='done' else ' ⚠'}",
              file=sys.stderr)
        if status != "done":
            pending += 1
    return pending or int(download_failed)


def realtime_capture(seconds: int) -> None:
    """BLE 实时推流（特殊场景：现场直播转写），非文件采集。"""
    stamp = time.strftime("realtime%Y%m%d-%H%M%S")
    cmd = [str(ENV_PY), str(BRINGUP), "--realtime", str(seconds), "--out", str(AUDIO_DIR)]
    print(f"  [realtime] {cmd[2:]} （{seconds}s，完成后落 {AUDIO_DIR}/{stamp}*）", file=sys.stderr)
    subprocess.run(cmd, cwd=str(BASE))
    mf = load_manifest()
    for p in AUDIO_DIR.glob(f"{stamp}.*"):
        if p.suffix.lower() in (".raw", ".ogg", ".opus", ".wav"):
            mf["files"][p.stem] = {"status": "done", "source": "ble-realtime",
                                   "path": str(p.relative_to(BASE)), "size": p.stat().st_size,
                                   "collected_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            session_flow_event(p.stem, "collected", channel="ble-realtime", seconds=seconds)
    save_manifest(mf)


# ── status ────────────────────────────────────────────────────────

def status(volume_name: str | None, source_dir: str | None, json_out: bool) -> int:
    vol = find_wired(volume_name, source_dir)
    mf = load_manifest()
    files = mf.get("files", {})
    counts = {}
    for f in files.values():
        counts[f.get("status", "unknown")] = counts.get(f.get("status", "unknown"), 0) + 1
    vol_files = card_files(vol) if vol else []
    vol_names = {p.stem for p in vol_files}
    not_collected = sorted(vol_names - set(files.keys()))
    suspect = sorted(f for f, d in files.items() if d.get("status") == "suspect")
    incomplete = sorted(f for f, d in files.items() if d.get("status") in ("incomplete", "failed"))

    data = {
        "wired": str(vol) if vol else None,
        "wired_files": [p.name for p in vol_files],
        "not_collected": not_collected,
        "suspect": suspect,
        "incomplete": incomplete,
        "manifest": {k: counts.get(k, 0) for k in ("done", "suspect", "incomplete", "failed")},
        "total": len(files),
    }
    if json_out:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print("=== 采集状态 ===")
    print(f"wired  : {'挂载 ' + str(vol) if vol else '未挂载'}"
          + (f"（{len(vol_files)} 个候选文件）" if vol_files else ""))
    for s in suspect:
        d = files[s]
        print(f"  ⚠ {s} suspect: opus_probe={d.get('opus_probe')}（{d.get('volume') or d.get('source')}）")
    for s in incomplete:
        print(f"  ! {s} {files[s].get('status')}（下轮重拷）")
    if not vol_files:
        print("pending: 无（wired 未见未采文件）")
    else:
        print(f"pending: {', '.join(not_collected) if not_collected else '无（卷上文件均已采）'}")
    m = data["manifest"]
    print(f"manifest: {data['total']} 文件 — done {m['done']} / suspect {m['suspect']} / "
          f"incomplete {m['incomplete']} / failed {m['failed']}")
    if suspect or incomplete:
        print("下一步: 重插/重连后 collect（suspect/incomplete 自动重拷校验）")
    elif not vol_files:
        print("下一步: 需要 BLE 补传时用 collect --mode ble；插 TypeC 线会自动（watch）采")
    return 0


# ── collect ───────────────────────────────────────────────────────

def collect(mode: str, volume_name: str | None, source_dir: str | None,
            realtime_s: int, watch: bool, interval: int,
            ble_names: list[str], ble_scan_s: int) -> int:
    if realtime_s:
        realtime_capture(realtime_s)
        return 0
    while True:
        vol = find_wired(volume_name, source_dir)
        use = mode
        if use == "auto":
            use = "wired" if vol else "ble"
        if use == "wired":
            if vol is None:
                if mode == "wired":
                    print("[error] --mode wired 但 TypeC 未挂载（/Volumes 未见候选卷）", file=sys.stderr)
                    return 1
                print("[auto] wired 未挂载，回退 BLE", file=sys.stderr)
                use = "ble"
            else:
                print(f"[collect] wired: {vol}（插线自动采）", file=sys.stderr)
                remaining = collect_wired(vol)
        elif use == "ble":
            if not ble_reachable(ble_scan_s):
                if mode == "ble":
                    print(f"[error] --mode ble 但 {ble_scan_s}s 内 BLE 未见设备（确认卡已开机+配对）", file=sys.stderr)
                    return 1
                print(f"[auto] BLE {ble_scan_s}s 内未见设备，本轮跳过", file=sys.stderr)
            else:
                print(f"[collect] ble（手动激活：scan→connect→import）", file=sys.stderr)
                remaining = collect_ble(ble_names, force=False)
        else:
            remaining = 0

        # realtime 特殊：跑完一次即结束（非文件采集）
        if realtime_s:
            realtime_capture(realtime_s)
            return 0

        if not watch:
            return 1 if remaining else 0
        if remaining == 0 and not (vol and card_files(vol)):
            print("[watch] 全部采完，退出", file=sys.stderr)
            return 0
        print(f"[watch] 未完成 {remaining} 项，{interval}s 后再查"
              + (f"（{vol}）" if vol else ""), file=sys.stderr)
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="qs668 采集控制器（wired/BLE，可控+可观测）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="看采集进度/完成情况")
    p.add_argument("--volume")
    p.add_argument("--source-dir")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("collect", help="采集音频进 archive/audio")
    p.add_argument("--mode", choices=["auto", "wired", "ble"], default="auto")
    p.add_argument("--volume", help="wired 指定 /Volumes 下卷名")
    p.add_argument("--source-dir", help="测试/自定义来源目录（绕过 /Volumes 探测）")
    p.add_argument("--realtime", type=int, metavar="SECONDS", help="BLE 实时推流 N 秒")
    p.add_argument("--watch", action="store_true", help="轮询：插线自动采，全 done 退出")
    p.add_argument("--interval", type=int, default=5, help="watch 轮询间隔秒（默认 5）")
    p.add_argument("--ble-name", action="append", default=[], help="BLE 指定导入文件名（可重复）")
    p.add_argument("--ble-scan-sec", type=int, default=2)

    args = ap.parse_args()
    if args.cmd == "status":
        return status(args.volume, args.source_dir, args.json)
    return collect(args.mode, args.volume, args.source_dir,
                   args.realtime or 0, args.watch, args.interval,
                   args.ble_name, args.ble_scan_sec)


if __name__ == "__main__":
    sys.exit(main())
