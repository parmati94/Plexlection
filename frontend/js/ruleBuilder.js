/**
 * Rule builder mixin.
 *
 * Alpine has no primitive for recursive components, and x-html recursion is a
 * dead end. So the tree is **flattened** into a depth-first array of rows and
 * rendered by one x-for, with indentation driven by `depth`. Mutations address
 * nodes by a `_id` assigned on load; a Map rebuilt on each flatten resolves an
 * id to its node and parent.
 *
 * `_id` is not stripped before saving — the server's validator rebuilds every
 * node from known fields only, so unknown keys are dropped for us.
 *
 * Nothing here is a getter. `ruleFlatNodes` lives in app.js, because object
 * spread copies a getter's value rather than the getter itself.
 */
import { api } from './api.js';

let _idSeq = 0;
let _nodeIndex = new Map(); // _id -> { node, parent, container }
let _previewTimer = null;

const PREVIEW_DEBOUNCE_MS = 300;

export const EMPTY_TREE = () => ({
  version: 1,
  root: { type: 'and', children: [], _id: `n${(_idSeq += 1)}` },
});

// Library-relative comparisons (agg_cmp nodes). The backend has supported
// these since the compiler was written; this is their UI. Operators and
// aggregates mirror AGG_OPERATORS / AGGREGATES in backend/rules/operators.py.
const AGG_OPS = [
  { op: 'gte', label: '≥' },
  { op: 'gt', label: '>' },
  { op: 'lte', label: '≤' },
  { op: 'lt', label: '<' },
];
const AGGREGATES = [
  { agg: 'percentile', label: 'library percentile' },
  { agg: 'median', label: 'library median' },
  { agg: 'mean', label: 'library average' },
  { agg: 'min', label: 'library minimum' },
  { agg: 'max', label: 'library maximum' },
];

function assignIds(node) {
  if (!node || typeof node !== 'object') return node;
  if (!node._id) node._id = `n${(_idSeq += 1)}`;
  (node.children ?? []).forEach(assignIds);
  if (node.child) assignIds(node.child);
  return node;
}

export function ruleBuilderMixin() {
  return {
    // ── loading ─────────────────────────────────────────────────────────
    async loadRules() {
      try {
        const res = await api.rules.list();
        this.rules = res.rules;
      } catch (e) {
        this.error = e.message;
      }
    },

    newRule() {
      this.editingRule = {
        id: null,
        name: '',
        description: '',
        rule: EMPTY_TREE(),
        library_keys: [...(this.settings?.plex?.libraries ?? [])],
        item_types: ['movie'],
        order_by_key: null,
        order_dir: 'desc',
        limit_n: null,
        enabled: true,
        sync_mode: 'label',
      };
      this.preview = null;
      this.runPreview();
    },

    // ── guided creation ─────────────────────────────────────────────────
    openNewRule() {
      this.newRuleDialog = { show: true, name: '', type: 'movie' };
    },

    createRuleFromDialog() {
      const { name, type } = this.newRuleDialog;
      if (!name.trim()) return;
      this.newRule();
      this.editingRule.name = name.trim();
      this.editingRule.item_types = [type];
      this.newRuleDialog.show = false;
      this.runPreview();
    },

    async editRule(id) {
      try {
        const res = await api.rules.get(id);
        const rule = res.rule;
        assignIds(rule.rule.root);
        this.editingRule = {
          id: rule.id,
          name: rule.name,
          description: rule.description ?? '',
          rule: rule.rule,
          library_keys: rule.library_keys ?? [],
          item_types: rule.item_types ?? ['movie'],
          order_by_key: rule.order_by_key,
          order_dir: rule.order_dir ?? 'desc',
          limit_n: rule.limit_n,
          enabled: rule.enabled,
          sync_mode: rule.sync_mode ?? 'label',
        };
        this.runPreview();
      } catch (e) {
        this.showToast(e.message, false);
      }
    },

    closeEditor() {
      this.editingRule = null;
      this.preview = null;
      this.previewError = null;
    },

    // ── flattening ──────────────────────────────────────────────────────
    /** Depth-first rows for the template, rebuilding the id index as it goes. */
    flattenTree() {
      _nodeIndex = new Map();
      const rows = [];
      const root = this.editingRule?.rule?.root;
      if (!root) return rows;

      // parentType/index travel with each row so the template can draw the
      // joining AND/OR *between* siblings. Without that the conjunction is
      // invisible unless you notice the group's toggle, and every rule reads
      // like an AND.
      const walk = (node, parent, container, depth, isLast, index) => {
        if (!node._id) node._id = `n${(_idSeq += 1)}`;
        _nodeIndex.set(node._id, { node, parent, container });
        rows.push({
          id: node._id,
          type: node.type,
          depth,
          isLast,
          index,
          parentId: parent?._id ?? null,
          parentType: parent && (parent.type === 'and' || parent.type === 'or')
            ? parent.type
            : null,
          key: node.key ?? null,
          op: node.op ?? null,
          node,
        });
        if (node.type === 'and' || node.type === 'or') {
          const kids = node.children ?? [];
          kids.forEach((c, i) => walk(c, node, kids, depth + 1, i === kids.length - 1, i));
        } else if (node.type === 'not' && node.child) {
          walk(node.child, node, null, depth + 1, true, 0);
        }
      };

      walk(root, null, null, 0, true, 0);
      return rows;
    },

    nodeById(id) {
      return _nodeIndex.get(id) ?? null;
    },

    // ── mutation ────────────────────────────────────────────────────────
    /** First numeric fact, so a new condition is immediately meaningful. */
    _defaultLeaf() {
      const spec =
        this.registry.find((f) => f.key === 'video.dar') ??
        this.registry.find((f) => f.type === 'number') ??
        this.registry[0];
      const op = spec?.operators?.[0]?.op ?? 'eq';
      return { type: 'cmp', key: spec?.key ?? '', op, value: null, _id: `n${(_idSeq += 1)}` };
    },

    addCondition(parentId) {
      const entry = this.nodeById(parentId);
      if (!entry) return;
      const target = entry.node;
      if (target.type === 'and' || target.type === 'or') {
        target.children = [...(target.children ?? []), this._defaultLeaf()];
        this.touchRule();
      }
    },

    addGroup(parentId) {
      const entry = this.nodeById(parentId);
      if (!entry) return;
      const target = entry.node;
      if (target.type === 'and' || target.type === 'or') {
        target.children = [
          ...(target.children ?? []),
          { type: 'or', children: [this._defaultLeaf()], _id: `n${(_idSeq += 1)}` },
        ];
        this.touchRule();
      }
    },

    removeNode(id) {
      const entry = this.nodeById(id);
      if (!entry || !entry.parent) return; // never remove the root
      const { node, parent } = entry;
      if (parent.type === 'not') {
        // Removing a NOT's only child removes the NOT with it.
        this.removeNode(parent._id);
        return;
      }
      parent.children = (parent.children ?? []).filter((c) => c !== node);
      this.touchRule();
    },

    setGroupOp(id, op) {
      const entry = this.nodeById(id);
      if (!entry) return;
      entry.node.type = op;
      this.touchRule();
    },

    /** Flip a group between AND and OR — bound to the joiner chips so the
     *  conjunction is editable exactly where it's displayed. */
    toggleGroupOp(id) {
      const entry = this.nodeById(id);
      if (!entry) return;
      entry.node.type = entry.node.type === 'and' ? 'or' : 'and';
      this.touchRule();
    },

    wrapInNot(id) {
      const entry = this.nodeById(id);
      if (!entry || !entry.parent) return;
      const { node, parent } = entry;
      const wrapper = { type: 'not', child: node, _id: `n${(_idSeq += 1)}` };
      parent.children = (parent.children ?? []).map((c) => (c === node ? wrapper : c));
      this.touchRule();
    },

    unwrapNot(id) {
      const entry = this.nodeById(id);
      if (!entry || entry.node.type !== 'not') return;
      const { node, parent } = entry;
      if (!parent) return;
      parent.children = (parent.children ?? []).map((c) => (c === node ? node.child : c));
      this.touchRule();
    },

    /** Reset op and value when the fact changes — the old ones rarely apply. */
    onFactChange(id) {
      const entry = this.nodeById(id);
      if (!entry) return;
      const spec = this.specFor(entry.node.key);
      // A library-relative comparison only holds for aggregatable facts; a
      // fact change that leaves that world reverts the node to a plain cmp.
      if (entry.node.type === 'agg_cmp' && !spec?.aggregatable) {
        entry.node.type = 'cmp';
        delete entry.node.agg;
        delete entry.node.agg_arg;
      }
      entry.node.op = entry.node.type === 'agg_cmp'
        ? 'gte'
        : (spec?.operators?.[0]?.op ?? 'eq');
      entry.node.value = spec?.type === 'bool' ? null : '';
      this.touchRule();
    },

    onOpChange(id) {
      const entry = this.nodeById(id);
      if (!entry) return;
      const kind = this.valueKind(entry.node);
      if (kind === null) entry.node.value = null;
      if (this.opArity(entry.node) === 2 && !Array.isArray(entry.node.value)) {
        entry.node.value = ['', ''];
      }
      if (this.opArity(entry.node) === 'n' && !Array.isArray(entry.node.value)) {
        entry.node.value = [];
      }
      this.touchRule();
    },

    // ── units ───────────────────────────────────────────────────────────
    /**
     * How a fact's value is entered versus how it's stored.
     *
     * Storage stays canonical — seconds, bytes, kbps, dollars — because that's
     * what the providers emit and what the SQL compares. Only the input scales,
     * so nobody has to type 7200 to mean two hours or 42000000000 to mean 42 GB.
     *
     * `stored = entered * scale`.
     */
    unitFor(key) {
      const spec = this.specFor(key);
      if (!spec) return null;
      const by = {
        duration_s: { suffix: 'min', scale: 60, step: 1, stored: 'seconds' },
        bytes: { suffix: 'GB', scale: 1e9, step: 0.1, stored: 'bytes' },
        kbps: { suffix: 'Mbps', scale: 1000, step: 0.1, stored: 'kbps' },
        percent: { suffix: '%', scale: 0.01, step: 1, stored: 'a 0–1 fraction' },
        ratio: { suffix: ':1', scale: 1, step: 0.01, stored: null },
      };
      if (spec.format && by[spec.format]) return by[spec.format];
      if (spec.unit === 'USD') return { suffix: '$M', scale: 1e6, step: 0.1, stored: 'dollars' };
      if (spec.unit) return { suffix: spec.unit, scale: 1, step: spec.unit === 'fps' ? 0.001 : 1, stored: null };
      return null;
    },

    /** Canonical stored value → what the user sees in the box. */
    toDisplay(key, stored) {
      if (stored === '' || stored === null || stored === undefined) return '';
      const u = this.unitFor(key);
      const n = Number(stored);
      if (!Number.isFinite(n)) return stored;
      if (!u || u.scale === 1) return n;
      return Math.round((n / u.scale) * 10000) / 10000;
    },

    /** What the user typed → the canonical value we store and compare. */
    fromDisplay(key, entered) {
      if (entered === '' || entered === null || entered === undefined) return null;
      const u = this.unitFor(key);
      const n = Number(entered);
      if (!Number.isFinite(n)) return entered;
      if (!u || u.scale === 1) return n;
      // Round to kill float dust: 42 GB must store as 42000000000, not …0000001.
      return Math.round(n * u.scale * 1000) / 1000;
    },

    /** Bound to the single-value number input. */
    numberIn(node) {
      return this.toDisplay(node.key, node.value);
    },
    setNumberIn(node, entered) {
      node.value = this.fromDisplay(node.key, entered);
      this.touchRule();
    },

    /** Bound to each half of a `between` range. */
    rangeIn(node, i) {
      return this.toDisplay(node.key, this.listValue(node)[i]);
    },
    setRangeIn(node, i, entered) {
      const pair = [this.listValue(node)[0] ?? '', this.listValue(node)[1] ?? ''];
      pair[i] = this.fromDisplay(node.key, entered);
      node.value = pair;
      this.touchRule();
    },

    /** Tooltip telling power users what actually goes into the rule. */
    unitHint(key) {
      const u = this.unitFor(key);
      if (!u) return '';
      return u.stored ? `Entered in ${u.suffix}, stored as ${u.stored}` : `Measured in ${u.suffix}`;
    },

    // ── registry lookups ────────────────────────────────────────────────
    specFor(key) {
      return this.registry.find((f) => f.key === key) ?? null;
    },

    operatorsFor(key) {
      return this.specFor(key)?.operators ?? [];
    },

    opMeta(node) {
      return this.operatorsFor(node.key).find((o) => o.op === node.op) ?? null;
    },

    opArity(node) {
      if (node.type === 'agg_cmp') return 0; // the aggregate IS the operand
      return this.opMeta(node)?.arity ?? 1;
    },

    valueKind(node) {
      if (node.type === 'agg_cmp') return null;
      const meta = this.opMeta(node);
      if (!meta || meta.arity === 0) return null;
      return meta.value_kind;
    },

    // ── library-relative comparisons ────────────────────────────────────
    aggOps() {
      return AGG_OPS;
    },
    aggregates() {
      return AGGREGATES;
    },
    isAggable(key) {
      return !!this.specFor(key)?.aggregatable;
    },
    setCompareMode(id, mode) {
      const entry = this.nodeById(id);
      if (!entry) return;
      const node = entry.node;
      if (mode === 'value') {
        node.type = 'cmp';
        delete node.agg;
        delete node.agg_arg;
        node.op = this.operatorsFor(node.key)[0]?.op ?? 'gte';
        node.value = '';
      } else {
        node.type = 'agg_cmp';
        if (!AGG_OPS.some((o) => o.op === node.op)) node.op = 'gte';
        node.agg = mode;
        if (mode === 'percentile') node.agg_arg = node.agg_arg ?? 90;
        else delete node.agg_arg;
        delete node.value;
      }
      this.touchRule();
    },
    /** "top 10%" for ≥ 90th percentile — the reading people actually want. */
    aggHint(node) {
      if (node.type !== 'agg_cmp' || node.agg !== 'percentile') return '';
      const pct = Number(node.agg_arg);
      if (!Number.isFinite(pct)) return '';
      if (node.op === 'gte' || node.op === 'gt') return `= top ${Math.round((100 - pct) * 10) / 10}%`;
      return `= bottom ${Math.round(pct * 10) / 10}%`;
    },

    coverageNote(key) {
      const spec = this.specFor(key);
      if (!spec?.coverage?.total) return '';
      const { known, total } = spec.coverage;
      if (known >= total) return '';
      return `known for ${known} of ${total}`;
    },

    /**
     * Registry grouped for the fact <select>, with the unit in the label.
     *
     * Three facts are called some variant of "runtime" and two of them are in
     * minutes while the third is in seconds — the label has to disambiguate
     * before you pick, not after.
     */
    factOptionGroups() {
      // Only facts that can exist on what this rule targets. A show has no
      // file, so offering "Aspect ratio" on a show rule is offering a condition
      // that can never match.
      const targets = this.editingRule?.item_types ?? ['movie'];
      const groups = {};
      for (const f of this.registry) {
        const scope = f.applies_to ?? ['movie'];
        if (!targets.some((t) => scope.includes(t))) continue;
        const u = this.unitFor(f.key);
        const label = u && u.suffix !== ':1' ? `${f.label} (${u.suffix})` : f.label;
        (groups[f.group] ??= []).push({ ...f, optionLabel: label });
      }
      return Object.entries(groups).map(([group, facts]) => ({ group, facts }));
    },

    /**
     * Switch a rule between movies and shows.
     *
     * The fact set changes with the target, so any condition referencing a fact
     * that no longer applies is dropped rather than left to fail validation on
     * the next preview.
     */
    setRuleTarget(type) {
      if (!this.editingRule) return;
      this.editingRule.item_types = [type];

      const valid = new Set(this.factOptionGroups().flatMap((g) => g.facts.map((f) => f.key)));
      let dropped = 0;
      const prune = (node) => {
        if (!node) return node;
        if (node.type === 'cmp' || node.type === 'agg_cmp') {
          return valid.has(node.key) ? node : (dropped++, null);
        }
        if (node.type === 'not') {
          const child = prune(node.child);
          return child ? { ...node, child } : null;
        }
        if (node.children) {
          node.children = node.children.map(prune).filter(Boolean);
        }
        return node;
      };
      prune(this.editingRule.rule.root);

      // Ordering can reference a fact that's gone too.
      if (this.editingRule.order_by_key && !valid.has(this.editingRule.order_by_key)) {
        this.editingRule.order_by_key = null;
      }
      if (dropped) {
        this.showToast(
          `Removed ${dropped} condition${dropped === 1 ? '' : 's'} that don't apply to ${type}s.`,
          'warn',
        );
      }
      this.touchRule();
    },

    /** Aggregatable facts for the Order-by select, unit-qualified. */
    sortableFacts() {
      return this.factOptionGroups().flatMap((g) => g.facts).filter((f) => f.aggregatable);
    },

    // ── list-valued editors ─────────────────────────────────────────────
    listValue(node) {
      return Array.isArray(node.value) ? node.value : [];
    },

    addListValue(id, value) {
      const entry = this.nodeById(id);
      if (!entry || !value) return;
      const current = this.listValue(entry.node);
      if (!current.includes(value)) entry.node.value = [...current, value];
      this.touchRule();
    },

    removeListValue(id, value) {
      const entry = this.nodeById(id);
      if (!entry) return;
      entry.node.value = this.listValue(entry.node).filter((v) => v !== value);
      this.touchRule();
    },

    /**
     * Values to offer for a fact, from the last lookup of that key.
     *
     * Keyed by fact key rather than held as one flat list: several conditions
     * are on screen at once, and a single shared array meant whichever input
     * was focused last poisoned the suggestions of every other one.
     */
    suggestFor(key) {
      return this.suggestions[key] ?? [];
    },

    // ── fact picker (searchable combobox over the registry) ─────────────
    /** The display name for a fact key, unit-qualified like the picker rows. */
    factLabel(key) {
      for (const g of this.factOptionGroups()) {
        const hit = g.facts.find((f) => f.key === key);
        if (hit) return hit.optionLabel;
      }
      return key || 'Pick a fact…';
    },

    /** factOptionGroups narrowed by a search query — matches the label, the
     *  raw key, and the group name, so "radarr", "score" and "bitrate" all
     *  land where you'd expect. */
    factSearchGroups(query) {
      const q = String(query ?? '').trim().toLowerCase();
      const groups = this.factOptionGroups();
      if (!q) return groups;
      return groups
        .map((g) => ({
          ...g,
          facts: g.facts.filter((f) =>
            `${f.optionLabel} ${f.key} ${g.group}`.toLowerCase().includes(q)),
        }))
        .filter((g) => g.facts.length);
    },

    /** Same picker, for "Order by": column sorts first, then sortable facts. */
    orderByGroups(query) {
      const basics = [
        { key: null, optionLabel: 'Title' },
        { key: 'year', optionLabel: 'Year' },
        { key: 'added_at', optionLabel: 'Date added' },
        { key: 'size', optionLabel: 'File size' },
        { key: 'random', optionLabel: 'Random' },
      ];
      const groups = [{ group: 'Sort', facts: basics },
                      { group: 'Facts', facts: this.sortableFacts() }];
      const q = String(query ?? '').trim().toLowerCase();
      if (!q) return groups;
      return groups
        .map((g) => ({
          ...g,
          facts: g.facts.filter((f) =>
            `${f.optionLabel} ${f.key ?? ''}`.toLowerCase().includes(q)),
        }))
        .filter((g) => g.facts.length);
    },

    orderByLabel() {
      const key = this.editingRule?.order_by_key;
      for (const g of this.orderByGroups('')) {
        const hit = g.facts.find((f) => f.key === key);
        if (hit) return hit.optionLabel;
      }
      return 'Title';
    },

    /** Client-side narrowing for the custom dropdown, capped so a huge
     *  vocabulary (every cast member in the library) stays scrollable rather
     *  than becoming a thousand rendered rows. */
    suggestFiltered(key, query) {
      const q = String(query ?? '').trim().toLowerCase();
      const all = this.suggestFor(key);
      const hits = q ? all.filter((s) => String(s.value).toLowerCase().includes(q)) : all;
      return hits.slice(0, 60);
    },

    async suggestValues(key, q = '') {
      if (!key) return;
      // Cache the unfiltered lookup: a datalist narrows on the client as you
      // type, so re-querying per keystroke buys nothing until the server-side
      // cap actually bites.
      if (!q && this.suggestions[key]) return;
      try {
        const res = await api.facts.values(key, q);
        this.suggestions = { ...this.suggestions, [key]: res.values };
      } catch {
        this.suggestions = { ...this.suggestions, [key]: [] };
      }
    },

    // ── preview ─────────────────────────────────────────────────────────
    touchRule() {
      this.ruleDirty = true;
      clearTimeout(_previewTimer);
      _previewTimer = setTimeout(() => this.runPreview(), PREVIEW_DEBOUNCE_MS);
    },

    /**
     * Conditions the user hasn't filled in yet.
     *
     * A fresh condition has no value, which the compiler rightly rejects — but
     * firing a request just to render "expected a number, got None" in red
     * treats an unfinished edit as a failure. Count them here and say what's
     * missing instead.
     */
    incompleteLeaves(node = this.editingRule?.rule?.root, out = []) {
      if (!node) return out;
      if (node.type === 'cmp') {
        const arity = this.opArity(node);
        const v = node.value;
        const empty =
          arity === 0 ? false
          : arity === 'n' ? !Array.isArray(v) || v.length === 0
          : arity === 2 ? !Array.isArray(v) || v.some((x) => x === '' || x === null || x === undefined)
          : v === '' || v === null || v === undefined;
        if (empty) out.push(node);
      }
      (node.children ?? []).forEach((c) => this.incompleteLeaves(c, out));
      if (node.child) this.incompleteLeaves(node.child, out);
      return out;
    },

    async runPreview() {
      if (!this.editingRule) return;

      const pending = this.incompleteLeaves();
      if (pending.length) {
        this.preview = null;
        this.previewError = null;
        this.previewHint =
          pending.length === 1
            ? `Set a value for ${this.specFor(pending[0].key)?.label ?? pending[0].key}.`
            : `${pending.length} conditions still need a value.`;
        this.previewLoading = false;
        return;
      }
      this.previewHint = null;

      this.previewLoading = true;
      this.previewError = null;
      try {
        this.preview = await api.rules.preview({
          rule: this.editingRule.rule,
          library_keys: this.editingRule.library_keys,
          item_types: this.editingRule.item_types,
          order_by_key: this.editingRule.order_by_key,
          order_dir: this.editingRule.order_dir,
          limit_n: this.editingRule.limit_n,
          sample_size: 12,
        });
      } catch (e) {
        this.preview = null;
        // Validation errors are expected mid-edit — show them inline, quietly.
        this.previewError = e.message;
      } finally {
        this.previewLoading = false;
      }
    },

    async explainRule() {
      try {
        const res = await api.rules.explain({
          rule: this.editingRule.rule,
          library_keys: this.editingRule.library_keys,
          item_types: this.editingRule.item_types,
          order_by_key: this.editingRule.order_by_key,
          order_dir: this.editingRule.order_dir,
          limit_n: this.editingRule.limit_n,
        });
        this.explain = res;
      } catch (e) {
        this.showToast(e.message, false);
      }
    },

    // ── persistence ─────────────────────────────────────────────────────
    async saveRule() {
      const r = this.editingRule;
      if (!r.name.trim()) {
        this.showToast('Give the rule a name.', false);
        return;
      }
      const body = {
        name: r.name,
        description: r.description || null,
        rule: r.rule,
        library_keys: r.library_keys,
        item_types: r.item_types,
        order_by_key: r.order_by_key || null,
        order_dir: r.order_dir,
        limit_n: r.limit_n || null,
        enabled: r.enabled,
        sync_mode: r.sync_mode,
        // No collection_* fields: presentation is edited on the Collections
        // screen, and an absent field in RuleUpdate means "leave it alone".
      };
      try {
        const res = r.id
          ? await api.rules.update(r.id, body)
          : await api.rules.create(body);
        this.editingRule.id = res.rule.id;
        this.ruleDirty = false;
        // Collections lists rules, so it goes stale on every save.
        await Promise.all([this.loadRules(), this.loadCollections()]);
        this.showToast('Rule saved.');
      } catch (e) {
        this.showToast(e.message, false);
      }
    },

    async deleteRule(id, name) {
      const ok = await this.showConfirm({
        title: 'Delete rule',
        message: `Delete "${name}"? Labels already written to Plex are not removed yet — that arrives with sync.`,
        confirmLabel: 'Delete',
        danger: true,
      });
      if (!ok) return;
      try {
        await api.rules.remove(id);
        if (this.editingRule?.id === id) this.closeEditor();
        await Promise.all([this.loadRules(), this.loadCollections()]);
        this.showToast('Rule deleted.');
      } catch (e) {
        this.showToast(e.message, false);
      }
    },
  };
}
