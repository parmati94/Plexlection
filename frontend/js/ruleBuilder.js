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
        collection_title: '',
        collection_summary: '',
      };
      this.preview = null;
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
          collection_title: rule.collection_title ?? '',
          collection_summary: rule.collection_summary ?? '',
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
      entry.node.op = spec?.operators?.[0]?.op ?? 'eq';
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
      return this.opMeta(node)?.arity ?? 1;
    },

    valueKind(node) {
      const meta = this.opMeta(node);
      if (!meta || meta.arity === 0) return null;
      return meta.value_kind;
    },

    coverageNote(key) {
      const spec = this.specFor(key);
      if (!spec?.coverage?.total) return '';
      const { known, total } = spec.coverage;
      if (known >= total) return '';
      return `known for ${known} of ${total}`;
    },

    /** Registry grouped for the fact <select>. */
    factOptionGroups() {
      const groups = {};
      for (const f of this.registry) {
        (groups[f.group] ??= []).push(f);
      }
      return Object.entries(groups).map(([group, facts]) => ({ group, facts }));
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

    async suggestValues(key, q) {
      try {
        const res = await api.facts.values(key, q);
        this.suggestions = res.values;
      } catch {
        this.suggestions = [];
      }
    },

    // ── preview ─────────────────────────────────────────────────────────
    touchRule() {
      this.ruleDirty = true;
      clearTimeout(_previewTimer);
      _previewTimer = setTimeout(() => this.runPreview(), PREVIEW_DEBOUNCE_MS);
    },

    async runPreview() {
      if (!this.editingRule) return;
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
        collection_title: r.collection_title || r.name,
        collection_summary: r.collection_summary || null,
      };
      try {
        const res = r.id
          ? await api.rules.update(r.id, body)
          : await api.rules.create(body);
        this.editingRule.id = res.rule.id;
        this.ruleDirty = false;
        await this.loadRules();
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
        await this.loadRules();
        this.showToast('Rule deleted.');
      } catch (e) {
        this.showToast(e.message, false);
      }
    },
  };
}
