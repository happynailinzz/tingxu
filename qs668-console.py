#!/usr/bin/env python3
"""听序本地录音工作台：文件管理、音频播放、任务进度与模型配置。

页面资源位于 webui/，文件与设置 API 位于 qs668_web_api.py，模型任务由
qs668-web-task.py 在 FunASR 虚拟环境执行。保留插卡监听、单实例保护与蓝牙工具。

用法：python3 qs668-console.py [--port 8765] [--no-notify] [--once]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import uuid
import qs668_web_api as web_api
from qs668_runtime import ble_python, failure_detail
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
PY = str(BASE / "funasr-env" / "bin" / "python") if (BASE / "funasr-env" / "bin" / "python").is_file() else sys.executable
COLLECTOR = BASE / "qs668-collector.py"
BATCH_ASR = BASE / "qs668-batch-asr.py"
SUMMARIZE = BASE / "qs668-summarize.py"
BRINGUP = BASE / "nextproto" / "tools" / "qs668-bringup.py"
ARCH = BASE / "nextproto" / "archive"
AUDIO_DIR = ARCH / "audio"
CORPUS_DIR = ARCH / "corpus"
BRIEF_DIR = ARCH / "brief"
STATE_FILE = Path.home() / ".qs668-console-state.json"

LOCK = threading.Lock()


# ── 持久状态（auto_open / 实际端口）────────────────────────────
def load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"auto_open": True, "port": 8765}


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2))


def pick_port(prefer: int) -> int:
    for p in range(prefer, prefer + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"端口 {prefer}..{prefer+19} 全被占")


# ── 复用 collector 的挂载探测（只 import 不跑 main）────────────
def _collector_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("qs668_collector", str(COLLECTOR))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def wired_mounts() -> list[dict]:
    try:
        m = _collector_mod()
    except Exception:
        return []
    out = []
    for v in m.wired_volumes():
        if not (v / "record").is_dir():
            continue
        try:
            files = m.card_files(v)
        except Exception:
            files = []
        out.append({"name": v.name, "path": str(v), "files": [p.name for p in files],
                    "stems": [p.stem for p in files]})
    return out


# ── job 系统：后台线程 + 行级流式日志 ─────────────────────────
class Job:
    def __init__(self, jid: str, kind: str):
        self.id = jid
        self.kind = kind
        self.status = "running"
        self.log: list[str] = []
        self.result = None
        self.started = time.strftime("%H:%M:%S")
        self.started_at = time.monotonic()
        self.finished_at = None
        self._lock = threading.Lock()

    def add(self, line: str) -> None:
        with self._lock:
            self.log.append(line)
            if len(self.log) > 800:  # 截尾，防 UI 崩
                del self.log[: len(self.log) - 800]

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            tail = self.log[since:]
        return {"id": self.id, "kind": self.kind, "status": self.status,
                "started": self.started, "new": tail, "result": self.result,
                "count": len(self.log), "stages": [dict(s) for s in getattr(self, "stages", [])],
                "message": getattr(self, "message", ""), "elapsed": round((self.finished_at or time.monotonic()) - self.started_at)}


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _stream_step(job: Job, label: str, cmd: list[str], cwd: str) -> int:
    """跑一条命令，把 stdout+stderr 逐行灌进 job log（live 进度）。"""
    job.add(f"▶ {label}\n  $ {' '.join(str(c) for c in cmd[1:]) if len(cmd) > 1 else cmd[0]}")
    try:
        p = subprocess.Popen([str(c) for c in cmd], cwd=cwd,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except Exception as e:
        job.add(f"  ✗ 起进程失败: {e}")
        job.status = "failed"
        job.message = f"{label} 无法启动：{e}"
        return 127
    recent_output = []
    for line in p.stdout:  # 已合并 stderr
        recent_output.append(line.rstrip())
        recent_output = recent_output[-12:]
        if getattr(job, "ble_listing", False):
            from qs668_ble import parse_file_list
            entries = parse_file_list(line)
            if entries:
                job.ble_files.extend(entries)
        if line.startswith("@qs668-progress "):
            try:
                event = json.loads(line.split(" ", 1)[1])
                stage = job.stages[job.current_stage]
                stage["completed"] = event["completed"]
                stage["total"] = event["total"]
                job.message = stage["label"] + " " + event["stem"]
            except (ValueError, KeyError, IndexError, AttributeError):
                job.add("进度事件无法解析")
            continue
        job.add(line.rstrip())
        if line.strip():
            job.message = line.strip()[:180]
    p.wait()
    if p.returncode:
        job.status = "failed"
        job.message = f"{label} 失败（exit={p.returncode}）：{failure_detail(recent_output)}"
    job.add(f"◀ {label} exit={p.returncode}")
    return p.returncode


def new_job(kind: str) -> Job:
    jid = time.strftime("%m%d-%H%M%S") + f"-{kind[:6]}-" + uuid.uuid4().hex[:6]
    job = Job(jid, kind)
    with JOBS_LOCK:
        JOBS[jid] = job
        if len(JOBS) > 40:  # 清旧的
            for k in list(JOBS)[: len(JOBS) - 40]:
                JOBS.pop(k, None)
    return job


def _run_closing_loop(job: Job, mode: str, do_transcribe: bool) -> None:
    """同步转存 → （可选）批量转写 → （可选）给新语料出纪要。幂等闭环。"""
    rc = _stream_step(job, "① 采集（wired 优先，幂等）",
                      [PY, str(COLLECTOR), "collect", "--mode", mode], str(BASE))
    if rc != 0:
        job.add(f"⚠ 采集未完成 exit={rc}（可能是 BLE 未见设备/拔线），继续看下方日志")
    if not do_transcribe:
        job.status = "done" if rc == 0 else "failed"
        job.result = {"collected": rc == 0, "transcribed": False}
        return
    if rc:
        job.status = "failed"
        return
    rc = _stream_step(job, "② 批量转写（--pending 幂等，已有命中 skip）",
                 [PY, str(BATCH_ASR), "--pending"], str(BASE))
    if rc:
        job.status = "failed"
        return
    rc = _stream_step(job, "③ 给新语料出纪要（已有 brief 的 skip）", [PY, str(SUMMARIZE), "--pending"], str(BASE))
    job.status = "done" if rc == 0 else "failed"
    job.result = {"collected": rc == 0, "transcribed": True}


def _run_collect(job: Job, mode: str, ble_names: list[str]) -> None:
    cmd = [PY, str(COLLECTOR), "collect", "--mode", mode]
    for n in ble_names:
        cmd += ["--ble-name", n]
    rc = _stream_step(job, "采集", cmd, str(BASE))
    job.status = "done" if rc == 0 else "failed"


def _run_ble(job: Job, op: str, record: str, seconds: int, names: list[str]) -> None:
    ble_py = ble_python(BASE, PY)
    job.add("BLE Python: " + ble_py)
    if op == "scan":
        _stream_step(job, "BLE 扫描", [ble_py, "-u", str(BRINGUP), "--scan"], str(BASE))
    elif op == "status":
        _stream_step(job, "BLE 状态（电量/容量/固件）", [ble_py, "-u", str(BRINGUP), "--status"], str(BASE))
    elif op == "list":
        job.ble_listing = True
        job.ble_files = []
        rc = _stream_step(job, "列卡上文件", [ble_py, "-u", str(BRINGUP), "--list"], str(BASE))
        job.result = {"list_ok": rc == 0, "files": job.ble_files}
    elif op == "download-all":
        _stream_step(job, "BLE 下载全部", [ble_py, "-u", str(COLLECTOR), "collect", "--mode", "ble"], str(BASE))
    elif op == "download-selected":
        cmd = [ble_py, "-u", str(COLLECTOR), "collect", "--mode", "ble"]
        for n in names:
            cmd += ["--ble-name", n]
        _stream_step(job, f"BLE 下载选中 {len(names)} 项", cmd, str(BASE))
    elif op == "record":
        _stream_step(job, f"录音控制 {record}", [ble_py, "-u", str(BRINGUP), "--record", "save" if record == "stop" else record], str(BASE))
    elif op == "record-state":
        _stream_step(job, "读录音状态", [ble_py, "-u", str(BRINGUP), "--record-state"], str(BASE))
    elif op == "realtime":
        _stream_step(job, f"BLE 实时推流 {seconds}s",
                     [ble_py, "-u", str(COLLECTOR), "collect", "--realtime", str(seconds)], str(BASE))
    if job.status != "failed":
        job.status = "done"


# ── 看门狗：插线自动弹出/通知 ─────────────────────────────────
def start_watcher(port: int, stop_flag: dict) -> None:
    m = None
    # Include cards already mounted when the console starts. Keep watching while
    # auto_open is disabled so the settings toggle takes effect without a restart.
    notified: set[str] = set()
    url = f"http://127.0.0.1:{port}"
    while not stop_flag["stop"]:
        try:
            if m is None:
                m = _collector_mod()
            volumes = {v.name: v for v in m.wired_volumes()}
            for name in sorted(notified.difference(volumes)):
                print(f"[watcher] 已移除: {name}，等待下次插入", flush=True)
            notified.intersection_update(volumes)
            if load_state().get("auto_open", True):
                for name, volume in volumes.items():
                    if name in notified:
                        continue
                    nfile = len(m.card_files(volume))
                    # JSON string quoting also escapes volume names in AppleScript.
                    message = json.dumps(f"有 {nfile} 条录音 · 请在管理台同步", ensure_ascii=False)
                    subtitle = json.dumps(str(volume), ensure_ascii=False)
                    try:
                        result = subprocess.run(
                            ["osascript", "-e", f'display notification {message} '
                             f'with title "听序录音卡插入" subtitle {subtitle}'],
                            timeout=8, capture_output=True, text=True)
                        if result.returncode:
                            print(f"[watcher] 通知失败: {result.stderr.strip()}", flush=True)
                    except Exception as exc:
                        print(f"[watcher] 通知失败: {exc}", flush=True)
                    result = subprocess.run(["open", url], timeout=8,
                                            capture_output=True, text=True)
                    if result.returncode:
                        print(f"[watcher] 打开网页失败: {result.stderr.strip()}", flush=True)
                        continue
                    notified.add(name)
                    print(f"[watcher] 已打开管理台: {volume} ({nfile} 条录音)", flush=True)
        except Exception as exc:
            print(f"[watcher] 检测失败，下轮重试: {exc}", flush=True)
        time.sleep(2.5)


# ── HTTP 处理 ──────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    port = 8765
    stop_flag = {"stop": False}

    def log_message(self, *a):  # 静默默认访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self) -> None:
        if self.headers.get("Host") not in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}:
            self._json({"error": "无效本机服务地址"}, 403)
            return
        if web_api.get(self, sys.modules[__name__]):
            return
        if self.path == "/api/status":
            mounts = wired_mounts()
            mf = json.loads(BASE.joinpath("collected-manifest.json").read_text()) if (BASE / "collected-manifest.json").is_file() else {"files": {}}
            files = mf.get("files", {})
            counts = {}
            for f in files.values():
                counts[f.get("status", "unknown")] = counts.get(f.get("status", "unknown"), 0) + 1
            uncollected = []
            for mt in mounts:
                uncollected += sorted(set(mt["stems"]) - set(files.keys()))
            self._json({"port": self.port, "wired": mounts,
                        "uncollected": sorted(set(uncollected)),
                        "manifest_counts": counts, "total": len(files),
                        "auto_open": load_state().get("auto_open", True)})
        elif self.path == "/api/manifest":
            p = BASE / "collected-manifest.json"
            self._json(json.loads(p.read_text()) if p.is_file() else {"files": {}})
        elif self.path.startswith("/api/jobs/"):
            from urllib.parse import urlparse, parse_qs
            q = urlparse(self.path)
            jid = q.path.split("/")[-1]
            since = 0
            s = parse_qs(q.query).get("since", ["0"])[0]
            try:
                since = int(s)
            except Exception:
                since = 0
            with JOBS_LOCK:
                job = JOBS.get(jid)
            if not job:
                self._json({"error": "no such job"}, 404)
                return
            snap = job.snapshot(since)
            self._json(snap)
        elif self.path == "/api/settings":
            self._json(load_state())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        with web_api.REQUEST_LOCK:
            self._do_POST()

    def _do_POST(self) -> None:
        from urllib.parse import urlparse, parse_qs
        # Local credentials/file mutations must not be callable by arbitrary websites.
        expected = f"http://127.0.0.1:{self.port}"
        if self.headers.get("Host") not in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}:
            self._json({"error": "无效本机服务地址"}, 403)
            return
        origin = self.headers.get("Origin")
        if origin and origin not in {expected, f"http://localhost:{self.port}"}:
            self._json({"error": "不允许跨站请求"}, 403)
            return
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._json({"error": "不允许跨站请求"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                raise ValueError("无效请求长度")
            if urlparse(self.path).path == "/api/upload":
                web_api.require_idle(sys.modules[__name__])
                if not 0 < length <= 1024 * 1024 * 1024:
                    raise ValueError("上传须为 1 字节至 1 GB")
                name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
                path = web_api.Library(BASE).import_file(name, self.rfile, length)
                self._json({"ok": True, "stem": path.stem})
                return
            if length > 256 * 1024:
                raise ValueError("请求过大")
            body = json.loads(self.rfile.read(length).decode() or "{}") if length else {}
            if not isinstance(body, dict):
                raise ValueError("请求须为 JSON 对象")
        except (ValueError, FileExistsError) as exc:
            self._json({"error": str(exc)}, 400)
            return
        if web_api.post(self, sys.modules[__name__], body):
            return
        if self.path in {"/api/collect", "/api/pipeline", "/api/ble", "/api/eject"}:
            try:
                web_api.require_idle(sys.modules[__name__])
            except ValueError as exc:
                self._json({"error": str(exc)}, 409)
                return

        if self.path == "/api/collect":
            job = new_job("collect")
            threading.Thread(target=_job_wrap, args=(job, _run_collect,
                          (job, body.get("mode", "auto"), body.get("ble_names", []))),
                          daemon=True).start()
            self._json({"job_id": job.id})

        elif self.path == "/api/pipeline":
            job = new_job("pipeline")
            threading.Thread(target=_job_wrap, args=(job, _run_closing_loop,
                          (job, body.get("mode", "auto"), bool(body.get("transcribe", True)))),
                          daemon=True).start()
            self._json({"job_id": job.id})

        elif self.path == "/api/ble":
            job = new_job("ble")
            threading.Thread(target=_job_wrap, args=(job, _run_ble,
                          (job, body.get("op", "scan"), body.get("record", "start"),
                           int(body.get("seconds", 30)), body.get("names", []))),
                          daemon=True).start()
            self._json({"job_id": job.id})

        elif self.path == "/api/eject":
            vol = body.get("volume")
            if not vol:
                # 无明确目标卷 → 拒绝（diskutil eject 无参 = 弹出所有可移动卷，危险）
                self._json({"ok": False, "msg": "未定位到具体卷，不执行全量 eject（防误弹其它盘）"})
                return
            cmd = ["diskutil", "eject", vol]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                ok = r.returncode == 0
                self._json({"ok": ok, "msg": (r.stdout + r.stderr).strip()})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)})

        elif self.path == "/api/settings":
            st = load_state()
            if "auto_open" in body:
                st["auto_open"] = bool(body["auto_open"])
                save_state(st)
            self._json(st)
        else:
            self._json({"error": "not found"}, 404)


def _job_wrap(job: Job, fn, args) -> None:
    try:
        fn(*args)
    except Exception as e:
        job.add(f"✗ 异常: {e}")
        job.status = "failed"
        job.message = str(e)
    if job.status == "running":
        job.status = "done"
    job.finished_at = time.monotonic()


# Web assets live in webui/.

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-notify", action="store_true", help="关插线自动弹出/系统通知")
    ap.add_argument("--once", action="store_true", help="自检：打印 URL 后退出")
    args = ap.parse_args()

    # A second console would open duplicate tabs and choose a different port.
    lock_file = (BASE / ".qs668-console.lock").open("a+")
    if not args.once:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            print("[qs668-console] 管理台已在运行，请勿重复启动", flush=True)
            return 0

    stop = {"stop": False}
    srv = None
    watcher = None
    previous_term = signal.getsignal(signal.SIGTERM)

    def request_stop(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, request_stop)
        port = pick_port(args.port)
        st = load_state(); st["port"] = port
        if args.no_notify:
            st["auto_open"] = False
        save_state(st)

        Handler.port = port
        url = f"http://127.0.0.1:{port}"
        print(f"[qs668-console] 管理台 {url}  （wired 看门狗{'' if st.get('auto_open') else '·关'}）", flush=True)
        if args.once:
            print(url)
            _ = wired_mounts()
            return 0

        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        watcher = threading.Thread(target=start_watcher, args=(port, stop), daemon=True)
        watcher.start()
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[qs668-console] 正在停止插卡监听与管理台", flush=True)
    finally:
        stop["stop"] = True
        if srv is not None:
            srv.server_close()
        if watcher is not None:
            watcher.join(timeout=1)
        signal.signal(signal.SIGTERM, previous_term)
        lock_file.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
