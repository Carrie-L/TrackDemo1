# trackcheck · 埋点小岛 —— Android 埋点调试 / 校验工具

把"点一下 → 翻日志 → 对着埋点文档和接口 JSON 一个字段一个字段地比"交给工具：

- **点位有没有上报**：按用例列出必须触发的点位，没触发的标红
- **字段值对不对**：文档枚举、接口下发值、数据库值、字段间关系、由用户操作决定的值（人工确认 / 按场景比对）
- **自动化**：`run` 一边跑 Maestro / adb 脚本一边抓日志，结束出报告，退出码可接 CI
- **规则只配一次**：每个点位的规则存进 `trackcheck.yaml`，以后每次测试自动复用；也可以复制提示词交给 AI 写

```powershell
pip install pyyaml                                      # 唯一依赖，Python 3.8+
python trackcheck_web.py --spec trackcheck.yaml         # 可视化网站，浏览器打开 http://127.0.0.1:8686
```

## 1. 埋点值怎么匹配

一个点位里的值分三类，各有对应的校验办法：

| 值的类型 | 例子 | 怎么校验 |
|---|---|---|
| 文档定义的固定枚举 | `cardType=1`、`page=home` | `enum: {1: 大单卡, 2: 小单卡}` |
| 接口下发 / 数据库里的值 | `resID`、`onPlanID`、`resName` | 按上报的 ID 去接口 JSON 定位节点后取值（`context` + `from`）；或 `sql` 查库 |
| **会随操作变化的计数** | `allNums`、`novelNums` | ① **字段间关系**：`allNums == novelNums + webNums + videoNums`，不依赖数据时机，最稳<br>② **DB 快照区间**：实时调试时，每个用到 SQL 的事件到达后立即从手机拉一次 DB，和上一次快照组成区间，上报值落在 [事件前, 事件后] 就算对<br>③ **方向 / 容差**：离线校验时 DB 是测试结束才拉的，计数只会增加 → `compare: lte`（上报 ≤ DB 值）；或 `tolerance: 1`<br>④ **趋势**：同一点位多次上报只增不减 `trend: up` |
| **由用户操作决定的值** | `source=0/1`（端内/桌面小组件）、`tab=history/bookmark` | `ask: true`：先按文档枚举检查是否合法，再<br>① **本次操作**：操作前在网站上点选「入口=桌面小组件、Tab=书签」，之后到达的事件自动比对（命令行 `--set source=1 --set tab=bookmark`）<br>② **事后确认**：没设置就标「待确认」，在事件上点「对 / 不对」<br>③ 自动化脚本里每一步用 `--set` 声明期望 |

完整例子（[samples/trackcheck.yaml](samples/trackcheck.yaml) 里的「历史记录页曝光」）：

```yaml
events:
  88860005000001:
    name: 历史记录页曝光
    fields:
      allNums:   {sql: "SELECT count(*) FROM history", db: app, compare: lte, trend: up}
      novelNums: {sql: "SELECT count(*) FROM history WHERE type = 'novel'", db: app, compare: lte}
      webNums:   {sql: "SELECT count(*) FROM history WHERE type = 'web'", db: app, compare: lte}
      videoNums: {sql: "SELECT count(*) FROM history WHERE type = 'video'", db: app, compare: lte}
      source:    {enum: {0: 端内, 1: 桌面小组件}, ask: true}
      tab:       {enum: {history: 历史, bookmark: 书签}, ask: true}
    asserts:
      - {expr: "int(allNums) == int(novelNums) + int(webNums) + int(videoNums)", msg: "总数应等于各类之和"}
```

## 2. 网站页面

| 页面 | 做什么 |
|---|---|
| 实时调试 | 开始 / 结束抓 logcat；「本次操作」点选场景；事件前后自动拍 DB 快照；字段旁「设置规则」「对 / 不对」；「复制 AI 提示词」 |
| 埋点字典 | 导入公司埋点文档（Excel 导出的 Markdown 表格 / CSV），**多次导入不同版本自动去重**（点位 ID 已存在则跳过，记录出现在哪些文档）；按 ID / 名称 / 参数 / 含义搜索 |
| 点位规则 | 每个点位的数据定位、字段规则、字段间关系；顶部显示该点位的埋点文档；粘贴一条日志边改边试跑 |
| 规则草稿 | 选一份会话日志，自动反推每个字段来自接口 / DB 的哪里，确认后保存 |
| 数据源 | **填包名直接从手机拉数据库**（root 测试机或 debug 包，自动带 -wal）；拖入 / 粘贴接口 JSON。文件都放在数据源目录，**文件名就是数据源名**，不用填路径 |
| 测试用例 / 校验报告 | 覆盖率清单、历史报告 |
| 导入 / 分享 | 导出规则包发给同事、导入同事的规则（逐条对比差异） |

### 交给 AI 写规则
在实时调试的事件、埋点字典、点位规则上点「复制 AI 提示词」，会生成一段包含 **规则写法速查 + 该点位的埋点文档 + 上报日志 + 数据源结构（表 / 字段 / JSON 路径）+ 现有规则** 的提示词，粘贴给 AI，把返回的 YAML 放进 `trackcheck.yaml` 的 `events:` 下即可。

## 3. 数据源

```
trackcheck.yaml
datasources/            ← 数据源目录（options.datasource_dir 可改）
  app.db                → 数据源 app（数据库）
  mine_page.json        → 数据源 mine_page（接口 JSON）
event_dict.json         ← 埋点字典
```

- 数据库：网站「数据源 → 从手机拉数据库」，或在规则文件里写 `device: {package: com.xxx, dbs: [app.db]}` 后执行 `python trackcheck.py pull`。依次尝试 `su -c`、`run-as`、`su 0`、直接 `cat`（adb root）。
- 接口 JSON：拖进网站 / 从抓包工具粘贴；或 App debug 包加 [ApiDumpInterceptor](android/TrackDebug.kt) 后点「拉取接口 dump」。
- 特殊情况（接口 JSON 缓存在 DB 某张表里）在规则文件 `sources:` 写 `type: sqlite_json`。

## 4. 规则参考

### 字段规则（可组合）

| 键 | 说明 |
|---|---|
| `enum` | 文档枚举：`{0: 端内, 1: 桌面小组件}` 或 `[open, close]` |
| `from` | 期望值 = 接口数据路径，配合 `context` 用上报的 ID 定位节点：`card.cardType` |
| `map` | 接口值 → 上报值：`{large_normal: 1, small_normal: 2}` |
| `sql` + `db` | 期望值 = SQL 第一列，`:字段名` 绑定上报值 |
| `expr` | Python 表达式算期望值，可用其它上报字段：`int(novelNums) + int(webNums)` |
| `eq` | 固定值 |
| `compare` | `eq`（默认）/ `lte` 上报 ≤ 期望 / `gte` / `lt` / `gt` |
| `tolerance` | 数值容差，如 `1` |
| `trend` | 同一点位多次上报：`up` 只增不减 / `strict_up` / `down` / `same` |
| `ask` | 由用户操作决定，按「本次操作」比对或人工确认 |
| `type` / `regex` / `range` / `len` / `not_empty` | 格式检查（值为空时跳过） |
| `required: false` / `when` / `assert` / `severity: warn` / `ignore` / `desc` | 可不上报 / 条件校验 / 自定义断言 / 只警告 / 不校验 / 字段含义 |

### 数据定位（接口下发值）

```yaml
    context:
      res:  {from: mine_page, path: "data.myPage[*].res[*]", where: {id: "{resID}"}}
      card: {from: res, path: "body.cards[*]", where: {cardId: "{operationCardId}"}}
    fields:
      onPlanID: {from: res.planId}
      cardType: {from: card.cardType, map: {large_normal: 1, small_normal: 2}}
```
网站里在接口 JSON 上点一下值就会自动生成，不用手写。

### 事件级

```yaml
    extends: mine_card                 # 继承模板（曝光 / 点击共用字段）
    asserts: [{expr: "...", msg: "..."}]
    dedupe: [resID, operationCardId]   # 重复上报检测
    dedupe_window: 2
```

多个点位共用的映射也可以放到顶层 `dicts:`，字段里写 `map: 名字` / `enum: 名字` 引用；只有一个点位用的直接内联即可。

## 5. 命令行

| 场景 | 命令 |
|---|---|
| 离线校验日志 | `python trackcheck.py check --log xxx.log [--case 用例] [--set source=1]` |
| 实时抓 logcat | `python trackcheck.py live --clear [--pull]` |
| 自动化脚本 + 校验 | `python trackcheck.py run --case 用例 --set source=0 -- maestro test flow.yaml` |
| 拉手机数据库 | `python trackcheck.py pull` |
| 生成规则草稿 | `python trackcheck.py infer --log xxx.log --json datasources/*.json --db datasources/app.db -o draft.yaml` |

报告在 `reports/`（HTML + JSON），有失败或用例点位没触发时退出码为 1。

## 6. 快速体验（自带示例数据）

```powershell
python samples/make_sample_db.py
python trackcheck_web.py --spec samples/trackcheck.yaml
# 网站里：埋点字典 → 导入 samples/docs/ 下两份文档；首页 → 快速校验 samples/session.log
python trackcheck.py check --spec samples/trackcheck.yaml --log samples/session.log --set source=0 --set tab=history
```

## 7. App 端接入（debug 包）

见 [android/TrackDebug.kt](android/TrackDebug.kt)：埋点日志建议打印成 `Event: id: xxx data: {json}`（现有 `XxxEvent(a=1, b=2)` 也能解析，格式不同改 `log.pattern`）；接口 dump 拦截器可选。

样式用 Tailwind CSS 预编译在 `web/static/app.css`，图标为 Lucide（`web/static/icons.svg`），离线可用；改了页面样式后执行 `web/build_css.cmd`（需要 Node.js）。
