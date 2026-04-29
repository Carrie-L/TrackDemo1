# 配置说明

这个工具的配置目标是：换模块时不改脚本，只换 JSON。

核心文件有 3 个：

- `events.json`：要监听的埋点事件 ID，格式固定。
- `expected-data.json`：你从接口、后台、配置平台或本地导出的业务 JSON。
- `field_mapping.json`：告诉脚本“Logcat 字段”和“业务 JSON 字段”怎么对应。

## 1. events.json

格式保持不变：

```json
[
  {
    "eventId": "88860004086007",
    "eventName": "曝光事件"
  },
  {
    "eventId": "88860004086008",
    "eventName": "点击事件"
  }
]
```

脚本只会检查这里列出的事件 ID。Logcat 里其它埋点会被忽略。

## 2. expected-data.json

这里放业务 JSON 原文。脚本不要求固定字段名，也不要求你改造成统一结构。

当前样例里，核心结构是：

```text
data.myPage[].res[].body.cards[].items[]
```

其它模块可以完全不同，比如：

```text
data.homePage[].blocks[].materials[]
data.orderPage.cards[].contents[]
result.tabs[].feeds[]
```

只要在 `field_mapping.json` 里把路径写清楚即可。

## 3. field_mapping.json 总结构

推荐结构：

```json
{
  "fields": {},
  "fieldsByEventId": {},
  "fieldMergeMode": "merge",
  "caseGroups": [],
  "logParser": {},
  "lineFilters": {},
  "valueAliases": {}
}
```

### fields

`fields` 是默认字段映射：

```json
{
  "fields": {
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
}
```

左边是 Logcat 里打印出来的字段名，右边是业务 JSON 里的字段路径。

比如日志里是：

```text
onPlanID=112729
```

JSON 里是：

```json
{
  "planId": "112729"
}
```

那就写：

```json
"onPlanID": "res.planId"
```

### fieldsByEventId

如果不同事件要校验的字段不同，用 `fieldsByEventId` 写事件自己的字段配置。

```json
{
  "fields": {
    "resID": "res.id"
  },
  "fieldMergeMode": "merge",
  "fieldsByEventId": {
    "88860004086008": {
      "operationCardId": "cards.cardId",
      "resName": "res.planName"
    }
  }
}
```

命中 `88860004086008` 时，脚本会先拿默认 `fields`，再叠加这个事件自己的字段。

`fieldMergeMode` 有两种：

- `merge`：推荐。默认 `fields` 是公共字段，`fieldsByEventId` 只写该事件的差异字段。
- `override`：兼容旧逻辑。只要某个事件 ID 出现在 `fieldsByEventId` 里，这个事件就只校验它自己下面列出的字段，不继承 `fields`。

#### 同一个页面有很多埋点事件

同一个页面里有曝光、点击、关闭、跳转等多个埋点事件时，通常不需要按事件建目录。一个页面保持一套目录即可：

```text
modules/
  mine-page/
    events.json
    expected-data.json
    field_mapping.json
```

这个页面里的所有事件 ID 都放在同一个 `events.json` 里：

```json
[
  {
    "eventId": "88860004086007",
    "eventName": "曝光事件"
  },
  {
    "eventId": "88860004086008",
    "eventName": "点击事件"
  },
  {
    "eventId": "88860004086009",
    "eventName": "关闭事件"
  }
]
```

不同事件的字段差异写在同一个 `field_mapping.json` 的 `fieldsByEventId` 里。

#### 完全不同字段的事件

如果两个事件要校验的字段完全不同，可以把 `fieldMergeMode` 设为 `override`，然后为每个事件写一组完整字段：

```json
{
  "fields": {},
  "fieldMergeMode": "override",
  "fieldsByEventId": {
    "88860004086007": {
      "resID": "res.id",
      "resName": "res.planName",
      "operationCardId": "cards.cardId"
    },
    "88860004086008": {
      "cardType": "cards.cardType",
      "onPlanID": "res.planId",
      "dismissLogic": "items.disappearType",
      "contentPoolId": "items.contentPool.contentPoolId",
      "channelId": "items.link.channelId",
      "subChannelId": "items.link.subChannelId"
    }
  },
  "caseGroups": [
    {
      "name": "my-page-card-item",
      "roots": [
        {
          "alias": "res",
          "findListKey": "res"
        },
        {
          "alias": "cards",
          "from": "res",
          "path": "body.cards"
        },
        {
          "alias": "items",
          "from": "cards",
          "path": "items"
        }
      ]
    }
  ],
  "valueAliases": {}
}
```

这里 `88860004086007` 和 `88860004086008` 没有共享字段也没关系，脚本会按事件 ID 分别取对应字段集。

#### 部分字段不同的事件

如果多个事件大部分字段相同，只有少数字段不同，推荐使用 `merge` 模式。

公共字段放在 `fields`，每个事件只在 `fieldsByEventId` 里写差异字段。

推荐写法：

```json
{
  "fields": {
    "resID": "res.id",
    "resName": "res.planName",
    "onPlanID": "res.planId"
  },
  "fieldMergeMode": "merge",
  "fieldsByEventId": {
    "88860004086007": {
      "operationCardId": "cards.cardId"
    },
    "88860004086008": {
      "operationCardId": "cards.cardId",
      "dismissLogic": "items.disappearType"
    }
  },
  "caseGroups": [
    {
      "name": "my-page-card-item",
      "roots": [
        {
          "alias": "res",
          "findListKey": "res"
        },
        {
          "alias": "cards",
          "from": "res",
          "path": "body.cards"
        },
        {
          "alias": "items",
          "from": "cards",
          "path": "items"
        }
      ]
    }
  ],
  "valueAliases": {}
}
```

上面配置里，`88860004086007` 实际会校验 `resID`、`resName`、`onPlanID`、`operationCardId`。`88860004086008` 实际会校验 `resID`、`resName`、`onPlanID`、`operationCardId`、`dismissLogic`。

`fields` 更适合用在这些场景：

- 所有事件都校验同一批字段。
- 临时只跑一个事件，不想写 `fieldsByEventId`。
- 给没有单独配置的事件提供默认字段集。

#### override 模式下不建议的写法

如果 `fieldMergeMode` 是 `override`，下面这种写法容易误以为 `88860004086008` 会校验 `resID`、`resName`、`onPlanID` 三个字段，但实际上它只会校验 `onPlanID`：

```json
{
  "fields": {
    "resID": "res.id",
    "resName": "res.planName"
  },
  "fieldMergeMode": "override",
  "fieldsByEventId": {
    "88860004086008": {
      "onPlanID": "res.planId"
    }
  }
}
```

宝宝的结论：要测什么字段，就在该事件 ID 下显式写出来。埋点测试最危险的不是失败，而是“你以为测了，其实没测”。

### logParser

`logParser` 用来配置 Logcat 上报日志的模板格式。

默认模板是：

```text
Event: id: 88860004086008 data: MinePageCardClickEvent(cardType=1,onPlanID=112729)
```

对应配置是：

```json
{
  "logParser": {
    "lineRegex": "Event:\\s*id:\\s*(?P<event_id>[^\\s]+)\\s+data:\\s*(?P<data>.+)",
    "eventIdGroup": "event_id",
    "dataGroup": "data",
    "eventNameGroup": "event_name",
    "paramsGroup": "params",
    "keyValueSeparator": "="
  }
}
```

`lineRegex` 是整行日志匹配正则。推荐使用 Python 命名分组：

- `(?P<event_id>...)`：事件 ID，必需。
- `(?P<data>...)`：事件类名和参数整体，可选但推荐。
- `(?P<event_name>...)`：事件类名，可选。
- `(?P<params>...)`：参数段，可选。

如果没有配置 `paramsGroup`，脚本会尝试从 `data` 里解析括号内容，比如：

```text
MinePageCardClickEvent(cardType=1,onPlanID=112729)
```

会解析为：

```text
event_name = MinePageCardClickEvent
params = cardType=1,onPlanID=112729
```

#### 模板 1：默认 Event/id/data 格式

日志：

```text
Event: id: 88860004086008 data: MinePageCardClickEvent(cardType=1,onPlanID=112729)
```

配置：

```json
{
  "logParser": {
    "lineRegex": "Event:\\s*id:\\s*(?P<event_id>[^\\s]+)\\s+data:\\s*(?P<data>.+)"
  }
}
```

#### 模板 2：TrackUpload eventId=... payload=...

日志：

```text
TrackUpload eventId=88860004086008 payload=MinePageCardClickEvent(cardType=1,onPlanID=112729)
```

配置：

```json
{
  "logParser": {
    "lineRegex": "TrackUpload\\s+eventId=(?P<event_id>\\d+)\\s+payload=(?P<data>.+)"
  }
}
```

#### 模板 3：事件名和参数分开打印

日志：

```text
TrackReport id=88860004086008 name=MinePageCardClickEvent params=cardType=1,onPlanID=112729,resID=829731
```

配置：

```json
{
  "logParser": {
    "lineRegex": "TrackReport\\s+id=(?P<event_id>\\d+)\\s+name=(?P<event_name>\\w+)\\s+params=(?P<params>.+)",
    "eventIdGroup": "event_id",
    "eventNameGroup": "event_name",
    "paramsGroup": "params"
  }
}
```

#### 模板 4：参数分隔符不是等号

如果日志是：

```text
TrackReport id=88860004086008 params=cardType:1,onPlanID:112729
```

配置：

```json
{
  "logParser": {
    "lineRegex": "TrackReport\\s+id=(?P<event_id>\\d+)\\s+params=(?P<params>.+)",
    "paramsGroup": "params",
    "keyValueSeparator": ":"
  }
}
```

注意：当前脚本的参数项之间仍按英文逗号 `,` 切分，所以最好让日志参数长这样：

```text
a=1,b=2,c=3
```

或：

```text
a:1,b:2,c:3
```

如果某个模块打印的是 JSON 参数，比如 `params={"a":1,"b":2}`，这就不是简单 key-value 文本了，宝宝建议单独加一个 `paramFormat=json`，不要硬用逗号切。到那一步再扩展，别把现在的简单工具提前复杂化。

改了 `logParser` 之后，要顺手检查 `lineFilters`。如果 `lineFilters.includeRegex` 里还写着旧模板，比如 `Event:\\s*id:`，新模板日志会被过滤掉。

### lineFilters

`lineFilters` 用来过滤 Logcat 里的杂日志。

先说一个基础规则：脚本只会解析 `logParser.lineRegex` 能匹配的日志。默认情况下，它只解析这种固定格式：

```text
Event: id: 88860004086008 data: MinePageCardClickEvent(...)
```

如果其它日志只是包含 `88860004086008`，但不符合 `logParser.lineRegex`，脚本本来就会忽略。

如果其它地方也打印了类似格式，或者同一个 `eventId` 会在不同类名里出现，就用 `lineFilters` 做二次过滤：

```json
{
  "lineFilters": {
    "includeRegex": [
      "Event:\\s*id:\\s*\\d+\\s+data:"
    ],
    "excludeRegex": [
      "DebugClick",
      "LocalTest"
    ],
    "includeByEventId": {
      "88860004086008": [
        "MinePageCardClickEvent\\("
      ],
      "88860004086007": [
        "MinePageCardExposeEvent\\("
      ]
    },
    "excludeByEventId": {
      "88860004086008": [
        "ButtonClickDebugEvent\\("
      ]
    }
  }
}
```

字段含义：

- `includeRegex`：全局包含条件。配置后，日志行必须命中其中任意一个正则才会进入校验。
- `excludeRegex`：全局排除条件。命中任意一个正则就跳过。
- `includeByEventId`：按事件 ID 设置包含条件。比如 `88860004086008` 必须来自 `MinePageCardClickEvent(`。
- `excludeByEventId`：按事件 ID 设置排除条件。比如某个调试类也打印同一个事件 ID，就把它排掉。

推荐优先使用 `includeByEventId` 限定埋点类名，因为它最精确。

例如妈妈担心点击时其它地方也输出 `88860004086007`，但真正上报日志类名是 `MinePageCardExposeEvent`，可以这样写：

```json
{
  "lineFilters": {
    "includeByEventId": {
      "88860004086007": [
        "MinePageCardExposeEvent\\("
      ]
    }
  }
}
```

这样只有包含 `MinePageCardExposeEvent(` 的 `88860004086007` 日志会被校验，其它同 ID 日志会被计入 `skippedByFilter`，不会污染结果。

也可以临时在命令行过滤，不改配置文件：

```powershell
python .\track_log_check.py `
  --expected-json .\expected-data.json `
  --events .\events.json `
  --field-map .\field_mapping.json `
  --include-line-regex "MinePageCardExposeEvent\("
```

或者排除某些调试日志：

```powershell
python .\track_log_check.py `
  --expected-json .\expected-data.json `
  --events .\events.json `
  --field-map .\field_mapping.json `
  --exclude-line-regex "ButtonClickDebugEvent\("
```

严厉一点说：只靠 `eventId` 过滤是不够专业的。稳定埋点测试至少要同时卡住 `eventId + 上报日志特征`，最好再卡住上报类名或日志 tag。

### caseGroups

`caseGroups` 用来解决一个重要问题：业务 JSON 里可能有很多卡片、很多 item，不能让 `resID` 命中 A 卡片、`operationCardId` 命中 B 卡片，最后被误判为通过。

当前模块的配置是：

```json
{
  "caseGroups": [
    {
      "name": "my-page-card-item",
      "roots": [
        {
          "alias": "res",
          "findListKey": "res"
        },
        {
          "alias": "cards",
          "from": "res",
          "path": "body.cards"
        },
        {
          "alias": "items",
          "from": "cards",
          "path": "items"
        }
      ]
    }
  ]
}
```

含义是：

1. 在整个 JSON 中找到所有叫 `res` 的数组元素，并命名为 `res`。
2. 从每个 `res` 里取 `body.cards`，并命名为 `cards`。
3. 从每个 `cards` 里取 `items`，并命名为 `items`。
4. 每个 `res + cards + items` 组合是一条候选记录。
5. 一条 Logcat 事件必须在同一条候选记录里整体匹配，才算通过。

字段映射里的第一段，比如 `res.planId`、`cards.cardId`、`items.link.channelId`，就是引用这里定义的 `alias`。

`findListKey` 和 `from/path` 是两种不同的定位方式，不要写在同一个层级里：

- 用 `findListKey`：表示从整份 JSON 全局搜索这个数组名，适合作为起点，比如 `{ "alias": "res", "findListKey": "res" }`。
- 用 `from + path`：表示从上一层 alias 往下找，适合后续层级，比如 `{ "alias": "cards", "from": "res", "path": "body.cards" }`。

错误写法：

```json
{
  "alias": "res",
  "findListKey": "res",
  "from": "data"
}
```

正确写法：

```json
{
  "alias": "res",
  "findListKey": "res"
}
```

### valueAliases

如果日志值和 JSON 值不是字面相同，但业务上等价，用 `valueAliases`：

```json
{
  "valueAliases": {
    "cardType": {
      "1": [
        "large_normal"
      ]
    }
  }
}
```

这表示 `cardType=1` 可以匹配 JSON 里的 `large_normal`。

如果你们要求完全相等，写成：

```json
{
  "valueAliases": {}
}
```

## 4. 新模块怎么配置

假设新模块业务 JSON 是：

```json
{
  "data": {
    "home": [
      {
        "blocks": [
          {
            "blockId": "b_001",
            "materials": [
              {
                "materialId": "m_001",
                "title": "新人福利",
                "link": {
                  "channelId": "home",
                  "subChannelId": "top"
                }
              }
            ]
          }
        ]
      }
    ]
  }
}
```

Logcat 是：

```text
Event: id: 123 data: HomeMaterialClickEvent(blockId=b_001, materialId=m_001, title=新人福利, channelId=home, subChannelId=top)
```

可以这样写 `field_mapping.json`：

```json
{
  "fields": {
    "blockId": "blocks.blockId",
    "materialId": "materials.materialId",
    "title": "materials.title",
    "channelId": "materials.link.channelId",
    "subChannelId": "materials.link.subChannelId"
  },
  "fieldsByEventId": {},
  "caseGroups": [
    {
      "name": "home-block-material",
      "roots": [
        {
          "alias": "blocks",
          "path": "data.home.blocks"
        },
        {
          "alias": "materials",
          "from": "blocks",
          "path": "materials"
        }
      ]
    }
  ],
  "valueAliases": {}
}
```

注意：`path` 可以穿过数组。比如 `data.home.blocks` 中的 `home` 是数组，脚本会自动展开。

## 5. 运行命令

实时监听：

```powershell
python .\track_log_check.py --clear-logcat --expected-json .\expected-data.json --events .\events.json --field-map .\field_mapping.json
```

指定 HTML 报告输出位置：

```powershell
python .\track_log_check.py --expected-json .\expected-data.json --events .\events.json --field-map .\field_mapping.json --html-report .\reports\mine-page.html
```

离线验证：

```powershell
python .\track_log_check.py --expected-json .\expected-data.json --events .\events.json --field-map .\field_mapping.json --log-file .\logcat.txt
```

不生成 HTML：

```powershell
python .\track_log_check.py --no-html-report --expected-json .\expected-data.json --events .\events.json --field-map .\field_mapping.json
```

## 6. 换模块建议

每个模块单独建一个配置目录最清爽：

```text
tracking-checker/
  track_log_check.py
  modules/
    mine-page/
      events.json
      expected-data.json
      field_mapping.json
    home-page/
      events.json
      expected-data.json
      field_mapping.json
```

运行时指定对应模块：

```powershell
python .\track_log_check.py `
  --expected-json .\modules\home-page\expected-data.json `
  --events .\modules\home-page\events.json `
  --field-map .\modules\home-page\field_mapping.json `
  --html-report .\reports\home-page.html
```

什么时候不需要新目录：

- 同一个页面里有多个埋点事件。
- `events.json` 格式相同，只是事件 ID 增多。
- `expected-data.json` 还是同一份页面业务数据。
- `caseGroups` 的业务候选记录组合方式相同。
- 只是某些事件校验字段完全不同或部分不同。

什么时候建议新目录：

- 换了页面，比如我的页、首页、订单页。
- 换了业务 JSON 来源或结构。
- `caseGroups` 完全不同，放在一个文件里会很乱。
- 同一个页面有两套完全不同的数据源，需要分别出报告。
- 不同测试批次需要长期保留独立配置和 HTML 报告。

所以目录按“页面/模块/业务 JSON”分，不按“事件 ID”分。一个页面很多埋点事件，优先放在同一个目录里，用 `fieldsByEventId` 区分字段。

宝宝的严厉提醒：新模块不要先急着跑真机，先复制一条 Logcat 到 `logcat.txt` 做离线验证。字段解析都不稳定就上真机测，那是在浪费妈妈的时间。
