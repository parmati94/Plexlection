/**
 * Scan mixin — provider cards, scan launching, coverage.
 */
import { api } from './api.js';

const COST_LABELS = {
  free: { label: 'free', hint: 'No IO — computed from data already stored.' },
  cheap: { label: 'cheap', hint: 'Reads the media file. Fast, no decoding.' },
  network: { label: 'network', hint: 'External API. Rate-limited.' },
  expensive: { label: 'expensive', hint: 'Decodes frames. Seconds to minutes per item.' },
};

export function scanMixin() {
  return {
    async loadRegistry() {
      try {
        const res = await api.facts.registry();
        this.registry = res.facts;
        this.factGroups = res.groups;
        this.providerCards = res.providers;
      } catch (e) {
        this.error = e.message;
      }
    },

    async loadScanState() {
      try {
        const res = await api.scan.state();
        this.lastRun = res.last_run;
        this.coverage = res.coverage;
        if (res.live) this.scanState = res.live;
      } catch {
        /* health already surfaces a dead backend */
      }
    },

    async loadRuns() {
      try {
        const res = await api.scan.runs(10);
        this.scanRuns = res.runs;
      } catch {
        this.scanRuns = [];
      }
    },

    costMeta(cost) {
      return COST_LABELS[cost] ?? { label: cost, hint: '' };
    },

    coverageFor(providerId) {
      return this.coverage?.[providerId] ?? { known: 0, total: 0, errors: 0, skipped: 0, stale: 0 };
    },

    coveragePct(providerId) {
      const c = this.coverageFor(providerId);
      return c.total ? Math.round((c.known / c.total) * 100) : 0;
    },

    /**
     * Coverage split per item type, one row per bar.
     *
     * The backend supplies plural labels so every caller doesn't reinvent
     * "movie" -> "Movies". Types with nothing indexed are dropped rather than
     * shown as an empty 0/0 bar.
     */
    coverageRows(providerId) {
      const byType = this.coverageFor(providerId).by_type ?? {};
      return Object.entries(byType)
        .filter(([, b]) => b.total > 0)
        .map(([type, b]) => ({
          type,
          label: b.label,
          known: b.known,
          total: b.total,
          errors: b.errors,
          stale: b.stale,
          pct: b.total ? Math.round((b.known / b.total) * 100) : 0,
        }));
    },

    toggleProvider(id) {
      const set = new Set(this.selectedProviders);
      set.has(id) ? set.delete(id) : set.add(id);
      this.selectedProviders = [...set];
    },

    /**
     * Kick off a scan. Passing no provider list means "everything at or below
     * cheap"; expensive providers must be named explicitly so a stray click
     * can't start hours of frame decoding.
     */
    async startScan({ providers = null, force = false, discover = true } = {}) {
      try {
        await api.scan.start({
          providers: providers ?? (this.selectedProviders.length ? this.selectedProviders : null),
          force,
          discover,
          // 'network' so a plain "Scan library" includes TMDB and Tautulli.
          // Capping at 'cheap' silently skipped both, which is why they had to
          // be recomputed by hand — and why anything derived from them stayed
          // empty. Expensive providers still have to be asked for by name.
          max_cost: 'network',
        });
        this.showToast('Scan started.');
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

    async rescanProvider(id) {
      const ok = await this.showConfirm({
        title: `Recompute ${id}?`,
        message:
          'Every item will be reprocessed by this provider, ignoring cached results.',
        confirmLabel: 'Recompute',
        danger: false,
      });
      if (!ok) return;
      await this.startScan({ providers: [id], force: true, discover: false });
    },

    async cancelScan() {
      try {
        await api.scan.cancel();
        this.showToast('Cancelling…');
      } catch (e) {
        this.showToast(e.message, false);
      }
    },

    formatDuration(seconds) {
      if (!seconds) return '—';
      if (seconds < 60) return `${Math.round(seconds)}s`;
      const m = Math.floor(seconds / 60);
      return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
    },

    runDuration(run) {
      if (!run?.started_at) return '—';
      const end = run.finished_at ?? Math.floor(Date.now() / 1000);
      return this.formatDuration(end - run.started_at);
    },
  };
}
