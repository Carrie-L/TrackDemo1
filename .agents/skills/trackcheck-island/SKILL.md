---
name: trackcheck-island
description: >-
  埋点小岛（trackcheck，Android 埋点调试与自动校验本地可视化工具）的总架构、规则引擎、
  动森海岛风 UI 规范与打包发布维护手册。凡是要修改、修复、扩展埋点小岛——新增数据源类型
  （如 merged_json 兜底合并）、模板绑定接口与 OkHttp 实时抓包、校验引擎与人工复核逻辑、
  独立 HTML 报告、AI 提示词、调整海岛风 UI、打包无黑框 Windows exe、同步到 TrackDemo1
  并推送分支——都必须先读这份 skill。
---

# 🏝️ 埋点小岛（trackcheck）总架构与维护手册

> 核心定位：**每个点位只需配置一次规则，之后每次测试自动复用**。
> 视觉风格：**阳光海岛动森风**（波光粼粼、明亮柔和、暖可可字色 + 晴空蓝 + 叶绿 + 樱花粉，**绝对禁止深蓝/老蓝/黑蓝与暗沉中老年色**）。
> 运行形态：**Windows 无黑框单文件 `.exe`**，后台静默启动、网页内查看运行/报错日志并可一键优雅退出。

## Dependencies

- **`windows-local-webapp-craft`**：本项目的 Windows PyInstaller `--windowed` 无黑框加固、`ServerLogger` 双保险日志、`CREATE_NO_WINDOW` 子进程防护、治愈系 UI 审美红线及打包避坑均复用该通用 skill。做底层无黑框改造、日志机制或视觉精修前可一并查阅。

## 0. 基本信息与双目录约束

| 项 | 值 |
| --- | --- |
| 日常开发目录 | `C:\Users\windows\.gemini\antigravity\scratch\trackcheck` |
| Git 同步仓库 | `C:\Users\windows\.gemini\antigravity\scratch\TrackDemo1`（子目录 `trackcheck/`） |
| 远端与分支 | `https://github.com/Carrie-L/TrackDemo1.git`；默认推送到工作分支 **`小岛`**（`main` 未经明确指示不触碰） |
| 技术栈 | Python 3.8+ 标准库 + PyYAML；前端 Alpine.js + Tailwind CSS v3（`web/src/input.css` → `web/static/app.css`）；PyInstaller 单文件打包 |
| 本地预览 | `python trackcheck_web.py --spec samples\trackcheck.yaml --port 8686 --no-browser` |
| 样式构建 | `cmd /c web\build_css.cmd`（每次改 `web/index.html`、`web/app.js`、`web/src/input.css` 里的 Tailwind 类后必须执行） |
| 打包 exe/zip | `python build_exe.py`（输出 `dist/埋点小岛/` 与 `dist/埋点小岛_Windows版.zip`） |

> [!IMPORTANT] **两条硬红线**
> 1. **绝不覆盖个人规则文件**：同步 `scratch/trackcheck` → `TrackDemo1/trackcheck` 时，必须排除 `samples/trackcheck.yaml`、个人 `datasources/`、`reports/`、`build/`、`dist/` 和 `.exe`/`.zip` 二进制产物（使用下方 `island_ops.py sync` 自动严格过滤）。
> 2. **绝不强杀用户正在运行的 `dist\埋点小岛.exe`**：`build_exe.py` 已内置文件锁兜底，目标 `.exe` 被占用时会自动在 `build/dist_fresh/` 生成最新 `.exe` 并组装更新 `dist/埋点小岛_Windows版.zip`。

## 1. 架构总览

```text
trackcheck/
├── trackcheck.py          核心 CLI & 校验引擎：Spec、Sources(json/sqlite/sqlite_json/merged_json)、
│                          Checker、Capture(adb logcat + on_line)、build_html 独立报告、find_adb()、popen_kw()
├── trackcheck_web.py      本地 Web 服务：SpecStore、LiveSession(DB快照 + OkHttp接口自动抓包 + 5s回溯重校)、
│                          ApiCaptureSession、ServerLogger(双保险日志)、App API、无黑框启动/MessageBoxW/优雅退出
├── build_exe.py           PyInstaller --windowed 单文件打包 + 示例规则包导出 + Windows zip 组装
├── build_exe.cmd          Windows 双击打包脚本（CRLF + if errorlevel 1 goto fail）
├── 使用说明.txt            随分发包附带的用户说明（无黑框启动/日志/退出/adb/分享）
├── web/
│   ├── index.html         单页全部视图（#home #sources #live #rules #infer #cases #reports #evdict #share + 弹窗）
│   ├── app.js             Alpine.js 全部前端状态、交互、复核、日志抽屉与退出逻辑
│   ├── src/input.css      Tailwind 源码（色板、自定义绿框白勾 checkbox、卡片/胶囊组件）
│   ├── build_css.cmd      编译 web/src/input.css → web/static/app.css
│   └── static/
│       ├── app.css        编译产物（改完 Tailwind 类必须重建）
│       ├── icons.svg      SVG 线性图标精灵（含 scroll-text、power 等）
│       ├── mascot.jpg     小芽头像（兼 exe 圆形图标源图）
│       ├── hero.jpg       海岛欢迎横幅底图
│       └── icons/*.png    3D 治愈系贴纸图标（shell、sprout、fail、bud、data、template、maple、hydrangea 等）
└── samples/               本地示例规则与测试数据（trackcheck.yaml 个人化调试用，不同步覆盖仓库）
```

## 2. 核心引擎与功能设计（详见 references）

- **规则引擎、合并兜底与接口抓包**：完整 YAML 协议、边界行为和状态机见 [references/rules-and-data-model.md](references/rules-and-data-model.md)。
  - `merged_json`：支持主源优先级列表 `primary: [myPageV3, mine_api_cache]` + `fallback` + `path` + `key` + `empty_field: res` + `fallback_keys`，**严格保持接口下发顺序**，当 `res` 为空数组或主源缺失该 key 时自动降级到兜底数据。
  - 模板 `api` 绑定与继承：多点位共用一个接口时直接在模板定义 `api`，子点位 `extends` 自动继承；实时调试时 `resolve_event_apis()` 自动汇总清单中所有点位/模板/数据源绑定的接口并从 OkHttp logcat 抓最新 JSON，同时回溯 5 秒内及失败事件自动重校。
  - 人工复核联动：支持子字段及顶层 `__err__:<idx>` 逐项改判；当一个事件的所有子字段都已 `PASS` 时，后端 `api_report_review` 自动将事件顶层 `errors` 归为通过，重算事件状态与报告 `summary`。
  - 独立 HTML 报告：`/report_html/<name>.html` 优先读取同名 `.json` 实时调用 `tc.build_html(rep)` 渲染，确保历史报告也能立刻享受最新筛选器（`有问题的 / 通过的 / 全部` 单选 + `过滤未配置` 复选）。
- **视觉与 UI 设计规范**：完整色板、控件规范和 3D 图标约定见 [references/ui-design-system.md](references/ui-design-system.md)。

## 3. Quick Start & Utility Scripts

本 skill 内置零依赖 CLI 工具 [scripts/island_ops.py](scripts/island_ops.py)（标准库实现，支持 `--output <path>` 将 JSON 明细写入文件，终端只打印简明摘要）。

### 3.1 代码与配色防回归自检（`audit`）
检查 Python 语法编译、扫描 `web/` 中是否误引入违禁深蓝/老蓝、检查必需静态图标和 `app.css` 更新时间：
```powershell
uv run --no-project C:\Users\windows\.gemini\config\skills\trackcheck-island\scripts\island_ops.py audit `
  --src C:\Users\windows\.gemini\antigravity\scratch\trackcheck `
  --output C:\Users\windows\.gemini\antigravity\scratch\trackcheck\build\audit_report.json
```
*(无 `uv` 环境可直接用 `python` 运行同等参数。)*

### 3.2 安全同步到 Git 仓库（`sync`）
把开发目录中的核心源码/静态资源同步到 `TrackDemo1/trackcheck`（严格跳过 `samples/trackcheck.yaml`、`build/`、`dist/`、`reports/`、`datasources/` 等私密/二进制文件），并输出变更摘要：
```powershell
uv run --no-project C:\Users\windows\.gemini\config\skills\trackcheck-island\scripts\island_ops.py sync `
  --src C:\Users\windows\.gemini\antigravity\scratch\trackcheck `
  --dst C:\Users\windows\.gemini\antigravity\scratch\TrackDemo1\trackcheck `
  --output C:\Users\windows\.gemini\antigravity\scratch\trackcheck\build\sync_report.json
```
*(可先加 `--dry-run` 预览将同步的文件列表。)*

### 3.3 无黑框 `.exe` 全生命周期冒烟（`smoke`）
复制打包好的 `埋点小岛.exe` 到系统临时目录，实测无黑框启动、`/api/state`（`frozen=True`）、`/api/devices`（`adb` 无弹窗）、`/api/server_log`（`server.log` 落盘）与 `/api/shutdown`（干净返回码 `0`）：
```powershell
uv run --no-project C:\Users\windows\.gemini\config\skills\trackcheck-island\scripts\island_ops.py smoke `
  --exe C:\Users\windows\.gemini\antigravity\scratch\trackcheck\dist\埋点小岛\埋点小岛.exe `
  --port 8694 `
  --output C:\Users\windows\.gemini\antigravity\scratch\trackcheck\build\smoke_report.json
```

## 4. 标准改动与发布 SOP

每次修改埋点小岛，按以下 6 步闭环执行：

1. **改代码**：在 `scratch/trackcheck` 下修改 Python 或 `web/` 文件。
2. **编 CSS + 静态自检**：
   - 若改过 `web/` 样式或 HTML 类名 → 运行 `cmd /c web\build_css.cmd`。
   - 运行 `island_ops.py audit` 确保 0 语法错误、0 违禁深蓝/老蓝。
3. **重启 8686 并浏览器视觉验收**：
   - 重启 `python trackcheck_web.py --spec samples\trackcheck.yaml --port 8686 --no-browser`。
   - 委派 `browser` 子代理硬刷新 `http://127.0.0.1:8686/`，验证改动页面（含窄屏检查）、0 console error、0 断图。
   - 测试「运行日志」和「休息」确认弹窗时，**切记不要真的点「安心退出服务」**，以免把 `8686` 关掉。
4. **重新打包并冒烟（涉及打包或发布时）**：
   - 运行 `python build_exe.py` 生成最新无黑框 `埋点小岛.exe` 与 `埋点小岛_Windows版.zip`。
   - 运行 `island_ops.py smoke` 验证打包产物。
5. **安全同步 & 推送仓库**：
   - 运行 `island_ops.py sync`。
   - 在 `TrackDemo1` 检查 `git status`（确认未混入个人 yaml 或二进制），提交并推送到目标分支（默认 `git push origin 小岛`）。

## 5. 高频踩坑备忘

1. **`badCount` 误把 `INFO` 算成「1 个问题」**：
   - 未定义字段的提示属于 `status == "INFO"`，统计红标问题数时必须**只统计 `FAIL` 和 `WARN`**（前端 `badCount(r)` 与独立 HTML 报告逻辑保持一致）。
2. **手动改判子项全通过后，事件头仍显示失败**：
   - 原因：事件顶层 `r.errors` 可能挂着一条 `assert` 或上下文错误。`api_report_review` 在所有子字段均 `PASS` 后必须同步清理/标记顶层错误并重算 `summary`。
3. **`server.log` 混入抓包日志列表**：
   - `App.sessions()` 扫描 `reports/*.log` 时必须显式排除 `server.log`，避免出现在「离线校验 / 历史日志」里。
4. **浏览器刷新触发 `WinError 10053` 假报警**：
   - 已在 `trackcheck_web.py` 用 `_CLIENT_DISCONNECT` 过滤 `(BrokenPipeError, ConnectionResetError, ConnectionAbortedError)`；今后新增路由或流式响应也要注意捕获断连异常，避免给左下角「运行日志」刷假红点。
5. **点击文件路径无法唤起资源管理器（受限桌面环境）**：
   - `api_open_path` 返回 `sandbox: true` 时，前端直接复制到剪贴板并清晰提示「已复制路径：xxx」，不再展示误导性的“已打开资源管理器”。
6. **Alpine 状态读取顺序陷阱**：
   - 如 `openServerLog()` 中若先清零 `this.st.server.err_count = 0`，后面再判断 `this.srvErrCount > 0` 会永远为 `false`。类似“标记已读 + 根据旧状态决定 UI 默认 tab”的逻辑，务必先用局部变量缓存旧值（`const hadUnread = this.srvErrCount > 0`）。
