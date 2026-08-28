/**
 * Collections mixin — dry-run diffs, syncing, history, posters.
 */
import { api } from './api.js';

export function collectionsMixin() {
  return {
    async loadCollections() {
      try {
        const res = await api.collections.list();
        this.collections = res.collections;
        this.dryRunDefault = res.dry_run_default;
      } catch (e) {
        this.error = e.message;
      }
    },

    /** Preview. Never writes, and ignores the guards so you can always see
     *  what a refused sync *would* have done. */
    async showDiff(ruleId) {
      this.diffModal = { open: true, loading: true, diff: null, ruleId, guarded: false };
      try {
        this.diffModal = {
          open: true, loading: false, guarded: false, ruleId,
          diff: await api.collections.diff(ruleId),
        };
      } catch (e) {
        this.diffModal = { open: false, loading: false, diff: null, ruleId, guarded: false };
        this.showToast(e.message, false);
      }
    },

    closeDiff() {
      this.diffModal = { open: false, loading: false, diff: null, ruleId: null, guarded: false };
    },

    /**
     * Apply a rule. A 409 means a guard refused it — the response carries the
     * diff, so we show exactly what was refused and offer an explicit override
     * rather than a bare error.
     */
    async syncCollection(ruleId, { dryRun = null, force = false } = {}) {
      this.syncing = ruleId;
      try {
        const diff = await api.collections.sync(ruleId, { dryRun, force });
        this.diffModal = { open: true, loading: false, diff, ruleId, guarded: false };
        if (diff.applied) {
          this.showToast(`Synced: +${diff.add_count} / −${diff.remove_count}`);
          await this.loadCollections();
        } else {
          this.showToast('Dry run — nothing was written to Plex.');
        }
      } catch (e) {
        if (e.status === 409 && e.payload?.diff) {
          this.diffModal = {
            open: true, loading: false, ruleId,
            diff: e.payload.diff, guarded: true, guardMessage: e.message,
          };
        } else {
          this.showToast(e.message, false);
        }
      } finally {
        this.syncing = null;
      }
    },

    /**
     * Apply from the diff modal. Confirms first when dry run is on, because the
     * global switch says nothing gets written and this deliberately ignores it —
     * silently overriding a safety setting the user turned on is not on.
     */
    async applyFromDiff() {
      const diff = this.diffModal.diff;
      if (this.dryRunDefault) {
        const ok = await this.showConfirm({
          title: 'Write to Plex now?',
          message:
            `Dry run is on, so nothing has been written so far. This applies ` +
            `${diff.add_count} label${diff.add_count === 1 ? '' : 's'}` +
            (diff.remove_count ? ` and removes ${diff.remove_count}` : '') +
            ` in Plex right now, and creates the collection.\n\n` +
            `Reversible with "unsync" on the collection card.`,
          confirmLabel: 'Write to Plex',
          danger: true,
        });
        if (!ok) return;
      }
      await this.syncCollection(this.diffModal.ruleId, { dryRun: false });
    },

    async syncAll() {
      const ok = await this.showConfirm({
        title: 'Sync every enabled rule',
        message: this.dryRunDefault
          ? 'Dry run is on, so this only reports what would change.'
          : 'This will write labels to Plex for every enabled rule.',
        confirmLabel: 'Sync all',
        danger: !this.dryRunDefault,
      });
      if (!ok) return;
      this.syncing = 'all';
      try {
        const res = await api.collections.syncAll();
        const failed = res.results.filter((r) => !r.ok).length;
        // A guard refusal is the app being cautious, not a failure — amber.
        this.showToast(
          `${res.count - failed} of ${res.count} rules synced${failed ? `, ${failed} refused` : ''}.`,
          failed ? 'warn' : true,
        );
        await this.loadCollections();
      } catch (e) {
        this.showToast(e.message, false);
      } finally {
        this.syncing = null;
      }
    },

    async unsyncCollection(ruleId, name, stripAll = false) {
      const ok = await this.showConfirm({
        title: stripAll ? 'Force-remove from Plex' : 'Remove from Plex',
        message: stripAll
          ? `Strip the label from EVERY item in Plex carrying it — including any ` +
            `applied by hand — and delete the "${name}" collection.\n\n` +
            `Your :pin and :veto labels are still left alone.`
          : `Strip the Plexlection label from every item we applied it to and delete ` +
            `the "${name}" collection.\n\nYour own :pin and :veto labels are left alone.`,
        confirmLabel: stripAll ? 'Force remove' : 'Remove',
        danger: true,
      });
      if (!ok) return;
      try {
        const res = await api.collections.unsync(ruleId, stripAll);
        // Leftovers after a normal unsync are hand-applied labels — say so and
        // point at the force option rather than leaving them a mystery.
        if (res.still_labelled) {
          this.showToast(
            `Removed ${res.removed}. ${res.still_labelled} labels remain that we didn't apply — ` +
            `use force-remove to clear those too.`, 'warn');
        } else {
          this.showToast(`Removed ${res.removed} labels; collection deleted.`);
        }
        await this.loadCollections();
      } catch (e) {
        this.showToast(e.message, false);
      }
    },

    async loadHistory(ruleId) {
      try {
        const res = await api.collections.history(ruleId);
        this.syncHistory = res.history;
        this.historyFor = ruleId;
      } catch {
        this.syncHistory = [];
      }
    },

    // ── presentation details (title, summary, order) ────────────────────
    openDetails(c) {
      if (this.detailsFor === c.id) {
        this.detailsFor = null;
        return;
      }
      this.detailsFor = c.id;
      this.detailsDraft = {
        collection_title: c.collection_title ?? '',
        collection_sort_title: c.collection_sort_title ?? '',
        collection_summary: c.collection_summary ?? '',
        collection_sort: c.collection_sort ?? '',
      };
    },

    async saveDetails(ruleId) {
      try {
        await api.rules.update(ruleId, { ...this.detailsDraft });
        this.detailsFor = null;
        await this.loadCollections();
        this.showToast('Details saved — they apply on the next sync.');
      } catch (e) {
        this.showToast(e.message, false);
      }
    },

    async uploadPoster(ruleId, event) {
      const file = event.target.files?.[0];
      if (!file) return;
      try {
        await api.collections.uploadPoster(ruleId, file);
        this.showToast('Poster saved — it uploads on the next sync.');
        await this.loadCollections();
      } catch (e) {
        this.showToast(e.message, false);
      } finally {
        event.target.value = '';
      }
    },

    async removePoster(ruleId) {
      try {
        await api.collections.removePoster(ruleId);
        await this.loadCollections();
      } catch (e) {
        this.showToast(e.message, false);
      }
    },
  };
}
