# Statpad

Football predictions from a goals model blended with live bookmaker prices. Statpad covers 44 men's leagues and six women's competitions (five leagues and the Women's Champions League), plus the UEFA and South American club competitions, domestic cups and national teams. For each match it gives probabilities for every market, value against the best available price, and slips built to a target price.

The site is a single page (`index.html`). A GitHub Action (`update.py`) keeps its data file (`matches.json`) fresh and publishes both to GitHub Pages.

## Files

| File | What it does |
|---|---|
| `index.html` | The website: design, prediction engine, slips, coupon. It reloads `matches.json` every minute. |
| `update.py` | Data pipeline. It downloads results, fixtures and prices, fits team ratings and writes `matches.json`. |
| `.github/workflows/update.yml` | Runs `update.py` every 5 minutes and redeploys the site whenever the data changes. |
| `matches.json` | Starter data (5–9 Nov 2026). It is replaced on the first run. |
| `track.json` | The track record. The workflow creates it and commits it; don't edit it by hand. |
| `ledger/` | Every graded prediction and value bet, one CSV file per month. The workflow creates and commits it; rows are only ever added. GitHub shows each file as a table. |
| `backtest.json`, `results.json`, `live.json`, `miracles.json`, `teams/` | Made by the workflow on each publish (last season replayed with the tuning; recent final scores; live scores; the latest weekend's rarest results; one file per league for team pages and the season simulator). Nothing to upload. |
| `sw.js`, `manifest.webmanifest` | Let people install Statpad as a phone app and open it offline. `sw.js` also keeps crests and flags on the phone after the first download. |
| `crests/` | Club crests and competition logos: one sheet per competition in `crests/s/`, big single crests for match pages. The workflow makes them on each publish; nothing to upload. |
| `globe/` | The shock map's earth pictures. The workflow fetches and resizes them once; nothing to upload. |
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

## Performance lab (track record)

Every run saves each match's top pick before kick-off, along with the 10-odds slips for each day, and every bet the Value board shows. The slips are frozen once their first game starts. When results arrive, everything is graded and the **Lab** tab shows it in three parts.

**Value bets: do they pay?** Each value bet is taken once, 1 unit at the best price the moment the board first shows it. The Lab shows the return per unit with its 95% range (over a few hundred bets luck alone moves the return by several percent either way, so the range says whether a result means anything yet), profit, closing-line value (the price taken against the bookmakers' fair price at kick-off; beating it is the quickest sign of a real edge), the worst run from a peak, results by edge, the bets waiting or in play with their chance now, and the latest settled. Beside it is the same rule run on the last five full seasons of football-data.co.uk's main leagues (about 5,000 bets, recomputed weekly): **+5.0% a unit, 95% range +1.1% to +8.9%, closing price beaten 71% of the time, worst run −41 units**, positive in each of the five seasons. That uses the best price football-data lists; in practice the best price often comes from a bookmaker that limits winning accounts, so treat it as an upper bound.

**Closing-line value, with its own verdict.** Profit needs thousands of bets to separate an edge from luck (a 2% edge at even money needs roughly 7,000–10,000); closing-line value needs a few hundred, because each bet's gap to the closing price varies far less than its result. So the Lab gives closing-line value its own 95% range and verdict: *beating the closing price by more than luck explains* when the whole range is above zero. On the five past seasons it is **+3.1% a bet, 95% range +2.9% to +3.4%**. Records saved before v55 start counting the range from the update.

**Bankroll stress test.** Under the five-season replay, the same value bets are staked four ways from a bank of 1 (flat 1% of the bank, quarter, half and full Kelly; Kelly stakes the edge divided by the price less one) on the real seasons in order and on 2,000 made-up seasons, each 180 match days drawn at random from the real ones, so runs of bad luck that never happened in that order are counted too. No bet goes above its cap (1%, 5%, 10%, 25%) and no day above half the bank. On the five past seasons:

| Staking | Typical season | Bad season (1 in 10) | Losing season | Bank halves at some point |
|---|---|---|---|---|
| Flat 1% | ×1.49 | ×0.83 | 18% | 4% |
| Quarter Kelly | ×1.44 | ×1.03 | 8% | 0% |
| Half Kelly | ×1.92 | ×1.00 | 10% | 6% |
| Full Kelly | ×3.03 | ×0.87 | 12% | 56% |

Quarter Kelly grows almost as fast as flat staking with half the falls; full Kelly looks best on a typical season but halves the bank in more than half of them, because the edges it stakes on are estimates. These are upper bounds: they use the best price across dozens of bookmakers before any of them limits the account.

**Research log.** Lab → Accuracy lists the ideas tested for this version on games the model never saw, kept or off, with the numbers (log loss is home, draw and away plus over/under 2.5 a game, lower is better):

- **Goals from the market's memory where there's no over/under price: kept** (above).
- **Memory spread by recent data: kept** (above).
- **Memory moving supremacy in priced games, Pinnacle-first memory, a home advantage per side: off** (above).
- **Price outlook (bet now or wait): kept** (above).
- **Value measured against the forecast closing price: off.** Value bets that also held up against the forecast close beat the close by 5.7%, the rest by −0.7%. But moving a price all the way to its forecast close didn't predict results better (half way: +0.00025 a game; all the way: +0.00026, and ≤ 0 in 2023–27), and those bets lost money in 2023–27. A close you can see coming is not the same as value, so the forecast changes timing only.
- **Playing well beyond the price, for predictions: off.** Shots, shots on target and corners beyond what each side's prices expected didn't predict goals beyond the next early price (−0.00003 a game on 8,421 unseen games). They do move prices, which is what the outlook uses.
- **The memory's over 2.5 chance as a target in the price fit: off.** The first way tried of bringing the memory's goals into games with no over/under price (fit the price to the memory's over 2.5 chance, then blend Statpad's model as before) was within luck (1.6851 → 1.6846 on 2025/26). Blending the goals level directly, without the model, is what works (kept, above). Over 2023/24–2026/27 a real over/under price is worth 0.0016 a game over 1X2 alone; with the memory's goals level the gap is 0.0004.
- **Market memory: kept** (above).
- **Market memory blended into priced games: off.** Weight 0.05, gain 0.00003 a game.
- **Price momentum: off.** Whether the move from the early price to the closing price carries on past the close: on 8,839 unseen games, gain +0.00001 (95% range −0.0003 to +0.0003). The market finishes its moves.
- **Overreaction to luck: off.** Sides that recently scored more than their prices expected are very slightly overpriced next time (−0.06 per goal of luck), but the gain was within luck (+0.00015, 95% range −0.0004 to +0.0007).
- **Market memory as a value-bet filter: off.** Value bets the memory agreed with had more closing-line value in 2021–24 (+5.1% against +2.5%) but less in 2024–27 (+3.8% against +4.7%). Noted: the memory does point where prices go, the close moving about a tenth of the way from the early price towards it (correlation 0.11 on unseen seasons), too little to use.
- **Learning from the closing price: off.** Fitted to the bookmakers' closing prices instead of results, the learned blend predicted the unseen season worse on two different splits (1.6817 → 1.6829 and 1.6685 → 1.6689). The closing price is sharper than the early one (by about 0.003 a game on 1X2), but what makes it sharper, late team news and sharp money, isn't something the blend can learn from it.
- **Line-up rotation: off.** Starting line-ups for 32,731 past games in 19 leagues (from ESPN), compared with each side's usual team over its previous eight games. Against the early prices, a side missing 30% of its usual team scored about 6% fewer goals and conceded about 6% more, but the gain was within luck (+0.0005 a game, 95% range −0.0003 to +0.0013); against the closing prices it was nothing (−0.0001), because by kick-off the market has priced the line-ups in. Statpad already fetches fresh prices as soon as line-ups appear.
- **Rest days counting cup and European games: off.** ESPN's cup and European fixtures (21 competitions, 2021–2026) changed the rest days of 3,548 of 18,465 games, but the blend predicted the unseen season worse (1.6817 → 1.6820) and the rest effect it had found shrank by half.
- **Margin removal: power method kept.** On 34,232 games over five seasons, power read the results best against Shin's method, the odds-ratio method and an even split, on early and closing prices, average and Pinnacle alike (early average prices: power 0.99595, Shin 0.99608, odds ratio 0.99620, even 0.99680).
- **Quarter Kelly as the stake: kept** (see the stress test above).

**Accuracy: is Statpad right?** Log loss and Brier score on home, draw and away, for Statpad, the bookmakers' consensus and Statpad's model on its own; a calibration chart; the same numbers for each engine version (every prediction is stamped with the engine that made it, so an upgrade is judged on games it actually predicted); the weekly replay of last season; the tuning; and links to the ledger.

**Levels:**

- a separate tracker for each entry level (**Nearly definite**, **Highly possible**, **Possible**). A pick counts at the level it had at the last update before kick-off. Each tracker shows how many of its picks landed against the chance Statpad stated, units won or lost at 1 unit a pick at the listed price, the current run, the last 20 results, the level's 10-odds slips, and its picks waiting or in play with live scores (landing or not as it stands). Tap a level to switch; the phone remembers your choice
- the slips' record and profit for all three levels side by side, at 1 unit per slip
- a calibration chart
- closing-line value
- whether Statpad's 1X2 probabilities beat the bookmakers' consensus

The record uses the same engine as the page, run with Node from `index.html`.

**The ledger.** Each graded prediction and value bet is also written as one row to `ledger/YYYY-MM.csv` (month of kick-off) when the track record is saved: engine version, the first prediction and the last before kick-off, chance and cautious chance, price and bookmaker, edge, the closing fair chance, the result, profit and closing-line value, and the 1X2 chances of Statpad, the market and the model. Rows are only ever added. The workflow commits the folder along with `track.json`, and the site publishes a copy so the Lab can link to each month. **This needs the updated `update.yml`**: upload it to `.github/workflows/` (replace the old one).

**Live check.** Every graded game keeps Statpad's, the bookmakers' and the model's own home/draw/away chances. The Lab compares them on the games graded since the last tuning, by kind of league (top leagues, second tiers, other leagues, cups and the rest), with a 95% range on Statpad's error minus the bookmakers' worked out game by game. If, on 300 or more games in a kind of league, the learned blend is clearly behind the bookmakers' own prices, the workflow switches it off there until the next monthly tuning has looked again. It's the early warning for drift: the tuning tests on past seasons, this watches the games being played now.

**Model autopsy.** Each week every chance Statpad gave on the replayed season (all markets, before any correction) is grouped by kind of market and league, chance band and side. The Lab lists any group of 300 or more whose landed rate is more than 2.5 standard errors from what was stated, and whether the weekly corrections bring it back in line. Those corrections are now also fitted for each kind of market in each kind of league, held towards the market's own correction, and kept only where they predict the unseen season better. On the first run none of the 40 groups was that far off; the Lab then shows the closest calls. The live record's top picks get the same check once there are enough.

**Results speed.** Finished games are graded on the first run after the final whistle (within about 5 minutes), from ESPN's public scoreboards (no key, no daily limit; see **Live scores and cash-out**). Postponed and abandoned games leave the queue straight away. For the few competitions ESPN doesn't carry (the Swiss, Romanian, Polish, Czech, Serbian and Finnish leagues), an API-Football key grades them within a couple of hours; without it, those results come from football-data.co.uk, which publishes them twice a week. The site also gets a `results.json` with the last three weeks' final scores, which **My bets** uses.

**Self-tuning.** Once 150 top picks have been graded, every update compares the chances Statpad stated with what actually landed over the last 90 days, and fits a small correction. If Statpad has been over-confident, its stated chances, tiers and slips are trimmed; if under-confident, they're raised. The fit uses the chances as stated before any correction, so it never feeds on itself, and it stays close to "no change" until the evidence is strong. The Lab shows its progress and the current correction. Results for UEFA, cup and Saudi games come from The Odds API and are only fetched on paid plans.

**Last season, replayed.** Once a week the workflow replays the last full season of the main European leagues that football-data.co.uk covers (with the season before it for ratings), through the same engine. Each week's team ratings are rebuilt from earlier games only, every game is priced from the bookmakers' average odds before kick-off, the first four weeks are skipped while ratings settle, and self-tuning is off. The **Lab** tab shows the result as **Last season, replayed**: hit rate against the stated chance for each tier, 10-odds slips, value bets at the best price, and log loss against the market. It appears from the first run, so the Lab is useful before the live record has grown. It takes a few seconds and uses no API credits.

**Tuning.** Once a month (and on the first run) the workflow tunes the model's settings on football-data's main European leagues: how fast old games fade, how much last season counts, how hard teams are pulled towards average, how much shots on target count next to goals, how far below average promoted sides start, and the bookmakers' share of the blend. It searches on two past seasons, then checks the result on the last full season and this season's games so far, which the search never saw (so a change that helped last year but hurts now stays off). New settings are switched on only if they beat the old ones there. Leagues without shot counts get their own goals-only settings. Then come the newer stages below, one at a time and each on top of the last, each switched on only if it also lowers the error on the unseen season. The run takes two to four minutes once a month and uses no API credits. The Lab shows the settings before and after, and the error on the check season.

**Faster maths, smarter search.** The fits run as vector maths with NumPy: the same fits to the fifth decimal, about five times faster. The runner doesn't come with NumPy, so the runs that do heavy fitting (the tuning, the replay, team pages) install it into a scratch folder in a few seconds; if that ever fails they carry on in plain Python, only slower. The search is Bayesian: it fits a smooth stand-in for the error over all the settings at once from every setting tried so far, and tries next wherever the expected improvement is largest, stopping when nothing more is expected. It now tries about 160 settings in about a minute, where the old one-setting-at-a-time grid managed 47 in a minute and a half. On its own that found settings about as good as before; the time it frees runs the stages below. No change to `update.yml` is needed.

**Draw correction per league.** Every price comes from a Dixon–Coles scoreline grid, which corrects the low scores (0-0, 1-0, 0-1, 1-1) a plain Poisson model gets wrong. That correction was one value (−0.07) for every league; it is now fitted per league, on the error of the site's own numbers, and pulled towards the best value over all leagues when a league has few games. Tested on two seasons the tuning never saw, it lowered the error (results plus over/under 2.5, log loss) from 1.6826 to 1.6819 on 2025/26 and from 1.6689 to 1.6685 on 2024/25, and sharpened both teams to score; exact scores got slightly worse.

**The learned blend.** Instead of one fixed share for the bookmakers, the tuning learns, game by game, how far to lean from their numbers towards the model's or away from them, by how thin the ratings' data is, the time of season, the kind of league and short rest. It is fitted on the error itself, and its strength is chosen walk-forward (fitted on the older training season, measured on the newer one). What it found: when the model and the bookmakers disagree, leaning about a tenth of the gap further towards the bookmakers is better. Their prices move on news the ratings can't see, and move a little less than they should; it held with closing prices too. It lowered the error again, 1.6819 → 1.6810 on 2025/26 and 1.6685 → 1.6681 on 2024/25. Together with the draw correction, Statpad's error on results is now below the bookmakers' own on both unseen seasons (1.0020 → 1.0009 and 0.9942 → 0.9937), where before it matched them exactly. Short rest is part of the learned blend when it's on.

**Market memory.** Most games more than two or three days away have no bookmaker price yet, and Statpad used to price those from its goal-based ratings alone. Now it prices them from the **market's memory**: each side is rated from the expected goals that the closing prices of its recent games implied, instead of from the goals it scored. A closing price already holds everything the market knew about both sides (injuries, form, transfers, news), so a side's last few closing prices say more about it than its last few scores. The fit is log expected goals = average + home advantage + attack − defence, weighted so a game a fortnight old counts half as much as today's, gently pulled towards the league average, over the last 420 days; a side with no priced game in that window starts a little below average. The prices come from football-data's closing odds for every finished league game (average, then Pinnacle), and, for the latest days and for leagues football-data doesn't price, from the last price Statpad itself saw before each kick-off. It is refitted every hour.

On 2025/26, which it never saw:

| Prediction for each game | Log loss a game (home, draw, away plus over/under 2.5; lower is better) |
|---|---|
| Statpad's goal-based ratings | 1.7103 |
| **Market memory** | **1.6860** |
| The game's own early bookmaker price | 1.6825 |

So the memory gets 86% of the way from the ratings to the market without seeing the game's own price, and it is as well calibrated as a price (favourites it gave 55%, 64% and 78% won 55%, 66% and 79% of the time; the goal ratings were off by up to 6 points). On six extra leagues where football-data has only 1X2 closing prices (Norway, Sweden, Brazil, USA, Japan, Denmark; 7,409 games) it closed 84% of the gap. It also ages well: frozen 60 days before each game, it still beat goal ratings made the day before (1.7023 against 1.7103), so the season simulator and What's at stake use it for every game left to play. Memory-priced games show a **Market memory** badge in place of **Model only**, have no safety haircut (they don't need one), and a spread between a market's and the model's. Where a game has a real price, nothing changes: blended with the price, the memory added nothing (fitted weight 0.05, a gain of 0.00003).

**Price outlook: bet now or wait.** Prices move between the first quote and kick-off, and part of that move can be seen coming: the market drifts about a fifth of the way back towards its own memory of both sides, and towards sides that have been playing better than their prices expected (shots on target, shots, corners and goals for minus against, less what each game's closing price implied, over the last 120 days with a game a month old counting half). Statpad forecasts where each real price will be at kick-off and marks it in a game's Markets tab: ▼ forecast to shorten (take it now), ▲ forecast to drift (waiting usually pays). Value bets get a **Bet now** or **No rush** tag, and the match story says so when the top pick's price is forecast to move 1.5% or more. Fitted on 47,190 league games from 2019/20 to 2026/27 (football-data's early average price against its closing average price), each season forecast by a fit on the others:

| Forecast price change | Actual change (average) |
|---|---|
| shorter by 6% or more (average 8.6%) | 5.4% shorter |
| 3–6% shorter (4.0%) | 2.7% shorter |
| 1.5–3% shorter (2.1%) | 1.6% shorter |
| 1.5–3% longer (2.1%) | 2.2% longer |
| 3–6% longer (4.1%) | 3.9% longer |
| 6% or more longer (8.5%) | 6.3% longer |

The shown forecast is shrunk at the extremes to match (forecast − 3.5 × forecast × |forecast|). The direction was right 61.5% of the time when the forecast was 1% or more (57% of selections) and 66% at 3% or more. Where the forecast was 1% or more, following it (early when forecast to shorten, at kick-off when forecast to drift) got prices 1.2–2.0% better than always betting early and 0.5–1.2% better than always betting at kick-off, in every one of the eight seasons, for home, draw, away, over and under alike. Value bets forecast to shorten beat the closing price by 6.3% when taken at once and by 1.4% when left to kick-off; those forecast to drift did about the same either way (+1.2%). It is shown from 6 hours before kick-off, for league games with a market memory. It is timing advice only: it never changes a chance or what counts as value. Every forecast is graded live (the first one made 6 hours or more before kick-off against the last price Statpad saw before kick-off), and the record shows under each game's price history once 30 are in.

**Goals from the market's memory, where there's no over/under price.** About 42% of priced games have only home, draw and away prices, and those read the goals level from the draw price, which is weak. For them the engine keeps the price's supremacy (who is stronger) and blends the goals level with the market memory's: its share of log total goals is 50% for a price from many bookmakers and 65% for one to three (ESPN's single feed counts as one), or 35% and 60% where the league's memory was itself built from 1X2 prices alone (the extra leagues). Statpad's own model is no longer blended into these games: with the memory in, its fitted weight was 0.

| Price (no over/under) | Before | Goals level from memory | Gain a game |
|---|---|---|---|
| Bet365 alone, 2025/26, against the price + model blend | 1.68663 | 1.68157 | +0.0051 (95% +0.0031 to +0.0071) |
| Bet&Win alone, same | 1.68413 | 1.68149 | +0.0026 (+0.0011 to +0.0042) |
| average of many, same | 1.68307 | 1.68156 | +0.0015 (−0.0001 to +0.0027) |
| Bet365 alone, 2023/24–2026/27 (22,475 games) | | | +0.0045 (+0.0031 to +0.0059) |
| average of many, 2023/24–2026/27 (22,495 games) | | | +0.0012 (+0.0005 to +0.0019) |

Weights fitted on 2019/20–2022/23. Blending supremacy as well was worse (−0.0002), a price with its own over/under gained nothing (+0.00005, within luck; one bookmaker with its over/under −0.00001), and a memory built from 1X2 prices alone still helped (+0.0034 for one bookmaker, +0.0005 for the average). The match chips say when a game's goals level came from the memory.

**Memory spread by recent data.** Against the closing price, the memory's supremacy was off by 0.32 (in log goals) for sides with no priced game in the last 60 days (the start of a season, promoted sides), 0.22 with one or two and 0.15 with three or more. A memory-priced game's spread is now set per side from that (0.25, 0.17 and 0.12 in log expected goals), so its cautious chance is wider when the memory knows less. Also tested and off: Pinnacle's closing prices first in the memory (−0.00003) and a home advantage for each side (worse at every strength of pull, −0.0008 to −0.0030).

**Game-by-game ratings (tested, off for now).** A Kalman filter: each side's attack and defence drift a little every day, and each result moves them by how surprising it was, weighed by how sure they were. On its own it beat the season-long ratings on 2025/26, but next to Elo it added too little (1.7070 → 1.7063, under the bar of 0.001) and nothing on 2024/25, so the monthly check keeps it off until that changes.

**Shot quality from the commentary (xG).** ESPN's play-by-play lists every shot with its spot on the pitch for eight leagues: the Premier League, the Championship, La Liga, the Bundesliga, Serie A, Ligue 1, the Eredivisie and the Primeira Liga (elsewhere it lists only goals). A shot-quality model (distance, the angle of the goal mouth and the commentary's words: header, free kick, penalty, fast break, cross, through ball, corner) turns each shot into the chance it was a goal. It was fitted on 368,847 shots from 2021/22 to 2026/27, and on seasons it wasn't fitted on it scored 0.280 per shot (log loss) against 0.345 for knowing only how often shots go in. The workflow reads about 40 games a run, newest first, back to the seasons the tuning trains on (about 15,000 games, so expect a few days), and keeps them in `.statpad/xg.json`. Once most of those leagues' games have it, the tuning gives them their own settings with xG mixed into the goals and keeps them only if they beat the common settings on the latest season there. In the two tests so far it helped on 2024/25 (1.6647 → 1.6586) and not on 2025/26 (1.6860 → 1.6872), so it will come and go. **Method → Data status** shows how far the collection has got.

**Elo ratings in the mix.** Alongside the goals model, Statpad keeps World Football Elo ratings for every club (each result moves both teams' ratings by more for a bigger win), turns the gap between two teams into expected goals, and mixes that with the model's own expected goals. The monthly tuning fits how much the Elo gap counts and how big a share it gets on two past seasons, then checks on the season it never saw: it's switched on only if it makes the model's predictions better there and doesn't make the site's final chances worse. On the last offline check it was on, at a 20% share, and lowered the model's error from 1.7120 to 1.7091 (log loss on exact scores). **Lab → Accuracy → Tuning** shows whether it's on.

**Shots on target.** Where football-data has shot counts, each side's goals are mixed with its shots on target (times the league's goals per shot on target) before the ratings are fitted. Shots are steadier than goals, so ratings react to how well a team plays, not just how lucky its finishing was.

**Sharper chances.** Five past seasons of the main leagues (about 30,000 games with prices) were used to test how Statpad reads the bookmakers; only what predicted results better was kept:

- **Margin removed by the power method.** Bookmakers load more margin onto long shots, so dividing it out evenly understated favourites. The power method (each 1/price raised to the same power so the chances add to 1) read results better: 1X2 log loss 0.9966 against 0.9971 on average prices, and on the replay of 2025/26 Statpad's 1X2 log loss fell from 1.0035 to 1.0023.
- **Pinnacle counts three times; exchanges count once.** Pinnacle's prices were the most accurate before kick-off. Exchange prices before kick-off were the least accurate (thin markets), so they lost their extra weight.
- **Fresher prices.** Prices closer to kick-off predict better than any change of method (closing prices beat earlier ones by 0.003 log loss). DraftKings' prices from ESPN are free, so every two hours the workflow reads them for the games in the next day and a half, averages them in when The Odds API's are more than six hours old, and uses them alone for games nothing else prices.
- **Per-market correction.** Each week the replay of two past seasons checks, for each kind of market, whether stated chances came true as often as stated. A correction fitted on the older season is kept only where it predicts the newer one (which it never saw) better. On the last check that kept small corrections for home/away wins, draws, double chance and team goals: high chances had been landing a little more often than stated, and draws a little more often too. On the replay of 2025/26, **Nearly definite** top picks then landed 88.0% of the time against a stated 87.6%, and **Highly possible** 79.7% against 79.9%.
- **The stated chance is the best estimate.** Disagreement between model and market, and old prices, now make only the *cautious* chance (and so the tier) more careful, instead of lowering the chance shown. Picks that used to be cut that way landed 85% of the time against a stated 81%.
- **The market's share** of the blend can now be tuned up to 100%; the monthly tuning picks it only if it beats the current share on the season it never saw.

None of this makes football predictable: the bookmakers' consensus is already very sharp, and these are gains of a fraction of a percent per game. They are real, measured on games the changes never saw, and they add up over thousands of picks.

**Safer tiers.** Every chance now comes with a spread, from how much evidence is behind the ratings and how many bookmakers price the game. Tiers and slips use the cautious chance: the stated chance minus one standard deviation. A pick that rests on thin data drops a tier, so **Nearly definite** means more often what it says. The replay shows how the top tier did with and without this rule.

## Check a slip

Under **Slips → Check a slip**, tap **Screenshot or photo** and pick a screenshot of a slip (or take a photo of a printed one), or paste a slip's text from WhatsApp, Telegram or a tipster. Statpad finds each game among its fixtures for the next few days (nicknames like Man Utd, Barca, Spurs or Gladbach work) and reads the pick and odds: 1, X, 2, 1X, X2, 12, over/under, GG/NG, draw no bet, ±1.5 handicaps, win to nil and same-game combos such as "Arsenal win & over 1.5". The pick can sit on the same line as the game or the next one, and a "Total odds" line is read too.

It shows each leg's real chance, fair odds and whether its price is value or short, then the whole slip: the chance it lands, its fair odds, the odds needed to break even after tax, the average result per 1,000 staked, and the weakest leg with what dropping it would do. Picks it can't read, and markets it doesn't price (half-time, corners, cards, scorers), are left for you to choose from a list. **Put on my coupon** moves the legs to your coupon, from where you can save the bet.

**Pictures** are read on the phone itself by Tesseract, an open-source text reader, so the picture is never uploaded. Its files (about 4.5 MB) download from the jsDelivr CDN the first time and are cached after that. Before reading, Statpad turns the picture grey, flips dark-mode screenshots, evens out the light in photos and sizes it for the reader. It reads rows left to right, so a price stays with its pick, and it handles slips that print the pick before the game as well as after it. The text it read appears in the box: if a team name or price was misread, fix it there and tap **Check slip** again. Screenshots read almost perfectly; photos work best taken straight on, in good light, with the phone held upright.

## Slip autopsy

Lost a slip? Add it under **Slips → Check a slip** the same way (screenshot, photo or text) and Statpad examines it. It works for games from the last ten days: each leg is matched to its game in the track record, and the goal minutes come from the record or, for older games, from ESPN's scoreboard for that day. **Autopsy of a lost slip** builds one from real recent games so you can see what it looks like. If the slip's text says "Lost" (most bookmakers print it on settled slips), the checker knows its games are already played and picks those rather than the teams' next games.

- **Time of death.** A leg dies at the goal that made it impossible (the third goal for under 2.5, both teams scoring for "both teams to score: no", a goal against for win to nil), otherwise at the final whistle. The slip dies with its first dead leg: that leg is the **cause of death**, with the goal and the minute.
- **Let down by.** The leg with the highest chance before kick-off that still lost.
- **Vital signs.** A heart-monitor chart of the chance that the whole slip lands, from the first kick-off until it died, worked out again at every goal with the live model (on a log scale, 1 in 1,000 up to certain, with a beat at each goal and the hours between games cut out). It sweeps across and flatlines in red at the time of death, and the phone gives a buzz.
- **How unlucky.** How many of 10,000 weekends a slip like it lands (from the scoreline grids before kick-off, same-match legs counted together), whether the odds were fair, and, when only one leg lost, what the slip would have paid without it ("one leg from glory", after your tax setting). The verdict runs Robbed (it landed more often than not), Unlucky, Brave, Long shot and Lottery ticket (under 1 in 100).
- **Share the autopsy** makes a 1080 × 1920 picture for a status: the monitor, time and cause of death, the three numbers and the legs, with the site's link.

A slip whose legs all came in gets a short "Landed" note instead; one with games still to play is checked as usual.

## Live scores and cash-out

Live scores come from ESPN's public scoreboards, which need no key and have no daily limit. Two things read them:

- **The workflow**, on every run (every 5 minutes): each game from three hours before kick-off to the end is paired with ESPN's game (same kick-off, same clubs), its score, minute, goals and red cards go into `live.json`, and finished games go straight into `results.json` and the track record. It also takes confirmed line-ups from ESPN from about 75 minutes before kick-off. This uses one or two small requests per competition in play.
- **Your phone**, while Statpad is open: every 30 seconds for a competition where you have a bet running, every minute for the other games in play. GitHub takes a minute or two to publish each update, so this is what keeps scores, the shock map and your bets current. A finished game settles your bets straight away; the site's results file confirms them later. Each check is about 7 KB per competition. **Display → Live scores** has **Data saver** (every 1 and 3 minutes) and **Off** (the site's updates only). If ESPN can't be reached from your phone, it backs off and uses the site's updates; **Method → Data status** shows which is in use.

For the competitions ESPN doesn't carry, an API-Football key adds live scores the old way: one call covers every live game, at most 40 checks a day.

For each open bet in **My bets** with a game in play, Statpad shows each leg's chance now, the chance the whole bet lands, and what the bet is worth on average. It plays out the rest of each game from the score, the minute and both teams' expected goals. Type your bookmaker's **cash-out offer** and it tells you whether the offer is above or below that value, alongside the plain odds of holding. For a game with no live score (a competition ESPN doesn't carry), type the score and minute yourself. It counts red cards when the live feed lists them, but doesn't know about injuries, so treat it as a guide.

## Live win probability

While a game is being played, its card's bar shows the chances now, and the match page draws the whole game as a chart: home, draw and away chances minute by minute, with goals and red cards marked. Each point plays out the rest of the game from the score, the time left and both teams' expected goals; a team down to ten men scores less and its opponent more. Goal timing and the effect of the score were fitted on the half-time scores of 34,221 games (2021/22–2025/26, expected goals from each game's prices): 55% of the goals are still to come at half-time, a team a goal up scores about 6% less from there and a team a goal down about 11% more. Fitted on three seasons and checked on the next two, this read full-time results from half-time better than goal timing alone (log loss 0.8265 → 0.8237). The red-card effect is a typical published estimate; the data has no card times to test it on. The Lab uses the same model for the chance now of picks, value bets and slips in play. Goal and card times are exact when the feed lists the events (ESPN does); otherwise a new goal is placed halfway between two score checks, and the chart says so. Finished games keep their chart for about half an hour after the final whistle. The cash-out adviser uses red cards too.

## Match centre

For competitions ESPN follows, a match page also shows the game itself, from ESPN's free match summaries. Once the line-ups are in (usually about an hour before kick-off) it draws both teams on a pitch in their formations and kit colours (with a fix when the two kits clash), with the benches below. During and after the game it adds a timeline (goals, penalties, own goals, cards and substitutions) and the team stats: possession, shots, shots on target, corners, fouls, cards, offsides, saves, passes and tackles, each with a bar. While a game is on and its page is open it refreshes about once a minute, and after the final whistle every 15 minutes for a day. The phone asks ESPN directly, so it uses none of your odds or API-Football credits, and the workflow doesn't change.

## Momentum wave

Above the match centre's tabs, a wave shows which side is pushing, minute by minute, from ESPN's live commentary. Every attacking moment counts for the side making it:

| Moment | Weight |
|---|---|
| Goal | 4 |
| Shot on target, shot off the woodwork | 3 |
| Penalty saved or missed | 2.5 |
| Shot off target | 1.8 |
| Shot blocked | 1.6 |
| Corner | 1.4 |
| Free kick won in the attacking half (on a wing) | 0.6 (0.4) |
| Offside | 0.6 |

Shots from close in count a third more, from long range a third less. Each side's pressure fades with a half-life of about a minute and a half, and the wave is the difference: bars up for the home side, down for the away side, in the teams' kit colours (lifted or deepened until they read on the card). Goals, red cards and half time are marked, and stoppage time is drawn where it happened, so the axis is the game's own clock. The headline says who is on top right now (the last eight minutes); after the game, who was on top for most of it, plus each side's best ten-minute spell. It reads the commentary, not the ball, so a long spell of possession without shots barely shows. It refreshes with the match centre, about once a minute.

## Multiverse

Every match page plays the game 10,000 times and draws all of them at once, as a glowing river:

- **Before kick-off** each simulated game takes its final score from Statpad's scoreline grid (the one behind every price on the page) and spreads its goals over the 90 minutes the way goals really arrive, a little more often late on (the same goal clock as the live chances).
- **During the game** every simulation starts from the real score and minute and plays out the rest exactly as the live win probability does (score effects, red cards and all), so the river and the numbers always agree. It re-runs with each new minute and score, cross-fading as the futures narrow.
- **After full time** it shows the 10,000 games from before kick-off with the real game traced through them as a white line, and says how often the real score came up ("about 1 in 15").

It's drawn in 3D with WebGL: a floor of light with time running towards you, kick-off near the horizon and the final whistle at the front, where three gates stand for the home win, the draw and the away win, each with its count. Each lane of the floor is a goal difference (the home side ahead to the left, the away side to the right). The river painted on the floor is as wide as the share of simulated games at that goal difference at that moment, so the main channel thins as games branch off and the branches swell, and its colour shows how the games in it end (electric blue home win, violet draw, ember away win). About 2,600 of the games run as streaks of light along their own goal times into their gate; their paths are worked out on the graphics chip, so a frame costs the page a handful of draw calls. The picture goes through a glow pass (bloom), with slight lens fringing, scanlines and grain. While a game is on, the real game so far is a laser line and the present is a bright point the futures pour out of; after full time the real path runs all the way into its gate, marked "real result". Without WebGL (or if the phone drops the context) it falls back to the flat river: up is the home side ahead, down the away side. Tap a gate, or an ending on the buttons below, to follow only the games that reach it. The card also gives the most common final score, how many games have a comeback win, how many change result after 85 minutes, and, after a goal, how many of the 10,000 it swung. It runs on the phone in a few tens of milliseconds, animates only while it's on screen, and stays still with reduced motion.

## Match fingerprint

For games with ESPN's commentary, the match page draws the game as a fingerprint: one line wound from the centre outwards, where every turn is a lap of the game's clock (kick-off at the top, running clockwise). The ridge is pushed out where the home side was pressing (from the momentum wave) and pulled in where the away side was, a little more on every turn, so the outer ridges carry the most; its colour leans to the side on top, in the teams' kit colours. Goals cut the outer ridges and are marked at their minute, red cards notch the rim, more shots wind more turns, possession stretches it, and the game's id seeds the small irregularities, so no two games make the same print. While a game is on it draws only the minutes played so far.

**Share the poster** and **Save in 4K** give a 2160 × 3840 picture: the fingerprint, each goal's minute, the score in the teams' colours, possession, shots, and how often that exact score came up before kick-off ("1 in 15"). It's drawn on the phone.

## Weekend miracles

After each weekend (Friday to Monday, in the record's time zone), the update writes `miracles.json`: the weekend's graded games, rarest final score first. Rarity is how many of 10,000 versions of the game, drawn from Statpad's scoreline grid before kick-off, ended with exactly that score (a 5–5 can be 5 in 10,000, a 1–0 is often over 1,000). It also names the **biggest shock**, the win with the lowest chance before kick-off. Games graded before the record kept expected goals have them worked back from their home, draw and away chances.

On the Overview, **Weekend miracles** in the row of shortcuts (from Friday until Thursday) opens the full list with a field of 10,000 dots where the versions that ended with the real score light up (tap any game to light up its own). **Make WhatsApp clips** opens the Status pack with the Weekend miracles clip first. The file is a few KB, so the phone never downloads the whole track record for it.

## Scoreline explorer

Every match page has a scoreline grid you can play with. Tap scores to make your own set ("1–0, 2–0 or 2–1"), or pick conditions: result, total goals, both teams to score, winning margin, and each team's goals. Statpad adds up the matching scorelines from the whole grid, so the links between the parts are priced in, and shows the chance, the fair odds and a typical bet-builder price. Add it to the coupon and save it to My bets, where it settles itself like any other pick.

## Why this pick

Each match page opens with the story behind the top pick: the teams' attack and defence ranks on Statpad's ratings, recent form, expected goals, what the bookmakers and Statpad's own model each say, how the price has moved, team news, and any reason for caution (thin data, disagreement, old prices).

## Value board

**Value** lists real prices above the bookmakers' own fair price: edge = fair chance × best price − 1, where the fair chance is the consensus of all the bookmakers with their margin taken off (power method, Pinnacle counted three times). It's line-shopping: one bookmaker paying more than the market as a whole thinks the outcome is worth. Each row shows the best price and bookmaker, the average price, the fair odds and chance, a ¼-Kelly stake, how many bookmakers price the game, how old the prices are, which way the average price has moved, and how past bets with a similar edge did (from the five-season history in the Lab, with its 95% range). Edges are gold; a row's edge turns grey, with a note, when the fair price rests on a single bookmaker or the prices are more than 12 hours old. Each row also carries an opportunity score (see Look and feel). Sort by edge, opportunity, kick-off or number of bookmakers.

Until this update, edges were measured against Statpad's own fitted chance. The Lab's replay showed that was a mistake: on 2025/26 those edges came mostly from small gaps between the two-number scoreline model and the market's own draw and goals prices, and the 1,909 bets they flagged lost 7.3%. Measured against the consensus, the same season flagged 284 bets that won 13.9% and beat the closing price 73% of the time. Even so, about 6 value bets in 10 lose; the edge only shows over hundreds of bets.

**Coupon.** Picks from the same match are linked (over 2.5 goals and both teams to score tend to land together), so the coupon works out their chance together from that match's scoreline grid instead of multiplying them.

## Market movers

**Value → Market movers** lists the games whose bookmaker prices moved most since Statpad first saw them, measured as the change in the margin-free chance (not just the odds). A shortening price usually means money is coming in, often from sharp bettors; a drifting one the opposite. Each move shows a small price chart, the move in the last six hours, whether Statpad's own model agrees, team news, and whether a value price is still available. Tap one to open the match.

## Season simulator

**Leagues → Season simulator** plays the rest of the league season 5,000 times on the phone, in about a quarter of a second. Games that already have bookmaker prices use Statpad's blended expected goals; the rest use the team ratings, and each simulated season nudges every team's strength a little, because ratings are never exact. You get each team's average final points, its chance of the title (or promotion places in second divisions), the top places and the bottom places, and a strip showing every finishing position.

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

- Match cards show **High stakes** and **Huge stakes** badges. The **Leagues** page opens with **Most at stake**, and **All by stakes** sorts the Matches page by it.
- Each match page has a **What's at stake** card: for both teams, the chance that moves most, after a win, a draw and a defeat.
- The simulator's What-if list and team pages' next games show it too, and the spoken briefing names the game with most at stake.

It covers leagues where every team plays every other home and away.

## Fatigue and motivation

Once a month, alongside the other tuning, Statpad tests:

- **Rest**: a side with only two or three days since its last league game, against one with more.
- **Nothing to play for**: from the season simulation run week by week over past seasons, a side whose season chances barely move whatever the result.
- **Open games**: games where neither side has anything left to play for.

Each is fitted on two past seasons after blending with the market as the site does, then checked on the last full season, which the fit never saw. It goes on only if it lowers the error there too; the Lab shows which are on, and a match page says when one applies. football-data.co.uk lists league games only, so cup and European games don't count towards rest. The first run after this update does the fit, so it takes a few minutes longer than usual.

## Live shock map

**Shock map**, in the row of shortcuts on the Overview, opens a globe of the games in play, full screen, floating in open space with nothing around it; while games are on, the shortcut glows and counts them. It's the real earth, drawn by the phone's graphics chip (WebGL) from NASA's Blue Marble and city-lights pictures: daylight where the sun is up right now, city lights where it's night, with a thin atmosphere at the rim. It turns on its own and slows down while a shock passes the middle.

- **Turn it any way**: drag. It turns every way and keeps spinning a little after a flick.
- **Zoom** (full screen): pinch, scroll with a mouse, double-tap, or use **+** and **−** (up to 16 times). The globe button goes back to the whole world. Sharper pictures (4096 pixels wide) load once you zoom in close.
- **Busy evenings**: games too close together to tell apart merge into a numbered bubble in the colour of the most dramatic one. Tap a bubble to fly in. Zoomed in, every game in play gets a label with its score and minute, and closer still, the games about to start.

The pictures come from NASA (public domain) via the open three-globe project on jsDelivr; the workflow fetches them once, shrinks them to WebP (about 200 KB for both at normal size) and publishes them in `globe/`, and the phone keeps them. **Display → Save data** uses a simple built-in land map instead. Phones without WebGL get the old dotted globe.

Each game sits in its country; a game in play sends up a beam of light that rises higher for a bigger shock, and pulses faster and wider as its chances move:

- **Red, shock**: the favourite's chance of winning has collapsed since kick-off.
- **Amber, wobble**: a smaller shift.
- **Blue**: going to plan.
- **Grey**: kicks off within the next six hours.

The shock number is the fall in the favourite's chance in percentage points; a small favourite collapsing, or a big swing in the last ten minutes, counts too. Chances in play come from the score, the minute, red cards and each side's expected goals before kick-off. Tap a dot or a row for the match. The map only animates while it's on screen, and stays still if the phone asks for reduced motion.

## Goal seek

**Slips → Goal seek**: enter your stake and what you want to collect (or tap ×10 … ×1000). Statpad works out the odds needed after tax on winnings (set in My bets), then searches every combination of one pick per match, up to 12 legs, for the slip most likely to land in full while paying at least that. Choose today, today and tomorrow, or the next 3 days.

Every leg is weighed before it can be used:

- **Price:** what a typical bookmaker pays (the market average), not the single best price one bookmaker somewhere offers. Double chance is priced from the market and marked est.
- **Chance:** Statpad's cautious chance (one standard deviation below its estimate), and never above the bookmakers' own consensus, so a long shot that only looks good on paper can't buy its way in.
- **Minimum per leg:** every leg must clear the chance you set under **Every leg at least** (Any, 25%, 40%, 55% or 70%; 40% unless you change it).

It shows:

- the chance the whole slip lands (cautious, and Statpad's central estimate), and "about 1 in N";
- **Weighing the options**: the same goal with every minimum per leg, with the number of legs, the weakest leg and the chance it all lands. Long shots buy odds with few legs but each one rarely lands; safer legs need more of them, and every extra leg is another way to lose. Tap a row to use it;
- what the slip returns on average;
- the legs, with a button to add them all to the coupon;
- a value-only version, where every leg beats the bookmaker's price;
- the **price of ambition**: a curve of the best chance for every amount you could aim for, so you can see what halving the goal buys.

Ask Statpad understands it too: "turn 10,000 into 500,000", "I want 1 million from 5k", or in Kiswahili "nataka laki tano kutoka elfu kumi".

## WhatsApp Status pack

**Status pack** (in the Overview's shortcuts, and in Smart slips) makes 11-second videos for the day, with sound, sized for WhatsApp Status (1080p, or 4K for other apps). They're drawn and encoded on the phone itself, so nothing is uploaded anywhere and the workflow doesn't change.

**Pick the one you want first.** The pack opens on a poster of each clip and makes nothing until you choose: tap a clip and it's made straight away, then the rest follow in order; tap another while one is being made and it goes next (or takes over at once, if the one being made wasn't one you picked and isn't half done). **Make all** makes them in order. Opening the pack from a particular clip (Weekend miracles' **Make WhatsApp clips**) makes that one first.

**About twice as fast.** On a test machine a 1080p clip went from about 19 seconds to about 10. The frames are now drawn straight into the sideways shape the encoder takes, instead of drawn upright and copied round (a third of each frame's time); while the numbers hold, the floodlit pitch behind them barely moves, so it's drawn every few frames and reused in between; the fog is scaled once instead of every frame; the sound is mixed and encoded on other threads while the frames are drawn; and the video maker starts downloading as the pack opens, while the posters are drawn. The pictures are the same as before.

Every clip runs the same way: a line of light opens into a floodlit pitch drawn as a hologram, the clip's title slams across the middle while the camera swoops down, and the title docks at the top. The day's numbers then build up and hold long enough to read. Finally the picture powers down into a point that bursts into the end card: the Statpad mark drawn by a laser, the wordmark condensing out of particles, a line for the clip, the recent record of “Nearly definite” picks (when there are 20 or more and at least 70% landed), and the site's address.

### Thirteen kinds of clip, a fresh line-up every day

| Clip | What it shows |
|---|---|
| Safest pick | A confidence dial filling to Statpad's chance, the cautious chance marked, then the next two safest picks |
| Upset alert | A radar locking onto the underdog (real price 2.60+ with the best value), fair odds v price, one dot in N |
| Most at stake | Three futures (win, draw, defeat) for each team's season; falls back to the next few days |
| Odds slip | The total odds climbing leg by leg while the chance of landing drains |
| Goal rush | The game with most goals expected: the number, Over 2.5 and both-teams-score rings, and an equalizer of total goals with a ball hopping to the most likely |
| Exact score | A 6 × 6 grid of scorelines lighting up from 0–0, locking onto the most likely three, with home/draw/away |
| Match of the day | The headline game in the two teams' own colours: crests, who wins as a tug of war, last five results, expected goals, Statpad's pick |
| Best value | Fair odds and the best price as two needles on an odds dial, the edge between them, and how often it still loses |
| Results | The last day's graded picks flipping one by one to landed or missed, the hit rate, and the best prices that landed |
| Price drop | A price that has fallen 8% or more since it was first seen, drawn as it fell |
| In form | A team with four or five wins in its last five, its results igniting, and its chance today |
| Matchday | The day in numbers: games, competitions with their logos, top picks, value bets, and the kick-off times |
| Weekend miracles | The weekend's rarest real result: the score slams in, a field of 10,000 versions of the game wipes in and the few that ended that way light up, then the next three rarest. Most likely from Saturday to Tuesday |

Each day the pack takes the **four** kinds the day's games make most interesting (a huge edge, a clear favourite score, a big stakes game and so on), steers away from the kinds this phone made on the last four days, and turns a little by the date, so even a quiet week doesn't repeat; a kind with nothing to show that day is skipped. **4 more clips** makes the next ones on the day's list. Each kind also has a few titles and closing lines, and the camera's swoop and orbit change with the date, so the same kind looks different on another day. The record of what was made is kept on the phone (the last 10 days).

**The sound** is synthesized on the phone and timed to the frame: a low machine hum, a power-up sweep, decode ticks, servo whirs as dials fill, clunks when numbers land, radar pings, a glitch burst, rising tones for a win and falling ones for a defeat, a climbing scale for slip legs and goal bars, and a power-down. No voices and no music.

**Format, and why WhatsApp takes it.** The clips are plain MP4 files, H.264 video with AAC sound, stored the way phone cameras store portrait video: landscape frames with a "turn 90°" flag, which every phone decodes for its own camera videos. (Upright 4K frames, 2160 wide by 3840 tall, are a shape many phones' decoders refuse, and WhatsApp then shows only a blank video icon; that was the cause of clips that wouldn't post.) Before a clip is offered, Statpad plays it back on the phone; if the phone can't show it, that size is dropped and the clip is made smaller by itself (4K, then 1440p, 1080p, 720p). Sound is always AAC, which every phone and app plays: the browser's own encoder when it has one, otherwise an AAC encoder (FFmpeg's, built for WebAssembly, about 1 MB) that loads the first time. Only if that can't load are the clips silent, and the pack says so. **Sound → Off** makes silent clips on purpose.

**Built to survive WhatsApp's compression.** WhatsApp re-encodes every Status video, by default down to about 480p, so the clips are drawn for that: no film grain (random noise is the costliest thing a video can carry), the pitch and camera hold almost still while the numbers are on screen (a still background costs almost nothing, so the bits go to the numbers), lines are thicker, and small labels are drawn bigger and never lighter than semibold. In a test clip, the part where the numbers hold needed about a quarter of the data it used to. For the sharpest result, in WhatsApp go to **Settings → Storage and data → Media upload quality** and pick **HD** (720p) before posting; if your WhatsApp doesn't have that, send the clip to yourself with the **HD** button on and forward it to My status.

**Quality.** **1080p · WhatsApp** is the default: it's what WhatsApp Status shows anyway, and it's about three times quicker to make. **4K** (2160 × 3840 as shown) is for TikTok, Instagram and YouTube Shorts; 1080p clips are made at up to 10 Mbps (under WhatsApp's 16 MB limit for an 11-second clip) and 4K at up to 24 Mbps. Keep the sheet open while the videos are made (each shows its progress and the time left); close it and it stops, open it and it carries on.

**Sharing.** **Share all** opens the phone's share sheet with the MP4s and a caption with the site's link; pick WhatsApp, then My status. **Share pictures instead** gives the finished frame of each clip as a 4K picture, which is also what phones that can't make video get. The video packer (Mediabunny, MPL-2.0) and, where needed, its AAC encoder load from jsDelivr the first time, so the first pack needs a connection.

## Statpad, the assistant

There is one way in: **Ask Statpad** on the Overview (on a computer, **Ctrl/⌘ + K** too). It opens the assistant, listening. Tap the orb at the top of the conversation to **tuck it away**: the orb then floats over every page (the matches, every tab, match pages, team pages and the tools) while you look around. **Tap** the orb to bring the conversation back; **press and hold** it to talk straight away. Drag it to either side or up and down; it remembers. **Close** the conversation (×, or say "bye") and the orb goes with it, until the next time you tap Ask Statpad.

**Talk to it like a person.** English or Kiswahili, spoken or typed; it answers in the language you use. Some of what it handles:

| | For example |
|---|---|
| A game | "Who wins Arsenal v Chelsea?", "why?", "how sure are you?", "will both teams score?", "what's the most likely score?", "who scores first?", "best odds?", "in how many universes do they win?" |
| Picks | "Safest picks today", "sure bets for Saturday", "top 3 over 2.5 tips in the Premier League tomorrow", "nearly definite picks", "best value", "underdogs this weekend", "likely draws", "which games have the most goals", "biggest games" |
| Slips | "Give me a 10 odds slip", "build a 4-leg slip for tomorrow", "an over 1.5 accumulator", "make a slip of these", "another one", "make it safer", "turn 10,000 into 500,000" |
| Teams and leagues | "How are Arsenal doing?", "when do Chelsea play next?", "is Barcelona playing this weekend?", "Barcelona injuries", "compare Real Madrid and Barcelona", "Premier League table", "who is top of Serie A?", "who will win La Liga?", "who goes down?" |
| Live, bets, record | "What's live?", "any upsets?", "what's the score in the Arsenal game?", "how are my bets doing?", "what's in my coupon?", "how accurate is Statpad?", "yesterday's results", "how are value bets doing?" |
| Odds and how it works | "What does odds of 2.5 mean?", "5/2 in decimal", "if I stake 5,000 at 3.2 what do I win?" (after your tax setting), "what is a value bet?", "what's the cautious chance?", "how do you make predictions?", "how does the shock map work?" (answered from the Method text) |
| Doing things | "Open Arsenal v Chelsea", "take me to the Lab", "show the Bundesliga table", "open the shock map", "make WhatsApp clips", "read me the briefing", "add it to my coupon", "add them all", "clear my coupon", "dark mode", "set my budget to 20,000 a week", "set target odds to 5", "my team is Arsenal", "call me Sam" |
| Kiswahili | "Nani atashinda Arsenal dhidi ya Chelsea?", "kwa nini?", "na kesho?", "nipe mkeka wa odds 10", "machaguo salama ya leo", "mechi za kesho ni zipi", "Arsenal wanaendeleaje?", "msimamo wa Premier League" |

**It keeps the thread.** "And tomorrow?", "what about La Liga?", "and Chelsea?", "more", "why the second one?", "add it" and "yes" all lean on what came before. It knows what's on screen, so on a match page "who wins?" means that match. When a word fits several clubs ("Manchester") it asks which, and when a day has no games it offers the nearest one ("want those?"). Names a little off ("Chelsi", "Liverpul") are still found.

**It does things, then gets out of the way.** When it opens a match, a team, a tab or a tool, the panel shrinks to a strip at the bottom with the answer and suggestions, so the page shows; tap the strip, or ask again, to bring it back up. Answers come as cards: a match with the three-way chances and the pick, a list of picks with crests, a slip with its total odds and chance, a league table, the record. Tap any of them to open it.

**Voice.** Spoken questions get spoken answers; under the assistant's settings (the sliders) you can have every answer read aloud, or none. **Hands-free** listens again after each spoken answer, so you can keep talking without touching the phone; say "stop" or tap the orb to end. Tapping the orb or the microphone while it talks interrupts it. A short tone marks the microphone opening and closing.

**It speaks up.** While the page is open, when a game in one of your open bets, in your coupon or involving your team kicks off, has a goal or finishes, a note pops up in the corner (beside the orb, when a conversation is tucked away; it also goes into the conversation) with your bet's chance from there: "Goal for Arsenal! Arsenal 1–0 Hull, 34'. Your bet is now about 75% to land." Tap it to open the assistant. With every answer read aloud turned on, it says it too.

**What it remembers.** Your name and your team ("my team is Arsenal": the greeting then mentions their next game), the language, the voice settings and where you put the orb, on this phone only. The conversation itself is kept only while the page is open.

The phone's browser turns speech into text (Chrome on Android, Safari on iPhone); everything after that runs on the page, from the same numbers as everything else, and nothing is sent anywhere. It doesn't know about things outside Statpad (the weather, the news) and says so. If the microphone does nothing: allow it for the site (tap the icon left of the address, then Permissions), and check you're online. Inside the claude.ai preview the microphone is blocked; it works on your GitHub Pages site. Where a browser has no voice input, type instead.

## Combos, draw no bet and handicaps

Each match page has a **Combos, draw no bet & handicaps** section with:

- **Same-game combos.** Win and over 1.5 or 2.5, win and both teams score, win and under 3.5, win to nil, double chance and under 3.5 or both teams score, both teams score and over 2.5, and draw and under 2.5.
- **Draw no bet.** Stake back on a draw; the chance shown is of winning when there's a winner.
- **Handicaps ±1.5.** For example "Arsenal −1.5" (wins by 2+) and "Sunderland +1.5" (doesn't lose by 2+).

They come from the same scoreline grid as every other market, so the link between the two parts of a combo is priced in, rather than multiplying two chances as if they were unrelated. Prices are estimates with a typical bet-builder margin: if your bookmaker offers less than the fair odds, it's poor value. You can add them to the coupon and save them to **My bets**, which settles them. They never appear in the top picks, the slips or the Value board.

## My bets

Bets live in **Slip Studio** (the **Slips** tab): **Your coupon** (build a bet) and **My bets** (bets you placed; also under **Saved**).

- **Save a bet.** In the coupon, tap **Save to My bets**. Enter your bookmaker's total odds, your stake (quick buttons for 1,000 to 20,000 UGX), the bookmaker, and optionally the booking code and any accumulator bonus.
- **Price check.** Before you save, it shows Statpad's chance of the bet landing, its fair odds, the odds you need to break even after the tax on winnings, what you'd collect, and what the bet is worth on average. It also gives a verdict: good value, about fair or poor price.
- **Settling.** Bets settle themselves from `results.json`, using the 90-minute score. A postponed leg counts at odds 1. You can also mark a bet won, lost, void or cashed out (with the amount) by hand.
- **Your record.** It shows profit or loss, total staked, return on stakes and win rate, broken down by slip size, bookmaker and Statpad slips vs your own picks. A "Luck or choice?" panel compares your actual result with what Statpad's chances expected, and how your odds compare with fair odds.
- **Budget and settings.** You can set a weekly or monthly budget, the tax rate (15% by default, Uganda's withholding tax on net winnings from July 2026) and the currency (UGX).
- **Private.** Saved bets live only on that phone or browser. Use **Back up** to save a file, and **Restore** to load it on another device.

## Look and feel

Statpad's design is called **Floodlight Intelligence**: a match-day broadcast with a terminal's precision. Surfaces are flat midnight panels with thin, low-contrast edges, no glow and no colour washes; the clubs' own kit colours are the only decoration.

- **One typeface, three voices.** Everything is set in **Mona Sans**, GitHub's variable family, which can be narrow or wide. Wide and heavy for headlines, scores and the numbers that matter (the broadcast voice); normal width for everything you read; and its tabular figures wherever numbers line up in columns (the terminal voice: prices and percentages never jiggle as they change). It comes from Google Fonts and the phone keeps it after the first visit. Shared slip pictures and Status pack videos keep their own two faces (Big Shoulders and Bricolage Grotesque), loaded a moment after the page.
- **Colour with one job each.** Cool colours are chances: ice blue for the home side, orange for the away side, slate for the draw. Gold is value (edges, value prices, the radar). Coral is a loss, mint a win or the model's edge. Dark ("night match") is the default; **Display** has Light ("day match") and Match phone.
- **Navigation.** On a computer a sidebar holds every section (Overview, Matches, Value, Slips, Lab, Leagues, Saved, How it works); the button at its foot folds it down to icons, and it remembers. On a phone, five tabs: **Overview**, **Matches**, **Value**, **Slips** and **More** (Lab, Leagues, Saved, How it works, Display).
- **Find.** The search box in the header (press **/** on a computer) finds any team, match or competition on the board, and any section. Arrow keys move, Enter opens.
- **Links to a section.** The address follows the section, so `…/statpad/#value` opens the Value board and `#slips`, `#track`, `#tables` or `#saved` the others. Installed as an app, a long press on its icon offers Value, Slips, My bets and the Lab.

**Overview.** The home page is the day's broadcast open:

- the date, large, with how many games and competitions;
- four tiles (the day's games and the first kick-off, value spots and the best edge, how often top picks landed, and predictions graded), whose numbers count up when the page opens;
- the headline games as **score bugs**, the strip a TV broadcast puts in the corner: each club's kit colour and a three-letter code, its chance (or, in play, its score), the three-way bar (in kit colours, unless the two kits are too alike), the draw, expected goals and the top pick;
- the ticker, an LED board of games in play and the market's moves;
- the shortcuts (**Ask Statpad**, **Daily briefing**, **Shock map**, **Weekend miracles**, **Status pack**);
- the **intelligence feed**: the biggest edge on the board, where money is moving, where Statpad's model and the market disagree, the most confident pick (with how often a pick like it still loses), the goals game and the coin toss of the day, each a tap from its match;
- and the **next kick-offs**.

The first time the page opens in a visit, the board comes up bank by bank, like floodlights warming before kick-off. Reduced motion skips that and the counting.

**Match centre.** Each match page has four tabs. **Overview**: warnings, the live card, the match centre, the story behind the pick, who wins (Statpad, market and model side by side) and the scoreline explorer. **Markets**: every market, the combos and the price history. **Teams**: what's at stake and team news. **Model**: how the price was made (expected goals, the blend, the bookmakers and how fresh their prices are, the top pick and its cautious chance, and the draw correction for the league), the 10,000-game multiverse and the spread of total goals. Once a game is on, the multiverse and the fingerprint move to Overview.

**Value intelligence.** Every value bet gets an **opportunity score** out of 100, in a ring beside its edge: 45% the size of the edge (full marks at 10%), 20% the chance it lands, 20% how many bookmakers stand behind the fair price (full marks at eight) and 15% how fresh the prices are. Above 70 is strong, 50 good, 35 fair, below that thin. The value board can sort by it. Above the list, the **Opportunity radar** maps the edges: one row per competition, one column per market (result, double chance, goals, both teams to score, team goals), each cell the best edge in it, brighter for bigger, tap to open its match.

**Slip Studio.** Slips and bets are one place now, with one switch along the top: **Smart slips** (the target-odds slips), **Goal seek**, **Your coupon**, **Check a slip** and **My bets**. Tapping a price still sends it flying to the Slips tab, whose badge counts the coupon.

- **Club crests and flags.** Crests appear on match cards, the match page, slips, the coupon, the value board, tables and team pages. National teams show their flag, and competitions show their logo. Teams without a crest yet show a coloured monogram shield.
- **Smooth, not rebuilt.** When prices, scores or chances refresh, the page changes only what changed instead of redrawing whole lists, so bars glide to their new length, crests don't blink, the live globe keeps turning and nothing jumps under your thumb. The pill in each switch slides to the option you tap, a new day slides in from the side it's on and its chip glides to the middle of the row, and presses answer at once. **Display → Motion → Reduced** (or the phone's own reduce-motion setting) keeps everything still.
- **Quicker to use.** A row of competition chips (with a Live chip while games are in play) filters the day in one tap. Sections slide when you switch. Match pages spring open and can be swiped down to close. Each competition's header stays pinned while you scroll its games.
- **Install icon and link card.** The app icon and the link-preview picture (`og.jpg`) are redrawn in the new colours. The workflow draws the icons, so the new ones need the updated `update.yml`; until then the old icon stays.

**Where crests come from.** Three sources, in this order: API-Football (every club in each country Statpad covers, about 30 calls repeated monthly, with a key), the open football-logos collection on GitHub (Europe's top divisions), and **ESPN** (no key). ESPN gives every club in each competition it follows, about 55 of Statpad's 63, one small request per competition a month, and every game paired with ESPN's for live scores or prices tells Statpad both clubs' ESPN ids exactly, whatever the names. The match is careful: “Salzburg” finds “Red Bull Salzburg” and “Raith Rvs” finds “Raith Rovers”, but “Sheffield Wednesday” never takes Sheffield United's crest, and a name that fits two clubs is left alone. Every crest is downloaded once by the workflow (ESPN's too: the page no longer loads pictures from other sites), up to about 120 a run, the next games first. On a test board of 805 clubs this put a crest on file for 96% of them without an API-Football key; the rest (mostly the Finnish and Irish leagues, out of season at ESPN) come from API-Football with the key.

**Crests that stay right.** Some phones showed Tottenham's crest for clubs whose crest comes from ESPN (Alcoyano, for one), on the page and in Status clips. ESPN serves every crest from the same address with the club in the query (`…/combiner/i?img=…/{id}.png`), and the app's offline cache looked pictures up ignoring the query, so the first ESPN crest it had kept (Tottenham's) came back for all of them. The cache now keeps each picture under its full address, only keeps real pictures, and starts again empty (the old one, with its wrong crests, is deleted), and the colours worked out from crests are re-read once. Upload the new `sw.js` with `index.html` for this.

**Why they load quickly.** Each competition's crests are put on one picture, a sheet (WebP, about 2 KB a crest, 64 pixels each), named after its contents. A board of 600 clubs needs about 40 sheets instead of 600 pictures, a competition's crests appear together, and the phone keeps a sheet until its clubs change (then only that sheet is fetched again). Competition logos share one sheet. Match pages use bigger single crests. Each crest's main colour is worked out by the workflow too, so team colours are right straight away. The sheets need the Pillow imaging library: the workflow installs it once into its kept state folder (a few seconds, the first run only), and makes PNG sheets if it can't. **Display → Save data** stops pictures loading at all.

## App and sharing

- **Install.** On Android (Chrome), tap the download icon in the header to install Statpad like an app. On iPhone, open the site in Safari, tap **Share**, then **Add to Home Screen**; the site shows this tip when you tap the same icon. Once installed it opens full screen, and it still opens with the last data when you're offline.
- **Share a slip.** Every slip has a share button. It makes a branded picture of the slip, with both crests on every leg, and opens your phone's share menu, so you can send it to WhatsApp, Telegram or your status. On a computer it saves the picture instead.
- **Link previews.** When the site's link is shared, apps show the Statpad card (`og.jpg`). The workflow fills in the site's full address when it publishes. Apps keep old previews for a while, so a change can take a day to show.

## Where the data comes from

- **football-data.co.uk** (free, no key). Results for 34 leagues, which drive the tables and team ratings. Also fixtures with average and best 1X2 and over/under 2.5 prices, which the site updates about twice a week.
- **openfootball** (free, no key, on GitHub). Full-season fixture lists for the big leagues, so the coming week shows up before football-data's weekly file arrives.
- **International results** (free, no key, on GitHub: `martj42/international_results`). Every national-team result, used to rate teams for the UEFA Nations League and international friendlies and to grade those games. The maintainer updates it after each international window, so those grades can take a few weeks.
- **The Odds API** (optional key). Fixtures for leagues, UEFA competitions and cups come from a free listing that doesn't use credits. Prices from many bookmakers are paced to your monthly credit allowance by a refresh plan (below). The free plan has 500 credits a month; a paid plan refreshes as often as its credits and `ODDS_MIN_INTERVAL` allow.

- **ESPN play-by-play** (free, no key). Every shot with its spot on the pitch for eight leagues, turned into commentary xG (see **Tuning**). About 40 games a run until the past seasons are in, then a few a week.
- **ESPN** (free, no key: the public scoreboards behind espn.com). Live scores, goal and red-card times, final scores within minutes, and confirmed line-ups, for every competition Statpad covers except the Swiss, Romanian, Polish, Czech, Serbian and Finnish leagues and K League 1. Read by the workflow every 5 minutes and by your phone every 30 seconds to a minute while a game is on. Also DraftKings' prices for the next day and a half, and for these competitions the fixtures and the last two seasons of results that build their ratings: the Women's Super League, Liga F, Première Ligue, NWSL, the Women's Champions League, the Saudi Pro League, the Eerste Divisie, the A-League, Brazil's Série B, Chile, Colombia and South Africa, plus fixtures for the Libertadores, the Sudamericana and the Scottish Championship. Past months are read once (about 250 small requests spread over the first few runs); after that, a few a run.
- **API-Football** (optional key, free plan: 100 calls a day). Club lists for crests (about 30 calls a month). Team news: injuries, suspensions and doubts for today and tomorrow, plus line-ups for the games ESPN doesn't cover. Also results and live scores for the competitions ESPN doesn't carry, and the team ids that give each club its crest. The calls are rationed to fit the free plan, which only serves fixtures and results for yesterday, today and tomorrow, one fixture per call; anything older comes from football-data. When the plan refuses one kind of request (for example several fixtures in one call), only that kind waits a week and **Method → Data status** names it; everything else carries on.

Without a key, the site still works from the model, the football-data prices and DraftKings' prices from ESPN. Czech and Serbian matches, the Frauen-Bundesliga and K League 1, the UEFA and cup matches, and the Nations League and friendlies need the key, because their fixtures come from The Odds API.

**Women's football.** The Women's Super League, Liga F, Première Ligue, NWSL and Women's Champions League come from ESPN (with DraftKings' prices where they have them, and The Odds API's for the Women's Champions League); the Frauen-Bundesliga from The Odds API. Women's sides are named "… Women" (Chelsea Women) and are only ever matched with other women's sides, so they never borrow a men's team's ratings, results or form; they do wear their club's crest. In **Slips**, the scope menu has **Men's football** and **Women's football**. Serie A Femminile isn't covered: none of the free sources lists it with fixtures ahead of time.

## Settings (optional)

Add these under **Settings → Secrets and variables → Actions → Variables**:

| Variable | Default | Meaning |
|---|---|---|
| `ODDS_REGIONS` | `eu` | Bookmaker regions (`eu`, `uk`, `us`, `au`). Each one multiplies the credit cost. |
| `ODDS_MARKETS` | `h2h` | Use `h2h,totals` to add over/under 2.5 prices. This doubles the cost. |
| `ODDS_MIN_INTERVAL` | `5` | Fastest refresh per competition, in minutes. Your credits and the refresh plan decide the real pace. |
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
- **The refresh plan.** Each call to The Odds API prices every game of one competition, so the plan works per competition, by how close its next game is. With credits to spare it would refresh a competition every 30 minutes in the last 2 hours before its next kick-off, hourly from 2 to 6 hours, every 3 hours within a day, every 6 hours within 3 days, twice a day within a week and daily beyond, a little more often for the biggest competitions. Prices move most in the last hours (team news, line-ups), so that is where the credits go. With fewer credits every interval is stretched by the same factor until the day's budget covers it; with more (paid plans) they shrink, down to a third. On top of that, each kick-off gets **one last refresh in its final 90 minutes**: the closing price, the sharpest there is and what value bets are measured against. Games already under way are never asked for again, so their last price before kick-off stays put. The day's budget follows how many games each day has, is fixed at the day's first run, keeps room for the day's last calls, and 2% of the month's credits are never spent, so the key never runs dry.
- What that buys, simulated over a month of real fixtures (about 1,100 games): on the **free plan** (500 credits), 99% of games have bookmaker prices before kick-off (69% with the old pacing), 64% with prices refreshed in the last 90 minutes (21% before), and the median price is 1.5 hours old at kick-off (7.2 before), for 471 credits. On the **20K plan**, every game gets a price refreshed in its last 90 minutes (60% before), the median price is 12 minutes old, for about the same credits as before.
- **Stale prices.** A price counts as stale when it's older than the time left to kick-off (at least 6 hours, at most 36): this morning's price is fine for a game next week, not for one in an hour. Stale prices widen the cautious chance a little and show "refresh due" on the match page.
- Method → **Data status** shows the plan as it stands (how often games within 2 hours, a day and 3 days are refreshed), and the run log lists every refresh and why (due, last call, team news).
- GitHub pauses scheduled workflows in a repo that has had no commits for 60 days. The workflow avoids this by committing `track.json` at least every 20 days. That needs the `contents: write` permission at the top of `update.yml`.
- The run log (**Actions → latest run → Fetch data and fit the model**) lists every source, every competition key the API didn't list, and how many credits are left.
- Team names are matched across sources automatically. If a fixture shows "No prediction yet", the names didn't match or there is no data for that team. Add an entry to `ALIAS` in `update.py`.

Statpad gives probabilities, not guarantees. 18+ only. Stake small, and only what you can afford to lose.
