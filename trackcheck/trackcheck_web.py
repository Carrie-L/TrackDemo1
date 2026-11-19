#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
埋点小岛 —— trackcheck 的本地可视化网站

    python trackcheck_web.py --spec samples/trackcheck.yaml
    python trackcheck_web.py --spec trackcheck.yaml --port 8765 --no-browser

只监听 127.0.0.1，仅依赖 Python 标准库 + PyYAML。
"""
import argparse
import contextlib
import copy
import glob
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import traceback
import webbrowser
from collections import Counter, OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import yaml

import trackcheck as tc

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

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


# ----------------------------------------------------------------------------
# 实时调试会话
# ----------------------------------------------------------------------------
class LiveSession:
    def __init__(self, app, serial, clear, case, only):
        self.app, self.serial, self.case = app, serial or None, case or None
        self.only = {str(x) for x in (only or []) if str(x).strip()}
        self.lock = threading.Lock()
        self.results, self.running, self.report = [], True, None
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        self.raw = os.path.join(app.out, f"session_{self.stamp}.log")
        self.ck = tc.Checker(app.spec, app.sources)
        self.cap = tc.Capture(app.spec, self.serial, self.raw, self.on_event)
        self.cap.start(clear=clear)
        self.started = time.time()

    def on_event(self, ev):
        if self.only and ev.id not in self.only:
            return
        r = self.ck.check(ev)
        with self.lock:
            self.results.append(r)

    def events(self):
        evs = list(self.cap.events)
        return [e for e in evs if not self.only or e.id in self.only]

    def recheck(self):
        ck = tc.Checker(self.app.spec, self.app.sources)
        res = [ck.check(e) for e in self.events()]
        with self.lock:
            self.ck, self.results = ck, res

    def stop(self, pull=False):
        self.cap.stop(linger=0.3)
        self.running = False
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
# API
# ----------------------------------------------------------------------------
class App:
    def __init__(self, spec_path, out_dir=None):
        self.store = SpecStore(spec_path)
        self.out = os.path.abspath(out_dir or os.path.join(os.path.dirname(self.store.path), "reports"))
        os.makedirs(self.out, exist_ok=True)
        self.live = None
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
        files = sorted(set(map(os.path.abspath, files)), key=os.path.getmtime, reverse=True)
        return [{"path": p, "name": os.path.basename(p), **file_info(p)} for p in files[:40]]

    def source_status(self):
        out = {}
        for name, cfg in self.spec.sources.items():
            t = cfg.get("type", "json_file")
            info = {"type": t}
            try:
                if t in ("json_file", "sqlite"):
                    info.update(file_info(self.sources._file(cfg["path"])))
                else:
                    self.sources.json(name)
                    info.update(exists=True, path=f"{cfg.get('db')} · SQL")
            except Exception as e:
                info.update(exists=False, error=str(e))
            out[name] = info
        return out

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
                           "extends": (raw or {}).get("extends"), "file": os.path.relpath(p, os.path.dirname(self.store.path)),
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
                    "raw": self.live.raw, "case": self.live.case, "only": sorted(self.live.only)}
        return {
            "spec_path": self.store.path, "spec_error": self.spec_error, "out": self.out,
            "files": [os.path.relpath(p, os.path.dirname(self.store.path)) for p in self.store.files],
            "events": events,
            "templates": {k: v for k, (v, _) in self.store.items("templates").items()},
            "dicts": dicts, "dict_meta": dict_meta, "catalog": catalog,
            "sources": {k: v for k, (v, _) in self.store.items("sources").items()},
            "source_status": self.source_status(),
            "cases": {k: v for k, (v, _) in self.store.items("cases").items()},
            "device": self.store.main.get("device") or {},
            "options": self.store.main.get("options") or {},
            "log": self.store.main.get("log") or {},
            "reports": reps, "sessions": self.sessions(), "live": live,
        }

    def api_event(self, q):
        eid = str(q.get("id", ""))
        raw, p = self.store.get("events", eid)
        if raw is None:
            raise ApiError(f"没有点位 {eid}")
        return {"id": eid, "raw": raw, "resolved": self.spec.events.get(eid, {}),
                "file": os.path.relpath(p, os.path.dirname(self.store.path))}

    def api_report(self, q):
        name = os.path.basename(q.get("name", ""))
        p = os.path.join(self.out, name + ".json")
        if not os.path.exists(p):
            raise ApiError("报告不存在")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def api_source_json(self, q):
        try:
            return {"data": self.sources.json(q["name"])}
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
            p = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True, timeout=8, errors="replace")
        except FileNotFoundError:
            return {"devices": [], "error": "找不到 adb（把 platform-tools 加到 PATH）"}
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
            return {"running": False, "results": [], "count": 0}
        since = int(q.get("since", 0))
        with self.live.lock:
            res = self.live.results[since:]
            n = len(self.live.results)
        return {"running": self.live.running, "results": res, "count": n, "since": since,
                "report": self.live.report, "raw": self.live.raw}

    # ---- POST：规则 ----
    def api_event_save(self, b):
        eid = str(b.get("id", "")).strip()
        if not eid:
            raise ApiError("点位ID不能为空")
        raw = b.get("raw") or {}
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
        return self._put("sources", b)

    def api_case_save(self, b):
        return self._put("cases", b)

    def api_template_save(self, b):
        return self._put("templates", b)

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
        res = tc.check_all(self.spec, evs, verbose=False)
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
        # 规则里用到、但我这边和规则包里都没有的数据源
        used = set()
        for sec in ("events", "templates"):
            for v in (obj.get(sec) or {}).values():
                v = v or {}
                ctx = v.get("context") or {}
                for c in ctx.values():
                    if isinstance(c, dict) and c.get("from") and c["from"] not in ctx:
                        used.add(str(c["from"]))
                for r in (v.get("fields") or {}).values():
                    if isinstance(r, dict):
                        if r.get("db"):
                            used.add(str(r["db"]))
                        m = re.match(r"^([A-Za-z_]\w*):", str(r.get("from") or ""))
                        if m:
                            used.add(m.group(1))
        ctx_names = set()
        for sec in ("events", "templates"):
            for v in (obj.get(sec) or {}).values():
                ctx_names |= set(((v or {}).get("context") or {}).keys())
        have = set(self.spec.sources) | set(map(str, (obj.get("sources") or {}).keys()))
        missing = sorted(used - have - ctx_names)
        return {"kind": "bundle", "meta": obj.get("meta") or {}, "items": items, "missing_sources": missing,
                "catalog_new": len([k for k in cat if k not in self.spec.catalog]), "catalog_total": len(cat)}

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
        self.reload()
        if new_events:
            self._autotag(new_events)
            self.reload()
        names = {"events": "点位", "dicts": "字典", "templates": "模板", "cases": "用例", "sources": "数据源"}
        msg = [f"{names[k]} {n} 个" for k, n in done.items() if n] + msg
        return {"ok": True, "msg": "已导入：" + ("，".join(msg) if msg else "没有选中任何内容")}

    # ---- POST：实时 / 设备 ----
    def api_live_start(self, b):
        if self.live and self.live.running:
            raise ApiError("已经在抓取中")
        try:
            self.live = LiveSession(self, b.get("serial"), b.get("clear", True), b.get("case"), b.get("only"))
        except FileNotFoundError:
            raise ApiError("找不到 adb，请把 Android SDK platform-tools 加到 PATH")
        return {"ok": True, "raw": self.live.raw}

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

    def api_reload(self, b):
        self.reload()
        return {"ok": True, "error": self.spec_error}


ROUTES = {
    ("GET", "/api/state"): "api_state", ("GET", "/api/event"): "api_event", ("GET", "/api/report"): "api_report",
    ("GET", "/api/source_json"): "api_source_json", ("GET", "/api/db_schema"): "api_db_schema",
    ("GET", "/api/devices"): "api_devices", ("GET", "/api/live/poll"): "api_live_poll",
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
    ("POST", "/api/import/preview"): "api_import_preview", ("POST", "/api/import/apply"): "api_import_apply",
}

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".json": "application/json; charset=utf-8"}


def make_handler(app):
    lock = threading.RLock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

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
                    if isinstance(res, FileResponse):
                        self.send_response(200)
                        self.send_header("Content-Type", res.ctype)
                        self.send_header("Content-Disposition", f'attachment; filename="{res.filename}"')
                        self.send_header("Content-Length", str(len(res.data)))
                        self.end_headers()
                        self.wfile.write(res.data)
                        return
                    self._send(200, res)
                except ApiError as e:
                    self._send(400, {"error": str(e)})
                except Exception as e:
                    traceback.print_exc()
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})
                return
            if u.path.startswith("/report_html/"):
                p = os.path.join(app.out, os.path.basename(unquote(u.path)))
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


def main():
    ap = argparse.ArgumentParser(description="埋点小岛 —— trackcheck 本地可视化网站")
    ap.add_argument("--spec", default="trackcheck.yaml", help="规则文件（不存在会自动创建）")
    ap.add_argument("--out", help="报告 / 会话日志目录，默认 规则文件同目录/reports")
    ap.add_argument("--port", type=int, default=8686)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    app = App(a.spec, a.out)
    srv = None
    for port in range(a.port, a.port + 20):  # 端口被占用就顺延
        try:
            srv = Server(("127.0.0.1", port), make_handler(app))
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit(f"端口 {a.port}~{a.port + 19} 都被占用了，用 --port 指定一个")
    url = f"http://127.0.0.1:{port}/"
    print(tc.color("🌱 埋点小岛已开张: ", "green") + tc.color(url, "bold"))
    print(f"   规则文件: {app.store.path}\n   报告目录: {app.out}\n   Ctrl+C 退出")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.live and app.live.running:
            app.live.cap.stop(0)


if __name__ == "__main__":
    main()
