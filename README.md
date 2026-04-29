# Android Logcat 埋点校验脚本

这个工具放在 Android 项目外部，默认路径：

```powershell
T:\OpenAIProjects\TrackDemo-tools\tracking-checker
```

它不改 Android 源码、不加 Gradle 依赖、不写 instrumentation test。你手动操作 App，脚本监听 `adb logcat`，发现指定 `eventId` 后，把日志里的字段和本地 JSON 里的字段做比对。

## 文件

- `track_log_check.py`：核心脚本。
- `events.sample.json`：事件 ID 列表样例。
- `field_mapping.sample.json`：日志字段到业务 JSON 路径的映射样例。
- `expected-data.sample.json`：业务 JSON 样例。
- `logcat.sample.txt`：离线调试用日志样例。
- `CONFIG_GUIDE.md`：配置说明和多模块接入说明。
- `config-builder.html`：可视化配置生成器，浏览器打开后用表单生成 `field_mapping.json`。

正式使用时，把 3 个 sample 复制成：

```powershell
Copy-Item .\events.sample.json .\events.json
Copy-Item .\field_mapping.sample.json .\field_mapping.json
Copy-Item .\expected-data.sample.json .\expected-data.json
```

然后把你的真实数据填进去。

## 可视化配置

不想手写 `field_mapping.json` 时，直接打开：

```powershell
.\config-builder.html
```

页面支持导入现有配置、填写字段映射、配置日志模板和过滤规则，并生成 `field_mapping.json`。

## 离线验证

先用样例日志验证脚本能跑：

```powershell
python .\track_log_check.py --expected-json .\expected-data.sample.json --events .\events.sample.json --field-map .\field_mapping.sample.json --log-file .\logcat.sample.txt
```

样例里大部分字段故意和 JSON 不一致，所以会出现 `FAIL`，这是正常的。你重点看输出是否能正确解析出 `cardType`、`onPlanID`、`resID` 等字段。

## 实时监听 Logcat

连接手机或模拟器后运行：

```powershell
python .\track_log_check.py --clear-logcat --expected-json .\expected-data.json --events .\events.json --field-map .\field_mapping.json
```

然后你手动操作屏幕。脚本看到 `events.json` 里的事件 ID 后会自动校验。停止时按 `Ctrl+C`。

每次运行默认会生成 HTML 报告：

```powershell
.\tracking-report.html
```

也可以手动指定报告位置：

```powershell
python .\track_log_check.py --expected-json .\expected-data.json --events .\events.json --field-map .\field_mapping.json --html-report .\reports\mine-page.html
```

## 字段映射

当前默认映射是：

```json
{
  "cardType": "cards.cardType",
  "onPlanID": "res.planId",
  "resID": "res.id",
  "operationCardId": "cards.cardId",
  "resName": "res.planName",
  "dismissLogic": "items.disappearType",
  "contentPoolId": "items.contentPool.contentPoolId",
  "channelId": "items.link.channelId",
  "subChannelId": "items.link.subChannelId"
}
```

脚本会优先按 `caseGroups` 组合候选记录，当前样例是 `res -> cards -> items`。一条日志必须在同一条候选记录里字段整体匹配，避免不同卡片的字段各自命中导致误判。

## 值映射

如果日志里是 `cardType=1`，但 JSON 里是 `cardType=large_normal`，可以在 `field_mapping.json` 里保留这个配置：

```json
"valueAliases": {
  "cardType": {
    "1": ["large_normal"]
  }
}
```

如果你们要求严格字面值相等，把 `valueAliases` 改成 `{}`。

## 多事件不同字段

如果曝光事件和点击事件字段不同，可以这样配：

```json
{
  "fields": {
    "cardType": "cards.cardType",
    "onPlanID": "res.planId",
    "resName": "res.planName"
  },
  "fieldMergeMode": "merge",
  "fieldsByEventId": {
    "88860004086008": {
      "dismissLogic": "items.disappearType"
    }
  },
  "valueAliases": {}
}
```

`fieldMergeMode: "merge"` 表示公共字段放在 `fields`，每个事件只在 `fieldsByEventId` 写差异字段。旧配置如果用 `"override"`，则事件字段会覆盖全局 `fields`。

## 多模块

事件文件 `events.json` 可以保持不变。换模块时通常只需要换：

- `expected-data.json`：新模块的业务 JSON。
- `field_mapping.json`：新模块的日志字段到 JSON 字段映射。
- `caseGroups`：新模块的业务候选记录组合方式。

详细写法见 [CONFIG_GUIDE.md](./CONFIG_GUIDE.md)。

## 过滤杂日志

脚本默认只解析 `Event: id: ... data:` 格式的日志。如果其它日志只是包含同一个事件 ID，会被自动忽略。

如果其它地方也打印了同样格式或同样事件 ID，可以在 `field_mapping.json` 里配置 `lineFilters`：

```json
{
  "lineFilters": {
    "includeByEventId": {
      "88860004086008": [
        "MinePageCardClickEvent\\("
      ]
    }
  }
}
```

也可以临时用命令行过滤：

```powershell
python .\track_log_check.py --include-line-regex "MinePageCardClickEvent\(" --expected-json .\expected-data.json --events .\events.json --field-map .\field_mapping.json
```

## 自定义日志模板

如果其它模块不是 `Event: id: ... data:`，在 `field_mapping.json` 里配置 `logParser`：

```json
{
  "logParser": {
    "lineRegex": "TrackUpload\\s+eventId=(?P<event_id>\\d+)\\s+payload=(?P<data>.+)"
  }
}
```

也支持事件名和参数分开：

```json
{
  "logParser": {
    "lineRegex": "TrackReport\\s+id=(?P<event_id>\\d+)\\s+name=(?P<event_name>\\w+)\\s+params=(?P<params>.+)",
    "paramsGroup": "params"
  }
}
```

详细规则见 [CONFIG_GUIDE.md](./CONFIG_GUIDE.md) 的 `logParser` 章节。
