/**
 * Settings mixin — connections, library selection, path mapping.
 *
 * State lives in app.js; this file is methods only (see the getter caveat there).
 */
import { api } from './api.js';

export function settingsMixin() {
  return {
    async loadSettings() {
      try {
        const res = await api.settings.get();
        this.settings = res.settings;
        this.configured = res.configured;
      } catch (e) {
        this.error = e.message;
      }
    },

    /**
     * Save a partial patch. Secrets the user didn't touch are still the mask
     * string, which the backend reads as "leave it alone".
     */
    async saveSettings(patch) {
      this.settingsSaving = true;
      try {
        const res = await api.settings.update(patch);
        this.settings = res.settings;
        this.configured = res.configured;
        this.showToast('Settings saved.');
        return true;
      } catch (e) {
        this.showToast(e.message, false);
        return false;
      } finally {
        this.settingsSaving = false;
      }
    },

    async saveConnection(service) {
      const ok = await this.saveSettings({ [service]: this.settings[service] });
      if (ok) await this.testConnection(service);
    },

    async testConnection(service) {
      this.connTest = { ...this.connTest, [service]: { busy: true } };
      try {
        const res = await api.settings.test(service);
        this.connTest = { ...this.connTest, [service]: res };
        if (res.ok && service === 'plex') await this.loadPlexSections();
      } catch (e) {
        this.connTest = { ...this.connTest, [service]: { ok: false, detail: e.message } };
      }
    },

    // ── Plex libraries ──────────────────────────────────────────────────
    async loadPlexSections() {
      try {
        const res = await api.settings.plexSections();
        this.plexSections = res.sections;
      } catch (e) {
        this.plexSections = [];
        // Not a toast: an unreachable Plex is already reported by the test button.
        console.warn('Could not list Plex sections:', e.message);
      }
    },

    async toggleLibrary(key) {
      const current = new Set(this.settings.plex.libraries || []);
      current.has(key) ? current.delete(key) : current.add(key);
      const libraries = [...current];
      this.settings.plex.libraries = libraries;
      await this.saveSettings({ plex: { libraries } });
      this.plexSections = this.plexSections.map((s) => ({ ...s, selected: current.has(s.key) }));
    },

    // ── Path mappings ───────────────────────────────────────────────────
    addMapping(plex = '', local = '') {
      this.settings.path_mappings = [...(this.settings.path_mappings || []), { plex, local }];
      this.pathTest = null;
    },

    removeMapping(index) {
      this.settings.path_mappings = this.settings.path_mappings.filter((_, i) => i !== index);
      this.pathTest = null;
    },

    /** Pre-fill from a prefix the backend reported as unmatched — this is the
     *  step that makes path mapping tolerable to set up. */
    adoptPrefix(prefix) {
      this.addMapping(prefix, '/media/videos');
      this.setTab('settings');
      this.showToast(`Added a mapping for ${prefix} — set the container path and save.`);
    },

    async testPaths() {
      this.pathTest = { busy: true };
      try {
        this.pathTest = await api.paths.test(this.settings.path_mappings || []);
      } catch (e) {
        this.pathTest = { error: e.message };
      }
    },

    async savePaths() {
      const ok = await this.saveSettings({ path_mappings: this.settings.path_mappings || [] });
      if (ok) {
        await this.testPaths();
        await this.loadPathHealth();
      }
    },

    async loadSchedule() {
      try {
        const res = await api.settings.schedule();
        this.scheduleJobs = res.jobs ?? [];
      } catch {
        this.scheduleJobs = [];
      }
    },

    async saveSchedule() {
      const ok = await this.saveSettings({ schedule: this.settings.schedule });
      // Next-run times only exist after the backend re-registers the jobs.
      if (ok) await this.loadSchedule();
    },

    async loadPathHealth() {
      try {
        this.pathHealth = await api.paths.health();
      } catch {
        this.pathHealth = null;
      }
    },
  };
}
