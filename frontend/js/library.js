/**
 * Library mixin — item table, detail drawer, discovery trigger.
 */
import { api } from './api.js';

let _searchTimer = null;

export function libraryMixin() {
  return {
    async loadItems(reset = true) {
      if (reset) this.itemOffset = 0;
      this.itemsLoading = true;
      try {
        const res = await api.items.list({
          q: this.librarySearch,
          path_status: this.libraryFilter,
          sort: this.librarySort,
          direction: this.librarySortDir,
          limit: this.itemLimit,
          offset: this.itemOffset,
        });
        this.items = reset ? res.items : [...this.items, ...res.items];
        this.itemTotal = res.total;
      } catch (e) {
        this.error = e.message;
      } finally {
        this.itemsLoading = false;
      }
    },

    async loadMoreItems() {
      this.itemOffset += this.itemLimit;
      await this.loadItems(false);
    },

    /** Debounced so typing doesn't fire a query per keystroke. */
    onLibrarySearch() {
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => this.loadItems(true), 300);
    },

    setLibrarySort(key) {
      if (this.librarySort === key) {
        this.librarySortDir = this.librarySortDir === 'asc' ? 'desc' : 'asc';
      } else {
        this.librarySort = key;
        this.librarySortDir = key === 'title' ? 'asc' : 'desc';
      }
      this.loadItems(true);
    },

    setLibraryFilter(status) {
      this.libraryFilter = this.libraryFilter === status ? '' : status;
      this.loadItems(true);
    },

    async loadItemStats() {
      try {
        this.itemStats = await api.items.stats();
      } catch {
        this.itemStats = null;
      }
    },

    // ── detail drawer ───────────────────────────────────────────────────
    /**
     * The debugging surface: which provider produced each fact, when, and
     * whether the file has changed underneath it since.
     */
    async openItem(id) {
      this.itemDrawer = { open: true, loading: true, item: null, provenance: [] };
      try {
        const res = await api.items.get(id);
        this.itemDrawer = {
          open: true,
          loading: false,
          item: res.item,
          provenance: res.provenance,
        };
      } catch (e) {
        this.itemDrawer = { open: false, loading: false, item: null, provenance: [] };
        this.showToast(e.message, false);
      }
    },

    closeItem() {
      this.itemDrawer = { open: false, loading: false, item: null, provenance: [] };
    },

    /** Flatten nested fact namespaces into sorted dotted rows for display. */
    flattenFacts(facts, prefix = '') {
      const out = [];
      for (const [key, value] of Object.entries(facts || {})) {
        const path = prefix ? `${prefix}.${key}` : key;
        if (value && typeof value === 'object' && !Array.isArray(value)) {
          out.push(...this.flattenFacts(value, path));
        } else {
          out.push({ key: path, value });
        }
      }
      return out.sort((a, b) => a.key.localeCompare(b.key));
    },

    // ── discovery ───────────────────────────────────────────────────────
    async startDiscovery() {
      try {
        await api.scan.discover();
        this.showToast('Discovery started.');
      } catch (e) {
        if (e.status === 409) {
          this.showToast('A scan is already running.', false);
        } else if (e.status === 503) {
          this.showToast(e.message, false);
          this.setTab('settings');
        } else {
          this.showToast(e.message, false);
        }
      }
    },

    // cancelScan lives in scan.js — defining it in two mixins means whichever
    // is spread last silently wins.

    /**
     * Colour for the aspect-ratio glyph. Only scope (>= 2.3:1) is picked out —
     * it's the founding use case, and if every frame were accented the shape
     * would stop carrying information.
     */
    arTone(item) {
      const dar = item.facts?.video?.dar;
      if (!dar) return 'text-ink-faint/40';
      return dar >= 2.3 ? 'text-accent-400' : 'text-ink-faint';
    },

    // ── formatting ──────────────────────────────────────────────────────
    formatBytes(n) {
      if (!n) return '—';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let i = 0;
      let v = n;
      while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
      }
      return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
    },

    formatDate(epoch) {
      if (!epoch) return '—';
      return new Date(epoch * 1000).toLocaleDateString();
    },

    formatFactValue(v) {
      if (v === null || v === undefined) return '—';
      if (Array.isArray(v)) return v.join(', ');
      if (typeof v === 'boolean') return v ? 'yes' : 'no';
      return String(v);
    },
  };
}
