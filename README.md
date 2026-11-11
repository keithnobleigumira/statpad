# Statpad

Football predictions from a goals model blended with live bookmaker prices. Statpad covers 38 leagues, plus the UEFA competitions and domestic cups when an Odds API key is added. For each match it gives probabilities for every market, value against the best available price, and slips built to a target price.

The site is a single page (`index.html`). A GitHub Action (`update.py`) keeps its data file (`matches.json`) fresh and publishes both to GitHub Pages.

## Files

| File | What it does |
|---|---|
| `index.html` | The website: design, prediction engine, slips, coupon. It reloads `matches.json` every minute. |
| `update.py` | Data pipeline. It downloads results, fixtures and prices, fits team ratings and writes `matches.json`. |
| `.github/workflows/update.yml` | Runs `update.py` every 5 minutes and redeploys the site whenever the data changes. |
| `matches.json` | Starter data (5–9 Nov 2026). It is replaced on the first run. |
| `track.json` | The track record. The workflow creates it and commits it; don't edit it by hand. |
| `sw.js`, `manifest.webmanifest` | Let people install Statpad as a phone app and open it offline. |
| App icons | Drawn by the workflow each time it publishes, so there's nothing to upload. |
| `og.jpg` | The picture shown when the link is shared on WhatsApp, Telegram or X. |

## Launch it (works from a phone browser)

1. **Create the repository.** On github.com tap **+ → New repository**. Name it `statpad`, choose **Public**, then tap **Create repository**.
2. **Upload the site files.** On the new repo page, tap **uploading an existing file**. Pick `index.html`, `update.py`, `matches.json`, `README.md` and the app files (`sw.js`, `manifest.webmanifest` and `og.jpg`), then tap **Commit changes**.
3. **Turn on Pages.** Go to **Settings → Pages**. Under *Build and deployment → Source*, choose **GitHub Actions**.
4. **(Optional, recommended) Add the odds key.** Get a free key at the-odds-api.com. Then go to **Settings → Secrets and variables → Actions → New repository secret**, use the name `ODDS_API_KEY` and paste the key as the value.
5. **Add the workflow.** Go to **Code → Add file → Create new file**. Type the name `.github/workflows/update.yml` (each `/` creates a folder), paste in the contents of `update.yml` and tap **Commit changes**. This starts the first run.
6. **Open the site.** Under **Actions → Statpad update**, wait for the green tick (about a minute). Your site is at `https://<your-username>.github.io/statpad/`.

On a computer you can unzip `statpad.zip` and drag every file, including the `.github` folder, onto the upload page in one go. Do step 3 first.

## Track record

Every run saves each match's top pick before kick-off, along with the 10-odds slips for each day. The slips are frozen once their first game starts. When results arrive, the picks are graded and the **Track** tab shows:

- hit rates for each tier against the chance Statpad stated
- the slips' record and profit, at 1 unit per slip
- a calibration chart
- closing-line value
- whether Statpad's 1X2 probabilities beat the bookmakers' consensus

The record uses the same engine as the page, run with Node from `index.html`.

**Results speed.** With an API-Football key, finished games are graded within a couple of hours: while games are waiting for a result, the workflow re-reads that day's results (at most hourly for today), and postponed games leave the queue straight away. This costs a few calls a day, and team-news line-ups always leave 20 calls spare for results. Without the key, results come from football-data.co.uk, which publishes them twice a week. The site also gets a `results.json` with the last three weeks' final scores, which **My bets** uses.

**Self-tuning.** Once 150 top picks have been graded, every update compares the chances Statpad stated with what actually landed over the last 90 days, and fits a small correction. If Statpad has been over-confident, its stated chances, tiers and slips are trimmed; if under-confident, they're raised. The fit uses the chances as stated before any correction, so it never feeds on itself, and it stays close to "no change" until the evidence is strong. The Track tab shows its progress and the current correction. Results for UEFA, cup and Saudi games come from The Odds API and are only fetched on paid plans.

## My bets

The **Bets** tab has two parts: **Coupon** (build a bet) and **My bets** (bets you placed).

- **Save a bet.** In the coupon, tap **Save to My bets**. Enter your bookmaker's total odds, your stake (quick buttons for 1,000 to 20,000 UGX), the bookmaker, and optionally the booking code and any accumulator bonus.
- **Price check.** Before you save, it shows Statpad's chance of the bet landing, its fair odds, the odds you need to break even after the tax on winnings, what you'd collect, and what the bet is worth on average. It also gives a verdict: good value, about fair or poor price.
- **Settling.** Bets settle themselves from `results.json`, using the 90-minute score. A postponed leg counts at odds 1. You can also mark a bet won, lost, void or cashed out (with the amount) by hand.
- **Your record.** It shows profit or loss, total staked, return on stakes and win rate, broken down by slip size, bookmaker and Statpad slips vs your own picks. A "Luck or choice?" panel compares your actual result with what Statpad's chances expected, and how your odds compare with fair odds.
- **Budget and settings.** You can set a weekly or monthly budget, the tax rate (15% by default, Uganda's withholding tax on net winnings from July 2026) and the currency (UGX).
- **Private.** Saved bets live only on that phone or browser. Use **Back up** to save a file, and **Restore** to load it on another device.

## App and sharing

- **Install.** On Android (Chrome), tap the download icon in the header to install Statpad like an app. On iPhone, open the site in Safari, tap **Share**, then **Add to Home Screen**; the site shows this tip when you tap the same icon. Once installed it opens full screen, and it still opens with the last data when you're offline.
- **Share a slip.** Every slip, and the slip at the top of the page, has a share button. It makes a branded picture of the slip and opens your phone's share menu, so you can send it to WhatsApp, Telegram or your status. On a computer it saves the picture instead.
- **Link previews.** When the site's link is shared, apps show the Statpad card (`og.jpg`). The workflow fills in the site's full address when it publishes. Apps keep old previews for a while, so a change can take a day to show.

## Where the data comes from

- **football-data.co.uk** (free, no key). Results for 34 leagues, which drive the tables and team ratings. Also fixtures with average and best 1X2 and over/under 2.5 prices, which the site updates about twice a week.
- **openfootball** (free, no key, on GitHub). Full-season fixture lists for the big leagues, so the coming week shows up before football-data's weekly file arrives.
- **International results** (free, no key, on GitHub: `martj42/international_results`). Every national-team result, used to rate teams for the UEFA Nations League and international friendlies and to grade those games. The maintainer updates it after each international window, so those grades can take a few weeks.
- **The Odds API** (optional key). Fixtures for leagues, UEFA competitions and cups come from a free listing that doesn't use credits. Prices from many bookmakers are paced to your monthly credit allowance. The free plan has 500 credits a month, which works out at roughly one price refresh per competition per day. A paid plan refreshes as often as `ODDS_MIN_INTERVAL` allows.

- **API-Football** (optional key, free plan: 100 calls a day). Team news: injuries, suspensions and doubts for today and tomorrow, plus confirmed starting line-ups from about an hour before kick-off. The calls are rationed to fit the free plan. If your plan doesn't cover the current season, the site says so under **Method → Data status** and pauses team news for a day at a time, using only 1 call.

Without a key, the site still works from the model and the football-data prices. Saudi, Czech, Serbian and Eerste Divisie matches, the UEFA and cup matches, and the Nations League and friendlies all need the key, because their fixtures come from The Odds API.

## Settings (optional)

Add these under **Settings → Secrets and variables → Actions → Variables**:

| Variable | Default | Meaning |
|---|---|---|
| `ODDS_REGIONS` | `eu` | Bookmaker regions (`eu`, `uk`, `us`, `au`). Each one multiplies the credit cost. |
| `ODDS_MARKETS` | `h2h` | Use `h2h,totals` to add over/under 2.5 prices. This doubles the cost. |
| `ODDS_MIN_INTERVAL` | `5` | Fastest refresh per competition, in minutes. Your credits decide the real pace. |
| `ODDS_MONTHLY_CREDITS` | `500` | Your plan's credits, used for pacing. |
| `ODDS_RESET_DAY` | `1` | Day of the month your credits reset. |
| `ODDS_SPORTS` | `mapped` | `all` follows every football competition the API lists. |
| `STATPAD_TIMEZONE` | `UTC` | Time zone used to split the recorded slips into days, e.g. `Europe/London`. |

Secrets (**Settings → Secrets and variables → Actions → Secrets**):

| Secret | Meaning |
|---|---|
| `ODDS_API_KEY` | Your The Odds API key, for live prices. |
| `API_FOOTBALL_KEY` | Your API-Football key, for team news. It also needs the line `API_FOOTBALL_KEY: ${{ secrets.API_FOOTBALL_KEY }}` in `update.yml` (already there in the current version). |

`API_FOOTBALL_DAILY` (variable, default `100`) sets the daily call limit if you're on a bigger API-Football plan.

To refresh everything at once, go to **Actions → Statpad update → Run workflow** and tick *force*.

## Good to know

- GitHub runs scheduled jobs every 5 minutes at best, and often later at busy times. The page itself checks for new data every minute.
- Prices are paced to your plan. Credits are split across the month by how many games each day has, and the games kicking off soonest refresh first. On a busy Saturday with about 30 leagues playing, the free plan refreshes each league's prices roughly once. The 20K plan refreshes them about every 20 minutes before kick-off, and the 100K plan about every 5 minutes.
- GitHub pauses scheduled workflows in a repo that has had no commits for 60 days. The workflow avoids this by committing `track.json` at least every 20 days. That needs the `contents: write` permission at the top of `update.yml`.
- The run log (**Actions → latest run → Fetch data and fit the model**) lists every source, every competition key the API didn't list, and how many credits are left.
- Team names are matched across sources automatically. If a fixture shows "No prediction yet", the names didn't match or there is no data for that team. Add an entry to `ALIAS` in `update.py`.

Statpad gives probabilities, not guarantees. 18+ only. Stake small, and only what you can afford to lose.
