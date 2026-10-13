/* Lecture adaptative au clic, avec secours MP4 économique. */
(function () {
    'use strict';
    const script = document.currentScript;
    if (!script || window.__publicVideosReady) return;
    window.__publicVideosReady = true;
    const libraryUrl = script.dataset.hlsLibrary;
    let libraryPromise, active;
    const players = [];

    function library() {
        if (window.Hls) return Promise.resolve(window.Hls);
        if (libraryPromise) return libraryPromise;
        libraryPromise = new Promise((resolve, reject) => {
            const element = document.createElement('script');
            const timeout = setTimeout(() => fail(), 15000);
            function fail() {
                clearTimeout(timeout);
                element.remove();
                reject(new Error('Lecteur indisponible'));
            }
            element.src = libraryUrl;
            element.async = true;
            element.onload = () => {
                clearTimeout(timeout);
                window.Hls ? resolve(window.Hls) : fail();
            };
            element.onerror = fail;
            document.head.appendChild(element);
        }).catch(error => { libraryPromise = null; throw error; });
        return libraryPromise;
    }

    function message(state) {
        state.status.textContent = state.mode === 'fallback' || state.quality.value === 'eco'
            ? 'Économie de données.'
            : state.quality.value === 'hd' ? 'Haute qualité.' : 'Qualité adaptée automatiquement à votre connexion.';
    }

    function play(state) {
        if (!state.wantPlay || active !== state) return;
        const result = state.video.play();
        if (result && result.catch) result.catch(error => {
            if (error.name === 'NotAllowedError') {
                state.status.textContent = 'Appuyez sur le bouton lecture du lecteur pour démarrer.';
            }
        });
    }

    function source(state, url, restore) {
        const position = typeof restore === 'number' ? restore : restore ? state.video.currentTime : 0;
        state.switching = true;
        if (state.metadataHandler) state.video.removeEventListener('loadedmetadata', state.metadataHandler);
        state.metadataHandler = () => {
            state.metadataHandler = null;
            state.switching = false;
            if (position > 0 && Number.isFinite(state.video.duration)) {
                state.video.currentTime = Math.min(position, Math.max(0, state.video.duration - 0.1));
            }
            play(state);
        };
        state.video.addEventListener('loadedmetadata', state.metadataHandler, {once: true});
        state.video.src = url;
        state.video.load();
        state.button.hidden = true;
        // Safari conserve ainsi le geste utilisateur lors du premier démarrage.
        if (position === 0) play(state);
    }

    function fallback(state) {
        const position = state.video.currentTime;
        state.switching = true;
        if (state.hls) { state.hls.destroy(); state.hls = null; }
        state.mode = 'fallback';
        state.selector.hidden = true;
        source(state, state.video.dataset.fallbackUrl, position);
        message(state);
    }

    function nativeUrl(state) {
        const saveData = !!(navigator.connection && navigator.connection.saveData);
        const file = state.quality.value === 'eco' || (state.quality.value === 'auto' && saveData) ? '360p/index.m3u8'
            : state.quality.value === 'hd' ? '720p/index.m3u8' : 'master.m3u8';
        return state.video.dataset.hlsUrl.replace('master.m3u8', file);
    }

    function quality(state) {
        if (state.hls) {
            const saveData = !!(navigator.connection && navigator.connection.saveData);
            const low = state.quality.value === 'eco' || (state.quality.value === 'auto' && saveData);
            state.hls.autoLevelCapping = low ? 0 : -1;
            state.hls.nextLevel = state.quality.value === 'hd' ? state.hls.levels.length - 1 : low ? 0 : -1;
        } else if (state.mode === 'native') {
            source(state, nativeUrl(state), true);
        }
        message(state);
    }

    function activate(state) {
        for (const other of players) {
            if (other !== state) {
                other.wantPlay = false;
                other.video.pause();
                if (other.hls) other.hls.stopLoad();
                other.loading = false;
            }
        }
        active = state;
        state.wantPlay = true;
    }

    async function start(state) {
        activate(state);
        if (state.preparing) return;
        state.preparing = true;
        state.button.disabled = true;
        state.label.textContent = 'Chargement…';
        state.status.textContent = 'Préparation de la lecture…';
        state.selector.hidden = false;
        state.mediaRecovery = false;
        try {
            if (state.video.canPlayType('application/vnd.apple.mpegurl')) {
                state.mode = 'native';
                source(state, nativeUrl(state), false);
                message(state);
                return;
            }
            const Hls = await library();
            if (active !== state || !state.wantPlay) return;
            if (!Hls.isSupported()) { fallback(state); return; }
            state.mode = 'hls';
            state.switching = true;
            state.hls = new Hls({
                autoStartLoad: false, enableWorker: false, lowLatencyMode: false,
                startLevel: 0, abrEwmaDefaultEstimate: 300000,
                capLevelToPlayerSize: true, maxBufferLength: 30,
                maxMaxBufferLength: 60, maxBufferSize: 8 * 1024 * 1024, backBufferLength: 10,
            });
            state.hls.on(Hls.Events.MEDIA_ATTACHED, () => state.hls.loadSource(state.video.dataset.hlsUrl));
            state.hls.on(Hls.Events.MANIFEST_PARSED, () => {
                quality(state);
                state.switching = false;
                if (state.wantPlay && active === state) {
                    state.hls.startLoad(state.video.currentTime);
                    state.loading = true;
                }
                state.button.hidden = true;
                play(state);
            });
            state.hls.on(Hls.Events.ERROR, (_event, data) => {
                if (!data.fatal) return; // HLS.js reprend les coupures temporaires.
                if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !state.mediaRecovery) {
                    state.mediaRecovery = true;
                    state.hls.recoverMediaError();
                } else fallback(state);
            });
            state.hls.attachMedia(state.video);
        } catch (_) {
            if (active === state && state.wantPlay) fallback(state);
        } finally {
            state.preparing = false;
            state.button.disabled = false;
            state.label.textContent = 'Lire la vidéo';
        }
    }

    function init() {
        document.querySelectorAll('[data-adaptive-video]').forEach(video => {
            const card = video.closest('[data-video-card]');
            const state = {video, mode: 'idle', wantPlay: false,
                button: card.querySelector('[data-video-start]'),
                label: card.querySelector('[data-video-start-label]'),
                quality: card.querySelector('[data-video-quality]'),
                selector: card.querySelector('[data-video-selector]'),
                status: card.querySelector('[data-video-status]')};
            players.push(state);
            // Sans JavaScript, les sources HTML servent déjà le MP4 léger.
            video.querySelectorAll('source').forEach(element => element.remove());
            video.removeAttribute('src');
            state.button.hidden = false;
            state.selector.hidden = false;
            state.button.addEventListener('click', () => start(state));
            state.quality.addEventListener('change', () => quality(state));
            video.addEventListener('play', () => {
                activate(state);
                if (state.hls && !state.switching && !state.loading) {
                    state.hls.startLoad(video.currentTime);
                    state.loading = true;
                }
            });
            video.addEventListener('pause', () => {
                if (state.switching) return;
                state.wantPlay = false;
                if (state.hls) state.hls.stopLoad();
                state.loading = false;
            });
            video.addEventListener('ended', () => {
                state.wantPlay = false;
                if (state.hls) state.hls.stopLoad();
                state.loading = false;
            });
            video.addEventListener('playing', () => message(state));
            video.addEventListener('waiting', () => {
                if (state.wantPlay) state.status.textContent = 'Mise en mémoire tampon…';
            });
            video.addEventListener('error', () => {
                if (state.mode === 'idle') return;
                if (state.mode !== 'fallback') fallback(state);
                else {
                    state.wantPlay = false;
                    state.button.hidden = false;
                    state.status.textContent = 'La connexion ne permet pas de charger la vidéo. Réessayez dans un instant.';
                }
            });
        });
        window.addEventListener('online', () => {
            for (const state of players) {
                if (state.hls && state.wantPlay && active === state) {
                    state.hls.startLoad(state.video.currentTime);
                    state.loading = true;
                }
            }
        });
        window.addEventListener('pagehide', event => {
            for (const state of players) {
                state.wantPlay = false;
                state.video.pause();
                if (state.hls) {
                    if (event && event.persisted) state.hls.stopLoad();
                    else state.hls.destroy();
                }
                state.loading = false;
            }
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
    else init();
})();
