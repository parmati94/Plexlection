import Alpine from 'alpinejs';

import { api } from './api.js';

document.addEventListener('alpine:init', () => {
  Alpine.data('loginForm', () => ({
    username: '',
    password: '',
    error: null,
    busy: false,

    async init() {
      document.documentElement.setAttribute(
        'data-theme',
        localStorage.getItem('pxl-theme') || 'violet',
      );
      // Already signed in, or auth is switched off entirely.
      try {
        const s = await api.auth.status();
        if (!s.enabled || s.authenticated) window.location.replace('/');
      } catch {
        /* show the form and let the submit surface the real error */
      }
    },

    async login() {
      this.busy = true;
      this.error = null;
      try {
        await api.auth.login(this.username, this.password);
        window.location.replace('/');
      } catch (e) {
        // 429 from the rate limiter arrives here with its retry-after message.
        this.error = e.message;
        this.password = '';
      } finally {
        this.busy = false;
      }
    },
  }));
});

Alpine.start();
