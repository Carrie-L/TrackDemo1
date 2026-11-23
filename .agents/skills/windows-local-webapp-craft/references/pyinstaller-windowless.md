# PyInstaller `--windowed`（无黑框）防雷与工程模板

在 Windows 上把 Python 本地 Web 服务打包成无黑框单文件 `.exe` 时，必须同时处理以下 **6 个核心环节**，缺一不可。

---

## 1. 启动期保护 `sys.stdout / sys.stderr`

PyInstaller 使用 `--windowed`（`runw.exe`）时，进程没有附着控制台，`sys.stdout`、`sys.stderr`、`sys.stdin` 均为 `None`。

**必须做的两处防护：**
1. 所有模块顶层（尤其是被 import 的核心模块）严禁无条件调用 `os.system("")` 或 `sys.stdout.reconfigure()`：
```python
if os.name == "nt" and sys.stdout is not None:
    os.system("")
for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
```
2. 入口第一时间安装 `ServerLogger` 替换 `sys.stdout` / `sys.stderr`，兼容原流为 `None` 的情况，避免任何三方库或 `print()` 抛 `AttributeError`。

---

## 2. 彻底禁止子进程闪黑框（`CREATE_NO_WINDOW`）

主进程无控制台后，Windows 启动任何命令行子进程（如 `adb.exe`、`git.exe`）都会默认新建一个黑色控制台窗口并迅速闪灭。

**统一 helper：**
```python
def popen_kw():
    """Windows 无黑框模式下禁止命令行子进程闪烁控制台窗口。"""
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if os.name == "nt" else {}
```
- 所有 `subprocess.run(...)` / `subprocess.Popen(...)` 调用命令行工具时统一传 `**popen_kw()`。
- **例外**：唤起系统 GUI（如 `explorer.exe /select,"..."`）不要加 `CREATE_NO_WINDOW`。

---

## 3. 双保险日志系统（内存缓冲 + 落盘 `server.log` + 启动弹窗）

### 运行期 `ServerLogger` 要点
- 使用 `threading.RLock()` + `collections.deque(maxlen=500)` 保存最近日志。
- 自动剥离 ANSI 颜色转义码（`\x1b\[[0-9;]*m`）。
- 绑定到 `<data_or_out_dir>/server.log`，启动时若文件超过 `512 KB` 自动保留后半段，防止长期使用无限膨胀。
- **未读异常计数 (`err_count`)**：同一段连续 Traceback 只计 1 次，避免一个异常让徽标显示几十条；打开 `/api/server_log?seen=1` 后清零未读数。
- **防噪音**：`BaseHTTPRequestHandler.log_message` 置空（不打印正常 200 轮询），且静默吞掉客户端正常断连：
```python
_CLIENT_DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
```
  在 `H.handle()`、`H._send()`、`H._route()` 和 `Server.handle_error()` 全部忽略 `_CLIENT_DISCONNECT`，确保日志里只出现**真正的业务或服务异常**。
- **避免日志自污染**：如果系统会扫描输出目录中的 `*.log`（例如会话抓包日志列表），务必排除 `server.log`。

### 启动期致命错误兜底
无黑框模式下绝不能写 `input("按回车关闭…")`（`sys.stdin` 为 `None` 会二次崩溃）。改为：
```python
def _notify_fatal_error(msg):
    # 1) 追加写入 server.log
    # 2) FROZEN 且 Windows 时弹出原生 MessageBoxW
    if FROZEN and os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, body[:1600], "程序启动遇到问题", 0x10)
```

---

## 4. 单实例唤醒 & 网页内优雅退出

### 单实例检测
- 启动时探测 `start_port .. start_port + 19` 的 `/api/state`（务必使用 `urllib.request.build_opener(ProxyHandler({}))` 绕过系统代理，直连 `127.0.0.1`）。
- 如果发现已有实例指向同一份数据/规则文件，直接 `webbrowser.open(running_url)` 并退出当前新进程。

### Windows 端口检测坑
`ThreadingHTTPServer` 必须显式设置：
```python
class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # Windows 上 SO_REUSEADDR 会抢占已绑定端口
    daemon_threads = True
```

### 优雅退出接口 `POST /api/shutdown`
1. 立即停止正在运行的后台抓取/子进程（如 `logcat`、`adb`）。
2. `flush` 日志缓冲区。
3. 起一个 `daemon=True` 的子线程 `time.sleep(0.35)` 后执行 `HTTP_SERVER.shutdown()` 与 `os._exit(0)`，保证 HTTP 响应先安全送达前端。
4. 前端收到响应后设置 `srvOff = true`，停止所有定时轮询并显示温和的欢送页。

---

## 5. 打包脚本与文件锁兜底

用户电脑上可能正开着旧版 `dist\App.exe`，直接让 PyInstaller 覆盖会报 `PermissionError: [WinError 5]`。

**推荐做法（不强杀用户进程）：**
```python
dist_exe_dir = out_dir
target_exe = os.path.join(out_dir, f"{APP_NAME}.exe")
if os.path.isfile(target_exe):
    try:
        os.remove(target_exe)
    except PermissionError:
        dist_exe_dir = os.path.join(work_dir, "dist_fresh")
        shutil.rmtree(dist_exe_dir, ignore_errors=True)
        os.makedirs(dist_exe_dir, exist_ok=True)
```
然后把生成的最新 `exe` 组装进分发子目录与 `.zip`，这样无论旧版是否在运行，打包脚本都能 100% 成功产出最新压缩包。

---

## 6. `.exe` 冒烟验证标准流程

每次重新打包无黑框 `.exe` 后，在系统临时目录跑一次端到端冒烟：
1. 复制新 `exe` 到 `tempfile.mkdtemp()`；
2. 以 `--port 8694 --no-browser` 启动子进程；
3. 轮询 `GET /api/state` 确认 `frozen == True`；
4. 调用一次会触发子进程的接口（如 `GET /api/devices`）确认无崩溃；
5. 调用 `GET /api/server_log` 确认返回日志行且 `server.log` 已落盘；
6. 调用 `POST /api/shutdown` 确认进程在几秒内以返回码 `0` 干净退出；
7. 清理临时目录。
