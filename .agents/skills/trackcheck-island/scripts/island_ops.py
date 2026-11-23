#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
埋点小岛（trackcheck）自动化运维与防回归校验脚本（仅依赖 Python 标准库）。

子命令：
  audit  编译检查 Python + 扫描违禁老蓝/深蓝 hex + 检查关键静态资源与 CSS 状态
  sync   将开发目录安全同步至 TrackDemo1/trackcheck（严格排除个人 trackcheck.yaml、build/dist/reports）
  smoke  在临时目录冒烟验证无黑框 埋点小岛.exe（启动 -> /api/state -> /api/devices -> /api/server_log -> /api/shutdown）
"""
import argparse
import filecmp
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

DEFAULT_SRC = r"C:\Users\windows\.gemini\antigravity\scratch\trackcheck"
DEFAULT_DST = r"C:\Users\windows\.gemini\antigravity\scratch\TrackDemo1\trackcheck"

SKIP_DIRS = {
    "build", "dist", "__pycache__", ".git", "node_modules",
    "reports", ".history", "datasources", "docs",
}
SKIP_FILES = {"trackcheck.yaml", "trackcheck.yaml.bak", "event_dict.json"}
SKIP_EXTS = (".pyc", ".bak", ".log", ".exe", ".zip")

REQUIRED_ICONS = [
    "shell.png", "sprout.png", "fail.png", "bud.png",
    "data.png", "template.png", "home.png", "live.png",
    "rules.png", "cases.png", "report.png", "dict.png",
]
# 明确允许的清透晴空高亮天蓝（Tailwind/渐变主色，明度高、非老蓝/深蓝）
ALLOWED_BRIGHT_SKY = {"#5EB8EE", "#58B4EB", "#4CAEE8", "#489ED6"}


def _write_json(path, data):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _is_forbidden_dark_blue(hex_code):
    h = hex_code.upper()
    if h in ALLOWED_BRIGHT_SKY:
        return False
    r, g, b = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
    # 老蓝色 / 深蓝色 / 暗冷灰蓝判定：蓝色明显主导且整体偏暗沉
    return (b > r + 25 and b > g + 10 and r < 95 and g < 165)


def cmd_audit(args):
    src = os.path.abspath(args.src)
    report = {
        "src": src,
        "py_compile": [],
        "forbidden_colors": [],
        "missing_assets": [],
        "css_stale": False,
        "ok": True,
    }

    # 1. Python 编译检查
    for fn in ("trackcheck.py", "trackcheck_web.py", "build_exe.py"):
        p = os.path.join(src, fn)
        if not os.path.isfile(p):
            report["missing_assets"].append(fn)
            report["ok"] = False
            continue
        try:
            py_compile.compile(p, doraise=True)
            report["py_compile"].append({"file": fn, "ok": True})
        except py_compile.PyCompileError as e:
            report["py_compile"].append({"file": fn, "ok": False, "error": str(e)})
            report["ok"] = False

    # 2. 配色红线扫描
    for rel in (r"web\index.html", r"web\app.js", r"web\src\input.css"):
        p = os.path.join(src, rel)
        if not os.path.isfile(p):
            report["missing_assets"].append(rel)
            report["ok"] = False
            continue
        txt = open(p, encoding="utf-8").read()
        for lineno, line in enumerate(txt.splitlines(), 1):
            for m in re.findall(r"#[0-9A-Fa-f]{6}\b", line):
                if _is_forbidden_dark_blue(m):
                    report["forbidden_colors"].append({"file": rel, "line": lineno, "hex": m})
                    report["ok"] = False

    # 3. 静态图标与 CSS 新鲜度检查
    icons_dir = os.path.join(src, "web", "static", "icons")
    for ic in REQUIRED_ICONS:
        if not os.path.isfile(os.path.join(icons_dir, ic)):
            report["missing_assets"].append(f"web/static/icons/{ic}")
            report["ok"] = False

    css_out = os.path.join(src, "web", "static", "app.css")
    if not os.path.isfile(css_out):
        report["missing_assets"].append("web/static/app.css")
        report["ok"] = False
    else:
        css_mtime = os.path.getmtime(css_out)
        for rel in (r"web\index.html", r"web\app.js", r"web\src\input.css"):
            p = os.path.join(src, rel)
            if os.path.isfile(p) and os.path.getmtime(p) > css_mtime + 2:
                report["css_stale"] = True

    _write_json(args.output, report)
    print(f"Audit {'PASSED' if report['ok'] else 'FAILED'} | py={len(report['py_compile'])} "
          f"| forbidden_colors={len(report['forbidden_colors'])} "
          f"| missing={len(report['missing_assets'])} | css_stale={report['css_stale']}")
    if args.output:
        print(f"Report written to: {os.path.abspath(args.output)}")
    return 0 if report["ok"] else 1


def _iter_sync_files(src):
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel_dir = os.path.relpath(root, src)
        for fn in sorted(files):
            if fn in SKIP_FILES or fn.lower().endswith(SKIP_EXTS):
                continue
            rel = os.path.normpath(os.path.join(rel_dir, fn)) if rel_dir != "." else fn
            if rel.lower() == os.path.normpath(r"samples\trackcheck.yaml").lower():
                continue
            yield rel, os.path.join(root, fn)


def cmd_sync(args):
    src = os.path.abspath(args.src)
    dst = os.path.abspath(args.dst)
    if not os.path.isdir(src):
        print(f"Error: source dir not found: {src}", file=sys.stderr)
        return 1
    os.makedirs(dst, exist_ok=True)

    copied, unchanged = [], []
    for rel, sp in _iter_sync_files(src):
        dp = os.path.join(dst, rel)
        if os.path.isfile(dp) and filecmp.cmp(sp, dp, shallow=False):
            unchanged.append(rel)
            continue
        copied.append(rel)
        if not args.dry_run:
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            shutil.copy2(sp, dp)

    result = {
        "src": src,
        "dst": dst,
        "dry_run": bool(args.dry_run),
        "copied": copied,
        "unchanged_count": len(unchanged),
    }
    _write_json(args.output, result)
    tag = "[DRY-RUN] " if args.dry_run else ""
    print(f"{tag}Sync complete: {len(copied)} file(s) updated, {len(unchanged)} unchanged.")
    for f in copied[:20]:
        print(f"  -> {f}")
    if len(copied) > 20:
        print(f"  ... and {len(copied) - 20} more")
    if args.output:
        print(f"Report written to: {os.path.abspath(args.output)}")
    return 0


def cmd_smoke(args):
    exe_src = os.path.abspath(args.exe)
    if not os.path.isfile(exe_src):
        print(f"Error: exe not found: {exe_src}", file=sys.stderr)
        return 1

    tmp_dir = tempfile.mkdtemp(prefix="tc_island_smoke_")
    proc = None
    result = {"exe": exe_src, "port": args.port, "ok": False}
    try:
        exe_dst = os.path.join(tmp_dir, os.path.basename(exe_src))
        shutil.copy2(exe_src, exe_dst)
        proc = subprocess.Popen([exe_dst, "--port", str(args.port), "--no-browser"], cwd=tmp_dir)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{args.port}"

        state = None
        for _ in range(40):
            time.sleep(0.3)
            try:
                state = json.loads(opener.open(f"{base}/api/state", timeout=1).read().decode("utf-8"))
                break
            except Exception:
                pass
        if state is None:
            raise RuntimeError(f"exe did not start HTTP server on {base} within timeout")

        srv = state.get("server") or {}
        if not srv.get("frozen"):
            raise RuntimeError(f"expected server.frozen=True, got: {srv}")

        devs = json.loads(opener.open(f"{base}/api/devices", timeout=6).read().decode("utf-8"))
        slog = json.loads(opener.open(f"{base}/api/server_log", timeout=2).read().decode("utf-8"))
        log_exists = bool(slog.get("path") and os.path.isfile(slog["path"]))
        if not log_exists:
            raise RuntimeError(f"server.log not created: {slog.get('path')}")

        req = urllib.request.Request(
            f"{base}/api/shutdown", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        bye = json.loads(opener.open(req, timeout=3).read().decode("utf-8"))
        rc = proc.wait(timeout=8)
        if rc != 0:
            raise RuntimeError(f"exe exited with non-zero code: {rc}")

        result.update({
            "ok": True,
            "frozen": srv.get("frozen"),
            "devices_count": len(devs.get("devices") or []),
            "log_lines": len(slog.get("lines") or []),
            "log_file_exists": log_exists,
            "shutdown_ok": bool(bye.get("ok")),
            "exit_code": rc,
        })
    except Exception as e:
        result["error"] = str(e)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                pass
    finally:
        time.sleep(0.3)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    _write_json(args.output, result)
    if result["ok"]:
        print(f"Smoke PASSED | frozen={result['frozen']} | log_lines={result['log_lines']} | exit_code={result['exit_code']}")
    else:
        print(f"Smoke FAILED | {result.get('error')}", file=sys.stderr)
    if args.output:
        print(f"Report written to: {os.path.abspath(args.output)}")
    return 0 if result["ok"] else 1


def main():
    ap = argparse.ArgumentParser(description="埋点小岛（trackcheck）防回归校验与安全同步工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_aud = sub.add_parser("audit", help="Python 语法编译 + 配色红线扫描 + 静态资源检查")
    p_aud.add_argument("--src", default=DEFAULT_SRC, help="trackcheck 源码目录")
    p_aud.add_argument("--output", required=True, help="输出 JSON 报告路径")

    p_syn = sub.add_parser("sync", help="安全同步源码到 TrackDemo1/trackcheck（排除个人配置与二进制）")
    p_syn.add_argument("--src", default=DEFAULT_SRC, help="开发源码目录")
    p_syn.add_argument("--dst", default=DEFAULT_DST, help="Git 仓库内目标目录")
    p_syn.add_argument("--dry-run", action="store_true", help="仅预览变更不实际写入")
    p_syn.add_argument("--output", required=True, help="输出 JSON 报告路径")

    p_smk = sub.add_parser("smoke", help="临时目录冒烟验证无黑框 exe 的启动、日志与优雅退出")
    p_smk.add_argument("--exe", default=os.path.join(DEFAULT_SRC, "dist", "埋点小岛", "埋点小岛.exe"), help="待测 exe 路径")
    p_smk.add_argument("--port", type=int, default=8694, help="冒烟测试监听端口")
    p_smk.add_argument("--output", required=True, help="输出 JSON 报告路径")

    args = ap.parse_args()
    fn = {"audit": cmd_audit, "sync": cmd_sync, "smoke": cmd_smoke}[args.cmd]
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
