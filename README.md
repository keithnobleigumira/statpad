# Statpad

Football predictions from a goals model blended with live bookmaker prices. Statpad covers 38 leagues, plus the UEFA competitions and domestic cups when an Odds API key is added. For each match it gives probabilities for every market, value against the best available price, and slips built to a target price.

The site is a single page (`index.html`). A GitHub Action (`update.py`) keeps its data file (`matches.json`) fresh and publishes both to GitHub Pages.

## Files

| File | What it does |
|---|---|
| `index.html` | The website: design, prediction engine, slips, coupon. It reloads `matches.json` every minute. |
| `update.py` | Data pipeline. It downloads results, fixtures and prices, fits team ratings and writes `matches.json`. |
| `.github/workflows/update.yml` | Runs `update.py` every 10 minutes and redeploys the site whenever the data changes. |
| `matches.json` | Starter data (5–9 Nov 2026). It is replaced on the first run. |

## Launch it (works from a phone browser)

1. **Create the repository.** On github.com tap **+ → New repository**. Name it `statpad`, choose **Public**, then tap **Create repository**.
2. **Upload the site files.** On the new repo page, tap **uploading an existing file**. Pick `index.html`, `update.py`, `matches.json` and `README.md`, then tap **Commit changes**.
3. **Turn on Pages.** Go to **Settings → Pages**. Under *Build and deployment → Source*, choose **GitHub Actions**.
4. **(Optional, recommended) Add the odds key.** Get a free key at the-odds-api.com. Then go to **Settings → Secrets and variables → Actions → New repository secret**, use the name `ODDS_API_KEY` and paste the key as the value.
5. **Add the workflow.** Go to **Code → Add file → Create new file**. Type the name `.github/workflows/update.yml` (each `/` creates a folder), paste in the contents of `update.yml` and tap **Commit changes**. This starts the first run.
6. **Open the site.** Under **Actions → Statpad update**, wait for the green tick (about a minute). Your site is at `https://<your-username>.github.io/statpad/`.

On a computer you can unzip `statpad.zip` and drag every file, including the `.github` folder, onto the upload page in one go. Do step 3 first.

## Where the data comes from

- **football-data.co.uk** (free, no key). Results for 34 leagues, which drive the tables and team ratings. Also fixtures with average and best 1X2 and over/under 2.5 prices, which the site updates about twice a week.
- **The Odds API** (optional key). Fixtures for leagues, UEFA competitions and cups come from a free listing that doesn't use credits. Prices from many bookmakers are paced to your monthly credit allowance. The free plan has 500 credits a month, which works out at roughly one price refresh per competition per day. A paid plan refreshes as often as `ODDS_MIN_INTERVAL` allows.

Without a key, the site still works from the model and the football-data prices. Saudi, Czech, Serbian and Eerste Divisie matches, and the UEFA and cup matches, need the key.

## Settings (optional)

Add these under **Settings → Secrets and variables → Actions → Variables**:

| Variable | Default | Meaning |
|---|---|---|
| `ODDS_REGIONS` | `eu` | Bookmaker regions (`eu`, `uk`, `us`, `au`). Each one multiplies the credit cost. |
| `ODDS_MARKETS` | `h2h` | Use `h2h,totals` to add over/under 2.5 prices. This doubles the cost. |
| `ODDS_MIN_INTERVAL` | `30` | Minimum minutes between price refreshes per competition. |
| `ODDS_MONTHLY_CREDITS` | `500` | Your plan's credits, used for pacing. |
| `ODDS_RESET_DAY` | `1` | Day of the month your credits reset. |
| `ODDS_SPORTS` | `mapped` | `all` follows every football competition the API lists. |

To refresh everything at once, go to **Actions → Statpad update → Run workflow** and tick *force*.

## Good to know

- GitHub runs scheduled jobs every 10 minutes at best, and often later at busy times. The page itself checks for new data every minute.
- GitHub pauses scheduled workflows in a public repo after 60 days with no commits. Any commit, or a manual run, starts them again.
- The run log (**Actions → latest run → Fetch data and fit the model**) lists every source, every competition key the API didn't list, and how many credits are left.
- Team names are matched across sources automatically. If a fixture shows "No prediction yet", the names didn't match or there is no data for that team. Add an entry to `ALIAS` in `update.py`.

Statpad gives probabilities, not guarantees. 18+ only. Stake small, and only what you can afford to lose.
