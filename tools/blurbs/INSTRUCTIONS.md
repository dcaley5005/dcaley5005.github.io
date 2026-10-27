# Writing ETF descriptions for Portfolio Lab (style v2)

You write short plain-English descriptions of ETFs for a personal investing website.

Input: `todo/facts_NN.json`. A JSON list of up to 50 ETFs with: t (ticker), name, category, issuer,
top_holdings_pct, sectors_pct (industry mix), asset_mix_pct, fund_description (prospectus text, often dense).

Output: write `{"TICKER": "description", ...}` for every ticker to `todo/blurbs_NN.json`. Then check with
python3: parses, every ticker present, 2 sentences each, 30 to 55 words, no em dashes or en dashes.

## The three things each description says
1. What it holds, described by industries, sectors or types of assets (use sectors_pct and asset_mix_pct, rounded:
   "technology is about a third"). Never list the top companies by name. Never use top holding percentages.
2. Its purpose: the role it plays in a portfolio (a core holding, growth, income, a cushion against drops, a
   focused bet on one industry, a hedge, a short-term trade).
3. One thing that sets it apart: "no banks", "trails in strong rallies", "drops when interest rates rise",
   "resets daily so it is for short holding periods", "hedges the currency", "swings more than the broad market".

## Rules
- Exactly 2 sentences, 30 to 55 words. Casual, clear, concrete. Explain unavoidable terms in a few words.
- Do NOT restate the obvious: don't open with or repeat the fund's name, and don't say "tracks the X index".
  Mentioning an index is fine only when the relationship helps, e.g. "the technology slice of the S&P 500".
- Exception: a single-stock fund or a fund built around a short named list (like the Magnificent Seven) may name
  that stock or list, since that IS what it holds.
- No em dashes or en dashes. No hype, no advice ("you should"), no past returns, fees, fund size or launch dates.
- Buffer, floor or cap funds: never state a percentage unless it is in the fund's name or facts.
- Never mention problems with our data. Ignore cash, Treasury bills or swaps that are clearly just collateral.
- Only state facts from the file or well-known facts about the fund. If unsure, stay general.

## Approved examples (use these exact texts for these tickers if they are in your file)
SPY: "America's largest companies in one fund, covering every major industry. Technology is the biggest slice at about a third, followed by financials, health care and consumer companies, so it's built to be a core holding that moves with the overall US economy."
XLK: "The technology slice of the S&P 500: software makers, chip companies, and computer and networking hardware. It's a focused bet on the tech sector, so it swings more than the broad market, and internet and social media companies are classed in other sectors, so they aren't in it."
QQQ: "The 100 largest non-financial companies on the Nasdaq, led by technology, with internet, consumer brands and health care making up most of the rest. It's built for growth: there are no banks and far more tech than the broad market, so it tends to rise and fall harder."
JEPI: "Large US companies picked for steadier, lower-swing stocks across many industries, paired with an options strategy that trades some of the market's upside for monthly income. The goal is income and a smoother ride: it usually falls less in sell-offs but trails in strong rallies."
BND: "Nearly the whole US investment-grade bond market in one fund: Treasuries, government-backed mortgage bonds and corporate bonds. It's meant for steady interest income and as a cushion for a stock portfolio, though its price drops when interest rates rise."

## When done
Reply with one line: the count written, plus any tickers you were unsure about.
