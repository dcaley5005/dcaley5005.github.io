# danielcaley.com

Source for danielcaley.com, served by GitHub Pages.

- `site/` is the website. `site/portfolio-lab/index.html` is the Portfolio Lab app.
- `scripts/build_data.py` downloads prices from Yahoo Finance and writes them next to the app.
- `.github/workflows/refresh.yml` runs the script Tuesday to Saturday at about 5am Pacific and deploys the site. It also redeploys when anything in `site/` changes. Run it by hand from the Actions tab with "Run workflow".
- `data/universe.json` is the ETF list: every US-listed ETF except leveraged and inverse funds (about 4,900), ranked by 3-month dollar volume. It refreshes every Saturday, which also picks up new funds. Tick "Re-rank the ETF list now" when running by hand to refresh it sooner. To keep only the top N, set the `UNIVERSE_SIZE` environment variable (0 means all).

Price data is not stored in the repo. Each run downloads the full history and publishes it with the site. If a download fails, the previous day's file from the live site is kept, and if too many fail the deploy is skipped so the site keeps yesterday's data.
