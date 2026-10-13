const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const code = fs.readFileSync(path.join(__dirname, '../static/js/public-videos.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function element(initial = {}) {
    const listeners = new Map();
    return Object.assign({
        hidden: true, disabled: false, textContent: '',
        addEventListener(name, fn, options = {}) {
            listeners.set(name, [...(listeners.get(name) || []), {fn, once: options.once}]);
        },
        removeEventListener(name, fn) {
            listeners.set(name, (listeners.get(name) || []).filter(item => item.fn !== fn));
        },
        emit(name, event) {
            const calls = [...(listeners.get(name) || [])];
            return calls.map(item => {
                if (item.once) this.removeEventListener(name, item.fn);
                return item.fn(event);
            });
        },
    }, initial);
}

function page({native = false, supported = true, preloaded = true, saveData = false, count = 1} = {}) {
    const scripts = [], instances = [], timers = new Map();
    const window = element();
    class Hls {
        static Events = {MEDIA_ATTACHED: 'attached', MANIFEST_PARSED: 'manifest', ERROR: 'error'};
        static ErrorTypes = {MEDIA_ERROR: 'media'};
        static isSupported() { return supported; }
        constructor(config) {
            this.config = config;
            this.handlers = {};
            this.loads = [];
            this.stops = 0;
            this.recoveries = 0;
            this.levels = [{height: 360}, {height: 480}, {height: 720}];
            instances.push(this);
        }
        on(name, fn) { this.handlers[name] = fn; }
        emit(name, data) { this.handlers[name]?.(name, data); }
        attachMedia(video) { this.video = video; this.emit('attached'); }
        loadSource(url) { this.url = url; }
        startLoad(position) { this.loads.push(position); }
        stopLoad() { this.stops++; }
        recoverMediaError() { this.recoveries++; }
        destroy() {
            this.destroyed = true;
            this.video.pause();
            this.video.currentTime = 0; // Comme le détachement réel du MediaSource.
        }
    }
    if (preloaded) window.Hls = Hls;
    const cards = Array.from({length: count}, (_, index) => {
        const button = element(), label = element(), quality = element({value: 'auto'});
        const selector = element(), status = element();
        const card = {querySelector: selectorName => ({
            '[data-video-start]': button, '[data-video-start-label]': label,
            '[data-video-quality]': quality, '[data-video-selector]': selector,
            '[data-video-status]': status,
        })[selectorName]};
        const video = element({
            paused: true, src: '', currentTime: 0, duration: 132, plays: 0, loadCalls: 0,
            dataset: {hlsUrl: `/static/spot/stream-v1/${index}/master.m3u8`,
                fallbackUrl: `/static/spot/stream-v1/${index}/economique.mp4`},
            closest: () => card, querySelectorAll: () => [{remove() {}}], removeAttribute() {},
            canPlayType: () => native ? 'probably' : '',
            load() { this.loadCalls++; },
            play() { this.paused = false; this.plays++; this.emit('play'); return Promise.resolve(); },
            pause() { if (!this.paused) { this.paused = true; this.emit('pause'); } },
        });
        return {video, button, label, quality, selector, status};
    });
    const document = {
        currentScript: {dataset: {hlsLibrary: '/static/vendor/hls/hls-1.7.3.light.min.js'}},
        readyState: 'complete', querySelectorAll: () => cards.map(card => card.video),
        createElement: () => ({remove() {}}), head: {appendChild: item => scripts.push(item)},
    };
    const navigator = {connection: {saveData}};
    vm.runInNewContext(code, {document, window, navigator,
        setTimeout: (fn, delay) => { const id = Symbol(); timers.set(id, {fn, delay}); return id; },
        clearTimeout: id => timers.delete(id),
    });
    return {cards, scripts, instances, window, Hls, navigator, timers,
        async start(index = 0) { await Promise.all(cards[index].button.emit('click')); await flush(); },
        ready(index = 0) { instances[index].emit('manifest'); },
    };
}

test('aucune requête vidéo ni bibliothèque avant le clic', () => {
    const p = page({preloaded: false});
    assert.equal(p.scripts.length, 0);
    assert.equal(p.instances.length, 0);
    assert.equal(p.cards[0].video.plays, 0);
    assert.equal(p.cards[0].video.loadCalls, 0);
    assert.equal(p.cards[0].button.hidden, false);
});

test('Safari utilise son lecteur HLS sans télécharger une bibliothèque', async () => {
    const p = page({native: true, preloaded: false});
    await p.start();
    assert.equal(p.scripts.length, 0);
    assert.match(p.cards[0].video.src, /master.m3u8$/);
    assert.equal(p.cards[0].video.paused, false);
});

test('le mode économique natif charge directement les petits segments 360p', async () => {
    const p = page({native: true});
    p.cards[0].quality.value = 'eco';
    p.cards[0].quality.emit('change');
    assert.equal(p.cards[0].video.loadCalls, 0);
    await p.start();
    assert.match(p.cards[0].video.src, /360p\/index.m3u8$/);
});

test('le lecteur natif respecte aussi le réglage économie de données du navigateur', async () => {
    const p = page({native: true, saveData: true});
    await p.start();
    assert.match(p.cards[0].video.src, /360p\/index.m3u8$/);
});

test('le lecteur démarre au plus bas débit avec une réserve et sans double chargement', async () => {
    const p = page();
    await p.start();
    assert.equal(p.instances[0].loads.length, 0);
    p.ready();
    assert.equal(p.instances[0].config.startLevel, 0);
    assert.equal(p.instances[0].config.maxBufferLength, 30);
    assert.equal(p.instances[0].config.enableWorker, false);
    assert.equal(p.instances[0].loads.length, 1);
    assert.equal(p.cards[0].video.paused, false);
});

test('deux clics partagent la bibliothèque et seule la dernière vidéo démarre', async () => {
    const p = page({preloaded: false, count: 2});
    p.cards[0].button.emit('click');
    p.cards[1].button.emit('click');
    assert.equal(p.scripts.length, 1);
    p.window.Hls = p.Hls;
    p.scripts[0].onload();
    await flush();
    assert.equal(p.instances.length, 1);
    assert.equal(p.instances[0].video, p.cards[1].video);
    p.ready();
    assert.equal(p.cards[0].video.plays, 0);
    assert.equal(p.cards[1].video.paused, false);
});

test('une bibliothèque inaccessible conserve la lecture MP4 économique', async () => {
    const p = page({preloaded: false});
    p.cards[0].button.emit('click');
    p.scripts[0].onerror();
    await flush();
    assert.match(p.cards[0].video.src, /economique.mp4$/);
    assert.equal(p.cards[0].selector.hidden, true);
});

test('un navigateur sans MediaSource utilise le MP4 léger', async () => {
    const p = page({supported: false});
    await p.start();
    assert.match(p.cards[0].video.src, /economique.mp4$/);
    assert.equal(p.instances.length, 0);
});

test('une erreur fatale conserve la position après détachement du MediaSource', async () => {
    const p = page();
    await p.start(); p.ready();
    p.cards[0].video.currentTime = 43;
    p.instances[0].emit('error', {fatal: true, type: 'media'});
    assert.equal(p.instances[0].recoveries, 1);
    p.instances[0].emit('error', {fatal: true, type: 'media'});
    assert.equal(p.instances[0].destroyed, true);
    assert.match(p.cards[0].video.src, /economique.mp4$/);
    p.cards[0].video.emit('loadedmetadata');
    assert.equal(p.cards[0].video.currentTime, 43);
    assert.equal(p.cards[0].video.paused, false);
});

test('une pause arrête les téléchargements et le retour du réseau ne force pas la lecture', async () => {
    const p = page();
    await p.start(); p.ready();
    p.cards[0].video.pause();
    assert.equal(p.instances[0].stops, 1);
    p.window.emit('online');
    assert.equal(p.instances[0].loads.length, 1);
    p.cards[0].video.currentTime = 35;
    p.cards[0].video.play();
    assert.deepEqual(p.instances[0].loads, [0, 35]);
});

test('lancer une seconde vidéo arrête les téléchargements de la première', async () => {
    const p = page({count: 2});
    await p.start(); p.ready();
    await p.start(1); p.ready(1);
    assert.equal(p.cards[0].video.paused, true);
    assert.ok(p.instances[0].stops > 0);
    assert.equal(p.cards[1].video.paused, false);
});

test('économie de données, retour en automatique et choix HD', async () => {
    const p = page({saveData: true});
    await p.start(); p.ready();
    assert.equal(p.instances[0].autoLevelCapping, 0);
    p.navigator.connection.saveData = false;
    p.cards[0].quality.emit('change');
    assert.equal(p.instances[0].autoLevelCapping, -1);
    p.cards[0].quality.value = 'eco'; p.cards[0].quality.emit('change');
    assert.equal(p.instances[0].nextLevel, 0);
    p.cards[0].quality.value = 'hd'; p.cards[0].quality.emit('change');
    assert.equal(p.instances[0].autoLevelCapping, -1);
    assert.equal(p.instances[0].nextLevel, 2);
});

test('changer de qualité en pause conserve la position sans redémarrer sur Safari', async () => {
    const p = page({native: true});
    await p.start(); p.cards[0].video.emit('loadedmetadata');
    p.cards[0].video.currentTime = 45;
    p.cards[0].video.pause();
    p.cards[0].quality.value = 'eco'; p.cards[0].quality.emit('change');
    p.cards[0].video.emit('loadedmetadata');
    assert.equal(p.cards[0].video.currentTime, 45);
    assert.equal(p.cards[0].video.paused, true);
});

test('fin de vidéo et fermeture de page libèrent le lecteur', async () => {
    const p = page();
    await p.start(); p.ready();
    p.cards[0].video.emit('ended');
    p.window.emit('online');
    assert.equal(p.instances[0].loads.length, 1);
    p.window.emit('pagehide');
    assert.equal(p.instances[0].destroyed, true);
});

test('un retour arrière restaure le lecteur conservé dans le cache de navigation', async () => {
    const p = page();
    await p.start(); p.ready();
    p.cards[0].video.currentTime = 42;
    p.window.emit('pagehide', {persisted: true});
    assert.equal(p.instances[0].destroyed, undefined);
    assert.equal(p.cards[0].video.paused, true);
    p.window.emit('online');
    assert.equal(p.instances[0].loads.length, 1);
    p.cards[0].video.play();
    assert.deepEqual(p.instances[0].loads, [0, 42]);
});
