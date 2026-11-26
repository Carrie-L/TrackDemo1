#!/usr/bin/env python3
"""打包 埋点小岛 Windows 单文件 exe 及分发压缩包。"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "埋点小岛"
ZIP_NAME = "埋点小岛_Windows版.zip"
SAMPLE_PACK_NAME = "示例规则包（含数据源样本）.yaml"
DATA_DIRNAME = "埋点小岛数据"
DATA_SKIP_DIRS = {"reports", ".history", "__pycache__", "snapshots", "flows"}
DATA_SKIP_EXT = (".bak", ".log", ".py", ".pyc", ".tmp")


def _data_score(d):
    """目录里是否有「真的规则数据」（不是首次运行生成的空骨架）"""
    p = os.path.join(d or "", "trackcheck.yaml")
    if not os.path.isfile(p):
        return 0
    try:
        txt = open(p, encoding="utf-8").read()
    except OSError:
        return 0
    has_ds = os.path.isdir(os.path.join(d, "datasources")) and any(
        not x.startswith(".") for x in os.listdir(os.path.join(d, "datasources")))
    return len(txt) + (1000 if has_ds else 0) if (len(txt) > 600 or has_ds) else 0


def _data_mtime(d):
    latest = 0
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in DATA_SKIP_DIRS]
        for fn in files:
            try:
                latest = max(latest, os.path.getmtime(os.path.join(root, fn)))
            except OSError:
                pass
    return latest


def copy_data_dir(src, dst, merge=False, keep_reports=False):
    """复制规则数据目录：trackcheck.yaml(+include) / datasources（不含 .history）/ event_dict.json / docs"""
    skip_dirs = set(DATA_SKIP_DIRS) - ({"reports"} if keep_reports else set())
    if os.path.isdir(dst) and not merge:
        shutil.rmtree(dst)
    for root, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if x not in skip_dirs]
        rel = os.path.relpath(root, src)
        for fn in files:
            if fn.lower().endswith(DATA_SKIP_EXT) and not keep_reports:
                continue
            if rel == "." and not keep_reports and not fn.lower().endswith((".yaml", ".yml", ".json", ".md")):
                continue
            target = os.path.join(dst, rel, fn) if rel != "." else os.path.join(dst, fn)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(root, fn), target)


def make_icon(src_img, out_ico):
    """把 mascot.jpg 裁成圆形多尺寸 .ico（需要 Pillow；没有则跳过）。"""
    if not os.path.isfile(src_img):
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("提示：未安装 Pillow，跳过自定义 exe 图标（不影响功能）")
        return None
    im = Image.open(src_img).convert("RGBA")
    side = min(im.size)
    left = (im.width - side) // 2
    top = (im.height - side) // 2
    im = im.crop((left, top, left + side, top + side)).resize((512, 512), Image.LANCZOS)
    mask = Image.new("L", (512, 512), 0)
    ImageDraw.Draw(mask).ellipse((12, 12, 500, 500), fill=255)
    im.putalpha(mask)
    im.save(out_ico, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    return out_ico


def export_sample_pack(sample_spec, dst_yaml, work_dir):
    if not sample_spec or not os.path.isfile(sample_spec):
        return False
    sys.path.insert(0, HERE)
    import trackcheck_web as tcw
    tmp_reports = os.path.join(work_dir, "_sample_reports")
    app = tcw.App(sample_spec, out_dir=tmp_reports)
    res = app.api_export({
        "sources": "1",
        "samples": "1",
        "author": "示例",
        "note": "埋点小岛示例：我的页卡片 + 历史记录页",
    })
    with open(dst_yaml, "wb") as f:
        f.write(res.data)
    return True


def zip_folder(folder, zip_path):
    base = os.path.dirname(folder)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _, files in os.walk(folder):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, base)
                zf.write(full, arc)


def main():
    ap = argparse.ArgumentParser(description="打包 埋点小岛 Windows exe 及分享压缩包")
    ap.add_argument("--out", default=os.path.join(HERE, "dist"), help="输出目录（默认 dist/）")
    ap.add_argument("--work", default=os.path.join(HERE, "build"), help="临时构建目录（默认 build/）")
    ap.add_argument("--sample-spec", default=os.path.join(HERE, "samples", "trackcheck.yaml"),
                    help="生成示例规则包所用的 trackcheck.yaml（传入空串跳过）")
    ap.add_argument("--data-from", default=os.path.join(HERE, "samples"),
                    help="随包携带的数据目录（含 trackcheck.yaml / datasources / event_dict.json / docs），默认 samples/")
    ap.add_argument("--no-data", dest="with_data", action="store_false",
                    help="不携带规则与数据源（只打干净的 exe 包，适合发给外部同事）")
    ap.add_argument("--refresh-data", action="store_true",
                    help="强制用 --data-from 覆盖 dist/埋点小岛/埋点小岛数据（原数据会先备份到 build/data_backup/）")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    work_dir = os.path.abspath(args.work)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(work_dir, exist_ok=True)

    # 1. 检查 PyInstaller
    chk = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                         capture_output=True, text=True)
    if chk.returncode != 0:
        print("未安装 PyInstaller，请先运行：pip install pyinstaller")
        sys.exit(1)
    print(f"PyInstaller {chk.stdout.strip()}")

    # 2. 图标
    ico = make_icon(os.path.join(HERE, "web", "static", "mascot.jpg"),
                    os.path.join(work_dir, "icon.ico"))

    # 3. 打包单文件无黑框 exe（--windowed：静默后台启动，通过网页查看日志与退出）
    dist_exe_dir = out_dir
    target_exe = os.path.join(out_dir, f"{APP_NAME}.exe")
    if os.path.isfile(target_exe):
        try:
            os.remove(target_exe)
        except PermissionError:
            print(f"提示：{target_exe} 正在运行被占用，改在临时目录生成最新 exe 并更新压缩包…")
            dist_exe_dir = os.path.join(work_dir, "dist_fresh")
            if os.path.isdir(dist_exe_dir):
                shutil.rmtree(dist_exe_dir)
            os.makedirs(dist_exe_dir, exist_ok=True)

    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", APP_NAME,
        "--distpath", dist_exe_dir,
        "--workpath", os.path.join(work_dir, "pyi"),
        "--specpath", work_dir,
        "--paths", HERE,
        "--add-data", f"{os.path.join(HERE, 'web', 'index.html')}{sep}web",
        "--add-data", f"{os.path.join(HERE, 'web', 'app.js')}{sep}web",
        "--add-data", f"{os.path.join(HERE, 'web', 'static')}{sep}web{os.sep}static",
        "--exclude-module", "tkinter",
        "--exclude-module", "PIL",
        "--exclude-module", "numpy",
        "--exclude-module", "matplotlib",
    ]
    if ico:
        cmd += ["--icon", ico]
    cmd.append(os.path.join(HERE, "trackcheck_web.py"))

    print("正在打包无黑框 exe ...")
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        sys.exit(r.returncode)

    exe_path = os.path.join(dist_exe_dir, f"{APP_NAME}.exe")
    if not os.path.isfile(exe_path):
        print("未找到生成的 exe:", exe_path)
        sys.exit(1)

    # 4. 组装分发目录与 zip（先在 build/ 里拼好暂存目录，再增量同步到 dist/埋点小岛，绝不整目录删除用户数据）
    pkg_dir = os.path.join(out_dir, APP_NAME)
    os.makedirs(pkg_dir, exist_ok=True)
    stage_root = os.path.join(work_dir, "pkg_stage")
    stage = os.path.join(stage_root, APP_NAME)
    if os.path.isdir(stage_root):
        shutil.rmtree(stage_root)
    os.makedirs(stage, exist_ok=True)

    shutil.copy2(exe_path, os.path.join(stage, f"{APP_NAME}.exe"))
    readme = os.path.join(HERE, "使用说明.txt")
    if os.path.isfile(readme):
        shutil.copy2(readme, os.path.join(stage, "使用说明.txt"))
    if args.sample_spec:
        ok = export_sample_pack(os.path.abspath(args.sample_spec),
                                os.path.join(stage, SAMPLE_PACK_NAME), work_dir)
        if ok:
            print(f"已附带示例包：{SAMPLE_PACK_NAME}")

    # 4.1 随包携带规则 / 数据源 / 测试用例（埋点小岛数据），解压即用，重新打包也不会丢
    pkg_data = os.path.join(pkg_dir, DATA_DIRNAME)
    seed_src = os.path.abspath(args.data_from) if args.data_from else ""
    chosen = None
    if args.with_data:
        cands = []
        if seed_src and _data_score(seed_src):
            cands.append((_data_mtime(seed_src), seed_src, "开发数据"))
        if _data_score(pkg_data):
            cands.append((_data_mtime(pkg_data), pkg_data, "分发目录里已有的数据"))
        if args.refresh_data and seed_src and _data_score(seed_src):
            chosen = (seed_src, "开发数据（--refresh-data 强制）")
        elif cands:
            cands.sort(reverse=True)
            chosen = (cands[0][1], cands[0][2])
        if chosen:
            copy_data_dir(chosen[0], os.path.join(stage, DATA_DIRNAME))
            print(f"已随包携带数据：{chosen[1]} ← {chosen[0]}")
        else:
            print("提示：没有找到可携带的规则数据（--data-from 指向的目录里没有 trackcheck.yaml）")

    # 4.2 同步到 dist/埋点小岛：exe/说明/示例包覆盖；数据目录只在缺失或 --refresh-data 时写入（旧数据先备份）
    for fn in os.listdir(stage):
        sp, dp = os.path.join(stage, fn), os.path.join(pkg_dir, fn)
        if fn == DATA_DIRNAME:
            continue
        try:
            shutil.copy2(sp, dp)
        except PermissionError:
            print(f"提示：{dp} 正在运行被占用，跳过替换（压缩包里已是最新版本，退出旧程序后可重新打包或直接用压缩包）")
    stage_data = os.path.join(stage, DATA_DIRNAME)
    if os.path.isdir(stage_data) and chosen and os.path.abspath(chosen[0]) != os.path.abspath(pkg_data):
        if _data_score(pkg_data):
            bak = os.path.join(work_dir, "data_backup", time.strftime("%Y%m%d_%H%M%S"))
            copy_data_dir(pkg_data, bak, keep_reports=True)
            print(f"已备份分发目录原数据到：{bak}")
        copy_data_dir(stage_data, pkg_data, merge=True)

    zip_path = os.path.join(out_dir, ZIP_NAME)
    if os.path.isfile(zip_path):
        os.remove(zip_path)
    zip_folder(stage, zip_path)

    mb_exe = os.path.getsize(exe_path) / (1024 * 1024)
    mb_zip = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"\n打包完成：")
    print(f"  exe : {exe_path} ({mb_exe:.2f} MB)")
    print(f"  目录: {pkg_dir}")
    print(f"  压缩: {zip_path} ({mb_zip:.2f} MB)")


if __name__ == "__main__":
    main()
