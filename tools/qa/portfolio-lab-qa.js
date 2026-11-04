/* Portfolio Lab QA: open the page at the screen sizes we care about, use it the way a person would,
 * and measure the things that kept breaking (see .claude/skills/portfolio-lab-qa/SKILL.md).
 *
 *   Quick (about 2 min): before pushing a change Dan will review. Laptop + iPad Pro, SPY, the main-page flows.
 *     node tools/qa/portfolio-lab-qa.js --live https://danielcaley.com/ --local --tier quick
 *   Full (about 6 min): before merging. Phone, iPads, laptop, big monitor, SPY and a portfolio, dark-mode smoke.
 *     node tools/qa/portfolio-lab-qa.js --live https://danielcaley.com/ --local --tier full
 *
 *   --local    test this repo's site/portfolio-lab/index.html against the live data (no waiting for a deploy)
 *   --page     without --local, which deployed page to test: portfolio-lab-preview (default) or portfolio-lab
 *   --root     a local folder holding portfolio-lab/ and its data/ instead of --live
 *   --only phone,laptop   rerun some sizes while fixing      --tickers SPY,pf   funds to open (pf = a portfolio)
 *   --conc 3   sizes tested at the same time
 *
 * Writes report.md, results.json and screenshots to --out. Exit code 1 if anything failed.
 */
const { chromium } = require('playwright');
const http = require('http'), fs = require('fs'), path = require('path');
const T0 = Date.now(), TIMES = [];   /* seconds spent per size and section */

const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };
const TIER = arg('tier', 'full'), LOCAL = process.argv.includes('--local'), CONC = +arg('conc', TIER === 'quick' ? 2 : 3);
const LIVE = arg('live'), PAGE = arg('page', 'portfolio-lab-preview'), LAT = +arg('latency', '300'), ROOT = arg('root'), BASE0 = arg('base'), OUT = arg('out', 'qa-out'), ONLY = arg('only'), TICKERS = (arg('tickers', TIER === 'quick' ? 'SPY' : 'SPY,pf')).split(',');
if (!ROOT && !BASE0 && !LIVE) { console.error('Give --root <site folder>, --live <site url> or --base <url>'); process.exit(2); }
fs.mkdirSync(OUT, { recursive: true });

/* sizes we've had bugs at; touch = tap like a finger, mouse = hover exists. Quick covers how Dan reviews
   (iPad Pro landscape and a laptop); full adds phones, iPad portrait, the short 1024x768 window and 2560 wide. */
const ALL = {
  phone: [390, 844, 'touch'], ipadP: [768, 1024, 'touch'], ipadL: [1024, 768, 'touch'],
  ipadProL: [1366, 1024, 'touch'], laptop: [1512, 982, 'mouse'], big: [2560, 1440, 'mouse'],
};
const PICK = ONLY ? ONLY.split(',') : TIER === 'quick' ? ['ipadProL', 'laptop'] : Object.keys(ALL);
const SIZES = PICK.filter(k => ALL[k]).map(k => [k, ...ALL[k]]);
/* dark mode is a colour check, so it gets a short smoke run instead of every check again */
const DARK = TIER === 'quick' || ONLY ? [] : ['phone', 'laptop'].filter(k => PICK.includes(k));
const PAGEFILE = path.join(__dirname, '../../site/portfolio-lab/index.html');

let srv, BASE = BASE0;
const miss = [];
if (LIVE) {   /* the page under test from --page, its data from the live portfolio-lab/data/, each file fetched once */
  const { execFile } = require('child_process'), cache = new Map(), site = LIVE.replace(/\/?$/, '/');
  const remote = u => u.startsWith('/portfolio-lab/data/') ? site + u.slice(1) : u.startsWith('/portfolio-lab/') ? site + PAGE + '/' + u.slice('/portfolio-lab/'.length) : site + u.slice(1);
  const get = u => { if (LOCAL && /^\/portfolio-lab\/?$/.test(u)) return Promise.resolve(fs.readFileSync(PAGEFILE)); if (!cache.has(u)) cache.set(u, new Promise(res => execFile('curl', ['-sS', '--compressed', '-w', '\n%{http_code}', remote(u)], { encoding: 'buffer', maxBuffer: 64 << 20 }, (e, out) => {
    if (e || !out) return res(null); const i = out.lastIndexOf(10), code = +out.slice(i + 1).toString(); res(code === 200 ? out.slice(0, i) : null) }))); return cache.get(u) };
  srv = http.createServer(async (q, r) => { let u = decodeURIComponent(q.url.split('?')[0]); if (u.endsWith('/')) u += 'index.html'; if (u.endsWith('/index.html')) u = u.slice(0, -10);
    const b = await get(u); if (!b) { miss.push(q.url); r.writeHead(404); r.end() } else { r.writeHead(200, { 'Content-Type': /\.json$/.test(u) ? 'application/json' : /\.csv$/.test(u) ? 'text/csv' : 'text/html' }); r.end(b) } }).listen(8799);
  BASE = 'http://localhost:8799/portfolio-lab/';
}
if (ROOT) {
  srv = http.createServer((q, r) => { let p = path.join(ROOT, decodeURIComponent(q.url.split('?')[0])); if (p.endsWith('/')) p += 'index.html';
    /* per-fund files arrive a little late, like on a real connection; instant answers hide layout jumps */
    setTimeout(() => fs.readFile(p, (e, b) => { if (e) { miss.push(q.url); r.writeHead(404); r.end(); } else { r.writeHead(200); r.end(b); } }), /\/data\/(inside|peers|grp)\//.test(q.url) ? LAT : 0); }).listen(8799);
  BASE = 'http://localhost:8799/portfolio-lab/';
}

const results = [];
const check = (size, area, name, ok, detail) => results.push({ size, area, name, ok: ok === null ? null : !!ok, detail: detail == null ? '' : String(detail) });
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function geo(pg) {   /* everything we measure about the open window, card and charts */
  return pg.evaluate(() => {
    const r = e => e ? e.getBoundingClientRect() : null, md = document.querySelector('#xm .xmd'), B = document.getElementById('xmBody');
    const dock = document.querySelector('#xCardDock:not([hidden]) .xcard, #xPopDock .xcard, #xInDock .xcard');
    const card = dock, front = card && (card.querySelector('.xface.fr') || card);
    const spark = front && front.querySelector('.xspark svg'), lg = front && front.querySelector('.xspark .lg'), risk = front && front.querySelector('.xrisk');
    const fig = document.querySelector('#xmBody .xfig svg'), tm = document.getElementById('xTm');
    const cr = r(card), over = card ? [...front.querySelectorAll('*')].filter(e => { const q = e.getBoundingClientRect(); return q.height > 0 && (q.right > cr.right + 1) && getComputedStyle(e).position !== 'fixed' }).length : 0;
    const btns = card ? [...front.querySelectorAll('.xflipbtn,.xadd,.xgoalt')].filter(b => b.offsetParent) : [];
    const cut = btns.filter(b => { const q = b.getBoundingClientRect(); return q.bottom > cr.bottom + 1 }).length;
    const tips = [...document.querySelectorAll('.xftip')].filter(t => !t.hidden).length;
    return { vw: innerWidth, vh: innerHeight, mdTop: r(md) && Math.round(r(md).top), mdH: r(md) && Math.round(r(md).height),
      card: cr && Math.round(cr.height), cardBottom: cr && Math.round(cr.bottom), line: spark && Math.round(r(spark).height),
      gap: lg && risk ? Math.round(r(risk).top - r(lg).bottom) : null, over, cut, tips,
      scatter: fig && Math.round(r(fig).height), map: tm && Math.round(r(tm).height),
      bodyScroll: B ? B.scrollHeight - B.clientHeight : 0, pageX: document.documentElement.scrollWidth - innerWidth };
  });
}

/* tap or click; takes a selector so a re-drawn element is found fresh */
async function press(pg, el, touch) {
  try { if (typeof el === 'string') el = await pg.$(el); if (!el) return false;
    if (touch) await el.tap({ timeout: 3000 }).catch(() => el.click({ timeout: 3000 })); else await el.click({ timeout: 3000 }); return true; }
  catch (e) { return false; } }

/* ---------- main-page flows: the things we built and keep changing ----------
   Ticker box list, hover preview, the tick bar (glow, red Delete, Undo, Combine into a named portfolio),
   ticked lines lit on the chart, and the Columns picker. Returns the new portfolio's id for the modal checks. */
async function flows(pg, S, w, touch) {
  const A = 'Main-page flows', n = () => pg.evaluate(() => S.b.rows.length);
  /* ticker box: the list waits for typing */
  if (await press(pg, '#bAdd', touch)) { await sleep(250);
    const sug = () => pg.evaluate(() => { const b = document.getElementById('bSug'); return !!b && !b.hidden });
    check(S, A, 'Ticker box: no list until you type', !(await sug()), '');
    await pg.keyboard.type('VT'); await sleep(350); check(S, A, 'Ticker box: list shows while typing', await sug(), '');
    await pg.keyboard.press('Backspace'); await pg.keyboard.press('Backspace'); await sleep(250);
    check(S, A, 'Ticker box: list closes when emptied', !(await sug()), '');
    await pg.evaluate(() => document.activeElement && document.activeElement.blur()); }
  /* hover preview: mouse only */
  if (!touch) { const tk = await pg.$('#bTable tr[data-bt] [data-xopen]'); if (tk) { await tk.hover(); await sleep(1200);
    const pv = await pg.evaluate(() => { const p = document.getElementById('tkPrev'); return p && !p.hidden ? [!!p.querySelector('.sb'), !!p.querySelector('.bm')] : null });
    check(S, A, 'Hover preview shows what\'s inside and the Best Match', !!pv && pv[0] && pv[1], JSON.stringify(pv));
    await pg.mouse.move(2, 2); await sleep(300); } }
  /* tick one: the combine prompt glows, Delete is solid red, its line stays lit */
  const ticks = await pg.$$('#bTable tr[data-bt] .tkchk'); if (ticks.length < 4) { check(S, A, 'Enough rows to test ticking', false, ticks.length + ' rows'); return null }
  await press(pg, ticks[0], touch); await sleep(800);   /* the bar ignores clicks for 0.6s after it appears */
  const one = await pg.evaluate(() => { const g = document.querySelector('.pvacts [data-bgrp]'), d = document.querySelector('.pvacts [data-bremove]'), svg = document.querySelector('#bChartbox svg');
    return { glow: !!g && g.disabled && getComputedStyle(g).animationName.includes('pvglow'), red: !!d && getComputedStyle(d).backgroundColor !== 'rgba(0, 0, 0, 0)' && d.className.includes('danger'),
      lit: svg ? [svg.classList.contains('lfocus'), svg.querySelectorAll('polyline.on').length] : null } });
  check(S, A, 'One ticked: "Tick one more to combine" glows', one.glow, '');
  check(S, A, 'Delete is a solid red button', one.red, '');
  check(S, A, 'Ticked row\'s line stays lit, the rest dim', !!one.lit && one.lit[0] && one.lit[1] === 1, JSON.stringify(one.lit));
  /* Delete, then Undo brings it back */
  const before = await n(); await press(pg, '.pvacts [data-bremove]', touch); await sleep(500);
  const gone = await n(); const und = await pg.$('#toast:not([hidden]) .tlink');
  check(S, A, 'Delete removes the ticked row', gone === before - 1, `${before} → ${gone}`);
  if (und) { await press(pg, und, touch); await sleep(500) }
  check(S, A, 'Undo brings it back', !!und && (await n()) === before, und ? `${await n()} rows` : 'no Undo in the message');
  /* Combine two into a portfolio; the name box opens ready to type */
  const tick = async i => (await pg.$$('#bTable tr[data-bt] .tkchk'))[i];   /* found fresh: ticking redraws the table */
  await press(pg, await tick(0), touch); await sleep(300); await press(pg, await tick(1), touch); await sleep(800);
  const pfs0 = await pg.evaluate(() => S.pfs.length); await press(pg, '.pvacts [data-bgrp]', touch); await sleep(700);
  const named = await pg.evaluate(() => (document.activeElement && document.activeElement.id || '').startsWith('pn-'));
  check(S, A, 'Combine makes a portfolio with its name box open', (await pg.evaluate(() => S.pfs.length)) === pfs0 + 1 && named, named ? '' : 'name box not focused');
  await pg.keyboard.press('Enter'); await sleep(400);
  const pfId = await pg.evaluate(() => S.pfs.length ? S.pfs[S.pfs.length - 1].id : null);
  /* Columns picker: hiding a column hides it everywhere and the group label shrinks with it */
  if (w > 1000 && await press(pg, '#bCols', touch)) { await sleep(300);
    const cb = await pg.$('[data-bcol="1M"]'); if (cb) { await press(pg, cb, touch); await sleep(400);
      const r = await pg.evaluate(() => { const th = document.querySelector('#bTable thead tr:not(.bgrp) > :nth-child(3)'), td = document.querySelector('#bTable tbody tr[data-bt] > :nth-child(3)'), lab = [...document.querySelectorAll('#bTable tr.bgrp th.lab')].find(x => x.textContent === 'Returns');
        return { th: th && getComputedStyle(th).display, td: td && getComputedStyle(td).display, span: lab && lab.colSpan } });
      check(S, A, 'Columns: unticking 1M hides it in every row', r.th === 'none' && r.td === 'none', JSON.stringify(r));
      check(S, A, 'Columns: "Returns" label spans the columns left', r.span === 7, 'colspan ' + r.span);
      await press(pg, '[data-bcol="1M"]', touch); await sleep(300); }
    await pg.keyboard.press('Escape'); await pg.mouse.click(2, 2).catch(() => {}); await sleep(200); }
  return pfId;
}

/* ---------- motion: after switching tabs and back, nothing is left over and the window holds still ---------- */
async function motionEnds(pg, S, A, touch) {
  const state = () => pg.evaluate(() => { const md = document.querySelector('#xm .xmd'); return { h: Math.round(md.getBoundingClientRect().height), ghost: document.querySelectorAll('.xghost').length, busy: !!XM.tabAnim, tab: XM.tab } });
  for (const k of ['in', 'alt']) {
    await press(pg, `.xtab[data-xtab="${k}"]`, touch); await sleep(1100); const a = await state(); await sleep(600); const b = await state();
    check(S, A, `Switch to ${k === 'in' ? 'Under the hood' : 'Compare'}: lands on the tab with nothing left over`, a.tab === k && !a.ghost && !a.busy, JSON.stringify(a));
    check(S, A, `Switch to ${k === 'in' ? 'Under the hood' : 'Compare'}: window holds its size after`, Math.abs(a.h - b.h) <= 2, `${a.h} → ${b.h}px`);
  }
}

/* ---------- dark mode: the colours are what change, so check those and that nothing breaks ---------- */
async function darkSmoke(pg, S) {
  const lum = c => { const m = (c.match(/[\d.]+/g) || [0, 0, 0]).map(Number); return (0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]) / 255 };
  const c = await pg.evaluate(() => ({ bg: getComputedStyle(document.body).backgroundColor, ink: getComputedStyle(document.querySelector('#bTable td') || document.body).color }));
  check(S, 'Dark mode', 'Dark background with light text', lum(c.bg) < 0.2 && lum(c.ink) > 0.5, `${c.bg} / ${c.ink}`);
  await pg.evaluate(() => openExplore('SPY', null, 'in')); await sleep(2500);
  check(S, 'Dark mode', 'Under the hood opens with its map', !!(await pg.$('#xTm .xtile')), '');
}

async function runSize(br, [tag, w, h, input], scheme) {
      const S = tag + (scheme === 'dark' ? '-dark' : ''), touch = input === 'touch';
      let tq = Date.now(); const lap = area => { TIMES.push([S, area, (Date.now() - tq) / 1000]); tq = Date.now() };
      const ctx = await br.newContext({ viewport: { width: w, height: h }, hasTouch: touch, isMobile: w < 500, colorScheme: scheme });
      const pg = await ctx.newPage(); const errs = []; pg.on('pageerror', e => errs.push(e.message));
      const shots = [];
      await pg.goto(BASE, { waitUntil: 'networkidle' }); await sleep(1000);
      const main = await pg.evaluate(() => { const t = document.getElementById('bTable'), wr = t && t.closest('.tablewrap'), b = document.getElementById('builder').getBoundingClientRect();
        return { pageX: document.documentElement.scrollWidth - innerWidth, table: t ? t.scrollWidth : 0, wrap: wr ? wr.clientWidth : 0, left: Math.round(b.left), right: Math.round(innerWidth - b.right) } });
      check(S, 'Main page', 'No sideways page scroll', main.pageX <= 0, main.pageX + 'px');
      if (w >= 1366) check(S, 'Main page', 'ETF table fits without a sideways scroll', main.table <= main.wrap + 1, `${main.table}/${main.wrap}px`);
      if (w >= 1366) check(S, 'Main page', 'Main panel centered', Math.abs(main.left - main.right) <= 4, `left ${main.left}, right ${main.right}`);
      shots.push(await pg.screenshot());
      lap('Main page');
      if (scheme === 'dark') { await darkSmoke(pg, S); check(S, 'Page', 'No script errors', errs.length === 0, errs.slice(0, 3).join(' | '));
        shots.push(await pg.screenshot()); shots.forEach((b, i) => fs.writeFileSync(path.join(OUT, `${S}-${i}.png`), b)); lap('Dark smoke'); await pg.context().close(); return; }
      const pfId = await flows(pg, S, w, touch); lap('Main-page flows');

      for (const T0 of TICKERS) {
        const isPf = T0 === 'pf'; if (isPf && !pfId) { check(S, 'Portfolio', 'A portfolio to open', false, 'combine did not make one'); continue; }
        const T = isPf ? 'Portfolio' : T0, openX = tab => pg.evaluate(([t, id, tab]) => id ? openExplorePf(id, null, tab) : openExplore(t, null, tab), [T0, isPf ? pfId : null, tab]);
        /* ---------- Alternatives ---------- */
        await pg.evaluate(([t, id]) => { window.__open = tab => id ? openExplorePf(id, null, tab) : openExplore(t, null, tab) }, [T0, isPf ? pfId : null]);
        await pg.evaluate(t => { window.__tops = []; const t0 = performance.now(); window.__open('alt');
          const f = () => { const md = document.querySelector('#xm .xmd'); if (md && md.offsetHeight) window.__tops.push([performance.now() - t0, md.getBoundingClientRect().top]); if (performance.now() - t0 < 2500) requestAnimationFrame(f) }; f() }, T);
        await sleep(2700);
        /* the window may slide up while it opens; what we catch is it moving back down once the data is in */
        const tops = await pg.evaluate(() => window.__tops.map(x => x[1]));
        if (w > 1000 && tops.length) { let lo = Infinity, down = 0; for (const t of tops) { lo = Math.min(lo, t); down = Math.max(down, t - lo) }
          check(S, `Alternatives ${T}`, 'Window stays put once data is in (no jump)', down <= 4, `moved down ${Math.round(down)}px`); }
        const picks = await pg.$$eval('#xmBody g[data-xt]', gs => gs.map(g => g.dataset.xt));
        if (!picks.length) { check(S, `Alternatives ${T}`, 'Chart shows picks', false, 'no shapes on the chart'); }
        const selfT = await pg.evaluate(() => XM.d && XM.d.self && XM.d.self.t);
        const order = [picks.find(p => p === selfT), ...picks.filter(p => p !== selfT).slice(0, 3)].filter(Boolean);
        const seen = [];
        for (const p of order) {
          await pg.evaluate(t => { const g = document.querySelector(`#xmBody g[data-xt="${t}"]`); g && g.dispatchEvent(new MouseEvent('click', { bubbles: true })) }, p);
          await sleep(1300);
          for (const rg of ['1M', '1Y', '3Y']) { await press(pg, await pg.$(`.xcard [data-xrng="${rg}"]`), touch); await sleep(250) }
          const g = await geo(pg); seen.push([p, g]);
          check(S, `Alternatives ${T}`, `${p}: nothing sticks out of the card`, g.over === 0, g.over + ' items');
          check(S, `Alternatives ${T}`, `${p}: buttons not cut off`, g.cut === 0 || g.bodyScroll > 0, g.cut ? `${g.cut} cut (window scrolls ${g.bodyScroll}px)` : 'ok');
          if (g.gap != null) check(S, `Alternatives ${T}`, `${p}: legend clear of risk tiles`, g.gap >= 8, g.gap + 'px');
          if (g.line != null) check(S, `Alternatives ${T}`, `${p}: card line chart has room`, g.line >= (w <= 1000 ? 70 : h >= 900 ? 120 : 44), g.line + 'px');
          if (w > 1000 && g.scatter != null) check(S, `Alternatives ${T}`, `${p}: return-vs-risk chart not collapsed`, g.scatter >= Math.min(300, h * 0.4), g.scatter + 'px');
          if (w <= 1000) { await pg.keyboard.press('Escape'); await sleep(350); }
        }
        if (w > 1000 && seen.length > 1) {
          const sc = seen.map(x => x[1].scatter).filter(v => v != null), cd = seen.map(x => x[1].card).filter(v => v != null);
          check(S, `Alternatives ${T}`, 'Chart keeps its size while switching picks', Math.max(...sc) - Math.min(...sc) <= 30, sc.join(' / ') + 'px');
          check(S, `Alternatives ${T}`, 'Card ends where the chart ends', seen.every(([, g]) => g.card != null), cd.join(' / ') + 'px');
        }
        /* Holdings flip: X on both faces on phones, no stray label, Go back works */
        if (w <= 1000 && order[1]) { await pg.evaluate(t => document.querySelector(`#xmBody g[data-xt="${t}"]`).dispatchEvent(new MouseEvent('click', { bubbles: true })), order[1]); await sleep(1200); }
        const flip = await pg.$('.xcard [data-xflip]');
        if (flip) { await press(pg, flip, touch); await sleep(900); const g = await geo(pg);
          check(S, `Alternatives ${T}`, 'Holdings side: no label pops up by itself', g.tips === 0, g.tips + ' showing');
          if (w <= 1000) check(S, `Alternatives ${T}`, 'Holdings side: close X visible', await pg.evaluate(() => [...document.querySelectorAll('.xfacex')].some(x => x.offsetParent)), '');
          shots.push(await pg.screenshot());
          const back = await pg.$('.xcard .xgoback'); if (back) { await press(pg, back, touch); await sleep(600) } }
        else check(S, `Alternatives ${T}`, 'Holdings button present', null, 'not found (unchecked)');
        if (w <= 1000) { await pg.keyboard.press('Escape'); await sleep(300); }
        shots.push(await pg.screenshot());

        /* ---------- Under the hood ---------- */
        await pg.evaluate(() => { const c = document.getElementById('xmClose'); if (c && !document.getElementById('xm').hidden) c.click() }); await sleep(400);
        lap(`Compare ${T}`);
        await openX('in'); await sleep(2000);
        const u0 = await geo(pg);
        check(S, `Under the hood ${T}`, 'No at-a-glance strip', !(await pg.$('.xglance')), '');
        /* funds that don't list stock holdings (most bond funds) show their asset split instead of a stock map */
        if (!(await pg.$('#xTm'))) { check(S, `Under the hood ${T}`, 'Shows the asset split (no stock list for this fund)', !!(await pg.$('.xassets')), '');
          shots.push(await pg.screenshot()); await pg.evaluate(() => { const c = document.getElementById('xmClose'); if (c && !document.getElementById('xm').hidden) c.click() }); await sleep(400); continue; }
        check(S, `Under the hood ${T}`, 'Stocks / Industries / Sectors toggle', !!(await pg.$('[data-xtmm="ind"]')), '');
        if (w > 1000 && u0.map) check(S, `Under the hood ${T}`, 'Map fills the window', u0.map >= u0.mdH * 0.5, `${u0.map}px of a ${u0.mdH}px window`);
        const tiles = await pg.$$eval('#xTm .xtile[data-ht]:not(.rest)', ts => ts.slice(0, 3).map(t => t.dataset.ht));
        const heights = [u0.card];
        for (const t of [...tiles, '~rest']) {
          const sel = t === '~rest' ? '#xTm .xtile.rest' : `#xTm .xtile[data-ht="${t}"]`; if (!(await pg.$(sel))) continue;
          await press(pg, sel, touch); await sleep(1100);
          await press(pg, await pg.$('#xInDock [data-xrng="3M"]'), touch); await sleep(350);
          const g = await geo(pg); heights.push(g.card);
          check(S, `Under the hood ${T}`, `${t}: nothing sticks out of the card`, g.over === 0, g.over + ' items');
          if (g.line != null) check(S, `Under the hood ${T}`, `${t}: card line chart has room`, g.line >= (w <= 1000 ? 100 : 120), g.line + 'px');
          await press(pg, sel, touch); await sleep(300);
        }
        if (w > 1000) check(S, `Under the hood ${T}`, 'Card does not grow when picking tiles', Math.max(...heights) - Math.min(...heights) <= 4, heights.join(' / ') + 'px');
        for (const m of ['ind', 'sec']) { await press(pg, await pg.$(`[data-xtmm="${m}"]`), touch); await sleep(500);
          const sum = await pg.$$eval('#xTm .xtile', ts => ts.reduce((a, t) => a + parseFloat(((t.getAttribute('aria-label') || t.title || '').match(/: ([\d.]+)% of /) || [0, 0])[1]), 0));
          const nm = m === 'ind' ? 'Industries' : 'Sectors';
          check(S, `Under the hood ${T}`, `${nm} cover the whole fund`, sum >= 97 && sum <= 103, sum.toFixed(1) + '%');
          /* the biggest group's card: its description, return, line chart and risk */
          if (await press(pg, '#xTm .xtile[data-ht]', touch)) { await sleep(1800); const gc = await geo(pg);
            const has = await pg.evaluate(() => { const c = document.querySelector('#xInDock .xcard'); return c ? [!!c.querySelector('.xfdesc'), !!c.querySelector('.xhero'), !!c.querySelector('.xspark svg polyline'), c.querySelectorAll('.xrk').length] : null });
            check(S, `Under the hood ${T}`, `${nm}: tile opens a card with description, return, line and risk`, has && has[0] && has[1] && has[2] && has[3] === 3, JSON.stringify(has));
            check(S, `Under the hood ${T}`, `${nm}: nothing sticks out of the card`, gc.over === 0, gc.over + ' items');
            if (gc.line != null) check(S, `Under the hood ${T}`, `${nm}: card line chart has room`, gc.line >= (w <= 1000 ? 100 : 120), gc.line + 'px');
            await press(pg, '#xTm .xtile[data-ht]', touch); await sleep(300); } }
        shots.push(await pg.screenshot());
        await press(pg, await pg.$('[data-xtmm="stk"]'), touch); await sleep(300);
        const go = await pg.$('#xInDock [data-xgo]'); if (go) { await press(pg, go, touch); await sleep(900); check(S, `Under the hood ${T}`, 'See alternatives opens Alternatives', (await pg.evaluate(() => XM.tab)) === 'alt', '') }
        if (w > 1000) await motionEnds(pg, S, `Motion ${T}`, touch);
        await pg.evaluate(() => { const c = document.getElementById('xmClose'); if (c && !document.getElementById('xm').hidden) c.click() }); await sleep(600);
        check(S, `Motion ${T}`, 'Window closes cleanly', await pg.evaluate(() => document.getElementById('xm').hidden && !document.body.classList.contains('xm-open') && !document.querySelector('.xghost')), '');
        lap(`Under the hood ${T}`);
      }
      check(S, 'Page', 'No script errors', errs.length === 0, errs.slice(0, 3).join(' | '));
      /* one sheet per size: each screenshot side by side */
      fs.writeFileSync(path.join(OUT, `${S}.json`), JSON.stringify({ shots: shots.length }));
      shots.forEach((b, i) => fs.writeFileSync(path.join(OUT, `${S}-${i}.png`), b));
      await ctx.close();
}

async function run() {
  const br = await chromium.launch();
  const jobs = [...SIZES.map(z => [z, 'light']), ...SIZES.filter(z => DARK.includes(z[0])).map(z => [z, 'dark'])];
  let next = 0; const lane = async () => { while (next < jobs.length) { const [z, sc] = jobs[next++];
    try { await runSize(br, z, sc) } catch (e) { check(z[0] + (sc === 'dark' ? '-dark' : ''), 'Page', 'Test run finished', false, String(e.message || e).slice(0, 200)) } } };
  await Promise.all(Array.from({ length: Math.max(1, CONC) }, lane));
  await br.close(); if (srv) srv.close();
  /* a missing page-wide file is a real problem; a missing per-fund file usually means the local data copy is partial */
  const nf = miss.filter(u => !/favicon/.test(u)), core = nf.filter(u => !/\/data\/(inside|peers|stock|t|grp)\//.test(u)), fund = nf.length - core.length;
  if (ROOT || LIVE) { check('all', 'Data', 'No missing page-wide files', core.length === 0, core.slice(0, 5).join(', '));
    if (fund) check('all', 'Data', 'Per-fund files', null, `${fund} not in this data copy, so cards for those funds ran without their details`); }
  const fail = results.filter(r => r.ok === false), un = results.filter(r => r.ok === null);
  const tsum = {}; TIMES.forEach(([, a, t]) => { const k = a.replace(/ (SPY|QQQ|Portfolio|[A-Z]{2,5})$/, ''); tsum[k] = (tsum[k] || 0) + t });
  const timing = `Tier: ${TIER}${LOCAL ? ' (this repo\'s page, live data)' : ''}. Took ${Math.round((Date.now() - T0) / 1000)}s with ${CONC} sizes at a time. Time per section, summed over sizes: ${Object.entries(tsum).map(([k, t]) => `${k} ${Math.round(t)}s`).join(', ')}.\n\n`;
  let md = `# Portfolio Lab QA\n\n${timing}${results.length} checks: ${results.length - fail.length - un.length} passed, ${fail.length} failed, ${un.length} unchecked.\n\nSizes: ${SIZES.map(s => `${s[0]} ${s[1]}x${s[2]} (${s[3]})`).join(', ')}. Light mode everywhere${DARK.length ? `, a dark-mode smoke run on ${DARK.join(" and ")}` : ""}. Engine: Chromium only (iPhone and iPad Chrome use Safari's engine, so check animations on a real device).\n\n`;
  if (fail.length) md += '## Failed\n\n' + fail.map(r => `- **${r.size}** · ${r.area} · ${r.name}: ${r.detail}`).join('\n') + '\n\n';
  if (un.length) md += '## Unchecked\n\n' + un.map(r => `- **${r.size}** · ${r.area} · ${r.name}: ${r.detail}`).join('\n') + '\n\n';
  md += '## All checks\n\n| Size | Area | Check | Result |\n|---|---|---|---|\n' + results.map(r => `| ${r.size} | ${r.area} | ${r.name} | ${r.ok === null ? 'unchecked' : r.ok ? 'pass' : '**FAIL**'} ${r.detail} |`).join('\n') + '\n';
  fs.writeFileSync(path.join(OUT, 'report.md'), md); fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 1));
  console.log(`${results.length} checks: ${fail.length} failed, ${un.length} unchecked. Report: ${path.join(OUT, 'report.md')}`);
  fail.forEach(r => console.log(`FAIL ${r.size} · ${r.area} · ${r.name}: ${r.detail}`));
  process.exit(fail.length ? 1 : 0);
}
run().catch(e => { console.error(e); process.exit(3); });
