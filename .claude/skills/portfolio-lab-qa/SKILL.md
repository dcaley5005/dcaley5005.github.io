---
name: portfolio-lab-qa
description: QA for any change to the Portfolio Lab page (site/portfolio-lab/index.html) before it is shown to Dan or merged. Runs the page at every phone, iPad, laptop and monitor size we've had bugs at, uses it like a person would, and measures the things that kept breaking. Use before opening or updating a pull request, before saying a page change is done, and whenever Dan reports a layout problem.
---

# Portfolio Lab QA

Dan reviews on an iPhone, an iPad (Chrome, landscape) and a 2560x1440 monitor. Most of our back-and-forth
came from layout problems that only showed at one size, after something loaded late, or in a state the
test never reached. This skill exists so those get caught before Dan sees them.

## When: two tiers

Dan reviews the preview while we work, so a long run during his review is wasted: the page changes
under it. Run the quick tier before every push he'll look at, and the full tier only before a merge.

- **Quick, about 1 minute:** before pushing any page change Dan will review. iPad Pro landscape and a
  laptop (how he reviews), SPY, and the main-page flows.
- **Full, about 3 minutes:** when Dan says merge, before merging. Phone, iPad portrait, the short
  1024x768 iPad window, iPad Pro, laptop and 2560 wide, SPY and a portfolio, plus a dark-mode smoke run.
- When Dan reports something looks off: reproduce it with `--only <size>` first, then fix, then rerun.

## How

1. Test this repo's page against the live data, so nothing waits on a deploy:
   `node tools/qa/portfolio-lab-qa.js --live https://danielcaley.com/ --local --tier quick --out <dir>`
   (`--tier full` before a merge). Without `--local`, `--page portfolio-lab-preview` tests the deployed
   preview and `--page portfolio-lab` the live page after a merge. If the site isn't reachable, use a
   local folder holding `portfolio-lab/index.html` and real `portfolio-lab/data/`: `--root <folder>`.
   Never add made-up data files to make a test pass; missing per-fund files are reported as unchecked.
2. Sizes run three at a time (`--conc`). `--only phone,laptop` reruns some sizes while fixing.
   `--tickers SPY,pf` picks what to open; `pf` is a portfolio made by the Combine check. Add a mostly-bond
   fund when a change touches empty states.
3. Read `report.md`. Its first line gives the time per section. Every FAIL gets fixed or explained before
   Dan sees the change. A failing check can be the test's fault (see the rules below): prove which with a
   small script before changing the page or the check.
4. Look at the screenshots, at least phone, iPad landscape, laptop and big monitor (`python3 tools/qa/sheet.py <dir>`).
   The script measures; your eyes catch overlaps, wrapping, colours and cut-off text.
5. In the pull request and the message to Dan, say which tier ran, what failed and was fixed, and what
   was unchecked.

## What the script checks (and why)

Sizes: phone 390x844, iPad portrait 768x1024, iPad landscape 1024x768 (a short window: cards are taller
than the space), iPad Pro 1366x1024, laptop 1512x982, 2560x1440. Touch sizes tap; laptop and up use a mouse.
Dark mode gets a smoke run on phone and laptop: colours, the map opens, no script errors.

- **Main-page flows.** The ticker box's list waits for typing; the hover preview shows what's inside and
  the Best Match; ticking one row makes "Tick one more to combine" glow, Delete solid red, and only that
  row's line lit; Delete removes it and Undo brings it back; Combine makes a portfolio with its name box
  open; the Columns picker hides a column in every row and shrinks the "Returns" label with it.
- **Motion.** After switching to Under the hood and back to Compare, nothing is left over from the slide,
  the window holds its size, and closing leaves nothing behind.

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
Alternatives jump in testing.

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
- Find elements fresh after anything that redraws the table; a held element goes stale and the click
  silently misses. The tick bar ignores clicks for 0.6s after it appears, so wait before pressing its buttons.
