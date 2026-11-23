# 埋点小岛 · 规则引擎、数据源与复核协议参考

## 1. 合并 / 兜底 JSON 数据源（`type: merged_json`）

适用场景：接口下发模块列表（如我的页 4 个卡片点位共用 `myPageV3`），其中部分资源卡片（如 `myPage_function_new`、`myPage_top`）存在本地 `fallback_data.json` 兜底——接口下发了什么就按顺序显示什么；若某兜底 key 下发了但 `res: []` 为空数组，或接口根本没下发该 key，则自动用兜底数据补齐。

### YAML 结构
```yaml
sources:
  mine_page_merged:
    type: merged_json
    primary: [myPageV3, mine_api_cache]  # 优先级列表，前面的不可用时自动降级到下一个
    fallback: fallback_data              # 本地兜底 JSON 数据源名
    path: data.myPage                    # 要合并的数组路径（留空则合并根）
    key: key                             # 数组元素匹配的唯一键名
    empty_field: res                     # 当主源该字段为空数组/空值时，视为"云侧未配置，用兜底替换"
    fallback_keys: [myPage_function_new, myPage_top]  # 留空表示 fallback 中全部 key 生效
```

### 合并规则（`Sources._merge_json`）
1. 依次尝试 `primary` 列表中的数据源，取第一个可用结果；若全部不可用且有 `fallback`，则直接回退全量兜底。
2. 保持主源数组的原始下发顺序遍历元素：
   - 若元素 `key` 在启用兜底集合内，且 `empty_field`（默认 `res`）为空（`None / "" / [] / {}`），用兜底对应元素替换（保留主源顺位）。
   - 若主源非空（例如接口下发 5 个按钮，兜底只有 4 个），则完全采用主源数据。
3. 主源完全未下发的兜底 key，按兜底文件原有顺序追加/补齐。
4. `Sources.merge_info(name)` 返回本次实际命中的 `primary_used`、`fallback` 与替换详情，供「数据源」页卡片展示。

---

## 2. `context.from` 列表降级

定位上下文记录时，`context.<var>.from` 支持填单个数据源名或**优先级数组**：
```yaml
context:
  card:
    from: [mine_page_merged, myPageV3, mine_api_cache, fallback_data]
    path: data.myPage[*]
    where: { key: $.cardKey }
```
按顺序查找第一个加载成功的数据源，避免单一抓包缺失导致整条规则瘫痪。

---

## 3. 模板绑定接口与实时 OkHttp 自动抓包

### 3.1 模板 `api` 继承
多个同页面点位（如我的页曝光/点击）共用同一接口时，直接在 `templates.<tpl>` 上配置 `api`：
```yaml
templates:
  mine_card:
    api:
      - endpoint: /api/v3/myPageV3
        source: myPageV3
        tag: OkHttp            # 可选 logcat tag 过滤
    fields: ...
```
- `merge_spec` 会把模板的 `api` 自动继承给子点位；子点位也可追加或覆写自己的 `api`。
- 在模板里修改 `api` 后，所有继承该模板的点位自动同步生效。
- `#rules` 页面的「关联接口」区域支持折叠展开、修改和删除已关联接口。

### 3.2 实时调试自动抓接口（`LiveSession`）
1. `App.resolve_event_apis(eids)` 汇总本次调试清单里所有点位（含模板继承 + 显式数据源 `api_endpoint` + `merged_json` 依赖链）涉及的接口。
2. 开启实时调试且勾选自动抓接口时：
   - 若 `logcat_args` 未限制 tag，直接复用 `Capture.on_line` 回调喂给 `OkHttpApiExtractor`，零额外 `adb` 进程；
   - 若 `logcat_args` 过滤了特定 tag，额外开一条轻量 `adb logcat -v threadtime -T 1` 专抓绑定接口（带 `popen_kw()` 防闪窗）。
3. 每当捕获到完整 JSON 响应：
   - 写入对应数据源（旧版本自动进入 `.history/`）；
   - 自动用最新数据源重校 **最近 5 秒内** 触发的事件以及所有当前状态为 `FAIL` 的事件，递增 `version` 通知前端全量刷新。

---

## 4. 人工复核状态机与独立 HTML 报告

### 4.1 人工复核（`POST /api/report/review`）
- 支持对单条事件中的任意普通字段改判（`PASS / FAIL / WARN / ASK`），也支持对顶层断言/定位错误以 `__err__:<idx>` 形式改判。
- 当事件下所有字段都被人工或自动判定为 `PASS` 时，若顶层还残留非手动标红的 `errors`，自动联动归为通过，事件头状态立即从 `FAIL` 变为 `PASS` 并同步刷新报告 `summary` 和首页最近状态。
- 问题数统计 `badCount(r)`：**只计 `FAIL` 与 `WARN`**，绝不计入 `INFO`（未定义字段提示）。

### 4.2 独立 HTML 报告（`tc.build_html` & `/report_html/`）
- `/report_html/<name>.html` 优先加载同名 `.json` 实时生成 HTML（若 `.json` 不存在再回退静态 `.html`），保证升级模板后旧报告立即可享新交互。
- 内置筛选栏：
  - 单选状态组：**`有问题的`** / **`通过的`** / **`全部`**；
  - 复选开关：**`过滤未配置`**（隐藏无规则的 `UNKNOWN` 事件）。

---

## 5. AI 提示词生成规范（`api_ai_prompt`）

点击点位上的「复制 AI 提示词」时，后端自动注入以下完整上下文，让 AI 一次写出可直接导入的 YAML：
- 点位基础信息、已有规则及埋点字典里的字段说明；
- 当前已配置的数据源列表（含 `merged_json` 结构、`sqlite_json`、JSON 字段路径样本与 DB 表结构）；
- 已有公共字典 `dicts` 与模板 `templates`（含模板 `api` 绑定写法说明）；
- 近期抓到的真实日志样本。

---

## 6. 规则分享与数据源样本

- 导出（`GET /api/export?sources=1&samples=1`）时，会自动打包规则用到的接口 JSON 以及 ≤ `3 MB`（`SAMPLE_DB_LIMIT`）的 SQLite 数据库样本，同事拿到规则包即使没连手机也能直接跑通预览。
- 导入（`/api/import/preview` → `/api/import/apply`）默认保留本地已有数据源；选择覆盖时旧文件自动归档到 `datasources/.history/`，随时一键回滚。
