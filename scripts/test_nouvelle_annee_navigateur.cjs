/* Navigateur isolé, à lancer via verifier_nouvelle_annee_navigateur.py. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
    const setup = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
    const browser = await chromium.launch({
        executablePath: process.env.MYSCHOOL_TEST_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
        headless: true, args: ['--disable-background-networking'],
    });
    try {
        const context = await browser.newContext();
        await context.addCookies([{name: setup.cookie_name, value: setup.cookie_value, url: setup.url}]);
        await context.route('**/*', route => route.request().url().startsWith(setup.url)
            ? route.continue() : route.abort());
        const page = await context.newPage();
        const posts = [], getCreate = [], errors = [];
        let accept = false, dialogs = 0;
        page.on('dialog', async dialog => { dialogs++; await (accept ? dialog.accept() : dialog.dismiss()); });
        page.on('pageerror', error => errors.push(error.message));
        page.on('request', request => {
            if (new URL(request.url()).pathname === '/eleves/nouvelle-annee/creer/') {
                if (request.method() === 'POST') posts.push(request);
                else if (request.method() === 'GET') getCreate.push(request.url());
            }
        });
        await page.goto(setup.url + '/eleves/nouvelle-annee/', {waitUntil: 'domcontentloaded'});
        const button = page.locator('#btn-lancer');
        await button.click();
        await page.waitForTimeout(200);
        assert.equal(posts.length, 0, 'Annuler ne doit pas créer une année');
        assert.equal(await button.isEnabled(), true);
        for (const checkbox of await page.locator('.cb-convention').all()) await checkbox.check();
        accept = true;
        const started = Date.now();
        await button.click();
        await page.waitForURL('**/eleves/classes/', {timeout: 45000});
        await page.waitForLoadState('domcontentloaded');
        assert.equal(posts.length, 3, 'Les trois lots doivent être envoyés automatiquement');
        assert.ok(posts.every(request => request.isNavigationRequest()));
        assert.equal(getCreate.length, 0, 'L’actualisation de synchronisation ne doit pas interrompre le passage');
        assert.equal(dialogs, 2);
        assert.match(await page.locator('body').innerText(), /2026-2027/);
        const dureeCreation = Date.now() - started;
        for (const [path, prefixe] of [['/bus/liste/', 'BUS'], ['/bus/cantine/liste/', 'CANTINE']]) {
            await page.goto(setup.url + path, {waitUntil: 'domcontentloaded'});
            assert.equal(await page.locator('#annee-abonnements').inputValue(), '2026-2027');
            assert.equal(await page.getByText(prefixe + '-NOUVEAU', {exact: true}).count(), 1);
            assert.equal(await page.getByText(prefixe + '-ANCIEN', {exact: true}).count(), 0);
            const selecteur = page.locator('#annee-abonnements');
            await selecteur.selectOption('2025-2026');
            await Promise.all([
                page.waitForNavigation({waitUntil: 'domcontentloaded'}),
                selecteur.locator('..').getByRole('button', {name: 'Afficher', exact: true}).click(),
            ]);
            assert.equal(await page.getByText(prefixe + '-ANCIEN', {exact: true}).count(), 1);
            assert.equal(await page.getByText(prefixe + '-NOUVEAU', {exact: true}).count(), 0);
        }
        await page.goto(setup.url + '/bus/cantine/nouveau/?eleve=' + setup.eleve_id,
            {waitUntil: 'domcontentloaded'});
        assert.equal(await page.locator('#id_annee_scolaire').inputValue(), '2026-2027');
        assert.equal(await page.locator('#id_montant_tarif').inputValue(), '120000');
        assert.equal(await page.locator('#id_type_remise').inputValue(), 'POURCENTAGE');
        assert.equal(Number(await page.locator('#id_valeur_remise').inputValue()), 10);
        await Promise.all([
            page.waitForResponse(response => response.url().includes('/cantine/api/eleve/' + setup.eleve_sans_abonnement + '/')),
            page.locator('#id_eleve').selectOption(String(setup.eleve_sans_abonnement)),
        ]);
        await page.waitForFunction(() => document.getElementById('id_type_remise').value === '');
        assert.equal(await page.locator('#id_montant_tarif').inputValue(), '');
        assert.equal(await page.locator('#id_valeur_remise').inputValue(), '');
        assert.deepEqual(errors, [], 'Erreurs JavaScript sur le parcours');
        console.log(JSON.stringify({clic_reel: true, annulation: true, lots_automatiques: posts.length,
            rechargements_interrompant_creation: getCreate.length, nouvelle_annee_affichee: true,
            duree_creation_ms: dureeCreation, filtres_annee_bus_cantine: true,
            renouvellement_cantine_tarif_remise: true,
            changement_eleve_efface_ancienne_remise: true,
            duree_parcours_ms: Date.now() - started, erreurs_js: errors}, null, 2));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
