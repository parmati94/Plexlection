/**
 * SSE mixin — spread into the root `app` component.
 *
 * The backend coalesces progress into a single `state` event every 250ms, so
 * this listener stays cheap even during a scan pushing thousands of updates
 * per second.
 *
 * State (sseConnected, scanState, …) is declared in app.js, not here: object
 * spread copies a getter's *value* rather than the getter, which would silently
 * break reactivity for anything derived.
 */

// Module scope, outside Alpine — an EventSource wrapped in a reactive proxy is
// pure overhead and can misbehave.
let _es = null;
let _reconnectAttempts = 0;
let _reconnectTimer = null;

const MAX_RECONNECT_ATTEMPTS = 8;
const RECONNECT_BASE_MS = 1500;

export function eventsMixin() {
  return {
    connectEvents() {
      this.disconnectEvents();

      const es = new EventSource('/api/events');
      _es = es;

      es.onopen = () => {
        this.sseConnected = true;
        _reconnectAttempts = 0;
        // Reconnecting is the proof the banner is stale. Without this a single
        // dropped stream — a backend restart, a phone waking from sleep — left
        // "Lost the live connection" on screen for the rest of the session
        // while everything behind it worked fine.
        this.clearError('Lost the live connection. Refresh the page to reconnect.');
      };

      // Snapshot on connect, so a client joining mid-scan sees the current state.
      es.addEventListener('init', (e) => this.applyState(JSON.parse(e.data)));
      es.addEventListener('state', (e) => this.applyState(JSON.parse(e.data)));

      es.addEventListener('scan_done', (e) => {
        const d = JSON.parse(e.data);
        this.scanState = null;
        // Report the summary, not just a count — "0 items" tells the user
        // nothing about why, which is how a fully-unmapped library looked like
        // a broken button.
        let message;
        if (d.cancelled) message = 'Scan cancelled.';
        else if (d.error) message = `Scan failed: ${d.error}`;
        else if (d.summary) message = d.summary;
        else message = `Scan finished — ${d.done} items.`;
        this.showToast(message, !d.error && !d.skipped);
        this.refreshHealth();
      });

      es.addEventListener('sync_done', (e) => {
        const d = JSON.parse(e.data);
        this.showToast(`Synced ${d.rule_name}: +${d.added} / −${d.removed}`, !d.error);
      });

      es.addEventListener('toast', (e) => {
        const d = JSON.parse(e.data);
        this.showToast(d.message, d.ok !== false);
      });

      es.addEventListener('ping', () => {});

      es.onerror = () => {
        this.sseConnected = false;
        if (_es && _es.readyState === EventSource.CLOSED) {
          _es = null;
          this.scheduleReconnect();
        }
      };
    },

    applyState(state) {
      this.scanState = state?.scan ?? null;
      this.syncState = state?.sync ?? null;
    },

    scheduleReconnect() {
      if (_reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
        this.error = 'Lost the live connection. Refresh the page to reconnect.';
        return;
      }
      _reconnectAttempts += 1;
      clearTimeout(_reconnectTimer);
      _reconnectTimer = setTimeout(
        () => this.connectEvents(),
        RECONNECT_BASE_MS * _reconnectAttempts, // linear backoff
      );
    },

    disconnectEvents() {
      clearTimeout(_reconnectTimer);
      if (_es) {
        _es.close();
        _es = null;
      }
      this.sseConnected = false;
    },

    /**
     * Browsers throttle or drop EventSource in background tabs. After a long
     * hide, verify the stream is actually alive rather than trusting the flag.
     */
    setupVisibilityListener() {
      let hiddenAt = null;
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
          hiddenAt = Date.now();
          return;
        }
        const away = hiddenAt ? Date.now() - hiddenAt : 0;
        hiddenAt = null;
        if (away > 30_000 || !_es || _es.readyState !== EventSource.OPEN) {
          _reconnectAttempts = 0;
          this.connectEvents();
          this.refreshHealth();
        }
      });
    },
  };
}
