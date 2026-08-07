/**
 * Root Alpine component.
 *
 * One `app` component holds shared state; feature areas are mixins spread in.
 * Two rules that are easy to break and hard to debug:
 *
 *   1. ALL getters live in this object literal, never in a spread mixin —
 *      object spread copies a getter's evaluated value, not the getter itself,
 *      so a getter defined in a mixin silently stops being reactive.
 *   2. Never add x-init="init()" in markup. Alpine 3 calls init() automatically;
 *      doing both runs it twice (two EventSources, double fetches).
 */
import Alpine from 'alpinejs';

import { api } from './api.js';
import { collectionsMixin } from './collections.js';
import { eventsMixin } from './events.js';
import { libraryMixin } from './library.js';
import { ruleBuilderMixin } from './ruleBuilder.js';
import { scanMixin } from './scan.js';
import { settingsMixin } from './settings.js';

const THEMES = [
  { id: 'amber', label: 'Amber', swatch: '#e89a15' },
  { id: 'violet', label: 'Violet', swatch: '#8b5cf6' },
  { id: 'emerald', label: 'Emerald', swatch: '#10b981' },
  { id: 'blue', label: 'Blue', swatch: '#3b82f6' },
  { id: 'rose', label: 'Rose', swatch: '#f43f5e' },
];

const TABS = [
  { id: 'library', label: 'Library' },
  { id: 'rules', label: 'Rules' },
  { id: 'collections', label: 'Collections' },
  { id: 'scan', label: 'Scan' },
  { id: 'settings', label: 'Settings' },
];

const LS = {
  theme: 'pxl-theme',
  tab: 'pxl-tab',
  reduceMotion: 'pxl-reduce-motion',
};

document.addEventListener('alpine:init', () => {
  Alpine.data('app', () => ({
    ...eventsMixin(),
    ...settingsMixin(),
    ...libraryMixin(),
    ...scanMixin(),
    ...ruleBuilderMixin(),
    ...collectionsMixin(),

    // ── shell ───────────────────────────────────────────────────────────
    tabs: TABS,
    themes: THEMES,
    activeTab: 'library',
    theme: 'amber',
    reduceMotion: false,

    loading: false,
    error: null,
    toast: null,

    loginEnabled: false,
    health: null,
    sseConnected: false,
    scanState: null,
    syncState: null,

    // ── settings ────────────────────────────────────────────────────────
    settings: null,
    configured: {},
    settingsSaving: false,
    connTest: {},
    plexSections: [],
    pathHealth: null,
    pathTest: null,
    scheduleJobs: [],

    // ── library ─────────────────────────────────────────────────────────
    items: [],
    itemStats: null,
    itemTotal: 0,
    itemOffset: 0,
    itemLimit: 100,
    itemsLoading: false,
    librarySearch: '',
    libraryFilter: '',
    librarySort: 'title',
    librarySortDir: 'asc',
    itemDrawer: { open: false, loading: false, item: null, provenance: [] },

    // ── scan / facts ────────────────────────────────────────────────────
    registry: [],
    factGroups: [],
    providerCards: [],
    coverage: {},
    selectedProviders: [],
    scanRuns: [],
    lastRun: null,

    // ── rules ───────────────────────────────────────────────────────────
    rules: [],
    editingRule: null,
    ruleDirty: false,
    preview: null,
    previewLoading: false,
    previewError: null,
    previewHint: null,
    explain: null,
    suggestions: [],

    // ── collections ─────────────────────────────────────────────────────
    collections: [],
    dryRunDefault: true,
    syncing: null,
    diffModal: { open: false, loading: false, diff: null, ruleId: null, guarded: false, guardMessage: '' },
    syncHistory: [],
    historyFor: null,

    confirmDialog: { show: false, title: '', message: '', confirmLabel: 'Confirm', danger: true, resolve: null },

    // ── getters (MUST stay in this literal) ─────────────────────────────
    get itemCount() {
      return this.itemStats?.total ?? this.health?.items ?? 0;
    },
    get unmappedCount() {
      return this.pathHealth?.counts?.unmapped ?? 0;
    },
    get missingCount() {
      return this.pathHealth?.counts?.missing ?? 0;
    },
    get mappedCount() {
      return this.pathHealth?.counts?.mapped ?? 0;
    },
    get plexConfigured() {
      return !!this.configured?.plex;
    },
    get librariesSelected() {
      return (this.settings?.plex?.libraries || []).length;
    },
    get scanActive() {
      return !!this.scanState && this.scanState.status === 'running';
    },
    get scanPct() {
      const s = this.scanState;
      return s && s.total ? Math.min(1, s.done / s.total) : 0;
    },
    get libraryStats() {
      return [
        { label: 'Items', value: this.itemCount },
        { label: 'Mapped', value: this.mappedCount },
        { label: 'Unmapped', value: this.unmappedCount, tone: this.unmappedCount ? 'warn' : null },
        { label: 'With facts', value: this.itemStats?.with_facts ?? 0 },
      ];
    },
    get hasMoreItems() {
      return this.items.length < this.itemTotal;
    },
    get drawerFacts() {
      return this.flattenFacts(this.itemDrawer.item?.facts);
    },
    // Depth-first rows for the rule builder. Must be a getter here — a getter
    // defined inside a spread mixin is copied by value and stops being reactive.
    get ruleFlatNodes() {
      return this.flattenTree();
    },

    // ── lifecycle ───────────────────────────────────────────────────────
    async init() {
      // Theme first, before paint, so there's no flash of the wrong accent.
      this.applyTheme(localStorage.getItem(LS.theme) || 'amber');
      this.setReduceMotion(
        localStorage.getItem(LS.reduceMotion) === 'true' ||
          (localStorage.getItem(LS.reduceMotion) === null &&
            window.matchMedia('(prefers-reduced-motion: reduce)').matches),
      );

      const savedTab = localStorage.getItem(LS.tab);
      if (savedTab && TABS.some((t) => t.id === savedTab)) this.activeTab = savedTab;

      try {
        const auth = await api.auth.status();
        this.loginEnabled = auth.enabled;
        if (auth.enabled && !auth.authenticated) {
          window.location.replace('/login.html');
          return;
        }
      } catch {
        // A failed auth probe shouldn't block the app; health will show the problem.
      }

      await Promise.all([
        this.refreshHealth(),
        this.loadSettings(),
        this.loadPathHealth(),
        this.loadItemStats(),
        this.loadRegistry(),
        this.loadScanState(),
        this.loadRuns(),
        this.loadRules(),
        this.loadCollections(),
        this.loadSchedule(),
      ]);

      if (this.plexConfigured) this.loadPlexSections();
      this.loadItems(true);

      this.connectEvents();
      this.setupVisibilityListener();

      this.$watch('toast', (t) => {
        clearTimeout(this._toastTimer);
        if (t) this._toastTimer = setTimeout(() => { this.toast = null; }, 4500);
      });

      // A finished scan changes the catalog underneath the table.
      this.$watch('scanState', (now, before) => {
        if (before && !now) this.afterScan();
      });
    },

    async afterScan() {
      await Promise.all([
        this.loadItemStats(),
        this.loadPathHealth(),
        this.refreshHealth(),
        this.loadScanState(),
        this.loadRuns(),
        this.loadRegistry(),
      ]);
      await this.loadItems(true);
    },

    // ── actions ─────────────────────────────────────────────────────────
    setTab(id) {
      this.activeTab = id;
      localStorage.setItem(LS.tab, id);
      this.refreshTab(id);
    },

    /**
     * Pull the data a tab depends on when it's opened.
     *
     * Everything used to load once at init, so a rule created on the Rules tab
     * didn't appear under Collections until a full page reload — and scan
     * coverage went stale the same way.
     */
    refreshTab(id) {
      if (id === 'collections') this.loadCollections();
      else if (id === 'rules') this.loadRules();
      else if (id === 'scan') { this.loadScanState(); this.loadRuns(); this.loadRegistry(); }
      else if (id === 'library') this.loadItemStats();
      else if (id === 'settings') { this.loadPathHealth(); this.loadSchedule(); }
    },

    applyTheme(id) {
      if (!THEMES.some((t) => t.id === id)) id = 'amber';
      this.theme = id;
      document.documentElement.setAttribute('data-theme', id);
      localStorage.setItem(LS.theme, id);
    },


    setReduceMotion(on) {
      this.reduceMotion = !!on;
      document.documentElement.classList.toggle('reduce-motion', this.reduceMotion);
      localStorage.setItem(LS.reduceMotion, String(this.reduceMotion));
    },

    async refreshHealth() {
      try {
        this.health = await api.health();
      } catch (e) {
        this.health = null;
        this.error = e.message;
      }
    },

    async logout() {
      await api.auth.logout();
      window.location.replace('/login.html');
    },

    showToast(message, ok = true) {
      this.toast = { message, ok };
    },

    // Promise-based confirm, rendered once in footer.html.
    showConfirm({ title, message, confirmLabel = 'Confirm', danger = true }) {
      return new Promise((resolve) => {
        this.confirmDialog = { show: true, title, message, confirmLabel, danger, resolve };
      });
    },
    confirmDialogAccept() {
      this.confirmDialog.resolve?.(true);
      this.confirmDialog.show = false;
    },
    confirmDialogCancel() {
      this.confirmDialog.resolve?.(false);
      this.confirmDialog.show = false;
    },
  }));
});

Alpine.start();
