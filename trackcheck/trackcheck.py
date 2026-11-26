#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trackcheck —— Android 埋点调试 / 自动校验工具

  check  离线校验一份 logcat 日志
  live   实时抓 adb logcat 校验，Ctrl+C 结束后输出报告
  run    执行自动化脚本（Maestro / adb / UIAutomator ...）的同时抓日志，结束后校验 + 覆盖率
  infer  根据 日志 + 接口JSON + DB 反推每个字段的数据来源，生成规则草稿
  pull   按规则文件 device 配置，从手机拉取 db / 接口 dump

依赖：Python 3.8+，PyYAML（pip install pyyaml）
"""
import argparse
import copy
import datetime as dt
import glob
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, OrderedDict, defaultdict

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("缺少 PyYAML，请先执行: pip install pyyaml")

if os.name == "nt" and sys.stdout is not None:
    os.system("")  # 打开 Windows 终端 ANSI 颜色
for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def popen_kw():
    """Windows 无黑框打包（--windowed）时禁止子进程（如 adb）弹出黑色控制台闪烁。"""
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if os.name == "nt" else {}

_C = dict(red="\033[31m", green="\033[32m", yellow="\033[33m", cyan="\033[36m",
          gray="\033[90m", bold="\033[1m", reset="\033[0m")


def color(s, c):
    return f"{_C[c]}{s}{_C['reset']}"


DEFAULT_PATTERN = r"Event:\s*id:\s*(?P<id>\d+)\s*data:\s*(?P<data>.*?)\s*$"
TS_RE = re.compile(r"^(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})")
NULL_EQUALS_EMPTY = True


class SourceError(Exception):
    pass


class LocateError(Exception):
    pass


class RuleError(Exception):
    pass


# ----------------------------------------------------------------------------
# 值的归一化 / 比较
# ----------------------------------------------------------------------------
def norm(v):
    if v is None:
        return "" if NULL_EQUALS_EMPTY else "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v).strip()


_NUM_RE = re.compile(r"-?\d+(\.\d+)?")


def eq_val(a, b):
    na, nb = norm(a), norm(b)
    if na == nb:
        return True
    if ("." in na or "." in nb) and _NUM_RE.fullmatch(na) and _NUM_RE.fullmatch(nb):
        return float(na) == float(nb)
    return False


def disp(v, limit=120):
    if v is None:
        return "null"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = norm(v)
    return s if len(s) <= limit else s[:limit] + "…"


def uniq(seq):
    seen, out = set(), []
    for x in seq:
        k = norm(x)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


# ----------------------------------------------------------------------------
# 日志解析
# ----------------------------------------------------------------------------
class Event:
    __slots__ = ("seq", "id", "cls", "fields", "raw", "ts", "tsv", "line_no")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def split_top_level(body):
    parts, depth, buf = [], 0, []
    for ch in body:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


_KV_RE = re.compile(r"^\s*([A-Za-z_][\w.\-]*)\s*=(.*)$", re.S)
_BARE_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*$")


def _kv_value(s):
    s = s.strip()
    return None if s == "null" else s


def parse_kv_body(body):
    """解析 Kotlin data class toString / Java Map toString 的 k=v, k=v 内容"""
    fields = OrderedDict()
    parts = split_top_level(body)
    last = None
    for i, p in enumerate(parts):
        if not p.strip():
            continue
        m = _KV_RE.match(p)
        if m:
            last = m.group(1)
            fields[last] = _kv_value(m.group(2))
            continue
        b = _BARE_RE.match(p)
        if b and (i == len(parts) - 1 or last is None):
            last = b.group(1)
            fields[last] = ""  # 形如 "subChannelId" 没有 "="，视为空值
            continue
        if last is not None:  # 值里本身带逗号
            fields[last] = (fields[last] or "") + "," + p
    return fields


def flatten(obj, prefix=""):
    out = OrderedDict()
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)) and v:
                out.update(flatten(v, key))
            else:
                out[key] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}[{i}]"
            if isinstance(v, (dict, list)) and v:
                out.update(flatten(v, key))
            else:
                out[key] = v
    else:
        out[prefix or "value"] = obj
    return out


def _to_field_str(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return norm(v)


def parse_data(s):
    s = s.strip()
    if s[:1] in "{[":
        try:
            obj = json.loads(s)
            return None, OrderedDict((k, _to_field_str(v)) for k, v in flatten(obj).items())
        except Exception:
            if s.startswith("{") and s.endswith("}"):
                return None, parse_kv_body(s[1:-1])  # HashMap.toString()
    m = re.match(r"^([\w.$]+)\s*\((.*)\)\s*$", s, re.S)
    if m:
        return m.group(1), parse_kv_body(m.group(2))
    return None, parse_kv_body(s)


def parse_ts(ts):
    if not ts:
        return None
    try:
        return dt.datetime.strptime(f"2000-{ts}", "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except ValueError:
        return None


class LogParser:
    def __init__(self, pattern=None):
        self.re = re.compile(pattern or DEFAULT_PATTERN)
        self.seq = 0

    def feed(self, line, line_no=None):
        line = line.rstrip("\r\n")
        m = self.re.search(line)
        if not m:
            return None
        self.seq += 1
        tm = TS_RE.match(line)
        ts = tm.group("ts") if tm else ""
        cls, fields = parse_data(m.group("data"))
        return Event(seq=self.seq, id=m.group("id").strip(), cls=cls, fields=fields, raw=line,
                     ts=ts, tsv=parse_ts(ts), line_no=line_no)


def read_log(path, pattern=None):
    p = LogParser(pattern)
    events = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            ev = p.feed(line, i)
            if ev:
                events.append(ev)
    return events


# ----------------------------------------------------------------------------
# JSON 路径
# ----------------------------------------------------------------------------
_TOK_RE = re.compile(r"\[(\*|-?\d+)\]|([^.\[\]]+)")


def tokenize(path):
    toks = []
    for m in _TOK_RE.finditer(path or ""):
        if m.group(1) is not None:
            toks.append("*" if m.group(1) == "*" else int(m.group(1)))
        else:
            toks.append(m.group(2))
    return toks


def maybe_json(v):
    """接口里经常把 JSON 再塞成字符串，遇到就自动解开"""
    if isinstance(v, str):
        s = v.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                pass
    return v


def _join(p, t):
    if isinstance(t, int):
        return f"{p}[{t}]"
    if not p or p.endswith(":"):
        return f"{p}{t}"
    return f"{p}.{t}"


def resolve(obj, path, base=""):
    cur = [(obj, base)]
    for t in tokenize(path) if isinstance(path, str) else path:
        nxt = []
        for v, p in cur:
            v = maybe_json(v)
            if t == "*":
                if isinstance(v, list):
                    nxt += [(x, _join(p, i)) for i, x in enumerate(v)]
                elif isinstance(v, dict):
                    nxt += [(x, _join(p, k)) for k, x in v.items()]
            elif isinstance(t, int):
                if isinstance(v, list) and -len(v) <= t < len(v):
                    nxt.append((v[t], _join(p, t)))
            elif isinstance(v, dict) and t in v:
                nxt.append((v[t], _join(p, t)))
        cur = nxt
    return cur


_SUB_RE = re.compile(r"\{([A-Za-z_]\w*)\}")


def subst(s, fields):
    if not isinstance(s, str):
        return s
    return _SUB_RE.sub(lambda m: norm(fields[m.group(1)]) if m.group(1) in fields else m.group(0), s)


_SAFE = {k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
         for k in ("len", "int", "float", "str", "bool", "min", "max", "abs", "any", "all", "sorted",
                   "round", "list", "dict", "set", "tuple", "isinstance", "enumerate", "range", "sum", "zip")}
_SAFE.update({"True": True, "False": False, "None": None, "re": re, "json": json})


def eval_expr(expr, env):
    return eval(expr, {"__builtins__": {}}, env)  # noqa: S307  本地调试工具，规则文件由自己维护


# ----------------------------------------------------------------------------
# 规则文件
# ----------------------------------------------------------------------------
def split_dict(v):
    """字典两种写法：{1: 首页, ...} 或 {desc, events: [关联点位], items: {1: 首页}} -> (items, meta)"""
    if isinstance(v, dict) and "items" in v and set(v) <= {"items", "events", "desc"}:
        return dict(v.get("items") or {}), {"events": [str(x) for x in (v.get("events") or [])], "desc": str(v.get("desc") or "")}
    return dict(v or {}), {"events": [], "desc": ""}


_ID_KEYS = {"eventid", "event_id", "id", "code", "eventcode", "event_code", "点位", "点位id", "点位编号", "事件id", "埋点id"}
_NAME_KEYS = {"eventname", "event_name", "name", "title", "label", "desc", "点位名称", "名称", "事件名称", "埋点名称"}


def parse_catalog(obj):
    """点位清单 JSON -> OrderedDict(id -> 名称)。支持 [{eventId, eventName}]、{id: 名称}、{events: [...]} 等常见格式"""
    out = OrderedDict()
    if isinstance(obj, dict):
        for k in ("events", "data", "list", "items", "catalog"):
            if isinstance(obj.get(k), (list, dict)) and len(obj) <= 3:
                return parse_catalog(obj[k])
        for k, v in obj.items():
            if isinstance(v, dict):
                out[str(k).strip()] = str(next((v[x] for x in v if str(x).lower() in _NAME_KEYS), ""))
            elif not isinstance(v, list):
                out[str(k).strip()] = "" if v is None else str(v)
        return out
    for it in obj if isinstance(obj, list) else []:
        if isinstance(it, dict):
            idk = next((k for k in it if str(k).lower() in _ID_KEYS), None) or \
                next((k for k, v in it.items() if re.fullmatch(r"\d{4,}", str(v).strip())), None)
            if idk is None:
                continue
            nk = next((k for k in it if str(k).lower() in _NAME_KEYS and k != idk), None) or \
                next((k for k, v in it.items() if k != idk and isinstance(v, str)), None)
            out[str(it[idk]).strip()] = str(it.get(nk) or "") if nk else ""
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            out[str(it[0]).strip()] = str(it[1])
    return out


def is_sqlite_file(p):
    try:
        with open(p, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def scan_datasources(d):
    """数据源目录里的文件 -> 数据源配置。*.json = 接口 JSON；SQLite 文件（.db / 无扩展名均可）= 数据库"""
    out = OrderedDict()
    if not os.path.isdir(d):
        return out
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if not os.path.isfile(p) or f.startswith(".") or re.search(r"-(wal|shm|journal)$", f):
            continue
        stem, ext = os.path.splitext(f)
        name = re.sub(r"[^\w]", "_", stem) or "src"
        if ext.lower() == ".json":
            out[name] = {"type": "json_file", "path": p, "auto": True}
        elif ext.lower() in (".db", ".sqlite", ".sqlite3") or is_sqlite_file(p):
            out[name] = {"type": "sqlite", "path": p, "auto": True}
    return out


# ----------------------------------------------------------------------------
# 埋点字典：把公司埋点文档（Excel 导出的 Markdown / CSV）解析成 点位ID -> 参数说明
# ----------------------------------------------------------------------------
def load_event_dict(p):
    if p and os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("entries", {})
            d.setdefault("docs", [])
            d.setdefault("columns", [])
            return d
        except Exception:
            pass
    return {"entries": {}, "docs": [], "columns": []}


def _md_cells(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    parts = re.split(r"(?<!\\)\|", s)
    return [re.sub(r"<br\s*/?>", "\n", p.strip().replace("\\|", "|")) for p in parts]


def _is_md_sep(line):
    s = line.strip()
    return "-" in s and set(s) <= set("|-: ") and s.count("-") >= 3


def parse_md_tables(text):
    """Markdown 里的所有表格 -> [(表头, [行...])]"""
    lines = (text or "").splitlines()
    tables, i = [], 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and _is_md_sep(lines[i + 1]):
            headers = [h or f"列{n + 1}" for n, h in enumerate(_md_cells(lines[i]))]
            rows, i = [], i + 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                c = _md_cells(lines[i])
                rows.append((c + [""] * len(headers))[:len(headers)])
                i += 1
            tables.append((headers, rows))
            continue
        i += 1
    return tables


def parse_doc_tables(text, filename=""):
    import csv
    import io as _io
    fn = filename.lower()
    if fn.endswith((".csv", ".tsv")):
        rows = list(csv.reader(_io.StringIO(text), delimiter="\t" if fn.endswith(".tsv") else ","))
        return [(rows[0], [(r + [""] * len(rows[0]))[:len(rows[0])] for r in rows[1:]])] if rows else []
    return parse_md_tables(text)


_MOD_HEADERS = ("模块", "所属模块", "功能模块", "业务模块", "页面模块", "模块名称", "模块名", "一级模块", "二级模块", "module", "Module")


def entry_module(e):
    """提取点位所属模块（优先 module 字段，其次扫描 columns / rows 中的「模块」列）"""
    if not isinstance(e, dict):
        return ""
    m = str(e.get("module") or "").strip()
    if m:
        return m
    cols = e.get("columns") or {}
    for k in _MOD_HEADERS:
        if cols.get(k):
            return str(cols[k]).strip()
    for k, v in cols.items():
        if v and re.search(r"(模块|module)", str(k), re.I) and not re.search(r"(id|编号|编码)", str(k), re.I):
            return str(v).strip()
    for r in (e.get("rows") or []):
        if not isinstance(r, dict):
            continue
        for k, v in r.items():
            if v and (k in _MOD_HEADERS or (re.search(r"(模块|module)", str(k), re.I) and not re.search(r"(id|编号|编码)", str(k), re.I))):
                return str(v).strip()
    return ""


def dict_modules(store):
    """按文档出现顺序汇总埋点字典里的全部模块 -> [{name, count, ids, events}]"""
    groups = OrderedDict()
    for eid, e in (store or {}).get("entries", {}).items():
        mod = entry_module(e)
        if not mod:
            continue
        groups.setdefault(mod, []).append(str(eid))
    ents = (store or {}).get("entries", {})
    return [{"name": m, "count": len(ids), "ids": ids,
             "events": [{"id": i, "name": (ents.get(i) or {}).get("name") or ""} for i in ids]}
            for m, ids in groups.items()]


def doc_to_event_entries(text, filename=""):
    """文档 -> OrderedDict(点位ID -> {id, name, module, columns, rows})。合并单元格导出后 ID / 模块为空的行自动沿用上一行。"""
    out = OrderedDict()
    for headers, rows in parse_doc_tables(text, filename):
        if not rows:
            continue
        idc = next((i for i, h in enumerate(headers) if h.strip() in ("点位ID", "点位id", "埋点ID", "事件ID")), None)
        if idc is None:
            idc = next((i for i, h in enumerate(headers) if re.search(r"(id|ID|Id|编号|编码)", h) and
                        re.search(r"(点位|埋点|事件|event|Event|^ID$|^id$)", h)), None)
        if idc is None:
            idc = next((i for i, h in enumerate(headers) if h.strip().lower() in _ID_KEYS), None)
        if idc is None:  # 按内容猜：大多数值是长数字的那一列
            best = (0, None)
            for i in range(len(headers)):
                n = sum(1 for r in rows if re.fullmatch(r"\s*\d{5,}\s*", r[i] or ""))
                best = max(best, (n, i))
            idc = best[1] if best[0] >= max(1, len(rows) // 3) else None
        if idc is None:
            continue
        namec = next((i for i, h in enumerate(headers) if i != idc and h.strip() in ("事件名称", "点位名称", "埋点名称")), None)
        if namec is None:
            namec = next((i for i, h in enumerate(headers) if i != idc and re.search(r"(事件名|点位名|埋点名|名称|name|Name)", h)), None)
        modc = next((i for i, h in enumerate(headers) if i not in (idc, namec) and h.strip() in _MOD_HEADERS), None)
        if modc is None:
            modc = next((i for i, h in enumerate(headers) if i not in (idc, namec)
                         and re.search(r"(模块|module)", h, re.I) and not re.search(r"(id|编号|编码)", h, re.I)), None)
        last = None
        last_mod = ""
        for r in rows:
            row = OrderedDict((h, (r[i] or "").strip()) for i, h in enumerate(headers))
            if modc is not None:
                mv = row[headers[modc]]
                if mv:
                    last_mod = mv
                elif last_mod:
                    row[headers[modc]] = last_mod   # Excel 合并「模块」单元格：沿用当前模块
            m = re.search(r"\d{5,}", row[headers[idc]].replace(" ", ""))
            if m:
                rid = m.group(0)
            elif not row[headers[idc]] and last:
                rid = last                          # 合并单元格：沿用上一行的点位
            else:
                continue
            mod_val = row.get(headers[modc], "") if modc is not None else ""
            e = out.get(rid)
            if e is None:
                e = out[rid] = {"id": rid, "name": row.get(headers[namec], "") if namec is not None else "",
                                "module": mod_val, "columns": OrderedDict(row), "rows": []}
            for k, v in row.items():
                if v and not e["columns"].get(k):
                    e["columns"][k] = v
            if not e["name"] and namec is not None:
                e["name"] = row.get(headers[namec], "")
            if not e.get("module") and mod_val:
                e["module"] = mod_val
            if any(v for k, v in row.items() if k != headers[idc]):
                e["rows"].append(row)
            last = rid
    return out


def merge_event_dict(store, entries, doc_name, overwrite=False):
    """增量合并：点位ID 已存在则跳过（只记录它也出现在这份文档里），overwrite=True 时用新文档覆盖"""
    added, skipped, updated = [], [], []
    ents = store.setdefault("entries", {})
    cols = store.setdefault("columns", [])
    for rid, e in entries.items():
        for c in e["columns"]:
            if c not in cols:
                cols.append(c)
        cur = ents.get(rid)
        mod_val = e.get("module") or entry_module(e)
        if cur is None or overwrite:
            docs = (cur or {}).get("docs", [])
            item = {"id": rid, "name": e["name"], "columns": dict(e["columns"]), "rows": [dict(r) for r in e["rows"]],
                    "docs": list(dict.fromkeys(docs + [doc_name])), "added_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            if mod_val or (cur or {}).get("module"):
                item["module"] = mod_val or (cur or {}).get("module")
            ents[rid] = item
            (updated if cur is not None else added).append(rid)
        else:
            if mod_val and not cur.get("module"):
                cur["module"] = mod_val
            if doc_name not in cur.setdefault("docs", []):
                cur["docs"].append(doc_name)
            skipped.append(rid)
    if doc_name not in [x.get("name") for x in store.setdefault("docs", [])]:
        store["docs"].append({"name": doc_name, "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"), "count": len(entries)})
    return added, skipped, updated


# ----------------------------------------------------------------------------
# 从手机拉 App 数据库（debug 包用 run-as，root 测试机用 su）
# ----------------------------------------------------------------------------
def _shell_err(b):
    head = b[:300].decode("utf-8", "replace").lower()
    return len(b) < 400 and any(x in head for x in ("no such file", "permission denied", "not found", "unknown package",
                                                     "is not debuggable", "inaccessible", "cat:", "su:", "run-as:"))


def list_app_dbs(adb, pkg):
    for cmd in (adb + ["shell", f"su -c 'ls /data/data/{pkg}/databases'"], adb + ["shell", "run-as", pkg, "ls", "databases"],
                adb + ["shell", f"su 0 ls /data/data/{pkg}/databases"], adb + ["shell", f"ls /data/data/{pkg}/databases"]):
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=20, **popen_kw())
        except (OSError, subprocess.TimeoutExpired):
            continue
        out = p.stdout.decode("utf-8", "replace")
        if p.returncode == 0 and out.strip() and not _shell_err(p.stdout):
            return [x.strip() for x in out.split() if x.strip() and not re.search(r"-(wal|shm|journal)$", x.strip())]
    raise RuntimeError(f"读不到 /data/data/{pkg}/databases：确认包名正确、手机已 root 或是 debug 包")


def pull_app_db(adb, pkg, db, local):
    """拉 /data/data/<包名>/databases/<db>（连同 -wal/-shm）到本地 local"""
    os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
    remote = f"/data/data/{pkg}/databases/{db}"
    for suffix in ("", "-wal", "-shm"):
        data = None
        for cmd in (adb + ["exec-out", f"su -c 'cat {remote}{suffix}'"], adb + ["exec-out", "run-as", pkg, "cat", f"databases/{db}{suffix}"],
                    adb + ["exec-out", f"su 0 cat {remote}{suffix}"], adb + ["exec-out", f"cat {remote}{suffix}"]):
            try:
                p = subprocess.run(cmd, capture_output=True, timeout=120, **popen_kw())
            except (OSError, subprocess.TimeoutExpired):
                continue
            if p.returncode == 0 and p.stdout and not _shell_err(p.stdout):
                if suffix == "" and not p.stdout.startswith(b"SQLite format 3"):
                    continue
                data = p.stdout
                break
        lf = local + suffix
        if data is None:
            if suffix == "":
                raise RuntimeError(f"拉取失败：{remote}（确认 db 名、手机已 root 或是 debug 包）")
            if os.path.exists(lf):
                os.remove(lf)   # 手机上没有 wal 了，删掉本地旧的
            continue
        with open(lf, "wb") as f:
            f.write(data)
    return local


def merge_spec(a, b):
    out = dict(a)
    for k, v in b.items():
        if k in ("context", "fields", "dicts") and isinstance(v, dict):
            m = OrderedDict(out.get(k) or {})
            m.update(v)
            out[k] = m
        elif k == "asserts":
            out[k] = list(out.get(k) or []) + list(v or [])
        elif k == "api":
            if v:
                out[k] = copy.deepcopy(v)
        else:
            out[k] = v
    return out


class Spec:
    SECTIONS = ("dicts", "sources", "templates", "events", "cases")

    def __init__(self, path):
        global NULL_EQUALS_EMPTY
        self.path = os.path.abspath(path)
        self.base = os.path.dirname(self.path)
        d = self._load(self.path)
        for inc in d.get("include") or []:
            files = sorted(glob.glob(os.path.join(self.base, inc)))
            if not files:
                print(color(f"[warn] include 没匹配到文件: {inc}", "yellow"))
            for f in files:
                sub = self._load(f)
                for key in self.SECTIONS:
                    d[key] = dict(d.get(key) or {})
                    d[key].update(sub.get(key) or {})
        self.raw = d
        self.options = {"null_equals_empty": True, "strict_fields": False, **(d.get("options") or {})}
        # 数据源目录：放进去的 .json / .db 文件自动成为数据源，名字 = 文件名（不含扩展名）
        self.datasource_dir = self.abspath(str(self.options.get("datasource_dir") or "datasources"))
        auto_sources = scan_datasources(self.datasource_dir)
        NULL_EQUALS_EMPTY = bool(self.options["null_equals_empty"])
        self.dicts, self.dict_meta = {}, {}
        for k, v in (d.get("dicts") or {}).items():
            items, meta = split_dict(v)
            self.dicts[str(k)] = OrderedDict((norm(kk), vv) for kk, vv in items.items())
            self.dict_meta[str(k)] = meta
        self.catalog = OrderedDict()   # 点位清单：id -> 名称（只有名字、还没配字段的点位）
        if d.get("catalog_file"):
            cp = self.abspath(str(d["catalog_file"]))
            if os.path.exists(cp):
                with open(cp, encoding="utf-8-sig") as f:
                    self.catalog.update(parse_catalog(json.load(f)))
        self.catalog.update(parse_catalog(d.get("catalog") or {}))
        # 埋点字典（从公司埋点文档导入）：点位ID -> {name, columns, rows, docs}
        self.event_dict_path = self.abspath(str(d.get("event_dict_file") or "event_dict.json"))
        self.event_dict = load_event_dict(self.event_dict_path)
        for k, e in self.event_dict.get("entries", {}).items():
            if e.get("name") and not self.catalog.get(k):
                self.catalog[k] = e["name"]
        self.sources = {**auto_sources, **(d.get("sources") or {})}
        self.templates = {str(k): v for k, v in (d.get("templates") or {}).items()}
        self._raw_events = {str(k): (v or {}) for k, v in (d.get("events") or {}).items()}
        self.events = {k: self._resolve(v, {k}) for k, v in self._raw_events.items()}
        self.cases = {str(k): v for k, v in (d.get("cases") or {}).items()}
        self.device = d.get("device") or {}
        log = d.get("log") or {}
        self.pattern = log.get("pattern") or DEFAULT_PATTERN
        self.logcat_args = [str(x) for x in (log.get("logcat_args") or [])]

    @staticmethod
    def _load(path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _resolve(self, ev, seen):
        parents = ev.get("extends")
        if not parents:
            return ev
        if isinstance(parents, (str, int)):
            parents = [parents]
        merged = {}
        for p in map(str, parents):
            if p in seen:
                raise RuleError(f"extends 循环引用: {p}")
            src = self.templates.get(p) or self._raw_events.get(p)
            if src is None:
                raise RuleError(f"extends 找不到模板/事件: {p}")
            merged = merge_spec(merged, self._resolve(src, seen | {p}))
        return merge_spec(merged, {k: v for k, v in ev.items() if k != "extends"})

    def abspath(self, p):
        return p if os.path.isabs(p) else os.path.join(self.base, p)


# ----------------------------------------------------------------------------
# 数据源：接口 JSON 文件 / SQLite / SQLite 里缓存的 JSON
# ----------------------------------------------------------------------------
class Sources:
    def __init__(self, spec):
        self.spec = spec
        self._json = {}
        self._db = {}
        self._merge_meta = {}

    def has(self, name):
        return name in self.spec.sources

    def merge_info(self, name):
        return self._merge_meta.get(name)

    @staticmethod
    def _str_list(v):
        if not v:
            return []
        if isinstance(v, str):
            return [x.strip() for x in re.split(r"[,，\s]+", v) if x.strip()]
        return [str(x).strip() for x in v if str(x).strip()]

    @staticmethod
    def _is_empty_val(v):
        return v is None or v == "" or v == [] or v == {}

    @staticmethod
    def _nav_path(root, path):
        """按点分路径定位 (parent, last_key, node)；空路径返回 (None, None, root)"""
        if not path:
            return None, None, root
        parts = [p for p in str(path).split(".") if p]
        cur, parent, last_k = root, None, None
        for p in parts:
            parent, last_k = cur, p
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None, None, None
        return parent, last_k, cur

    def _merge_json(self, name, cfg, seen):
        primaries = self._str_list(cfg.get("primary") or cfg.get("sources"))
        fb_name = str(cfg.get("fallback") or "").strip()
        if not primaries and not fb_name:
            raise SourceError(f"{name}: merged_json 至少要配置 primary 或 fallback 数据源")
        p_used, p_data, p_errs = None, None, []
        for pn in primaries:
            if not self.has(pn):
                p_errs.append(f"{pn} 未定义")
                continue
            try:
                p_data = self.json(pn, seen)
                p_used = pn
                break
            except SourceError as e:
                p_errs.append(str(e))
        fb_data, fb_err = None, None
        if fb_name:
            if not self.has(fb_name):
                fb_err = f"{fb_name} 未定义"
            else:
                try:
                    fb_data = self.json(fb_name, seen)
                except SourceError as e:
                    fb_err = str(e)
        if p_data is None:
            if fb_data is not None:
                ck = ("fb_only", fb_name, id(fb_data))
                c = self._json.get(name)
                if c and c[0] == ck:
                    return c[1]
                out = copy.deepcopy(fb_data)
                self._merge_meta[name] = {
                    "primary_used": None, "fallback": fb_name, "mode": "fallback_all",
                    "primary_errors": p_errs, "replaced_keys": [], "appended_keys": [], "kept_keys": [],
                }
                self._json[name] = (ck, out)
                return out
            detail = "；".join(p_errs + ([f"兜底 {fb_err}"] if fb_err else []))
            raise SourceError(f"{name}: 主源和兜底源均不可用（{detail}）")
        if fb_data is None:
            ck = ("p_only", p_used, id(p_data), fb_err)
            c = self._json.get(name)
            if c and c[0] == ck:
                return c[1]
            out = copy.deepcopy(p_data)
            self._merge_meta[name] = {
                "primary_used": p_used, "fallback": fb_name or None, "mode": "primary_only",
                "fallback_error": fb_err, "replaced_keys": [], "appended_keys": [], "kept_keys": [],
            }
            self._json[name] = (ck, out)
            return out

        ck = ("merged", p_used, id(p_data), fb_name, id(fb_data))
        c = self._json.get(name)
        if c and c[0] == ck:
            return c[1]

        out = copy.deepcopy(p_data)
        path = str(cfg.get("path") or "").strip()
        fb_path = str(cfg.get("fallback_path") or path).strip()
        key_field = str(cfg.get("key") or "key").strip()
        empty_field = str(cfg.get("empty_field") if cfg.get("empty_field") is not None else "res").strip()
        fb_keys_cfg = self._str_list(cfg.get("fallback_keys"))

        p_parent, p_last, p_target = self._nav_path(out, path)
        _, _, fb_target = self._nav_path(fb_data, fb_path)
        if fb_target is None and fb_path:
            # 兜底 JSON 可能直接就是数组或对象（没包外层 data.myPage）
            if isinstance(fb_data, (list, dict)):
                fb_target = fb_data

        replaced, appended, kept = [], [], []
        if isinstance(p_target, list) and isinstance(fb_target, (list, dict)):
            fb_map = OrderedDict()
            if isinstance(fb_target, list):
                for item in fb_target:
                    if isinstance(item, dict) and item.get(key_field) is not None:
                        fb_map[str(item[key_field])] = item
            else:
                for k, v in fb_target.items():
                    ks = str(k)
                    if isinstance(v, dict):
                        fb_map[ks] = {key_field: ks, **v} if key_field not in v else v
                    elif isinstance(v, list) and empty_field:
                        fb_map[ks] = {key_field: ks, empty_field: v}
            allowed = set(fb_keys_cfg) if fb_keys_cfg else set(fb_map.keys())
            seen_keys = set()
            merged_list = []
            for item in p_target:
                k = str(item[key_field]) if isinstance(item, dict) and item.get(key_field) is not None else None
                if k is not None:
                    seen_keys.add(k)
                if k is not None and k in allowed and k in fb_map:
                    need_fb = bool(empty_field) and self._is_empty_val(item.get(empty_field))
                    if need_fb:
                        fb_item = copy.deepcopy(fb_map[k])
                        for mk, mv in item.items():
                            if mk != empty_field and mk not in fb_item and not self._is_empty_val(mv):
                                fb_item[mk] = copy.deepcopy(mv)
                        merged_list.append(fb_item)
                        replaced.append(k)
                    else:
                        merged_list.append(item)
                        kept.append(k)
                else:
                    merged_list.append(item)
            for k in (fb_keys_cfg or list(fb_map.keys())):
                if k not in seen_keys and k in fb_map:
                    merged_list.append(copy.deepcopy(fb_map[k]))
                    appended.append(k)
            if p_parent is None:
                out = merged_list
            else:
                p_parent[p_last] = merged_list
        elif isinstance(p_target, dict) and isinstance(fb_target, dict):
            allowed = set(fb_keys_cfg) if fb_keys_cfg else set(fb_target.keys())
            for k in (fb_keys_cfg or list(fb_target.keys())):
                if k not in allowed or k not in fb_target:
                    continue
                if k not in p_target:
                    p_target[k] = copy.deepcopy(fb_target[k])
                    appended.append(k)
                else:
                    cur_v = p_target[k]
                    is_empty = self._is_empty_val(cur_v) or (
                        bool(empty_field) and isinstance(cur_v, dict) and self._is_empty_val(cur_v.get(empty_field))
                    )
                    if is_empty:
                        p_target[k] = copy.deepcopy(fb_target[k])
                        replaced.append(k)
                    else:
                        kept.append(k)

        self._merge_meta[name] = {
            "primary_used": p_used, "fallback": fb_name, "mode": "merged",
            "replaced_keys": replaced, "appended_keys": appended, "kept_keys": kept,
        }
        self._json[name] = (ck, out)
        return out

    def _file(self, pattern):
        p = self.spec.abspath(pattern)
        if any(c in p for c in "*?["):
            files = glob.glob(p)
            if not files:
                raise SourceError(f"没有匹配的文件: {p}")
            return max(files, key=os.path.getmtime)
        if not os.path.exists(p):
            raise SourceError(f"文件不存在: {p}")
        return p

    def json(self, name, _seen=None):
        seen = set(_seen or ())
        if name in seen:
            raise SourceError(f"数据源存在循环引用: {name}")
        seen.add(name)
        cfg = self.spec.sources.get(name)
        if not cfg:
            raise SourceError(f"未定义的数据源: {name}")
        t = cfg.get("type", "json_file")
        if t == "merged_json":
            return self._merge_json(name, cfg, seen)
        if t == "json_file":
            path = self._file(cfg["path"])
            key = (path, os.path.getmtime(path))
            c = self._json.get(name)
            if c and c[0] == key:
                return c[1]
            try:
                with open(path, encoding="utf-8-sig") as f:
                    data = json.load(f)
            except Exception as e:
                raise SourceError(f"{name}: JSON 解析失败 {path}: {e}")
        elif t == "sqlite_json":
            conn, mt = self._conn(cfg["db"])
            key = (cfg["sql"], mt)
            c = self._json.get(name)
            if c and c[0] == key:
                return c[1]
            row = conn.execute(cfg["sql"]).fetchone()
            if not row or row[0] is None:
                raise SourceError(f"{name}: SQL 没有查到 JSON: {cfg['sql']}")
            try:
                data = json.loads(row[0])
            except Exception as e:
                raise SourceError(f"{name}: DB 中的内容不是合法 JSON: {e}")
        else:
            raise SourceError(f"数据源 {name} 类型为 {t}，不能当 JSON 用（sqlite 源请在字段里写 sql）")
        if cfg.get("root"):
            hits = resolve(data, cfg["root"])
            if not hits:
                raise SourceError(f"{name}: root 路径不存在 {cfg['root']}")
            data = hits[0][0]
        self._json[name] = (key, data)
        return data

    def _conn(self, name):
        cfg = self.spec.sources.get(name)
        if not cfg:
            raise SourceError(f"未定义的数据源: {name}")
        path = self._file(cfg["path"])
        mt = max(os.path.getmtime(p) for p in (path, path + "-wal") if os.path.exists(p))
        c = self._db.get(name)
        if c and c[1] == mt:
            return c[0], mt
        if c:
            c[0].close()
        conn = sqlite3.connect(path, check_same_thread=False)
        self._db[name] = (conn, mt)
        return conn, mt

    def db(self, name=None):
        if name is None:
            name = next((k for k, v in self.spec.sources.items() if v.get("type") == "sqlite"), None)
            if name is None:
                raise SourceError("规则里用了 sql，但没有定义 type: sqlite 的数据源")
        return self._conn(name)[0]


# ----------------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------------
_RANK = {"FAIL": 4, "WARN": 3, "ASK": 2, "PASS": 1}


def _num(x):
    try:
        return float(norm(x))
    except (TypeError, ValueError):
        return None


def match_expected(actual, exp, rule, alt=None):
    """期望值比较：精确 / 容差 tolerance / 方向 compare / 事件前后快照区间。返回 (是否通过, 说明)"""
    tol = rule.get("tolerance")
    cmp = rule.get("compare", "eq")
    a = _num(actual)
    for x in exp:
        n = _num(x)
        if cmp == "eq":
            if eq_val(actual, x):
                return True, ""
            if tol is not None and a is not None and n is not None and abs(a - n) <= float(tol):
                return True, f"与期望 {disp(x)} 相差在 ±{tol} 内"
        elif a is not None and n is not None:
            if {"lte": a <= n, "gte": a >= n, "lt": a < n, "gt": a > n}.get(cmp, False):
                return True, ""
    if alt:
        if any(eq_val(actual, x) for x in alt):
            return True, "与事件发生前的 DB 快照一致"
        nums = [v for v in (_num(x) for x in list(exp) + list(alt)) if v is not None]
        t = float(tol or 0)
        if a is not None and nums and min(nums) - t <= a <= max(nums) + t:
            lo, hi = min(nums), max(nums)
            f = lambda v: str(int(v)) if v.is_integer() else str(v)
            return True, f"落在事件前后的快照区间 [{f(lo)}, {f(hi)}] 内"
    return False, ""


def worst(statuses, default="PASS"):
    best = default
    for s in statuses:
        if _RANK.get(s, 0) > _RANK.get(best, 0):
            best = s
    return best


def type_ok(v, t):
    t = str(t).lower()
    try:
        if t == "int":
            return re.fullmatch(r"-?\d+", v) is not None
        if t in ("number", "float"):
            float(v)
            return True
        if t == "bool":
            return v.lower() in ("true", "false", "0", "1")
        if t == "url":
            return re.match(r"^[a-zA-Z][\w+.-]*://", v) is not None
        if t == "json":
            json.loads(v)
            return True
        if t in ("ts13", "timestamp_ms"):
            return re.fullmatch(r"\d{13}", v) is not None
        if t in ("ts10", "timestamp"):
            return re.fullmatch(r"\d{10}", v) is not None
    except Exception:
        return False
    return True


def is_stub_event(es):
    """只有点位ID和事件名称、还没配置任何字段/断言/定位的占位条目"""
    if not es:
        return True
    return not any(es.get(k) for k in ("fields", "context", "asserts", "dedupe", "extends", "strict_fields"))


def norm_rule(rule):
    if rule is None:
        return {}
    if isinstance(rule, dict):
        return rule
    if isinstance(rule, list):
        return {"enum": rule}
    if isinstance(rule, str):
        return {"from": rule}
    return {"eq": rule}


class Checker:
    def __init__(self, spec, sources, alt_sources=None, scenario=None):
        self.spec = spec
        self.sources = sources
        self.alt_sources = alt_sources      # 事件发生前的 DB 快照（实时调试时自动拉取）
        self.scenario = dict(scenario or {})  # 本次操作场景：{字段: 期望值}，给 ask 字段用
        self.seen = {}
        self.last_vals = {}                 # trend 用：同一点位同一字段上一次的值
        self.cur_dicts = spec.dicts
        self._scn, self._dec = {}, {}

    # ---- 上下文定位 ----
    def build_context(self, es, ev):
        ctx, meta, err = {}, {}, {}
        for name, c in (es.get("context") or {}).items():
            c = c or {}
            raw_from = c.get("from", "")
            frms = [str(x).strip() for x in raw_from if str(x).strip()] if isinstance(raw_from, list) else [str(raw_from)]
            path = subst(c.get("path", ""), ev.fields)
            where = c.get("where") or {}
            wdesc = ", ".join(f"{k}={subst(v, ev.fields)}" for k, v in where.items())

            def _match_where(node):
                for k, want in where.items():
                    wants = want if isinstance(want, list) else [want]
                    wants = [subst(w, ev.fields) for w in wants]
                    if not any(eq_val(v, w) for v, _ in resolve(node, k) for w in wants):
                        return False
                return True

            found = None
            last_err = ""
            for frm in frms:
                try:
                    if frm in ctx:
                        base, base_path = ctx[frm], meta[frm]["path"]
                    elif frm in err:
                        last_err = f"依赖的上下文 {frm} 定位失败"
                        continue
                    elif self.sources.has(frm):
                        base, base_path = self.sources.json(frm), f"{frm}:"
                    else:
                        last_err = f"from 指向未知的数据源/上下文: {frm!r}"
                        continue
                    cands = resolve(base, path, base_path) if path else [(base, base_path)]
                    if where:
                        cands = [x for x in cands if _match_where(x[0])]
                    if cands:
                        found = cands
                        break
                    last_err = f"在 {frm} 的 {path or '<根>'} 中找不到" + (f" {wdesc}" if wdesc else "")
                except SourceError as e:
                    last_err = f"数据源错误: {e}"
            if not found:
                err[name] = last_err or f"上下文 {name} 定位失败"
                continue
            pick = c.get("pick", "first")
            v, p = found[-1] if pick == "last" else found[0]
            idx = re.findall(r"\[(\d+)\]", p)
            ctx[name] = v
            meta[name] = {"path": p, "index": int(idx[-1]) if idx else None, "count": len(found)}
        return ctx, meta, err

    def env(self, ev, ctx, meta):
        e = dict(_SAFE)
        for k, v in ev.fields.items():
            if k.isidentifier():
                e[k] = "" if v is None else v
        e.update(ctx)
        e.update(f=dict(ev.fields), ctx=ctx, idx={n: m["index"] for n, m in meta.items()},
                 dicts=self.cur_dicts, event_id=ev.id,
                 get=lambda o, p: (resolve(o, p) or [(None, "")])[0][0])
        return e

    def resolve_from(self, spec_from, ev, ctx, meta, cerr):
        s = subst(str(spec_from), ev.fields)
        m = re.match(r"^([A-Za-z_]\w*):(.*)$", s)
        if m and self.sources.has(m.group(1)):
            base, base_path, rest = self.sources.json(m.group(1)), f"{m.group(1)}:", m.group(2)
        else:
            hm = re.match(r"^([A-Za-z_]\w*)(.*)$", s)
            if not hm:
                raise RuleError(f"from 写法不对: {s}")
            name, rest = hm.group(1), hm.group(2).lstrip(".")
            if name in cerr:
                raise LocateError(f"上下文 {name} 定位失败：{cerr[name]}")
            if name not in ctx:
                raise RuleError(f"from 引用了未定义的上下文: {name}")
            base, base_path = ctx[name], meta[name]["path"]
        hits = resolve(base, rest, base_path) if rest else [(base, base_path)]
        if not hits:
            raise LocateError(f"接口数据中没有路径 {s}")
        return hits

    def enum_values(self, e):
        if isinstance(e, str):
            d = self.cur_dicts.get(e)
            if d is None:
                raise RuleError(f"未定义的字典: {e}")
            return list(d.keys()), d, e
        if isinstance(e, dict):
            d = OrderedDict((norm(k), v) for k, v in e.items())
            return list(d.keys()), d, "inline"
        return [norm(x) for x in e], None, None

    def apply_map(self, vals, m):
        chain = m if isinstance(m, list) else [m]
        tables = []
        for t in chain:
            if isinstance(t, dict):
                tables.append(("inline", {norm(k): v for k, v in t.items()}))
            elif t in self.cur_dicts:
                tables.append((t, self.cur_dicts[t]))
            else:
                raise RuleError(f"未定义的字典: {t}")
        mapped, unmapped, notes = [], [], []
        for v in vals:
            cur, trail, ok = v, [disp(v)], True
            for name, tb in tables:
                k = norm(cur)
                if k not in tb:
                    unmapped.append(f'"{k}" 不在字典 {name} 中')
                    ok = False
                    break
                cur = tb[k]
                trail.append(f"{name}→{disp(cur)}")
            if ok:
                mapped.append(cur)
                notes.append(" ⇒ ".join(trail))
        return mapped, unmapped, notes

    # ---- 单字段 ----
    def check_field(self, fname, rule, ev, env, ctx, meta, cerr):
        rule = norm_rule(rule)
        present = fname in ev.fields
        raw = ev.fields.get(fname)
        r = {"field": fname, "desc": str(rule.get("desc", "") or ""),
             "actual": disp(raw, 400) if present else "<缺失>",
             "expected": "", "source": "", "status": "PASS", "reason": ""}
        sev = "WARN" if rule.get("severity") == "warn" else "FAIL"

        def done(status, reason=""):
            r["status"], r["reason"] = status, reason
            return r

        if rule.get("ignore"):
            return done("SKIP", "已忽略")
        if "when" in rule:
            try:
                if not eval_expr(str(rule["when"]), env):
                    return done("SKIP", f"条件不满足: {rule['when']}")
            except Exception as e:
                return done(sev, f"when 表达式错误: {e}")
        if not present:
            return done(sev, "事件中缺少该字段") if rule.get("required", True) else done("SKIP", "可选字段，未上报")

        actual = norm(raw)
        problems, label, alt = [], "", None
        try:
            if actual == "":
                if rule.get("not_empty"):
                    problems.append("值为空")
            else:
                t = rule.get("type")
                if t and not type_ok(actual, t):
                    problems.append(f"类型应为 {t}")
                if rule.get("regex") and not re.fullmatch(subst(str(rule["regex"]), ev.fields), actual):
                    problems.append(f"不匹配正则 {rule['regex']}")
                for key, getter, what in (("range", lambda: float(actual), "数值"), ("len", lambda: len(actual), "长度")):
                    if rule.get(key):
                        lo, hi = rule[key]
                        try:
                            x = getter()
                            if (lo is not None and x < lo) or (hi is not None and x > hi):
                                problems.append(f"{what}应在 [{lo}, {hi}]")
                        except ValueError:
                            problems.append(f"{what}无法解析")

            if "enum" in rule and not (actual == "" and rule.get("allow_empty")):
                allowed, labels, dname = self.enum_values(rule["enum"])
                if any(eq_val(actual, a) for a in allowed):
                    if labels:
                        label = f"{actual} = {labels.get(actual, '')}"
                else:
                    shown = ", ".join(f"{k}={v}" if labels else k for k, v in
                                      (labels.items() if labels else [(a, None) for a in allowed]))
                    problems.append(f"不在文档枚举内" + (f"（{dname}）" if dname and dname != "inline" else ""))
                    r["expected"] = shown if len(shown) < 200 else shown[:200] + "…"
                r["source"] = "文档枚举" + (f" {dname}" if dname and dname != "inline" else "")

            exp = None
            if "from" in rule:
                hits = self.resolve_from(rule["from"], ev, ctx, meta, cerr)
                exp = [v for v, _ in hits]
                r["source"] = ", ".join(p for _, p in hits[:3]) + (f" 等{len(hits)}处" if len(hits) > 3 else "")
            elif "sql" in rule:
                conn = self.sources.db(rule.get("db"))
                sql = subst(str(rule["sql"]), ev.fields)
                params = {k: norm(v) for k, v in ev.fields.items()}
                rows = conn.execute(sql, params).fetchall()
                r["source"] = f"SQL: {sql}"
                if self.alt_sources is not None:
                    try:
                        alt = [row[0] for row in self.alt_sources.db(rule.get("db")).execute(sql, params).fetchall()]
                    except Exception:
                        alt = None
                if not rows and not alt:
                    problems.append("SQL 没有查到数据（数据不存在 / db 没重新拉取？）")
                else:
                    exp = [row[0] for row in rows]
            elif "expr" in rule:
                v = eval_expr(str(rule["expr"]), env)
                exp = list(v) if isinstance(v, (list, tuple, set)) else [v]
                r["source"] = f"expr: {rule['expr']}"
            elif "eq" in rule:
                exp = [subst(rule["eq"], ev.fields)]
                r["source"] = "固定值"

            if exp is not None and "map" in rule:
                mapped, unmapped, notes = self.apply_map(exp, rule["map"])
                if notes:
                    r["source"] += f"（{notes[0]}）"
                if not mapped:
                    problems.append("接口值无法映射：" + "；".join(unmapped[:3]) + "（文档字典缺这个值？）")
                exp = mapped if mapped else None

            if exp or alt:
                ex = uniq(exp or [])
                cmp_txt = {"lte": "≤ ", "gte": "≥ ", "lt": "< ", "gt": "> "}.get(rule.get("compare"), "")
                r["expected"] = cmp_txt + " | ".join(disp(x) for x in ex[:5]) + (" …" if len(ex) > 5 else "")
                if rule.get("tolerance") is not None:
                    r["expected"] += f"（±{rule['tolerance']}）"
                if alt and uniq(alt) != ex:
                    r["expected"] += f"；事件前快照 {' | '.join(disp(x) for x in uniq(alt)[:3])}"
                ok, note = match_expected(raw, ex, rule, alt)
                if ok:
                    label = note or label
                else:
                    problems.append({"lte": "上报值应 ≤ 期望值", "gte": "上报值应 ≥ 期望值", "lt": "上报值应 < 期望值",
                                     "gt": "上报值应 > 期望值"}.get(rule.get("compare"), "值不一致"))

            if rule.get("trend") and actual != "":
                key = (ev.id, fname)
                prev = self.last_vals.get(key)
                try:
                    a, p = float(actual), (float(prev[1]) if prev else None)
                    if p is not None:
                        bad = {"up": a < p, "down": a > p, "same": a != p, "strict_up": a <= p}.get(rule["trend"], False)
                        if bad:
                            problems.append(f"和上一次（#{prev[0]} 上报 {prev[1]}）相比趋势不对，应为 "
                                            + {"up": "不减少", "down": "不增加", "same": "不变", "strict_up": "增加"}.get(rule["trend"], rule["trend"]))
                    self.last_vals[key] = (ev.seq, actual)
                except ValueError:
                    problems.append("趋势检查需要数字")

            if "assert" in rule:
                env2 = dict(env, value="" if raw is None else raw)
                if not eval_expr(str(rule["assert"]), env2):
                    problems.append(str(rule.get("msg") or f"断言失败: {rule['assert']}"))

            cw = self._scn.get(f"{ev.id}.{fname}")
            if not rule.get("ask") and cw not in (None, "", []) and not problems:
                wants = cw if isinstance(cw, list) else [cw]
                meaning = label.split(" = ", 1)[1] if " = " in label else ""
                r["source"] = (r["source"] + " · " if r["source"] else "") + "用例期望"
                if not r["expected"]:
                    r["expected"] = " / ".join(str(w) for w in wants)
                if any(eq_val(raw, w) for w in wants):
                    label = "与用例期望一致" + (f"（{meaning}）" if meaning else "")
                else:
                    problems.append(f"用例期望 {' / '.join(str(w) for w in wants)}，上报的是 {actual}")

            if rule.get("ask") and not problems:
                dec = self._dec.get(fname)
                want = self._scn.get(f"{ev.id}.{fname}", self._scn.get(fname))
                wants = want if isinstance(want, list) else [want]
                meaning = label.split(" = ", 1)[1] if " = " in label else ""
                if dec in ("ok", "bad"):
                    if dec == "bad":
                        problems.append("人工确认：值不对")
                    else:
                        label = "人工确认 ✓" + (f"（{meaning}）" if meaning else "")
                elif want not in (None, "", []):
                    shown = " / ".join(str(w) for w in wants)
                    r["expected"] = shown
                    r["source"] = "用例期望" if f"{ev.id}.{fname}" in self._scn else "本次操作场景"
                    if any(eq_val(raw, w) for w in wants):
                        label = f"与{r['source']}一致" + (f"（{meaning}）" if meaning else "")
                    else:
                        problems.append(f"{r['source']}是 {shown}，上报的是 {actual}")
                else:
                    return done("ASK", f"需要你确认：上报 {actual}" + (f"（{meaning}）" if meaning else "") + "，和你刚才的操作一致吗？")
        except LocateError as e:
            problems.append(f"无法取得期望值：{e}")
        except (RuleError, SourceError) as e:
            problems.append(f"规则/数据源错误：{e}")
        except sqlite3.Error as e:
            problems.append(f"SQL 错误：{e}")
        except Exception as e:
            problems.append(f"表达式错误：{type(e).__name__}: {e}")

        if problems:
            return done(sev, "；".join(problems))
        return done("PASS", label)

    # ---- 单事件 ----
    def check(self, ev, scenario=None, decisions=None):
        es = self.spec.events.get(ev.id)
        self._scn = dict(self.scenario)
        self._scn.update(scenario or {})
        self._dec = dict(decisions or {})
        res = {"seq": ev.seq, "ts": ev.ts, "id": ev.id, "cls": ev.cls or "", "raw": ev.raw,
               "line_no": ev.line_no, "fields": [], "errors": [], "ctx": {}, "data": dict(ev.fields)}
        if es is None or is_stub_event(es):
            res.update(name=str((es or {}).get("name") or self.spec.catalog.get(ev.id) or ev.cls or ""), status="UNKNOWN")
            return res
        res["name"] = str(es.get("name") or self.spec.catalog.get(ev.id) or ev.cls or "")
        # 点位自己的字典（events.<id>.dicts）优先于全局共享字典
        self.cur_dicts = dict(self.spec.dicts)
        for dn, dv in (es.get("dicts") or {}).items():
            self.cur_dicts[str(dn)] = OrderedDict((norm(kk), vv) for kk, vv in split_dict(dv)[0].items())
        ctx, meta, cerr = self.build_context(es, ev)
        for n, m in meta.items():
            res["ctx"][n] = {"ok": True, "info": m["path"] + (f"（共 {m['count']} 个匹配，取第1个）" if m["count"] > 1 else "")}
        for n, msg in cerr.items():
            res["ctx"][n] = {"ok": False, "info": msg}
        env = self.env(ev, ctx, meta)
        fspec = es.get("fields") or {}
        for fname, rule in fspec.items():
            res["fields"].append(self.check_field(str(fname), rule, ev, env, ctx, meta, cerr))
        strict = bool(es.get("strict_fields", self.spec.options.get("strict_fields")))
        for k, v in ev.fields.items():
            if k not in fspec:
                res["fields"].append({"field": k, "desc": "", "actual": disp(v, 400), "expected": "", "source": "",
                                      "status": "FAIL" if strict else "INFO", "reason": "规则中未定义该字段"})
        for a in es.get("asserts") or []:
            a = {"expr": a} if isinstance(a, str) else a
            try:
                ok, msg = bool(eval_expr(str(a["expr"]), env)), a.get("msg") or f"断言失败: {a['expr']}"
            except Exception as e:
                ok, msg = False, f"断言表达式错误: {a['expr']} -> {e}"
            if not ok:
                res["errors"].append({"status": "WARN" if a.get("severity") == "warn" else "FAIL", "msg": msg})
        dd = es.get("dedupe")
        if dd:
            keys = dd if isinstance(dd, list) else [dd]
            k = (ev.id,) + tuple(norm(ev.fields.get(x)) for x in keys)
            win = es.get("dedupe_window")
            prev = self.seen.get(k)
            if prev is not None:
                gap = ev.tsv - prev.tsv if (ev.tsv is not None and prev.tsv is not None) else None
                if win is None or (gap is not None and gap <= float(win)):
                    res["errors"].append({"status": "WARN", "msg": f"疑似重复上报：与 #{prev.seq} 的 {'/'.join(keys)} 相同"
                                          + (f"，间隔 {gap:.2f}s" if gap is not None else "")})
            self.seen[k] = ev
        res["status"] = worst([f["status"] for f in res["fields"]] + [e["status"] for e in res["errors"]])
        return res


# ----------------------------------------------------------------------------
# 覆盖率 & 报告
# ----------------------------------------------------------------------------
def coverage(spec, results, case_name=None):
    rows = []
    if case_name:
        case = spec.cases.get(case_name)
        if case is None:
            raise SystemExit(f"规则文件中没有用例: {case_name}（可选: {', '.join(spec.cases) or '无'}）")
        for exp in case.get("expect") or []:
            exp = {"id": exp} if not isinstance(exp, dict) else exp
            eid = str(exp["id"])
            where = exp.get("where") or {}
            n = sum(1 for r in results if r["id"] == eid and
                    all(eq_val(r["data"].get(k), v) for k, v in where.items()))
            lo, hi = int(exp.get("min", 1)), exp.get("max")
            ok = n >= lo and (hi is None or n <= int(hi))
            name = (spec.events.get(eid) or {}).get("name", "")
            cond = f"次数 ≥ {lo}" + (f" 且 ≤ {hi}" if hi is not None else "") + \
                   (f"，条件 {', '.join(f'{k}={v}' for k, v in where.items())}" if where else "")
            rows.append({"id": eid, "name": name, "count": n, "expect": cond, "status": "PASS" if ok else "FAIL"})
    else:
        cnt = Counter(r["id"] for r in results)
        for eid, es in spec.events.items():
            if is_stub_event(es):
                continue
            rows.append({"id": eid, "name": es.get("name", ""), "count": cnt.get(eid, 0), "expect": "",
                         "status": "PASS" if cnt.get(eid) else "MISS"})
    return rows


def case_values(spec, case_name):
    """用例里给点位字段填的期望值 -> 场景 {点位ID.字段: 值或候选值列表}"""
    out = {}
    case = spec.cases.get(case_name) if case_name else None
    for exp in (case or {}).get("expect") or []:
        if isinstance(exp, dict):
            for k, v in (exp.get("values") or {}).items():
                if v not in (None, "", []):
                    out[f"{exp['id']}.{k}"] = v
    return out


def build_report(spec, results, case_name=None, meta=None):
    cov = coverage(spec, results, case_name)
    st = Counter(r["status"] for r in results)
    hot = defaultdict(lambda: {"count": 0, "reason": "", "name": ""})
    for r in results:
        for f in r["fields"]:
            if f["status"] in ("FAIL", "WARN"):
                h = hot[(r["id"], f["field"])]
                h["count"] += 1
                h["reason"] = h["reason"] or f["reason"]
                h["name"] = r.get("name", "")
    hot_rows = sorted(({"id": k[0], "field": k[1], **v} for k, v in hot.items()), key=lambda x: -x["count"])
    cov_fail = sum(1 for c in cov if c["status"] == "FAIL")
    return {
        "meta": {"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "spec": spec.path,
                 "case": case_name, **(meta or {})},
        "summary": {"total": len(results), "pass": st.get("PASS", 0), "warn": st.get("WARN", 0),
                    "fail": st.get("FAIL", 0), "unknown": st.get("UNKNOWN", 0), "ask": st.get("ASK", 0), "coverage_fail": cov_fail,
                    "ok": st.get("FAIL", 0) == 0 and cov_fail == 0},
        "coverage": cov,
        "hot_fields": hot_rows,
        "results": results,
    }


_HTML_CSS = """
*{box-sizing:border-box}body{font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#222}
header{background:#1f2937;color:#fff;padding:16px 24px}header h1{margin:0;font-size:20px}header .m{color:#9ca3af;font-size:12px;margin-top:4px}
main{padding:16px 24px;max-width:1500px;margin:auto}.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.card{background:#fff;border-radius:8px;padding:12px 18px;min-width:120px;box-shadow:0 1px 2px #0001}.card b{font-size:24px;display:block}
.PASS{color:#15803d}.FAIL{color:#b91c1c}.WARN{color:#b45309}.UNKNOWN,.MISS,.INFO,.SKIP{color:#6b7280}
section{background:#fff;border-radius:8px;padding:12px 16px;margin-bottom:16px;box-shadow:0 1px 2px #0001}h2{font-size:16px;margin:4px 0 10px}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #eee;padding:5px 8px;text-align:left;vertical-align:top;word-break:break-all}
th{background:#fafafa;font-weight:600;white-space:nowrap}tr.r-FAIL td{background:#fde8e8}tr.r-WARN td{background:#fff5d6}tr.r-SKIP td,tr.r-INFO td{color:#888}
.badge{display:inline-block;border-radius:4px;padding:0 6px;font-size:12px;font-weight:600;color:#fff}.b-PASS{background:#16a34a}.b-FAIL{background:#dc2626}
.b-WARN{background:#d97706}.b-ASK{background:#7c3aed}.ASK{color:#7c3aed}tr.r-ASK td{background:#f3edff}.ev-ASK{border-left:4px solid #7c3aed!important}.b-UNKNOWN,.b-MISS,.b-INFO,.b-SKIP{background:#9ca3af}
details.ev{border:1px solid #e5e7eb;border-radius:6px;margin:8px 0;background:#fff}details.ev>summary{cursor:pointer;padding:8px 12px;list-style:none}
details.ev>summary::-webkit-details-marker{display:none}details.ev[open]>summary{border-bottom:1px solid #eee}.evb{padding:8px 12px}
.ev-FAIL{border-left:4px solid #dc2626!important}.ev-WARN{border-left:4px solid #d97706!important}.ev-PASS{border-left:4px solid #16a34a!important}
.mono{font-family:Consolas,Menlo,monospace;font-size:12px}pre{background:#f3f4f6;padding:8px;white-space:pre-wrap;word-break:break-all;margin:6px 0}
.tool{position:sticky;top:0;background:#f5f6f8;padding:8px 0;z-index:2;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.tool label{display:inline-flex;align-items:center;gap:5px;cursor:pointer;user-select:none;font-weight:600;color:#444}
.tool input[type=text]{padding:6px 10px;width:280px;border:1px solid #d5dde5;border-radius:8px;background:#fff}
.ctx{font-size:12px;color:#555}.ctx .bad{color:#b91c1c}
.err{color:#b91c1c;margin:4px 0}.err.WARN{color:#b45309}.err.PASS,.err.INFO{color:#15803d}
"""

_HTML_JS = """
function apply(fromChk){
  const hideUnk=document.getElementById('hideUnk');
  const hideU=hideUnk?hideUnk.checked:true;
  if(fromChk&&!hideU){document.querySelector('input[name=v][value=all]').checked=true;}
  const v=(document.querySelector('input[name=v]:checked')||{}).value||'bad';
  const q=document.getElementById('q').value.trim().toLowerCase();
  document.querySelectorAll('details.ev').forEach(d=>{
    const st=d.dataset.st;
    const ok=v==='bad'?(st==='FAIL'||st==='WARN'||st==='ASK'):v==='pass'?(st==='PASS'):(!hideU||st!=='UNKNOWN');
    const hit=!q||d.textContent.toLowerCase().includes(q);
    d.style.display=(ok&&hit)?'':'none';
    d.querySelectorAll('tr.fr').forEach(tr=>{
      tr.style.display=(v!=='bad'||tr.classList.contains('r-FAIL')||tr.classList.contains('r-WARN')||tr.classList.contains('r-ASK'))?'':'none';
    });
  });
}
window.addEventListener('DOMContentLoaded',()=>apply(false));
"""


def build_html(rep):
    e = lambda x: html.escape("" if x is None else str(x))
    s, m = rep["summary"], rep["meta"]
    out = [f"<!doctype html><html lang=zh><meta charset=utf-8><title>埋点校验报告 {e(m['generated_at'])}</title>"
           f"<style>{_HTML_CSS}</style><script>{_HTML_JS}</script><body>"]
    verdict = '<span class="badge b-PASS">通过</span>' if s["ok"] else '<span class="badge b-FAIL">未通过</span>'
    out.append(f"<header><h1>埋点校验报告 {verdict}</h1><div class=m>生成时间 {e(m['generated_at'])} · 规则 {e(m['spec'])}"
               + (f" · 日志 {e(m.get('log'))}" if m.get("log") else "") + (f" · 用例 {e(m['case'])}" if m.get("case") else "")
               + "</div></header><main>")
    out.append("<div class=cards>" + "".join(
        f"<div class=card><b class={c}>{n}</b>{t}</div>" for n, t, c in [
            (s["total"], "上报事件", ""), (s["pass"], "通过", "PASS"), (s["fail"], "失败", "FAIL"),
            (s["warn"], "警告", "WARN"), (s.get("ask", 0), "待确认", "ASK"), (s["unknown"], "未配置规则", "UNKNOWN"),
            (s["coverage_fail"], "未触发(用例要求)", "FAIL" if s["coverage_fail"] else "")]) + "</div>")

    title = f"用例覆盖：{e(m['case'])}" if m.get("case") else "点位覆盖（规则里定义的点位本次是否触发）"
    out.append(f"<section><h2>{title}</h2><table><tr><th>点位ID</th><th>名称</th><th>触发次数</th><th>要求</th><th>结果</th></tr>")
    for c in rep["coverage"]:
        label = {"PASS": "已触发", "FAIL": "未满足", "MISS": "未触发"}[c["status"]]
        out.append(f"<tr class='r-{'FAIL' if c['status'] == 'FAIL' else ''}'><td class=mono>{e(c['id'])}</td><td>{e(c['name'])}</td>"
                   f"<td>{c['count']}</td><td>{e(c['expect'])}</td><td><span class='badge b-{c['status']}'>{label}</span></td></tr>")
    out.append("</table></section>")

    if rep["hot_fields"]:
        out.append("<section><h2>问题字段汇总</h2><table><tr><th>点位ID</th><th>名称</th><th>字段</th><th>出错次数</th><th>示例原因</th></tr>")
        for h in rep["hot_fields"]:
            out.append(f"<tr><td class=mono>{e(h['id'])}</td><td>{e(h['name'])}</td><td class=mono><b>{e(h['field'])}</b></td>"
                       f"<td>{h['count']}</td><td>{e(h['reason'])}</td></tr>")
        out.append("</table></section>")

    n_bad_ev = sum(1 for r in rep["results"] if r["status"] in ("FAIL", "WARN", "ASK"))
    n_pass_ev = sum(1 for r in rep["results"] if r["status"] == "PASS")
    n_all_ev = len(rep["results"])
    out.append("<section><h2>逐条明细</h2><div class=tool>"
               f"<label><input type=radio name=v value=bad checked onchange=apply(false)> 有问题的 ({n_bad_ev})</label>"
               f"<label><input type=radio name=v value=pass onchange=apply(false)> 通过的 ({n_pass_ev})</label>"
               f"<label><input type=radio name=v value=all onchange=apply(false)> 全部 ({n_all_ev})</label>"
               f"<label><input type=checkbox id=hideUnk checked onchange=apply(true)> 过滤未配置 ({s['unknown']})</label>"
               "<input type=text id=q placeholder='搜索 点位ID / 字段 / 值' oninput=apply(false)></div>")
    for r in rep["results"]:
        nbad = sum(1 for f in r["fields"] if f["status"] in ("FAIL", "WARN")) + sum(1 for er in r["errors"] if er.get("status") in ("FAIL", "WARN"))
        is_open = " open" if r["status"] in ("FAIL", "WARN") else ""
        out.append(f"<details class='ev ev-{r['status']}' data-st={r['status']}{is_open}><summary>"
                   f"<span class='badge b-{r['status']}'>{r['status']}</span> <span class=mono>#{r['seq']} {e(r['ts'])}</span> "
                   f"<b class=mono>{e(r['id'])}</b> {e(r.get('name'))} <span class=mono style='color:#888'>{e(r['cls'])}</span>"
                   + (f" <span class=FAIL>· {nbad} 个问题</span>" if nbad else "") + "</summary><div class=evb>")
        if r["status"] == "UNKNOWN":
            out.append("<div class=ctx>规则文件中没有定义这个点位。</div>")
        if r["ctx"]:
            out.append("<div class=ctx>数据定位：" + "；".join(
                f"<b>{e(n)}</b> → " + (e(c["info"]) if c["ok"] else f"<span class=bad>{e(c['info'])}</span>")
                for n, c in r["ctx"].items()) + "</div>")
        for er in r["errors"]:
            out.append(f"<div class='err {er['status']}'>⚠ {e(er['msg'])}</div>")
        if r["fields"]:
            out.append("<table><tr><th>字段</th><th>含义</th><th>上报值</th><th>期望值</th><th>期望值来源</th><th>结果</th><th>说明</th></tr>")
            for f in r["fields"]:
                out.append(f"<tr class='fr r-{f['status']}'><td class=mono><b>{e(f['field'])}</b></td><td>{e(f['desc'])}</td>"
                           f"<td class=mono>{e(f['actual'])}</td><td class=mono>{e(f['expected'])}</td>"
                           f"<td class=mono style='color:#666'>{e(f['source'])}</td>"
                           f"<td><span class='badge b-{f['status']}'>{f['status']}</span></td><td>{e(f['reason'])}</td></tr>")
            out.append("</table>")
        elif r["data"]:
            out.append("<table><tr><th>字段</th><th>上报值</th></tr>" + "".join(
                f"<tr><td class=mono>{e(k)}</td><td class=mono>{e(v)}</td></tr>" for k, v in r["data"].items()) + "</table>")
        out.append(f"<pre class=mono>{e(r['raw'])}</pre></div></details>")
    out.append("</section></main></body></html>")
    return "".join(out)


def write_report(rep, out_dir, stem=None):
    os.makedirs(out_dir, exist_ok=True)
    stem = stem or "trackcheck_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    hp, jp = os.path.join(out_dir, stem + ".html"), os.path.join(out_dir, stem + ".json")
    with open(hp, "w", encoding="utf-8") as f:
        f.write(build_html(rep))
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    return hp, jp


def print_result(r):
    tag = {"PASS": color("PASS", "green"), "WARN": color("WARN", "yellow"), "FAIL": color("FAIL", "red"), "ASK": color("待确认", "cyan"),
           "UNKNOWN": color("????", "gray")}[r["status"]]
    print(f"{tag} #{r['seq']:<4} {r['ts']:<18} {color(r['id'], 'cyan')} {r.get('name', '')}"
          + (color("  (规则外点位)", "gray") if r["status"] == "UNKNOWN" else ""))
    for n, c in r["ctx"].items():
        if not c["ok"]:
            print(color(f"       定位失败 {n}: {c['info']}", "red"))
    blocked = 0
    for f in r["fields"]:
        if f["status"] in ("FAIL", "WARN", "ASK"):
            if f["reason"].startswith("无法取得期望值：上下文"):
                blocked += 1
                continue
            c = {"FAIL": "red", "WARN": "yellow", "ASK": "cyan"}[f["status"]]
            mark = "?" if f["status"] == "ASK" else "✗"
            print(color(f"       {mark} {f['field']:<18}", c) + f" 上报={f['actual']!s:<20} 期望={f['expected'] or '-'}  "
                  + color(f["reason"], c))
    if blocked:
        print(color(f"       … 另有 {blocked} 个字段因数据定位失败无法比对（详见报告）", "red"))
    for er in r["errors"]:
        print(color(f"       ⚠ {er['msg']}", "red" if er["status"] == "FAIL" else "yellow"))


def print_summary(rep, hp):
    s = rep["summary"]
    print()
    print(color("=" * 60, "gray"))
    print(f"事件 {s['total']}  " + color(f"通过 {s['pass']}", "green") + "  " + color(f"失败 {s['fail']}", "red") + "  "
          + color(f"警告 {s['warn']}", "yellow") + f"  规则外 {s['unknown']}" + (color(f"  待人工确认 {s.get('ask', 0)}", "cyan") if s.get("ask") else ""))
    miss = [c for c in rep["coverage"] if c["status"] in ("FAIL", "MISS")]
    if miss:
        lab = "用例未满足" if rep["meta"].get("case") else "规则内未触发"
        print(color(f"{lab}: ", "yellow") + ", ".join(f"{c['id']}({c['name']})" for c in miss))
    print("报告: " + color(hp, "bold"))
    print(color("结论: 通过", "green") if s["ok"] else color("结论: 未通过", "red"))


# ----------------------------------------------------------------------------
# adb 相关
# ----------------------------------------------------------------------------
_ADB_PATH = None


def find_adb():
    """依次查找 adb：PATH → ANDROID_HOME / ANDROID_SDK_ROOT → Android Studio 默认 SDK 目录 → 程序同目录 platform-tools"""
    global _ADB_PATH
    if _ADB_PATH and os.path.isfile(_ADB_PATH):
        return _ADB_PATH
    import shutil
    exe = "adb.exe" if os.name == "nt" else "adb"
    cands = [shutil.which("adb")]
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env):
            cands.append(os.path.join(os.environ[env], "platform-tools", exe))
    if os.name == "nt":
        if os.environ.get("LOCALAPPDATA"):
            cands.append(os.path.join(os.environ["LOCALAPPDATA"], "Android", "Sdk", "platform-tools", exe))
    else:
        home = os.path.expanduser("~")
        cands += [os.path.join(home, "Library", "Android", "sdk", "platform-tools", exe),
                  os.path.join(home, "Android", "Sdk", "platform-tools", exe)]
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    cands += [os.path.join(base, "platform-tools", exe), os.path.join(base, exe)]
    for c in cands:
        if c and os.path.isfile(c):
            _ADB_PATH = c
            return c
    return "adb"


def adb_base(spec, serial=None):
    serial = serial or (spec.device.get("serial") if spec else None)
    return [find_adb()] + (["-s", serial] if serial else [])


def do_pull(spec, serial=None):
    dev = spec.device
    pkg = dev.get("package")
    adb = adb_base(spec, serial)
    items = dev.get("pull") or []
    dbs = [str(x) for x in (dev.get("dbs") or [])]
    for db in dbs:   # 只填 包名 + db 名：拉到数据源目录
        try:
            lp = pull_app_db(adb, pkg, db, os.path.join(spec.datasource_dir, db))
            print(color("  ✓ ", "green") + f"{pkg}/databases/{db} -> {lp}")
        except Exception as e:
            print(color(f"  ✗ {db}: {e}", "red"))
    if not items:
        if not dbs:
            print(color("没有配置要拉取的内容（device.package + device.dbs）", "yellow"))
        return
    for it in items:
        remote, local = str(it["remote"]), spec.abspath(str(it["local"]))
        os.makedirs(os.path.dirname(local.rstrip("/\\")) or ".", exist_ok=True)
        if remote.startswith("/"):
            if remote.endswith("/.") or local.endswith(("/", "\\")):
                os.makedirs(local, exist_ok=True)
            p = subprocess.run(adb + ["pull", remote, local], capture_output=True, text=True, errors="replace", **popen_kw())
            msg = (p.stdout + p.stderr).strip().splitlines()
            print((color("  ✓ ", "green") if p.returncode == 0 else color("  ✗ ", "red"))
                  + f"{remote} -> {local}  " + (msg[-1] if msg else ""))
            continue
        if not pkg:
            print(color(f"  ✗ {remote}: 相对路径需要 device.package（通过 run-as 读私有目录）", "red"))
            continue
        files = [remote]
        if it.get("with_wal", remote.endswith(".db")):
            files += [remote + "-wal", remote + "-shm"]
        for i, rf in enumerate(files):
            lf = local + rf[len(remote):]
            chk = subprocess.run(adb + ["shell", "run-as", pkg, "ls", rf], capture_output=True, text=True, errors="replace", **popen_kw())
            if chk.returncode != 0 or "No such file" in chk.stdout + chk.stderr:
                if i == 0:
                    print(color(f"  ✗ {rf}: {(chk.stdout + chk.stderr).strip()}", "red"))
                    break
                if os.path.exists(lf):
                    os.remove(lf)  # 手机上没有 wal 了，删掉本地旧的，避免读到脏数据
                continue
            with open(lf, "wb") as f:
                p = subprocess.run(adb + ["exec-out", "run-as", pkg, "cat", rf], stdout=f, **popen_kw())
            print((color("  ✓ ", "green") if p.returncode == 0 else color("  ✗ ", "red")) + f"{pkg}/{rf} -> {lf}")


class Capture:
    """后台线程跑 adb logcat，解析埋点行"""

    def __init__(self, spec, serial, raw_path, on_event=None, on_line=None):
        self.spec, self.serial, self.raw_path, self.on_event, self.on_line = spec, serial, raw_path, on_event, on_line
        self.parser = LogParser(spec.pattern)
        self.events, self.proc, self.thread = [], None, None

    def start(self, clear=False):
        adb = adb_base(self.spec, self.serial)
        if clear:
            subprocess.run(adb + ["logcat", "-c"], capture_output=True, **popen_kw())
        os.makedirs(os.path.dirname(self.raw_path) or ".", exist_ok=True)
        self.proc = subprocess.Popen(adb + ["logcat", "-v", "threadtime"] + self.spec.logcat_args,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen_kw())
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        with open(self.raw_path, "w", encoding="utf-8") as raw:
            n = 0
            for b in iter(self.proc.stdout.readline, b""):
                n += 1
                line = b.decode("utf-8", errors="replace")
                if self.on_line:
                    try:
                        self.on_line(line)
                    except Exception:
                        pass
                ev = self.parser.feed(line, n)
                if ev:
                    raw.write(line.rstrip("\r\n") + "\n")
                    raw.flush()
                    self.events.append(ev)
                    if self.on_event:
                        try:
                            self.on_event(ev)
                        except Exception as e:  # 不让一条异常打断抓取
                            print(color(f"[error] 校验 #{ev.seq} 出错: {e}", "red"))
                elif "error:" in line and n < 5:
                    print(color(line.strip(), "red"))

    def stop(self, linger=1.0):
        time.sleep(linger)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if self.thread:
            self.thread.join(timeout=3)


# ----------------------------------------------------------------------------
# 命令
# ----------------------------------------------------------------------------
def _stamp():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_sets(items):
    out = {}
    for it in items or []:
        k, _, v = str(it).partition("=")
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def check_all(spec, events, verbose=True, scenario=None):
    ck = Checker(spec, Sources(spec), scenario=scenario)
    results = []
    for ev in events:
        r = ck.check(ev)
        results.append(r)
        if verbose:
            print_result(r)
    return results


def cmd_check(a):
    spec = Spec(a.spec)
    events = read_log(a.log, spec.pattern)
    if not events:
        print(color(f"日志里没有匹配到埋点行，检查 log.pattern：{spec.pattern}", "yellow"))
    if a.only:
        events = [e for e in events if e.id in set(a.only)]
    results = check_all(spec, events, verbose=not a.quiet, scenario={**case_values(spec, a.case), **parse_sets(a.set)})
    rep = build_report(spec, results, a.case, {"log": os.path.abspath(a.log)})
    hp, _ = write_report(rep, a.out)
    print_summary(rep, hp)
    return 0 if rep["summary"]["ok"] else 1


def _live_common(a, runner=None):
    spec = Spec(a.spec)
    scn = {**case_values(spec, a.case), **parse_sets(a.set)}
    ck = Checker(spec, Sources(spec), scenario=scn)
    stamp = _stamp()
    raw = os.path.join(a.out, f"session_{stamp}.log")
    lock = threading.Lock()

    def on_event(ev):
        with lock:
            print_result(ck.check(ev))

    cap = Capture(spec, a.serial, raw, on_event)
    cap.start(clear=a.clear)
    print(color(f"正在抓取 logcat（原始埋点日志保存到 {raw}）", "cyan"))
    rc_runner = 0
    try:
        if runner:
            rc_runner = runner()
        else:
            print(color("去手机上操作吧，结束后按 Ctrl+C 生成报告 ...", "cyan"))
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        cap.stop(linger=a.linger if runner else 0.3)
    if a.pull:
        print(color("拉取手机最新 db / 接口 dump 后重新校验 ...", "cyan"))
        do_pull(spec, a.serial)
    results = check_all(spec, cap.events, verbose=False, scenario=scn)
    rep = build_report(spec, results, a.case, {"log": raw})
    hp, _ = write_report(rep, a.out, f"trackcheck_{stamp}")
    print_summary(rep, hp)
    return 0 if rep["summary"]["ok"] and rc_runner == 0 else 1


def cmd_live(a):
    return _live_common(a)


def cmd_run(a):
    cmd = a.command[1:] if a.command and a.command[0] == "--" else a.command
    if not cmd:
        raise SystemExit("用法: trackcheck.py run --spec x.yaml --case 用例 -- maestro test flow.yaml")

    def runner():
        print(color("执行: " + " ".join(cmd), "cyan"))
        rc = subprocess.run(cmd, shell=(os.name == "nt")).returncode
        if rc != 0:
            print(color(f"自动化脚本退出码 {rc}", "red"))
        return rc

    return _live_common(a, runner)


def cmd_pull(a):
    do_pull(Spec(a.spec), a.serial)
    return 0


# ----------------------------------------------------------------------------
# infer：反推字段来源，生成规则草稿
# ----------------------------------------------------------------------------
def iter_leaves(obj, path=()):
    obj = maybe_json(obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_leaves(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_leaves(v, path + (i,))
    else:
        yield path, obj


def path_str(tokens, generalize=False):
    out = ""
    for t in tokens:
        if isinstance(t, int):
            out += "[*]" if generalize else f"[{t}]"
        else:
            out += ("." if out else "") + str(t)
    return out


def key_sim(f, k):
    if not isinstance(k, str):
        return 0
    a, b = re.sub(r"[_\-]", "", f.lower()), re.sub(r"[_\-]", "", k.lower())
    if a == b:
        return 3
    if len(b) >= 3 and (a in b or b in a):
        return 2
    s = lambda x: re.sub(r"(id|name|type|num|no|code)$", "", x)
    return 1 if s(a) and s(a) == s(b) else 0


def _ctx_name(node, used, fields):
    base = next((t for t in reversed(node) if isinstance(t, str)), "root")
    base = re.sub(r"\W", "_", base)
    if base.endswith("ies"):
        base = base[:-3] + "y"
    elif base.endswith("s") and len(base) > 3:
        base = base[:-1]
    if not base or base[0].isdigit():
        base = "n_" + base
    name, i = base, 2
    while name in used or name in fields:
        name, i = f"{base}{i}", i + 1
    used.add(name)
    return name


def _key_rank(k):
    k = str(k).lower()
    return (0 if k == "id" else 1 if k.endswith("id") else 2, len(k))


class JsonIndex:
    def __init__(self, name, data):
        self.name = name
        self.leaves = [(p, v) for p, v in iter_leaves(data) if p and isinstance(p[-1], str)]
        self.index = defaultdict(list)
        for p, v in self.leaves:
            self.index[norm(v)].append(p)


def infer_occurrence(ev, ji):
    fields = ev.fields
    idlike = lambda f: re.search(r"(id|key|code)$", f, re.I) is not None

    def find_anchors(strict):
        out = []
        for f, v in fields.items():
            nv = norm(v)
            if len(nv) < 3 or nv.lower() in ("true", "false", "null") or (strict and not idlike(f)):
                continue
            hits = ji.index.get(nv, [])
            if len(hits) > 1:
                sim = [p for p in hits if key_sim(f, p[-1]) >= 2]
                hits = sim if len(sim) == 1 else hits
            # 只把"数组里的一个元素"当作定位节点（资源位、卡片这类实体）
            if len(hits) == 1 and len(hits[0]) >= 2 and isinstance(hits[0][-2], int):
                out.append((f, hits[0]))
        return out

    anchors = find_anchors(True) or find_anchors(False)
    nodes = OrderedDict()
    for f, p in sorted(anchors, key=lambda x: (len(x[1]), _key_rank(x[1][-1]))):
        if p[:-1] not in nodes:
            nodes[p[:-1]] = (f, p[-1])
    ctxs, used = [], set()
    for node in sorted(nodes, key=len):
        parent = None
        for c in ctxs:
            if len(c["node"]) < len(node) and node[:len(c["node"])] == c["node"]:
                if parent is None or len(c["node"]) > len(parent["node"]):
                    parent = c
        ctxs.append({"name": _ctx_name(node, used, fields), "node": node, "parent": parent,
                     "field": nodes[node][0], "key": nodes[node][1]})

    def container(p):
        best = None
        for c in ctxs:
            if len(c["node"]) < len(p) and p[:len(c["node"])] == c["node"]:
                if best is None or len(c["node"]) > len(best["node"]):
                    best = c
        return best

    def in_sibling(p):
        """p 是否落在某个上下文的"兄弟元素"里（比如定位的是 cards[0]，命中却在 cards[1]）"""
        for c in ctxs:
            n = c["node"]
            k = 0
            while k < len(n) and k < len(p) and n[k] == p[k]:
                k += 1
            if k < len(n) and k < len(p) and isinstance(n[k], int) and isinstance(p[k], int):
                return True
        return False

    sugg = {}
    anchor_fields = {c["field"]: c for c in ctxs}
    for f, v in fields.items():
        nv = norm(v)
        if f in anchor_fields:
            c = anchor_fields[f]
            sugg[f] = {"kind": "from", "expr": f"{c['name']}.{c['key']}", "note": "定位键"}
            continue
        cands = []
        for p in ji.index.get(nv, []):
            c = container(p)
            ks = key_sim(f, p[-1])
            if c is None or in_sibling(p):
                continue
            if (nv == "" and ks < 2) or (len(nv) < 3 and ks == 0 and len(p) - len(c["node"]) > 3):
                continue  # 空值/短值只接受字段名相近或离上下文很近的命中
            cands.append((ks * 10 + len(c["node"]) - 0.1 * (len(p) - len(c["node"])), c, p))
        if cands:
            cands.sort(key=lambda x: -x[0])
            sc, c, p = cands[0]
            note = ""
            ties = [x for x in cands[1:] if x[0] == sc]
            if ties:
                note = "多个候选，请确认: " + ", ".join(f"{x[1]['name']}.{path_str(x[2][len(x[1]['node']):])}" for x in ties[:3])
            elif key_sim(f, p[-1]) == 0 and len(nv) < 3:
                note = f"短值 {nv!r} 按值匹配到，字段名不同，请确认"
            sugg[f] = {"kind": "from", "expr": f"{c['name']}.{path_str(p[len(c['node']):])}", "note": note,
                       "weak": key_sim(f, p[-1]) == 0 and len(nv) < 3}
            continue
        hits = ji.index.get(nv, [])
        if len(nv) >= 3 and len(hits) == 1 and not in_sibling(hits[0]):
            sugg[f] = {"kind": "from", "expr": f"{ji.name}:{path_str(hits[0])}",
                       "note": "全局唯一匹配（绝对路径），建议改成上下文相对路径"}
            continue
        nm = []
        for c in ctxs:
            for p, lv in ji.leaves:
                d = len(p) - len(c["node"])
                if 0 < d <= 4 and p[:len(c["node"])] == c["node"] and key_sim(f, p[-1]) == 3:
                    nm.append((len(c["node"]), -d, c, p, lv))
        if nm:
            nm.sort(key=lambda x: (-x[0], -x[1]))
            _, _, c, p, lv = nm[0]
            expr, api = f"{c['name']}.{path_str(p[len(c['node']):])}", norm(lv)
            numeric = lambda x: re.fullmatch(r"-?\d+(\.\d+)?", x) is not None
            if nv and not (numeric(nv) and numeric(api)):
                sugg[f] = {"kind": "map", "expr": expr, "api": api, "reported": nv}
            else:  # 上报为空 / 两边都是数字却不相等：更像是 bug，而不是需要映射
                sugg[f] = {"kind": "from", "expr": expr, "weak": True,
                           "note": f"按字段名匹配；本次接口值 {api!r} 与上报值 {nv!r} 不一致，请确认"}
    score = sum(1 for s in sugg.values() if s["kind"] == "from")
    return score, ctxs, sugg


def infer_db(ev, conns, todo):
    """在 DB 每张表里找"同一行同时包含 定位键的值 和 该字段的值"，反推 SQL"""
    out = {}
    if not todo:
        return out
    idlike = lambda s: re.search(r"(id|key|code)$", s, re.I) is not None
    anchors = {f: norm(v) for f, v in ev.fields.items() if len(norm(v)) >= 3}
    for dbname, conn in conns.items():
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'android_%'")]
        except sqlite3.Error:
            continue
        for t in tables:
            try:
                cur = conn.execute(f'SELECT * FROM "{t}" LIMIT 50000')
                cols = [d[0] for d in cur.description]
            except sqlite3.Error:
                continue
            for row in cur:
                vals = [norm(x) for x in row]
                cand = [(af, cols[i]) for af, av in anchors.items() for i, cv in enumerate(vals) if cv == av]
                if not cand:
                    continue
                cand.sort(key=lambda x: (-key_sim(x[0], x[1]), not idlike(x[0]), not idlike(x[1])))
                af, ac = cand[0]
                if af in todo and af not in out:
                    out[af] = {"kind": "sql", "db": dbname, "note": "定位键（校验 DB 中存在）",
                               "sql": f'SELECT "{ac}" FROM "{t}" WHERE "{ac}" = :{af}'}
                for f in todo:
                    if f in out or f == af:
                        continue
                    fv = norm(ev.fields.get(f))
                    if not fv:
                        continue
                    hit = [cols[i] for i, cv in enumerate(vals) if cv == fv and cols[i] != ac]
                    if hit:
                        hit.sort(key=lambda c: -key_sim(f, c))
                        out[f] = {"kind": "sql", "db": dbname,
                                  "sql": f'SELECT "{hit[0]}" FROM "{t}" WHERE "{ac}" = :{af}'}
            if len(out) == len(todo):
                return out
    return out


def yq(v):
    return json.dumps("" if v is None else str(v), ensure_ascii=False)


def ykey(k):
    k = str(k)
    return k if re.fullmatch(r"[A-Za-z_][\w]*", k) else yq(k)


def _ctx_sig(ctxs):
    out = []
    for c in ctxs:
        rel = c["node"][len(c["parent"]["node"]):] if c["parent"] else c["node"]
        out.append((c["name"], c["parent"]["name"] if c["parent"] else None, path_str(rel, True), c["key"], c["field"]))
    return tuple(out)


def _vote(runs, all_fields):
    """多次出现的同一点位分别推断，按字段投票；不一致的给出提示（不一致本身常常就是埋点 bug）"""
    sugg = {}
    for f in all_fields:
        votes, samples, present = Counter(), {}, 0
        for r in runs:
            ev, s = r["ev"], r["sugg"].get(f)
            if f in ev.fields and norm(ev.fields[f]) != "":
                present += 1
            if s:
                k = (s["kind"], s["expr"] if s["kind"] != "sql" else s["sql"])
                votes[k] += 1
                if k not in samples or (samples[k].get("weak") and not s.get("weak")):
                    samples[k] = s
        if votes:
            k, n = votes.most_common(1)[0]
            s = dict(samples[k])
            if s.get("weak") and present and n < present:
                continue  # 弱匹配（短值/仅字段名）且多次出现不一致 -> 大概率是巧合，不采纳
            if present and n < present:
                s["vote"] = f"{n}/{present} 次一致，其余出现的值对不上，可能是埋点 bug，请看报告"
            sugg[f] = s
    return sugg


def cmd_infer(a):
    spec = Spec(a.spec) if a.spec else None
    pattern = spec.pattern if spec else (a.pattern or DEFAULT_PATTERN)
    events = read_log(a.log, pattern)
    if not events:
        raise SystemExit("日志中没有匹配到埋点行")
    jsons, conns, cli_sources = [], {}, []
    if spec:
        src = Sources(spec)
        for name, cfg in spec.sources.items():
            try:
                if cfg.get("type", "json_file") in ("json_file", "sqlite_json", "merged_json"):
                    jsons.append(JsonIndex(name, src.json(name)))
                elif cfg.get("type") == "sqlite":
                    conns[name] = src.db(name)
            except SourceError as e:
                print(color(f"[warn] 数据源 {name} 读取失败: {e}", "yellow"))
    for p in a.json or []:
        for fp in glob.glob(p) or [p]:
            name = re.sub(r"\W", "_", os.path.splitext(os.path.basename(fp))[0])
            with open(fp, encoding="utf-8-sig") as f:
                jsons.append(JsonIndex(name, json.load(f)))
            cli_sources.append((name, {"type": "json_file", "path": os.path.abspath(fp)}))
    for p in a.db or []:
        name = re.sub(r"\W", "_", os.path.splitext(os.path.basename(p))[0]) + "_db"
        conns[name] = sqlite3.connect(p)
        cli_sources.append((name, {"type": "sqlite", "path": os.path.abspath(p)}))

    drafts, dict_drafts = infer_events(events, jsons, conns)
    text = drafts_to_yaml(drafts, dict_drafts, cli_sources, a.log)
    if a.output:
        with open(a.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(color(f"规则草稿已写入 {a.output}（{len(drafts)} 个点位）", "green"))
    else:
        print(text)
    return 0


def load_infer_sources(spec):
    jsons, conns, warns = [], {}, []
    src = Sources(spec)
    for name, cfg in spec.sources.items():
        try:
            if cfg.get("type", "json_file") in ("json_file", "sqlite_json", "merged_json"):
                jsons.append(JsonIndex(name, src.json(name)))
            elif cfg.get("type") == "sqlite":
                conns[name] = src.db(name)
        except (SourceError, sqlite3.Error) as e:
            warns.append(f"数据源 {name} 读取失败: {e}")
    return jsons, conns, warns


def infer_events(events, jsons, conns):
    """返回结构化草稿: [{id, cls, count, source, contexts:[...], fields:[{field, kind, rule, status, note, observed}]}]"""
    groups = OrderedDict()
    for ev in events:
        groups.setdefault(ev.id, []).append(ev)
    dict_drafts = OrderedDict()
    drafts = []
    for eid, evs in groups.items():
        all_fields = list(OrderedDict.fromkeys(k for ev in evs for k in ev.fields))
        runs = []
        for ev in evs[:50]:
            for ji_ in jsons:
                sc, ctxs_, sugg_ = infer_occurrence(ev, ji_)
                if ctxs_:
                    runs.append({"ji": ji_, "ctxs": ctxs_, "sugg": sugg_, "ev": ev, "score": sc,
                                 "sig": (ji_.name, _ctx_sig(ctxs_))})
        ji, ctxs, sugg, bev = None, [], {}, evs[0]
        if runs:
            cnt, tot = Counter(r["sig"] for r in runs), Counter()
            for r in runs:
                tot[r["sig"]] += r["score"]
            best_sig = max(cnt, key=lambda k: (cnt[k], tot[k]))
            chosen = [r for r in runs if r["sig"] == best_sig]
            ji, ctxs, bev = chosen[0]["ji"], chosen[0]["ctxs"], chosen[0]["ev"]
            sugg = _vote(chosen, all_fields)
            for f, s in sugg.items():  # map 草稿：汇总所有出现的 接口值 -> 上报值
                if s["kind"] == "map":
                    dname = f"{f}_map"
                    d = OrderedDict()  # 映射表跟着点位走（每个点位各自一份），直接内联在字段规则里
                    for r in chosen:
                        x = r["sugg"].get(f)
                        if x and x["kind"] == "map" and x["expr"] == s["expr"]:
                            d.setdefault(x["api"], x["reported"])
                    s["dict"] = dname
                    s["inline"] = dict(d)
        todo = [f for f in all_fields if f not in sugg]
        if todo and conns:
            db_runs = [{"ev": ev, "sugg": infer_db(ev, conns, todo)} for ev in evs[:10]]
            sugg.update(_vote(db_runs, todo))
        observed = {f: list(OrderedDict.fromkeys(norm(ev.fields.get(f)) for ev in evs if f in ev.fields)) for f in all_fields}

        contexts = []
        for c in ctxs:
            frm = c["parent"]["name"] if c["parent"] else ji.name
            rel = c["node"][len(c["parent"]["node"]):] if c["parent"] else c["node"]
            contexts.append({"name": c["name"], "from": frm, "path": path_str(rel, True),
                             "where": {str(c["key"]): "{" + c["field"] + "}"}})
        fields = []
        for f in all_fields:
            s = sugg.get(f)
            obs = observed.get(f, [])
            notes = [x for x in ((s or {}).get("note"), (s or {}).get("vote")) if x]
            if s and s["kind"] == "from":
                rule, status = {"from": s["expr"]}, "confirm" if notes and notes != ["定位键"] else "ok"
            elif s and s["kind"] == "map":
                rule, status = {"from": s["expr"], "map": s.get("inline") or {}}, "confirm"
                notes.insert(0, f"接口值与上报值不同（如 {s['api']!r} → {s['reported']!r}），已按本次观测生成本点位专用映射表，请对照文档补全")
            elif s and s["kind"] == "sql":
                rule, status = {"db": s["db"], "sql": s["sql"]}, "confirm"
                notes.insert(0, "从 DB 反推")
            elif obs and any(obs):
                rule, status = {"enum": [x for x in obs if x][:20]}, "todo"
                notes = ["接口/DB 里没找到，多半是文档定义值：按文档补全枚举，或引用文档字典"]
            else:
                rule, status = {}, "todo"
                notes = ["本次全为空，无法推断"]
            fields.append({"field": f, "kind": (s or {}).get("kind", "enum" if rule else "empty"), "rule": rule,
                           "status": status, "note": "；".join(notes), "observed": obs[:20]})
        drafts.append({"id": eid, "cls": bev.cls or "", "count": len(evs), "source": ji.name if ji and ctxs else "",
                       "contexts": contexts, "fields": fields, "sample": bev.raw})
    return drafts, dict_drafts


def drafts_to_yaml(drafts, dict_drafts, cli_sources=None, log_path=""):
    def flow(d):
        parts = []
        for k, v in d.items():
            if isinstance(v, list):
                parts.append(f"{k}: [{', '.join(yq(x) for x in v)}]")
            elif isinstance(v, dict):
                parts.append(f"{k}: {flow({ykey(kk): vv for kk, vv in v.items()})}")
            else:
                plain = k not in ("from", "sql", "path") and re.fullmatch(r"[A-Za-z_]\w*", str(v))
                parts.append(f"{k}: {v if plain else yq(v)}")
        return "{" + ", ".join(parts) + "}"

    out = ["# trackcheck infer 自动生成的规则草稿 —— 请逐条确认，尤其是 TODO 和注释提示",
           f"# 日志: {os.path.abspath(log_path) if log_path else '-'}   生成时间: {dt.datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    if cli_sources:
        out.append("sources:")
        for n, cfg in cli_sources:
            out.append(f"  {n}: {{type: {cfg['type']}, path: {yq(cfg['path'])}}}")
        out.append("")
    if dict_drafts:
        out.append("dicts:   # 根据观测到的 接口值 -> 上报值 生成，只包含本次见到的值，请按文档补全")
        for n, d in dict_drafts.items():
            out.append(f"  {n}:")
            for k, v in d.items():
                out.append(f"    {yq(k)}: {yq(v)}")
        out.append("")
    out.append("events:")
    for d in drafts:
        out += [f"  {yq(d['id'])}:", f"    name: {yq(d['cls'] or d['id'])}   # TODO 改成文档里的点位名称",
                f"    # 本次日志出现 {d['count']} 次" + (f"；主数据源 {d['source']}" if d["source"] else "；未在接口 JSON 中定位到")]
        if d["contexts"]:
            out.append("    context:")
            for c in d["contexts"]:
                out.append(f"      {c['name']}: {flow({'from': c['from'], 'path': c['path'], 'where': {ykey(k): v for k, v in c['where'].items()}})}")
        out.append("    fields:")
        for f in d["fields"]:
            mark = {"ok": "", "confirm": "请确认: ", "todo": "TODO "}[f["status"]]
            note = f["note"] if f["note"] != "定位键" or not mark else f["note"]
            out.append(f"      {ykey(f['field'])}: {flow(f['rule'])}" + (f"   # {mark}{note}" if note else ""))
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="trackcheck", description="Android 埋点调试 / 自动校验工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, need_spec=True):
        p.add_argument("--spec", default="trackcheck.yaml" if need_spec else None, help="规则文件，默认 ./trackcheck.yaml")
        p.add_argument("--serial", help="adb 设备序列号（多台设备时）")

    p = sub.add_parser("check", help="离线校验日志文件")
    common(p)
    p.add_argument("--log", required=True, help="logcat 日志文件")
    p.add_argument("--case", help="按 cases 中的用例检查覆盖率")
    p.add_argument("--set", action="append", metavar="字段=值", help="本次操作场景，给 ask 字段做期望值，如 --set source=1 --set tab=bookmark")
    p.add_argument("--only", nargs="*", help="只校验这些点位ID")
    p.add_argument("--out", default="reports", help="报告输出目录")
    p.add_argument("-q", "--quiet", action="store_true", help="不逐条打印")
    p.set_defaults(fn=cmd_check)

    for name, fn, hlp in (("live", cmd_live, "实时抓 logcat 校验，Ctrl+C 结束"),
                          ("run", cmd_run, "执行自动化脚本并校验，例: run --case X -- maestro test flow.yaml")):
        p = sub.add_parser(name, help=hlp)
        common(p)
        p.add_argument("--case", help="按 cases 中的用例检查覆盖率")
        p.add_argument("--set", action="append", metavar="字段=值", help="本次操作场景（ask 字段的期望值）")
        p.add_argument("--out", default="reports", help="报告/会话日志输出目录")
        p.add_argument("--clear", action="store_true", default=(name == "run"), help="开始前清空 logcat 缓冲")
        p.add_argument("--pull", action="store_true", help="结束后按 device.pull 拉取最新 db/接口 dump 再出最终报告")
        p.add_argument("--linger", type=float, default=2.0, help="脚本结束后继续抓取的秒数（等最后的埋点打出来）")
        if name == "run":
            p.add_argument("command", nargs=argparse.REMAINDER, help="-- 之后为要执行的自动化命令")
        p.set_defaults(fn=fn)

    p = sub.add_parser("infer", help="根据日志+接口JSON+DB反推字段来源，生成规则草稿")
    common(p, need_spec=False)
    p.add_argument("--log", required=True)
    p.add_argument("--json", nargs="*", help="接口返回 JSON 文件（可多个/通配符）")
    p.add_argument("--db", nargs="*", help="SQLite 文件")
    p.add_argument("--pattern", help="日志正则（不用 --spec 时）")
    p.add_argument("-o", "--output", help="草稿输出文件，默认打印到屏幕")
    p.set_defaults(fn=cmd_infer)

    p = sub.add_parser("pull", help="按 device.pull 从手机拉取 db / 接口 dump")
    common(p)
    p.set_defaults(fn=cmd_pull)

    a = ap.parse_args(argv)
    if getattr(a, "spec", None) and not os.path.exists(a.spec):
        raise SystemExit(f"找不到规则文件 {a.spec}（用 --spec 指定，或先用 infer 生成草稿）")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
