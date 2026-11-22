#!/usr/bin/env python3
"""打包 埋点小岛 Windows 单文件 exe 及分发压缩包。"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "埋点小岛"
ZIP_NAME = "埋点小岛_Windows版.zip"
SAMPLE_PACK_NAME = "示例规则包（含数据源样本）.yaml"


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

    # 3. 打包单文件 exe
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", APP_NAME,
        "--distpath", out_dir,
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

    print("正在打包 exe ...")
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        sys.exit(r.returncode)

    exe_path = os.path.join(out_dir, f"{APP_NAME}.exe")
    if not os.path.isfile(exe_path):
        print("未找到生成的 exe:", exe_path)
        sys.exit(1)

    # 4. 组装分发目录与 zip
    pkg_dir = os.path.join(out_dir, APP_NAME)
    if os.path.isdir(pkg_dir):
        shutil.rmtree(pkg_dir)
    os.makedirs(pkg_dir, exist_ok=True)

    shutil.copy2(exe_path, os.path.join(pkg_dir, f"{APP_NAME}.exe"))
    readme = os.path.join(HERE, "使用说明.txt")
    if os.path.isfile(readme):
        shutil.copy2(readme, os.path.join(pkg_dir, "使用说明.txt"))
    if args.sample_spec:
        ok = export_sample_pack(os.path.abspath(args.sample_spec),
                                os.path.join(pkg_dir, SAMPLE_PACK_NAME), work_dir)
        if ok:
            print(f"已附带示例包：{SAMPLE_PACK_NAME}")

    zip_path = os.path.join(out_dir, ZIP_NAME)
    if os.path.isfile(zip_path):
        os.remove(zip_path)
    zip_folder(pkg_dir, zip_path)

    mb_exe = os.path.getsize(exe_path) / (1024 * 1024)
    mb_zip = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"\n打包完成：")
    print(f"  exe : {exe_path} ({mb_exe:.2f} MB)")
    print(f"  目录: {pkg_dir}")
    print(f"  压缩: {zip_path} ({mb_zip:.2f} MB)")


if __name__ == "__main__":
    main()
