// 埋点小岛 —— 前端逻辑（Alpine.js）
const NAV = [
  { id: 'home', icon: 'home', label: '首页' },
  { id: 'live', icon: 'live', label: '实时调试' },
  { id: 'evdict', icon: 'dict', label: '埋点字典' },
  { id: 'rules', icon: 'rules', label: '点位规则' },
  { id: 'infer', icon: 'draft', label: '规则草稿' },
  { id: 'sources', icon: 'data', label: '数据源' },
  { id: 'cases', icon: 'cases', label: '测试用例' },
  { id: 'reports', icon: 'report', label: '校验报告' },
  { id: 'share', icon: 'share', label: '导入 / 分享' },
];
const CARD_TONES = [
  'bg-pond-50 border-pond-200', 'bg-leaf-50 border-leaf-200', 'bg-peach-50 border-peach-200',
  'bg-berry-50 border-berry-200', 'bg-lilac-50 border-lilac-200', 'bg-sun-50 border-sun-200',
];
const TIPS = [
  '一个点位只需要配置一次规则，之后每次测试都会自动复用。',
  '会变的计数（如历史记录数）：实时调试时自动在事件前后拍 DB 快照，上报值落在区间内即通过。',
  '入口、Tab 这类由操作决定的值：在「测试用例」里给字段点选期望值，实时调试选上用例就自动比对。',
  '没填期望值也没关系，标了「看操作」的字段会显示「待确认」，点一下「对 / 不对」即可。',
  '多个点位共用的定位和字段可以做成「模板」，在「点位规则 → 模板」里查看和编辑。',
  '规则可以交给 AI 写：在事件或点位上点「复制 AI 提示词」，粘贴给 AI 就有文档、日志和数据源。',
  '埋点字典支持多次导入不同版本的文档，重复的点位会自动跳过。',
  '数据源不用填路径：把接口 JSON / 数据库放进数据源目录，或者填包名直接从手机拉。',
];
const STATUS_TEXT = { PASS: '通过', FAIL: '失败', WARN: '警告', ASK: '待确认', SKIP: '跳过', INFO: '未定义', UNKNOWN: '无规则', MISS: '未触发' };
const DRAFT_TEXT = { ok: '已推断', confirm: '请确认', todo: '待补全' };

function norm(v) { return v === null || v === undefined ? '' : String(v).trim(); }
function clone(o) { return JSON.parse(JSON.stringify(o ?? null)); }
function splitList(s) { return String(s || '').split(/[,，\n]/).map(x => x.trim()).filter(x => x !== ''); }
function parseWhere(s) {
  const o = {};
  splitList(s).forEach(p => { const i = p.indexOf('='); if (i > 0) o[p.slice(0, i).trim()] = p.slice(i + 1).trim(); });
  return o;
}
function whereText(w) { return Object.entries(w || {}).map(([k, v]) => `${k}=${v}`).join(', '); }
// 从文档里的说明提取枚举：「0 端内，1 桌面小组件」「open 打开 / close 关闭」「1：首页、2：搜索框」
function extractPairs(text) {
  const out = [];
  String(text || '').split(/[，,；;、\n/]|\s{2,}/).forEach(seg => {
    const m = seg.trim().match(/^(?:[^\s:：=]*[:：])?\s*([A-Za-z0-9_\-.]+)\s*(?:[:：=]|\s)\s*(.+)$/);
    if (m && m[2].trim()) out.push({ k: m[1].trim(), v: m[2].trim() });
  });
  return out;
}
const fmtSize = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n > 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B';

function app() {
  return {
    NAV, CARD_TONES, STATUS_TEXT, DRAFT_TEXT, fmtSize,
    page: 'home', st: null, busy: false, toasts: [], tipIdx: 0, devices: [], devErr: '', search: '', searchOpen: false,
    live: { serial: '', clear: true, pull: false, snapshot: true, caseName: '', only: '', results: [], running: false, sel: -1, report: null, raw: '', filter: 'all', scn: {}, snapErr: '', plan: [], addId: '', onlyPlan: false, showUnk: false },
    inf: { mode: 'session', path: '', content: '', fileName: '', drafts: [], dicts: {}, warns: [], log: '', open: 0 },
    ed: null, ruleFilter: '', ruleTab: 'all',
    exp: { mode: 'all', ids: [], q: '', author: '', note: '', sources: true },
    imp: { fileName: '', content: '', preview: null, choices: {}, mode: 'merge', overwriteNames: true, catalog: true },
    fm: null, ai: null,
    evd: { entries: [], docs: [], q: '', filter: 'all', open: null, loaded: false, overwrite: false, result: null, limit: 100 },
    ds: { files: [], dir: '', pkg: '', dbs: [], devDbs: null, pasteName: '', pasteText: '', showPaste: false, log: '', advanced: false, drag: false },
    srcEd: null, caseEd: null, settingsEd: null, pullLog: '',
    rep: { list: [], cur: null, view: 'bad', showUnk: false, q: '' }, yv: null,
    checkForm: { path: '', content: '', fileName: '', caseName: '' },

    async init() {
      if (this._inited) return;
      this._inited = true;
      await this.refresh();
      this.loadDevices(); this.loadEvdict();
      setInterval(() => this.pollLive(), 1000);
      setInterval(() => this.loadDevices(), 15000);
      setInterval(() => { this.tipIdx = (this.tipIdx + 1) % TIPS.length; }, 9000);
      const h = location.hash.slice(1); if (NAV.some(n => n.id === h)) this.go(h, true);
      window.addEventListener('hashchange', () => { const x = location.hash.slice(1); if (NAV.some(n => n.id === x) && x !== this.page) this.go(x, true); });
      window.addEventListener('keydown', e => {
        const tag = (e.target?.tagName || '').toLowerCase();
        const editing = tag === 'input' || tag === 'textarea' || tag === 'select' || e.target?.isContentEditable;
        if (e.key === '/' && !editing && !e.ctrlKey && !e.metaKey && !e.altKey) {
          e.preventDefault(); this.$refs.gsearch?.focus(); this.searchOpen = true;
        } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
          if (this.busy || e.repeat) { e.preventDefault(); return; }
          if (this.yv?.edit) { e.preventDefault(); this.saveYaml(); }
          else if (this.fm) { e.preventDefault(); this.saveField(); }
          else if (this.page === 'rules' && this.ed) { e.preventDefault(); this.saveEditor(); }
          else if (this.page === 'cases' && this.caseEd) { e.preventDefault(); this.saveCase(); }
        }
      });
    },
    go(p, quiet) {
      this.page = p; if (!quiet) location.hash = p; window.scrollTo({ top: 0 });
      if (p === 'evdict' && !this.evd.loaded) this.loadEvdict();
      if (p === 'sources') { this.loadDs(); if (!this.settingsEd && this.st) this.openSettings(); }
    },
    get tip() { return TIPS[this.tipIdx % TIPS.length]; },
    icon(n) { return '/static/icons.svg#i-' + n; },
    pic(n) { return '/static/icons/' + n + '.png'; },

    // ---------------- 通用 ----------------
    async api(path, body) {
      const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
      const r = await fetch(path, opt);
      const d = await r.json().catch(() => ({ error: '服务器返回异常' }));
      if (!r.ok) throw new Error(d.error || r.statusText);
      return d;
    },
    async call(fn, okMsg) {
      this.busy = true;
      try { const r = await fn(); if (okMsg) this.toast(okMsg, 'ok'); return r; }
      catch (e) { this.toast(e.message, 'err'); throw e; }
      finally { this.busy = false; }
    },
    toast(msg, kind = 'ok', action = null) {
      const id = Math.random(); this.toasts.push({ id, msg, kind, action });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, action ? 6500 : (kind === 'err' ? 6000 : 3200));
    },
    runToastAction(t) {
      if (t.action?.fn) t.action.fn();
      this.toasts = this.toasts.filter(x => x.id !== t.id);
    },
    async openPath(arg) {
      const body = typeof arg === 'string' ? { path: arg } : (arg || {});
      try {
        const r = await this.api('/api/open_path', body);
        this.toast('已在文件管理器打开：' + this.fileName(r.path), 'ok');
      } catch (e) { this.toast(e.message, 'err'); }
    },
    get quickHits() {
      const q = this.search.trim().toLowerCase();
      if (!q) return { events: [], dict: [], cases: [], total: 0 };
      const events = (this.st?.events || []).filter(e => e.id.includes(q) || (e.name || '').toLowerCase().includes(q)).slice(0, 6);
      const evIds = new Set(events.map(e => e.id));
      const dict = (this.evd.entries || []).filter(e => !evIds.has(e.id) && (e.id.includes(q) || (e.name || '').toLowerCase().includes(q) || JSON.stringify(e.rows || []).toLowerCase().includes(q))).slice(0, 6);
      const cases = Object.entries(this.st?.cases || {}).filter(([n, c]) => n.toLowerCase().includes(q) || (c.desc || '').toLowerCase().includes(q)).map(([name, c]) => ({ name, desc: c.desc || '' })).slice(0, 4);
      return { events, dict, cases, total: events.length + dict.length + cases.length };
    },
    pickQuickEvent(id) { this.searchOpen = false; this.loadEditor(id); this.go('rules'); },
    pickQuickDict(e) { this.searchOpen = false; this.evd.q = e.id; this.evd.open = e.id; this.evd.filter = 'all'; this.go('evdict'); },
    pickQuickCase(name) { this.searchOpen = false; this.openCase(name); },
    async refresh() {
      try { this.st = await this.api('/api/state'); } catch (e) { this.toast(e.message, 'err'); }
      if (this.st?.live) {
        this.live.running = this.st.live.running; this.live.report = this.st.live.report;
        if (this.st.live.running && !this.live.plan.length) {
          if (this.st.live.case) { this.live.caseName = this.st.live.case; this.live.plan = this.caseRows(this.st.cases?.[this.st.live.case]); }
          const scn = this.st.live.scenario || {};
          Object.entries(scn).forEach(([k, v]) => {
            const dot = k.indexOf('.'); if (dot <= 0) return;
            const id = k.slice(0, dot), f = k.slice(dot + 1);
            let row = this.live.plan.find(r => r.id === id);
            if (!row) { row = { key: Math.random(), id, min: 1, max: '', values: {}, showAll: false }; this.live.plan.push(row); }
            row.values[f] = v;
          });
        }
      }
      if (this.st && !this.ds.pkg) { this.ds.pkg = this.st.device.package || ''; this.ds.dbs = [...(this.st.device.dbs || [])]; }
    },
    async loadDevices() {
      try { const d = await this.api('/api/devices'); this.devices = d.devices; this.devErr = d.error || '';
        if (!this.live.serial && this.devices.length) this.live.serial = this.devices.find(x => x.state === 'device')?.serial || '';
      } catch (e) { this.devErr = e.message; }
    },
    statusText(s) { return STATUS_TEXT[s] || s; },
    tone(i) { return CARD_TONES[i % CARD_TONES.length]; },
    fileName(p) { return String(p || '').split(/[\\/]/).pop(); },
    eventName(id) { id = String(id); return this.st?.events.find(e => e.id === id)?.name || this.st?.catalog.find(c => c.id === id)?.name || ''; },
    get onlineDevice() { return this.devices.find(d => d.state === 'device'); },
    get onlineCount() { return this.devices.filter(d => d.state === 'device').length; },
    get devTitle() {
      const d = this.onlineDevice;
      return d ? `${d.model || ''} ${d.serial}` + (this.st?.device.package ? ' · 包名 ' + this.st.device.package : '') + '（点击刷新）' : (this.devErr || '没有检测到在线设备，点击刷新');
    },
    get evPickList() {
      const s = this.st; if (!s) return [];
      const m = new Map(); s.events.forEach(e => m.set(e.id, { id: e.id, name: e.name }));
      s.catalog.forEach(c => { if (!m.has(c.id)) m.set(c.id, { id: c.id, name: c.name }); });
      return [...m.values()].slice(0, 1500);
    },
    get stats() {
      const s = this.st; if (!s) return {};
      const last = s.reports[0];
      const evs = s.events, ok = evs.filter(e => e.last?.status === 'PASS').length, bad = evs.filter(e => e.last && e.last.status !== 'PASS').length;
      return {
        events: evs.length, sources: Object.keys(s.sources).length, evdict: s.evdict.count,
        rate: last && last.summary.total ? Math.round(last.summary.pass * 100 / last.summary.total) : null, ok, bad,
        untested: evs.filter(e => !e.last).length,
        catalog: s.catalog.length, unconfigured: s.catalog.filter(c => !c.configured).length,
      };
    },
    get notices() {
      const s = this.st; if (!s || !s.reports.length) return [];
      const r = s.reports[0];
      return r.hot.map(h => ({ ...h, report: r.name }));
    },
    fmtRule(r, eid, field) {
      if (typeof r === 'string') r = { from: r }; else if (Array.isArray(r)) r = { enum: r };
      r = r || {}; const out = [];
      if (r.ignore) return [{ i: 'circle-x', t: '不校验', c: 'bg-wood-100 text-ink-soft' }];
      if (r.from) out.push({ i: 'file-json', t: r.from, c: 'bg-pond-50 text-pond-700' });
      if (r.map) out.push({ i: 'git-compare-arrows', t: typeof r.map === 'object' && !Array.isArray(r.map) ? Object.entries(r.map).map(([k, v]) => k + '→' + v).join(' / ') : [].concat(r.map).join(' → '), c: 'bg-lilac-50 text-lilac-600' });
      if (r.sql) out.push({ i: 'database', t: (r.db ? r.db + ': ' : '') + r.sql, c: 'bg-peach-50 text-peach-600' });
      if (r.enum) {
        let t;
        if (Array.isArray(r.enum)) {
          const dm = eid ? this.docMeanings(eid, field) : {}, any = r.enum.some(k => dm[String(k)]);
          t = r.enum.map(k => dm[String(k)] ? `${k} ${dm[String(k)]}` : String(k)).join(' · ') + (any ? '（含义取自埋点文档）' : '（含义未填，点编辑补上）');
        } else if (typeof r.enum === 'object') t = Object.entries(r.enum).map(([k, v]) => v !== '' && v !== null ? `${k} ${v}` : k).join(' · ');
        else t = '字典 ' + r.enum;
        out.push({ i: 'book-open', t, c: 'bg-sun-50 text-sun-700', w: true });
      }
      if (r.eq !== undefined) out.push({ i: 'equal', t: String(r.eq), c: 'bg-leaf-50 text-leaf-700' });
      if (r.expr) out.push({ i: 'sigma', t: r.expr, c: 'bg-leaf-50 text-leaf-700' });
      if (r.compare && r.compare !== 'eq') out.push({ i: 'sliders-horizontal', t: { lte: '上报 ≤ 期望', gte: '上报 ≥ 期望', lt: '上报 < 期望', gt: '上报 > 期望' }[r.compare], c: 'bg-wood-50 text-wood-600' });
      if (r.tolerance !== undefined) out.push({ i: 'sliders-horizontal', t: '容差 ±' + r.tolerance, c: 'bg-wood-50 text-wood-600' });
      if (r.trend) out.push({ i: 'trending-up', t: { up: '只增不减', down: '只减不增', same: '保持不变', strict_up: '必须增加' }[r.trend] || r.trend, c: 'bg-wood-50 text-wood-600' });
      if (r.ask) out.push({ i: 'user-check', t: '人工确认 / 按场景', c: 'bg-lilac-50 text-lilac-600' });
      if (r.type) out.push({ i: 'tag', t: '类型 ' + r.type, c: 'bg-wood-50 text-wood-600' });
      if (r.regex) out.push({ i: 'tag', t: '正则 ' + r.regex, c: 'bg-wood-50 text-wood-600' });
      if (r.range) out.push({ i: 'tag', t: '范围 ' + r.range.join('~'), c: 'bg-wood-50 text-wood-600' });
      if (r.not_empty) out.push({ i: 'tag', t: '不能为空', c: 'bg-wood-50 text-wood-600' });
      if (r.required === false) out.push({ i: 'tag', t: '可不上报', c: 'bg-wood-50 text-wood-600' });
      if (r.when) out.push({ i: 'filter', t: '当 ' + r.when, c: 'bg-wood-50 text-wood-600' });
      if (r.assert) out.push({ i: 'circle-check', t: '断言 ' + r.assert, c: 'bg-wood-50 text-wood-600' });
      if (r.len) out.push({ i: 'tag', t: '长度 ' + r.len.join('~'), c: 'bg-wood-50 text-wood-600' });
      if (r.allow_empty) out.push({ i: 'tag', t: '允许为空', c: 'bg-wood-50 text-wood-600' });
      if (r.severity === 'warn') out.push({ i: 'tag', t: '不通过只警告', c: 'bg-wood-50 text-wood-600' });
      const known = ['desc', 'ignore', 'from', 'map', 'sql', 'db', 'enum', 'allow_empty', 'eq', 'expr', 'compare', 'tolerance', 'trend', 'ask', 'type', 'regex', 'range', 'len', 'not_empty', 'required', 'when', 'assert', 'msg', 'severity'];
      Object.keys(r).filter(k => !known.includes(k)).forEach(k => out.push({ i: 'circle-alert', t: '不认识的写法 ' + k + ': ' + JSON.stringify(r[k]), c: 'bg-berry-50 text-berry-600' }));
      if (!out.length || (out.length === 1 && r.desc && Object.keys(r).length === 1)) out.push({ i: 'circle-alert', t: '还没设置', c: 'bg-berry-50 text-berry-600' });
      return out;
    },

    // ---------------- 给 AI 的提示词 ----------------
    async copyPrompt(id, line, fields) {
      const r = await this.call(() => this.api('/api/ai_prompt', { id, line, fields }));
      this.ai = { id, text: r.text };
      try { await navigator.clipboard.writeText(r.text); this.toast('提示词已复制，粘贴给 AI 即可', 'ok'); } catch (e) { /* 弹窗里手动复制 */ }
    },
    async copyAiAgain() { try { await navigator.clipboard.writeText(this.ai.text); this.toast('已复制', 'ok'); } catch (e) { this.toast('复制失败，请手动全选复制', 'err'); } },

    // ---------------- 实时调试 ----------------
    get askFields() {
      const a = this.st?.ask_fields || {}, only = splitList(this.live.only);
      return Object.entries(a).filter(([, v]) => !only.length || v.events.some(e => only.includes(e))).map(([k, v]) => ({ field: k, ...v }));
    },
    async setScn(field, val, applyPending) {
      this.live.scn = { ...this.live.scn, [field]: this.live.scn[field] === val ? '' : val };
      if (this.live.running || this.live.results.length) {
        await this.api('/api/live/scenario', { scenario: this.live.scn, apply_pending: !!applyPending }).catch(e => this.toast(e.message, 'err'));
        if (applyPending) await this.reloadLive();
      }
    },
    async applyScnToPending() {
      await this.call(() => this.api('/api/live/scenario', { scenario: this.live.scn, apply_pending: true }), '已按「本次操作」重新判定待确认的事件');
      await this.reloadLive();
    },
    async decide(r, f, value) {
      await this.api('/api/live/decide', { seq: r.seq, field: f.field, value }).catch(e => this.toast(e.message, 'err'));
      await this.reloadLive();
    },
    async startLive() {
      const only = this.live.onlyPlan && this.live.plan.length ? this.live.plan.map(r => r.id) : [];
      const r = await this.call(() => this.api('/api/live/start', { serial: this.live.serial, clear: this.live.clear, case: this.live.caseName, only, scenario: this.planScenario, snapshot: this.live.snapshot }));
      Object.assign(this.live, { results: [], running: true, sel: -1, report: null, snapErr: r.snap_err || '' });
      this.toast(r.snap_on ? '开始抓取，已开启 DB 快照，去手机上操作吧' : '开始抓取 logcat，去手机上操作吧', 'ok');
      if (r.snap_err) this.toast('DB 快照没开成：' + r.snap_err, 'warn');
      this.refresh();
    },
    async stopLive() {
      const r = await this.call(() => this.api('/api/live/stop', { pull: this.live.pull }));
      this.live.running = false; this.live.report = r.report;
      if (r.pull_log) this.pullLog = r.pull_log;
      await this.reloadLive();
      this.toast(r.summary.ok ? '本轮全部通过，报告已生成' : `报告已生成：失败 ${r.summary.fail} 条`, r.summary.ok ? 'ok' : 'warn');
      this.refresh();
    },
    async pollLive(force) {
      if (!force && !this.live.running) return;
      try {
        const d = await this.api('/api/live/poll?since=' + this.live.results.length);
        if (d.since === this.live.results.length) this.live.results.push(...d.results);
        this.live.running = d.running; this.live.raw = d.raw || this.live.raw;
        if (this.live.sel < 0 && this.liveList.length) this.live.sel = this.liveList[0].i;
      } catch (e) { /* 忽略一次轮询错误 */ }
    },
    async reloadLive() { const sel = this.live.sel; this.live.results = []; await this.pollLive(true); this.live.sel = sel; },
    async recheckLive() { await this.api('/api/live/recheck', {}); await this.reloadLive(); },
    get liveList() {
      const f = this.live.filter, su = this.live.showUnk;
      const keep = r => (su || r.status !== 'UNKNOWN') && (f === 'all' || (f === 'bad' ? (r.status === 'FAIL' || r.status === 'WARN') : f === 'ask' ? r.status === 'ASK' || r.fields.some(x => x.status === 'ASK') : r.status === 'PASS'));
      return this.live.results.map((r, i) => ({ r, i })).filter(({ r }) => keep(r)).reverse();
    },
    get liveCur() { return this.live.results[this.live.sel] || null; },
    get liveCounts() {
      const c = { PASS: 0, FAIL: 0, WARN: 0, ASK: 0, UNKNOWN: 0 }; this.live.results.forEach(r => { c[r.status] = (c[r.status] || 0) + 1; }); return c;
    },
    badCount(r) { return r.fields.filter(f => f.status === 'FAIL' || f.status === 'WARN').length + r.errors.length; },
    askCount(r) { return r.fields.filter(f => f.status === 'ASK').length; },
    async editFieldFromResult(r, f) {
      const ev = await this.api('/api/event?id=' + r.id);
      const rule = (ev.raw.fields || {})[f.field] ?? (ev.resolved.fields || {})[f.field] ?? {};
      this.openField({ field: f.field, rule, sample: r.data, eventId: r.id, raw: ev.raw, target: { kind: 'live' } });
    },
    async newEventFromResult(r) {
      const fields = {}; Object.keys(r.data).forEach(k => fields[k] = {});
      this.openEditor({ id: r.id, isNew: true, raw: { name: r.name || r.cls || r.id, fields }, sample: { fields: r.data, cls: r.cls, line: r.raw } });
      this.go('rules');
    },
    async openEventFromResult(r) { await this.loadEditor(r.id, { fields: r.data, cls: r.cls, line: r.raw }); this.go('rules'); },

    // ---------------- 用例 / 本次清单：点位 + 字段期望值 ----------------
    docMeanings(eid, field) {
      const out = {};
      this.docRowsFor(eid, field).forEach(r => extractPairs(Object.values(r).filter(v => String(v).trim() !== field).join('，'))
        .forEach(p => { if (!(p.k in out)) out[p.k] = p.v; }));
      return out;
    },
    _rowAll(row) {
      const ef = this.st?.event_fields?.[row.id] || {};
      const names = [...new Set([...Object.keys(ef), ...Object.keys(row.values || {})])];
      return names.map(n => {
        const f = { name: n, desc: '', options: {}, ask: false, kind: '', ...(ef[n] || {}) };
        if (Object.values(f.options).some(v => !v)) {   // 枚举没写含义的，去埋点文档里找
          const dm = this.docMeanings(row.id, n); f.options = Object.fromEntries(Object.entries(f.options).map(([k, v]) => [k, v || dm[k] || '']));
        }
        return f;
      }).filter(f => !f.ignore);
    },
    _isMain(row, f) { return f.ask || Object.keys(f.options).length || (row.values || {})[f.name] !== undefined; },
    rowFields(row) {
      const all = this._rowAll(row), main = all.filter(f => this._isMain(row, f));
      return row.showAll ? [...main, ...all.filter(f => !this._isMain(row, f))] : main;
    },
    rowHidden(row) { return this._rowAll(row).filter(f => !this._isMain(row, f)).length; },
    _vals(v) { return v === undefined || v === '' ? [] : (Array.isArray(v) ? v.map(String) : [String(v)]); },
    _setVals(row, f, list) { const v = { ...(row.values || {}) }; v[f] = list.length === 0 ? '' : list.length === 1 ? list[0] : list; row.values = v; },
    valOn(row, f, k) { return this._vals((row.values || {})[f]).includes(String(k)); },
    toggleVal(row, f, k) { const cur = this._vals((row.values || {})[f]); k = String(k); this._setVals(row, f, cur.includes(k) ? cur.filter(x => x !== k) : [...cur, k]); },
    valText(v) { return this._vals(v).join(' | '); },
    setValText(row, f, t) { this._setVals(row, f, String(t || '').split('|').map(x => x.trim()).filter(Boolean)); },
    addRowField(row, key) { key = String(key || '').trim(); if (!key || (row.values || {})[key] !== undefined) return; row.values = { ...(row.values || {}), [key]: '' }; },
    caseRows(c) {
      return (c?.expect || []).map(x => { const o = typeof x === 'object' ? x : { id: x };
        return { key: Math.random(), id: String(o.id), min: o.min ?? 1, max: o.max ?? '', where: o.where || null, values: clone(o.values || {}), showAll: false }; });
    },
    rowsToExpect(rows) {
      return rows.filter(r => r.id).map(r => {
        const o = { id: r.id };
        if (r.min !== '' && r.min !== null && Number(r.min) !== 1) o.min = Number(r.min);
        if (r.max !== '' && r.max !== null && r.max !== undefined) o.max = Number(r.max);
        if (r.where && Object.keys(r.where).length) o.where = r.where;
        const v = {}; Object.entries(r.values || {}).forEach(([k, x]) => { if (x !== '' && !(Array.isArray(x) && !x.length)) v[k] = x; });
        if (Object.keys(v).length) o.values = v;
        return Object.keys(o).length === 1 ? r.id : o;
      });
    },
    pickId(t) {
      t = String(t || '').trim(); if (!t) return '';
      if (this.evPickList.some(x => x.id === t) || /^\d+$/.test(t)) return t;
      const m = t.match(/\d{6,}/); if (m) return m[0];
      const e = this.evPickList.find(x => (x.name || '') === t) || this.evPickList.find(x => (x.name || '').includes(t));
      return e ? e.id : '';
    },
    loadPlanFromCase() { this.live.plan = this.caseRows(this.st?.cases?.[this.live.caseName]); this.pushPlan(); },
    addPlanRow() {
      const id = this.pickId(this.live.addId); if (!id) return;
      if (!this.live.plan.some(r => r.id === id)) this.live.plan.push({ key: Math.random(), id, min: 1, max: '', values: {}, showAll: false });
      this.live.addId = '';
    },
    removePlanRow(ri) {
      const removed = this.live.plan.splice(ri, 1)[0]; if (!removed) return;
      this.pushPlan();
      this.toast(`已移出点位 ${this.eventName(removed.id) || removed.id}`, 'warn', {
        label: '撤销', fn: () => { this.live.plan.splice(Math.min(ri, this.live.plan.length), 0, removed); this.pushPlan(); }
      });
    },
    get planScenario() {
      const o = {};
      this.live.plan.forEach(r => Object.entries(r.values || {}).forEach(([k, v]) => { if (v !== '' && !(Array.isArray(v) && !v.length)) o[r.id + '.' + k] = v; }));
      return o;
    },
    pushPlan() {
      clearTimeout(this._planT);
      this._planT = setTimeout(async () => {
        if (!this.live.running && !this.live.results.length) return;
        await this.api('/api/live/scenario', { scenario: this.planScenario, apply_pending: 'all' }).catch(e => this.toast(e.message, 'err'));
        await this.reloadLive();
      }, 350);
    },
    liveHits(id) { return this.live.results.filter(r => r.id === id).length; },
    async savePlanToCase() {
      let name = this.live.caseName, isNew = !name;
      if (isNew) { const d = new Date(), p = n => String(n).padStart(2, '0'); name = `调试清单_${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`; }
      const old = this.st.cases[name] || {}, value = {};
      if (old.desc) value.desc = old.desc; value.expect = this.rowsToExpect(this.live.plan);
      await this.call(() => this.api('/api/case/save', { name, old_name: isNew ? '' : name, value }), isNew ? `已存为用例「${name}」，可以去「测试用例」改名` : `已保存回用例「${name}」`);
      await this.refresh(); this.live.caseName = name;
    },

    // ---------------- 埋点字典 ----------------
    async loadEvdict() {
      try { const d = await this.api('/api/evdict'); Object.assign(this.evd, { entries: d.entries, docs: d.docs, loaded: true }); }
      catch (e) { this.toast(e.message, 'err'); }
    },
    importDocs(ev) {
      const files = [...(ev.target.files || ev.dataTransfer?.files || [])]; if (ev.target.value !== undefined) ev.target.value = '';
      (async () => {
        for (const f of files) {
          const text = await f.text();
          try {
            const r = await this.call(() => this.api('/api/evdict/import', { filename: f.name, content: text, overwrite: this.evd.overwrite }));
            this.evd.result = { file: f.name, ...r };
            this.toast(`${f.name}：解析 ${r.parsed} 个点位，新增 ${r.added}，跳过重复 ${r.skipped}` + (r.updated ? `，覆盖 ${r.updated}` : ''), 'ok');
          } catch (e) { /* 已提示 */ }
        }
        await this.loadEvdict(); this.refresh();
      })();
    },
    get evdView() {
      const q = this.evd.q.trim().toLowerCase(), f = this.evd.filter;
      return this.evd.entries.filter(e => (f === 'all' || (f === 'todo' ? !e.configured : e.configured))
        && (!q || e.id.includes(q) || (e.name || '').toLowerCase().includes(q) || JSON.stringify(e.rows).toLowerCase().includes(q) || JSON.stringify(e.columns).toLowerCase().includes(q)));
    },
    evdMainCols(e) {
      // 每个点位共有的列（动作、上报时机、组件……）
      return Object.entries(e.columns || {}).filter(([k, v]) => v && !this.evdParamCols(e).includes(k) && !/^(点位|埋点|事件)?\s*(id|ID|编号)$/.test(k) && v !== e.name && v !== e.id);
    },
    evdParamCols(e) {
      // 参数相关的列：多行里有 ≥2 行有值、且值不全相同（合并单元格那种只有第一行有值的列不算）
      const rows = e.rows || []; if (rows.length < 2) return [];
      return Object.keys(rows[0]).filter(k => {
        const vals = rows.map(r => (r[k] || '').trim()).filter(Boolean);
        return vals.length >= 2 && new Set(vals).size > 1;
      });
    },
    evdEntry(id) { return this.evd.entries.find(e => e.id === String(id)); },
    async loadEvdSamples() {
      const r = await this.call(() => this.api('/api/evdict/samples', {}));
      this.toast(`已导入示例文档 ${r.docs.join('、')}：新增 ${r.added} 个点位`, 'ok'); await this.loadEvdict(); this.refresh();
    },
    async delEvd(id) {
      if (!confirm(id ? `从埋点字典删除 ${id}？` : '清空整个埋点字典？')) return;
      await this.call(() => this.api('/api/evdict/delete', id ? { id } : { all: true }), '已删除');
      await this.loadEvdict(); this.refresh();
    },
    configFromDict(e) {
      if (this.st.events.some(x => x.id === e.id)) this.loadEditor(e.id);
      else {
        const fields = {};
        const pc = this.evdParamCols(e).find(k => /参数|字段|key|param/i.test(k)) || this.evdParamCols(e)[0];
        (e.rows || []).forEach(r => { const k = (r[pc] || '').trim(); if (/^[A-Za-z_][\w.]*$/.test(k)) fields[k] = {}; });
        this.openEditor({ id: e.id, isNew: true, raw: { name: e.name, fields } });
      }
      this.go('rules');
    },
    // 埋点文档里关于某个字段的说明行
    docRowsFor(eid, field) {
      const e = this.evdEntry(eid); if (!e) return [];
      return (e.rows || []).filter(r => Object.values(r).some(v => String(v).trim() === field));
    },

    // ---------------- 字段编辑弹窗 ----------------
    openField({ field, rule, sample, eventId, raw, target, isNew }) {
      rule = clone(rule) || {};
      if (typeof rule === 'string') rule = { from: rule }; else if (Array.isArray(rule)) rule = { enum: rule };
      let tab = 'none';
      if (rule.ignore) tab = 'ignore'; else if (rule.sql) tab = 'sql'; else if (rule.from) tab = 'api';
      else if (rule.enum !== undefined) tab = 'enum'; else if (rule.eq !== undefined) tab = 'eq'; else if (rule.expr) tab = 'expr';
      const enumIsDict = typeof rule.enum === 'string';
      const enumInline = rule.enum && typeof rule.enum === 'object' && !Array.isArray(rule.enum);
      const mapInline = rule.map && typeof rule.map === 'object' && !Array.isArray(rule.map);
      const toRows = o => Object.entries(o || {}).map(([k, v]) => ({ k: String(k), v: String(v ?? '') }));
      const jsonSrcs = Object.entries(this.st.sources).filter(([, v]) => (v.type || 'json_file') !== 'sqlite').map(([k]) => k);
      const dbs = Object.entries(this.st.sources).filter(([, v]) => v.type === 'sqlite').map(([k]) => k);
      if (!this.evd.loaded) this.loadEvdict();
      this.fm = {
        field, eventId, raw: clone(raw) || {}, target, sample: sample || {}, tab, isNew: !!isNew,
        desc: rule.desc || '', from: rule.from || '',
        mapMode: !rule.map ? 'none' : mapInline ? 'inline' : 'shared',
        map: mapInline ? [] : [].concat(rule.map || []).filter(x => typeof x === 'string'),
        mapRows: mapInline ? toRows(rule.map) : [], mapHint: null, mapBulk: '',
        sql: rule.sql || '', db: rule.db || dbs[0] || '', dbs, schema: null,
        enumMode: enumIsDict ? 'dict' : 'inline', enumDict: enumIsDict ? rule.enum : '',
        enumRows: enumInline ? toRows(rule.enum) : Array.isArray(rule.enum) ? rule.enum.map(x => ({ k: String(x), v: '' })) : [], enumBulk: '',
        allowEmpty: !!rule.allow_empty, eq: rule.eq ?? '', expr: rule.expr || '',
        cmp: rule.compare || 'eq', tol: rule.tolerance ?? '', trend: rule.trend || '', ask: !!rule.ask,
        type: rule.type || '', regex: rule.regex || '', rmin: rule.range ? rule.range[0] : '', rmax: rule.range ? rule.range[1] : '',
        notEmpty: !!rule.not_empty, optional: rule.required === false, when: rule.when || '', warn: rule.severity === 'warn',
        assert: rule.assert || '', msg: rule.msg || '',
        jsonSrc: jsonSrcs[0] || '', jsonSrcs, jsonRows: [], jsonQ: '', jsonOnlyHit: true, jsonErr: '',
        newCtx: {}, note: '', sqlRes: null, tryRes: null,
        showMore: !!(rule.type || rule.regex || rule.range || rule.when || rule.not_empty || rule.assert),
      };
      if (tab === 'api' || tab === 'none') this.loadJson();
      if (tab === 'sql') this.loadSchema();
    },
    get fmValue() { return this.fm ? norm(this.fm.sample[this.fm.field]) : ''; },
    get fmDoc() { return this.fm && this.fm.field ? this.docRowsFor(this.fm.eventId, this.fm.field) : []; },
    get fmKeyHints() {
      const m = this.fm; if (!m) return [];
      const out = new Set(Object.keys(m.sample || {})), e = this.evdEntry(m.eventId);
      if (e) {
        const pcs = this.evdParamCols(e), pc = pcs.find(k => /参数|字段|key|param/i.test(k)) || pcs[0];
        (e.rows || []).forEach(r => { const k = String(r[pc] || '').trim(); if (/^[A-Za-z_][\w.]*$/.test(k)) out.add(k); });
      }
      const have = new Set(Object.keys(this.ed?.raw.fields || {}));
      return [...out].filter(k => !have.has(k));
    },
    fmDocDesc(field) {
      const r = this.docRowsFor(this.fm?.eventId, field)[0]; if (!r) return '';
      const k = Object.keys(r).find(c => /含义|说明|名称|描述|中文/.test(c) && String(r[c]).trim() && String(r[c]).trim() !== field);
      return k ? String(r[k]).trim().split(/[，,；;\n]/)[0].slice(0, 30) : '';
    },
    fillEnumFromDoc(text) {
      const pairs = extractPairs(text); if (!pairs.length) return this.toast('这段说明里没认出「值 含义」对，手动填吧', 'err');
      const m = this.fm; m.tab = 'enum'; m.enumMode = 'inline';
      pairs.forEach(p => { const r = m.enumRows.find(x => x.k === p.k); if (r) r.v = p.v; else m.enumRows.push(p); });
      this.tryField();
    },
    async loadJson() {
      const m = this.fm; if (!m || !m.jsonSrc) return;
      try {
        const d = await this.api('/api/source_json?name=' + encodeURIComponent(m.jsonSrc));
        const rows = []; const walk = (v, path, key, depth) => {
          if (typeof v === 'string' && /^\s*[\[{]/.test(v)) { try { v = JSON.parse(v); } catch (e) { } }
          if (Array.isArray(v)) { rows.push({ path, key, depth, kind: 'arr', n: v.length }); v.forEach((x, i) => walk(x, `${path}[${i}]`, `[${i}]`, depth + 1)); }
          else if (v && typeof v === 'object') { if (path) rows.push({ path, key, depth, kind: 'obj', n: Object.keys(v).length }); Object.entries(v).forEach(([k, x]) => walk(x, path ? `${path}.${k}` : k, k, depth + (path ? 1 : 0))); }
          else rows.push({ path, key, depth, kind: 'leaf', value: v });
        };
        walk(d.data, '', '', 0); m.jsonRows = rows; m.jsonErr = '';
        m.jsonOnlyHit = !!this.fmValue && rows.some(r => r.kind === 'leaf' && norm(r.value) === this.fmValue);
      } catch (e) { m.jsonErr = e.message; m.jsonRows = []; }
    },
    get jsonView() {
      const m = this.fm; if (!m) return [];
      const q = m.jsonQ.trim().toLowerCase(), v = this.fmValue;
      let rows = m.jsonRows;
      if (m.jsonOnlyHit && v) rows = rows.filter(r => r.kind === 'leaf' && norm(r.value) === v);
      else if (q) rows = rows.filter(r => r.path.toLowerCase().includes(q) || (r.kind === 'leaf' && norm(r.value).toLowerCase().includes(q)));
      return rows.slice(0, 400);
    },
    async pickJson(row) {
      const m = this.fm;
      const r = await this.call(() => this.api('/api/suggest', { id: m.eventId, source: m.jsonSrc, path: row.path, fields: m.sample, raw: this.fmRawWithCtx() }));
      m.from = r.from; m.newCtx = { ...m.newCtx, ...r.contexts }; m.note = r.note; m.tab = 'api';
      const apiV = norm(r.value);
      m.mapHint = (r.value === null || typeof r.value !== 'object') && this.fmValue !== '' && apiV !== this.fmValue ? { k: apiV, v: this.fmValue } : null;
      this.tryField();
    },
    rowsObj(rows) { return Object.fromEntries(rows.filter(x => x.k !== '').map(x => [x.k, x.v])); },
    importRows(text, rows) {
      String(text || '').split(/\n/).forEach(line => {
        const m = line.trim().match(/^([^:：=\t ]+)\s*[:：=\t ]\s*(.+)$/); if (m) rows.push({ k: m[1].trim(), v: m[2].trim() });
      });
    },
    addMapHint() {
      const m = this.fm, h = m.mapHint; if (!h) return;
      m.mapMode = 'inline';
      const row = m.mapRows.find(x => x.k === h.k); if (row) row.v = h.v; else m.mapRows.push({ ...h });
      m.mapHint = null; this.tryField();
    },
    fmRawWithCtx() {
      const m = this.fm; const raw = clone(m.raw) || {};
      if (Object.keys(m.newCtx).length) raw.context = { ...(raw.context || {}), ...m.newCtx };
      return raw;
    },
    async loadSchema() {
      const m = this.fm; if (!m || !m.db) return;
      try { m.schema = (await this.api('/api/db_schema?db=' + encodeURIComponent(m.db))).tables; } catch (e) { m.schema = null; this.toast(e.message, 'err'); }
    },
    insertSql(t, c) {
      const m = this.fm; const key = Object.keys(m.sample).find(k => /id$/i.test(k)) || 'xxx';
      m.sql = `SELECT ${c} FROM ${t} WHERE ??? = :${key}`;
    },
    sqlCount(t) { this.fm.sql = `SELECT count(*) FROM ${t}`; this.runSql(); },
    async runSql() {
      const m = this.fm;
      try { m.sqlRes = await this.api('/api/sql', { db: m.db, sql: m.sql, fields: m.sample }); }
      catch (e) { m.sqlRes = { error: e.message }; }
    },
    async smartSql() {
      const m = this.fm;
      const r = await this.call(() => this.api('/api/suggest_sql', { id: m.eventId, field: m.field, fields: m.sample }));
      m.sql = r.sql; m.db = r.db; this.runSql();
    },
    sqlHit() { const m = this.fm; return m?.sqlRes?.rows?.some(r => norm(r[0]) === this.fmValue); },
    sumOfFields() {
      const m = this.fm, others = Object.keys(m.sample).filter(k => k !== m.field);
      const pick = prompt('等于哪些字段之和？（逗号分隔）', others.filter(k => /num|count|cnt/i.test(k)).join(', '));
      if (!pick) return;
      m.expr = splitList(pick).map(k => `int(${k} or 0)`).join(' + '); m.tab = 'expr'; this.tryField();
    },
    buildRule() {
      const m = this.fm, r = {};
      if (m.desc) r.desc = m.desc;
      if (m.tab === 'api' && m.from) {
        r.from = m.from;
        if (m.mapMode === 'shared' && m.map.length) r.map = m.map.length === 1 ? m.map[0] : m.map;
        if (m.mapMode === 'inline') { const o = this.rowsObj(m.mapRows); if (Object.keys(o).length) r.map = o; }
      }
      if (m.tab === 'sql' && m.sql) { r.sql = m.sql; if (m.db) r.db = m.db; }
      if (m.tab === 'enum') {
        if (m.enumMode === 'dict' && m.enumDict) r.enum = m.enumDict;
        else { const o = this.rowsObj(m.enumRows); if (Object.keys(o).length) r.enum = Object.values(o).every(v => v === '') ? Object.keys(o) : o; }
        if (m.allowEmpty) r.allow_empty = true;
      }
      if (m.tab === 'eq') r.eq = m.eq;
      if (m.tab === 'expr' && m.expr) r.expr = m.expr;
      if (m.tab === 'ignore') r.ignore = true;
      if (['api', 'sql', 'expr', 'eq'].includes(m.tab)) {
        if (m.cmp && m.cmp !== 'eq') r.compare = m.cmp;
        if (m.tol !== '' && m.tol !== null) r.tolerance = Number(m.tol);
      }
      if (m.trend) r.trend = m.trend;
      if (m.ask) r.ask = true;
      if (m.type) r.type = m.type;
      if (m.regex) r.regex = m.regex;
      if (m.rmin !== '' || m.rmax !== '') r.range = [m.rmin === '' ? null : Number(m.rmin), m.rmax === '' ? null : Number(m.rmax)];
      if (m.notEmpty) r.not_empty = true;
      if (m.optional) r.required = false;
      if (m.when) r.when = m.when;
      if (m.assert) { r.assert = m.assert; if (m.msg) r.msg = m.msg; }
      if (m.warn) r.severity = 'warn';
      return r;
    },
    async tryField() {
      const m = this.fm; if (!m || !m.field || !Object.keys(m.sample).length) return;
      const raw = this.fmRawWithCtx(); raw.fields = { ...(raw.fields || {}), [m.field]: this.buildRule() };
      try {
        const res = await this.api('/api/try', { id: m.eventId, fields: m.sample, raw });
        m.tryRes = res.fields.find(f => f.field === m.field) || null;
      } catch (e) { m.tryRes = { status: 'FAIL', reason: e.message }; }
    },
    async saveField() {
      const m = this.fm, rule = this.buildRule(), t = m.target;
      m.field = String(m.field || '').trim();
      if (!m.field) return this.toast('先填字段名（上报的 key）', 'err');
      if (m.isNew && t.kind === 'editor' && (this.ed.raw.fields || {})[m.field] !== undefined && !confirm(`字段 ${m.field} 已经有规则了，覆盖它？`)) return;
      if (t.kind === 'editor') {
        this.ed.raw.fields = { ...(this.ed.raw.fields || {}), [m.field]: rule };
        if (Object.keys(m.newCtx).length) this.ed.raw.context = { ...(this.ed.raw.context || {}), ...m.newCtx };
        this.ed.dirty = true; this.fm = null; this.tryEditor(); return;
      }
      if (t.kind === 'draft') {
        const d = this.inf.drafts[t.di];
        Object.assign(d.fields[t.fi], { rule, status: 'ok', note: '已手动确认' });
        if (Object.keys(m.newCtx).length) d.context = { ...d.context, ...m.newCtx };
        this.fm = null; return;
      }
      if (t.kind === 'live') {
        const raw = this.fmRawWithCtx(); raw.fields = { ...(raw.fields || {}), [m.field]: rule };
        await this.call(() => this.api('/api/event/save', { id: m.eventId, raw }), `已保存 ${m.field} 的规则，以后自动复用`);
        this.fm = null; await this.refresh(); await this.recheckLive();
      }
    },

    // ---------------- 规则草稿 ----------------
    readFile(ev, target) {
      const f = ev.target.files[0]; if (!f) return;
      const rd = new FileReader(); rd.onload = () => { target.content = rd.result; target.fileName = f.name; }; rd.readAsText(f, 'utf-8');
    },
    logBody(t) { return t.content ? { content: t.content } : { path: t.path }; },
    async runInfer() {
      const i = this.inf;
      if (!i.content && !i.path) return this.toast('先选一份会话日志，或粘贴 / 上传日志', 'err');
      const r = await this.call(() => this.api('/api/infer', this.logBody(i)));
      r.drafts.forEach(d => {
        d.context = {}; d.contexts.forEach(c => d.context[c.name] = { from: c.from, path: c.path, where: c.where });
        d.saved = false; d.merge = false;
      });
      Object.assign(i, { drafts: r.drafts, dicts: r.dicts, warns: r.warns, log: r.log, open: 0 });
      this.toast(`生成了 ${r.drafts.length} 个点位的草稿`, 'ok'); this.refresh();
    },
    draftCount(d, s) { return d.fields.filter(f => f.status === s).length; },
    editDraftField(di, fi) {
      const d = this.inf.drafts[di], f = d.fields[fi];
      this.openField({ field: f.field, rule: f.rule, sample: d.sample_fields, eventId: d.id, raw: { name: d.name, context: d.context, fields: Object.fromEntries(d.fields.map(x => [x.field, x.rule])) }, target: { kind: 'draft', di, fi } });
    },
    async acceptDraft(d) {
      await this.call(() => this.api('/api/infer/accept', {
        id: d.id, name: d.name, context: d.context, merge: d.merge, fields: d.fields.map(f => ({ field: f.field, rule: f.rule })), dicts: {},
      }), `「${d.name}」已保存到规则文件，以后自动复用`);
      d.saved = true; d.exists = true; this.refresh();
    },

    // ---------------- 点位规则编辑器 ----------------
    get ruleList() {
      const q = (this.ruleFilter || this.search).trim().toLowerCase();
      const s = this.st; if (!s) return [];
      if (this.ruleTab === 'tpl') return Object.keys(s.templates || {}).filter(n => !q || n.toLowerCase().includes(q))
        .map(n => ({ id: n, name: n, isTpl: true, refs: (s.tpl_refs?.[n] || []).length }));
      const conf = new Set(s.events.map(e => e.id));
      let list = [];
      if (this.ruleTab !== 'todo') list = list.concat(s.events);
      if (this.ruleTab !== 'done') list = list.concat(s.catalog.filter(c => !conf.has(c.id)).map(c => ({ id: c.id, name: c.name, nfields: 0, unconfigured: true })));
      return list.filter(e => !q || e.id.includes(q) || (e.name || '').toLowerCase().includes(q)).slice(0, 500);
    },
    openRuleItem(e) {
      if (e.isTpl) return this.openTemplate(e.id);
      if (e.unconfigured) { const d = this.evdEntry(e.id); d ? this.configFromDict(d) : this.openEditor({ id: e.id, isNew: true, raw: { name: e.name, fields: {} } }); }
      else this.loadEditor(e.id);
    },
    openTemplate(name) {
      const raw = clone(this.st.templates[name]) || {};
      this.openEditor({ id: name, raw, resolved: raw, kind: 'template' });
      if (this.page !== 'rules') this.go('rules');
    },
    newTemplate() { this.openEditor({ id: '', isNew: true, raw: { context: {}, fields: {} }, kind: 'template' }); },
    async deleteTemplate() {
      const e = this.ed, refs = this.st.tpl_refs?.[e.oldId] || [];
      if (!confirm(refs.length ? `模板 ${e.oldId} 还被 ${refs.length} 个点位继承，删除后这些点位会报错。确定删除？` : `删除模板 ${e.oldId}？`)) return;
      await this.call(() => this.api('/api/item/delete', { section: 'templates', name: e.oldId }), '已删除'); this.ed = null; this.refresh();
    },
    openEditor({ id, isNew, raw, resolved, file, sample, kind }) {
      raw = clone(raw) || {};
      if (!this.evd.loaded) this.loadEvdict();
      this.ed = {
        id, oldId: isNew ? '' : id, isNew, raw, resolved: resolved || {}, file: file || '', kind: kind || 'event',
        sampleText: sample?.line || '', sample: sample || null, tryRes: null, dirty: !!isNew, showDoc: true,
        dedupe: (raw.dedupe ? [].concat(raw.dedupe) : []).join(', '),
        asserts: (raw.asserts || []).map(a => typeof a === 'string' ? { expr: a, msg: '' } : { expr: a.expr || '', msg: a.msg || '' }),
      };
      if (sample) this.tryEditor();
    },
    async loadEditor(id, sample) {
      const ev = await this.call(() => this.api('/api/event?id=' + id));
      this.openEditor({ id, raw: ev.raw, resolved: ev.resolved, file: ev.file, sample });
    },
    newEvent() { this.openEditor({ id: '', isNew: true, raw: { name: '', fields: {} } }); },
    get edFields() {
      const e = this.ed; if (!e) return [];
      const own = e.raw.fields || {}, inh = (e.resolved && e.resolved.fields) || {};
      const names = [...new Set([...Object.keys(inh), ...Object.keys(own), ...Object.keys(e.sample?.fields || {})])];
      return names.map(n => ({
        name: n, rule: own[n] ?? (e.raw.extends ? inh[n] : undefined), own: n in own, inherited: !(n in own) && !!e.raw.extends && n in inh,
        res: e.tryRes?.fields.find(f => f.field === n), value: e.sample?.fields?.[n],
      }));
    },
    get edDoc() { return this.ed ? this.evdEntry(this.ed.id) : null; },
    get edContexts() { const e = this.ed; return Object.entries(e?.raw.context || {}).map(([name, c]) => ({ name, ...c })); },
    get edInheritedCtx() {
      const e = this.ed; if (!e || !e.raw.extends) return [];
      const own = e.raw.context || {}; return Object.entries(e.resolved.context || {}).filter(([k]) => !(k in own)).map(([name, c]) => ({ name, ...c }));
    },
    ctxSources() { return [...Object.keys(this.st.sources), ...Object.keys(this.ed?.resolved?.context || {}), ...Object.keys(this.ed?.raw.context || {})].filter((v, i, a) => a.indexOf(v) === i); },
    addCtx() { const e = this.ed; const c = { ...(e.raw.context || {}) }; let n = 'node', i = 1; while (c[n]) n = 'node' + (++i); c[n] = { from: Object.keys(this.st.sources)[0] || '', path: '', where: {} }; e.raw.context = c; e.dirty = true; },
    setCtx(oldName, key, val) {
      const e = this.ed, c = {}; Object.entries(e.raw.context || {}).forEach(([k, v]) => {
        if (k !== oldName) { c[k] = v; return; }
        if (key === 'name') { c[val || k] = v; } else c[k] = { ...v, [key]: key === 'where' ? parseWhere(val) : val };
      }); e.raw.context = c; e.dirty = true;
    },
    delCtx(name) { const e = this.ed, c = { ...(e.raw.context || {}) }; delete c[name]; e.raw.context = c; e.dirty = true; },
    editEdField(f) {
      const e = this.ed;
      this.openField({ field: f.name, rule: f.rule, sample: e.sample?.fields || {}, eventId: e.id || 'new', raw: this.edRaw(), target: { kind: 'editor' } });
    },
    delEdField(name) { const e = this.ed, f = { ...(e.raw.fields || {}) }; delete f[name]; e.raw.fields = f; e.dirty = true; },
    addEdField() {
      const e = this.ed;
      this.openField({ field: '', rule: {}, sample: e.sample?.fields || {}, eventId: e.id || 'new', raw: this.edRaw(), target: { kind: 'editor' }, isNew: true });
    },
    addSumAssert() {
      const e = this.ed, names = Object.keys({ ...(e.resolved.fields || {}), ...(e.raw.fields || {}) });
      const total = prompt('总数字段（如 allNums）', names.find(n => /^all|total/i.test(n)) || '');
      if (!total) return;
      const parts = prompt('等于哪些字段之和（逗号分隔）', names.filter(n => n !== total && /num|count/i.test(n)).join(', '));
      if (!parts) return;
      e.asserts.push({ expr: `int(${total} or 0) == ` + splitList(parts).map(k => `int(${k} or 0)`).join(' + '), msg: `${total} 应等于 ${splitList(parts).join(' + ')}` });
      e.dirty = true; this.tryEditor();
    },
    edRaw() {
      const e = this.ed, raw = clone(e.raw);
      const dd = splitList(e.dedupe); if (dd.length) raw.dedupe = dd; else delete raw.dedupe;
      const as = (e.asserts || []).filter(a => a.expr.trim()).map(a => a.msg ? { expr: a.expr, msg: a.msg } : a.expr);
      if (as.length) raw.asserts = as; else delete raw.asserts;
      if (raw.extends === '') delete raw.extends;
      return raw;
    },
    async parseSample() {
      const e = this.ed;
      const r = await this.call(() => this.api('/api/parse', { text: e.sampleText, id: e.id }));
      if (!r.events.length) return this.toast('没解析出埋点，检查格式（需要包含 Event: id: ... data: ...）', 'err');
      const ev = r.events.find(x => !e.id || x.id === e.id) || r.events[0];
      if (!e.id && e.kind !== 'template') e.id = ev.id;
      if (!e.raw.name) e.raw.name = this.eventName(ev.id) || ev.cls || ev.id;
      e.sample = { fields: ev.fields, cls: ev.cls, line: ev.raw };
      this.tryEditor();
    },
    async tryEditor() {
      const e = this.ed; if (!e?.sample || !e.id) return;
      try { e.tryRes = await this.api('/api/try', { id: e.id, fields: e.sample.fields, cls: e.sample.cls, raw: this.edRaw() }); }
      catch (err) { this.toast(err.message, 'err'); }
    },
    async saveEditor() {
      const e = this.ed;
      if (e.kind === 'template') {
        if (!e.id) return this.toast('先填模板名', 'err');
        const raw = this.edRaw(); delete raw.name; delete raw.extends;
        await this.call(() => this.api('/api/template/save', { name: e.id, old_name: e.oldId, value: raw }), '模板已保存，继承它的点位都会用新规则');
        e.isNew = false; e.oldId = e.id; e.dirty = false; await this.refresh();
        e.resolved = clone(this.st.templates[e.id]) || {};
        if (this.live.results.length) this.recheckLive();
        return;
      }
      if (!e.id) return this.toast('先填点位ID', 'err');
      await this.call(() => this.api('/api/event/save', { id: e.id, old_id: e.oldId, raw: this.edRaw() }), '保存成功，以后测试这个点位自动复用');
      e.isNew = false; e.oldId = e.id; e.dirty = false;
      await this.refresh(); const ev = await this.api('/api/event?id=' + e.id); e.resolved = ev.resolved; e.file = ev.file;
      if (this.live.results.length) this.recheckLive();
    },
    async deleteEditor() {
      const e = this.ed; if (!confirm(`确定删除点位 ${e.id} 的规则？`)) return;
      await this.call(() => this.api('/api/event/delete', { id: e.id }), '已删除');
      this.ed = null; this.refresh();
    },

    // ---------------- 数据源 ----------------
    async loadDs() { try { const d = await this.api('/api/ds/list'); this.ds.files = d.files; this.ds.dir = d.dir; } catch (e) { this.toast(e.message, 'err'); } },
    uploadDs(ev) {
      const files = [...(ev.target?.files || ev.dataTransfer?.files || [])]; if (ev.target?.value !== undefined) ev.target.value = '';
      this.ds.drag = false;
      (async () => {
        for (const f of files) {
          const b64 = await new Promise(res => { const rd = new FileReader(); rd.onload = () => res(String(rd.result).split(',')[1] || ''); rd.readAsDataURL(f); });
          await this.call(() => this.api('/api/ds/upload', { filename: f.name, b64 }), `已放入数据源目录：${f.name}`).catch(() => {});
        }
        await this.loadDs(); this.refresh();
      })();
    },
    async pasteDs() {
      await this.call(() => this.api('/api/ds/paste', { name: this.ds.pasteName, text: this.ds.pasteText }), '已保存接口 JSON');
      Object.assign(this.ds, { pasteName: '', pasteText: '', showPaste: false }); await this.loadDs(); this.refresh();
    },
    async delDs(f) {
      if (!confirm(`删除数据源文件 ${f}？`)) return;
      await this.call(() => this.api('/api/ds/delete', { file: f }), '已删除'); await this.loadDs(); this.refresh();
    },
    async listDevDbs() {
      const r = await this.call(() => this.api('/api/device/dbs', { package: this.ds.pkg, serial: this.live.serial }));
      this.ds.devDbs = r.dbs; if (!this.ds.dbs.length) this.ds.dbs = r.dbs.filter(x => /\.db$/.test(x)).slice(0, 1);
    },
    async pullDbs() {
      const r = await this.call(() => this.api('/api/pull/dbs', { package: this.ds.pkg, dbs: this.ds.dbs, serial: this.live.serial }));
      this.toast(`已拉取 ${r.ok.join('、')}` + (r.errors.length ? `；失败：${r.errors.join('；')}` : ''), r.errors.length ? 'warn' : 'ok');
      await this.loadDs(); this.refresh();
    },
    async pullApiDump() {
      const r = await this.call(() => this.api('/api/pull/apidump', { package: this.ds.pkg, serial: this.live.serial }), '已拉取接口 dump');
      this.ds.log = r.log; await this.loadDs(); this.refresh();
    },
    openSrc(name) {
      const s = name ? clone(this.st.sources_explicit[name]) : { type: 'sqlite_json' };
      this.srcEd = { name: name || '', old: name || '', type: s.type || 'json_file', path: s.path || '', db: s.db || '', sql: s.sql || '', root: s.root || '' };
    },
    async saveSrc() {
      const s = this.srcEd, v = { type: s.type };
      if (s.type !== 'sqlite_json') v.path = s.path; else { v.db = s.db; v.sql = s.sql; }
      if (s.root) v.root = s.root;
      await this.call(() => this.api('/api/source/save', { name: s.name, old_name: s.old, value: v }), '数据源已保存');
      this.srcEd = null; this.refresh();
    },
    async delItem(section, name) {
      if (!confirm(`确定删除 ${name}？`)) return;
      await this.call(() => this.api('/api/item/delete', { section, name }), '已删除'); this.srcEd = this.caseEd = null; this.refresh();
    },
    openSettings() {
      const s = this.st;
      this.settingsEd = {
        pattern: s.log.pattern || '', logcat_args: (s.log.logcat_args || []).join(' '),
        null_equals_empty: s.options.null_equals_empty !== false, strict_fields: !!s.options.strict_fields,
      };
    },
    async saveSettings() {
      const e = this.settingsEd;
      await this.call(() => this.api('/api/settings/save', {
        log: { pattern: e.pattern, logcat_args: e.logcat_args.split(/\s+/).filter(Boolean) },
        options: { ...(this.st.options || {}), null_equals_empty: e.null_equals_empty, strict_fields: e.strict_fields },
      }), '设置已保存');
      this.refresh();
    },

    // ---------------- 导入 / 分享 ----------------
    get expList() {
      const q = this.exp.q.trim().toLowerCase();
      return (this.st?.events || []).filter(e => !q || e.id.includes(q) || (e.name || '').toLowerCase().includes(q));
    },
    doExport() {
      const p = new URLSearchParams();
      if (this.exp.mode === 'pick') { if (!this.exp.ids.length) return this.toast('先勾选要分享的点位', 'err'); p.set('ids', this.exp.ids.join(',')); }
      if (this.exp.author) p.set('author', this.exp.author);
      if (this.exp.note) p.set('note', this.exp.note);
      if (this.exp.sources) p.set('sources', '1');
      window.location.href = '/api/export?' + p.toString();
    },
    readImport(ev) {
      const f = ev.target.files[0]; if (!f) return;
      const rd = new FileReader();
      rd.onload = async () => { this.imp.content = rd.result; this.imp.fileName = f.name; ev.target.value = ''; await this.previewImport(); };
      rd.readAsText(f, 'utf-8');
    },
    async previewImport() {
      const i = this.imp;
      const r = await this.call(() => this.api('/api/import/preview', { content: i.content, filename: i.fileName }));
      const ch = {};
      if (r.kind === 'bundle') r.items.forEach(x => { ch[x.section + '\t' + x.key] = x.status === 'new' ? 'import' : 'skip'; });
      i.preview = r; i.choices = ch;
    },
    impCount(st) { return (this.imp.preview?.items || []).filter(x => x.status === st).length; },
    setAllChoices(status, act) {
      (this.imp.preview?.items || []).forEach(x => {
        if (x.status !== status) return;
        this.imp.choices[x.section + '\t' + x.key] = act === 'merge' && x.section !== 'dicts' ? 'import' : act;
      });
    },
    async applyImport() {
      const i = this.imp;
      const r = await this.call(() => this.api('/api/import/apply', { content: i.content, filename: i.fileName, choices: i.choices, mode: i.mode, overwrite_names: i.overwriteNames, catalog: i.catalog }));
      this.toast(r.msg, 'ok'); i.preview = null; i.content = ''; i.fileName = ''; this.refresh();
    },

    // ---------------- 用例 ----------------
    openCase(name) {
      const c = name ? clone(this.st.cases[name]) : { expect: [] };
      this.caseEd = { name: name || '', old: name || '', desc: c.desc || '', addId: '', rows: this.caseRows(c), dirty: !name };
      if (this.page !== 'cases') this.go('cases');
    },
    addCaseRow() {
      const c = this.caseEd, id = this.pickId(c.addId); if (!id) return this.toast('没找到这个点位，输入完整的点位 ID 试试', 'err');
      if (c.rows.some(r => r.id === id)) { c.addId = ''; return this.toast('这个点位已经在用例里了，多个期望值可以直接多选', 'warn'); }
      c.rows.push({ key: Math.random(), id, min: 1, max: '', values: {}, showAll: false }); c.addId = ''; c.dirty = true;
    },
    removeCaseRow(ri) {
      const c = this.caseEd, removed = c.rows.splice(ri, 1)[0]; if (!removed) return;
      c.dirty = true;
      this.toast(`已从用例移除 ${this.eventName(removed.id) || removed.id}`, 'warn', {
        label: '撤销', fn: () => { if (this.caseEd) { this.caseEd.rows.splice(Math.min(ri, this.caseEd.rows.length), 0, removed); this.caseEd.dirty = true; } }
      });
    },
    eventLast(id) { return this.st?.events.find(e => e.id === String(id))?.last || null; },
    async saveCase() {
      const c = this.caseEd, name = c.name.trim();
      if (!name) return this.toast('先填用例名', 'err');
      const value = {}; if (c.desc) value.desc = c.desc; value.expect = this.rowsToExpect(c.rows);
      await this.call(() => this.api('/api/case/save', { name, old_name: c.old, value }), '用例已保存');
      if (this.live.caseName === c.old) this.live.caseName = name;
      c.old = name; c.dirty = false; await this.refresh();
    },
    useCaseInLive(name) { this.live.caseName = name; this.loadPlanFromCase(); this.go('live'); },

    // ---------------- 报告 ----------------
    async openReport(name) {
      this.rep.cur = await this.call(() => this.api('/api/report?name=' + encodeURIComponent(name)));
      this.rep.cur.name = name; this.go('reports');
    },
    repKeep(x, view) {
      if (view === 'bad') return ['FAIL', 'WARN', 'ASK'].includes(x.status);
      if (view === 'pass') return x.status === 'PASS';
      return this.rep.showUnk || x.status !== 'UNKNOWN';
    },
    repCount(view) { return (this.rep.cur?.results || []).filter(x => this.repKeep(x, view)).length; },
    get repResults() {
      const r = this.rep.cur; if (!r) return [];
      const q = this.rep.q.trim().toLowerCase();
      return r.results.filter(x => this.repKeep(x, this.rep.view) && (!q || JSON.stringify(x).toLowerCase().includes(q)));
    },

    // ---------------- 规则文件 ----------------
    async openYaml() { const d = await this.call(() => this.api('/api/spec_text')); this.yv = { files: d.files, cur: 0, edit: false, text: '' }; },
    yvFile() { return this.yv ? this.yv.files[this.yv.cur] : null; },
    async saveYaml() {
      const f = this.yvFile();
      await this.call(() => this.api('/api/spec_text/save', { name: f.name, text: this.yv.text }), '规则文件已保存');
      f.text = this.yv.text; this.yv.edit = false; await this.refresh();
      if (this.ed && !this.ed.dirty && !this.ed.isNew) { this.ed.kind === 'template' ? this.openTemplate(this.ed.id) : this.loadEditor(this.ed.id, this.ed.sample); }
    },
    async copyText(t) { try { await navigator.clipboard.writeText(t); this.toast('已复制', 'ok'); } catch (e) { this.toast('复制失败，请手动选择复制', 'err'); } },
    yamlHtml(t) {
      const esc = x => x.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      return String(t).split('\n').map(line => {
        let code = line, cm = '';
        const ci = line.search(/(^|\s)#/);
        if (ci >= 0 && (line.slice(0, ci).split("'").length % 2 === 1) && (line.slice(0, ci).split('"').length % 2 === 1)) { code = line.slice(0, ci); cm = line.slice(ci); }
        let h = esc(code).replace(/^(\s*-?\s*)([^\s:#'"{\[][^:#{}]*?)(:)(\s|$)/, '$1<span class="yk">$2</span>$3$4');
        h = h.replace(/([{,]\s*)([\w.\u4e00-\u9fa5]+)(:\s)/g, '$1<span class="yk2">$2</span>$3');
        return h + (cm ? `<span class="yc">${esc(cm)}</span>` : '');
      }).join('\n');
    },
    async checkLog() {
      const c = this.checkForm;
      if (!c.content && !c.path) return this.toast('先选择或上传一份日志', 'err');
      const r = await this.call(() => this.api('/api/check_log', { ...this.logBody(c), case: c.caseName }));
      this.toast(r.summary.ok ? '校验通过' : `校验完成：失败 ${r.summary.fail} 条`, r.summary.ok ? 'ok' : 'warn');
      await this.refresh(); this.openReport(r.report);
    },
  };
}
