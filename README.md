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
| `backtest.json`, `results.json`, `live.json`, `teams/` | Made by the workflow on each publish (last season replayed with the tuning; recent final scores; live scores; one file per league for team pages and the season simulator). Nothing to upload. |
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

**Last season, replayed.** Once a week the workflow replays the last full season of the main European leagues that football-data.co.uk covers (with the season before it for ratings), through the same engine. Each week's team ratings are rebuilt from earlier games only, every game is priced from the bookmakers' average odds before kick-off, the first four weeks are skipped while ratings settle, and self-tuning is off. The **Track** tab shows the result as **Last season, replayed**: hit rate against the stated chance for each tier, 10-odds slips, value bets at the best price, and log loss against the market. It appears from the first run, so the Track tab is useful before the live record has grown. It takes a few seconds and uses no API credits.

**Tuning.** Once a month (and on the first run) the workflow tunes the model's settings on football-data's main European leagues: how fast old games fade, how much last season counts, how hard teams are pulled towards average, how much shots on target count next to goals, and the bookmakers' share of the blend. It searches on two past seasons, then checks the result on the last full season, which the search never saw. New settings are switched on only if they beat the old ones there. Leagues without shot counts get their own goals-only settings. The run takes one to three minutes once a month and uses no API credits. The Track tab shows the settings before and after, and the error on the check season.

**Shots on target.** Where football-data has shot counts, each side's goals are mixed with its shots on target (times the league's goals per shot on target) before the ratings are fitted. Shots are steadier than goals, so ratings react to how well a team plays, not just how lucky its finishing was.

**Safer tiers.** Every chance now comes with a spread, from how much evidence is behind the ratings and how many bookmakers price the game. Tiers and slips use the cautious chance: the stated chance minus one standard deviation. A pick that rests on thin data drops a tier, so **Nearly definite** means more often what it says. The replay shows how the top tier did with and without this rule.

## Check a slip

Under **Bets → Check slip**, tap **Screenshot or photo** and pick a screenshot of a slip (or take a photo of a printed one), or paste a slip's text from WhatsApp, Telegram or a tipster. Statpad finds each game among its fixtures for the next few days (nicknames like Man Utd, Barca, Spurs or Gladbach work) and reads the pick and odds: 1, X, 2, 1X, X2, 12, over/under, GG/NG, draw no bet, ±1.5 handicaps, win to nil and same-game combos such as "Arsenal win & over 1.5". The pick can sit on the same line as the game or the next one, and a "Total odds" line is read too.

It shows each leg's real chance, fair odds and whether its price is value or short, then the whole slip: the chance it lands, its fair odds, the odds needed to break even after tax, the average result per 1,000 staked, and the weakest leg with what dropping it would do. Picks it can't read, and markets it doesn't price (half-time, corners, cards, scorers), are left for you to choose from a list. **Put on my coupon** moves the legs to your coupon, from where you can save the bet.

**Pictures** are read on the phone itself by Tesseract, an open-source text reader, so the picture is never uploaded. Its files (about 4.5 MB) download from the jsDelivr CDN the first time and are cached after that. Before reading, Statpad turns the picture grey, flips dark-mode screenshots, evens out the light in photos and sizes it for the reader. It reads rows left to right, so a price stays with its pick, and it handles slips that print the pick before the game as well as after it. The text it read appears in the box: if a team name or price was misread, fix it there and tap **Check slip** again. Screenshots read almost perfectly; photos work best taken straight on, in good light, with the phone held upright.

## Live scores and cash-out

With the API-Football key, the workflow checks the live scores of Statpad's games while they're being played. One call covers every live game; at most 40 checks a day are spread over the hours games are in play (about every 10–20 minutes on a busy day), and line-ups and results always get their calls first. The scores go into `live.json`, and match cards show them.

For each open bet in **My bets** with a game in play, Statpad shows each leg's chance now, the chance the whole bet lands, and what the bet is worth on average. It plays out the rest of each game from the score, the minute and both teams' expected goals. Type your bookmaker's **cash-out offer** and it tells you whether the offer is above or below that value, alongside the plain odds of holding. Without live scores (no key, or between checks), type the score and minute yourself. It counts red cards when the live feed lists them, but doesn't know about injuries, so treat it as a guide.

## Live win probability

While a game is being played, its card's bar shows the chances now, and the match page draws the whole game as a chart: home, draw and away chances minute by minute, with goals and red cards marked. Each point plays out the rest of the game from the score, the time left and both teams' expected goals; a team down to ten men scores less and its opponent more. With live scores from API-Football, goal and card times are exact when the feed lists the events; otherwise a new goal is placed halfway between two score checks, and the chart says so. Finished games keep their chart for about half an hour after the final whistle. The cash-out adviser uses red cards too.

## Scoreline explorer

Every match page has a scoreline grid you can play with. Tap scores to make your own set ("1–0, 2–0 or 2–1"), or pick conditions: result, total goals, both teams to score, winning margin, and each team's goals. Statpad adds up the matching scorelines from the whole grid, so the links between the parts are priced in, and shows the chance, the fair odds and a typical bet-builder price. Add it to the coupon and save it to My bets, where it settles itself like any other pick.

## Why this pick

Each match page opens with the story behind the top pick: the teams' attack and defence ranks on Statpad's ratings, recent form, expected goals, what the bookmakers and Statpad's own model each say, how the price has moved, team news, and any reason for caution (thin data, disagreement, old prices).

## Market movers

**Value → Market movers** lists the games whose bookmaker prices moved most since Statpad first saw them, measured as the change in the margin-free chance (not just the odds). A shortening price usually means money is coming in, often from sharp bettors; a drifting one the opposite. Each move shows a small price chart, the move in the last six hours, whether Statpad's own model agrees, team news, and whether a value price is still available. Tap one to open the match.

## Season simulator

**Tables → Season simulator** plays the rest of the league season 5,000 times on the phone, in about a quarter of a second. Games that already have bookmaker prices use Statpad's blended expected goals; the rest use the team ratings, and each simulated season nudges every team's strength a little, because ratings are never exact. You get each team's average final points, its chance of the title (or promotion places in second divisions), the top places and the bottom places, and a strip showing every finishing position.

**What if…** lets you set results for the next games (1, X or 2). The season is replayed with those results fixed, and changes from the normal forecast are marked ▲▼. Clear them to go back.

Teams level on points are split on goal difference, then goals scored. Head-to-head records, points deductions and play-offs aren't modelled. It covers leagues where every team plays every other home and away (up to 23 of them), not leagues with split seasons or cups.

## Team pages

Tap a team name in a table, the simulator or a match page. A team page shows:

- points against **expected points**: the chances Statpad gave before each game, added up. A big gap means a team is doing better or worse than its play suggests, which tends not to last;
- goals and shots on target for and against, and goals per shot on target compared with the league;
- attack and defence ratings through the season, as a chart;
- home and away records, the season outlook from the simulator, and the next games;
- every result with the chance Statpad gave it beforehand. These are worked out again from the ratings the teams had before each game, so they aren't hindsight.

The workflow writes one file per league to `teams/` with the results, every 6 hours.

## Listen mode

Tap **Listen** and Statpad reads out the day's briefing (the safest picks, the best value, the slips), the picks in a slip, a match page's story, or a team page. It uses the phone's own text-to-speech voice, so nothing is downloaded; pick the voice and the speed in the player at the bottom of the screen. English only.

## What's at stake

Every hour the workflow plays the rest of each league season 10,000 times (the season simulator's method, seeded so the same inputs give the same answer) and measures how far each coming game moves both teams' chances of the title, the top places or the bottom places, after a win, a draw or a defeat. The **stakes** number adds both teams' swings between winning and losing, in percentage points: 45+ is Huge, 25+ High, 10+ Some.

- Match cards show **High stakes** and **Huge stakes** badges, and the Matches tab opens with **Most at stake** and can sort by it.
- Each match page has a **What's at stake** card: for both teams, the chance that moves most, after a win, a draw and a defeat.
- The simulator's What-if list and team pages' next games show it too, and the spoken briefing names the game with most at stake.

It covers leagues where every team plays every other home and away.

## Fatigue and motivation

Once a month, alongside the other tuning, Statpad tests:

- **Rest**: a side with only two or three days since its last league game, against one with more.
- **Nothing to play for**: from the season simulation run week by week over past seasons, a side whose season chances barely move whatever the result.
- **Open games**: games where neither side has anything left to play for.

Each is fitted on two past seasons after blending with the market as the site does, then checked on the last full season, which the fit never saw. It goes on only if it lowers the error there too; the Track tab shows which are on, and a match page says when one applies. football-data.co.uk lists league games only, so cup and European games don't count towards rest. The first run after this update does the fit, so it takes a few minutes longer than usual.

## WhatsApp Status pack

**Status pack** (Matches and Slips tabs) draws four 1080×1920 pictures for the day: the safest pick, an upset alert (the underdog at a real price of 2.60 or more with the best value, and how often it should land), the game with most at stake, and the slip at your target odds with the best chance. **Share all 4** opens the phone's share sheet; pick WhatsApp, then My status. On a computer the pictures download instead. Each carries the site's address and an 18+ note.

## Ask Statpad

Tap the microphone in the header, or **Ask Statpad** on the Matches tab, and speak straight away (it starts listening), or type. English or Kiswahili. It listens until you've finished: pauses mid-sentence are fine, and it sends your question once you've been quiet for 3 seconds, or when you tap ✓. In the Ask panel you can make that 5 seconds, or only when you tap ✓.

- "Who wins Arsenal v Chelsea?" / "Nani atashinda Arsenal dhidi ya Chelsea?"
- "Safest picks today", "Give me a 5 odds slip", "Best value", "Biggest games"
- "Who will win the Premier League?", "Who gets relegated from La Liga?"
- "How are Arsenal doing?", "What's the score in the Arsenal game?", "How are my bets doing?"

The phone's browser turns speech into text (Chrome on Android, Safari on iPhone); everything else runs on the page, from the same numbers. Spoken questions are answered aloud (typed ones too, if you tick the box), and answers come with buttons to open the match, add the pick or the slip to the coupon, or open the simulator. Follow-ups work: "and tomorrow?", "what about Serie A?". You can name a market ("over 2.5 tips", "both teams to score today"), and names a little off ("Chelsi", "Liverpul") are still found.

If the microphone does nothing: allow it for the site (tap the icon left of the address, then Permissions), and check you're online, since the phone needs the internet to turn speech into text. Inside the claude.ai preview the microphone is blocked; it works on your GitHub Pages site. Where a browser has no voice input, type instead.

## Combos, draw no bet and handicaps

Each match page has a **Combos, draw no bet & handicaps** section with:

- **Same-game combos.** Win and over 1.5 or 2.5, win and both teams score, win and under 3.5, win to nil, double chance and under 3.5 or both teams score, both teams score and over 2.5, and draw and under 2.5.
- **Draw no bet.** Stake back on a draw; the chance shown is of winning when there's a winner.
- **Handicaps ±1.5.** For example "Arsenal −1.5" (wins by 2+) and "Sunderland +1.5" (doesn't lose by 2+).

They come from the same scoreline grid as every other market, so the link between the two parts of a combo is priced in, rather than multiplying two chances as if they were unrelated. Prices are estimates with a typical bet-builder margin: if your bookmaker offers less than the fair odds, it's poor value. You can add them to the coupon and save them to **My bets**, which settles them. They never appear in the top picks, the slips or the Value board.

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

- **API-Football** (optional key, free plan: 100 calls a day). Team news: injuries, suspensions and doubts for today and tomorrow, plus confirmed starting line-ups from about an hour before kick-off. Also fast results and live scores for the cash-out adviser. The calls are rationed to fit the free plan, which only serves fixtures and results for yesterday, today and tomorrow; anything older comes from football-data. If your plan doesn't cover the current season at all, the site says so under **Method → Data status** and pauses team news for a day at a time, using only 1 call.

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
