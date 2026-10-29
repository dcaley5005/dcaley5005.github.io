---
name: portfolio-lab-qa
description: QA for any change to the Portfolio Lab page (site/portfolio-lab/index.html) before it is shown to Dan or merged. Runs the page at every phone, iPad, laptop and monitor size we've had bugs at, uses it like a person would, and measures the things that kept breaking. Use before opening or updating a pull request, before saying a page change is done, and whenever Dan reports a layout problem.
---

# Portfolio Lab QA

Dan reviews on an iPhone, an iPad (Chrome, landscape) and a 2560x1440 monitor. Most of our back-and-forth
came from layout problems that only showed at one size, after something loaded late, or in a state the
test never reached. This skill exists so those get caught before Dan sees them.

## When

- Before opening or updating a pull request that touches `site/portfolio-lab/index.html`.
- After any fix Dan asks for, before telling him it's ready.
- When Dan reports something looks off: reproduce it with the script first, then fix, then rerun.

## How

1. Test the real site with live data (the session can reach danielcaley.com):
   `node tools/qa/portfolio-lab-qa.js --live https://danielcaley.com/ --page portfolio-lab-preview --out <dir>`
   tests the open pull request's preview; `--page portfolio-lab` tests the live page after a merge.
   Files come through curl (the session's proxy and certificates) and are served to the browser locally.
   If the site isn't reachable, use a local folder holding `portfolio-lab/index.html` and real
   `portfolio-lab/data/`: `--root <folder>`. Say which data it is and how old. Never add made-up data
   files to make a test pass; missing per-fund files are reported as unchecked.
2. Use `--tickers SPY,QQQ` by default. Add a fund that's mostly bonds or one with thin data when the
   change touches empty states. Add `--only phone,ipadL` to rerun one size while fixing.
3. Read `report.md`. Every FAIL gets fixed or explained before Dan sees the change.
4. Run `python3 tools/qa/sheet.py <dir>` and look at the sheets, at least phone, iPad landscape, laptop and
   big monitor. The script measures; your eyes catch what it doesn't (overlaps, awkward wrapping, colors,
   cut-off text). Send Dan the sheets that show the change.
5. In the pull request and in the message to Dan, list the sizes checked, what failed and was fixed,
   and anything unchecked. Name the data used.

## What the script checks (and why)

Sizes: phone 390x844 and 430x932, iPad portrait 768x1024, iPad landscape 1024x768 (a short window:
cards are taller than the space), iPad 11" 1180x820, iPad Pro 1366x1024, laptop 1512x982, 1920x1080,
2560x1440. Light everywhere, dark on phone and laptop. Touch sizes tap; laptop and up use a mouse.

- **No sideways page scroll; the ETF table fits and the main panel is centered on wide screens.**
  2560 once cut off the table's last columns.
- **Alternatives window stays put while it loads.** It used to shrink after the data arrived and
  re-center, so everything jumped down about 30px.
- **The return-vs-risk chart never collapses.** On a 1024x768 iPad, a card with a long description
  squeezed it to a thin strip.
- **The chart keeps its size while clicking from pick to pick.** It once grew a little with every click.
- **Nothing sticks out of a card, buttons aren't cut off** (or the window scrolls to them), and the line
  chart's legend clears the risk tiles. The line chart has real room, not 45px.
- **Holdings side: no hover label pops up by itself, and the X shows on phones.**
- **Under the hood: no at-a-glance strip, the Stocks / Industries / Sectors toggle works, the map fills
  the window, the card doesn't grow when picking tiles, Industries and Sectors add up to the whole fund,
  See alternatives switches tabs.**
- **Each card is checked after its description has loaded and after changing the period.** Late-loading
  text caused several bugs that a first-paint check missed.
- **No script errors, no missing page-wide files.** Per-fund files missing from a partial local copy are
  listed as unchecked, not passed.

Local runs delay per-fund files by 300ms (`--latency`), like a real connection. Instant answers hid the
Alternatives jump in testing. A full run (9 sizes, 2 funds) takes about 15 minutes; use `--only` while fixing.

## Rules we learned the hard way

- Test the page as it really is. Never force hidden parts to show. That once produced a "fix" for
  a panel that no longer exists.
- Real data only, from the newest build available. Don't leave stand-in files in a test copy.
- Click through several cards in a row and change periods. One-click checks miss growing or drifting layouts.
- Check before and after anything that loads late (descriptions, holdings, price files).
- Change only what Dan asked for. If the right fix needs a design choice, ask him first.
- The test browser is Chromium. Dan's iPhone and iPad use Safari's engine under Chrome, so say that
  animations and fine spacing still need his eye on a real device.
- A preview or a merge counts as done only when the deploy run succeeded. Say if you couldn't open the
  page yourself.
