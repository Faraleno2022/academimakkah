/* Régressions de l'actualisation automatique : saisie, visibilité et connexion. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/js/sync-refresh.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function page({fields = [], hidden = false, response} = {}) {
    const listeners = {}, timers = new Map(), storage = new Map(), banners = [];
    const stats = {reloads: 0, requests: 0, events: []};
    let timerId = 0, next = response || {ok: true, status: 200, json: async () => ({ok: true, revision: 'initial'})};
    const document = {
        currentScript: {dataset: {syncRevision: 'initial', syncUrl: '/api/v1/sync/revision/'}},
        hidden, readyState: 'complete', activeElement: null, forms: [],
        querySelectorAll: () => fields, querySelector: () => null,
        addEventListener: (name, fn) => {listeners[name] = fn;},
        createElement: () => ({style: {}, setAttribute() {}}),
        body: {appendChild: element => banners.push(element)}
    };
    const location = {pathname: '/bus/cantine/liste/', search: '?periodicite=ANNUEL', reload: () => stats.reloads++};
    const window = {
        location, scrollX: 0, scrollY: 320, scrollTo() {},
        addEventListener: (name, fn) => {listeners[name] = fn;},
        dispatchEvent: event => stats.events.push(event.type)
    };
    vm.runInNewContext(source, {
        document, window, location, AbortController,
        CustomEvent: class {constructor(type) {this.type = type;}},
        sessionStorage: {getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k)},
        requestAnimationFrame: fn => fn(),
        setTimeout: (fn, ms) => {const id = ++timerId; timers.set(id, {fn, ms}); return id;},
        clearTimeout: id => timers.delete(id),
        fetch: async (url, options) => {
            stats.requests++;
            assert.equal(url, '/api/v1/sync/revision/');
            assert.equal(options.cache, 'no-store');
            assert.equal(options.credentials, 'same-origin');
            return typeof next === 'function' ? next() : next;
        }
    });
    return {document, listeners, timers, storage, banners, stats,
        changed: () => {next = {ok: true, status: 200, json: async () => ({ok: true, revision: 'nouvelle'})};},
        respond: value => {next = value;},
        tick: async () => {
            const entry = [...timers].find(([, t]) => t.ms !== 4000);
            assert.ok(entry, 'Un prochain contrôle doit être programmé');
            timers.delete(entry[0]);
            entry[1].fn();
            await flush();
        }
    };
}

test('aucun rechargement sans nouvelle donnée ; prochain contrôle à 2 s', async () => {
    const p = page();
    await flush();
    assert.equal(p.stats.reloads, 0);
    assert.deepEqual([...p.timers.values()].map(t => t.ms), [2000]);
    await p.tick();
    assert.equal(p.stats.requests, 2);
    assert.equal(p.stats.reloads, 0);
});

test('nouvelle donnée : actualisation unique, filtres et défilement conservés', async () => {
    const p = page();
    await flush();
    p.changed();
    await p.tick();
    assert.equal(p.stats.reloads, 1);
    assert.equal(p.storage.get('myschool-sync-scroll:/bus/cantine/liste/?periodicite=ANNUEL'), '[0,320]');
    p.listeners.focusout();
    await p.tick();
    assert.equal(p.stats.reloads, 1);
});

test('une saisie modifiée reste intacte ; actualisation après retour à la valeur initiale', async () => {
    const field = {tagName: 'INPUT', type: 'text', value: '200000', isConnected: true};
    const p = page({fields: [field]});
    await flush();
    field.value = '250000';
    p.changed();
    await p.tick();
    assert.equal(p.stats.reloads, 0);
    assert.equal(field.value, '250000');
    assert.equal(p.banners.length, 1);
    assert.match(p.banners[0].textContent, /après votre saisie/);
    field.value = '200000';
    p.listeners.change();
    assert.equal(p.stats.reloads, 1);
});

test('champ actif ou fenêtre de saisie ouverte : aucun rechargement', async () => {
    const p = page();
    await flush();
    p.document.activeElement = {matches: () => true};
    p.changed();
    await p.tick();
    assert.equal(p.stats.reloads, 0);
    p.document.activeElement = null;
    p.document.querySelector = () => ({});
    p.listeners.change();
    assert.equal(p.stats.reloads, 0);
    p.document.querySelector = () => null;
    p.listeners.change();
    assert.equal(p.stats.reloads, 1);
});

test('onglet caché : aucun sondage, contrôle dès son retour', async () => {
    const p = page({hidden: true});
    await flush();
    assert.equal(p.stats.requests, 0);
    p.changed();
    p.document.hidden = false;
    p.listeners.visibilitychange();
    await flush();
    assert.equal(p.stats.requests, 1);
    assert.equal(p.stats.reloads, 1);
});

test('session expirée : arrêt des sondages', async () => {
    const p = page({response: {ok: false, status: 403}});
    await flush();
    assert.equal(p.timers.size, 0);
    p.listeners.online();
    await flush();
    assert.equal(p.stats.requests, 1);
    assert.equal(p.stats.reloads, 0);
});

test('une requête en cours reste unique ; reprise après coupure réseau', async () => {
    let reject;
    const p = page({response: () => new Promise((resolve, fail) => {reject = fail;})});
    p.listeners.online();
    p.listeners.pageshow();
    assert.equal(p.stats.requests, 1);
    reject(new Error('Réseau coupé'));
    await flush();
    assert.deepEqual([...p.timers.values()].map(t => t.ms), [6000]);
    p.changed();
    await p.tick();
    assert.equal(p.stats.reloads, 1);
});
