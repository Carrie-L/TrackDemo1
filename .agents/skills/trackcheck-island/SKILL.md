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
  - 数据源绑定：用例 / 模板上的 `sources: [...]` 决定 AI 提示词带哪些数据源；`App.event_sources(eid)` 汇总「模板绑定 + 用例绑定 + 规则 context/from/sql/api 引用 + 合并源依赖」，用例页「复制整组 AI 提示词」走 `api_ai_prompt({case})`。
  - 实时调试：选了用例默认「本用例点位」视图（按清单顺序列出、未触发也显示），「合并重复上报」默认开（键 = 点位 ID + 全部字段值），对重复组人工判定会一次作用到整组 `seqs`；判定后自动跳到下一个待确认。
  - 数据库浏览器：独立窗口 `/#dbview?db=&t=`（只读：`mode=ro` 连接 + 仅允许 SELECT/WITH/PRAGMA/EXPLAIN），可 `postMessage({type:'tc-sql'})` 把 SQL 填回主窗口字段规则；字段规则「数据库」页有 SQL 向导。
  - 打包：`build_exe.py` 先在 `build/pkg_stage` 拼包、默认携带 `--data-from samples/` 的规则数据，增量同步到 `dist/埋点小岛`，**不再整目录删除** `埋点小岛数据`（`--refresh-data` 强制覆盖前自动备份，`--no-data` 打干净包）。
  - 测试用例（最高频页面之一，改动要格外稳）：
    - **草稿自动保存**：`saveCaseDraft()` 每秒 + 切换用例/离开页面/`beforeunload` 时把未保存修改写入 localStorage `tc_case_drafts`（键 = 用例名，新用例 = `__new__`）；`_caseDiffers()` 用「服务器版本经 `caseRows→rowsToExpect` 往返」对比判定 dirty，别再只靠手动 `dirty = true`。`openCase()` 有草稿就恢复并显示黄色「已恢复…」横幅（可丢弃）；`tc_case_last` 记住上次打开的用例，回到用例页/刷新自动打开。左列表给有草稿的用例挂「草稿」小胶囊，新用例草稿常驻列表顶部虚线行。
    - **复制用例**直接 `api/case/save` 存一份「xxx - 副本」，左侧立刻多一行并高亮；左列表悬停按钮 = 实时调试 + 删除（`delCase` 同步清草稿 / 解绑实时调试）。
    - **复制点位**存 `tc_copied_row`（跨标签页 `storage` 事件同步），在其他用例页面空白处 **Ctrl+V** 粘贴（实时调试页粘贴进待测清单）；已存在时 toast 提供「用复制的期望值覆盖」。
    - **批量设置只列 `ask: true`（看操作）字段**，其它字段有点位规则自动比对，不要列出来添乱。
    - **数据同步**：`refresh()` 时编辑器无未保存修改就跟随服务器（实时调试「存回用例」/导入/改 YAML）；`saveCase()` 后 `syncLivePlan()` 刷新实时调试同名用例的待测清单；`useCaseInLive` 遇到未保存修改先自动保存。
  - 校验报告：点位覆盖行可点 → `rep.evId` 筛选逐条明细（「只看：xxx ×」胶囊取消，计数同步）；每条明细底部和实时调试一样附「Logcat 原始日志」（`r.raw` + `line_no` + 复制）。
  - 文字可复制：`body` 允许 `user-select: text` 但 `caret-color: transparent; cursor: default`（不出现闪烁光标和 I 形鼠标）；按钮/summary/label/`.cursor-pointer` 禁选防误选，`.code/pre/.tbl td` 强制可选。
  - 点位 ID 一点即复制：给元素加 `:data-copy-id="id"` + `.id-copy`，`document` **捕获阶段**监听统一 `preventDefault + stopPropagation`，所以放在折叠按钮 / `<summary>` / 可点行里也不会触发折叠或筛选。
  - 数据库浏览器：新标签页 `/#dbview?db=&t=`（`window.open(url, '_blank')`，只读 `mode=ro` + 仅允许 SELECT/WITH/PRAGMA/EXPLAIN）；**按表保存成功 SQL 历史**（localStorage `tc_dbv_hist_v2` 以 `db::table` 分组）、SQL 输入框右上角一键清空、单击任意结果单元格复制值 / 单击表头复制列名 / 悬停单元格 `= 筛选` / `转 COUNT(*)`，并通过 `postMessage` + `tc_dbv_sql_pick` 双通道填回主页面字段规则。
  - 埋点字典文档列表与「模块」列：`api_evdict` 自动隐藏 `docs/` 目录中已删除的文档（不再显示划线记录），识别埋点文档中的「模块」列（含 Excel 纵向合并单元格自动沿用 `last_mod`），汇总 `modules` 供「测试用例」新建/编辑时**按模块一键导入整组点位**。
  - 右侧悬浮用例清单（`#live` 与 `#reports`）：采用与 JSON 预览弹窗一致的清爽卡片风（`!bg-cream`、`w-[216px]`、小 2 号字、无便利贴装饰/渐变条、带 `fold_right.png` 收起图标，标题栏可拖拽移动，勾选状态与报告覆盖表同步并持久化到 `tc_note_checks`）。
  - **数据随 exe 走**：`build_exe.py` 先选好数据 → 复制到 `build/seed_data` → `--add-data seed_data` 内置进 exe；`trackcheck_web._seed_from_exe()` 在默认数据目录为空骨架时释放（已有文件绝不覆盖，写 `.seeded_from_exe` 标记）。用户常直接双击 `dist\埋点小岛.exe`（数据目录是 `dist\埋点小岛数据`），以前那里只有空骨架，所以"打开什么都没有"。
  - 分享包 `api_export(evdict=1)` 附带 `event_dict` + `doc_files`，导入预览出现「埋点字典」「埋点文档」两类（字典只补对方没有的点位）。「导入 / 分享」页顶部「我的数据都在这里」读 `/api/data_info`。
  - 运行日志 = 用户操作日志：`_route` 成功后 `describe_action()` 把接口翻成一句人话记为 `ACT`，`ApiError` 记 `WARN`（"没成功：…"，不计入异常红点）；纯前端操作走 `this.act(text)` → `/api/log/act`。日志抽屉有「全部 / 我的操作 / 只看异常」。新增接口时顺手在 `describe_action` 补一句描述。
  - 布局：外层 `min-h-screen` + `main.flex-1` + `footer.mt-auto`，空页面页脚也贴底；运行日志 / 休息在页头右上角，侧栏小芽卡 `[@media(max-height:820px)]:hidden`。
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

0. **改文件别用 Python 文本模式整体重写**：`open(p).read()` + 默认写回会把 LF/CRLF 互转（`trackcheck_web.py`/`app.js` 是 LF，`index.html`/`input.css`/`trackcheck.py` 是 CRLF），git 里会变成整文件改动。批量替换请用 `open(p, 'rb')` 字节读写或写完再按仓库原换行恢复。
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
