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
        await page.locator('#id_date_debut').fill('2026-10-15');
        for (const [periodicite, fin] of [['TRIMESTRIEL', '14/01/2027'],
                ['DEUX_TRIMESTRES', '14/04/2027'], ['ANNUEL', '14/07/2027']]) {
            await page.locator('#id_periodicite').selectOption(periodicite);
            await page.waitForFunction(fin => document.getElementById('apercu-financier-cantine').textContent.includes(fin), fin);
        }
        await page.locator('#id_montant_tarif').fill('900000');
        await page.locator('#id_type_remise').selectOption('POURCENTAGE');
        await page.locator('#id_valeur_remise').fill('10');
        for (const [montant, mois, fin] of [['270000', 3, '14/01/2027'], ['540000', 6, '14/04/2027'], ['810000', 9, '14/07/2027']]) {
            await page.locator('#id_montant').fill(montant);
            const apercu = await page.locator('#apercu-financier-cantine').innerText();
            assert.ok(apercu.includes(mois + ' mois sur 9 mois'));
            assert.ok(apercu.includes(fin));
        }
        await page.goto(setup.url + '/bus/nouveau/?eleve=' + setup.eleve_sans_abonnement,
            {waitUntil: 'domcontentloaded'});
        await page.locator('#id_grille').selectOption(String(setup.grille_bus_id));
        await page.locator('#id_date_debut').fill('2026-10-15');
        await page.locator('#id_periodicite').selectOption('ANNUEL');
        for (const [montant, fin] of [['100000', '14/01/2027'], ['190000', '14/04/2027'], ['270000', '14/07/2027']]) {
            await page.locator('#id_montant').fill(montant);
            await page.waitForFunction(fin => document.getElementById('periode-abonnement-bus').textContent.includes(fin), fin);
            assert.match(await page.locator('#periode-abonnement-bus').innerText(), /15\/10\/2026/);
        }
        await page.goto(setup.url + '/eleves/liste/', {waitUntil: 'domcontentloaded'});
        const edit = page.locator('#tel-edit-' + setup.eleve_id);
        await page.locator('.btn-edit-tel[data-eleve-id="' + setup.eleve_id + '"]').click();
        await edit.locator('.input-tel-secondaire').fill('622 444 555');
        await edit.locator('.btn-save-tel').click();
        await page.waitForFunction(id => document.querySelector('#tel-display-' + id + ' .tel-secondaire-text').textContent === '+224622444555', setup.eleve_id);
        await page.reload({waitUntil: 'domcontentloaded'});
        assert.equal(await page.locator('#tel-display-' + setup.eleve_id + ' .tel-secondaire-text').innerText(), '+224622444555');
        await page.locator('.btn-edit-tel[data-eleve-id="' + setup.eleve_id + '"]').click();
        await edit.locator('.input-tel-secondaire').fill('');
        await edit.locator('.btn-save-tel').click();
        await page.waitForFunction(id => document.querySelector('#tel-display-' + id + ' .tel-secondaire').hidden, setup.eleve_id);
        assert.equal(await page.locator('#tel-display-' + setup.eleve_id + ' .tel-text').innerText(), '+224620111222');
        assert.deepEqual(errors, [], 'Erreurs JavaScript sur le parcours');
        console.log(JSON.stringify({clic_reel: true, annulation: true, lots_automatiques: posts.length,
            rechargements_interrompant_creation: getCreate.length, nouvelle_annee_affichee: true,
            duree_creation_ms: dureeCreation, filtres_annee_bus_cantine: true,
            renouvellement_cantine_tarif_remise: true,
            changement_eleve_efface_ancienne_remise: true,
            validite_bus_cantine_trois_six_neuf_mois: true,
            cantine_couverture_montants_apres_remise: true,
            deuxieme_telephone_ajout_rechargement_effacement: true,
            duree_parcours_ms: Date.now() - started, erreurs_js: errors}, null, 2));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
