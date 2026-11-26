#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
埋点小岛 —— trackcheck 的本地可视化网站

    python trackcheck_web.py --spec samples/trackcheck.yaml
    python trackcheck_web.py --spec trackcheck.yaml --port 8765 --no-browser

只监听 127.0.0.1，仅依赖 Python 标准库 + PyYAML。
"""
import argparse
import base64
import contextlib
import copy
import glob
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from collections import Counter, OrderedDict, defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from urllib.parse import quote as urllib_quote

import yaml

import trackcheck as tc

FROZEN = bool(getattr(sys, "frozen", False))          # PyInstaller 打包后的 exe
HERE = os.path.dirname(os.path.abspath(__file__))
RES = getattr(sys, "_MEIPASS", HERE)                  # 打包后网页资源解压在临时目录
WEB = os.path.join(RES, "web")
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else HERE
DATA_DIRNAME = "埋点小岛数据"
DOC_EXTS = (".md", ".markdown", ".csv", ".tsv", ".txt", ".json")
SEED_DIRNAME = "seed_data"                              # 打包时塞进 exe 的默认数据（首次运行自动释放）
SAMPLE_DB_LIMIT = 3 * 1024 * 1024                      # 分享时附带的数据库样本上限
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HTTP_SERVER = None


class ServerLogger:
    """无黑框运行时的服务日志收集器：同时写入内存环形缓冲与 server.log，并追踪未读异常数。"""
    MAX_LINES = 1500
    MAX_BYTES = 512 * 1024

    def __init__(self):
        self.lock = threading.RLock()
        self.lines = deque(maxlen=self.MAX_LINES)
        self.err_count = 0
        self.path = ""
        self._buf = {"INFO": "", "ERROR": ""}
        self._orig_out = sys.stdout
        self._orig_err = sys.stderr

    def install(self):
        sys.stdout = _LogStream(self, "INFO", self._orig_out)
        sys.stderr = _LogStream(self, "ERROR", self._orig_err)

    def bind_file(self, path):
        with self.lock:
            self.path = os.path.abspath(path)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            try:
                if os.path.isfile(self.path) and os.path.getsize(self.path) > self.MAX_BYTES:
                    with open(self.path, "rb") as f:
                        f.seek(-self.MAX_BYTES // 2, os.SEEK_END)
                        tail = f.read().decode("utf-8", "replace")
                    nl = tail.find("\n")
                    if nl >= 0:
                        tail = tail[nl + 1:]
                    with open(self.path, "w", encoding="utf-8") as f:
                        f.write(tail)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 埋点小岛启动 (PID {os.getpid()}) =====\n")
                    for e in self.lines:
                        f.write(f"[{e['time']}] [{e['level']}] {e['text']}\n")
            except Exception:
                pass

    def emit(self, level, text):
        clean = ANSI.sub("", text).rstrip("\r\n")
        if not clean.strip():
            return
        ts = time.strftime("%H:%M:%S")
        is_err = level == "ERROR" or "[error]" in clean.lower() or clean.lstrip().startswith("Traceback ")
        lvl = "ERROR" if is_err else level
        entry = {"time": ts, "level": lvl, "text": clean}
        with self.lock:
            self.lines.append(entry)
            if is_err and not clean.lstrip().startswith(('File "', "During handling")):
                # 同一段 Traceback 只计一次异常提醒，避免刷几十个红点
                prev_err = len(self.lines) >= 2 and self.lines[-2]["level"] == "ERROR" and (self.lines[-2]["time"] == ts)
                if not prev_err:
                    self.err_count += 1
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(f"[{ts}] [{lvl}] {clean}\n")
                except Exception:
                    pass

    def write_chunk(self, level, s):
        if not s:
            return
        with self.lock:
            buf = self._buf.get(level, "") + s
            parts = buf.split("\n")
            self._buf[level] = parts[-1]
            for line in parts[:-1]:
                self.emit(level, line)

    def flush_partial(self):
        with self.lock:
            for lvl in ("INFO", "ERROR"):
                rem = self._buf.get(lvl, "")
                if rem.strip():
                    self.emit(lvl, rem)
                self._buf[lvl] = ""

    def snapshot(self, mark_seen=False):
        self.flush_partial()
        with self.lock:
            if mark_seen:
                self.err_count = 0
            return {
                "lines": list(self.lines),
                "err_count": self.err_count,
                "path": self.path,
            }

    def clear(self):
        with self.lock:
            self.lines.clear()
            self.err_count = 0
            self._buf = {"INFO": "", "ERROR": ""}


class _LogStream(io.TextIOBase):
    encoding = "utf-8"

    def __init__(self, logger, level, orig):
        self._logger = logger
        self._level = level
        self._orig = orig

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        if self._orig is not None:
            try:
                self._orig.write(s)
                self._orig.flush()
            except Exception:
                pass
        self._logger.write_chunk(self._level, s)
        return len(s)

    def flush(self):
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:
                pass

    def isatty(self):
        return False


SERVER_LOG = ServerLogger()

SKELETON = {
    "options": {"null_equals_empty": True, "strict_fields": False},
    "log": {"pattern": tc.DEFAULT_PATTERN, "logcat_args": []},
    "device": {"package": "com.example.app", "pull": []},
    "sources": {},
    "dicts": {},
    "templates": {},
    "events": {},
    "cases": {},
}


class ApiError(Exception):
    pass


class FileResponse:
    def __init__(self, data, filename, ctype="application/octet-stream"):
        self.data, self.filename, self.ctype = data, filename, ctype


def rule_dict_refs(fields):
    """字段规则里引用到的字典名（enum: 名字 / map: 名字 或 [名字...]）"""
    names = set()
    for r in (fields or {}).values():
        if not isinstance(r, dict):
            continue
        if isinstance(r.get("enum"), str):
            names.add(r["enum"])
        m = r.get("map")
        for x in (m if isinstance(m, list) else [m]):
            if isinstance(x, str):
                names.add(x)
    return names


def dict_value(items, events, desc=""):
    v = {}
    if desc:
        v["desc"] = desc
    v["events"] = [str(x) for x in events]
    v["items"] = items
    return v


def plain(o):
    """OrderedDict / tuple -> 普通 dict / list，方便 yaml.safe_dump"""
    if isinstance(o, dict):
        return {(k if isinstance(k, (int, float, bool)) else str(k)): plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [plain(x) for x in o]
    return o


def yload(p):
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def ydump(d, p):
    text = yaml.safe_dump(plain(d), allow_unicode=True, sort_keys=False, default_flow_style=None, width=160)
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 由 埋点小岛 保存（原注释不保留，上一个版本备份在同名 .bak）\n" + text)


# ----------------------------------------------------------------------------
# 规则文件读写（支持 include 拆分：点位在哪个文件里就写回哪个文件）
# ----------------------------------------------------------------------------
class SpecStore:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        if not os.path.exists(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            ydump(SKELETON, self.path)
        self.load()

    def load(self):
        self.files = OrderedDict()
        self.files[self.path] = yload(self.path)
        base = os.path.dirname(self.path)
        for inc in self.files[self.path].get("include") or []:
            for f in sorted(glob.glob(os.path.join(base, inc))):
                self.files[os.path.abspath(f)] = yload(f)

    @property
    def main(self):
        return self.files[self.path]

    def find(self, section, key):
        for p, d in self.files.items():
            for k in (d.get(section) or {}):
                if str(k) == str(key):
                    return p, k
        return None, None

    def get(self, section, key):
        p, k = self.find(section, key)
        return (self.files[p][section][k], p) if p else (None, None)

    def items(self, section):
        out = OrderedDict()
        for p, d in self.files.items():
            for k, v in (d.get(section) or {}).items():
                out[str(k)] = (v, p)
        return out

    def put(self, section, key, value, old_key=None, save=True):
        p, k = self.find(section, old_key if old_key not in (None, "") else key)
        if p is None:
            p = self.path
        d = self.files[p]
        new, done = {}, False
        for kk, vv in (d.get(section) or {}).items():
            if k is not None and kk == k:
                new[key] = value
                done = True
            elif str(kk) == str(key):
                continue
            else:
                new[kk] = vv
        if not done:
            new[key] = value
        d[section] = new
        if save:
            self.save(p)
        return p

    def delete(self, section, key):
        p, k = self.find(section, key)
        if p is None:
            raise ApiError(f"{section} 中没有 {key}")
        del self.files[p][section][k]
        self.save(p)

    def put_top(self, key, value):
        self.main[key] = value
        self.save(self.path)

    def save(self, p):
        if os.path.exists(p):
            shutil.copyfile(p, p + ".bak")
        ydump(self.files[p], p)


def mk_event(eid, fields, cls=None, raw="", seq=1):
    return tc.Event(seq=seq, id=str(eid), cls=cls, fields=OrderedDict((k, v) for k, v in (fields or {}).items()),
                    raw=raw, ts="", tsv=None, line_no=None)


def file_info(p):
    if p and os.path.exists(p):
        st = os.stat(p)
        return {"exists": True, "path": p, "size": st.st_size, "mtime": time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime))}
    return {"exists": False, "path": p}


def fmt_kb(n):
    return f"{n} B" if n < 1024 else (f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB")


# ----------------------------------------------------------------------------
# 实时调试会话
# ----------------------------------------------------------------------------
class LiveSession:
    """实时调试：抓 logcat；需要时在事件前后自动拍 DB 快照，并同步捕获点位/模板绑定的接口响应 JSON"""

    def __init__(self, app, serial, clear, case, only, scenario=None, snapshot=True, capture_apis=None):
        self.app, self.serial, self.case = app, serial or None, case or None
        self.only = {str(x) for x in (only or []) if str(x).strip()}
        self.lock = threading.Lock()
        self.results, self.running, self.report = [], True, None
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        self.raw = os.path.join(app.out, f"session_{self.stamp}.log")
        self.scenario = {k: v for k, v in (scenario or {}).items() if v not in (None, "", [])}
        self.ev_scn, self.decisions, self.ev_snap, self.evs = {}, {}, {}, []
        self._ev_ts = {}
        self.version = 0
        self.pending, self.pend_cv = [], threading.Condition()   # 等待「事件后」快照的事件（一阵连发只拍一张）
        dev = app.spec.device or {}
        self.pkg, self.dbs = dev.get("package"), [str(x) for x in (dev.get("dbs") or [])]
        self.snap_on = bool(snapshot and self.pkg and self.dbs)
        self.snaps, self.snap_err = [], ""          # [{"t": 时间, "dir": 目录, "sources": Sources}]
        self.snap_root = os.path.join(app.out, "snapshots", self.stamp)
        if self.snap_on:
            self._cleanup_old()
            self._snapshot()                       # 开始前先拍一张，作为第一个事件的「事件前」
            threading.Thread(target=self._snap_worker, daemon=True).start()
        self.api_targets = [dict(t) for t in (capture_apis or []) if (t or {}).get("endpoint")]
        self.api_extractors = [(t, OkHttpApiExtractor(t["endpoint"], t.get("tag", ""))) for t in self.api_targets]
        self.api_hits, self.api_err = [], ""
        self._api_proc, self._api_thread = None, None
        self.ck = tc.Checker(app.spec, app.sources)
        share_stream = bool(self.api_targets and not app.spec.logcat_args)
        self.cap = tc.Capture(app.spec, self.serial, self.raw, self.on_event,
                              on_line=(self.on_log_line if share_stream else None))
        self.cap.start(clear=clear)
        if self.api_targets and not share_stream:
            self._start_api_stream()
        self.started = time.time()

    def _start_api_stream(self):
        """当 logcat_args 过滤了特定 TAG 时，额外开一条轻量 logcat 流专抓绑定接口的 OkHttp 响应"""
        try:
            adb = tc.adb_base(self.app.spec, self.serial)
            self._api_proc = subprocess.Popen(
                adb + ["logcat", "-v", "threadtime", "-T", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **tc.popen_kw(),
            )
            self._api_thread = threading.Thread(target=self._api_stream_loop, daemon=True)
            self._api_thread.start()
        except Exception as e:
            self.api_err = str(e)

    def _api_stream_loop(self):
        try:
            for b in iter(self._api_proc.stdout.readline, b""):
                if not self.running:
                    break
                self.on_log_line(b.decode("utf-8", errors="replace"))
        except Exception as e:
            self.api_err = str(e)

    def _record_api_hit(self, target, url, obj):
        try:
            rec = self.app._save_json_ds(target["source"], obj, url=url, source="实时调试自动抓取")
        except Exception as e:
            self.api_err = str(e)
            return
        now = time.time()
        hit = {"source": target["source"], "endpoint": target["endpoint"], "url": url,
               "time": rec.get("time") or time.strftime("%H:%M:%S"), "hist": rec.get("hist", "")}
        with self.lock:
            self.api_hits.append(hit)
            if len(self.api_hits) > 30:
                self.api_hits = self.api_hits[-30:]
            ck = tc.Checker(self.app.spec, self.app.sources)
            self.ck = ck
            # 若接口日志比刚触发的埋点晚刷完几秒，自动用最新接口数据重校近期/失败事件
            changed = False
            for idx, ev in enumerate(self.evs):
                ts = self._ev_ts.get(ev.seq, 0)
                cur = self.results[idx] if idx < len(self.results) else None
                if (now - ts <= 5.0) or (cur and cur.get("status") == "FAIL"):
                    self.results[idx] = self._check(ck, ev)
                    changed = True
            if changed:
                self.version += 1

    def on_log_line(self, line):
        if not self.api_extractors:
            return
        for target, ext in self.api_extractors:
            hit = ext.feed(line)
            if hit:
                url, obj = hit
                self._record_api_hit(target, url, obj)

    # ---- DB 快照 ----
    def _snapshot(self):
        d = os.path.join(self.snap_root, f"{len(self.snaps):03d}")
        adb = tc.adb_base(self.app.spec, self.serial)
        try:
            for db in self.dbs:
                tc.pull_app_db(adb, self.pkg, db, os.path.join(d, db))
        except Exception as e:
            self.snap_err = str(e)
            self.snap_on = False
            return None
        snap = {"t": time.time(), "dir": d, "sources": self._sources_for(d)}
        self.snaps.append(snap)
        return len(self.snaps) - 1

    def _cleanup_old(self, keep=5):
        root = os.path.dirname(self.snap_root)
        try:
            olds = sorted(glob.glob(os.path.join(root, "*")), key=os.path.getmtime)
            for d in olds[:-keep]:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    def _snap_worker(self):
        """连续到达的 SQL 事件合并成一次「事件后」快照：最后一个事件后安静 0.6 秒（最多等 2 秒）再拉一次 DB"""
        while self.running:
            with self.pend_cv:
                while not self.pending and self.running:
                    self.pend_cv.wait(0.5)
                if not self.pending:
                    continue
                first = self.pending[0][1]
                while self.running:
                    last = self.pending[-1][1]
                    now = time.time()
                    if now - last >= 0.6 or now - first >= 2.0:
                        break
                    self.pend_cv.wait(0.15)
                batch, self.pending = self.pending, []
            after = self._snapshot() if self.snap_on else None
            for ev, arrived in batch:
                before = max((i for i, s in enumerate(self.snaps) if s["t"] < arrived), default=None)
                if after is not None:
                    self.ev_snap[ev.seq] = (before, after)
                self._append(ev)
        with self.pend_cv:
            batch, self.pending = self.pending, []
        for ev, _ in batch:
            self._append(ev)

    def _append(self, ev):
        with self.lock:
            self.evs.append(ev)
            self.results.append(self._check(self.ck, ev))

    def _sources_for(self, d):
        s = copy.copy(self.app.spec)
        srcs = {}
        names = set(self.dbs)
        for k, v in self.app.spec.sources.items():
            if v.get("type") == "sqlite" and os.path.basename(str(v.get("path", ""))) in names:
                v = dict(v, path=os.path.join(d, os.path.basename(str(v["path"]))))
            srcs[k] = v
        s.sources = srcs
        return tc.Sources(s)

    def _uses_db(self, eid):
        es = self.app.spec.events.get(eid) or {}
        return any(isinstance(r, dict) and r.get("sql") for r in (es.get("fields") or {}).values())

    # ---- 事件 ----
    def on_event(self, ev):
        if self.only and ev.id not in self.only:
            return
        arrived = time.time()
        self._ev_ts[ev.seq] = arrived
        self.ev_scn[ev.seq] = dict(self.scenario)
        if self.snap_on and self._uses_db(ev.id):
            with self.pend_cv:
                self.pending.append((ev, arrived))
                self.pend_cv.notify()
            return
        self._append(ev)

    def _check(self, ck, ev):
        b, a = self.ev_snap.get(ev.seq, (None, None))
        ck.sources = self.snaps[a]["sources"] if a is not None else self.app.sources
        ck.alt_sources = self.snaps[b]["sources"] if b is not None and a is not None else None
        r = ck.check(ev, scenario=self.ev_scn.get(ev.seq), decisions=self.decisions.get(ev.seq))
        r["scenario"] = self.ev_scn.get(ev.seq) or {}
        if a is not None:
            r["snapshot"] = {"before": b, "after": a}
        return r

    def events(self):
        return list(self.evs)

    def recheck(self):
        ck = tc.Checker(self.app.spec, self.app.sources)
        with self.lock:
            self.results = [self._check(ck, e) for e in self.evs]
            self.ck = ck

    def set_scenario(self, scn, apply_pending=False):
        """apply_pending: True/'pending' = 补到待确认的事件；'all' = 已收到的全部事件按新期望值重判"""
        new = {k: v for k, v in (scn or {}).items() if v not in (None, "", [])}
        if apply_pending == "all":
            for seq in list(self.ev_scn):
                old = self.ev_scn.get(seq, {})
                self.ev_scn[seq] = {**{k: v for k, v in old.items() if k not in self.scenario}, **new}
            self.scenario = new
            self.recheck()
            return
        self.scenario = new
        if apply_pending:   # 把场景补到还在「待确认」的事件上
            for r in self.results:
                if r["status"] == "ASK" or any(f["status"] == "ASK" for f in r["fields"]):
                    self.ev_scn[r["seq"]] = {**self.ev_scn.get(r["seq"], {}), **self.scenario}
            self.recheck()

    def decide(self, seq, field, value):
        seqs = seq if isinstance(seq, (list, tuple)) else [seq]
        for sq in seqs:
            d = self.decisions.setdefault(int(sq), {})
            if value in ("ok", "bad"):
                d[field] = value
            else:
                d.pop(field, None)
        self.recheck()

    def stop(self, pull=False):
        self.cap.stop(linger=0.3)
        self.running = False
        if self._api_proc and self._api_proc.poll() is None:
            try:
                self._api_proc.terminate()
            except Exception:
                pass
        if self._api_thread:
            self._api_thread.join(timeout=2)
        for target, ext in self.api_extractors:
            for url, obj in ext.flush():
                self._record_api_hit(target, url, obj)
        with self.pend_cv:
            self.pend_cv.notify_all()
        time.sleep(0.3)
        with self.pend_cv:                     # 还没来得及拍快照的事件直接按当前数据校验
            batch, self.pending = self.pending, []
        for ev, _ in batch:
            self._append(ev)
        log = ""
        if pull:
            log = self.app.pull_log()
            self.app.reload()
        self.recheck()
        rep = tc.build_report(self.app.spec, self.results, self.case, {"log": self.raw})
        tc.write_report(rep, self.app.out, f"trackcheck_{self.stamp}")
        self.report = f"trackcheck_{self.stamp}"
        return {"report": self.report, "summary": rep["summary"], "pull_log": log}


# ----------------------------------------------------------------------------
# OkHttp logcat 接口响应抓取（只提取指定接口的返回 JSON）
# ----------------------------------------------------------------------------
_LOGCAT_LINE_RE = re.compile(
    r"^(?:\d{2}-\d{2}\s+)?(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3})\s+(?P<pid>\d+)\s+(?P<tid>\d+)\s+[VDIWEFA]\s+(?P<tag>[^:]+?)\s*:\s?(?P<msg>.*)$"
)
_OKHTTP_REQ_RE = re.compile(r"^-->\s+(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b", re.I)
_OKHTTP_RESP_RE = re.compile(r"^<--\s+(\d{3})\b")
_URL_RE = re.compile(r"(https?://[^\s'\"<>]+|/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+)")
_BOX_PREFIX_RE = re.compile(r"^[│║┃|]\s?")
_HEADER_LINE_RE = re.compile(r"^[A-Za-z0-9\-_]+:\s+")
_REQ_HINT_RE = re.compile(r"(?:^|\b)(?:request|req|requrl|requestbody|发送请求)(?:\b|[:=])", re.I)
_RESP_HINT_RE = re.compile(r"(?:^|\b)(?:response|resp|respbody|返回|响应|<--|\b200\b)(?:\b|[:=])", re.I)


def infer_api_ds_name(endpoint):
    ep = str(endpoint or "").strip()
    if re.match(r"^https?://", ep, re.I):
        ep = urlparse(ep).path or ep
    ep = ep.split("?")[0].strip().rstrip("/")
    seg = ep.rsplit("/", 1)[-1] if "/" in ep else ep
    name = re.sub(r"[^\w\-]", "_", seg).strip("_")
    return name or "api_data"


def _extract_balanced_json(text):
    """从单行/多行文本中尝试提取第一个完整 JSON 对象或数组"""
    if not text:
        return None
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = dec.raw_decode(text[i:])
                if isinstance(obj, (dict, list)):
                    return obj
            except Exception:
                continue
    return None


class OkHttpApiExtractor:
    """按 (pid, tid, tag) 分流解析 OkHttp / 自定义 HTTP 日志，仅提取匹配 endpoint 的响应 JSON"""

    def __init__(self, endpoint, tag_filter=""):
        ep = str(endpoint or "").strip()
        if re.match(r"^https?://", ep, re.I):
            ep = urlparse(ep).path or ep
        self.endpoint = ep.split("?")[0].strip()
        self.ep_low = self.endpoint.lower()
        self.tag_low = str(tag_filter or "").strip().lower()
        self.streams = {}

    def _matches(self, text):
        return bool(self.ep_low) and self.ep_low in text.lower()

    @staticmethod
    def _extract_url(text):
        m = _URL_RE.search(text)
        return m.group(1) if m else text[:120]

    @staticmethod
    def _try_parse_buf(buf):
        if not buf:
            return None
        for joined in ("".join(buf), "\n".join(buf)):
            s = joined.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, (dict, list)):
                    return obj
            except Exception:
                pass
            obj = _extract_balanced_json(s)
            if obj is not None:
                return obj
        return None

    def _emit_and_reset(self, st):
        obj = self._try_parse_buf(st["buf"])
        url = st["url"] or self.endpoint
        st.update(mode="idle", url="", buf=[], collecting=False, t=0.0)
        return (url, obj) if obj is not None else None

    def feed(self, raw_line, now=None):
        now = time.time() if now is None else now
        line = raw_line.rstrip("\r\n")
        m = _LOGCAT_LINE_RE.match(line)
        if m:
            tag = m.group("tag").strip()
            if self.tag_low and self.tag_low not in tag.lower():
                return None
            key = (m.group("pid"), m.group("tid"), tag)
            msg = m.group("msg")
        else:
            key = ("0", "0", "")
            msg = line

        clean = _BOX_PREFIX_RE.sub("", msg).strip()
        st = self.streams.setdefault(key, {"mode": "idle", "url": "", "buf": [], "collecting": False, "t": 0.0})
        if st["mode"] != "idle" and st["t"] and (now - st["t"] > 10.0):
            flushed = self._emit_and_reset(st)
            if flushed:
                return flushed

        # 1. 标准 OkHttp 请求开始 / 结束（忽略请求体）
        if _OKHTTP_REQ_RE.match(clean):
            st.update(mode="req", url="", buf=[], collecting=False, t=now)
            return None
        if clean.startswith("--> END"):
            st.update(mode="idle", url="", buf=[], collecting=False, t=0.0)
            return None

        # 2. 标准 OkHttp 响应开始
        if _OKHTTP_RESP_RE.match(clean):
            prev = self._emit_and_reset(st) if st["mode"] == "resp" and st["buf"] else None
            if self._matches(clean):
                st.update(mode="resp", url=self._extract_url(clean), buf=[], collecting=False, t=now)
            else:
                st.update(mode="idle", url="", buf=[], collecting=False, t=0.0)
            return prev

        # 3. 标准 OkHttp 响应结束或盒式日志底边
        if clean.startswith("<-- END") or clean.startswith(("└", "╚", "╰")):
            if st["mode"] == "resp":
                return self._emit_and_reset(st)
            st.update(mode="idle", url="", buf=[], collecting=False, t=0.0)
            return None

        if st["mode"] == "req":
            return None

        # 4. 正在收集目标接口的响应体
        if st["mode"] == "resp":
            st["t"] = now
            if not st["collecting"]:
                if not clean:
                    return None
                raw_no_box = _BOX_PREFIX_RE.sub("", msg)
                if clean[0] in "{[":
                    st["collecting"] = True
                    st["buf"].append(raw_no_box.lstrip())
                else:
                    # 处理同前缀如 "body: {...}" 或 "Response Body: {...}"，跳过普通 HTTP Header
                    if _HEADER_LINE_RE.match(clean) and "{" not in clean and "[" not in clean:
                        return None
                    idx = min((i for i in (raw_no_box.find("{"), raw_no_box.find("[")) if i >= 0), default=-1)
                    if idx >= 0:
                        st["collecting"] = True
                        st["buf"].append(raw_no_box[idx:])
                    else:
                        return None
            else:
                # 保留原始有效内容，防止 4KB 分片断在空格处丢空格
                payload = _BOX_PREFIX_RE.sub("", msg)
                st["buf"].append(payload)
            obj = self._try_parse_buf(st["buf"])
            if obj is not None:
                url = st["url"] or self.endpoint
                st.update(mode="idle", url="", buf=[], collecting=False, t=0.0)
                return (url, obj)
            return None

        # 5. 非标准格式（自定义日志器）：一行里同时出现接口路径和 JSON，或先打响应 URL 下行打 JSON
        if self._matches(clean):
            if _REQ_HINT_RE.search(clean) and not _RESP_HINT_RE.search(clean):
                return None
            url = self._extract_url(clean)
            pos = clean.lower().find(self.ep_low)
            tail = clean[pos + len(self.ep_low):]
            obj = _extract_balanced_json(tail)
            if obj is not None:
                return (url, obj)
            # 若没在同一行带 JSON，且包含响应特征或未标明请求，挂起等后续行的 JSON
            if _RESP_HINT_RE.search(clean) or not re.search(r"\b(?:GET|POST|PUT|DELETE|PATCH)\b", clean):
                st.update(mode="resp", url=url, buf=[], collecting=False, t=now)
        return None

    def flush(self):
        out = []
        for st in self.streams.values():
            if st["mode"] == "resp" and st["buf"]:
                r = self._emit_and_reset(st)
                if r:
                    out.append(r)
        return out


class ApiCaptureSession:
    """后台跑 adb logcat -v threadtime，实时提取目标接口的返回 JSON 并写入数据源目录与历史"""

    def __init__(self, app, endpoint, name, serial=None, clear=True, tag=""):
        self.app = app
        self.endpoint = str(endpoint or "").strip()
        self.name = re.sub(r"[^\w\-]", "_", str(name or "").strip()) or infer_api_ds_name(self.endpoint)
        self.serial = serial or None
        self.tag = str(tag or "").strip()
        self.lock = threading.Lock()
        self.running = True
        self.lines = 0
        self.captured = []
        self.error = ""
        self.proc = None
        self.extractor = OkHttpApiExtractor(self.endpoint, self.tag)
        self._start(clear=bool(clear))

    def _start(self, clear=True):
        adb = tc.adb_base(self.app.spec, self.serial)
        if clear:
            subprocess.run(adb + ["logcat", "-c"], capture_output=True, timeout=10, **tc.popen_kw())
        cmd = adb + ["logcat", "-v", "threadtime"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1, **tc.popen_kw())
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                if not self.running:
                    break
                with self.lock:
                    self.lines += 1
                hit = self.extractor.feed(line)
                if hit:
                    url, obj = hit
                    rec = self.app._save_json_ds(self.name, obj, url=url, source="logcat")
                    with self.lock:
                        self.captured.insert(0, rec)
                        del self.captured[30:]
        except Exception as e:
            with self.lock:
                self.error = str(e)
        finally:
            with self.lock:
                self.running = False

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        for url, obj in self.extractor.flush():
            rec = self.app._save_json_ds(self.name, obj, url=url, source="logcat")
            with self.lock:
                self.captured.insert(0, rec)

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "endpoint": self.endpoint,
                "name": self.name,
                "tag": self.tag,
                "lines": self.lines,
                "captured": list(self.captured),
                "error": self.error,
            }


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
class App:
    def __init__(self, spec_path, out_dir=None):
        self.store = SpecStore(spec_path)
        self.out = os.path.abspath(out_dir or os.path.join(os.path.dirname(self.store.path), "reports"))
        os.makedirs(self.out, exist_ok=True)
        self.live = None
        self.api_cap = None
        self._ds_lock = threading.RLock()
        self.spec_error = None
        self.reload()

    # ---- 基础 ----
    def reload(self):
        self.store.load()
        try:
            self.spec = tc.Spec(self.store.path)
            self.spec_error = None
        except Exception as e:  # 规则写坏了也让网站能打开
            self.spec_error = f"{type(e).__name__}: {e}"
            if not hasattr(self, "spec"):
                raise
        self.sources = tc.Sources(self.spec)

    def spec_with(self, eid, raw):
        s = copy.copy(self.spec)
        s.events = dict(self.spec.events)
        if raw is not None:
            s._raw_events = dict(self.spec._raw_events)
            s._raw_events[str(eid)] = raw
            s.events[str(eid)] = s._resolve(raw, {str(eid)})
        return s

    def adb(self, serial=None):
        return tc.adb_base(self.spec, serial)

    def pull_log(self, serial=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                tc.do_pull(self.spec, serial)
            except FileNotFoundError:
                print("✗ 找不到 adb，请确认 Android SDK platform-tools 在 PATH 里")
        return ANSI.sub("", buf.getvalue())

    def reports(self, limit=60):
        out = []
        for p in sorted(glob.glob(os.path.join(self.out, "trackcheck_*.json")), key=os.path.getmtime, reverse=True)[:limit]:
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                out.append({"name": os.path.splitext(os.path.basename(p))[0], "time": d["meta"]["generated_at"],
                            "case": d["meta"].get("case"), "log": d["meta"].get("log"), "summary": d["summary"],
                            "hot": d.get("hot_fields", [])[:5],
                            "last": {r["id"]: r["status"] for r in d.get("results", [])}})
            except Exception:
                continue
        return out

    def sessions(self):
        files = glob.glob(os.path.join(self.out, "*.log")) + glob.glob(os.path.join(os.path.dirname(self.store.path), "*.log"))
        files = [p for p in sorted(set(map(os.path.abspath, files)), key=os.path.getmtime, reverse=True)
                 if os.path.basename(p).lower() != "server.log"]
        return [{"path": p, "name": os.path.basename(p), **file_info(p)} for p in files[:40]]

    def source_status(self):
        out = {}
        for name, cfg in self.spec.sources.items():
            t = cfg.get("type", "json_file")
            info = {"type": t}
            try:
                if t in ("json_file", "sqlite"):
                    info.update(file_info(self.sources._file(cfg["path"])))
                elif t == "merged_json":
                    obj = self.sources.json(name)
                    mi = self.sources.merge_info(name) or {}
                    keys = list(obj.keys())[:8] if isinstance(obj, dict) else [f"[{len(obj)}]"]
                    pr = mi.get("primary_used") or "兜底全量"
                    fb = mi.get("fallback") or ""
                    p_txt = f"主源 {pr}" + (f" + 兜底 {fb}" if fb else "")
                    info.update(exists=True, path=p_txt, keys=keys, merge_info=mi)
                else:
                    obj = self.sources.json(name)
                    keys = list(obj.keys())[:8] if isinstance(obj, dict) else [f"[{len(obj)}]"]
                    info.update(exists=True, path=f"{cfg.get('db')} · SQL", keys=keys)
            except Exception as e:
                info.update(exists=False, error=str(e))
            out[name] = info
        return out

    @staticmethod
    def _norm_api_list(raw_api):
        if not raw_api:
            return []
        items = raw_api if isinstance(raw_api, list) else [raw_api]
        out = []
        for it in items:
            if isinstance(it, str) and it.strip():
                ep = it.strip()
                out.append({"endpoint": ep, "source": infer_api_ds_name(ep), "tag": ""})
            elif isinstance(it, dict) and str(it.get("endpoint") or "").strip():
                ep = str(it["endpoint"]).strip()
                src = re.sub(r"[^\w\-]", "_", str(it.get("source") or "").strip()) or infer_api_ds_name(ep)
                out.append({"endpoint": ep, "source": src, "tag": str(it.get("tag") or "").strip()})
        return out

    def resolve_event_apis(self, eids=None):
        """根据点位清单（含继承自模板的 api 及数据源绑定）汇总需要监听的接口列表"""
        s = self.spec
        eids = [str(x).strip() for x in (eids or []) if str(x).strip()]
        seen, out = OrderedDict(), []

        def add_one(item, via):
            key = (item["endpoint"].lower(), item["source"])
            if key not in seen:
                seen[key] = {**item, "via": [via] if via else []}
                out.append(seen[key])
            elif via and via not in seen[key]["via"]:
                seen[key]["via"].append(via)

        targets = eids if eids else list(s.events.keys())
        used_sources = set()
        for eid in targets:
            es = s.events.get(eid) or {}
            for a in self._norm_api_list(es.get("api")):
                add_one(a, es.get("name") or eid)
            for c in (es.get("context") or {}).values():
                if isinstance(c, dict) and c.get("from"):
                    frms = c["from"] if isinstance(c["from"], list) else [c["from"]]
                    for f in frms:
                        used_sources.add(str(f))
        if not eids:
            for tname, tpl in s.templates.items():
                for a in self._norm_api_list((tpl or {}).get("api")):
                    add_one(a, f"模板 {tname}")
            used_sources |= set(s.sources.keys())
        # 展开 merged_json 依赖，检查显式数据源上绑定的 api_endpoint
        queue = list(used_sources)
        visited = set()
        while queue:
            sn = queue.pop(0)
            if sn in visited:
                continue
            visited.add(sn)
            cfg = s.sources.get(sn)
            if not isinstance(cfg, dict):
                continue
            if cfg.get("api_endpoint"):
                ep = str(cfg["api_endpoint"]).strip()
                add_one({"endpoint": ep, "source": sn, "tag": str(cfg.get("api_tag") or "").strip()}, f"数据源 {sn}")
            if cfg.get("type") == "merged_json":
                for dep in tc.Sources._str_list(cfg.get("primary") or cfg.get("sources")):
                    queue.append(dep)
                if cfg.get("fallback"):
                    queue.append(str(cfg["fallback"]))
        return out

    # ---- 点位 ↔ 数据源 绑定（AI 提示词只带「这个点位真正用得到的」数据源） ----
    def _case_ids(self, case):
        out = []
        for item in (case or {}).get("expect") or []:
            eid = item.get("id") if isinstance(item, dict) else item
            if eid not in (None, ""):
                out.append(str(eid))
        return out

    def event_sources(self, eid):
        """返回 (有序数据源名列表, {数据源: [来由...]})。
        来由：模板/点位 sources 绑定、所在用例 sources 绑定、规则里 context/from/sql/api 实际引用，并展开合并源依赖。"""
        s = self.spec
        es = s.events.get(str(eid)) or {}
        why = OrderedDict()

        def add(name, reason):
            name = str(name or "").strip()
            if not name or name not in s.sources:
                return
            why.setdefault(name, [])
            if reason not in why[name]:
                why[name].append(reason)

        raw = s._raw_events.get(str(eid)) or {}
        own = tc.Sources._str_list(raw.get("sources"))
        for x in tc.Sources._str_list(es.get("sources")):
            add(x, "点位绑定" if x in own else f"模板 {raw.get('extends') or ''} 绑定".replace("  ", " "))
        for cname, c in s.cases.items():
            if str(eid) in self._case_ids(c):
                for x in tc.Sources._str_list((c or {}).get("sources")):
                    add(x, f"用例「{cname}」绑定")
        ctx_names = set((es.get("context") or {}).keys())
        for c in (es.get("context") or {}).values():
            if isinstance(c, dict) and c.get("from"):
                for f in (c["from"] if isinstance(c["from"], list) else [c["from"]]):
                    add(f, "规则 context 引用")
        first_db = next((k for k, v in s.sources.items() if v.get("type") == "sqlite"), None)
        for r in (es.get("fields") or {}).values():
            if isinstance(r, str):
                r = {"from": r}
            if not isinstance(r, dict):
                continue
            frm = str(r.get("from") or "")
            if frm:
                head = frm.split(":", 1)[0] if ":" in frm else re.split(r"[.\[]", frm, 1)[0]
                if head not in ctx_names:
                    add(head, "字段 from 引用")
            if r.get("sql"):
                add(r.get("db") or first_db, "字段 SQL 引用")
        for a in self._norm_api_list(es.get("api")):
            add(a["source"], "关联接口")
        queue, seen = list(why.keys()), set()
        while queue:
            sn = queue.pop(0)
            if sn in seen:
                continue
            seen.add(sn)
            cfg = s.sources.get(sn) or {}
            if cfg.get("type") == "merged_json":
                for dep in tc.Sources._str_list(cfg.get("primary") or cfg.get("sources")) + tc.Sources._str_list(cfg.get("fallback")):
                    add(dep, f"合并源 {sn} 的依赖")
                    queue.append(dep)
            elif cfg.get("type") == "sqlite_json" and cfg.get("db"):
                add(cfg["db"], f"{sn} 所在数据库")
        return list(why.keys()), why

    def _source_prompt_line(self, name, reasons=None, max_paths=120):
        s = self.spec
        cfg = s.sources.get(name) or {}
        t = cfg.get("type", "json_file")
        label = {"sqlite": "SQLite 数据库", "sqlite_json": "DB 缓存 JSON", "merged_json": "合并兜底 JSON（优先直接引用）",
                 "json_file": "接口/本地 JSON"}.get(t, t)
        extra = ""
        if t == "merged_json":
            pr = cfg.get("primary")
            pr_s = " → ".join(pr) if isinstance(pr, list) else str(pr or "")
            extra = f" [主源: {pr_s}, 兜底: {cfg.get('fallback', '')}, 路径: {cfg.get('path', '<根>')}, 匹配键: {cfg.get('key', 'key')}]"
        try:
            if t == "sqlite":
                conn = self.sources.db(name)
                tbs = []
                for (tb,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'android_metadata' ORDER BY name"):
                    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{tb}")')]
                    tbs.append(f"{tb}({', '.join(cols)})")
                desc = "；".join(tbs[:40]) + (" …" if len(tbs) > 40 else "")
            else:
                paths = sorted({tc.path_str(p, True) for p, _ in tc.iter_leaves(self.sources.json(name))})
                desc = "，".join(paths[:max_paths]) + (" …" if len(paths) > max_paths else "")
        except Exception as e:
            desc = f"（读取失败：{e}）"
        why = f" ←{'、'.join(reasons)}" if reasons else ""
        return f"- `{name}`（{label}{extra}{why}）：{desc}"

    # ---- GET ----
    def api_state(self, q):
        s = self.spec
        reps = self.reports(20)
        last = {}
        for r in reversed(reps):
            for k, v in r["last"].items():
                last.setdefault(k, {})
                last[k] = {"status": v, "report": r["name"], "time": r["time"]}
        events = []
        configured = set()
        refs = {}
        for eid, (raw, p) in self.store.items("events").items():
            res = s.events.get(eid, {})
            configured.add(eid)
            for n in rule_dict_refs(res.get("fields")):
                refs.setdefault(n, []).append(eid)
            events.append({"id": eid, "name": res.get("name") or s.catalog.get(eid, ""), "nfields": len(res.get("fields") or {}),
                           "stub": tc.is_stub_event(res),
                           "extends": (raw or {}).get("extends"), "api": res.get("api"),
                           "file": os.path.relpath(p, os.path.dirname(self.store.path)),
                           "last": last.get(eid)})
        dicts, dict_meta = {}, {}
        for k, (v, _) in self.store.items("dicts").items():
            items, meta = tc.split_dict(v)
            dicts[k] = items
            dict_meta[k] = {**meta, "refs": refs.get(k, [])}
        catalog = [{"id": k, "name": v, "configured": k in configured} for k, v in s.catalog.items()]
        for r in reps:
            r.pop("last", None)
        live = None
        if self.live:
            live = {"running": self.live.running, "count": len(self.live.results), "report": self.live.report,
                    "raw": self.live.raw, "case": self.live.case, "only": sorted(self.live.only),
                    "scenario": self.live.scenario, "snap_on": self.live.snap_on, "snap_err": self.live.snap_err,
                    "snaps": len(self.live.snaps), "api_targets": self.live.api_targets,
                    "api_hits": self.live.api_hits[-10:], "api_err": self.live.api_err, "version": self.live.version}
        ask = OrderedDict()   # 需要人工确认 / 按场景比对的字段：{字段: {desc, options, events}}
        for eid, es in s.events.items():
            for fname, r in (es.get("fields") or {}).items():
                if isinstance(r, dict) and r.get("ask"):
                    a = ask.setdefault(str(fname), {"desc": r.get("desc", ""), "options": {}, "events": []})
                    e = r.get("enum")
                    if isinstance(e, dict):
                        a["options"].update({str(k): str(v) for k, v in e.items()})
                    elif isinstance(e, list):
                        a["options"].update({str(k): "" for k in e})
                    elif isinstance(e, str):
                        a["options"].update({str(k): str(v) for k, v in s.dicts.get(e, {}).items()})
                    a["events"].append(eid)
        evd = s.event_dict
        ev_fields = {}
        for eid, es in s.events.items():
            fs = OrderedDict()
            for fname, r in (es.get("fields") or {}).items():
                r = tc.norm_rule(r) if not isinstance(r, dict) else r
                opts, e = OrderedDict(), r.get("enum")
                if isinstance(e, dict):
                    opts.update((str(k), str(v)) for k, v in e.items())
                elif isinstance(e, list):
                    opts.update((str(k), "") for k in e)
                elif isinstance(e, str):
                    opts.update((str(k), str(v)) for k, v in s.dicts.get(e, {}).items())
                kind = next((k for k in ("from", "sql", "expr", "eq", "enum") if k in r), "")
                fs[str(fname)] = {"desc": str(r.get("desc", "") or ""), "options": opts, "ask": bool(r.get("ask")), "kind": kind,
                                  "ignore": bool(r.get("ignore"))}
            ev_fields[eid] = fs
        tpl_refs = {}
        for eid, (raw, _) in self.store.items("events").items():
            ex = (raw or {}).get("extends")
            for t in ([ex] if isinstance(ex, (str, int)) else (ex or [])):
                tpl_refs.setdefault(str(t), []).append(eid)
        return {
            "spec_path": self.store.path, "spec_error": self.spec_error, "out": self.out,
            "files": [os.path.relpath(p, os.path.dirname(self.store.path)) for p in self.store.files],
            "events": events,
            "templates": {k: v for k, (v, _) in self.store.items("templates").items()},
            "dicts": dicts, "dict_meta": dict_meta, "catalog": catalog,
            "sources": {k: v for k, v in s.sources.items()},
            "sources_explicit": {k: v for k, (v, _) in self.store.items("sources").items()},
            "datasource_dir": s.datasource_dir,
            "source_status": self.source_status(),
            "cases": {k: v for k, (v, _) in self.store.items("cases").items()},
            "device": self.store.main.get("device") or {},
            "options": self.store.main.get("options") or {},
            "log": self.store.main.get("log") or {},
            "reports": reps, "sessions": self.sessions(), "live": live, "ask_fields": ask,
            "event_fields": ev_fields, "tpl_refs": tpl_refs, "all_apis": self.resolve_event_apis(),
            "evdict": {"count": len(evd.get("entries", {})),
                       "docs": [x for x in evd.get("docs", []) if self._doc_path(x.get("name"), x.get("path"))],
                       "modules": tc.dict_modules(evd)},
            "server": {"frozen": FROZEN, "pid": os.getpid(), "err_count": SERVER_LOG.err_count, "log_path": SERVER_LOG.path},
        }

    def api_event(self, q):
        eid = str(q.get("id", ""))
        raw, p = self.store.get("events", eid)
        if raw is None:
            raise ApiError(f"没有点位 {eid}")
        return {"id": eid, "raw": raw, "resolved": self.spec.events.get(eid, {}),
                "file": os.path.relpath(p, os.path.dirname(self.store.path))}

    def _del_report_files(self, name):
        stem = os.path.basename(str(name or ""))
        if not stem or not re.fullmatch(r"trackcheck_[\w\-]+", stem):
            raise ApiError("无效的报告文件名")
        removed = 0
        for ext in (".json", ".html"):
            p = os.path.join(self.out, stem + ext)
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
        if self.live and self.live.report == stem:
            self.live.report = None
        return removed

    def api_report(self, q):
        name = os.path.basename(q.get("name", ""))
        p = os.path.join(self.out, name + ".json")
        if not os.path.exists(p):
            raise ApiError("报告不存在")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def api_report_delete(self, b):
        if not self._del_report_files(b.get("name")):
            raise ApiError("报告不存在")
        return {"ok": True}

    def api_reports_clear(self, b):
        n = 0
        for p in glob.glob(os.path.join(self.out, "trackcheck_*.json")) + glob.glob(os.path.join(self.out, "trackcheck_*.html")):
            try:
                os.remove(p)
                if p.endswith(".json"):
                    n += 1
            except OSError:
                pass
        if self.live:
            self.live.report = None
        return {"ok": True, "deleted": n}

    def api_report_review(self, b):
        name = os.path.basename(str(b.get("name", "")).strip())
        if not name:
            raise ApiError("缺少报告名称")
        jp = os.path.join(self.out, name + ".json")
        if not os.path.exists(jp):
            raise ApiError("报告不存在")
        status = str(b.get("status", "")).strip().upper()
        if status not in ("PASS", "FAIL", "ASK"):
            raise ApiError("状态无效，仅支持 PASS / FAIL / ASK")
        seq = int(b.get("seq", 0))
        field = str(b.get("field") or "").strip()
        note = str(b.get("note") or "").strip()

        old_stat = os.stat(jp)
        with open(jp, encoding="utf-8") as f:
            rep = json.load(f)

        target = next((r for r in rep.get("results", []) if int(r.get("seq", -1)) == seq), None)
        if not target:
            raise ApiError(f"找不到事件 #{seq}")

        st_label = {"PASS": "通过", "FAIL": "有问题", "ASK": "待确认"}[status]
        if field.startswith("__err__:"):
            try:
                ei = int(field.split(":", 1)[1])
                er = target.get("errors", [])[ei]
            except (ValueError, IndexError):
                raise ApiError("找不到该条错误信息")
            er.setdefault("orig_status", er.get("status"))
            er["status"] = status
            er["manual"] = True
            er["note"] = note or ("" if status == er.get("orig_status") else f"人工判定为「{st_label}」")
            if not note and status == er.get("orig_status"):
                er.pop("manual", None)
            target.pop("manual_status", None)
            target.pop("manual_note", None)
            target["status"] = tc.worst([x["status"] for x in target.get("fields", [])] + [e["status"] for e in target.get("errors", [])])
        elif field:
            fobj = next((x for x in target.get("fields", []) if x.get("field") == field), None)
            if not fobj:
                raise ApiError(f"找不到字段 {field}")
            fobj.setdefault("orig_status", fobj.get("status"))
            fobj.setdefault("orig_reason", fobj.get("reason", ""))
            fobj["status"] = status
            fobj["manual"] = True
            fobj["note"] = note
            if note:
                fobj["reason"] = f"人工判定（{st_label}）：{note}"
            elif status == fobj.get("orig_status"):
                fobj["reason"] = fobj.get("orig_reason", "")
                fobj.pop("manual", None)
            else:
                orig = fobj.get("orig_reason", "")
                fobj["reason"] = f"人工判定为「{st_label}」" + (f"（原：{orig}）" if orig else "")
            # 当所有子字段均已不再存在失败/警告/待确认时，同步将关联的整条断言错误标记为通过，确保 header 自动变为通过
            fields_list = target.get("fields", [])
            if status == "PASS" and fields_list and not any(x.get("status") in ("FAIL", "WARN", "ASK") for x in fields_list):
                for er in target.get("errors", []):
                    if er.get("status") in ("FAIL", "WARN", "ASK"):
                        er.setdefault("orig_status", er.get("status"))
                        er["status"] = "PASS"
                        er["manual"] = True
                        er["note"] = "全部子项已人工判定通过"
            target.pop("manual_status", None)
            target.pop("manual_note", None)
            target["status"] = tc.worst([x["status"] for x in fields_list] + [e["status"] for e in target.get("errors", [])])
        else:
            target.setdefault("orig_status", target.get("status"))
            target["manual_status"] = status
            target["manual_note"] = note
            if status == "PASS":
                for x in target.get("fields", []):
                    if x.get("status") in ("FAIL", "WARN", "ASK"):
                        x.setdefault("orig_status", x.get("status"))
                        x.setdefault("orig_reason", x.get("reason", ""))
                        x["status"] = "PASS"
                        x["manual"] = True
                        x["note"] = note
                        x["reason"] = f"人工整条判定通过：{note}" if note else "人工整条判定为「通过」"
                for er in target.get("errors", []):
                    if er.get("status") in ("FAIL", "WARN", "ASK"):
                        er.setdefault("orig_status", er.get("status"))
                        er["status"] = "PASS"
                        er["manual"] = True
                        er["note"] = f"人工整条判定通过：{note}" if note else "人工整条判定为「通过」"
            target["status"] = status

        cnt = Counter(r["status"] for r in rep.get("results", []))
        hot = defaultdict(lambda: {"count": 0, "reason": "", "name": ""})
        for r in rep.get("results", []):
            for x in r.get("fields", []):
                if x.get("status") in ("FAIL", "WARN"):
                    h = hot[(r["id"], x["field"])]
                    h["count"] += 1
                    h["reason"] = h["reason"] or x.get("reason", "")
                    h["name"] = r.get("name", "")
        rep["hot_fields"] = sorted(({"id": k[0], "field": k[1], **v} for k, v in hot.items()), key=lambda x: -x["count"])
        cov_fail = sum(1 for c in rep.get("coverage", []) if c.get("status") == "FAIL")
        rep["summary"] = {
            "total": len(rep.get("results", [])),
            "pass": cnt.get("PASS", 0),
            "warn": cnt.get("WARN", 0),
            "fail": cnt.get("FAIL", 0),
            "unknown": cnt.get("UNKNOWN", 0),
            "ask": cnt.get("ASK", 0),
            "coverage_fail": cov_fail,
            "ok": cnt.get("FAIL", 0) == 0 and cov_fail == 0,
        }

        hp, jp_out = tc.write_report(rep, self.out, name)
        try:
            os.utime(jp_out, (old_stat.st_atime, old_stat.st_mtime))
            os.utime(hp, (old_stat.st_atime, old_stat.st_mtime))
        except OSError:
            pass
        rep["name"] = name
        return rep

    def api_source_json(self, q):
        try:
            name = q["name"]
            return {"data": self.sources.json(name), "merge_info": self.sources.merge_info(name)}
        except tc.SourceError as e:
            raise ApiError(str(e))

    def api_db_schema(self, q):
        try:
            conn = self.sources.db(q.get("db") or None)
        except tc.SourceError as e:
            raise ApiError(str(e))
        out = {}
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            out[t] = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        return {"tables": out}

    def api_devices(self, q):
        try:
            p = subprocess.run([tc.find_adb(), "devices", "-l"], capture_output=True, text=True, timeout=8, errors="replace", **tc.popen_kw())
        except FileNotFoundError:
            return {"devices": [], "error": "找不到 adb：装 Android Studio，或把 platform-tools 文件夹放到埋点小岛同目录"}
        except subprocess.TimeoutExpired:
            return {"devices": [], "error": "adb 无响应"}
        devs = []
        for line in p.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                model = next((x.split(":", 1)[1] for x in parts if x.startswith("model:")), "")
                devs.append({"serial": parts[0], "state": parts[1], "model": model})
        return {"devices": devs}

    def api_live_poll(self, q):
        if not self.live:
            return {"running": False, "results": [], "count": 0, "api_targets": [], "api_hits": [], "version": 0}
        since = int(q.get("since", 0))
        with self.live.lock:
            res = self.live.results[since:]
            n = len(self.live.results)
            hits = list(self.live.api_hits)
            ver = self.live.version
        return {"running": self.live.running, "results": res, "count": n, "since": since,
                "report": self.live.report, "raw": self.live.raw, "err_count": SERVER_LOG.err_count,
                "api_targets": self.live.api_targets, "api_hits": hits, "api_err": self.live.api_err, "version": ver}

    @staticmethod
    def _clean_api(raw):
        api = raw.get("api")
        if isinstance(api, dict):
            ep = str(api.get("endpoint") or "").strip()
            if not ep:
                raw.pop("api", None)
            else:
                cleaned = {"endpoint": ep}
                src = re.sub(r"[^\w\-]", "_", str(api.get("source") or "").strip())
                if src:
                    cleaned["source"] = src
                tag = str(api.get("tag") or "").strip()
                if tag:
                    cleaned["tag"] = tag
                raw["api"] = cleaned
        return raw

    # ---- POST：规则 ----
    def api_event_save(self, b):
        eid = str(b.get("id", "")).strip()
        if not eid:
            raise ApiError("点位ID不能为空")
        raw = self._clean_api(dict(b.get("raw") or {}))
        raw = {k: v for k, v in raw.items() if v not in (None, "", [], {}) or k in ("fields",)}
        self.store.put("events", eid, raw, b.get("old_id"))
        self._autotag({eid: raw})
        self.reload()
        if self.spec_error:
            raise ApiError("已保存，但规则文件解析出错：" + self.spec_error)
        return {"ok": True, "id": eid}

    def _autotag(self, raws):
        """点位规则引用了某个字典 -> 自动把这个点位加到字典的「关联点位」标签里"""
        touched = set()
        for eid, raw in raws.items():
            fields = dict(raw.get("fields") or {})
            if raw.get("extends"):
                try:
                    fields = self.spec_with(eid, raw).events[str(eid)].get("fields") or fields
                except Exception:
                    pass
            for n in rule_dict_refs(fields):
                v, p = self.store.get("dicts", n)
                if v is None:
                    continue
                items, meta = tc.split_dict(v)
                if str(eid) not in meta["events"]:
                    touched.add(self.store.put("dicts", n, dict_value(items, meta["events"] + [str(eid)], meta["desc"]), save=False))
        for p in touched:
            self.store.save(p)

    def api_dict_tag(self, b):
        name, eid, op = b["name"], str(b["event"]).strip(), b.get("op", "add")
        v, _ = self.store.get("dicts", name)
        if v is None:
            raise ApiError(f"没有字典 {name}")
        items, meta = tc.split_dict(v)
        evs = [x for x in meta["events"] if x != eid]
        if op == "add":
            evs.append(eid)
        self.store.put("dicts", name, dict_value(items, evs, meta["desc"]))
        self.reload()
        return {"ok": True}

    def api_event_delete(self, b):
        self.store.delete("events", str(b["id"]))
        self.reload()
        return {"ok": True}

    def _put(self, section, b):
        name = str(b.get("name", "")).strip()
        if not name:
            raise ApiError("名称不能为空")
        self.store.put(section, name, b.get("value") or {}, b.get("old_name"))
        self.reload()
        return {"ok": True}

    def api_dict_save(self, b):
        name = str(b.get("name", "")).strip()
        if not name:
            raise ApiError("名称不能为空")
        self.store.put("dicts", name, dict_value(b.get("items") or {}, b.get("events") or [], b.get("desc") or ""), b.get("old_name"))
        self.reload()
        return {"ok": True}

    def api_source_save(self, b):
        val = dict(b.get("value") or {})
        if val.get("type") == "merged_json":
            primaries = tc.Sources._str_list(val.get("primary"))
            fb = str(val.get("fallback") or "").strip()
            if not primaries and not fb:
                raise ApiError("合并兜底数据源至少需要填一个主数据源或兜底数据源")
            cleaned = {"type": "merged_json"}
            if primaries:
                cleaned["primary"] = primaries[0] if len(primaries) == 1 else primaries
            if fb:
                cleaned["fallback"] = fb
            for k in ("path", "fallback_path", "key", "empty_field"):
                v = str(val.get(k) or "").strip()
                if v:
                    cleaned[k] = v
            fb_keys = tc.Sources._str_list(val.get("fallback_keys"))
            if fb_keys:
                cleaned["fallback_keys"] = fb_keys
            val = cleaned
        return self._put("sources", {**b, "value": val})

    def api_case_save(self, b):
        return self._put("cases", b)

    def api_template_save(self, b):
        val = self._clean_api(dict(b.get("value") or {}))
        return self._put("templates", {**b, "value": val})

    def api_item_delete(self, b):
        if b.get("section") not in ("dicts", "sources", "cases", "templates"):
            raise ApiError("不支持")
        self.store.delete(b["section"], b["name"])
        self.reload()
        return {"ok": True}

    def api_settings_save(self, b):
        for k in ("options", "log", "device"):
            if k in b:
                self.store.put_top(k, b[k])
        self.reload()
        return {"ok": True}

    # ---- POST：调试 ----
    def api_parse(self, b):
        text = b.get("text") or ""
        p = tc.LogParser(self.spec.pattern)
        out = []
        for i, line in enumerate(text.splitlines(), 1):
            ev = p.feed(line, i)
            if ev:
                out.append(ev)
        if not out and b.get("id") and text.strip():
            cls, fields = tc.parse_data(text.strip())
            out.append(mk_event(b["id"], fields, cls, text.strip()))
        return {"events": [{"id": e.id, "cls": e.cls, "fields": e.fields, "raw": e.raw, "known": e.id in self.spec.events}
                           for e in out]}

    def api_try(self, b):
        eid = str(b["id"])
        s = self.spec_with(eid, b.get("raw"))
        ev = mk_event(eid, b.get("fields"), b.get("cls"), b.get("line", ""))
        return tc.Checker(s, self.sources).check(ev)

    def api_sql(self, b):
        try:
            conn = self.sources.db(b.get("db") or None)
            fields = b.get("fields") or {}
            sql = tc.subst(b.get("sql", ""), fields)
            cur = conn.execute(sql, {k: tc.norm(v) for k, v in fields.items()})
            cols = [d[0] for d in cur.description or []]
            rows = [[tc.disp(x, 200) for x in r] for r in cur.fetchmany(50)]
        except (tc.SourceError, sqlite3.Error) as e:
            raise ApiError(str(e))
        return {"columns": cols, "rows": rows}

    # ---- 数据库浏览器（只读，像 sqlite3 工具一样看表 / 查表，帮用户写 sql 规则） ----
    _SQL_WRITE = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|vacuum|reindex)\b", re.I)

    def _db_names(self):
        return [k for k, v in self.spec.sources.items() if v.get("type") == "sqlite"]

    def _db_ro(self, name):
        dbs = self._db_names()
        if not dbs:
            raise ApiError("还没有数据库：去「数据源」页从手机拉一个 .db，或把 .db 放进数据源目录")
        name = name if name in dbs else dbs[0]
        try:
            path = self.sources._file(str(self.spec.sources[name].get("path") or ""))
        except tc.SourceError as e:
            raise ApiError(str(e))
        uri = "file:" + urllib_quote(os.path.abspath(path).replace("\\", "/")) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        return name, path, conn

    def api_db_tables(self, q):
        name, path, conn = self._db_ro(q.get("db"))
        try:
            tables = []
            for (tb, kind) in conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
                                           "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"):
                cols = [{"name": r[1], "type": r[2] or "", "pk": bool(r[5])} for r in conn.execute(f'PRAGMA table_info("{tb}")')]
                try:
                    n = conn.execute(f'SELECT count(*) FROM "{tb}"').fetchone()[0]
                except sqlite3.Error:
                    n = None
                tables.append({"name": tb, "kind": kind, "columns": cols, "rows": n})
        finally:
            conn.close()
        return {"db": name, "dbs": self._db_names(), "path": path, "tables": tables,
                "size": os.path.getsize(path) if os.path.exists(path) else 0}

    def api_db_query(self, b):
        sql = str(b.get("sql") or "").strip().rstrip(";").strip()
        if not sql:
            raise ApiError("先输入一条 SQL")
        body = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip()
        first = (body.split(None, 1) or [""])[0].lower()
        if first not in ("select", "with", "pragma", "explain", "values") or self._SQL_WRITE.search(body) \
                or (first == "pragma" and "=" in body):
            raise ApiError("数据库浏览器是只读的：只支持 SELECT / WITH / PRAGMA / EXPLAIN 查询")
        limit = max(1, min(int(b.get("limit") or 200), 2000))
        offset = max(0, int(b.get("offset") or 0))
        fields = b.get("fields") or {}
        name, _, conn = self._db_ro(b.get("db"))
        t0 = time.time()
        try:
            cur = conn.execute(tc.subst(sql, fields) if fields else sql,
                               {k: tc.norm(v) for k, v in fields.items()} if fields else {})
            cols = [d[0] for d in cur.description or []]
            if offset:
                cur.fetchmany(offset)
            raw = cur.fetchmany(limit + 1)
        except sqlite3.Error as e:
            raise ApiError(f"SQL 执行出错：{e}")
        finally:
            ms = int((time.time() - t0) * 1000)
        conn.close()
        more = len(raw) > limit
        rows = [[None if x is None else tc.disp(x, 400) for x in r] for r in raw[:limit]]
        return {"db": name, "columns": cols, "rows": rows, "more": more, "offset": offset, "limit": limit, "ms": ms}

    def api_suggest(self, b):
        """在 JSON 树里点了一个叶子 -> 生成可复用的 from 表达式（必要时自动补上下文定位）"""
        eid, src, path = str(b["id"]), b["source"], b["path"]
        fields = b.get("fields") or {}
        raw = b.get("raw")
        if raw is None:
            raw = self.store.get("events", eid)[0] or {}
        s = self.spec_with(eid, raw)
        es = s.events[eid]
        ev = mk_event(eid, fields)
        toks = tuple(tc.tokenize(path))
        data = self.sources.json(src)
        value = (tc.resolve(data, list(toks)) or [(None, "")])[0][0]
        ctx, meta, err = tc.Checker(s, self.sources).build_context(es, ev)
        best = None
        for name, m in meta.items():
            pref, sep, rest = m["path"].partition(":")
            if not sep or pref != src:
                continue
            mt = tuple(tc.tokenize(rest))
            if len(mt) < len(toks) and toks[:len(mt)] == mt and (best is None or len(mt) > len(best[1])):
                best = (name, mt)
        if best:
            return {"from": f"{best[0]}.{tc.path_str(toks[len(best[1]):])}", "contexts": {}, "value": value,
                    "note": f"相对已有的数据定位「{best[0]}」，以后这个点位每次都会自动定位"}
        ji = tc.JsonIndex(src, data)
        _, ctxs, _ = tc.infer_occurrence(ev, ji)
        cont = None
        for c in ctxs:
            if len(c["node"]) < len(toks) and toks[:len(c["node"])] == c["node"] and (cont is None or len(c["node"]) > len(cont["node"])):
                cont = c
        if cont:
            chain, c = [], cont
            while c:
                chain.insert(0, c)
                c = c["parent"]
            existing = es.get("context") or {}
            rename, defs = {}, OrderedDict()
            for c in chain:
                frm = rename.get(c["parent"]["name"], c["parent"]["name"]) if c["parent"] else src
                rel = c["node"][len(c["parent"]["node"]):] if c["parent"] else c["node"]
                d = {"from": frm, "path": tc.path_str(rel, True), "where": {str(c["key"]): "{" + c["field"] + "}"}}
                name, i = c["name"], 2
                while name in existing and existing[name] != d:
                    name, i = f"{c['name']}{i}", i + 1
                rename[c["name"]] = name
                if existing.get(name) != d:
                    defs[name] = d
            nm = rename[cont["name"]]
            return {"from": f"{nm}.{tc.path_str(toks[len(cont['node']):])}", "contexts": defs, "value": value,
                    "note": "已根据上报的 " + "、".join(c["field"] for c in chain) + " 自动生成数据定位，以后自动复用"}
        return {"from": f"{src}:{path}", "contexts": {}, "value": value,
                "note": "上报值里的 ID 在这份接口数据中匹配不到，只能用固定下标的绝对路径，换一批数据可能失效，请确认"}

    def api_suggest_sql(self, b):
        ev = mk_event(b["id"], b.get("fields"))
        _, conns, _ = tc.load_infer_sources(self.spec)
        if not conns:
            raise ApiError("还没有配置 sqlite 数据源")
        s = tc.infer_db(ev, conns, [b["field"]]).get(b["field"])
        if not s:
            raise ApiError("没在 DB 里找到同时包含 ID 和这个值的行，手动写一条吧")
        return s

    def _read_log(self, b):
        if b.get("content"):
            p = os.path.join(self.out, f"upload_{time.strftime('%Y%m%d_%H%M%S')}.log")
            with open(p, "w", encoding="utf-8") as f:
                f.write(b["content"])
        else:
            p = b.get("path") or ""
            if not os.path.exists(p):
                raise ApiError(f"日志文件不存在: {p}")
        evs = tc.read_log(p, self.spec.pattern)
        if not evs:
            raise ApiError(f"日志里没有匹配到埋点行，检查日志格式（当前正则 {self.spec.pattern}）")
        return p, evs

    def api_check_log(self, b):
        p, evs = self._read_log(b)
        res = tc.check_all(self.spec, evs, verbose=False, scenario=tc.case_values(self.spec, b.get("case") or None))
        rep = tc.build_report(self.spec, res, b.get("case") or None, {"log": p})
        stem = "trackcheck_" + time.strftime("%Y%m%d_%H%M%S")
        tc.write_report(rep, self.out, stem)
        return {"report": stem, "summary": rep["summary"]}

    def api_infer(self, b):
        p, evs = self._read_log(b)
        jsons, conns, warns = tc.load_infer_sources(self.spec)
        drafts, dicts = tc.infer_events(evs, jsons, conns)
        first = {}
        for e in evs:
            first.setdefault(e.id, e)
        for d in drafts:
            d["exists"] = d["id"] in self.spec.events
            d["name"] = self.spec.events.get(d["id"], {}).get("name") or d["cls"] or d["id"]
            d["sample_fields"] = dict(first[d["id"]].fields)
            for e in evs:
                if e.raw == d["sample"]:
                    d["sample_fields"] = dict(e.fields)
                    break
        return {"drafts": drafts, "dicts": dicts, "warns": warns, "log": p}

    def api_infer_accept(self, b):
        eid = str(b["id"])
        old = self.store.get("events", eid)[0] or {}
        raw = dict(old) if b.get("merge") else {k: v for k, v in old.items() if k in ("extends", "dedupe", "dedupe_window", "asserts")}
        raw["name"] = b.get("name") or old.get("name") or eid
        ctx = dict(raw.get("context") or {}) if b.get("merge") else {}
        ctx.update(b.get("context") or {})
        if ctx:
            raw["context"] = ctx
        fields = dict(raw.get("fields") or {}) if b.get("merge") else {}
        for f in b.get("fields") or []:
            if not b.get("merge") or f["field"] not in fields:
                fields[f["field"]] = f.get("rule") or {}
        raw["fields"] = fields
        for name, items in (b.get("dicts") or {}).items():
            cur, _ = self.store.get("dicts", name)
            ci, meta = tc.split_dict(cur)
            merged = dict(items)
            merged.update(ci)
            self.store.put("dicts", name, dict_value(merged, meta["events"], meta["desc"]))
        self.store.put("events", eid, raw)
        self._autotag({eid: raw})
        self.reload()
        return {"ok": True}

    # ---- 导出 / 导入 / 点位清单 ----
    def api_export(self, q):
        ids = [x.strip() for x in (q.get("ids") or "").split(",") if x.strip()]
        allx = not ids
        evs = self.store.items("events")
        sel = OrderedDict((k, v) for k, (v, _) in evs.items() if allx or k in ids)
        tpls = self.store.items("templates")
        need_t = OrderedDict()

        def walk(raw):
            for p in ([raw.get("extends")] if isinstance(raw.get("extends"), (str, int)) else (raw.get("extends") or [])):
                p = str(p)
                if p in tpls and p not in need_t:
                    need_t[p] = tpls[p][0]
                    walk(tpls[p][0] or {})
                elif p in evs and p not in sel:
                    sel[p] = evs[p][0]
                    walk(evs[p][0] or {})
        for raw in list(sel.values()):
            walk(raw or {})
        refs = set()
        for eid in sel:
            refs |= rule_dict_refs(self.spec.events.get(eid, {}).get("fields"))
        dicts = OrderedDict()
        for k, (v, _) in self.store.items("dicts").items():
            items, meta = tc.split_dict(v)
            if allx or k in refs or set(meta["events"]) & set(sel):
                dicts[k] = v
        out = OrderedDict()
        out["meta"] = {"tool": "trackcheck / 埋点小岛", "author": q.get("author") or "", "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "events": len(sel), "note": q.get("note") or ""}
        if q.get("sources") == "1":
            out["sources"] = {k: v for k, (v, _) in self.store.items("sources").items()}
        out["dicts"] = dicts
        out["templates"] = need_t if not allx else {k: v for k, (v, _) in tpls.items()}
        out["events"] = sel
        if allx:
            out["cases"] = {k: v for k, (v, _) in self.store.items("cases").items()}
            if self.spec.catalog:
                out["catalog"] = dict(self.spec.catalog)
        if q.get("samples") == "1":
            samples, skipped = self._collect_samples(out, allx)
            if samples:
                out["datasource_samples"] = samples
            out["meta"]["samples"] = len(samples)
            if skipped:
                out["meta"]["samples_skipped"] = skipped
        if q.get("evdict") == "1":
            d = tc.load_event_dict(self.spec.event_dict_path)
            if d.get("entries"):
                out["event_dict"] = {"entries": d["entries"], "columns": d.get("columns", []),
                                     "docs": [{k: x.get(k) for k in ("name", "count", "imported_at")} for x in d.get("docs", [])]}
                out["meta"]["event_dict"] = len(d["entries"])
                files = OrderedDict()
                for x in d.get("docs", []):
                    p = self._doc_path(x.get("name"), x.get("path"))
                    if p and os.path.getsize(p) <= 2 * 1024 * 1024:
                        try:
                            with open(p, encoding="utf-8-sig", errors="replace") as f:
                                files[os.path.basename(p)] = f.read()
                        except OSError:
                            pass
                if files:
                    out["doc_files"] = files
        text = "# 埋点小岛 规则分享包 —— 在网站「导入 / 分享」页导入即可\n" + yaml.safe_dump(
            plain(out), allow_unicode=True, sort_keys=False, default_flow_style=None, width=160)
        fn = f"trackcheck_share_{time.strftime('%Y%m%d_%H%M%S')}.yaml"
        return FileResponse(text.encode("utf-8"), fn, "application/x-yaml; charset=utf-8")

    @staticmethod
    def _parse_upload(content, filename=""):
        obj = None
        s = (content or "").lstrip("\ufeff").strip()
        if not s:
            raise ApiError("文件是空的")
        if filename.lower().endswith(".json") or s[:1] in "[{":
            try:
                obj = json.loads(s)
            except Exception:
                obj = None
        if obj is None:
            try:
                obj = yaml.safe_load(s)
            except Exception as e:
                raise ApiError(f"无法解析文件（既不是 JSON 也不是 YAML）：{e}")
        spec_keys = {"events", "dicts", "templates", "cases", "sources", "meta", "catalog", "options", "log", "device"}
        if isinstance(obj, list) or (isinstance(obj, dict) and not (set(map(str, obj)) & spec_keys)):
            cat = tc.parse_catalog(obj)
            if not cat:
                raise ApiError("没认出点位清单格式：需要类似 [{\"eventId\": \"888...\", \"eventName\": \"曝光\"}] 或 {\"888...\": \"曝光\"}")
            return "catalog", cat
        if not isinstance(obj, dict):
            raise ApiError("文件内容不是规则包")
        return "bundle", obj

    def api_import_preview(self, b):
        kind, obj = self._parse_upload(b.get("content"), b.get("filename", ""))
        if kind == "catalog":
            cur = self.spec.catalog
            items = [{"id": k, "name": v, "status": "new" if k not in cur else ("same" if cur[k] == v else "diff"),
                      "old": cur.get(k, ""), "configured": k in self.spec.events} for k, v in obj.items()]
            return {"kind": "catalog", "items": items, "total": len(items)}
        items = []
        names = {"events": "点位规则", "dicts": "字典", "templates": "模板", "cases": "用例", "sources": "数据源"}
        for sec in ("events", "dicts", "templates", "cases", "sources"):
            for k, v in (obj.get(sec) or {}).items():
                cur, _ = self.store.get(sec, k)
                if sec == "dicts":
                    same = cur is not None and tc.split_dict(cur)[0] == tc.split_dict(v)[0]
                else:
                    same = cur is not None and plain(cur) == plain(v)
                st = "new" if cur is None else ("same" if same else "diff")
                label = (v or {}).get("name", "") if sec == "events" and isinstance(v, dict) else ""
                if sec == "events" and not label:
                    label = self.spec.catalog.get(str(k), "")
                if sec == "dicts":
                    label = f"{len(tc.split_dict(v)[0])} 项"
                if sec == "events" and isinstance(v, dict):
                    label += f"（{len(v.get('fields') or {})} 个字段）"
                items.append({"section": sec, "section_name": names[sec], "key": str(k), "label": label, "status": st})
        cat = tc.parse_catalog(obj.get("catalog") or {})
        # 数据源样本（同事导出时附带的接口 JSON / 小数据库）
        for fn, smp in (obj.get("datasource_samples") or {}).items():
            fn = os.path.basename(str(fn))
            if not isinstance(smp, dict) or not fn:
                continue
            st, label = self._sample_status(fn, smp)
            items.append({"section": "samples", "section_name": "数据源样本", "key": fn, "label": label, "status": st})
        # 埋点字典 + 埋点文档
        ed = obj.get("event_dict") or {}
        if isinstance(ed, dict) and ed.get("entries"):
            cur = tc.load_event_dict(self.spec.event_dict_path).get("entries", {})
            new_n = len([k for k in ed["entries"] if str(k) not in cur])
            items.append({"section": "evdict", "section_name": "埋点字典", "key": "埋点字典",
                          "label": f"{len(ed['entries'])} 个点位，你这边还没有的 {new_n} 个（已有的保留你的）",
                          "status": "new" if not cur else ("diff" if new_n else "same")})
        for fn, text in (obj.get("doc_files") or {}).items():
            fn = os.path.basename(str(fn))
            if not fn:
                continue
            p = os.path.join(self._docs_dir(), fn)
            st = "new"
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8-sig", errors="replace") as f:
                        st = "same" if f.read() == text else "diff"
                except OSError:
                    st = "diff"
            items.append({"section": "docs", "section_name": "埋点文档", "key": fn, "label": fmt_kb(len(str(text).encode("utf-8"))), "status": st})
        # 规则里用到、但我这边和规则包里都没有的数据源
        have = set(self.spec.sources) | set(map(str, (obj.get("sources") or {}).keys()))
        have |= {re.sub(r"[^\w]", "_", os.path.splitext(str(k))[0]) for k in (obj.get("datasource_samples") or {})}
        missing = sorted(self._used_source_names(obj) - have)
        meta = obj.get("meta") or {}
        return {"kind": "bundle", "meta": meta, "items": items, "missing_sources": missing,
                "samples_skipped": meta.get("samples_skipped") or [],
                "catalog_new": len([k for k in cat if k not in self.spec.catalog]), "catalog_total": len(cat)}

    @staticmethod
    def _used_source_names(obj):
        """规则包里 events / templates 引用到的数据源名（排除 context 内部互相引用的名字）"""
        used, ctx_names = set(), set()
        for sec in ("events", "templates"):
            for v in (obj.get(sec) or {}).values():
                v = v or {}
                ctx = v.get("context") or {}
                ctx_names |= set(map(str, ctx.keys()))
                for c in ctx.values():
                    if isinstance(c, dict) and c.get("from"):
                        frms = c["from"] if isinstance(c["from"], list) else [c["from"]]
                        for f in frms:
                            used.add(str(f))
                for a in App._norm_api_list(v.get("api")):
                    used.add(a["source"])
                for r in (v.get("fields") or {}).values():
                    if isinstance(r, dict):
                        if r.get("db"):
                            used.add(str(r["db"]))
                        m = re.match(r"^([A-Za-z_]\w*):", str(r.get("from") or ""))
                        if m:
                            used.add(m.group(1))
        return used - ctx_names

    # ---- 数据源样本：导出附带 / 导入落盘 ----
    @staticmethod
    def _sqlite_bytes(p):
        """用 SQLite backup 把 db（含 -wal 里还没合并的数据）整理成一份完整文件"""
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _collect_samples(self, bundle, allx):
        auto = tc.scan_datasources(self._ds_dir())
        if allx:
            names = list(auto)
        else:
            used = set(self._used_source_names(bundle))
            # 递归展开特殊数据源依赖（sqlite_json -> db，merged_json -> primary / fallback）
            queue = list(used)
            seen = set()
            while queue:
                k = queue.pop(0)
                if k in seen:
                    continue
                seen.add(k)
                v = (self.spec.sources or {}).get(k)
                if not isinstance(v, dict):
                    continue
                if v.get("db"):
                    used.add(str(v["db"]))
                    queue.append(str(v["db"]))
                if v.get("type") == "merged_json":
                    for dep in tc.Sources._str_list(v.get("primary") or v.get("sources")):
                        used.add(dep)
                        queue.append(dep)
                    if v.get("fallback"):
                        fb = str(v["fallback"])
                        used.add(fb)
                        queue.append(fb)
            names = [n for n in auto if n in used]
        samples, skipped = OrderedDict(), []
        for n in names:
            cfg = auto[n]
            p = cfg["path"]
            fn = os.path.basename(p)
            if cfg["type"] == "json_file":
                try:
                    with open(p, encoding="utf-8-sig") as f:
                        samples[fn] = {"type": "json", "data": json.load(f)}
                except Exception:
                    skipped.append(f"{fn}（JSON 解析失败）")
            elif cfg["type"] == "sqlite":
                try:
                    raw = self._sqlite_bytes(p)
                except Exception as e:
                    skipped.append(f"{fn}（读取失败：{e}）")
                    continue
                if len(raw) > SAMPLE_DB_LIMIT:
                    skipped.append(f"{fn}（{len(raw) // 1024} KB，超过 3 MB 未附带）")
                    continue
                samples[fn] = {"type": "sqlite", "size": len(raw),
                               "gz_b64": base64.b64encode(gzip.compress(raw)).decode("ascii")}
        return samples, skipped

    @staticmethod
    def _sample_bytes(smp):
        if smp.get("type") == "sqlite":
            return gzip.decompress(base64.b64decode(smp.get("gz_b64") or ""))
        return json.dumps(smp.get("data"), ensure_ascii=False, sort_keys=True).encode("utf-8")

    def _sample_status(self, fn, smp):
        p = os.path.join(self._ds_dir(), fn)
        kind = smp.get("type")
        try:
            new = self._sample_bytes(smp)
        except Exception:
            return "diff", "样本已损坏"
        label = ("数据库 · " if kind == "sqlite" else "接口 JSON · ") + fmt_kb(len(new))
        if not os.path.isfile(p):
            return "new", label
        try:
            if kind == "sqlite":
                cur = self._sqlite_bytes(p)
            else:
                with open(p, encoding="utf-8-sig") as f:
                    cur = json.dumps(json.load(f), ensure_ascii=False, sort_keys=True).encode("utf-8")
        except Exception:
            return "diff", label
        return ("same" if cur == new else "diff"), label

    def _apply_sample(self, fn, smp, author=""):
        src = "同事样本" + (f"（{author}）" if author else "")
        if smp.get("type") == "sqlite":
            raw = self._sample_bytes(smp)
            if not raw.startswith(b"SQLite format 3"):
                raise ApiError(f"{fn} 不是有效的 SQLite 数据库")
            with self._ds_lock:
                self._backup_ds(fn)
                p = os.path.join(self._ds_dir(), fn)
                for suffix in ("-wal", "-shm", "-journal"):
                    if os.path.exists(p + suffix):
                        os.remove(p + suffix)
                with open(p, "wb") as f:
                    f.write(raw)
        else:
            self._save_json_ds(os.path.splitext(fn)[0], smp.get("data"), source=src)

    def _write_catalog(self, cat):
        rel = self.store.main.get("catalog_file") or "catalog.json"
        if not self.store.main.get("catalog_file"):
            self.store.put_top("catalog_file", rel)
        p = self.spec.abspath(rel) if not os.path.isabs(rel) else rel
        with open(p, "w", encoding="utf-8") as f:
            json.dump([{"eventId": k, "eventName": v} for k, v in cat.items()], f, ensure_ascii=False, indent=2)
        return p

    def _file_catalog(self):
        rel = self.store.main.get("catalog_file")
        p = self.spec.abspath(rel) if rel else None
        if p and os.path.exists(p):
            with open(p, encoding="utf-8-sig") as f:
                return tc.parse_catalog(json.load(f))
        return OrderedDict()

    def _merge_catalog(self, incoming, mode="merge", overwrite_names=True):
        cat = OrderedDict() if mode == "replace" else self._file_catalog()
        added = 0
        for k, v in incoming.items():
            if k not in cat:
                added += 1
                cat[k] = v
            elif overwrite_names and v:
                cat[k] = v
        p = self._write_catalog(cat)
        return added, len(cat), p

    def api_import_apply(self, b):
        kind, obj = self._parse_upload(b.get("content"), b.get("filename", ""))
        if kind == "catalog":
            added, total, p = self._merge_catalog(obj, b.get("mode", "merge"), b.get("overwrite_names", True))
            self.reload()
            return {"ok": True, "msg": f"点位清单已更新：新增 {added} 个，共 {total} 个（{os.path.basename(p)}）"}
        choices = b.get("choices") or {}
        touched, done, new_events = set(), Counter(), {}
        for sec in ("templates", "dicts", "events", "cases", "sources"):
            for k, v in (obj.get(sec) or {}).items():
                act = choices.get(f"{sec}\t{k}", "skip")
                if act == "skip":
                    continue
                cur, _ = self.store.get(sec, k)
                key = str(k)
                if sec == "dicts" and act == "merge" and cur is not None:
                    ci, cm = tc.split_dict(cur)
                    ni, nm = tc.split_dict(v)
                    items = dict(ni)
                    items.update(ci)  # 已有的值保持不变，只补新的
                    evs = list(dict.fromkeys(cm["events"] + nm["events"]))
                    v = dict_value(items, evs, cm["desc"] or nm["desc"])
                touched.add(self.store.put(sec, key, v, save=False))
                done[sec] += 1
                if sec == "events":
                    new_events[key] = v or {}
        for p in touched:
            self.store.save(p)
        msg = []
        if obj.get("catalog") and b.get("catalog", True):
            added, total, _ = self._merge_catalog(tc.parse_catalog(obj["catalog"]), "merge", False)
            msg.append(f"点位清单新增 {added} 个")
        author = str((obj.get("meta") or {}).get("author") or "")
        sample_errs = []
        for fn, smp in (obj.get("datasource_samples") or {}).items():
            fn = os.path.basename(str(fn))
            if not fn or not isinstance(smp, dict) or choices.get(f"samples\t{fn}", "skip") == "skip":
                continue
            try:
                self._apply_sample(fn, smp, author)
                done["samples"] += 1
            except Exception as e:
                sample_errs.append(f"{fn}: {e}")
        ed = obj.get("event_dict") or {}
        if isinstance(ed, dict) and ed.get("entries") and choices.get("evdict\t埋点字典", "skip") != "skip":
            d = tc.load_event_dict(self.spec.event_dict_path)
            ents = d.setdefault("entries", {})
            for k, e in ed["entries"].items():
                if str(k) not in ents and isinstance(e, dict):
                    ents[str(k)] = e
                    done["evdict"] += 1
            cols = d.setdefault("columns", [])
            cols.extend(c for c in (ed.get("columns") or []) if c not in cols)
            have = {x.get("name") for x in d.setdefault("docs", [])}
            d["docs"].extend(dict(x) for x in (ed.get("docs") or []) if isinstance(x, dict) and x.get("name") not in have)
            self._save_evdict(d)
        for fn, text in (obj.get("doc_files") or {}).items():
            fn = os.path.basename(str(fn))
            if not fn or choices.get(f"docs\t{fn}", "skip") == "skip":
                continue
            os.makedirs(self._docs_dir(), exist_ok=True)
            with open(os.path.join(self._docs_dir(), fn), "w", encoding="utf-8") as f:
                f.write(str(text))
            done["docs"] += 1
        self.reload()
        if new_events:
            self._autotag(new_events)
            self.reload()
        names = {"events": "点位", "dicts": "字典", "templates": "模板", "cases": "用例", "sources": "数据源配置", "samples": "数据源样本",
                 "evdict": "埋点字典点位", "docs": "埋点文档"}
        msg = [f"{names[k]} {n} 个" for k, n in done.items() if n] + msg
        if sample_errs:
            msg.append("样本失败：" + "；".join(sample_errs))
        return {"ok": True, "msg": "已导入：" + ("，".join(msg) if msg else "没有选中任何内容")}

    # ---- POST：实时 / 设备 ----
    def api_live_start(self, b):
        if self.live and self.live.running:
            raise ApiError("已经在抓取中")
        only = b.get("only") or []
        case_name = b.get("case")
        target_eids = [str(x) for x in only if str(x).strip()]
        if not target_eids and case_name and case_name in self.spec.cases:
            for item in (self.spec.cases[case_name] or {}).get("expect") or []:
                eid = item.get("id") if isinstance(item, dict) else item
                if eid:
                    target_eids.append(str(eid))
        apis = self.resolve_event_apis(target_eids or None) if b.get("auto_api", True) else []
        try:
            self.live = LiveSession(self, b.get("serial"), b.get("clear", True), case_name, only,
                                    b.get("scenario"), b.get("snapshot", True), capture_apis=apis)
        except FileNotFoundError:
            raise ApiError("找不到 adb，请把 Android SDK platform-tools 加到 PATH")
        return {"ok": True, "raw": self.live.raw, "snap_on": self.live.snap_on, "snap_err": self.live.snap_err,
                "api_targets": self.live.api_targets, "api_err": self.live.api_err}

    def api_live_scenario(self, b):
        if not self.live:
            raise ApiError("还没开始抓取")
        ap = b.get("apply_pending")
        self.live.set_scenario(b.get("scenario") or {}, ap if ap == "all" else bool(ap))
        return {"ok": True}

    def api_live_decide(self, b):
        if not self.live:
            raise ApiError("还没开始抓取")
        self.live.decide(b.get("seqs") or b["seq"], b["field"], b.get("value"))
        return {"ok": True}

    # ---- 数据源目录 & 历史版本 & OkHttp 接口抓取 ----
    def _ds_dir(self):
        d = self.spec.datasource_dir
        os.makedirs(d, exist_ok=True)
        return d

    def _hist_dir(self):
        hd = os.path.join(self._ds_dir(), ".history")
        os.makedirs(hd, exist_ok=True)
        return hd

    def _hist_meta_path(self):
        return os.path.join(self._hist_dir(), "_meta.json")

    def _hist_meta_load(self):
        p = self._hist_meta_path()
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    d.setdefault("active", {})
                    d.setdefault("records", {})
                    return d
            except Exception:
                pass
        return {"active": {}, "records": {}}

    def _hist_meta_save(self, meta):
        with open(self._hist_meta_path(), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _file_sha1(p):
        try:
            with open(p, "rb") as f:
                return hashlib.sha1(f.read()).hexdigest()
        except OSError:
            return ""

    def _list_ds_history(self, fname, meta=None):
        hd = self._hist_dir()
        stem, ext = os.path.splitext(fname)
        pat = re.compile(r"^" + re.escape(stem) + r"_(\d{8}_\d{6}(?:_\d+)?)" + re.escape(ext) + r"$")
        meta = meta if meta is not None else self._hist_meta_load()
        cur_path = os.path.join(self._ds_dir(), fname)
        cur_sha = self._file_sha1(cur_path)
        active_name = meta.get("active", {}).get(fname)
        items = []
        for hf in os.listdir(hd):
            m = pat.match(hf)
            if not m:
                continue
            hp = os.path.join(hd, hf)
            if not os.path.isfile(hp):
                continue
            rec = meta.get("records", {}).get(hf) or {}
            st = os.stat(hp)
            stamp = m.group(1)
            t_str = rec.get("time") or f"{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"
            items.append({
                "file": hf,
                "time": t_str,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "url": rec.get("url") or "",
                "source": rec.get("source") or "",
                "sha": self._file_sha1(hp),
            })
        items.sort(key=lambda x: (x["mtime"], x["file"]), reverse=True)
        matched_active = False
        for i, it in enumerate(items):
            it["is_latest"] = (i == 0)
            is_act = (it["file"] == active_name and it["sha"] == cur_sha)
            it["active"] = is_act
            if is_act:
                matched_active = True
        if not matched_active and cur_sha:
            for it in items:
                if it["sha"] == cur_sha:
                    it["active"] = True
                    break
        for it in items:
            it.pop("sha", None)
            it.pop("mtime", None)
        return items

    def _save_json_ds(self, name, obj, url="", source="manual", keep=20):
        name = re.sub(r"[^\w\-]", "_", str(name or "").strip()) or "api_data"
        fname = name + ".json"
        with self._ds_lock:
            d = self._ds_dir()
            hd = self._hist_dir()
            cur_path = os.path.join(d, fname)
            meta = self._hist_meta_load()

            # 若当前已有该数据源文件且尚未存入历史，先归档旧版本避免覆盖丢失
            if os.path.isfile(cur_path):
                cur_sha = self._file_sha1(cur_path)
                stem, ext = os.path.splitext(fname)
                pat = re.compile(r"^" + re.escape(stem) + r"_\d{8}_\d{6}(?:_\d+)?" + re.escape(ext) + r"$")
                known_shas = {self._file_sha1(os.path.join(hd, hf)) for hf in os.listdir(hd) if pat.match(hf)}
                if cur_sha and cur_sha not in known_shas:
                    old_st = os.stat(cur_path)
                    old_stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(old_st.st_mtime))
                    old_hf = f"{stem}_{old_stamp}{ext}"
                    idx = 1
                    while os.path.exists(os.path.join(hd, old_hf)):
                        old_hf = f"{stem}_{old_stamp}_{idx}{ext}"
                        idx += 1
                    shutil.copy2(cur_path, os.path.join(hd, old_hf))
                    meta["records"][old_hf] = {
                        "url": "", "source": "原有文件",
                        "time": time.strftime("%m-%d %H:%M:%S", time.localtime(old_st.st_mtime)),
                    }

            payload = json.dumps(obj, ensure_ascii=False, indent=2)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            now_str = time.strftime("%m-%d %H:%M:%S")
            hf = f"{name}_{stamp}.json"
            idx = 1
            while os.path.exists(os.path.join(hd, hf)):
                hf = f"{name}_{stamp}_{idx}.json"
                idx += 1
            hp = os.path.join(hd, hf)
            with open(hp, "w", encoding="utf-8") as f:
                f.write(payload)
            with open(cur_path, "w", encoding="utf-8") as f:
                f.write(payload)

            meta["active"][fname] = hf
            meta["records"][hf] = {"url": url, "source": source, "time": now_str}

            # 清理超过 keep 条的旧历史
            pat = re.compile(r"^" + re.escape(name) + r"_\d{8}_\d{6}(?:_\d+)?\.json$")
            all_h = sorted([os.path.join(hd, x) for x in os.listdir(hd) if pat.match(x)], key=os.path.getmtime)
            for oldp in all_h[:-keep]:
                bn = os.path.basename(oldp)
                if bn == hf:
                    continue
                try:
                    os.remove(oldp)
                except OSError:
                    pass
                meta["records"].pop(bn, None)

            self._hist_meta_save(meta)
            self.reload()
            keys = list(obj.keys())[:8] if isinstance(obj, dict) else [f"[{len(obj)}]"]
            return {
                "name": name, "file": fname, "hist_file": hf, "time": now_str,
                "size": os.path.getsize(cur_path), "url": url, "keys": keys,
            }

    def _remember_api_preset(self, endpoint, name, tag=""):
        dev = dict(self.store.main.get("device") or {})
        presets = [p for p in (dev.get("api_presets") or []) if isinstance(p, dict) and p.get("endpoint")]
        ep = str(endpoint or "").strip()
        presets = [p for p in presets if p.get("endpoint") != ep]
        presets.insert(0, {"endpoint": ep, "name": name, **({"tag": tag} if tag else {})})
        dev["api_presets"] = presets[:12]
        self.store.put_top("device", dev)

    def api_ds_list(self, q):
        d = self._ds_dir()
        auto = tc.scan_datasources(d)
        by_file = {os.path.basename(v["path"]): k for k, v in auto.items()}
        meta = self._hist_meta_load()
        files = []
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if not os.path.isfile(p) or f.startswith(".") or re.search(r"-(wal|shm|journal)$", f):
                continue
            name = by_file.get(f)
            info = {"file": f, "name": name, "type": auto.get(name, {}).get("type") if name else None, **file_info(p)}
            if info["type"] == "sqlite":
                try:
                    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                    info["tables"] = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'android_metadata' ORDER BY name")]
                    conn.close()
                except sqlite3.Error as e:
                    info["error"] = str(e)
            elif info["type"] == "json_file":
                try:
                    with open(p, encoding="utf-8-sig") as fh:
                        obj = json.load(fh)
                    info["keys"] = list(obj.keys())[:8] if isinstance(obj, dict) else [f"[{len(obj)}]"]
                except Exception as e:
                    info["error"] = f"JSON 解析失败: {e}"
            hist = self._list_ds_history(f, meta)
            info["history"] = hist
            act = next((h for h in hist if h.get("active")), None)
            info["active_hist"] = act
            info["url"] = (act.get("url") if act else "") or (hist[0].get("url") if hist else "")
            files.append(info)
        dev = self.store.main.get("device") or {}
        return {"dir": d, "files": files, "api_presets": dev.get("api_presets") or []}

    def api_ds_upload(self, b):
        import base64
        fn = os.path.basename(str(b.get("filename") or "")).strip()
        if not fn:
            raise ApiError("文件名为空")
        data = base64.b64decode(b.get("b64") or "")
        if fn.lower().endswith(".json"):
            try:
                obj = json.loads(data.decode("utf-8-sig"))
            except Exception as e:
                raise ApiError(f"{fn} 不是合法的 JSON：{e}")
            stem = os.path.splitext(fn)[0]
            rec = self._save_json_ds(stem, obj, source="上传文件")
            return {"ok": True, "file": rec["file"]}
        p = os.path.join(self._ds_dir(), fn)
        with open(p, "wb") as f:
            f.write(data)
        self.reload()
        return {"ok": True, "file": fn}

    def api_ds_paste(self, b):
        name = re.sub(r"[^\w\-]", "_", str(b.get("name") or "").strip())
        if not name:
            raise ApiError("给这份数据起个名字（比如 mine_page）")
        try:
            obj = json.loads(b.get("text") or "")
        except Exception as e:
            raise ApiError(f"不是合法的 JSON：{e}")
        self._save_json_ds(name, obj, source="手动粘贴")
        return {"ok": True, "name": name}

    def api_ds_history_use(self, b):
        fname = os.path.basename(str(b.get("file") or "").strip())
        hname = os.path.basename(str(b.get("hist") or "").strip())
        if not fname or not hname:
            raise ApiError("缺少文件参数")
        with self._ds_lock:
            hp = os.path.join(self._hist_dir(), hname)
            if not os.path.isfile(hp):
                raise ApiError("该历史记录不存在")
            dst = os.path.join(self._ds_dir(), fname)
            shutil.copyfile(hp, dst)
            meta = self._hist_meta_load()
            meta["active"][fname] = hname
            self._hist_meta_save(meta)
            self.reload()
            rec = meta.get("records", {}).get(hname) or {}
            return {"ok": True, "file": fname, "hist": hname, "time": rec.get("time") or hname}

    def api_ds_history_delete(self, b):
        hname = os.path.basename(str(b.get("hist") or "").strip())
        if not hname:
            raise ApiError("缺少历史文件名")
        with self._ds_lock:
            hp = os.path.join(self._hist_dir(), hname)
            if not os.path.isfile(hp):
                raise ApiError("该历史记录不存在")
            os.remove(hp)
            meta = self._hist_meta_load()
            meta["records"].pop(hname, None)
            for k, v in list(meta.get("active", {}).items()):
                if v == hname:
                    meta["active"].pop(k, None)
            self._hist_meta_save(meta)
            return {"ok": True}

    def api_ds_history_json(self, q):
        hname = os.path.basename(str(q.get("hist") or "").strip())
        hp = os.path.join(self._hist_dir(), hname)
        if not hname or not os.path.isfile(hp):
            raise ApiError("该历史记录不存在")
        try:
            with open(hp, encoding="utf-8-sig") as f:
                return {"data": json.load(f), "file": hname}
        except Exception as e:
            raise ApiError(f"读取失败：{e}")

    def api_ds_api_capture_start(self, b):
        ep = str(b.get("endpoint") or "").strip()
        if not ep:
            raise ApiError("先输入要抓取的接口地址，如 /config/app/myPageV3")
        if self.api_cap and self.api_cap.running:
            self.api_cap.stop()
        name = re.sub(r"[^\w\-]", "_", str(b.get("name") or "").strip()) or infer_api_ds_name(ep)
        tag = str(b.get("tag") or "").strip()
        try:
            self.api_cap = ApiCaptureSession(self, ep, name, b.get("serial"), b.get("clear", True), tag)
        except FileNotFoundError:
            raise ApiError("找不到 adb，请把 Android SDK platform-tools 加到 PATH")
        self._remember_api_preset(ep, name, tag)
        return self.api_cap.snapshot()

    def api_ds_api_capture_poll(self, q):
        if not self.api_cap:
            return {"running": False, "endpoint": "", "name": "", "tag": "", "lines": 0, "captured": [], "error": ""}
        return self.api_cap.snapshot()

    def api_ds_api_capture_stop(self, b):
        if not self.api_cap:
            return {"ok": True, "captured": []}
        self.api_cap.stop()
        snap = self.api_cap.snapshot()
        return {"ok": True, **snap}

    def api_ds_api_capture_extract(self, b):
        ep = str(b.get("endpoint") or "").strip()
        text = str(b.get("text") or "")
        if not ep:
            raise ApiError("先输入要提取的接口地址，如 /config/app/myPageV3")
        if not text.strip():
            raise ApiError("请粘贴包含 OkHttp 响应的 logcat 日志文本")
        name = re.sub(r"[^\w\-]", "_", str(b.get("name") or "").strip()) or infer_api_ds_name(ep)
        tag = str(b.get("tag") or "").strip()
        ext = OkHttpApiExtractor(ep, tag)
        hits = []
        for line in text.splitlines():
            h = ext.feed(line)
            if h:
                hits.append(h)
        hits.extend(ext.flush())
        if not hits:
            raise ApiError(f"没有在日志中找到接口「{ep}」的有效 JSON 返回值（请确认 OkHttp 日志级别为 BODY 且包含返回 JSON）")
        saved = []
        for url, obj in hits:
            saved.append(self._save_json_ds(name, obj, url=url, source="日志提取"))
        self._remember_api_preset(ep, name, tag)
        return {"ok": True, "count": len(saved), "latest": saved[-1], "saved": saved}

    def api_ds_delete(self, b):
        fn = os.path.basename(str(b.get("file") or ""))
        p = os.path.join(self._ds_dir(), fn)
        if not os.path.isfile(p):
            raise ApiError("文件不存在")
        for suffix in ("", "-wal", "-shm", "-journal"):
            if os.path.exists(p + suffix):
                os.remove(p + suffix)
        self.reload()
        return {"ok": True}

    def api_device_dbs(self, b):
        pkg = str(b.get("package") or "").strip()
        if not pkg:
            raise ApiError("先填 App 包名")
        try:
            return {"dbs": tc.list_app_dbs(tc.adb_base(self.spec, b.get("serial")), pkg)}
        except FileNotFoundError:
            raise ApiError("找不到 adb")
        except RuntimeError as e:
            raise ApiError(str(e))

    def api_pull_dbs(self, b):
        pkg = str(b.get("package") or "").strip()
        dbs = [str(x) for x in (b.get("dbs") or []) if str(x).strip()]
        if not pkg or not dbs:
            raise ApiError("需要包名和至少一个数据库")
        adb = tc.adb_base(self.spec, b.get("serial"))
        ok, errs = [], []
        for db in dbs:
            try:
                self._backup_ds(db)
                tc.pull_app_db(adb, pkg, db, os.path.join(self._ds_dir(), db))
                ok.append(db)
            except Exception as e:
                errs.append(f"{db}: {e}")
        dev = dict(self.store.main.get("device") or {})
        dev["package"], dev["dbs"] = pkg, dbs        # 记住，下次 / 实时调试快照直接用
        self.store.put_top("device", dev)
        self.reload()
        if not ok:
            raise ApiError("；".join(errs))
        return {"ok": ok, "errors": errs}

    def _backup_ds(self, fname, keep=5):
        p = os.path.join(self._ds_dir(), fname)
        if not os.path.exists(p):
            return
        hd = os.path.join(self._ds_dir(), ".history")
        os.makedirs(hd, exist_ok=True)
        stem, ext = os.path.splitext(fname)
        shutil.copyfile(p, os.path.join(hd, f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}{ext}"))
        olds = sorted(glob.glob(os.path.join(hd, f"{stem}_*{ext}")), key=os.path.getmtime)
        for x in olds[:-keep]:
            try:
                os.remove(x)
            except OSError:
                pass

    def api_pull_apidump(self, b):
        pkg = str(b.get("package") or (self.spec.device or {}).get("package") or "").strip()
        if not pkg:
            raise ApiError("先填 App 包名")
        adb = tc.adb_base(self.spec, b.get("serial"))
        remote = f"/sdcard/Android/data/{pkg}/files/api_dump/."
        try:
            p = subprocess.run(adb + ["pull", remote, self._ds_dir()], capture_output=True, text=True, errors="replace", timeout=120, **tc.popen_kw())
        except FileNotFoundError:
            raise ApiError("找不到 adb")
        if p.returncode != 0:
            raise ApiError(f"拉取失败（App 里加了 ApiDumpInterceptor 吗？）：{(p.stderr or p.stdout).strip()[-200:]}")
        self.reload()
        return {"ok": True, "log": (p.stdout or "").strip()[-300:]}

    # ---- 埋点字典 ----
    def _save_evdict(self, d):
        p = self.spec.event_dict_path
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)

    def _docs_dir(self):
        return os.path.join(os.path.dirname(self.store.path), "docs")

    def _doc_path(self, name, stored_path=None):
        if stored_path and os.path.exists(stored_path):
            return stored_path
        cand = os.path.join(self._docs_dir(), os.path.basename(str(name or "")))
        return cand if os.path.isfile(cand) else ""

    def _sync_events_from_entries(self, entries):
        """把埋点字典里「点位规则」还没有的点位，自动新增到规则文件 events 下（点位ID + 事件名称）"""
        existing = self.store.items("events")
        added = []
        touched = set()
        for rid, e in entries.items():
            rid = str(rid)
            if rid in existing:
                continue
            touched.add(self.store.put("events", rid, {"name": e.get("name") or "", "fields": {}}, save=False))
            added.append(rid)
        for p in touched:
            self.store.save(p)
        return added

    def api_evdict(self, q):
        d = tc.load_event_dict(self.spec.event_dict_path)
        conf = {eid for eid, es in self.spec.events.items() if not tc.is_stub_event(es)}
        docs, known, valid_names = [], set(), set()
        for x in d.get("docs", []):
            item = dict(x)
            p = self._doc_path(item.get("name"), item.get("path"))
            if not p:
                continue  # docs 目录里已删除的文档直接不显示
            item["path"] = p
            known.add(os.path.normcase(os.path.abspath(p)))
            valid_names.add(item.get("name"))
            item["entries"] = sum(1 for e in d["entries"].values() if item.get("name") in (e.get("docs") or []))
            docs.append(item)
        ents = []
        for raw_e in sorted(d["entries"].values(), key=lambda e: e["id"]):
            e = dict(raw_e)
            e["configured"] = e["id"] in conf
            e["module"] = tc.entry_module(e)
            e["docs"] = [dn for dn in (e.get("docs") or []) if dn in valid_names]
            ents.append(e)
        # docs 目录里有、但还没导入字典的文档（比如手动拷进来的）
        untracked = []
        dd = self._docs_dir()
        if os.path.isdir(dd):
            for fn in sorted(os.listdir(dd)):
                fp = os.path.join(dd, fn)
                if os.path.isfile(fp) and fn.lower().endswith(DOC_EXTS) and os.path.normcase(os.path.abspath(fp)) not in known \
                        and fn not in valid_names:
                    untracked.append({"name": fn, "size": os.path.getsize(fp)})
        return {"entries": ents, "docs": docs, "untracked": untracked, "docs_dir": dd,
                "columns": d.get("columns", []), "modules": tc.dict_modules(d)}

    def api_evdict_doc_remove(self, b):
        """把一份文档从字典的文档列表里移除；drop_entries=True 时顺带删掉只来自这份文档的点位"""
        name = str(b.get("name") or "")
        d = tc.load_event_dict(self.spec.event_dict_path)
        before = len(d.get("docs", []))
        d["docs"] = [x for x in d.get("docs", []) if x.get("name") != name]
        if len(d["docs"]) == before:
            raise ApiError(f"字典里没有文档「{name}」")
        dropped = 0
        for rid in list(d["entries"]):
            e = d["entries"][rid]
            ds = [x for x in (e.get("docs") or []) if x != name]
            if b.get("drop_entries") and (e.get("docs") or []) and not ds:
                del d["entries"][rid]
                dropped += 1
            else:
                e["docs"] = ds
        if b.get("delete_file"):
            p = self._doc_path(name)
            if p and os.path.realpath(p).startswith(os.path.realpath(self._docs_dir())):
                os.remove(p)
        self._save_evdict(d)
        self.reload()
        return {"ok": True, "dropped": dropped, "total": len(d["entries"])}

    def api_evdict_import_file(self, b):
        """导入 docs 目录里已有的文档文件"""
        fn = os.path.basename(str(b.get("name") or ""))
        p = os.path.join(self._docs_dir(), fn)
        if not fn or not os.path.isfile(p):
            raise ApiError(f"docs 目录里没有 {fn}")
        with open(p, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
        return self.api_evdict_import({"content": text, "filename": fn, "overwrite": b.get("overwrite")})

    def api_evdict_import(self, b):
        text, fn = b.get("content") or "", os.path.basename(str(b.get("filename") or "埋点文档"))
        if fn.lower().endswith(".json"):
            cat = tc.parse_catalog(json.loads(text))
            entries = OrderedDict((k, {"id": k, "name": v, "columns": {"点位ID": k, "事件名称": v}, "rows": []}) for k, v in cat.items())
        else:
            entries = tc.doc_to_event_entries(text, fn)
        if not entries:
            n = len(tc.parse_doc_tables(text, fn))
            raise ApiError(f"没解析出点位：找到 {n} 个表格，但没有认出「点位ID」列。确认是 Markdown 表格（| 列 | 列 |，第二行 |---|），且有一列是点位 ID")
        # 保存副本到 docs/ 目录，方便后续点击直接在文件管理器打开
        doc_name = b.get("doc_name") or fn
        docs_dir = self._docs_dir()
        os.makedirs(docs_dir, exist_ok=True)
        safe_fn = re.sub(r'[<>:"/\\|?*]+', "_", fn) or "埋点文档.md"
        saved = os.path.join(docs_dir, safe_fn)
        with open(saved, "w", encoding="utf-8") as fh:
            fh.write(text)
        d = tc.load_event_dict(self.spec.event_dict_path)
        added, skipped, updated = tc.merge_event_dict(d, entries, doc_name, bool(b.get("overwrite")))
        for item in d.get("docs", []):
            if item.get("name") == doc_name:
                item["path"] = saved
        self._save_evdict(d)
        rules_added = self._sync_events_from_entries(entries)
        self.reload()
        return {"parsed": len(entries), "added": len(added), "skipped": len(skipped), "updated": len(updated),
                "rules_added": len(rules_added), "added_ids": added[:200], "total": len(d["entries"]), "path": saved}

    def api_evdict_delete(self, b):
        d = tc.load_event_dict(self.spec.event_dict_path)
        if b.get("all"):
            d = {"entries": {}, "docs": [], "columns": []}
        else:
            d["entries"].pop(str(b.get("id")), None)
        self._save_evdict(d)
        self.reload()
        return {"ok": True}

    # ---- 给 AI 的提示词 ----
    def _prompt_cheatsheet(self):
        return [
            "## 规则写法速查",
            "- 顶层可包含 `events:`（必填），若确需新增公共模板/字典/合并数据源也可一并输出 `templates:` / `dicts:` / `sources:`（优先复用下方已有的）。",
            "- 点位与模板结构：",
            "  - 复用公共模板：`extends: <模板名>`（多个同页面/同接口点位建议抽模板，子点位只写差异字段）",
            "  - 绑定接口（实时调试自动从 OkHttp logcat 抓取响应 JSON 更新数据源）：",
            "    `api: {endpoint: /config/app/myPageV3, source: myPageV3}`（可写在模板或点位上，子点位自动继承）",
            "  - 绑定数据源：模板或用例上可写 `sources: [mine_page_merged, app]`，表示这组点位只关注这些数据源（下方列出的就是绑定结果）",
            "  - 去重校验：`dedupe: [resID, operationCardId]`，`dedupe_window: 2`（秒）",
            "  - 上下文定位 `context`：按上报字段值去接口 JSON 里定位当前卡片/条目：",
            "    `res: {from: <数据源名>, path: \"data.myPage[*].res[*]\", where: {id: \"{resID}\"}}`",
            "    （`from` 也支持数组回退 `[主源, 兜底源]`；若下方已有 `合并兜底 JSON` 数据源，直接 `from: <合并源名>` 即可同时覆盖接口下发与空数组默认兜底）",
            "- 字段规则 `fields`（每个字段一条）：",
            "  - 接口取值：`{from: res.planId, desc: 投放计划ID}`；需字典映射加 `map: 字典名` 或链式 `map: [字典1, 字典2]` 或内联 `{a: 1}`",
            "  - 文档枚举：`{enum: {0: 端内, 1: 桌面小组件}, desc: 入口}`（或 `enum: 已有字典名`）",
            "  - 取决于用户手势/入口的字段：加 `ask: true`（测试时按用例期望比对或人工点选确认）",
            "  - 数据库 SQL：`{db: <db名>, sql: \"SELECT count(*) FROM t WHERE type = :type\", desc: 数量}`（`:字段名` 引用上报字段）",
            "  - 动态变化计数：加 `compare: lte`（上报 ≤ 快照值）、`trend: up`（单调不减）或 `tolerance: 1`",
            "  - 其它常用：`type: int`、`range: [1, 20]`、`regex: \"^...\"`、`not_empty: true`、`required: false`、`ignore: true`",
            "- 跨字段断言：`asserts: [{expr: \"int(allNums) == int(a) + int(b)\", msg: 总数应等于分项之和}]`",
            "- 若业务存在「接口某模块 `res` 为空数组 `[]` 时走本地 `fallback_data` 默认兜底、非空走云侧且保持接口顺序」，可在 `sources:` 定义合并源：",
            "  `mine_page_merged: {type: merged_json, primary: [myPageV3, mine_api_cache], fallback: fallback_data, path: data.myPage, key: key, empty_field: res, fallback_keys: [myPage_function_new, myPage_top]}`",
            "",
        ]

    def _case_prompt(self, case_name):
        s = self.spec
        case = s.cases.get(case_name)
        if case is None:
            raise ApiError(f"没有用例 {case_name}")
        eids = self._case_ids(case)
        if not eids:
            raise ApiError("这个用例里还没有点位")
        lines = ["你是 Android 埋点校验规则助手（埋点小岛 trackcheck）。下面是一个测试用例里的全部点位，",
                 f"请为用例「{case_name}」中的 {len(eids)} 个点位一次性编写 trackcheck 校验规则（YAML），只输出可直接导入的 YAML 代码块。",
                 "同页面 / 同接口的点位请优先抽成模板（templates），子点位只写差异字段。", ""]
        if case.get("desc"):
            lines += [f"用例场景：{case['desc']}", ""]
        lines += self._prompt_cheatsheet()
        union = OrderedDict()
        for eid in eids:
            names, why = self.event_sources(eid)
            for n in names:
                union.setdefault(n, [])
                for w in why.get(n, []):
                    if w not in union[n]:
                        union[n].append(w)
        if union:
            lines.append("## 本用例用到的数据源")
            for n, w in union.items():
                lines.append(self._source_prompt_line(n, w))
        else:
            lines.append("## 可用数据源（用例还没有绑定数据源，以下列出全部；建议先在用例上「绑定数据源」）")
            for n in s.sources:
                lines.append(self._source_prompt_line(n, max_paths=60))
        lines.append("")
        expect = {}
        for item in case.get("expect") or []:
            if isinstance(item, dict) and item.get("values"):
                expect[str(item.get("id"))] = item["values"]
        lines.append("## 点位清单")
        for i, eid in enumerate(eids, 1):
            ent = s.event_dict.get("entries", {}).get(eid) or {}
            name = ent.get("name") or (s.events.get(eid) or {}).get("name") or s.catalog.get(eid, "")
            lines.append(f"### {i}. `{eid}` {name}")
            for k, v in (ent.get("columns") or {}).items():
                if v:
                    lines.append(f"- {k}：{v}")
            rows = ent.get("rows") or []
            if len(rows) > 1:
                hdr = list(rows[0].keys())
                lines += ["", "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
                lines += ["| " + " | ".join(str(r.get(h, "")).replace("\n", " ") for h in hdr) + " |" for r in rows]
            if expect.get(eid):
                lines.append("- 用例期望值（由操作决定，可写 `ask: true`）：" + json.dumps(expect[eid], ensure_ascii=False))
            cur = s._raw_events.get(eid)
            if cur and not tc.is_stub_event(s.events.get(eid) or {}):
                lines += ["- 现有规则：", "```yaml",
                          yaml.safe_dump(plain({eid: cur}), allow_unicode=True, sort_keys=False, default_flow_style=None).rstrip(), "```"]
            lines.append("")
        if s.templates:
            lines.append("## 已有模板（可 `extends` 直接继承复用）")
            for tname, tv in s.templates.items():
                tv = tv or {}
                api_ep = (tv.get("api") or {}).get("endpoint") if isinstance(tv.get("api"), dict) else tv.get("api")
                lines.append(f"- `{tname}`" + (f"（关联接口 {api_ep}）" if api_ep else "") +
                             f"：context={list((tv.get('context') or {}).keys())}，fields={list((tv.get('fields') or {}).keys())}")
        if s.dicts:
            lines.append("\n## 已有公共字典（可在 `enum` / `map` 中按名字直接引用）")
            for dname, items in s.dicts.items():
                preview = ", ".join(f"{k}:{v}" for k, v in list(items.items())[:10])
                lines.append(f"- `{dname}`：{{{preview}{' ...' if len(items) > 10 else ''}}}")
        return {"text": "\n".join(lines), "events": len(eids), "sources": list(union.keys())}

    def api_ai_prompt(self, b):
        if b.get("case"):
            return self._case_prompt(str(b["case"]))
        eid = str(b.get("id") or "").strip()
        s = self.spec
        ent = s.event_dict.get("entries", {}).get(eid)
        lines = ["你是 Android 埋点校验规则助手（埋点小岛 trackcheck）。请根据下面的【埋点文档】【上报日志】【可用数据源 / 模板 / 字典】，",
                 f"为点位 `{eid}` 编写 trackcheck 校验规则（YAML），只输出可直接导入或粘贴进 trackcheck.yaml 的 YAML 代码块。", ""]
        lines += self._prompt_cheatsheet()
        if ent:
            lines += ["## 埋点文档", f"点位ID：{eid}　名称：{ent.get('name', '')}"]
            for k, v in ent.get("columns", {}).items():
                if v:
                    lines.append(f"- {k}：{v}")
            rows = ent.get("rows") or []
            if len(rows) > 1:
                hdr = list(rows[0].keys())
                lines += ["", "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
                lines += ["| " + " | ".join(str(r.get(h, "")).replace("\n", " ") for h in hdr) + " |" for r in rows]
            lines.append("")
        if b.get("line") or b.get("fields"):
            lines += ["## 上报日志样本", b.get("line") or json.dumps(b.get("fields"), ensure_ascii=False), ""]
        names, why = self.event_sources(eid)
        if names:
            lines.append("## 这个点位用到的数据源（来自模板 / 用例绑定和现有规则引用）")
            for name in names:
                lines.append(self._source_prompt_line(name, why.get(name)))
        else:
            lines.append("## 可用数据源（该点位还没有绑定数据源，以下列出全部；建议在「测试用例」或「模板」里绑定后再复制，提示词会更精准）")
            for name in self.spec.sources:
                lines.append(self._source_prompt_line(name, max_paths=60))
        lines.append("")
        if s.templates:
            lines.append("\n## 已有模板（可 `extends` 直接继承复用）")
            for tname, tv in s.templates.items():
                tv = tv or {}
                api_ep = (tv.get("api") or {}).get("endpoint") if isinstance(tv.get("api"), dict) else tv.get("api")
                ctx_k = list((tv.get("context") or {}).keys())
                fld_k = list((tv.get("fields") or {}).keys())
                lines.append(f"- `{tname}`" + (f"（关联接口 {api_ep}）" if api_ep else "") +
                             f"：context={ctx_k}，fields={fld_k}")
        if s.dicts:
            lines.append("\n## 已有公共字典（可在 `enum` / `map` 中按名字直接引用）")
            for dname, items in s.dicts.items():
                preview = ", ".join(f"{k}:{v}" for k, v in list(items.items())[:10])
                lines.append(f"- `{dname}`：{{{preview}{' ...' if len(items) > 10 else ''}}}")
        cur = s._raw_events.get(eid)
        if cur:
            lines += ["", "## 现有规则（在此基础上修改）",
                      yaml.safe_dump(plain({"events": {eid: cur}}), allow_unicode=True, sort_keys=False, default_flow_style=None)]
        return {"text": "\n".join(lines), "sources": names}

    def api_live_stop(self, b):
        if not self.live or not self.live.running:
            raise ApiError("当前没有在抓取")
        return self.live.stop(bool(b.get("pull")))

    def api_live_recheck(self, b):
        if not self.live:
            return {"ok": False}
        self.live.recheck()
        return {"ok": True, "count": len(self.live.results)}

    def api_live_clear(self, b):
        if self.live and self.live.running:
            raise ApiError("先停止抓取")
        self.live = None
        return {"ok": True}

    def api_pull(self, b):
        log = self.pull_log(b.get("serial"))
        self.reload()
        return {"log": log}

    def api_spec_text(self, q):
        p = self.store.path
        files = []
        for f in self.store.files:
            with open(f, encoding="utf-8") as fh:
                files.append({"path": f, "name": os.path.relpath(f, os.path.dirname(p)), "text": fh.read()})
        return {"files": files}

    def api_spec_text_save(self, b):
        name, text = str(b.get("name") or ""), b.get("text") or ""
        target = next((f for f in self.store.files if os.path.relpath(f, os.path.dirname(self.store.path)) == name), None)
        if not target:
            raise ApiError("没有这个规则文件")
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ApiError(f"YAML 格式有误：{e}")
        old = open(target, encoding="utf-8").read()
        shutil.copyfile(target, target + ".bak")
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
        self.reload()
        if self.spec_error:
            with open(target, "w", encoding="utf-8") as f:
                f.write(old)
            err = self.spec_error
            self.reload()
            raise ApiError("规则解析失败，已撤回：" + err)
        return {"ok": True}

    def api_evdict_samples(self, b):
        docs = sorted(glob.glob(os.path.join(os.path.dirname(self.store.path), "docs", "*.md")))
        if not docs:
            raise ApiError("规则文件同目录的 docs/ 下没有示例文档")
        d = tc.load_event_dict(self.spec.event_dict_path)
        tot, all_entries = 0, OrderedDict()
        for p in docs:
            bn = os.path.basename(p)
            with open(p, encoding="utf-8") as f:
                entries = tc.doc_to_event_entries(f.read(), bn)
            all_entries.update(entries)
            added, _, _ = tc.merge_event_dict(d, entries, bn, False)
            for item in d.get("docs", []):
                if item.get("name") == bn:
                    item["path"] = p
            tot += len(added)
        self._save_evdict(d)
        rules_added = self._sync_events_from_entries(all_entries)
        self.reload()
        return {"added": tot, "rules_added": len(rules_added), "total": len(d["entries"]), "docs": [os.path.basename(x) for x in docs]}

    def _is_hidden_desktop(self):
        if os.name != "nt":
            return False
        try:
            import ctypes
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            hd = u32.GetThreadDesktop(k32.GetCurrentThreadId())
            buf, n = ctypes.create_unicode_buffer(256), ctypes.c_ulong()
            if u32.GetUserObjectInformationW(hd, 2, buf, 512, ctypes.byref(n)):
                return buf.value.lower() != "default"
        except Exception:
            pass
        return False

    def _open_in_fm(self, rp):
        rp = os.path.normpath(rp)
        if os.name == "nt":
            import ctypes
            shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
            if os.path.isfile(rp):
                co_hr = ole32.CoInitialize(None)
                try:
                    shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
                    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
                    shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
                    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
                    shell32.ILFree.argtypes = [ctypes.c_void_p]
                    pidl = shell32.ILCreateFromPathW(rp)
                    if pidl:
                        try:
                            if shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0:
                                return
                        finally:
                            shell32.ILFree(pidl)
                finally:
                    if co_hr in (0, 1):
                        ole32.CoUninitialize()
                subprocess.Popen(f'explorer.exe /select,"{rp}"')
            else:
                if shell32.ShellExecuteW(None, "open", rp, None, None, 1) <= 32:
                    subprocess.Popen(f'explorer.exe "{rp}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", rp] if os.path.isfile(rp) else ["open", rp])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(rp) if os.path.isfile(rp) else rp])

    def api_open_path(self, b):
        base = os.path.dirname(self.store.path)
        target = ""
        if b.get("doc"):
            dn = str(b["doc"])
            evd = tc.load_event_dict(self.spec.event_dict_path)
            doc = next((x for x in evd.get("docs", []) if x.get("name") == dn), None)
            target = self._doc_path(dn, (doc or {}).get("path"))
            if not target and os.path.isdir(self._docs_dir()):
                target = self._docs_dir()
            if not target:
                raise ApiError(f"本地还没有保存「{dn}」的副本，重新拖入导入一次即可点击打开")
        elif b.get("spec_file"):
            sf = str(b["spec_file"])
            target = next((f for f in self.store.files if os.path.relpath(f, base) == sf), os.path.join(base, sf))
        elif b.get("ds_file"):
            target = os.path.join(self._ds_dir(), os.path.basename(str(b["ds_file"])))
        elif b.get("report"):
            nm = os.path.basename(str(b["report"]))
            for ext in (".html", ".json"):
                cand = os.path.join(self.out, nm + ext)
                if os.path.isfile(cand):
                    target = cand
                    break
            target = target or self.out
        elif b.get("path"):
            p = str(b["path"]).strip()
            target = p if os.path.isabs(p) else os.path.join(base, p)
        if not target or not os.path.exists(target):
            raise ApiError("文件或目录不存在：" + (target or "空路径"))
        rp = os.path.realpath(target)
        roots = [os.path.realpath(x) for x in (base, self._ds_dir(), self.out, self._docs_dir()) if x]
        allowed_exact = {os.path.realpath(f) for f in self.store.files}
        allowed_exact |= {os.path.realpath(s["path"]) for s in self.sessions() if s.get("path")}
        ok = rp in allowed_exact or any(rp == r or rp.startswith(r + os.sep) for r in roots)
        if not ok:
            raise ApiError("安全限制：只能打开项目、数据源或报告目录下的路径")
        sandbox = self._is_hidden_desktop()
        if not b.get("dry_run") and not sandbox:
            self._open_in_fm(rp)
        return {"ok": True, "path": rp, "kind": "file" if os.path.isfile(rp) else "dir", "sandbox": sandbox}

    def api_reload(self, b):
        self.reload()
        return {"ok": True, "error": self.spec_error}

    def api_data_info(self, q):
        """「我的数据在哪」：规则 / 用例 / 数据源 / 字典 / 文档 / 报告各自的位置与数量"""
        base = os.path.dirname(self.store.path)

        def cnt(d, exts=None):
            if not os.path.isdir(d):
                return 0
            return len([f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)) and not f.startswith(".")
                        and (not exts or f.lower().endswith(exts))])
        evd = tc.load_event_dict(self.spec.event_dict_path)
        ds = self._ds_dir()
        return {
            "frozen": FROZEN, "app_dir": APP_DIR, "data_dir": base, "spec": self.store.path,
            "spec_files": [os.path.relpath(p, base) for p in self.store.files],
            "counts": {"events": len(self.store.items("events")), "cases": len(self.store.items("cases")),
                       "templates": len(self.store.items("templates")), "dicts": len(self.store.items("dicts")),
                       "sources": len(self.spec.sources or {})},
            "datasources": {"path": ds, "files": cnt(ds, (".json", ".db", ".sqlite", ".sqlite3"))},
            "evdict": {"path": self.spec.event_dict_path, "entries": len(evd.get("entries", {}))},
            "docs": {"path": self._docs_dir(), "files": cnt(self._docs_dir(), DOC_EXTS)},
            "reports": {"path": self.out, "files": cnt(self.out, (".json",))},
            "seeded": os.path.isfile(os.path.join(base, ".seeded_from_exe")),
        }

    def api_server_log(self, q):
        mark = str(q.get("seen", "1")) != "0"
        return SERVER_LOG.snapshot(mark_seen=mark)

    def api_server_log_clear(self, b):
        SERVER_LOG.clear()
        return {"ok": True}

    def api_shutdown(self, b):
        print("收到网页端退出请求，正在关闭埋点小岛后台服务…")
        try:
            if self.live and self.live.running:
                self.live.running = False
                self.live.cap.stop(linger=0)
                if self.live._api_proc and self.live._api_proc.poll() is None:
                    self.live._api_proc.terminate()
            if self.api_cap and self.api_cap.running:
                self.api_cap.stop()
        except Exception:
            pass
        SERVER_LOG.flush_partial()

        def _bye():
            time.sleep(0.35)
            try:
                if HTTP_SERVER is not None:
                    HTTP_SERVER.shutdown()
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=_bye, daemon=True).start()
        return {"ok": True}
    def api_log_act(self, b):
        """网页端纯前端操作（复制点位、粘贴、恢复草稿…）也记进运行日志"""
        txt = re.sub(r"\s+", " ", str(b.get("text") or "")).strip()[:240]
        if txt:
            SERVER_LOG.emit("ACT", txt)
        return {"ok": True}


# ----------------------------------------------------------------------------
# 运行日志里的「用户操作 + 结果」：把每次接口调用翻译成一句人话
# ----------------------------------------------------------------------------
_SEC_NAME = {"cases": "测试用例", "sources": "数据源", "dicts": "字典映射", "templates": "模板", "events": "点位规则"}


def _sum_text(s):
    s = s or {}
    parts = [f"通过 {s.get('pass', 0)}"]
    for k, n in (("fail", "失败"), ("warn", "警告"), ("ask", "待确认"), ("unknown", "无规则")):
        if s.get(k):
            parts.append(f"{n} {s[k]}")
    return " / ".join(parts)


def describe_action(app, fn, a, res):
    """返回一句操作描述；None 表示这类请求太琐碎不记（轮询、读取等）"""
    a = a if isinstance(a, dict) else {}
    r = res if isinstance(res, dict) else {}

    def ev(eid):
        eid = str(eid or "")
        nm = ((app.spec.events.get(eid) or {}).get("name") or "") if app.spec else ""
        return f"「{nm}」{eid}" if nm else eid

    def renamed(sec):
        old, new = str(a.get("old_name") or a.get("old_id") or ""), str(a.get("name") or a.get("id") or "")
        return f"（由「{old}」改名）" if old and old != new else ""

    if fn == "api_event_save":
        n = len(((a.get("raw") or {}).get("fields") or {}))
        return f"保存点位规则 {ev(a.get('id'))}，{n} 个字段规则{renamed('events')}"
    if fn == "api_event_delete":
        return f"删除点位规则 {a.get('id')}"
    if fn == "api_case_save":
        v = a.get("value") or {}
        return f"保存测试用例「{a.get('name')}」，{len(v.get('expect') or [])} 个点位" + \
               (f"，绑定数据源 {len(v.get('sources') or [])} 个" if v.get("sources") else "") + renamed("cases")
    if fn in ("api_template_save", "api_source_save", "api_dict_save"):
        sec = {"api_template_save": "模板", "api_source_save": "数据源配置", "api_dict_save": "字典映射"}[fn]
        return f"保存{sec}「{a.get('name')}」{renamed(sec)}"
    if fn == "api_item_delete":
        return f"删除{_SEC_NAME.get(a.get('section'), a.get('section'))}「{a.get('name')}」"
    if fn == "api_settings_save":
        return "修改了高级设置（日志识别 / logcat 参数 / 校验选项）"
    if fn == "api_live_start":
        return "开始实时调试" + (f"，用例「{a.get('case')}」" if a.get("case") else "") + \
               (f"，设备 {a.get('serial')}" if a.get("serial") else "") + \
               (f"，自动抓接口 {len(r.get('api_targets') or [])} 个" if r.get("api_targets") else "") + "，去手机上操作吧"
    if fn == "api_live_stop":
        return f"停止实时调试，生成报告 {r.get('report')}：{_sum_text(r.get('summary'))}"
    if fn == "api_live_recheck":
        return f"用最新规则重新校验了 {r.get('count', 0)} 条上报"
    if fn == "api_live_clear":
        return "清空了实时调试记录"
    if fn == "api_live_decide":
        n = len(a.get("seqs") or [a.get("seq")])
        val = {"ok": "对", "bad": "不对"}.get(a.get("value"), "撤销")
        return f"人工确认字段 {a.get('field')} → {val}" + (f"（重复上报 {n} 条一起）" if n > 1 else "")
    if fn == "api_check_log":
        return f"离线校验日志，生成报告 {r.get('report')}：{_sum_text(r.get('summary'))}"
    if fn == "api_report_review":
        st = {"PASS": "通过", "FAIL": "有问题", "ASK": "待确认"}.get(a.get("status"), a.get("status"))
        f = a.get("field") or "整条"
        return f"改判报告 #{a.get('seq')} 的 {f} → {st}" + (f"（{a.get('note')}）" if a.get("note") else "")
    if fn == "api_report_delete":
        return f"删除校验报告 {a.get('name')}"
    if fn == "api_reports_clear":
        return "一键清空了全部校验报告"
    if fn == "api_ds_upload":
        return f"上传数据源文件 {r.get('file') or a.get('filename')}"
    if fn == "api_ds_paste":
        return f"粘贴 JSON 保存为数据源 {r.get('name') or a.get('name')}"
    if fn == "api_ds_delete":
        return f"删除数据源文件 {a.get('file')}"
    if fn == "api_ds_history_use":
        return f"数据源 {a.get('file')} 切回历史版本 {a.get('hist')}"
    if fn == "api_ds_history_delete":
        return f"删除数据源 {a.get('file')} 的历史版本 {a.get('hist')}"
    if fn == "api_ds_api_capture_start":
        return f"开始抓取接口 {a.get('endpoint') or ''}".rstrip()
    if fn == "api_ds_api_capture_stop":
        return "停止抓取接口" + (f"，共抓到 {len(r.get('captured') or [])} 份" if isinstance(r.get("captured"), list) else "")
    if fn == "api_ds_api_capture_extract":
        return "从粘贴的 OkHttp 日志里提取接口 JSON"
    if fn in ("api_pull", "api_pull_dbs"):
        return "从手机拉取数据库到数据源"
    if fn == "api_pull_apidump":
        return "从手机拉取接口缓存"
    if fn == "api_evdict_import":
        return f"导入埋点文档 {a.get('filename')}：解析 {r.get('parsed', 0)} 个，新增 {r.get('added', 0)} 个"
    if fn == "api_evdict_import_file":
        return f"导入 docs 里的埋点文档 {a.get('name')}：新增 {r.get('added', 0)} 个"
    if fn == "api_evdict_doc_remove":
        return f"从埋点字典移除文档 {a.get('name')}" + (f"，顺带删掉 {r.get('dropped', 0)} 个只来自它的点位" if r.get("dropped") else "")
    if fn == "api_evdict_delete":
        return "清空了埋点字典" if a.get("all") else f"从埋点字典删除点位 {a.get('id')}"
    if fn == "api_import_apply":
        return f"导入规则包 {a.get('filename') or ''}：{(r.get('msg') or '').replace('已导入：', '')}"
    if fn == "api_export":
        return "导出规则包" + ("（全部）" if not a.get("ids") else f"（{len(str(a.get('ids')).split(','))} 个点位）")
    if fn == "api_ai_prompt":
        return f"生成整组 AI 提示词：用例「{a.get('case')}」" if a.get("case") else f"生成 AI 提示词：{ev(a.get('id'))}"
    if fn == "api_infer":
        return f"从日志推断规则草稿 {len(r.get('drafts') or [])} 个"
    if fn == "api_infer_accept":
        return "采纳规则草稿写入点位规则"
    if fn == "api_spec_text_save":
        return f"直接编辑并保存了规则文件 {a.get('name')}"
    if fn == "api_reload":
        return "重新加载了规则文件"
    if fn == "api_db_query":
        return f"数据库浏览器查询 {a.get('db')}：返回 {len(r.get('rows') or [])} 行"
    if fn == "api_dict_tag":
        return "给字典映射打上点位标签"
    if fn == "api_server_log_clear":
        return None
    return None


ROUTES = {
    ("GET", "/api/state"): "api_state", ("GET", "/api/event"): "api_event", ("GET", "/api/report"): "api_report",
    ("GET", "/api/source_json"): "api_source_json", ("GET", "/api/db_schema"): "api_db_schema",
    ("GET", "/api/devices"): "api_devices", ("GET", "/api/live/poll"): "api_live_poll",
    ("GET", "/api/server_log"): "api_server_log", ("POST", "/api/server_log/clear"): "api_server_log_clear",
    ("POST", "/api/shutdown"): "api_shutdown",
    ("GET", "/api/db/tables"): "api_db_tables", ("POST", "/api/db/query"): "api_db_query",
    ("POST", "/api/event/save"): "api_event_save", ("POST", "/api/event/delete"): "api_event_delete",
    ("POST", "/api/dict/save"): "api_dict_save", ("POST", "/api/source/save"): "api_source_save",
    ("POST", "/api/case/save"): "api_case_save", ("POST", "/api/template/save"): "api_template_save",
    ("POST", "/api/item/delete"): "api_item_delete", ("POST", "/api/settings/save"): "api_settings_save",
    ("POST", "/api/parse"): "api_parse", ("POST", "/api/try"): "api_try", ("POST", "/api/sql"): "api_sql",
    ("POST", "/api/suggest"): "api_suggest", ("POST", "/api/suggest_sql"): "api_suggest_sql",
    ("POST", "/api/check_log"): "api_check_log", ("POST", "/api/infer"): "api_infer",
    ("POST", "/api/infer/accept"): "api_infer_accept", ("POST", "/api/live/start"): "api_live_start",
    ("POST", "/api/live/stop"): "api_live_stop", ("POST", "/api/live/recheck"): "api_live_recheck",
    ("POST", "/api/live/clear"): "api_live_clear", ("POST", "/api/pull"): "api_pull", ("POST", "/api/reload"): "api_reload",
    ("POST", "/api/dict/tag"): "api_dict_tag", ("GET", "/api/export"): "api_export",
    ("POST", "/api/live/scenario"): "api_live_scenario", ("POST", "/api/live/decide"): "api_live_decide",
    ("GET", "/api/ds/list"): "api_ds_list", ("POST", "/api/ds/upload"): "api_ds_upload", ("POST", "/api/ds/paste"): "api_ds_paste",
    ("POST", "/api/ds/delete"): "api_ds_delete",
    ("POST", "/api/ds/history/use"): "api_ds_history_use", ("POST", "/api/ds/history/delete"): "api_ds_history_delete",
    ("GET", "/api/ds/history/json"): "api_ds_history_json",
    ("POST", "/api/ds/api_capture/start"): "api_ds_api_capture_start", ("GET", "/api/ds/api_capture/poll"): "api_ds_api_capture_poll",
    ("POST", "/api/ds/api_capture/stop"): "api_ds_api_capture_stop", ("POST", "/api/ds/api_capture/extract"): "api_ds_api_capture_extract", ("POST", "/api/device/dbs"): "api_device_dbs", ("POST", "/api/pull/dbs"): "api_pull_dbs",
    ("POST", "/api/pull/apidump"): "api_pull_apidump", ("GET", "/api/evdict"): "api_evdict",
    ("POST", "/api/evdict/import"): "api_evdict_import", ("POST", "/api/evdict/delete"): "api_evdict_delete",
    ("POST", "/api/ai_prompt"): "api_ai_prompt", ("GET", "/api/spec_text"): "api_spec_text",
    ("POST", "/api/spec_text/save"): "api_spec_text_save", ("POST", "/api/evdict/samples"): "api_evdict_samples",
    ("POST", "/api/import/preview"): "api_import_preview", ("POST", "/api/import/apply"): "api_import_apply",
    ("POST", "/api/open_path"): "api_open_path",
    ("POST", "/api/report/delete"): "api_report_delete", ("POST", "/api/reports/clear"): "api_reports_clear",
    ("POST", "/api/report/review"): "api_report_review",
    ("POST", "/api/log/act"): "api_log_act", ("GET", "/api/data_info"): "api_data_info",
    ("POST", "/api/evdict/doc_remove"): "api_evdict_doc_remove", ("POST", "/api/evdict/import_file"): "api_evdict_import_file",
}

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".json": "application/json; charset=utf-8"}


_CLIENT_DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def make_handler(app):
    lock = threading.RLock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def handle(self):
            try:
                super().handle()
            except _CLIENT_DISCONNECT:
                pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except _CLIENT_DISCONNECT:
                pass

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method):
            u = urlparse(self.path)
            if u.path.startswith("/api/"):
                fn = ROUTES.get((method, u.path))
                if not fn:
                    return self._send(404, {"error": "no such api"})
                try:
                    if method == "POST":
                        n = int(self.headers.get("Content-Length") or 0)
                        arg = json.loads(self.rfile.read(n) or b"{}")
                    else:
                        arg = {k: v[0] for k, v in parse_qs(u.query).items()}
                    if fn in ("api_live_poll", "api_devices"):
                        res = getattr(app, fn)(arg)
                    else:
                        with lock:
                            res = getattr(app, fn)(arg)
                    if method == "POST" or fn == "api_export":
                        try:
                            txt = describe_action(app, fn, arg, res if not isinstance(res, FileResponse) else {})
                            if txt:
                                SERVER_LOG.emit("ACT", txt)
                        except Exception:
                            pass
                    if isinstance(res, FileResponse):
                        try:
                            self.send_response(200)
                            self.send_header("Content-Type", res.ctype)
                            self.send_header("Content-Disposition", f'attachment; filename="{res.filename}"')
                            self.send_header("Content-Length", str(len(res.data)))
                            self.end_headers()
                            self.wfile.write(res.data)
                        except _CLIENT_DISCONNECT:
                            pass
                        return
                    self._send(200, res)
                except _CLIENT_DISCONNECT:
                    return
                except ApiError as e:
                    try:
                        what = describe_action(app, fn, arg if isinstance(arg, dict) else {}, {})
                        if what:
                            SERVER_LOG.emit("WARN", f"{what.split('，')[0]} —— {e}")
                    except Exception:
                        pass
                    self._send(400, {"error": str(e)})
                except Exception as e:
                    tb = traceback.format_exc().rstrip()
                    SERVER_LOG.emit("ERROR", f"API 异常 {method} {u.path}: {type(e).__name__}: {e}\n{tb}")
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})
                return
            if u.path.startswith("/report_html/"):
                fn = os.path.basename(unquote(u.path))
                p = os.path.join(app.out, fn)
                jp = os.path.join(app.out, os.path.splitext(fn)[0] + ".json")
                if os.path.isfile(jp):
                    try:
                        with open(jp, encoding="utf-8") as f:
                            rep = json.load(f)
                        return self._send(200, tc.build_html(rep).encode("utf-8"), MIME[".html"])
                    except Exception:
                        pass
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        return self._send(200, f.read(), MIME[".html"])
                return self._send(404, b"not found", "text/plain")
            rel = "index.html" if u.path in ("", "/") else unquote(u.path).lstrip("/")
            p = os.path.realpath(os.path.join(WEB, rel))
            if not p.startswith(os.path.realpath(WEB)) or not os.path.isfile(p):
                return self._send(404, b"not found", "text/plain")
            with open(p, "rb") as f:
                self._send(200, f.read(), MIME.get(os.path.splitext(p)[1].lower(), "application/octet-stream"))

    return H


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # Windows 上 SO_REUSEADDR 会"抢"已被占用的端口，关掉才能正确检测冲突
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, _CLIENT_DISCONNECT):
            return
        super().handle_error(request, client_address)


def _find_running(spec_path, start_port):
    """已有埋点小岛在跑同一份规则文件 → 返回它的地址（双击 exe 第二次时直接打开浏览器，不重复启动）"""
    import urllib.request
    target = os.path.normcase(os.path.abspath(spec_path))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 只访问本机 127.0.0.1
    for port in range(start_port, start_port + 20):
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=0.4) as r:
                d = json.loads(r.read().decode("utf-8"))
            if os.path.normcase(os.path.abspath(str(d.get("spec_path") or ""))) == target:
                return f"http://127.0.0.1:{port}/"
        except Exception:
            continue
    return None


def _prepare_console():
    for s in (sys.stdout, sys.stderr):
        if s is not None:
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    if os.name == "nt" and sys.stdout is not None and not FROZEN:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("埋点小岛 —— 关闭这个窗口即退出")
            os.system("")   # 打开 Windows 终端的颜色支持
        except Exception:
            pass
    SERVER_LOG.install()


def _notify_fatal_error(msg):
    """无黑框（FROZEN）启动发生致命异常时：写入 server.log 并弹出原生提示框，绝不静默消失。"""
    log_path = SERVER_LOG.path or os.path.join(APP_DIR, DATA_DIRNAME, "reports", "server.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] [FATAL]\n{msg}\n")
    except Exception:
        pass
    if FROZEN and os.name == "nt":
        try:
            import ctypes
            body = f"{msg}\n\n详细日志已保存至：\n{log_path}"
            ctypes.windll.user32.MessageBoxW(0, body[:1600], "埋点小岛 · 启动遇到问题", 0x10)
        except Exception:
            pass


def _spec_is_empty(p):
    """规则文件不存在，或只是首次运行生成的空骨架（没有任何点位 / 用例 / 数据源）"""
    if not os.path.isfile(p):
        return True
    try:
        d = yload(p)
    except Exception:
        return False
    if d.get("include"):
        return False
    return not any(d.get(k) for k in ("events", "cases", "sources", "templates", "dicts"))


def _seed_from_exe(spec_path):
    """exe 里内置了开发者打包时的规则 / 用例 / 数据源 / 字典：数据目录还是空的时自动释放，已有文件绝不覆盖"""
    seed = os.path.join(RES, SEED_DIRNAME)
    if not FROZEN or not os.path.isdir(seed) or not _spec_is_empty(spec_path):
        return 0
    data_dir = os.path.dirname(os.path.abspath(spec_path))
    spec_name = os.path.basename(spec_path)
    n = 0
    for root, _, files in os.walk(seed):
        rel = os.path.relpath(root, seed)
        for fn in files:
            dst = os.path.join(data_dir, fn) if rel == "." else os.path.join(data_dir, rel, fn)
            if os.path.exists(dst) and not (rel == "." and fn == spec_name):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(root, fn), dst)
            n += 1
    with open(os.path.join(data_dir, ".seeded_from_exe"), "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    return n


def main():
    global HTTP_SERVER
    _prepare_console()
    default_spec = os.path.join(APP_DIR, DATA_DIRNAME, "trackcheck.yaml") if FROZEN else "trackcheck.yaml"
    ap = argparse.ArgumentParser(description="埋点小岛 —— trackcheck 本地可视化网站")
    ap.add_argument("--spec", default=default_spec, help="规则文件（不存在会自动创建）")
    ap.add_argument("--out", help="报告 / 会话日志目录，默认 规则文件同目录/reports")
    ap.add_argument("--port", type=int, default=8686)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    running = _find_running(a.spec, a.port)
    if running:
        print(f"埋点小岛已经在运行：{running}，直接帮你打开浏览器")
        if not a.no_browser:
            webbrowser.open(running)
        return
    first_run = not os.path.exists(a.spec)
    seeded = _seed_from_exe(a.spec) if os.path.abspath(a.spec) == os.path.abspath(default_spec) else 0
    if seeded:
        first_run = False
    app = App(a.spec, a.out)
    SERVER_LOG.bind_file(os.path.join(app.out, "server.log"))
    os.makedirs(app.spec.datasource_dir, exist_ok=True)
    srv = None
    for port in range(a.port, a.port + 20):  # 端口被占用就顺延
        try:
            srv = Server(("127.0.0.1", port), make_handler(app))
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit(f"端口 {a.port}~{a.port + 19} 都被占用了，请先在网页右上角「休息」退出旧实例或用 --port 指定端口")
    HTTP_SERVER = srv
    url = f"http://127.0.0.1:{port}/"
    print(tc.color("🌱 埋点小岛已开张: ", "green") + tc.color(url, "bold"))
    if seeded:
        print(f"   第一次运行：已从程序里释放内置的规则 / 用例 / 数据源 / 埋点字典（{seeded} 个文件），打开就能用。")
    if first_run:
        print("   第一次运行，已创建空白规则文件。去「导入 / 分享」页导入同事的规则包即可开始。")
    adb = tc.find_adb()
    print(f"   adb: {adb if adb != 'adb' else '没找到（装 Android Studio，或把 platform-tools 放到程序同目录）'}")
    print(f"   规则文件: {app.store.path}")
    print(f"   报告目录: {app.out}")
    print(f"   运行日志: {SERVER_LOG.path}")
    print("   提示: 可在网页右上角查看运行日志或点击「休息退出」关闭后台服务")
    SERVER_LOG.flush_partial()
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.live and app.live.running:
            app.live.cap.stop(0)
        SERVER_LOG.flush_partial()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            _notify_fatal_error(str(e))
        raise
    except Exception:
        tb = traceback.format_exc()
        try:
            sys.stderr.write(tb)
        except Exception:
            pass
        _notify_fatal_error(tb)
        if not FROZEN and sys.stdin is not None and getattr(sys.stdin, "isatty", lambda: False)():
            pass
        raise
