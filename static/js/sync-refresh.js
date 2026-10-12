/* Affiche les données arrivées après le rendu de cette page, sans perdre une saisie. */
(function () {
    "use strict";
    const script = document.currentScript;
    if (!script || !script.dataset.syncRevision) return;
    const revision = script.dataset.syncRevision;
    const endpoint = script.dataset.syncUrl;
    const fields = new Map();
    let timer, busy = false, stopped = false, pending = false, reloading = false, notice;
    const scrollKey = 'myschool-sync-scroll:' + location.pathname + location.search;

    function state(field) {
        if (field.type === 'checkbox' || field.type === 'radio') return field.checked;
        if (field.tagName === 'SELECT' && field.multiple) {
            return Array.from(field.selectedOptions, option => option.value).join('\u0000');
        }
        return field.isContentEditable ? field.textContent : field.value;
    }
    function rememberFields() {
        document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]),select,textarea,[contenteditable="true"]').forEach(field => {
            if (!fields.has(field)) fields.set(field, state(field));
        });
    }
    function editing() {
        for (const [field, initial] of fields) {
            if (field.isConnected && state(field) !== initial) return true;
        }
        const focused = document.activeElement;
        return !!(focused && focused.matches('input,select,textarea,[contenteditable="true"]'))
            || !!document.querySelector('.modal.show,.offcanvas.show,form[data-sync-preserve]')
            || Array.from(document.forms).some(form => form.__submitting);
    }
    function refresh() {
        if (!pending || reloading || document.hidden) return;
        if (editing()) {
            if (!notice) {
                notice = document.createElement('div');
                notice.className = 'alert alert-info position-fixed bottom-0 start-50 translate-middle-x mb-3 shadow';
                notice.style.zIndex = '1040';
                notice.setAttribute('role', 'status');
                notice.textContent = 'De nouvelles données sont disponibles. Elles s’afficheront après votre saisie.';
                document.body.appendChild(notice);
            }
            return;
        }
        reloading = true;
        try { sessionStorage.setItem(scrollKey, JSON.stringify([window.scrollX, window.scrollY])); } catch (_) {}
        window.location.reload();
    }
    function schedule(delay) {
        clearTimeout(timer);
        if (!stopped && !document.hidden) timer = setTimeout(check, delay);
    }
    async function check() {
        if (stopped || busy || document.hidden) return;
        clearTimeout(timer);
        busy = true;
        let delay = 2000;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 4000);
        try {
            const response = await fetch(endpoint, {
                credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
                headers: {'Accept': 'application/json'}
            });
            if (response.redirected || response.status === 401 || response.status === 403) {
                stopped = true;
                return;
            }
            if (!response.ok) throw new Error('Révision indisponible');
            const data = await response.json();
            if (data.ok && typeof data.revision === 'string' && data.revision !== revision) {
                pending = true;
                window.dispatchEvent(new CustomEvent('myschool:data-changed'));
                refresh();
            }
        } catch (_) {
            delay = 6000;
        } finally {
            clearTimeout(timeout);
            busy = false;
            if (!reloading) schedule(delay);
        }
    }
    function init() {
        rememberFields();
        document.addEventListener('focusin', rememberFields);
        document.addEventListener('input', refresh);
        document.addEventListener('change', refresh);
        document.addEventListener('focusout', () => setTimeout(refresh, 0));
        document.addEventListener('visibilitychange', () => {
            clearTimeout(timer);
            if (!document.hidden) { refresh(); check(); }
        });
        window.addEventListener('online', check);
        window.addEventListener('pageshow', check);
        try {
            const saved = sessionStorage.getItem(scrollKey);
            if (saved) {
                sessionStorage.removeItem(scrollKey);
                const position = JSON.parse(saved);
                requestAnimationFrame(() => window.scrollTo(position[0], position[1]));
            }
        } catch (_) {}
        check();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
