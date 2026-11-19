# trackcheck —— Android 埋点调试 / 自动校验工具

把"点一下 → 翻日志 → 对着埋点文档和接口 JSON 一个字段一个字段地比"这件事交给脚本做。

- ✅ **点位有没有上报**：按用例列出必须触发的点位，没触发的标红
- ✅ **每个字段值对不对**：文档枚举值、接口下发值、DB 里的值、固定值、正则/类型/范围、跨字段断言
- ✅ **自动化**：`run` 命令一边跑 Maestro / adb 脚本一边抓日志，结束出报告，退出码可接 CI
- ✅ **规则不用从零写**：`infer` 根据"日志 + 接口 JSON + DB"反推每个字段的来源，生成规则草稿

```
pip install pyyaml          # 唯一依赖，Python 3.8+
python trackcheck.py -h
```

## 0. 可视化网站「埋点小岛」（推荐）

```powershell
python trackcheck_web.py --spec trackcheck.yaml     # 规则文件不存在会自动创建；浏览器自动打开 http://127.0.0.1:8686
```

| 页面 | 做什么 |
|---|---|
| 🔍 实时调试 | 开始/结束抓 logcat，每个埋点一张卡片；点开后字段旁「设置规则」→ 弹窗里点接口 JSON 的值 / 写 SQL / 选文档字典，**保存后这个点位以后自动复用** |
| 🌱 规则草稿 | 选一份会话日志 → 自动推断每个字段来源 → 已推断 / 请确认 / 待补全 三色标记 → 「确认并保存」 |
| 📒 点位规则 | 查看、修改每个点位的数据定位和字段规则，粘贴一条日志即可边改边试跑 |
| 📚 字典 | 字典像标签一样挂在点位上（哪些点位用它自己维护），支持从文档批量粘贴 `1:首页` |
| 🧺 数据源 | 接口 JSON / DB / DB 里缓存的 JSON；一键从手机拉取 |
| 🧪 测试用例 / 📊 校验报告 | 覆盖率清单、历史报告（含问题字段汇总） |
| 🤝 导入 / 分享 | 导出规则包发给同事、导入同事的规则（逐条对比差异）、导入全量点位清单 JSON |

网站读写的就是同一份 `trackcheck.yaml`，命令行和网站可以混用。保存时会把上一个版本备份成 `trackcheck.yaml.bak`（注意：通过网站保存后 YAML 里的注释不会保留）。

样式用 Tailwind CSS 预先编译在 `web/static/app.css`，离线可用；改了页面样式后执行 `web/build_css.cmd` 重新生成（需要 Node.js）。

## 1. 原理：一个点位 = 定位数据 + 字段规则

上报日志是扁平的 key=value，接口 JSON 是层层嵌套的。关键是**用上报值里的 ID 反过来去接口 JSON 里定位到"那个资源位 / 那张卡片"节点**，然后其它字段都拿这个节点里的值比：

```
上报: resID=829731, operationCardId=a_47jc0, onPlanID=112729, cardType=1 ...
          │                  │
          ▼                  ▼
接口: data.myPage[*].res[*] where id==829731   →  res 节点
      res.body.cards[*]    where cardId==a_47jc0 →  card 节点
期望: onPlanID = res.planId
      cardType = card.cardType ("large_normal") 经文档字典 card_type 映射 → 1
```

```yaml
events:
  88860004086007:
    name: 我的页-卡片曝光
    context:                        # ① 用上报值去接口 JSON 里定位节点
      res:  {from: mine_api, path: "data.myPage[*].res[*]", where: {id: "{resID}"}}
      card: {from: res, path: "body.cards[*]", where: {cardId: "{operationCardId}"}}
      item: {from: card, path: "items[0]"}
    fields:                         # ② 每个字段的期望值来源
      onPlanID:     {from: res.planId, desc: 投放计划ID}
      cardType:     {from: card.cardType, map: card_type}           # 接口值经文档字典映射
      cardName:     {from: card.cardType, map: [card_type, card_type_name]}
      dismissLogic: {from: item.disappearType}
      source:       {enum: source}                                  # 文档定义的枚举
      resName:      {db: app_db, sql: "SELECT plan_name FROM res_plan WHERE res_id = :resID"}
```

如果 `resID` 在接口里都找不到，所有依赖它的字段会标成"定位失败"——这本身就说明 resID 传错了。

完整示例见 [samples/trackcheck.yaml](samples/trackcheck.yaml)。

## 2. 快速体验（自带示例数据，无需手机）

```powershell
python samples/make_sample_db.py
python trackcheck.py check --spec samples/trackcheck.yaml --log samples/session.log --case 我的页卡片
python trackcheck.py infer --log samples/session.log --json samples/api_dump/mine_page.json --db samples/app.db -o draft.yaml
```

## 3. 日常使用

| 场景 | 命令 |
|---|---|
| 手动点，实时看结果，Ctrl+C 出报告 | `python trackcheck.py live --spec trackcheck.yaml --clear --pull` |
| 已有一份 logcat 日志 | `python trackcheck.py check --log xxx.log --case 我的页卡片` |
| 自动化脚本 + 校验 | `python trackcheck.py run --case 我的页卡片 --pull -- maestro test flows/mine.yaml` |
| 拉手机 DB / 接口 dump | `python trackcheck.py pull` |
| 生成规则草稿 | `python trackcheck.py infer --log xxx.log --json api_dump/*.json --db app.db -o draft.yaml` |

- 报告：`reports/trackcheck_时间.html`（可筛选"只看问题"、搜索）+ 同名 `.json`（给 CI 用）
- 退出码：有失败字段或用例点位没触发时为 1
- `live/run` 会把抓到的埋点行存成 `reports/session_时间.log`，规则改了可以用 `check` 重跑，不用再点一遍
- `--pull`：结束时先按 `device.pull` 拉最新的 DB/接口 dump 再出最终报告（保证对照数据和本次操作一致）

## 4. 规则参考

### 4.1 sources 数据源

```yaml
sources:
  mine_api:   {type: json_file, path: api_dump/v1_mine_page.json}     # path 支持通配符，取最新的文件
  app_db:     {type: sqlite, path: app.db}                            # 自动带上 app.db-wal
  mine_cache: {type: sqlite_json, db: app_db, sql: "SELECT content FROM api_cache WHERE cache_key='mine'"}
```
JSON 中如果某个字段的值是"JSON 字符串"，会自动解开继续往下走。

### 4.2 context 定位

| 键 | 说明 |
|---|---|
| `from` | 数据源名，或前面已定义的 context 名 |
| `path` | `a.b[0].c`、`list[*]`（任意元素），可用 `{上报字段}` 占位 |
| `where` | 在 path 匹配到的多个节点里筛选：`{id: "{resID}"}`，键也可以是子路径 `contentPool.contentPoolId` |
| `pick` | 多个匹配时取 `first`（默认）/ `last` |

### 4.3 fields 字段规则（可组合）

| 键 | 说明 |
|---|---|
| `from` | 期望值 = 上下文路径 `card.cardType`，或绝对路径 `mine_api:data.x[0].y`；多个匹配时命中任意一个即通过 |
| `sql` + `db` | 期望值 = SQL 第一列（任一行匹配即通过），`:字段名` 绑定上报值 |
| `map` | 对期望值做字典映射，可链式 `[card_type, card_type_name]` |
| `eq` | 固定值，可用 `{字段}` 引用其它上报字段 |
| `expr` | Python 表达式算期望值，可用上报字段名、context 名、`idx['card']`(数组下标)、`dicts`、`get(obj,'a.b')` |
| `enum` | 文档枚举：列表 `[open, close]`、字典名 `source`、或内联字典 `{0: 未领取, 1: 已领取}` |
| `type` | `int` `number` `bool` `url` `json` `ts13` `ts10` |
| `regex` / `range: [1, 20]` / `len: [1, 64]` | 格式校验（值为空时跳过） |
| `not_empty` | 不允许空值 |
| `allow_empty` | 配合 `enum`：空值也算通过 |
| `required: false` | 字段可以不上报 |
| `when` | 条件成立才校验：`when: "type == 'open'"` |
| `assert` + `msg` | 自定义断言，`value` 为当前字段值 |
| `severity: warn` | 不通过时只算警告 |
| `ignore: true` | 不校验 |
| `desc` | 字段含义，显示在报告里 |

简写：`字段: card.cardId`（= from）、`字段: [a, b]`（= enum）、`字段: 1`（= eq）。

### 4.3.1 字典和点位：像标签一样关联

没有"全局通用字典"这回事，字典**挂在点位上**，哪些点位用它由你自己维护（网站「字典」页里像打标签一样加 / 删）：

```yaml
dicts:
  card_type:
    desc: 我的页卡片类型映射
    events: [88860004086007, 88860004086008]     # 关联点位（标签）
    items: {large_normal: 1, small_normal: 2}

events:
  88860004086007:
    fields:
      cardType:  {from: card.cardType, map: card_type}                # 引用字典
      cardState: {enum: {0: 未领取, 1: 已领取}}                        # 只有本点位用：直接内联
      tabType:   {from: card.tab, map: {top: 1, bottom: 2}}           # 本点位专用映射表
```

- 字段规则引用了某个字典，保存时会**自动把这个点位挂到字典的关联点位里**；字典页还会把"规则里引用了但没挂标签"的点位用虚线标出来，一键补上。
- 字段弹窗里选字典时，已关联本点位的字典排在前面；本点位专用的枚举 / 映射也能一键「存为字典」给别的点位挂用。
- 也支持点位内部的字典 `events.<id>.dicts`，优先级最高。旧写法 `dicts: {source: {1: 首页}}` 继续兼容。

### 4.3.2 分享 / 导入规则（团队协作）

网站「🤝 导入 / 分享」页：
- **导出**：全部规则，或挑选部分点位 → 下载 `trackcheck_share_时间.yaml`（自动带上继承的模板、用到的字典，可选带数据源）。
- **导入**：上传同事的规则包（或整份 trackcheck.yaml），逐条显示「新增 / 和我的不同 / 完全相同」，每条可选导入、覆盖、保留我的；字典可"合并"（补充我没有的值、关联点位取并集）。规则包里用到但你本地没有的数据源会提示出来。
- **点位清单 JSON**：只有点位ID和名称的全量清单，如 `[{"eventId": "888…", "eventName": "曝光事件"}]`（也支持 `{id: 名称}` 等格式），导入后存为 `catalog.json`。「点位规则」页可以按 已配置 / 未配置 筛选，点未配置的点位直接开始配；实时调试里没规则的点位也会显示名称。

### 4.4 事件级

```yaml
    extends: mine_card                 # 继承 templates 中的模板（或另一个点位），曝光/点击共用
    asserts:
      - {expr: "not (source == '1' and page != 'home')", msg: "source=1 时 page 必须是 home"}
    dedupe: [resID, operationCardId]   # 重复上报检测
    dedupe_window: 2                   # 秒；不写则整个会话内重复都提示
    strict_fields: true                # 上报了规则外字段算失败
```

### 4.5 cases 用例（覆盖率）

```yaml
cases:
  我的页卡片:
    expect:
      - 88860004086007
      - {id: 88860004086008, where: {type: open}, min: 1, max: 1}
```

### 4.6 点位很多时

```yaml
include: ["events/*.yaml"]    # 每个业务模块一个文件，内容同样是 dicts/templates/events/cases
```

## 5. App 端接入（debug 包）

见 [android/TrackDebug.kt](android/TrackDebug.kt)：

1. **埋点日志**：现有 `Event: id: xxx data: XxxEvent(a=1, b=2)` 格式可以直接用；建议改成 `data: {json}`，值里带逗号/括号时更稳。格式不同就改 `log.pattern`。
2. **接口 dump**：加一个 debug 拦截器把每个接口的最新响应写到 `Android/data/<包名>/files/api_dump/`，工具 `pull` 下来当对照数据。
3. **DB**：debug 包可 `run-as`，`device.pull` 里写 `databases/xxx.db` 会自动带上 `-wal/-shm`（只拉 .db 可能读不到最新写入）。

## 6. 推荐流程

1. 选一个页面，手动点一遍：`live --clear`，得到 `session_xxx.log`
2. `pull` 拉下 DB / 接口 dump
3. `infer` 生成草稿 → 对照埋点文档补全枚举、改名称、确认带"请确认"注释的字段（**规则写一次，以后每次回归都能复用**）
4. 之后每次测试：`live` 或 `run`，直接看报告里标红的字段
5. 稳定后把操作写成 Maestro flow，`run` 接进 CI / 每日回归
