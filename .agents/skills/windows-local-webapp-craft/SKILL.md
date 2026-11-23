---
name: windows-local-webapp-craft
description: >-
  跨项目通用的本地 Python + 轻量前端（Alpine.js / Tailwind）可视化工具工程与审美规范。
  沉淀了 Windows PyInstaller 无黑框（--windowed）静默运行、后台运行/报错日志双保险、
  网页端优雅退出、Windows 子进程防闪窗、治愈系/动森海岛风高颜值 UI 准则与多轮实测踩坑。
  凡是要开发本地桌面化 Web 工具、打包 Windows 无黑框 exe、设计明亮柔和治愈系界面，
  或给本地工具补齐运行日志与退出机制时，优先读取本 skill。
---

# 🌴 Windows Local WebApp Craft · 本地桌面化 Web 工具工程与审美指南

## Overview

面向「Python 本地 HTTP 服务 + 轻量网页前端 + Windows 单文件无黑框 `.exe` 分发」这类桌面化小工具的全流程工程规范。目标：**双击即用零黑框、出错必留痕可排查、界面明亮治愈不土气、打包分发不踩坑**。

## Quick Start

启动一个新项目或改造现有本地 Web 工具时，按下面 4 条主线对照实施：

1. **无黑框架构**（详解见 [references/pyinstaller-windowless.md](references/pyinstaller-windowless.md)）：
   - `PyInstaller --onefile --windowed`；
   - 启动入口接管 `sys.stdout / sys.stderr`（兼容 `None`），写入内存环形缓冲 + `server.log`；
   - 所有子进程（`adb`、`git` 等）加 `CREATE_NO_WINDOW`（`0x08000000`）；
   - 提供 `GET /api/server_log`、`POST /api/server_log/clear`、`POST /api/shutdown`；
   - 启动致命异常写 `server.log` + 弹原生 `MessageBoxW`（严禁调 `input()`）。
2. **HTTP 服务健壮性**：
   - `ThreadingHTTPServer` 设 `allow_reuse_address = False`（Windows 必须关掉，否则会抢占已有端口而非顺延）；
   - `daemon_threads = True`；
   - 静默吞掉 `(BrokenPipeError, ConnectionResetError, ConnectionAbortedError)`，防止浏览器刷新/切换页签触发 `WinError 10053` 污染错误日志。
3. **治愈系高颜值 UI**（详解见 [references/cozy-pastel-ui.md](references/cozy-pastel-ui.md)）：
   - 严禁深蓝/老蓝/黑蓝（`R < 95` 且蓝色通道主导的冷暗蓝）与中老年暗沉棕灰；
   - 任何新加的系统控制按钮（运行日志、退出服务）必须**自然融入现有视觉体系**，绝不突兀堆砌开发者风黑框控件；
   - 长列表做日期分组与极淡水印，防止几十上百条记录时眼花。
4. **Windows 构建与真实浏览器验收**：
   - 打包脚本自动检测目标 `.exe` 文件锁（`PermissionError`），被运行时切到临时目录构建，确保分发 `.zip` 永远顺利生成；
   - 改完 UI 后必在真实浏览器中做多分辨率截图验收（检查折行、对齐、0 console error、0 断图），无黑框 `.exe` 必做临时目录启动 → API 探活 → `/api/shutdown` 退出码 `0` 冒烟测试。

## Workflow

### 1. 服务端无黑框加固清单
- [ ] 导入期检查 `sys.stdout is not None` 再调 `os.system("")` 或 `.reconfigure()`。
- [ ] 所有非 GUI 子进程统一传 `**popen_kw()`（Windows 下 `creationflags=subprocess.CREATE_NO_WINDOW`）。
- [ ] 安装 `ServerLogger` 接管标准输出/错误流，自动去除 ANSI 色码，写入 `deque(maxlen=500)` 和上限截断的 `server.log`。
- [ ] 单实例检测 `_find_running()`：重复双击 exe 时探测已运行端口，匹配同一数据目录则直接 `webbrowser.open()` 唤醒浏览器。
- [ ] `POST /api/shutdown` 先优雅停止后台抓取线程/子进程，返回 `{"ok": True}` 后延迟 `0.35s` 调 `HTTP_SERVER.shutdown()` + `os._exit(0)`。
- [ ] `if __name__ == "__main__":` 捕获启动期 `Exception` / 非零 `SystemExit`，落盘 `server.log` 并弹 Windows `MessageBoxW`。

### 2. 前端系统控制与错误感知
- [ ] 侧边栏底部卡片嵌入小型状态指示点（绿 = 正常，红脉冲 = 有未读异常）及两枚柔和胶囊：`运行日志` + `休息/退出`。
- [ ] `api()` 捕获 HTTP `>= 500` 时同步递增未读异常计数，并在错误 Toast 上附带「查看日志」一键跳转按钮。
- [ ] 运行日志弹窗提供：`服务正常/含异常` 胶囊、`server.log` 可点击路径、`全部 / 只看异常` 切换、刷新、复制、清空面板。
- [ ] 退出按钮必须二次确认（若有后台抓取任务在跑，提示先结束并出报告），确认后切到温和的全屏欢送页并停止所有前端 `setInterval` 轮询。

### 3. UI 视觉验收标准
- [ ] 运行配色扫描：确认无违禁深蓝/老蓝十六进制色值。
- [ ] 同时检查宽屏（`1440×900`）与窄屏（如 `1100×800`）下的分栏比例、筛选条折行与按钮防溢出。
- [ ] 确认全部可见 `<img>` 加载成功（`naturalWidth > 0`），控制台 0 报错，网络请求 0 失败。

## Common Mistakes

1. **只改了 `--windowed` 没管 `sys.stdout = None` 和 `adb` 子进程**：
   - 结果：程序启动即崩溃在 `sys.stdout.reconfigure()`，或者每次轮询 `adb devices` 屏幕上每隔 15 秒狂闪一次黑框。
   - 做法：按 [references/pyinstaller-windowless.md](references/pyinstaller-windowless.md) 一次性完成 `ServerLogger` + `popen_kw()`。
2. **没过滤浏览器断连 `WinError 10053`**：
   - 结果：用户一刷新页面，后端 `wfile.write` 抛 `ConnectionAbortedError`，被错误日志收集器当成严重报错，侧边栏动不动亮红点。
   - 做法：在 `BaseHTTPRequestHandler.handle / _send / _route` 与 `Server.handle_error` 静默忽略 `(BrokenPipeError, ConnectionResetError, ConnectionAbortedError)`。
3. **PowerShell 单行引号坑 & `.cmd` 中文括号坑**：
   - PowerShell 跑复杂 Python 校验时，带嵌套引号和反斜杠的单行 `python -c` 极易触发 `SyntaxError`——多行逻辑一律写成临时 `.py` 文件执行。
   - Windows `.cmd` 含 UTF-8 中文时，`if errorlevel 1 ( ... )` 括号块容易解析错乱导致退出码 0 仍落入失败分支——一律用 CRLF + `if errorlevel 1 goto fail`。
4. **强杀用户正在运行的旧 `.exe`**：
   - 结果：打断用户当前正在看或正在调试的会话。
   - 做法：绝不强杀用户进程；打包脚本遇到目标 `.exe` `PermissionError` 时自动转到临时目录生成新 `.exe` 并组装最新 `.zip`。
