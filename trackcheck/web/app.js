// 埋点小岛 —— 前端逻辑（Alpine.js）
const NAV = [
  { id: 'home', icon: '🏡', label: '小岛首页' },
  { id: 'live', icon: '🔍', label: '实时调试' },
  { id: 'infer', icon: '🌱', label: '规则草稿' },
  { id: 'rules', icon: '📒', label: '点位规则' },
  { id: 'dicts', icon: '📚', label: '字典' },
  { id: 'sources', icon: '🧺', label: '数据源' },
  { id: 'cases', icon: '🧪', label: '测试用例' },
  { id: 'reports', icon: '📊', label: '校验报告' },
  { id: 'share', icon: '🤝', label: '导入 / 分享' },
];
const CARD_TONES = [
  'bg-pond-50 border-pond-200', 'bg-leaf-50 border-leaf-200', 'bg-peach-50 border-peach-200',
  'bg-berry-50 border-berry-200', 'bg-lilac-50 border-lilac-200', 'bg-sun-50 border-sun-200',
];
const TIPS = [
  '一个点位只需要配置一次规则，之后每次测试都会自动复用哦 🌱',
  '在「实时调试」里点开某条事件，直接点字段旁的「设置规则」最省事～',
  '接口 JSON 里点一下对应的值，小岛会自动帮你生成数据定位，不用手写路径。',
  '每个点位的枚举都不一样？直接在字段规则里选「本点位专用」填就好，只跟着这个点位走。',
  '字典像标签一样挂在点位上：哪些点位用它，在「字典」页自己加；规则里引用了也会自动挂上。',
  '同事配好的规则，在「导入 / 分享」导出一个 .yaml 发给你，导入前会逐条对比差异。',
  '导入全部点位清单 JSON 后，「点位规则」里能看到哪些点位还没配置。',
  '曝光、点击这类共用字段的点位，可以用模板 extends 一次写好。',
  '结束调试时勾选「拉取手机数据」，对照数据就和这次操作完全一致啦。',
];
const STATUS_TEXT = { PASS: '通过', FAIL: '失败', WARN: '警告', SKIP: '跳过', INFO: '未定义', UNKNOWN: '无规则', MISS: '未触发' };
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

function app() {
  return {
    NAV, CARD_TONES, STATUS_TEXT, DRAFT_TEXT,
    page: 'home', st: null, busy: false, toasts: [], tipIdx: 0, devices: [], devErr: '', search: '',
    // 实时调试
    live: { serial: '', clear: true, pull: false, caseName: '', only: '', results: [], running: false, sel: -1, report: null, raw: '', filter: 'all' },
    // 规则草稿
    inf: { mode: 'session', path: '', content: '', fileName: '', drafts: [], dicts: {}, warns: [], log: '', open: 0 },
    // 点位规则
    ed: null, ruleFilter: '', ruleTab: 'all',
    // 导入 / 分享
    exp: { mode: 'all', ids: [], q: '', author: '', note: '', sources: true },
    imp: { fileName: '', content: '', preview: null, choices: {}, mode: 'merge', overwriteNames: true, catalog: true },
    // 字段编辑弹窗
    fm: null,
    // 字典 / 数据源 / 用例 / 报告
    dictEd: null, srcEd: null, caseEd: null, settingsEd: null, pullLog: '',
    rep: { list: [], cur: null, onlyBad: true, q: '' },
    checkForm: { path: '', content: '', fileName: '', caseName: '' },

    async init() {
      await this.refresh();
      this.loadDevices();
      setInterval(() => this.pollLive(), 1000);
      setInterval(() => { this.tipIdx = (this.tipIdx + 1) % TIPS.length; }, 9000);
      const h = location.hash.slice(1); if (NAV.some(n => n.id === h)) this.page = h;
      window.addEventListener('hashchange', () => { const x = location.hash.slice(1); if (NAV.some(n => n.id === x)) this.page = x; });
    },
    go(p) { this.page = p; location.hash = p; window.scrollTo({ top: 0, behavior: 'smooth' }); },
    get tip() { return TIPS[this.tipIdx % TIPS.length]; },

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
    toast(msg, kind = 'ok') {
      const id = Math.random(); this.toasts.push({ id, msg, kind });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, kind === 'err' ? 6000 : 3000);
    },
    async refresh() {
      try { this.st = await this.api('/api/state'); } catch (e) { this.toast(e.message, 'err'); }
      if (this.st?.live) { this.live.running = this.st.live.running; this.live.report = this.st.live.report; }
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
    get allEventOptions() {
      const s = this.st; if (!s) return [];
      const seen = new Set(), out = [];
      s.events.forEach(e => { seen.add(e.id); out.push({ id: e.id, name: e.name }); });
      s.catalog.forEach(c => { if (!seen.has(c.id)) out.push({ id: c.id, name: c.name }); });
      return out;
    },
    get onlineDevice() { return this.devices.find(d => d.state === 'device'); },
    get stats() {
      const s = this.st; if (!s) return {};
      const last = s.reports[0];
      const evs = s.events, ok = evs.filter(e => e.last?.status === 'PASS').length, bad = evs.filter(e => e.last && e.last.status !== 'PASS').length;
      return {
        events: evs.length, dicts: Object.keys(s.dicts).length, sources: Object.keys(s.sources).length,
        rate: last && last.summary.total ? Math.round(last.summary.pass * 100 / last.summary.total) : null, ok, bad,
        untested: evs.filter(e => !e.last).length,
        catalog: s.catalog.length, unconfigured: s.catalog.filter(c => !c.configured).length,
      };
    },
    get goals() {
      const s = this.st; if (!s) return [];
      const last = s.reports[0];
      return [
        { t: '跑一次实时调试', done: !!s.reports.length },
        { t: '所有点位都有规则', done: s.events.length > 0 && s.events.every(e => e.nfields > 0) && !s.catalog.some(c => !c.configured) },
        { t: '最近一次 0 失败', done: !!last && last.summary.fail === 0 },
      ];
    },
    get notices() {
      const s = this.st; if (!s || !s.reports.length) return [];
      const r = s.reports[0];
      return r.hot.map(h => ({ ...h, report: r.name }));
    },
    fmtRule(r) {
      r = r || {}; const out = [];
      if (r.ignore) return [{ t: '不校验', c: 'bg-wood-100 text-ink-soft' }];
      if (r.from) out.push({ t: '🌐 ' + r.from, c: 'bg-pond-50 text-pond-700' });
      if (r.map) out.push({ t: '🔁 ' + (typeof r.map === 'object' && !Array.isArray(r.map) ? Object.entries(r.map).map(([k, v]) => k + '→' + v).join(' / ') : [].concat(r.map).join(' → ')), c: 'bg-lilac-50 text-lilac-600' });
      if (r.sql) out.push({ t: '🗃️ ' + (r.db ? r.db + ': ' : '') + r.sql, c: 'bg-peach-50 text-peach-600' });
      if (r.enum) out.push({ t: '📖 ' + (Array.isArray(r.enum) ? r.enum.join(' / ') : typeof r.enum === 'object' ? Object.entries(r.enum).map(([k, v]) => k + '=' + v).join(' / ') : '字典 ' + r.enum), c: 'bg-sun-50 text-sun-700' });
      if (r.eq !== undefined) out.push({ t: '📌 = ' + r.eq, c: 'bg-leaf-50 text-leaf-700' });
      if (r.expr) out.push({ t: '🧮 ' + r.expr, c: 'bg-leaf-50 text-leaf-700' });
      if (r.type) out.push({ t: '类型 ' + r.type, c: 'bg-wood-50 text-wood-600' });
      if (r.regex) out.push({ t: '正则 ' + r.regex, c: 'bg-wood-50 text-wood-600' });
      if (r.range) out.push({ t: '范围 ' + r.range.join('~'), c: 'bg-wood-50 text-wood-600' });
      if (r.not_empty) out.push({ t: '不能为空', c: 'bg-wood-50 text-wood-600' });
      if (r.required === false) out.push({ t: '可不上报', c: 'bg-wood-50 text-wood-600' });
      if (r.when) out.push({ t: '当 ' + r.when, c: 'bg-wood-50 text-wood-600' });
      if (r.assert) out.push({ t: '断言 ' + r.assert, c: 'bg-wood-50 text-wood-600' });
      if (!out.length) out.push({ t: '还没设置', c: 'bg-berry-50 text-berry-600' });
      return out;
    },

    // ---------------- 实时调试 ----------------
    async startLive() {
      await this.call(async () => {
        await this.api('/api/live/start', { serial: this.live.serial, clear: this.live.clear, case: this.live.caseName, only: splitList(this.live.only) });
        Object.assign(this.live, { results: [], running: true, sel: -1, report: null });
      }, '开始抓取 logcat，去手机上操作吧 ✨');
      this.refresh();
    },
    async stopLive() {
      const r = await this.call(() => this.api('/api/live/stop', { pull: this.live.pull }));
      this.live.running = false; this.live.report = r.report;
      if (r.pull_log) this.pullLog = r.pull_log;
      this.live.results = []; await this.pollLive(true);
      this.toast(r.summary.ok ? '本轮全部通过 🎉 报告已生成' : `报告已生成：失败 ${r.summary.fail} 条`, r.summary.ok ? 'ok' : 'warn');
      this.refresh();
    },
    async pollLive(force) {
      if (!force && !this.live.running) return;
      try {
        const d = await this.api('/api/live/poll?since=' + this.live.results.length);
        if (d.since === this.live.results.length) this.live.results.push(...d.results);
        this.live.running = d.running; this.live.raw = d.raw || this.live.raw;
        if (this.live.sel < 0 && this.live.results.length) this.live.sel = this.live.results.length - 1;
      } catch (e) { /* 忽略一次轮询错误 */ }
    },
    async recheckLive() {
      await this.api('/api/live/recheck', {});
      const sel = this.live.sel; this.live.results = []; await this.pollLive(true); this.live.sel = sel;
    },
    get liveList() {
      const f = this.live.filter;
      return this.live.results.map((r, i) => ({ r, i })).filter(({ r }) => f === 'all' || (f === 'bad' ? (r.status === 'FAIL' || r.status === 'WARN') : r.status === 'UNKNOWN')).reverse();
    },
    get liveCur() { return this.live.results[this.live.sel] || null; },
    get liveCounts() {
      const c = { PASS: 0, FAIL: 0, WARN: 0, UNKNOWN: 0 }; this.live.results.forEach(r => { c[r.status] = (c[r.status] || 0) + 1; }); return c;
    },
    badCount(r) { return r.fields.filter(f => f.status === 'FAIL' || f.status === 'WARN').length + r.errors.length; },
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
    async openEventFromResult(r) {
      await this.loadEditor(r.id, { fields: r.data, cls: r.cls, line: r.raw }); this.go('rules');
    },

    // ---------------- 字段编辑弹窗 ----------------
    openField({ field, rule, sample, eventId, raw, target }) {
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
      this.fm = {
        field, eventId, raw: clone(raw) || {}, target, sample: sample || {}, tab,
        desc: rule.desc || '', from: rule.from || '', mapPick: '',
        mapMode: !rule.map ? 'none' : mapInline ? 'inline' : 'shared',
        map: mapInline ? [] : [].concat(rule.map || []).filter(x => typeof x === 'string'),
        mapRows: mapInline ? toRows(rule.map) : [], mapHint: null, mapBulk: '',
        sql: rule.sql || '', db: rule.db || dbs[0] || '', dbs, schema: null,
        enumMode: enumIsDict ? 'dict' : 'inline',
        enumDict: enumIsDict ? rule.enum : (this.dictsFor(eventId).mine[0] || ''),
        enumRows: enumInline ? toRows(rule.enum) : Array.isArray(rule.enum) ? rule.enum.map(x => ({ k: String(x), v: '' })) : [], enumBulk: '',
        enumList: Array.isArray(rule.enum) ? rule.enum.join(', ') : '',
        allowEmpty: !!rule.allow_empty, eq: rule.eq ?? '', expr: rule.expr || '',
        type: rule.type || '', regex: rule.regex || '', rmin: rule.range ? rule.range[0] : '', rmax: rule.range ? rule.range[1] : '',
        notEmpty: !!rule.not_empty, optional: rule.required === false, when: rule.when || '', warn: rule.severity === 'warn',
        assert: rule.assert || '', msg: rule.msg || '',
        jsonSrc: jsonSrcs[0] || '', jsonSrcs, jsonRows: [], jsonQ: '', jsonOnlyHit: true, jsonErr: '',
        newCtx: {}, note: '', sqlRes: null, tryRes: null, showMore: !!(rule.type || rule.regex || rule.range || rule.when || rule.not_empty || rule.assert),
      };
      if (tab === 'api' || tab === 'none') this.loadJson();
      if (tab === 'sql') this.loadSchema();
    },
    get fmValue() { return this.fm ? norm(this.fm.sample[this.fm.field]) : ''; },
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
        else if (m.enumMode === 'list') r.enum = splitList(m.enumList);
        else { const o = this.rowsObj(m.enumRows); if (Object.keys(o).length) r.enum = o; }
        if (m.allowEmpty) r.allow_empty = true;
      }
      if (m.tab === 'eq') r.eq = m.eq;
      if (m.tab === 'expr' && m.expr) r.expr = m.expr;
      if (m.tab === 'ignore') r.ignore = true;
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
      const m = this.fm; if (!m || !Object.keys(m.sample).length) return;
      const raw = this.fmRawWithCtx(); raw.fields = { ...(raw.fields || {}), [m.field]: this.buildRule() };
      try {
        const res = await this.api('/api/try', { id: m.eventId, fields: m.sample, raw });
        m.tryRes = res.fields.find(f => f.field === m.field) || null;
      } catch (e) { m.tryRes = { status: 'FAIL', reason: e.message }; }
    },
    async saveField() {
      const m = this.fm, rule = this.buildRule(), t = m.target;
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
        await this.call(() => this.api('/api/event/save', { id: m.eventId, raw }), `已保存 ${m.field} 的规则，以后自动复用 🌱`);
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
    draftDicts(d) { return {}; },
    async acceptDraft(d) {
      await this.call(() => this.api('/api/infer/accept', {
        id: d.id, name: d.name, context: d.context, merge: d.merge,
        fields: d.fields.map(f => ({ field: f.field, rule: f.rule })), dicts: this.draftDicts(d),
      }), `「${d.name}」已保存到规则文件，以后自动复用 🌱`);
      d.saved = true; d.exists = true; this.refresh();
    },

    // ---------------- 点位规则编辑器 ----------------
    get ruleList() {
      const q = (this.ruleFilter || this.search).trim().toLowerCase();
      const s = this.st; if (!s) return [];
      const conf = new Set(s.events.map(e => e.id));
      let list = [];
      if (this.ruleTab !== 'todo') list = list.concat(s.events);
      if (this.ruleTab !== 'done') list = list.concat(s.catalog.filter(c => !conf.has(c.id)).map(c => ({ id: c.id, name: c.name, nfields: 0, unconfigured: true })));
      return list.filter(e => !q || e.id.includes(q) || (e.name || '').toLowerCase().includes(q)).slice(0, 500);
    },
    openRuleItem(e) {
      if (e.unconfigured) this.openEditor({ id: e.id, isNew: true, raw: { name: e.name, fields: {} } });
      else this.loadEditor(e.id);
    },
    openEditor({ id, isNew, raw, resolved, file, sample }) {
      raw = clone(raw) || {};
      this.ed = {
        id, oldId: isNew ? '' : id, isNew, raw, resolved: resolved || {}, file: file || '',
        sampleText: sample?.line || '', sample: sample || null, tryRes: null, dirty: !!isNew, tab: 'fields',
        dedupe: (raw.dedupe ? [].concat(raw.dedupe) : []).join(', '),
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
    addEdField() { const n = prompt('字段名（上报 key）'); if (!n) return; this.ed.raw.fields = { ...(this.ed.raw.fields || {}), [n.trim()]: {} }; this.ed.dirty = true; },
    edRaw() {
      const e = this.ed, raw = clone(e.raw);
      const dd = splitList(e.dedupe); if (dd.length) raw.dedupe = dd; else delete raw.dedupe;
      if (raw.extends === '') delete raw.extends;
      return raw;
    },
    async parseSample() {
      const e = this.ed;
      const r = await this.call(() => this.api('/api/parse', { text: e.sampleText, id: e.id }));
      if (!r.events.length) return this.toast('没解析出埋点，检查格式（需要包含 Event: id: ... data: ...）', 'err');
      const ev = r.events.find(x => !e.id || x.id === e.id) || r.events[0];
      if (!e.id) e.id = ev.id;
      if (!e.raw.name) e.raw.name = ev.cls || ev.id;
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
      if (!e.id) return this.toast('先填点位ID', 'err');
      await this.call(() => this.api('/api/event/save', { id: e.id, old_id: e.oldId, raw: this.edRaw() }), '保存成功，以后测试这个点位自动复用 🌱');
      e.isNew = false; e.oldId = e.id; e.dirty = false;
      await this.refresh(); const ev = await this.api('/api/event?id=' + e.id); e.resolved = ev.resolved; e.file = ev.file;
      if (this.live.results.length) this.recheckLive();
    },
    async deleteEditor() {
      const e = this.ed; if (!confirm(`确定删除点位 ${e.id} 的规则？`)) return;
      await this.call(() => this.api('/api/event/delete', { id: e.id }), '已删除');
      this.ed = null; this.refresh();
    },

    // ---------------- 字典（像标签一样挂在点位上） ----------------
    dictFilter: '',
    get dictList() {
      const s = this.st; if (!s) return [];
      const q = this.dictFilter.trim().toLowerCase();
      return Object.entries(s.dicts).map(([name, items]) => {
        const m = s.dict_meta[name] || { events: [], refs: [], desc: '' };
        const untagged = (m.refs || []).filter(x => !m.events.includes(x));
        return { name, items, desc: m.desc, events: m.events, refs: m.refs || [], untagged };
      }).filter(d => !q || d.name.toLowerCase().includes(q) || (d.desc || '').toLowerCase().includes(q)
        || d.events.concat(d.refs).some(id => id.includes(q) || this.eventName(id).toLowerCase().includes(q)));
    },
    dictsFor(eid) {
      const s = this.st; eid = String(eid || '');
      const mine = [], other = [];
      Object.keys(s.dicts).forEach(n => ((s.dict_meta[n]?.events || []).includes(eid) ? mine : other).push(n));
      return { mine, other };
    },
    dictTagged(name, eid) { return (this.st.dict_meta[name]?.events || []).includes(String(eid)); },
    async tagDict(name, eid, op = 'add') {
      eid = String(eid || '').trim().split(/\s/)[0]; if (!eid) return;
      await this.call(() => this.api('/api/dict/tag', { name, event: eid, op }));
      this.refresh();
    },
    openDict(name) {
      const s = this.st;
      const items = name ? Object.entries(s.dicts[name] || {}).map(([k, v]) => ({ k: String(k), v: String(v) })) : [{ k: '', v: '' }];
      const m = name ? s.dict_meta[name] || {} : {};
      this.dictEd = { name: name || '', old: name || '', items, bulk: '', desc: m.desc || '', events: [...(m.events || [])], addEv: '' };
    },
    importBulk() {
      const d = this.dictEd; this.importRows(d.bulk, d.items);
      d.items = d.items.filter(x => x.k !== '' || x.v !== ''); d.bulk = '';
    },
    addDictEv() {
      const d = this.dictEd; const id = String(d.addEv || '').trim().split(/\s/)[0];
      if (id && !d.events.includes(id)) d.events.push(id); d.addEv = '';
    },
    async saveDict() {
      const d = this.dictEd;
      await this.call(() => this.api('/api/dict/save', { name: d.name, old_name: d.old, items: this.rowsObj(d.items), events: d.events, desc: d.desc }), '字典已保存');
      this.dictEd = null; this.refresh();
    },
    async saveRowsAsDict(kind) {
      const m = this.fm, rows = kind === 'enum' ? m.enumRows : m.mapRows;
      const items = this.rowsObj(rows); if (!Object.keys(items).length) return this.toast('先填几行', 'err');
      const name = prompt('给这个字典起个名字（以后其它点位也能挂上它）', m.field + (kind === 'map' ? '_map' : ''));
      if (!name) return;
      if (this.st.dicts[name] && !confirm(`字典 ${name} 已存在，覆盖它的内容吗？`)) return;
      const evs = [...new Set([...(this.st.dict_meta[name]?.events || []), String(m.eventId)])];
      await this.call(() => this.api('/api/dict/save', { name, old_name: name, items, events: evs, desc: this.st.dict_meta[name]?.desc || '' }), `已存为字典 ${name}，并关联本点位`);
      await this.refresh();
      if (kind === 'enum') { m.enumMode = 'dict'; m.enumDict = name; } else { m.mapMode = 'shared'; m.map = [name]; }
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
    async delItem(section, name) {
      if (!confirm(`确定删除 ${name}？`)) return;
      await this.call(() => this.api('/api/item/delete', { section, name }), '已删除'); this.dictEd = this.srcEd = this.caseEd = null; this.refresh();
    },

    // ---------------- 数据源 / 设置 ----------------
    openSrc(name) {
      const s = name ? clone(this.st.sources[name]) : { type: 'json_file', path: '' };
      this.srcEd = { name: name || '', old: name || '', type: s.type || 'json_file', path: s.path || '', db: s.db || '', sql: s.sql || '', root: s.root || '' };
    },
    async saveSrc() {
      const s = this.srcEd, v = { type: s.type };
      if (s.type !== 'sqlite_json') v.path = s.path; else { v.db = s.db; v.sql = s.sql; }
      if (s.root) v.root = s.root;
      await this.call(() => this.api('/api/source/save', { name: s.name, old_name: s.old, value: v }), '数据源已保存');
      this.srcEd = null; this.refresh();
    },
    openSettings() {
      const s = this.st;
      this.settingsEd = {
        pattern: s.log.pattern || '', logcat_args: (s.log.logcat_args || []).join(' '),
        null_equals_empty: s.options.null_equals_empty !== false, strict_fields: !!s.options.strict_fields,
        package: s.device.package || '', serial: s.device.serial || '',
        pull: clone(s.device.pull || []).map(p => ({ remote: p.remote || '', local: p.local || '' })),
      };
    },
    async saveSettings() {
      const e = this.settingsEd;
      await this.call(() => this.api('/api/settings/save', {
        log: { pattern: e.pattern, logcat_args: e.logcat_args.split(/\s+/).filter(Boolean) },
        options: { null_equals_empty: e.null_equals_empty, strict_fields: e.strict_fields },
        device: { package: e.package, ...(e.serial ? { serial: e.serial } : {}), pull: e.pull.filter(p => p.remote) },
      }), '设置已保存');
      this.refresh();
    },
    async doPull() {
      const r = await this.call(() => this.api('/api/pull', { serial: this.live.serial }));
      this.pullLog = r.log || '（没有输出）'; this.refresh();
    },

    // ---------------- 用例 ----------------
    openCase(name) {
      const c = name ? clone(this.st.cases[name]) : { expect: [] };
      this.caseEd = {
        name: name || '', old: name || '',
        rows: (c.expect || []).map(x => typeof x === 'object' ? { id: String(x.id), min: x.min ?? 1, max: x.max ?? '', where: whereText(x.where) } : { id: String(x), min: 1, max: '', where: '' }),
      };
    },
    async saveCase() {
      const c = this.caseEd;
      const expect = c.rows.filter(r => r.id).map(r => {
        const o = { id: r.id }; if (Number(r.min) !== 1) o.min = Number(r.min); if (r.max !== '' && r.max !== null) o.max = Number(r.max);
        const w = parseWhere(r.where); if (Object.keys(w).length) o.where = w;
        return Object.keys(o).length === 1 ? r.id : o;
      });
      await this.call(() => this.api('/api/case/save', { name: c.name, old_name: c.old, value: { expect } }), '用例已保存');
      this.caseEd = null; this.refresh();
    },

    // ---------------- 报告 ----------------
    async openReport(name) {
      this.rep.cur = await this.call(() => this.api('/api/report?name=' + encodeURIComponent(name)));
      this.rep.cur.name = name; this.go('reports');
    },
    get repResults() {
      const r = this.rep.cur; if (!r) return [];
      const q = this.rep.q.trim().toLowerCase();
      return r.results.filter(x => (!this.rep.onlyBad || x.status === 'FAIL' || x.status === 'WARN') && (!q || JSON.stringify(x).toLowerCase().includes(q)));
    },
    async checkLog() {
      const c = this.checkForm;
      if (!c.content && !c.path) return this.toast('先选择或上传一份日志', 'err');
      const r = await this.call(() => this.api('/api/check_log', { ...this.logBody(c), case: c.caseName }));
      this.toast(r.summary.ok ? '校验通过 🎉' : `校验完成：失败 ${r.summary.fail} 条`, r.summary.ok ? 'ok' : 'warn');
      await this.refresh(); this.openReport(r.report);
    },
  };
}
