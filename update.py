#!/usr/bin/env python3
"""
Statpad data pipeline. Python 3.9+ standard library; NumPy speeds up the heavy fits when it's there (the run installs it
on demand into a scratch folder, and carries on in plain Python if it can't), so GitHub Actions needs no setup.

Each run it:
  1. results   football-data.co.uk results (every RESULTS_EVERY hours) -> league tables and team
                attack/defence ratings (Poisson model with home advantage, time decay and shrinkage)
  2. fixtures  football-data.co.uk fixtures.csv + new_league_fixtures.csv (every FIXTURES_EVERY hours),
                including their average / best 1X2 and over-under 2.5 prices, plus full-season
                schedules from openfootball (GitHub) so the week ahead is never empty
  3. events    The Odds API /events (free of credits) for leagues, UEFA competitions and cups
  4. odds      The Odds API /odds, paced so the monthly credit allowance lasts the month
  5. builds    matches.json for the next DAYS days: expected goals per side from the ratings, market
                consensus (sharp books weighted up), best available prices and price history.
  6. xG        ESPN's play-by-play for the 8 leagues it lists shot by shot, turned into commentary xG a few
                dozen games a run (kept in .statpad/xg.json)

The browser (index.html) turns that into probabilities for every market, value picks and slips.

Environment (all optional):
  ODDS_API_KEY          The Odds API key (store it as a GitHub Actions secret, never in the repo)
  ODDS_REGIONS          bookmaker regions, default "eu"            (each region costs credits)
  ODDS_MARKETS          markets, default "h2h"  (add ",totals" for over/under; doubles the cost)
  ODDS_MIN_INTERVAL     fastest refresh per competition in minutes, default 5 (the credit budget
                        decides the real pace; games kicking off soonest are refreshed first)
  ODDS_MONTHLY_CREDITS  your plan's monthly credits, default 500 (the free plan); 2% are always kept back
  ODDS_RESET_DAY        day of month the credits reset, default 1
  ODDS_SPORTS           "mapped" (default: listed leagues + any UEFA/cup competition) or "all"
  STATPAD_DAYS          days ahead to publish, default 7
  STATPAD_OUT           output folder for the site data, default ./_site
  STATPAD_STATE         folder for state kept between runs, default ./.statpad
  STATPAD_OFFLINE_DIR   read every download from files in this folder instead (testing)
  STATPAD_NOW           pretend the time is this ISO timestamp (testing)
  STATPAD_FORCE=1       refresh everything now, ignoring the schedule
"""
import csv
import difflib
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import sys
import time
import unicodedata
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

UTC = timezone.utc
ENV = os.environ.get
ROOT = Path(__file__).resolve().parent
OUT_DIR = Path(ENV('STATPAD_OUT') or ROOT / '_site')
STATE_DIR = Path(ENV('STATPAD_STATE') or ROOT / '.statpad')
SEED = ROOT / 'matches.json'
OFFLINE = ENV('STATPAD_OFFLINE_DIR')
FORCE = ENV('STATPAD_FORCE') == '1'


def _now():
    v = ENV('STATPAD_NOW')
    return datetime.fromisoformat(v.replace('Z', '+00:00')) if v else datetime.now(UTC)


NOW = _now()
DAYS = float(ENV('STATPAD_DAYS') or 7)
RESULTS_EVERY = 6      # hours
FIXTURES_EVERY = 1     # hours
SPORTS_EVERY = 12      # hours
EVENTS_EVERY = 30      # minutes
API_KEY = (ENV('ODDS_API_KEY') or '').strip()
REGIONS = [r.strip() for r in (ENV('ODDS_REGIONS') or 'eu').split(',') if r.strip()]
MARKETS = [m.strip() for m in (ENV('ODDS_MARKETS') or 'h2h').split(',') if m.strip()]
MIN_INTERVAL = max(3.0, float(ENV('ODDS_MIN_INTERVAL') or 5))
MONTHLY = float(ENV('ODDS_MONTHLY_CREDITS') or 500)
RESET_DAY = int(ENV('ODDS_RESET_DAY') or 1)
SPORTS_MODE = (ENV('ODDS_SPORTS') or 'mapped').lower()

FD = 'https://www.football-data.co.uk'
OF_BASE = 'https://raw.githubusercontent.com/openfootball/football.json/master'
# openfootball season files: league -> (file, local UTC offset in winter, uses EU summer time)
OF_MAP = {'EPL': ('en.1', 0, True), 'ENG2': ('en.2', 0, True), 'ENG3': ('en.3', 0, True), 'ENG4': ('en.4', 0, True),
          'BUN': ('de.1', 1, True), 'GER2': ('de.2', 1, True), 'LaLiga': ('es.1', 1, True), 'ESP2': ('es.2', 1, True),
          'SA': ('it.1', 1, True), 'ITA2': ('it.2', 1, True), 'L1': ('fr.1', 1, True), 'FRA2': ('fr.2', 1, True),
          'ERE': ('nl.1', 1, True), 'NED2': ('nl.2', 1, True), 'POR': ('pt.1', 0, True), 'BEL': ('be.1', 1, True),
          'SCO1': ('sco.1', 0, True), 'TUR1': ('tr.1', 3, False), 'AUT1': ('at.1', 1, True), 'SUI1': ('ch.1', 1, True),
          'GRE1': ('gr.1', 2, True), 'DEN1': ('dk.1', 1, True), 'CZE1': ('cz.1', 1, True)}
OA = 'https://api.the-odds-api.com/v4'

# ----------------------------------------------------------------------------------------------
# Competitions. strength = rough log goal-rate offset vs the Premier League, used only when teams
# from different leagues meet (European ties, cups). Market prices dominate those games anyway.
# ----------------------------------------------------------------------------------------------
L = {}


def comp(lk, name, country, group, strength, fd=None, x=None, oa=(), es=None, hist=False, w=False, cup=False):
    """es: ESPN's scoreboard, for fixtures and DraftKings prices; hist: ratings built from ESPN's results;
    w: women's football (its teams are matched only with other women's teams); cup: teams from several leagues."""
    L[lk] = dict(name=name, country=country, group=group, strength=strength, fd=fd, x=x, oa=list(oa), es=es, hist=hist, w=w, cup=cup)


comp('EPL', 'Premier League', 'England', 'big', 0.00, fd='E0', oa=['soccer_epl'])
comp('LaLiga', 'La Liga', 'Spain', 'big', -0.10, fd='SP1', oa=['soccer_spain_la_liga'])
comp('BUN', 'Bundesliga', 'Germany', 'big', -0.12, fd='D1', oa=['soccer_germany_bundesliga'])
comp('SA', 'Serie A', 'Italy', 'big', -0.12, fd='I1', oa=['soccer_italy_serie_a'])
comp('L1', 'Ligue 1', 'France', 'big', -0.20, fd='F1', oa=['soccer_france_ligue_one'])
comp('POR', 'Primeira Liga', 'Portugal', 'big', -0.30, fd='P1', oa=['soccer_portugal_primeira_liga'])
comp('ERE', 'Eredivisie', 'Netherlands', 'big', -0.32, fd='N1', oa=['soccer_netherlands_eredivisie'])
comp('BEL', 'Belgian Pro League', 'Belgium', 'big', -0.38, fd='B1', oa=['soccer_belgium_first_div'])
comp('KSA', 'Saudi Pro League', 'Saudi Arabia', 'big', -0.45, oa=['soccer_saudi_arabia_pro_league'], es='ksa.1', hist=True)
comp('ENG2', 'Championship', 'England', 'second', -0.45, fd='E1', oa=['soccer_efl_champ'])
comp('GER2', '2. Bundesliga', 'Germany', 'second', -0.50, fd='D2', oa=['soccer_germany_bundesliga2'])
comp('ITA2', 'Serie B', 'Italy', 'second', -0.55, fd='I2', oa=['soccer_italy_serie_b'])
comp('ESP2', 'LaLiga 2', 'Spain', 'second', -0.52, fd='SP2', oa=['soccer_spain_segunda_division'])
comp('FRA2', 'Ligue 2', 'France', 'second', -0.60, fd='F2', oa=['soccer_france_ligue_two'])
comp('NED2', 'Eerste Divisie', 'Netherlands', 'second', -0.80, es='ned.2', hist=True)
comp('ENG3', 'League One', 'England', 'second', -0.75, fd='E2', oa=['soccer_england_league1'])
comp('ENG4', 'League Two', 'England', 'second', -0.95, fd='E3', oa=['soccer_england_league2'])
comp('SCO1', 'Scottish Premiership', 'Scotland', 'more', -0.50, fd='SC0', oa=['soccer_spl'])
comp('SCO2', 'Scottish Championship', 'Scotland', 'second', -0.95, fd='SC1', es='sco.2')
comp('TUR1', 'Süper Lig', 'Turkey', 'more', -0.42, fd='T1', oa=['soccer_turkey_super_league'])
comp('GRE1', 'Greek Super League', 'Greece', 'more', -0.48, fd='G1', oa=['soccer_greece_super_league'])
comp('AUT1', 'Austrian Bundesliga', 'Austria', 'more', -0.45, x='AUT', oa=['soccer_austria_bundesliga'])
comp('SUI1', 'Swiss Super League', 'Switzerland', 'more', -0.50, x='SWZ', oa=['soccer_switzerland_superleague'])
comp('DEN1', 'Danish Superliga', 'Denmark', 'more', -0.50, x='DNK', oa=['soccer_denmark_superliga'])
comp('NOR1', 'Eliteserien', 'Norway', 'more', -0.55, x='NOR', oa=['soccer_norway_eliteserien'])
comp('SWE1', 'Allsvenskan', 'Sweden', 'more', -0.58, x='SWE', oa=['soccer_sweden_allsvenskan'])
comp('POL1', 'Ekstraklasa', 'Poland', 'more', -0.58, x='POL', oa=['soccer_poland_ekstraklasa'])
comp('ROU1', 'Liga I', 'Romania', 'more', -0.65, x='ROU')
comp('CZE1', 'Czech First League', 'Czechia', 'more', -0.50)
comp('SRB1', 'Serbian SuperLiga', 'Serbia', 'more', -0.62)
comp('IRL1', 'League of Ireland', 'Ireland', 'more', -0.85, x='IRL', oa=['soccer_league_of_ireland'])
comp('FIN1', 'Veikkausliiga', 'Finland', 'more', -0.90, x='FIN', oa=['soccer_finland_veikkausliiga'])
comp('BRA1', 'Brasileirão', 'Brazil', 'more', -0.30, x='BRA', oa=['soccer_brazil_campeonato'])
comp('ARG1', 'Liga Profesional', 'Argentina', 'more', -0.40, x='ARG', oa=['soccer_argentina_primera_division'])
comp('MEX1', 'Liga MX', 'Mexico', 'more', -0.45, x='MEX', oa=['soccer_mexico_ligamx'])
comp('USA1', 'MLS', 'USA', 'more', -0.48, x='USA', oa=['soccer_usa_mls'])
comp('JPN1', 'J1 League', 'Japan', 'more', -0.50, x='JPN', oa=['soccer_japan_j_league'])
comp('CHN1', 'Chinese Super League', 'China', 'more', -0.70, x='CHN', oa=['soccer_china_superleague'])
comp('UCL', 'Champions League', 'Europe', 'cups', 0, oa=['soccer_uefa_champs_league'])
comp('UEL', 'Europa League', 'Europe', 'cups', 0, oa=['soccer_uefa_europa_league'])
comp('UECL', 'Conference League', 'Europe', 'cups', 0, oa=['soccer_uefa_europa_conference_league'])
comp('FAC', 'FA Cup', 'England', 'cups', 0, oa=['soccer_fa_cup'])
comp('EFLC', 'EFL Cup', 'England', 'cups', 0, oa=['soccer_england_efl_cup'])
comp('DFB', 'DFB-Pokal', 'Germany', 'cups', 0, oa=['soccer_germany_dfb_pokal'])
comp('CDR', 'Copa del Rey', 'Spain', 'cups', 0, oa=['soccer_spain_copa_del_rey'])
comp('CI', 'Coppa Italia', 'Italy', 'cups', 0, oa=['soccer_italy_coppa_italia'])
comp('CDF', 'Coupe de France', 'France', 'cups', 0, oa=['soccer_france_coupe_de_france'])
comp('UNL', 'UEFA Nations League', 'Europe', 'intl', 0, oa=['soccer_uefa_nations_league'])
comp('INTF', 'International friendlies', 'World', 'intl', 0)
# more of the world, from ESPN's scoreboards (fixtures, results for the ratings, DraftKings prices) and The Odds API where it has them
comp('AUS1', 'A-League Men', 'Australia', 'more', -0.70, oa=['soccer_australia_aleague'], es='aus.1', hist=True)
comp('KOR1', 'K League 1', 'South Korea', 'more', -0.60, oa=['soccer_korea_kleague1'])
comp('BRA2', 'Brasileirão Série B', 'Brazil', 'second', -0.70, oa=['soccer_brazil_serie_b'], es='bra.2', hist=True)
comp('CHI1', 'Primera División', 'Chile', 'more', -0.75, oa=['soccer_chile_campeonato'], es='chi.1', hist=True)
comp('COL1', 'Primera A', 'Colombia', 'more', -0.65, es='col.1', hist=True)
comp('RSA1', 'Premiership', 'South Africa', 'more', -0.85, es='rsa.1', hist=True)
comp('LIB', 'Copa Libertadores', 'South America', 'cups', 0, oa=['soccer_conmebol_copa_libertadores'], es='conmebol.libertadores')
comp('SUD', 'Copa Sudamericana', 'South America', 'cups', 0, oa=['soccer_conmebol_copa_sudamericana'], es='conmebol.sudamericana')
# women's football: its own pool of teams, so Chelsea's women's side is never mistaken for the men's
comp('WSL', "Women's Super League", 'England', 'women', 0.0, es='eng.w.1', hist=True, w=True)
comp('LIGAF', 'Liga F', 'Spain', 'women', -0.08, es='esp.w.1', hist=True, w=True)
comp('D1F', 'Première Ligue', 'France', 'women', -0.15, es='fra.w.1', hist=True, w=True)
comp('FBL', 'Frauen-Bundesliga', 'Germany', 'women', -0.05, oa=['soccer_germany_bundesliga_women'], w=True)
comp('NWSL', 'NWSL', 'USA', 'women', -0.05, es='usa.nwsl', hist=True, w=True)
comp('UWCL', "Women's Champions League", 'Europe', 'women', 0, oa=['soccer_uefa_champs_league_women'], es='uefa.wchampions', w=True, cup=True)


def is_cup(lk):
    c = L.get(lk) or {}
    return c.get('group') == 'cups' or bool(c.get('cup'))


def knockout(lk):   # cups and national teams: no league table of their own
    return is_cup(lk) or (L.get(lk) or {}).get('group') == 'intl'


def pool(lk):
    return 'w' if (L.get(lk) or {}).get('w') else 'm'

FD_DIV = {v['fd']: k for k, v in L.items() if v['fd']}
X_COUNTRY = {'Austria': 'AUT1', 'Switzerland': 'SUI1', 'Denmark': 'DEN1', 'Norway': 'NOR1', 'Sweden': 'SWE1',
             'Poland': 'POL1', 'Romania': 'ROU1', 'Ireland': 'IRL1', 'Finland': 'FIN1', 'Brazil': 'BRA1',
             'Argentina': 'ARG1', 'Mexico': 'MEX1', 'USA': 'USA1', 'Japan': 'JPN1', 'China': 'CHN1'}
OA_KEY = {k: lk for lk, v in L.items() for k in v['oa']}
CUP_WORDS = ('uefa', 'cup', 'pokal', 'copa', 'coppa', 'coupe', 'taca', 'beker', 'trophy', 'shield', 'super_cup')
# auto-added competitions: UEFA club competitions and domestic cups of covered countries only
NO_AUTO = ('women', 'qualif', 'nations', 'world_cup', 'friendl', 'international', 'olympic', 'u17', 'u19', 'u20',
           'u21', 'u23', 'youth', 'euro_', 'copa_america', 'africa', 'asia', 'concacaf', 'conmebol', 'club_world',
           'gold_cup', 'afc_', 'caf_')
CUP_COUNTRIES = ('england', 'scotland', 'germany', 'spain', 'italy', 'france', 'netherlands', 'portugal', 'belgium',
                 'turkey', 'greece', 'austria', 'switzerland', 'denmark', 'norway', 'sweden', 'poland', 'czech',
                 'serbia', 'saudi', 'fa', 'efl')


def auto_competition(key):
    k = key.replace('soccer_', '')
    if any(w in k for w in NO_AUTO):
        return False
    if k.startswith('uefa_'):
        return True
    return any(w in k for w in CUP_WORDS) and k.split('_')[0] in CUP_COUNTRIES


SHARP = {'pinnacle': 3.0, 'betfair_ex_eu': 1.0, 'betfair_ex_uk': 1.0, 'matchbook': 1.0, 'smarkets': 1.0}
# exchanges count like any bookmaker: on 30,000 past games their pre-match prices predicted worse than Pinnacle's
# or the average (thin markets before kick-off), so they no longer get extra weight


def demargin(odds):
    """A bookmaker's prices -> chances, taking the margin off by the power method: each 1/price is raised to the
    same power k so they add up to 1. Bookmakers load more margin onto long shots, and on five seasons of the main
    leagues this read the results better than dividing the margin out evenly (log loss 0.9966 against 0.9971 on
    average prices), mainly by giving favourites back the chance an even split takes off them."""
    q = [1 / x for x in odds]
    if sum(q) <= 1:
        s = sum(q)
        return [x / s for x in q]
    lo, hi = 1.0, 4.0
    for _ in range(50):
        k = (lo + hi) / 2
        if sum(x ** k for x in q) > 1:
            lo = k
        else:
            hi = k
    p = [x ** ((lo + hi) / 2) for x in q]
    s = sum(p)
    return [x / s for x in p]
EXCHANGES = {'betfair_ex_eu', 'betfair_ex_uk', 'betfair_ex_au', 'matchbook', 'smarkets'}

# ----------------------------------------------------------------------------------------------
# Team names: football-data uses short names ("Man United"), The Odds API long ones.
# Everything is matched on a normalised key; display uses PRETTY where we know a nicer name.
# ----------------------------------------------------------------------------------------------
STOP = {'fc', 'cf', 'afc', 'sc', 'ac', 'as', 'ssc', 'ss', 'us', 'sv', 'vfl', 'vfb', 'tsg', 'fk', 'sk', 'nk', 'cd',
        'ud', 'sd', 'rc', 'rcd', 'ca', 'club', 'de', 'the', 'calcio', 'bc', 'fsv', 'ogc', 'sl', 'cp', 'sad', 'kv',
        'kaa', 'krc', 'rsc', 'royal', 'hsc', 'osc', 'stade', 'olympique', 'aj', 'losc', 'kvc', 'acf', 'cfc', 'es',
        'estac', 'sco', 'sbv', 'gd'}
ALIAS = {
    'manchester united': 'man united', 'man utd': 'man united', 'manchester city': 'man city',
    'tottenham hotspur': 'tottenham', 'spurs': 'tottenham', 'wolverhampton wanderers': 'wolves', 'wolverhampton': 'wolves',
    'nottingham forest': 'nottm forest', 'brighton and hove albion': 'brighton', 'brighton hove albion': 'brighton',
    'west ham united': 'west ham', 'newcastle united': 'newcastle', 'leeds united': 'leeds', 'leicester city': 'leicester',
    'ipswich town': 'ipswich', 'luton town': 'luton', 'hull city': 'hull', 'coventry city': 'coventry',
    'norwich city': 'norwich', 'stoke city': 'stoke', 'swansea city': 'swansea', 'cardiff city': 'cardiff',
    'birmingham city': 'birmingham', 'queens park rangers': 'qpr', 'west bromwich albion': 'west brom',
    'preston north end': 'preston', 'blackburn rovers': 'blackburn', 'derby county': 'derby', 'oxford united': 'oxford',
    'plymouth argyle': 'plymouth', 'sheffield wednesday': 'sheffield weds', 'huddersfield town': 'huddersfield',
    'bolton wanderers': 'bolton', 'wycombe wanderers': 'wycombe', 'charlton athletic': 'charlton',
    'wigan athletic': 'wigan', 'peterborough united': 'peterborough', 'cambridge united': 'cambridge',
    'atletico madrid': 'ath madrid', 'atletico de madrid': 'ath madrid', 'athletic bilbao': 'ath bilbao',
    'athletic': 'ath bilbao', 'real sociedad': 'sociedad', 'real betis': 'betis', 'celta vigo': 'celta',
    'rayo vallecano': 'vallecano', 'espanyol': 'espanol', 'deportivo alaves': 'alaves', 'real valladolid': 'valladolid',
    'deportivo la coruna': 'la coruna', 'deportivo': 'la coruna', 'real oviedo': 'oviedo', 'racing santander': 'santander',
    'racing': 'santander', 'sporting gijon': 'sp gijon', 'real zaragoza': 'zaragoza', 'real mallorca': 'mallorca',
    'bayern munchen': 'bayern munich', 'bayern': 'bayern munich', 'borussia dortmund': 'dortmund',
    'borussia monchengladbach': 'mgladbach', 'monchengladbach': 'mgladbach', 'bayer leverkusen': 'leverkusen',
    'eintracht frankfurt': 'ein frankfurt', 'koln': 'koln', 'cologne': 'koln', 'hamburger': 'hamburg',
    'werder bremen': 'werder bremen', 'mainz': 'mainz', 'union berlin': 'union berlin', 'hertha berlin': 'hertha',
    'hertha bsc': 'hertha', 'schalke': 'schalke', 'fortuna dusseldorf': 'fortuna dusseldorf',
    'inter milan': 'inter', 'internazionale': 'inter', 'hellas verona': 'verona', 'atalanta': 'atalanta',
    'paris saint germain': 'paris sg', 'psg': 'paris sg', 'saint etienne': 'st etienne', 'lyonnais': 'lyon',
    'marseille': 'marseille', 'rennais': 'rennes', 'brestois': 'brest', 'reims': 'reims', 'nice': 'nice',
    'psv eindhoven': 'psv eindhoven', 'psv': 'psv eindhoven', 'az alkmaar': 'az alkmaar', 'az': 'az alkmaar',
    'nec nijmegen': 'nijmegen', 'nec': 'nijmegen', 'pec zwolle': 'zwolle', 'fortuna sittard': 'for sittard',
    'sporting lisbon': 'sp lisbon', 'sporting': 'sp lisbon', 'braga': 'sp braga', 'porto': 'porto',
    'vitoria guimaraes': 'guimaraes', 'vitoria': 'guimaraes', 'royal antwerp': 'antwerp', 'sint truiden': 'st truiden',
    'standard liege': 'standard', 'union saint gilloise': 'st gilloise', 'union sg': 'st gilloise',
    'oud heverlee leuven': 'oud heverlee leuven', 'celtic': 'celtic', 'rangers': 'rangers',
    'real betis balompie': 'betis', 'rayo vallecano madrid': 'vallecano', 'espanyol barcelona': 'espanol',
    'real sociedad futbol': 'sociedad', 'internazionale milano': 'inter', 'strasbourg alsace': 'strasbourg',
    'willem ii tilburg': 'willem ii', 'ado den haag': 'den haag', 'feyenoord rotterdam': 'feyenoord',
    'sporting clube portugal': 'sp lisbon', 'atletico madrid': 'ath madrid', 'club atletico madrid': 'ath madrid',
}
PRETTY = {
    'Man United': 'Manchester United', 'Man City': 'Manchester City', "Nott'm Forest": 'Nottingham Forest',
    'Sheffield Weds': 'Sheffield Wednesday', 'Ath Madrid': 'Atlético Madrid', 'Ath Bilbao': 'Athletic Club',
    'Sociedad': 'Real Sociedad', 'Betis': 'Real Betis', 'Celta': 'Celta Vigo', 'Vallecano': 'Rayo Vallecano',
    'Espanol': 'Espanyol', 'La Coruna': 'Deportivo La Coruña', 'Sp Gijon': 'Sporting Gijón', 'Alaves': 'Alavés',
    'Ein Frankfurt': 'Eintracht Frankfurt', "M'gladbach": "Borussia M'gladbach", 'FC Koln': '1. FC Köln',
    'Hamburg': 'Hamburger SV', 'Paris SG': 'Paris Saint-Germain', 'St Etienne': 'Saint-Étienne',
    'For Sittard': 'Fortuna Sittard', 'Sp Lisbon': 'Sporting CP', 'Sp Braga': 'Braga', 'St Truiden': 'Sint-Truiden',
    'St. Gilloise': 'Union Saint-Gilloise', 'Leganes': 'Leganés', 'Cadiz': 'Cádiz', 'Malaga': 'Málaga',
    'Almeria': 'Almería', 'Atletico Madrid': 'Atlético Madrid', 'Nurnberg': 'Nürnberg', 'Dusseldorf': 'Düsseldorf',
}


def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = s.replace('&', ' and ').replace("'", '').replace('.', ' ')
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    toks = [t for t in s.split() if t not in STOP and not re.fullmatch(r'\d{1,2}|1[89]\d\d|20\d\d', t)]
    key = ' '.join(toks) or s.strip()
    return ALIAS.get(key, key)


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', norm(s)).strip('-')[:24] or 'x'


def pretty(name):
    return PRETTY.get(name, name)


class Teams:
    """Resolve a raw team name to the canonical (football-data) name within a league or globally."""

    def __init__(self):
        self.by_league = {}   # lk -> {normkey: canonical}
        self.globals = {}     # pool ('m' or 'w') -> {normkey: (lk, canonical)}

    @property
    def global_(self):
        return self.globals.setdefault('m', {})

    def add(self, lk, name):
        k = norm(name)
        self.by_league.setdefault(lk, {})[k] = name
        if not is_cup(lk):
            self.globals.setdefault(pool(lk), {}).setdefault(k, (lk, name))

    def resolve(self, lk, raw, cutoff=0.80):
        k = norm(raw)
        table = self.by_league.get(lk) or {}
        if k in table:
            return table[k]
        hit = self._fuzzy(k, table, cutoff)
        return hit

    def resolve_global(self, raw, cutoff=0.86, pool_='m'):
        g = self.globals.get(pool_) or {}
        k = norm(raw)
        if k in g:
            return g[k]
        keys = {kk: kk for kk in g}
        hit = self._fuzzy(k, keys, cutoff)
        return g[hit] if hit else None

    @staticmethod
    def _fuzzy(k, table, cutoff):
        if not table or not k:
            return None
        # token containment ("bodo glimt" vs "bodo glimt fk") with a unique hit
        toks = set(k.split())
        cont = [v for kk, v in table.items() if toks and (toks <= set(kk.split()) or set(kk.split()) <= toks)]
        if len(set(cont)) == 1:
            return cont[0]
        m = difflib.get_close_matches(k, list(table.keys()), n=2, cutoff=cutoff)
        if len(m) == 1 or (len(m) == 2 and difflib.SequenceMatcher(None, k, m[0]).ratio() - difflib.SequenceMatcher(None, k, m[1]).ratio() > 0.05):
            return table[m[0]]
        return None


# ----------------------------------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------------------------------

def disp(lk, name):
    """The name shown on the site: women's sides carry "Women" so they're never confused with the men's club."""
    n = pretty(name)
    if (L.get(lk) or {}).get('w') and not re.search(r'\b(women|woman|femen\w*|f[eé]minin\w*|frauen|ladies|lionesses|wfc)\b', n, re.I):
        n += ' Women'
    return n


def base_name(n):
    return re.sub(r'\s+Women$', '', n or '')

def log(*a):
    print('[statpad]', *a, flush=True)


def iso(dt):
    return dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ') if dt else None


def parse_iso(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')) if s else None


class Fetched:
    def __init__(self, status, body, headers):
        self.status, self.body, self.headers = status, body, headers


def offline_name(url):
    u = urlsplit(url)
    return re.sub(r'[^A-Za-z0-9.-]+', '_', u.netloc + u.path).strip('_')


def fetch(url, params=None, tries=3, headers=None, timeout=40):
    full = url + ('?' + urlencode(params) if params else '')
    if OFFLINE:
        p = Path(OFFLINE) / offline_name(url)
        if params and 'api-sports' in url:   # test files for API-Football are per query
            p = Path(OFFLINE) / (offline_name(url) + '__' + re.sub(r'[^A-Za-z0-9.-]+', '_', urlencode(sorted(params.items()))))
        if not p.exists():
            return Fetched(404, b'', {})
        hdr = {}
        q = Path(OFFLINE) / 'quota.json'
        if 'the-odds-api' in url and q.exists():
            hdr = json.loads(q.read_text())
        return Fetched(200, p.read_bytes(), hdr)
    last = None
    for i in range(tries):
        try:
            req = Request(full, headers={'User-Agent': 'Statpad/1.0 (GitHub Actions data refresh)', 'Accept-Encoding': 'identity', **(headers or {})})
            with urlopen(req, timeout=timeout) as r:
                return Fetched(r.status, r.read(), {k.lower(): v for k, v in r.headers.items()})
        except HTTPError as e:
            if e.code in (401, 403, 404, 422):
                return Fetched(e.code, e.read() or b'', {k.lower(): v for k, v in (e.headers or {}).items()})
            last = e
        except (URLError, TimeoutError, OSError) as e:
            last = e
        time.sleep(2 * (i + 1))
    log('fetch failed', url, last)
    return Fetched(0, b'', {})


def text_of(body):
    for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode('utf-8', 'ignore')


def read_csv(body):
    rows = list(csv.DictReader(io.StringIO(text_of(body))))
    out = []
    for r in rows:
        r = {(k or '').strip().lstrip('﻿'): (v or '').strip() for k, v in r.items() if k}
        if any(r.values()):
            out.append(r)
    return out


def col(r, *names):
    for n in names:
        v = r.get(n)
        if v not in (None, ''):
            return v
    return None


def fnum(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def parse_date(s):
    s = (s or '').strip()
    for f in ('%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, f).replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


def last_sunday(y, m):
    d = (datetime(y, m + 1, 1, tzinfo=UTC) if m < 12 else datetime(y + 1, 1, 1, tzinfo=UTC)) - timedelta(days=1)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d


def local_to_utc(day, hhmm, base=0, dst=True):
    """Local kick-off time -> UTC. base = winter offset from UTC; dst = clocks go forward in summer (EU rules)."""
    t = hhmm or ''
    m = re.search(r'(\d{1,2})[:.](\d{2})', t)
    h, mi = (int(m.group(1)) % 24, int(m.group(2))) if m else (15, 0)
    local = day.replace(hour=h, minute=mi)
    z = re.search(r'UTC\s*([+-]\d{1,2})', t)
    if z:
        return local - timedelta(hours=int(z.group(1)))
    off = base
    if dst:
        start = last_sunday(day.year, 3).replace(hour=1)
        end = last_sunday(day.year, 10).replace(hour=1)
        if start <= local - timedelta(hours=base) < end:
            off += 1
    return local - timedelta(hours=off)


def uk_to_utc(day, hhmm):
    """football-data times are UK local time (GMT in winter, BST in summer)."""
    return local_to_utc(day, hhmm, 0, True)


def season_code(dt, back=0):
    y = dt.year - (0 if dt.month >= 7 else 1) - back
    return f'{y % 100:02d}{(y + 1) % 100:02d}'


# ----------------------------------------------------------------------------------------------
# Ratings: weighted Poisson model  log(home goals) = mu + home + att[h] + def[a]
#                                  log(away goals) = mu + att[a] + def[h]
# fitted by iterative scaling with exponential time decay and shrinkage towards a prior.
# ----------------------------------------------------------------------------------------------
HALF_LIFE = 180.0   # days
PREV_W = 0.6        # extra down-weight for last season's games
K_SHRINK = 6.0      # pseudo-games of prior per team
NEW_TEAM_PRIOR = (-0.12, 0.12)   # newcomers start a bit below average
# the settings above, plus how much shots on target count next to goals. Replaced by settings tuned on
# past seasons when those beat the defaults on a season the tuning never saw (see "Tuning" below).
DEFAULT_MODEL = {'hl': HALF_LIFE, 'pw': PREV_W, 'k': K_SHRINK, 'sw': 0.0, 'nt': NEW_TEAM_PRIOR[1]}
MODEL = dict(DEFAULT_MODEL)
MODEL0 = dict(DEFAULT_MODEL)   # for leagues without shot counts: goals only, so they need their own shrinkage
MODEL_X = None      # for leagues with commentary xG, when the tuning found their own settings beat the common ones
XG_COVER_LIVE = 0.8     # the share of a league's games with xG before it uses those settings


def model_for(lk, games, shots_ok):
    """The settings a league's ratings use: its own with commentary xG (when tuned and most of its games have xG),
    the common ones with shot counts, or the goals-only ones."""
    if not shots_ok:
        return MODEL0
    if MODEL_X and lk in XG_SLUGS and games:
        n_x = sum(1 for g in games if len(g) > 8 and g[8])
        if n_x >= XG_COVER_LIVE * len(games):
            return MODEL_X
    return MODEL
MODEL_W = None      # the market's share in the blend when tuned; the engine's own default is 0.8

# ----------------------------------------------------------------------------------------------
# Elo: a second, independent read of each team's strength, game by game (World Football Elo style: the swing
# grows with the goal margin), turned into expected goals around the league's own scoring rate and mixed with
# the ratings above in log space. On 19 leagues it lowered the model's error on a season it never saw (1X2 + over/
# under log loss 1.7231 -> 1.7161 with the settings below); the monthly tuning refits it and keeps it only while it
# still wins on the latest completed season.
# ----------------------------------------------------------------------------------------------
ELO_NEW = -60.0                       # newcomers start a little below the league's average
ELO_GRID = [(0.0, 20.0), (20.0, 20.0), (40.0, 20.0), (40.0, 30.0), (65.0, 20.0)]   # (home advantage, K) tried by the tuning
ELO_DEF = {'hfa': 40.0, 'k': 20.0}
ELO = None                            # {'hfa', 'k', 'b', 'c', 'v'} when the tuning switched the mix on


def elo_run(games, hfa, k):
    """Ratings after every game, and each game's gap (home + advantage - away) as it stood before kick-off.
    games: (date, home, away, home goals, away goals, ...)."""
    R, gaps = {}, {}
    for g in sorted(games, key=lambda g: g[0]):
        d, h, a, hg, ag = g[:5]
        for t in (h, a):
            if t not in R:
                R[t] = (sum(R.values()) / len(R) + ELO_NEW) if R else 1500.0
        dr = R[h] + hfa - R[a]
        gaps[(d, h, a)] = dr
        we = 1 / (1 + 10 ** (-dr / 400))
        w = 1.0 if hg > ag else 0.5 if hg == ag else 0.0
        gd = abs(hg - ag)
        delta = k * (1.0 if gd <= 1 else 1.5 if gd == 2 else (11 + gd) / 8) * (w - we)
        R[h] += delta
        R[a] -= delta
    return R, gaps


def elo_lams(dr, hm, am, E):
    """Expected goals (home, away) from an Elo gap, around the league's average home and away goals."""
    s = dr / 400
    return hm * math.exp(E['b'] * s + E['c']), am * math.exp(-E['b'] * s + E['c'])


def elo_mix(lh, la, el, v):
    """The ratings' expected goals mixed with Elo's (share v for the ratings), in log space."""
    return math.exp(v * math.log(lh) + (1 - v) * math.log(el[0])), math.exp(v * math.log(la) + (1 - v) * math.log(el[1]))


# ----------------------------------------------------------------------------------------------
# Game-by-game ratings (a Kalman filter). Each side's attack and defence drift a little every day; each result
# pulls them by how surprising it was, weighed by how sure the ratings were, so a team that changes mid-season is
# picked up faster than by the season-long fit above, and each rating carries its own uncertainty. Mixed with the
# ratings in log space, like Elo, and only when the monthly tuning finds it lowers the error on a season it never saw.
# ----------------------------------------------------------------------------------------------
KAL_DEF = {'q': 0.0001, 'p0': 0.05, 'ps': 0.8, 'qs': 0.01}   # daily drift, starting spread, carry-over between seasons, extra spread then
KAL_SPACE = [('q', 0.000005, 0.002, True), ('p0', 0.005, 0.25, True), ('ps', 0.3, 1.0, False), ('qs', 0.0005, 0.1, True)]
KAL = None   # {'q', 'p0', 'ps', 'qs', 'v'} when the tuning switched the mix on


def kalman_run(games, K, sw=0.0, nt=0.12, sx=0.0):
    """games: (date, home, away, home goals, away goals, this season?, home SoT, away SoT), in any order.
    Returns ({(date, home, away): (home xg, away xg, spread of log home xg, of log away xg)} as each stood before
    kick-off, the final ratings {team: [attack, defence, attack variance, defence variance, last game]}, baseline)."""
    gs = sorted(games, key=lambda g: g[0])
    if len(gs) < 20:
        return {}, {}, None
    q, p0, ps, qs = K['q'], K['p0'], K['ps'], K['qs']
    prev_any = any(not g[5] for g in gs)
    first = [g for g in gs if not g[5]] or gs
    first = first[:min(len(first), 200)]
    mh = sum(g[3] for g in first) / len(first)
    ma = sum(g[4] for g in first) / len(first)
    conv0, cx0 = shot_scales(gs, sw, sx)
    S, pre, n_g = {}, {}, 0
    alpha = 1 / 400   # the league's scoring rate: a running average over roughly a season
    for g in gs:
        d, h, a, hg, ag, cur = g[:6]
        for t in (h, a):
            st = S.get(t)
            if st is None:
                x = nt if cur and prev_any else 0.0
                S[t] = st = [-x, x, p0, p0, d, cur]
            elif cur and not st[5]:   # a new season: ratings drift back a little and grow less sure
                st[0] *= ps
                st[1] *= ps
                st[2] += qs
                st[3] += qs
                st[5] = True
            dt = max(0.0, (d - st[4]).total_seconds() / 86400)
            st[2] += q * dt
            st[3] += q * dt
            st[4] = d
        H, A = S[h], S[a]
        mu, hm = math.log(ma), math.log(mh / ma)
        eh, ea = mu + hm + H[0] + A[1], mu + A[0] + H[1]
        lh, la = math.exp(eh), math.exp(ea)
        pre[(d, h, a)] = (lh, la, math.sqrt(H[2] + A[3]), math.sqrt(A[2] + H[3]))
        yh, ya = goal_mix(g, sw, sx, conv0, cx0) if conv0 or cx0 else (hg, ag)
        # one Newton step from the prior for each side's goals (Poisson, log link), uncertainty kept per rating
        sh_, sa_ = H[2] + A[3], A[2] + H[3]
        gh, ga = (yh - lh) / (1 + lh * sh_), (ya - la) / (1 + la * sa_)
        H0, H1, H2, H3 = H[0], H[1], H[2], H[3]
        A0, A1, A2, A3 = A[0], A[1], A[2], A[3]
        H[0], A[1] = H0 + H2 * gh, A1 + A3 * gh
        A[0], H[1] = A0 + A2 * ga, H1 + H3 * ga
        H[2] = H2 - lh * H2 * H2 / (1 + lh * sh_)
        A[3] = A3 - lh * A3 * A3 / (1 + lh * sh_)
        A[2] = A2 - la * A2 * A2 / (1 + la * sa_)
        H[3] = H3 - la * H3 * H3 / (1 + la * sa_)
        mh += alpha * (yh - mh)
        ma += alpha * (ya - ma)
        n_g += 1
        if n_g % 30 == 0:   # the baseline follows the league's scoring; the ratings stay centred on it
            live = [st for st in S.values() if st[5] == cur]
            ca = sum(st[0] for st in live) / len(live)
            cd = sum(st[1] for st in live) / len(live)
            for st in S.values():
                st[0] -= ca
                st[1] -= cd
    # centre on this season's sides (the baseline carries the league's level)
    cur_t = [t for t, st in S.items() if st[5]] or list(S)
    ca = sum(S[t][0] for t in cur_t) / len(cur_t)
    cd = sum(S[t][1] for t in cur_t) / len(cur_t)
    final = {t: [st[0] - ca, st[1] - cd, st[2], st[3], st[4]] for t, st in S.items()}
    return pre, final, (math.log(ma) + ca + cd, math.log(mh / ma))


def kal_lams(st_h, st_a, base, q, now):
    """Expected goals (home, away) and the spread of their logs from the filter's final ratings, for a game ahead."""
    mu, hm = base
    ph = [st_h[2] + q * max(0.0, (now - st_h[4]).total_seconds() / 86400), st_h[3] + q * max(0.0, (now - st_h[4]).total_seconds() / 86400)]
    pa = [st_a[2] + q * max(0.0, (now - st_a[4]).total_seconds() / 86400), st_a[3] + q * max(0.0, (now - st_a[4]).total_seconds() / 86400)]
    return (math.exp(mu + hm + st_h[0] + st_a[1]), math.exp(mu + st_a[0] + st_h[1]), math.sqrt(ph[0] + pa[1]), math.sqrt(pa[0] + ph[1]))


def goal_means(games, n=500):
    g = sorted(games, key=lambda g: g[0])[-n:]
    return (sum(x[3] for x in g) / len(g), sum(x[4] for x in g) / len(g)) if len(g) >= 40 else (1.45, 1.15)


def shot_scales(games, sw, sx):
    """The league's goals per shot on target and goals per expected goal (commentary xG), each over the games that
    have them; None where the setting is off or there are too few."""
    conv = cx = None
    if sw > 0:
        gs = ss = 0
        for g in games:
            if len(g) > 7 and g[6] is not None and g[7] is not None:
                gs += g[3] + g[4]
                ss += g[6] + g[7]
        conv = gs / ss if ss > 100 else None
    if sx > 0:
        gs = xs = 0.0
        for g in games:
            if len(g) > 8 and g[8]:
                gs += g[3] + g[4]
                xs += g[8][0] + g[8][1]
        cx = gs / xs if xs > 100 else None
    return conv, cx


def goal_mix(g, sw, sx, conv, cx):
    """One game's goals as the ratings count them: (1 - shares) x goals + share x shots on target x goals per shot
    on target + share x expected goals from the commentary (x goals per expected goal). A game without shots or
    xG gives that share back to its goals."""
    hg, ag = g[3], g[4]
    a_s = sw if conv and len(g) > 7 and g[6] is not None and g[7] is not None else 0.0
    a_x = sx if cx and len(g) > 8 and g[8] else 0.0
    if not a_s and not a_x:
        return hg, ag
    t = a_s + a_x
    if t > 1:
        a_s, a_x = a_s / t, a_x / t
    h2 = (1 - a_s - a_x) * hg + (a_s * conv * g[6] if a_s else 0.0) + (a_x * cx * g[8][0] if a_x else 0.0)
    a2 = (1 - a_s - a_x) * ag + (a_s * conv * g[7] if a_s else 0.0) + (a_x * cx * g[8][1] if a_x else 0.0)
    return h2, a2


def fit_ratings(games, current_teams, asof=None, init=None, P=None):
    """games: (date, home, away, home goals, away goals, this season?, home shots on target, away shots on target,
    [expected goals home, away] from the commentary or None). With a shot weight, each side's goals count as
    (1 - w) x goals + w x shots on target x the league's goals-per-shot-on-target, which is steadier than goals
    alone; the commentary's expected goals can take a share the same way. Returns ratings plus standard errors."""
    if len(games) < 20:
        return None
    P = P or MODEL
    ref = asof or NOW
    xi = math.log(2) / P['hl']
    K = P['k']
    sw, sx = P.get('sw') or 0.0, P.get('sx') or 0.0
    teams = sorted({g[1] for g in games} | {g[2] for g in games})
    ix = {t: i for i, t in enumerate(teams)}
    prev_teams = {g[1] for g in games if not g[5]} | {g[2] for g in games if not g[5]}
    conv, cx = shot_scales(games, sw, sx)
    W = []
    for g in games:
        d, h, a, hg, ag, cur = g[:6]
        if conv or cx:
            hg, ag = goal_mix(g, sw, sx, conv, cx)
        age = max(0.0, (ref - d).total_seconds() / 86400)
        W.append((ix[h], ix[a], hg, ag, math.exp(-xi * age) * (1.0 if cur else P['pw'])))
    n = len(teams)
    pa = [0.0] * n
    pd = [0.0] * n
    for t, i in ix.items():
        if prev_teams and t not in prev_teams:
            pa[i], pd[i] = -P.get('nt', NEW_TEAM_PRIOR[1]), P.get('nt', NEW_TEAM_PRIOR[1])
    sh = sum(w * hg for _, _, hg, _, w in W)
    sa = sum(w * ag for _, _, _, ag, w in W)
    sw_ = sum(w for *_, w in W)
    mu = math.log(max(0.3, sa / sw_))
    home = math.log(max(0.3, sh / sw_)) - mu
    att = pa[:]
    dfn = pd[:]
    if init:   # warm start from an earlier fit: converges in a few rounds
        mu, home = init['mu'], init['home']
        for t, (a_, d_) in init['teams'].items():
            if t in ix:
                att[ix[t]], dfn[ix[t]] = a_, d_
    cur_ix = [ix[t] for t in current_teams if t in ix] or list(range(n))
    np = numpy_lib()
    if np:   # the same fit as below, with vector maths
        mu, home, att, dfn, ia, idf = _scale_np(np, np.array(W, dtype=float).reshape(-1, 5), n, pa, pd, att, dfn, mu, home, K, cur_ix, sh, sa)
        return {'mu': round(mu, 5), 'home': round(home, 5), 'games': len(games),
                'teams': {t: [round(float(att[ix[t]]), 5), round(float(dfn[ix[t]]), 5)] for t in teams},
                'se': {t: [round(1 / math.sqrt(ia[ix[t]] + K * math.exp(mu + home / 2)), 4),
                           round(1 / math.sqrt(idf[ix[t]] + K * math.exp(mu + home / 2)), 4)] for t in teams}}
    for _ in range(200):
        g0 = math.exp(mu + home / 2)
        emh, em = math.exp(mu + home), math.exp(mu)
        ed = [math.exp(x) for x in dfn]
        num = [0.0] * n
        den = [0.0] * n
        for h, a, hg, ag, w in W:
            num[h] += w * hg
            den[h] += w * emh * ed[a]
            num[a] += w * ag
            den[a] += w * em * ed[h]
        new_att = [math.log((num[i] + K * g0 * math.exp(pa[i])) / (den[i] + K * g0)) for i in range(n)]
        ea_ = [math.exp(x) for x in new_att]
        num = [0.0] * n
        den = [0.0] * n
        for h, a, hg, ag, w in W:
            num[a] += w * hg
            den[a] += w * emh * ea_[h]
            num[h] += w * ag
            den[h] += w * em * ea_[a]
        new_dfn = [math.log((num[i] + K * g0 * math.exp(pd[i])) / (den[i] + K * g0)) for i in range(n)]
        ma = sum(new_att[i] for i in cur_ix) / len(cur_ix)
        md = sum(new_dfn[i] for i in cur_ix) / len(cur_ix)
        new_att = [x - ma for x in new_att]
        new_dfn = [x - md for x in new_dfn]
        eh = sum(w * math.exp(new_att[h] + new_dfn[a]) for h, a, _, _, w in W)
        ea = sum(w * math.exp(new_att[a] + new_dfn[h]) for h, a, _, _, w in W)
        new_mu = math.log(sa / ea)
        new_home = math.log(sh / eh) - new_mu
        delta = max(max(abs(x - y) for x, y in zip(new_att, att)), max(abs(x - y) for x, y in zip(new_dfn, dfn)),
                    abs(new_mu - mu), abs(new_home - home))
        att, dfn, mu, home = new_att, new_dfn, new_mu, new_home
        if delta < 1e-7:
            break
    # standard errors: the information each rating gets is the (weighted) goals it was expected to explain,
    # plus the prior's pseudo-games
    g0 = math.exp(mu + home / 2)
    emh, em = math.exp(mu + home), math.exp(mu)
    ia, idf = [0.0] * n, [0.0] * n
    for h, a, _, _, w in W:
        lh = emh * math.exp(att[h] + dfn[a])
        la = em * math.exp(att[a] + dfn[h])
        ia[h] += w * lh
        idf[a] += w * lh
        ia[a] += w * la
        idf[h] += w * la
    return {'mu': round(mu, 5), 'home': round(home, 5), 'games': len(games),
            'teams': {t: [round(att[ix[t]], 5), round(dfn[ix[t]], 5)] for t in teams},
            'se': {t: [round(1 / math.sqrt(ia[ix[t]] + K * g0), 4), round(1 / math.sqrt(idf[ix[t]] + K * g0), 4)] for t in teams}}


# ----------------------------------------------------------------------------------------------
# Fast maths. With NumPy the fits above run as vector maths: the very same fits (the same steps in the same
# order, so the same ratings to the 5th decimal), 10-30 times faster, which lets the monthly tuning try far more
# settings. The heavy jobs (tuning, replay, team pages) install it on demand into a scratch folder that isn't
# kept between runs; when that fails, everything still runs in plain Python, only slower.
# ----------------------------------------------------------------------------------------------
_NP = None             # None: not looked for yet; False: not available
_NP_INSTALL = [False]  # an install was tried this run


def numpy_lib(install=False):
    """NumPy, or None. install=True: the run has heavy fitting to do, so fetch it if the runner lacks it (once a run)."""
    global _NP
    if _NP:
        return _NP
    if ENV('STATPAD_NO_NUMPY') == '1':
        _NP = False
        return None
    if _NP is None:
        for v in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):   # small matrices, and worker processes fork
            os.environ.setdefault(v, '1')
        try:
            import numpy
            _NP = numpy
            return numpy
        except Exception:
            _NP = False
    if not install or _NP_INSTALL[0]:
        return None
    _NP_INSTALL[0] = True
    import tempfile
    lib = Path(ENV('STATPAD_PYLIB') or Path(tempfile.gettempdir()) / 'statpad-pylib')
    try:
        import importlib
        import subprocess
        t0 = time.time()
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--no-warn-script-location',
                        '--target', str(lib), 'numpy'], timeout=180, capture_output=True)
        if str(lib) not in sys.path:
            sys.path.insert(0, str(lib))
        importlib.invalidate_caches()
        import numpy
        _NP = numpy
        log(f'fast maths: NumPy {numpy.__version__} installed in {time.time() - t0:.0f} s')
        return numpy
    except Exception as e:
        _NP = False
        log('fast maths: NumPy unavailable, plain Python instead', repr(e)[:120])
        return None


def _scale_np(np, Wa, n, pa, pd, att, dfn, mu, home, K, cur_ix, sh, sa):
    """fit_ratings' iterative scaling and standard errors on arrays. Wa: rows of (home, away, home goals, away goals, weight)."""
    H, A = Wa[:, 0].astype(np.intp), Wa[:, 1].astype(np.intp)
    hg, ag, w = Wa[:, 2], Wa[:, 3], Wa[:, 4]
    bc = np.bincount
    num_a = bc(H, w * hg, n) + bc(A, w * ag, n)   # goals each side scored (weighted): fixed through the fit
    num_d = bc(A, w * hg, n) + bc(H, w * ag, n)   # and conceded
    epa, epd = K * np.exp(np.array(pa)), K * np.exp(np.array(pd))
    att, dfn = np.array(att, dtype=float), np.array(dfn, dtype=float)
    cur = np.array(cur_ix, dtype=np.intp)
    for _ in range(200):
        g0 = math.exp(mu + home / 2)
        emh, em = math.exp(mu + home), math.exp(mu)
        ed = np.exp(dfn)
        den = bc(H, w * emh * ed[A], n) + bc(A, w * em * ed[H], n)
        new_att = np.log((num_a + g0 * epa) / (den + K * g0))
        ea_ = np.exp(new_att)
        den = bc(A, w * emh * ea_[H], n) + bc(H, w * em * ea_[A], n)
        new_dfn = np.log((num_d + g0 * epd) / (den + K * g0))
        new_att -= new_att[cur].mean()
        new_dfn -= new_dfn[cur].mean()
        eh = float((w * np.exp(new_att[H] + new_dfn[A])).sum())
        ea = float((w * np.exp(new_att[A] + new_dfn[H])).sum())
        new_mu = math.log(sa / ea)
        new_home = math.log(sh / eh) - new_mu
        delta = max(float(np.abs(new_att - att).max()), float(np.abs(new_dfn - dfn).max()), abs(new_mu - mu), abs(new_home - home))
        att, dfn, mu, home = new_att, new_dfn, new_mu, new_home
        if delta < 1e-7:
            break
    emh, em = math.exp(mu + home), math.exp(mu)
    lh = w * emh * np.exp(att[H] + dfn[A])
    la = w * em * np.exp(att[A] + dfn[H])
    ia = bc(H, lh, n) + bc(A, la, n)
    idf = bc(A, lh, n) + bc(H, la, n)
    return mu, home, att, dfn, ia.tolist(), idf.tolist()


def fit_series(games, teams, cuts, P):
    """The ratings as they stood at each cut-off (a datetime), each from earlier games only, each fit starting from the
    previous one. {index of the cut: ratings}. With NumPy the games are laid out once and each fit takes the ones
    before its cut; the result is the same as calling fit_ratings for each cut."""
    np = numpy_lib()
    out, init = {}, None
    if not np:
        for i, cut in enumerate(cuts):
            rt = fit_ratings([g for g in games if g[0] < cut], teams, asof=cut, init=init, P=P)
            if rt:
                out[i], init = rt, rt
        return out
    gs = sorted(games, key=lambda g: g[0])
    if len(gs) < 20:
        return out
    names = sorted({g[1] for g in gs} | {g[2] for g in gs})
    ix = {t: i for i, t in enumerate(names)}
    n = len(names)
    t_ref = gs[0][0]
    day = np.array([(g[0] - t_ref).total_seconds() / 86400 for g in gs])
    H = np.array([ix[g[1]] for g in gs], dtype=np.intp)
    A = np.array([ix[g[2]] for g in gs], dtype=np.intp)
    HG = np.array([g[3] for g in gs], dtype=float)
    AG = np.array([g[4] for g in gs], dtype=float)
    CUR = np.array([bool(g[5]) for g in gs])
    has = np.array([len(g) > 7 and g[6] is not None and g[7] is not None for g in gs])
    HS = np.array([g[6] if h_ else 0.0 for g, h_ in zip(gs, has)], dtype=float)
    AS = np.array([g[7] if h_ else 0.0 for g, h_ in zip(gs, has)], dtype=float)
    cum_g = np.cumsum(np.where(has, HG + AG, 0.0))
    cum_s = np.cumsum(HS + AS)
    hasx = np.array([len(g) > 8 and bool(g[8]) for g in gs])
    HX = np.array([g[8][0] if h_ else 0.0 for g, h_ in zip(gs, hasx)], dtype=float)
    AX = np.array([g[8][1] if h_ else 0.0 for g, h_ in zip(gs, hasx)], dtype=float)
    cum_gx = np.cumsum(np.where(hasx, HG + AG, 0.0))
    cum_x = np.cumsum(HX + AX)
    xi, K, sw, sx = math.log(2) / P['hl'], P['k'], P.get('sw') or 0.0, P.get('sx') or 0.0
    cur_set = [ix[t] for t in teams if t in ix]
    for i, cut in enumerate(cuts):
        m = int(np.searchsorted(day, (cut - t_ref).total_seconds() / 86400, 'left'))
        if m < 20:
            continue
        h, a, cur = H[:m], A[:m], CUR[:m]
        seen = (np.bincount(h, minlength=n) + np.bincount(a, minlength=n)) > 0
        prev_seen = (np.bincount(h[~cur], minlength=n) + np.bincount(a[~cur], minlength=n)) > 0
        hg, ag = HG[:m], AG[:m]
        conv = cum_g[m - 1] / cum_s[m - 1] if sw > 0 and cum_s[m - 1] > 100 else None
        cx = cum_gx[m - 1] / cum_x[m - 1] if sx > 0 and cum_x[m - 1] > 100 else None
        if conv or cx:   # as goal_mix, on arrays
            a_s = np.where(has[:m], sw, 0.0) if conv else np.zeros(m)
            a_x = np.where(hasx[:m], sx, 0.0) if cx else np.zeros(m)
            t_ = np.maximum(1.0, a_s + a_x)
            a_s, a_x = a_s / t_, a_x / t_
            hg = (1 - a_s - a_x) * hg + a_s * (conv or 0.0) * HS[:m] + a_x * (cx or 0.0) * HX[:m]
            ag = (1 - a_s - a_x) * ag + a_s * (conv or 0.0) * AS[:m] + a_x * (cx or 0.0) * AX[:m]
        ref = (cut - t_ref).total_seconds() / 86400
        w = np.exp(-xi * np.maximum(0.0, ref - day[:m])) * np.where(cur, 1.0, P['pw'])
        pa, pd = np.zeros(n), np.zeros(n)
        if prev_seen.any():
            newc = seen & ~prev_seen
            pa[newc], pd[newc] = -P.get('nt', NEW_TEAM_PRIOR[1]), P.get('nt', NEW_TEAM_PRIOR[1])
        sh, sa, s_w = float((w * hg).sum()), float((w * ag).sum()), float(w.sum())
        mu = math.log(max(0.3, sa / s_w))
        home = math.log(max(0.3, sh / s_w)) - mu
        att, dfn = pa.copy(), pd.copy()
        if init:
            mu, home = init['mu'], init['home']
            for t, (a_, d_) in init['teams'].items():
                if t in ix:
                    att[ix[t]], dfn[ix[t]] = a_, d_
        cur_ix = [j for j in cur_set if seen[j]] or [j for j in range(n) if seen[j]]
        Wa = np.column_stack((h, a, hg, ag, w))
        mu, home, att, dfn, ia, idf = _scale_np(np, Wa, n, pa, pd, att, dfn, mu, home, K, cur_ix, sh, sa)
        g0 = math.exp(mu + home / 2)
        present = [t for t in names if seen[ix[t]]]
        rt = {'mu': round(mu, 5), 'home': round(home, 5), 'games': m,
              'teams': {t: [round(float(att[ix[t]]), 5), round(float(dfn[ix[t]]), 5)] for t in present},
              'se': {t: [round(1 / math.sqrt(ia[ix[t]] + K * g0), 4), round(1 / math.sqrt(idf[ix[t]] + K * g0), 4)] for t in present}}
        out[i], init = rt, rt
    return out


def build_table(games):
    T = {}
    for g in sorted(games, key=lambda g: g[0]):
        d, h, a, hg, ag, cur = g[:6]
        if not cur:
            continue
        for t in (h, a):
            T.setdefault(t, {'p': 0, 'w': 0, 'd': 0, 'l': 0, 'gf': 0, 'ga': 0, 'pts': 0, 'form': ''})
        for t, gf, ga in ((h, hg, ag), (a, ag, hg)):
            r = T[t]
            r['p'] += 1
            r['gf'] += gf
            r['ga'] += ga
            res = 'W' if gf > ga else 'D' if gf == ga else 'L'
            r[res.lower()] += 1
            r['pts'] += 3 if res == 'W' else 1 if res == 'D' else 0
            r['form'] = (r['form'] + res)[-5:]
    rows = sorted(T.items(), key=lambda kv: (-kv[1]['pts'], -(kv[1]['gf'] - kv[1]['ga']), -kv[1]['gf'], kv[0]))
    return rows


# ----------------------------------------------------------------------------------------------
# Step 1: results -> tables + ratings
# ----------------------------------------------------------------------------------------------
def load_results():
    out = {}
    cur_code, prev_code = season_code(NOW), season_code(NOW, 1)
    for lk, c in L.items():
        if not c['fd']:
            continue
        games = []
        season_label = None
        files = []
        for code, is_cur in ((cur_code, True), (prev_code, False)):
            r = fetch(f'{FD}/mmz4281/{code}/{c["fd"]}.csv')
            files.append((code, is_cur, r))
        have_cur = any(r.status == 200 and r.body for code, is_cur, r in files if is_cur)
        for code, is_cur, r in files:
            if r.status != 200 or not r.body:
                continue
            cur = is_cur or not have_cur   # pre-season: last season stands in as "current"
            if cur:
                season_label = f'20{code[:2]}/{code[2:]}'
            for row in read_csv(r.body):
                d = parse_date(col(row, 'Date'))
                h, a = col(row, 'HomeTeam', 'Home'), col(row, 'AwayTeam', 'Away')
                hg, ag = fnum(col(row, 'FTHG', 'HG')), fnum(col(row, 'FTAG', 'AG'))
                hst, ast = fnum(col(row, 'HST')), fnum(col(row, 'AST'))
                if d and h and a and hg is not None and ag is not None:
                    games.append((d, h, a, int(hg), int(ag), cur, hst, ast, xg_for(lk, d, h, a)))
        if games:
            out[lk] = (games, season_label)
        else:
            log('no results for', lk)
    # extra leagues: one file per country with every season
    for lk, c in L.items():
        if not c['x']:
            continue
        r = fetch(f'{FD}/new/{c["x"]}.csv')
        if r.status != 200 or not r.body:
            log('no results for', lk)
            continue
        rows = read_csv(r.body)
        parsed = []
        for row in rows:
            d = parse_date(col(row, 'Date'))
            h, a = col(row, 'Home', 'HomeTeam'), col(row, 'Away', 'AwayTeam')
            hg, ag = fnum(col(row, 'HG', 'FTHG')), fnum(col(row, 'AG', 'FTAG'))
            if d and h and a and hg is not None and ag is not None and (NOW - d).days < 600:
                parsed.append((d, h, a, int(hg), int(ag), col(row, 'Season') or ''))
        if not parsed:
            continue
        latest = max(parsed, key=lambda g: g[0])[5]
        games = [(d, h, a, hg, ag, s == latest, None, None) for d, h, a, hg, ag, s in parsed]
        out[lk] = (games, latest)
    return out


def compute_leagues(results, teams):
    leagues = {}
    for lk, (games, season) in results.items():
        table = build_table(games)
        cur_teams = [t for t, _ in table]
        cur_n = sum(1 for g in games if g[5])
        with_shots = sum(1 for g in games if g[5] and len(g) > 7 and g[6] is not None)
        P_ = model_for(lk, games, cur_n and with_shots >= 0.8 * cur_n)
        rt = fit_ratings(games, cur_teams, P=P_)
        for t in cur_teams:
            teams.add(lk, t)
        kal = kbase = None
        if KAL:   # the game-by-game ratings, when the tuning switched their mix on
            _, fin, kbase = kalman_run(games, KAL, P_.get('sw') or 0.0, P_.get('nt', NEW_TEAM_PRIOR[1]), P_.get('sx') or 0.0)
            kal = {t: [round(v[0], 4), round(v[1], 4), round(v[2], 5), round(v[3], 5), v[4].strftime('%Y-%m-%d')] for t, v in fin.items() if t in cur_teams}
        cur_d = [g[0] for g in games if g[5]]
        rows = []
        for pos, (t, r) in enumerate(table, 1):
            ar = (rt or {}).get('teams', {}).get(t)
            rows.append([t, r['p'], r['w'], r['d'], r['l'], r['gf'], r['ga'], r['pts'], r['form'],
                         ar[0] if ar else None, ar[1] if ar else None])
        E = ELO or ELO_DEF
        er, _ = elo_run(games, E['hfa'], E['k'])
        leagues[lk] = {'season': season, 'table': rows, 'elo': {t: round(v, 1) for t, v in er.items() if t in cur_teams},
                       'gm': [round(x, 4) for x in goal_means(games)],
                       'mu': rt['mu'] if rt else None, 'home': rt['home'] if rt else None,
                       'ratings': {t: v for t, v in (rt or {}).get('teams', {}).items() if t in cur_teams},
                       'se': {t: v for t, v in (rt or {}).get('se', {}).items() if t in cur_teams},
                       'shots': sum(1 for g in games if g[5] and len(g) > 7 and g[6] is not None),
                       'games': len(games), 'start': min(cur_d).strftime('%Y-%m-%d') if cur_d else None}
        if kal:
            leagues[lk]['kal'], leagues[lk]['kbase'] = kal, [round(kbase[0], 5), round(kbase[1], 5)]
    return leagues


# ----------------------------------------------------------------------------------------------
# Team pages and the season simulator: for each league, every team's ratings week by week this season,
# its results with the chances Statpad's ratings gave before each game, and the fixtures still to play
# ----------------------------------------------------------------------------------------------
TEAMS_BUDGET_S = 180
# leagues where every team plays every other once at home and once away, with no split: the season simulator works there
SIM_LEAGUES = {'EPL', 'LaLiga', 'BUN', 'SA', 'L1', 'POR', 'ERE', 'ENG2', 'GER2', 'ITA2', 'ESP2', 'FRA2', 'ENG3', 'ENG4',
               'TUR1', 'NED2', 'KSA', 'POL1', 'NOR1', 'SWE1', 'BRA1', 'JPN1', 'CHN1'}


def team_file(lk, games, season):
    cur = sorted([g for g in games if g[5]], key=lambda g: g[0])
    if len(cur) < 6:
        return None
    shots = sum(1 for g in cur if len(g) > 7 and g[6] is not None)
    P = model_for(lk, games, shots >= 0.8 * len(cur))
    teams = sorted({g[1] for g in cur} | {g[2] for g in cur})
    start = cur[0][0].replace(hour=0, minute=0, second=0, microsecond=0)
    cuts = []
    while True:   # the ratings at the start of each week of the season, from earlier games only
        cuts.append(min(NOW, start + timedelta(days=7 * len(cuts))))
        if cuts[-1] >= NOW:
            break
    fits = fit_series(games, teams, cuts, P)
    series = {t: [] for t in teams}
    for w, rt in sorted(fits.items()):
        d = min(NOW, start + timedelta(days=7 * w)).strftime('%Y-%m-%d')
        for t in teams:
            if t in rt['teams']:
                series[t].append([d, round(rt['teams'][t][0], 3), round(rt['teams'][t][1], 3)])
    rows = {t: [] for t in teams}
    r3 = lambda x: None if x is None else round(x, 3)
    for g in cur:
        d, h, a, hg, ag = g[:5]
        rt = fits.get((d - start).days // 7)
        pH = pD = pA = None
        if rt and h in rt['teams'] and a in rt['teams']:
            (ah, dh), (aa, da) = rt['teams'][h], rt['teams'][a]
            pH, pD, pA, _ = dc_probs(math.exp(rt['mu'] + rt['home'] + ah + da), math.exp(rt['mu'] + aa + dh), rho=rho_for(lk))
        hs, as_ = (g[6], g[7]) if len(g) > 7 else (None, None)
        hs, as_ = (None if hs is None else int(hs)), (None if as_ is None else int(as_))
        ds = d.strftime('%Y-%m-%d')
        rows[h].append([ds, disp(lk, a), 1, hg, ag, r3(pH), r3(pD), r3(pA), hs, as_])
        rows[a].append([ds, disp(lk, h), 0, ag, hg, r3(pA), r3(pD), r3(pH), as_, hs])
    out = {'app': 'Statpad', 'lk': lk, 'season': season, 'gen': iso(NOW), 'sh': 1 if P is not MODEL0 and shots else 0,
           't': {disp(lk, t): {'s': series[t], 'r': rows[t]} for t in teams}}
    if lk in SIM_LEAGUES:
        played = {(g[1], g[2]) for g in cur}
        n = len(teams)
        if len(played) == len(cur) and len(cur) <= n * (n - 1):   # no pair has met twice at the same ground
            idx = {t: i for i, t in enumerate(teams)}
            out['teams'] = [disp(lk, t) for t in teams]
            out['rem'] = [[idx[h], idx[a]] for h in teams for a in teams if h != a and (h, a) not in played]
    return out


def team_files(res):
    numpy_lib(True)   # heavy fitting ahead: fast maths if it can be had
    t0, out = time.time(), {}
    for lk, (games, season) in res.items():
        if knockout(lk):
            continue
        if time.time() - t0 > TEAMS_BUDGET_S:
            log('team pages: out of time, the rest next time')
            break
        try:
            got = team_file(lk, games, season)
        except Exception as e:
            log('team pages:', lk, repr(e))
            got = None
        if got:
            out[lk] = got
    log(f'team pages: {len(out)} leagues in {time.time() - t0:.0f} s')
    return out


def last_played(res):
    """The date of each team's latest league game this season (for rest days)."""
    out = {}
    for lk, (games, _season) in res.items():
        if knockout(lk):
            continue
        d = out.setdefault(lk, {})
        for g in games:
            if g[5]:
                for t in (g[1], g[2]):
                    k = disp(lk, t)
                    ds = g[0].strftime('%Y-%m-%d')
                    if ds > d.get(k, ''):
                        d[k] = ds
    return out


# ----------------------------------------------------------------------------------------------
# What's at stake: the rest of each league season is played thousands of times (as the page's season
# simulator does), and for every game in the coming days Statpad measures how much each team's title,
# top places or relegation chances swing on the result. The same simulation, run week by week over past
# seasons, tells the model when a team has nothing left to play for.
# ----------------------------------------------------------------------------------------------
STAKES_EVERY_H = 1
STAKES_N = 10000
SIM_SIG = 0.08          # each simulated season nudges every team's strength: ratings are never exact
ZONES = {'ENG2': [('Top 2', 1, 2), ('Top 6', 1, 6), ('Bottom 3', -3, 0)], 'ENG3': [('Top 2', 1, 2), ('Top 6', 1, 6), ('Bottom 4', -4, 0)],
         'ENG4': [('Top 3', 1, 3), ('Top 7', 1, 7), ('Bottom 2', -2, 0)], 'ESP2': [('Top 2', 1, 2), ('Top 6', 1, 6), ('Bottom 4', -4, 0)],
         'ITA2': [('Top 2', 1, 2), ('Top 8', 1, 8), ('Bottom 3', -3, 0)], 'GER2': [('Top 2', 1, 2), ('Top 3', 1, 3), ('Bottom 3', -3, 0)],
         'FRA2': [('Top 2', 1, 2), ('Top 5', 1, 5), ('Bottom 3', -3, 0)]}


def zones_for(lk):
    """The zones the page shows (as in its season simulator), plus a hidden European-places zone in top
    divisions that only the motivation signal uses."""
    z = ZONES.get(lk) or ([('Top 2', 1, 2), ('Top 6', 1, 6), ('Bottom 3', -3, 0)] if L[lk]['group'] == 'second'
                          else [('Title', 1, 1), ('Top 4', 1, 4), ('Bottom 3', -3, 0)])
    z = [[name, a, b, 0] for name, a, b in z]
    if L[lk]['group'] != 'second':
        z.append(['Top 6', 1, 6, 1])
    return z


NODE_SIM = r"""
const fs = require('fs');
const [, , htmlPath, inPath] = process.argv;
const inp = JSON.parse(fs.readFileSync(inPath, 'utf8'));
let E = null;
if (inp.engine) {
  try {
    const html = fs.readFileSync(htmlPath, 'utf8');
    const s = html.indexOf('/* Statpad engine'), e = html.indexOf('module.exports = ENGINE;', s);
    if (s >= 0 && e >= 0) { const mod = { exports: null }; new Function('module', html.slice(s, e) + 'module.exports = ENGINE;')(mod); E = mod.exports; if (E.setCalib) E.setCalib(null); }
  } catch (err) { E = null; }
}
const now = inp.now ? Date.parse(inp.now) : Date.now();
function rng(seed) { let a = seed >>> 0; return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
function job(J) {
  const R = rng(J.seed || 1), n = J.base.length, Z = J.zones, nzn = Z.length, FX = J.fx, F = FX.length, N = J.n, sig = J.sig;
  if (E && J.ms) for (const [k, m] of J.ms) { try { const a = E.analyse(m, now); if (a && a.lh > 0 && a.la > 0) { FX[k][2] = a.lh; FX[k][3] = a.la; } } catch (err) {} }
  const T = J.tgt, nt = T.length;
  const cnt = new Float64Array(nt * 3), inz = new Float64Array(nt * 6 * nzn);
  const pts = new Int32Array(n), gd = new Int32Array(n), gf = new Int32Array(n), nz = new Float64Array(2 * n), tie = new Float64Array(n);
  const order = [...Array(n).keys()], pos = new Int32Array(n), res = new Int8Array(F);
  const pois = (l) => { const L = Math.exp(-l); let k = 0, p = R(); while (p > L) { k++; p *= R(); } return k; };
  const gauss = () => { let u = 0; while (!u) u = R(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * R()); };
  const inZone = (p, z) => (z[1] < 0 ? p >= n + z[1] : p >= z[1] - 1 && p <= z[2] - 1);
  for (let s = 0; s < N; s++) {
    for (let t = 0; t < n; t++) { pts[t] = J.base[t][0]; gf[t] = J.base[t][1]; gd[t] = J.base[t][1] - J.base[t][2]; nz[2 * t] = gauss() * sig; nz[2 * t + 1] = gauss() * sig; tie[t] = R(); }
    for (let k = 0; k < F; k++) {
      const f = FX[k], i = f[0], j = f[1];
      const h = pois(f[2] * Math.exp(nz[2 * i] + nz[2 * j + 1])), a = pois(f[3] * Math.exp(nz[2 * j] + nz[2 * i + 1]));
      gf[i] += h; gf[j] += a; gd[i] += h - a; gd[j] += a - h;
      if (h > a) { pts[i] += 3; res[k] = 0; } else if (h < a) { pts[j] += 3; res[k] = 2; } else { pts[i]++; pts[j]++; res[k] = 1; }
    }
    order.sort((x, y) => pts[y] - pts[x] || gd[y] - gd[x] || gf[y] - gf[x] || tie[y] - tie[x]);
    for (let p = 0; p < n; p++) pos[order[p]] = p;
    for (let q = 0; q < nt; q++) {
      const k = T[q], o = res[k], f = FX[k];
      cnt[q * 3 + o]++;
      for (let side = 0; side < 2; side++) {
        const p = pos[side ? f[1] : f[0]], b = ((q * 2 + side) * 3 + o) * nzn;
        for (let z = 0; z < nzn; z++) if (inZone(p, Z[z])) inz[b + z]++;
      }
    }
  }
  const out = [];
  for (let q = 0; q < nt; q++) {
    const c = [cnt[q * 3], cnt[q * 3 + 1], cnt[q * 3 + 2]], tot = c[0] + c[1] + c[2];
    const sides = [];
    for (let side = 0; side < 2; side++) {
      let best = null, mx = 0;
      for (let z = 0; z < nzn; z++) {
        const v = [0, 1, 2].map((o) => inz[((q * 2 + side) * 3 + o) * nzn + z]);
        const all = (v[0] + v[1] + v[2]) / tot;
        const pz = [0, 1, 2].map((o) => (c[o] >= 30 ? v[o] / c[o] : all));
        const win = side ? pz[2] : pz[0], loss = side ? pz[0] : pz[2], sw = Math.abs(win - loss);
        if (sw > mx) mx = sw;
        if (!Z[z][3] && (!best || sw > best[5])) best = [z, all, win, pz[1], loss, sw];
      }
      sides.push([best, mx]);
    }
    out.push(sides);
  }
  return out;
}
const out = inp.jobs.map((J) => { try { return job(J); } catch (err) { return null; } });
process.stdout.write(JSON.stringify(out));
"""


def sim_seed(*parts):
    h = 2166136261
    for ch in '|'.join(str(x) for x in parts):
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h


def round_robin(cur):
    """True when no pair of teams has met twice at the same ground: a league the simulator can play out."""
    teams = {g[1] for g in cur} | {g[2] for g in cur}
    n = len(teams)
    return n >= 6 and len({(g[1], g[2]) for g in cur}) == len(cur) and len(cur) <= n * (n - 1)


def stakes_jobs(state, doc):
    """One simulation per league with a team file: the table now, every game still to play (priced games
    use the engine's expected goals, the rest the ratings), and the coming games to measure."""
    jobs, meta = [], []
    upcoming = {}
    for m in doc['matches']:
        if parse_iso(m['ko']) > NOW and m['lk'] in SIM_LEAGUES:
            upcoming.setdefault(m['lk'], {})[(m['h'], m['a'])] = m
    for lk, tf in (state.get('teamfiles') or {}).items():
        if lk not in upcoming or not tf.get('rem') or not tf.get('teams'):
            continue
        lg = (doc.get('leagues') or {}).get(lk) or {}
        rows = {r[0]: r for r in lg.get('table') or []}
        names = tf['teams']
        if not all(nm in rows for nm in names) or lg.get('mu') is None:
            continue
        mu, home = lg['mu'], lg.get('home') or 0.25
        rr = [rows[nm] for nm in names]
        att = [r[9] or 0.0 for r in rr]
        dfn = [r[10] or 0.0 for r in rr]
        fx, ms, tgt = [], [], []
        for k, (i, j) in enumerate(tf['rem']):
            fx.append([i, j, round(math.exp(mu + home + att[i] + dfn[j]), 4), round(math.exp(mu + att[j] + dfn[i]), 4)])
            m = upcoming[lk].get((names[i], names[j]))
            if m:
                ms.append([k, {x: v for x, v in m.items() if x not in ('hist', 'tn', 'sk', 'fx', 'fxw')}])
                tgt.append(k)
        if not tgt:
            continue
        jobs.append({'n': STAKES_N, 'sig': SIM_SIG, 'seed': sim_seed(lk, NOW.strftime('%Y-%m-%d')), 'zones': zones_for(lk),
                     'base': [[r[7], r[5], r[6]] for r in rr], 'fx': fx, 'ms': ms, 'tgt': tgt})
        meta.append((lk, [upcoming[lk][(names[tf['rem'][k][0]], names[tf['rem'][k][1]])]['id'] for k in tgt]))
    return jobs, meta


def refresh_stakes(state, doc):
    """Each coming league game's stakes, refreshed hourly (the simulation is seeded, so the same inputs give
    the same answer and don't trigger a redeploy)."""
    cache = state.get('stakes') or {}
    ids = {m['id'] for m in doc['matches'] if parse_iso(m['ko']) > NOW and m['lk'] in SIM_LEAGUES}
    seen = set(state.get('stakes_seen') or [])
    if due(state, 'stakes', STAKES_EVERY_H) or state.get('stakes_teams') != state.get('teams_sig') or not ids <= seen:
        t0 = time.time()
        state.setdefault('last', {})['stakes'] = iso(NOW)
        state['stakes_seen'] = sorted(ids)
        state['stakes_teams'] = state.get('teams_sig')
        jobs, meta = stakes_jobs(state, doc)
        got = run_node(NODE_SIM, {'engine': True, 'now': iso(NOW), 'jobs': jobs}, timeout=300) if jobs else []
        if got is not None:
            cache = {}
            for (lk, mids), res in zip(meta, got):
                if not res:
                    continue
                zn = zones_for(lk)
                for mid, sides in zip(mids, res):
                    rec, score = [], 0.0
                    for best, _mx in sides:
                        if best and best[5] >= 0.005:
                            rec.append([zn[best[0]][0]] + [int(round(x * 1000)) for x in best[1:5]])
                            score += best[5]
                        else:
                            rec.append(None)
                    cache[mid] = {'sk': [int(round(score * 100))] + rec, 'mx': [round(sides[0][1], 4), round(sides[1][1], 4)]}
            state['stakes'] = cache
            log(f'stakes: {len(cache)} games in {len(jobs)} leagues ({time.time() - t0:.1f} s)')
    for m in doc['matches']:
        c = cache.get(m['id'])
        if c and parse_iso(m['ko']) > NOW:
            m['sk'] = c['sk']
    return cache


def stack_ok(m):
    """Games the learned blend was fitted on the like of: league games, both sides rated in the league, several bookmakers' prices."""
    mk = m.get('mkt') or {}
    return bool(STACK and m.get('lm') and mk.get('p') and not m.get('xl') and not m.get('fr') and not knockout(m['lk'])
                and L.get(m['lk'], {}).get('group') in ('big', 'second', 'more') and (mk.get('n') or 0) != 1)


def apply_stack(state, doc):
    """The learned blend for the coming games, when the tuning switched it on: per side, the weight on the model's
    log expected goals against the bookmakers' and a shift, from how thin the ratings are, the time of season, the
    kind of league and short rest. The engine applies them."""
    if not STACK:
        return 0
    lp, lgs, n = state.get('last_played') or {}, state.get('leagues') or {}, 0
    for m in doc['matches']:
        m.pop('st', None)
        if not stack_ok(m) or parse_iso(m['ko']) <= NOW or (state.get('guard') or {}).get(lc_group(m['lk'])):
            continue
        ko, d = datetime.strptime(m['ko'][:10], '%Y-%m-%d'), lp.get(m['lk']) or {}

        def rest(t):
            x = d.get(t)
            r = (ko - datetime.strptime(x, '%Y-%m-%d')).days if x else None
            return r if r and r > 0 else None
        st = (lgs.get(m['lk']) or {}).get('start')
        week = (ko - datetime.strptime(st, '%Y-%m-%d')).days // 7 if st else None
        ls = m.get('ls') or [None, None]
        grp = L[m['lk']]['group']
        rh, ra = rest(m['h']), rest(m['a'])
        bh, ch = stack_coefs(STACK['b'], ls[0], week, grp, rh, ra)
        ba, ca = stack_coefs(STACK['b'], ls[1], week, grp, ra, rh)
        m['st'] = [round(bh, 4), round(ch, 4), round(ba, 4), round(ca, 4)]
        m.pop('wm', None)
        n += 1
    return n


def apply_fx(state, doc):
    """Fatigue and motivation for the coming league games, when the tuning switched them on."""
    lp = state.get('last_played') or {}
    for m in doc['matches']:   # games that have kicked off count as played, before football-data lists them
        if knockout(m['lk']) or parse_iso(m['ko']) > NOW:
            continue
        d, ds = lp.setdefault(m['lk'], {}), m['ko'][:10]
        for t in (m['h'], m['a']):
            if ds > d.get(t, ''):
                d[t] = ds
    state['last_played'] = lp
    if not (FX['rest'] or FX['dead'] or FX['open']):
        return 0
    stakes, tfs, n = state.get('stakes') or {}, state.get('teamfiles') or {}, 0
    for m in doc['matches']:
        if knockout(m['lk']) or parse_iso(m['ko']) <= NOW:
            continue
        ko, d = datetime.strptime(m['ko'][:10], '%Y-%m-%d'), lp.get(m['lk']) or {}

        def rest(t):
            x = d.get(t)
            r = (ko - datetime.strptime(x, '%Y-%m-%d')).days if x else None
            return r if r and r > 0 else None
        rh, ra = rest(m['h']), rest(m['a'])
        dh = da = None
        st, tf = stakes.get(m['id']), tfs.get(m['lk']) or {}
        if (FX['dead'] or FX['open']) and st and tf.get('teams') and tf.get('rem') is not None:
            nt = len(tf['teams'])
            if len(tf['rem']) <= 0.5 * nt * (nt - 1):   # as in the fit: only in the second half of the season
                dh, da = st['mx']
        xh, xa = fx_shift(rh, ra, dh, da)
        if abs(xh) > 1e-4 or abs(xa) > 1e-4:
            m['fx'] = [round(math.exp(xh), 4), round(math.exp(xa), 4)]
            why = {}
            if FX['rest'] and tired(rh, FX['rest'][1]) != tired(ra, FX['rest'][1]):
                why['r'] = [rh, ra, 1 if FX['rest'][0] > 0 else -1]
            if FX['dead'] and is_dead(dh, FX['dead'][1]) != is_dead(da, FX['dead'][1]):
                why['d'] = [int(is_dead(dh, FX['dead'][1])), int(is_dead(da, FX['dead'][1])), 1 if FX['dead'][0] > 0 else -1]
            if FX['open'] and is_dead(dh, FX['open'][1]) and is_dead(da, FX['open'][1]):
                why['o'] = 1
            m['fxw'] = why
            n += 1
    return n


# ----------------------------------------------------------------------------------------------
# Step 2: football-data fixtures (with average / best prices)
# ----------------------------------------------------------------------------------------------
def fd_market(row, upd):
    avg = [fnum(col(row, 'AvgH', 'AvgCH', 'BbAvH', 'PSH', 'B365H')), fnum(col(row, 'AvgD', 'AvgCD', 'BbAvD', 'PSD', 'B365D')),
           fnum(col(row, 'AvgA', 'AvgCA', 'BbAvA', 'PSA', 'B365A'))]
    if not all(x and x > 1 for x in avg):
        return None
    mx = [fnum(col(row, 'MaxH', 'MaxCH', 'BbMxH')), fnum(col(row, 'MaxD', 'MaxCD', 'BbMxD')), fnum(col(row, 'MaxA', 'MaxCA', 'BbMxA'))]
    best = [round(max(m or 0, a), 3) for m, a in zip(mx, avg)]
    ps = [fnum(col(row, 'PSH')), fnum(col(row, 'PSD')), fnum(col(row, 'PSA'))]
    p = demargin(avg)
    if all(x and x > 1 for x in ps):   # Pinnacle's prices, when listed, count three times (as in the live consensus)
        pp = demargin(ps)
        p = [(3 * a + b) / 4 for a, b in zip(pp, p)]
    m = {'p': [round(x, 4) for x in p], 'avg': avg, 'best': best, 'bk': None, 'n': None,
         'ou': None, 'src': 'football-data', 'upd': upd}
    o, u = fnum(col(row, 'Avg>2.5', 'AvgC>2.5', 'BbAv>2.5', 'B365>2.5')), fnum(col(row, 'Avg<2.5', 'AvgC<2.5', 'BbAv<2.5', 'B365<2.5'))
    if o and u and o > 1 and u > 1:
        mo, mu_ = fnum(col(row, 'Max>2.5', 'MaxC>2.5', 'BbMx>2.5')), fnum(col(row, 'Max<2.5', 'MaxC<2.5', 'BbMx<2.5'))
        m['ou'] = {'line': 2.5, 'pO': round(demargin([o, u])[0], 4), 'avg': [o, u],
                   'best': [round(max(mo or 0, o), 3), round(max(mu_ or 0, u), 3)]}
    return m


def fd_close(row):
    """The closing consensus in a past season's row (football-data's closing columns): chances for home, draw, away
    and over 2.5, read like the live consensus. Used to measure closing-line value in the replay."""
    avg = [fnum(col(row, 'AvgCH')), fnum(col(row, 'AvgCD')), fnum(col(row, 'AvgCA'))]
    if not all(x and x > 1 for x in avg):
        return None
    p = demargin(avg)
    ps = [fnum(col(row, 'PSCH')), fnum(col(row, 'PSCD')), fnum(col(row, 'PSCA'))]
    if all(x and x > 1 for x in ps):
        p = [(3 * a + b) / 4 for a, b in zip(demargin(ps), p)]
    out = {'H': p[0], 'D': p[1], 'A': p[2]}
    o, u = fnum(col(row, 'AvgC>2.5')), fnum(col(row, 'AvgC<2.5'))
    if o and u and o > 1 and u > 1:
        out['O25'] = demargin([o, u])[0]
        out['U25'] = 1 - out['O25']
    return {k: round(v, 4) for k, v in out.items()}


def load_fd_fixtures():
    out = []
    upd = iso(NOW)
    r = fetch(f'{FD}/fixtures.csv')
    ok_main = r.status == 200 and bool(r.body)
    if ok_main:
        for row in read_csv(r.body):
            lk = FD_DIV.get(col(row, 'Div') or '')
            d = parse_date(col(row, 'Date'))
            h, a = col(row, 'HomeTeam', 'Home'), col(row, 'AwayTeam', 'Away')
            if lk and d and h and a:
                out.append({'lk': lk, 'ko': iso(uk_to_utc(d, col(row, 'Time'))), 'h': h, 'a': a, 'mkt': fd_market(row, upd)})
    r2 = fetch(f'{FD}/new_league_fixtures.csv')
    ok_new = r2.status == 200 and bool(r2.body)
    if ok_new:
        for row in read_csv(r2.body):
            lk = X_COUNTRY.get(col(row, 'Country') or '')
            d = parse_date(col(row, 'Date'))
            h, a = col(row, 'Home', 'HomeTeam'), col(row, 'Away', 'AwayTeam')
            if lk and d and h and a:
                out.append({'lk': lk, 'ko': iso(uk_to_utc(d, col(row, 'Time'))), 'h': h, 'a': a, 'mkt': fd_market(row, upd)})
    return out, ok_main or ok_new


def of_season(dt):
    y = dt.year if dt.month >= 7 else dt.year - 1
    return f'{y}-{(y + 1) % 100:02d}'


KEEP_SUFFIX = {'Paris FC'}


def of_name(n):
    n = (n or '').strip()
    return n if n in KEEP_SUFFIX else re.sub(r'\s+(FC|AFC|CF)$', '', n)


def load_openfootball():
    """Full-season schedules (no key needed) so upcoming games show before football-data's weekly file."""
    out, ok = [], False
    season = of_season(NOW)
    for lk, (code, base, dst) in OF_MAP.items():
        r = fetch(f'{OF_BASE}/{season}/{code}.json', tries=1)
        if r.status != 200 or not r.body:
            continue
        try:
            d = json.loads(text_of(r.body))
        except ValueError:
            continue
        ok = True
        for m in d.get('matches') or []:
            if not isinstance(m, dict):
                continue
            sc = m.get('score')
            if sc and (not isinstance(sc, dict) or sc.get('ft')):
                continue
            day = parse_date(m.get('date'))
            if not day or not (NOW - timedelta(days=1) <= day <= NOW + timedelta(days=DAYS + 1)):
                continue
            h, a = of_name(m.get('team1')), of_name(m.get('team2'))
            if h and a:
                out.append({'lk': lk, 'ko': iso(local_to_utc(day, m.get('time'), base, dst)), 'h': h, 'a': a,
                            'tbc': not m.get('time')})
    return out, ok


# ----------------------------------------------------------------------------------------------
# Steps 3-4: The Odds API
# ----------------------------------------------------------------------------------------------
def oa_get(path, params, state):
    params = dict(params)
    params['apiKey'] = API_KEY
    r = fetch(f'{OA}{path}', params)
    rem, used = r.headers.get('x-requests-remaining'), r.headers.get('x-requests-used')
    if rem is not None:
        q = state.setdefault('quota', {})
        q['remaining'] = fnum(rem)
        q['used'] = fnum(used)
        q['at'] = iso(NOW)
    if r.status == 401:
        state['odds_block_until'] = iso(NOW + timedelta(hours=6))
        log('The Odds API refused the key (invalid or out of credits); pausing odds for 6 h')
        return None
    if r.status != 200:
        log('Odds API', path, 'status', r.status)
        return None
    try:
        return json.loads(text_of(r.body))
    except ValueError:
        return None


def sport_plan(state, quiet=False):
    """Which Odds API sport keys to follow -> {key: (lk, title, group)}."""
    sports = state.get('oa_sports') or []
    plan = {}
    listed = {s.get('key') for s in sports}
    for s in sports:
        key = s.get('key') or ''
        if s.get('group') != 'Soccer' or s.get('has_outrights') or 'winner' in key:
            continue
        if key in OA_KEY:
            plan[key] = OA_KEY[key]
        elif intl_lk(key, s.get('title')):
            plan[key] = intl_lk(key, s.get('title'))
        elif SPORTS_MODE == 'all' or auto_competition(key):
            lk = 'X_' + key.replace('soccer_', '')[:28]
            if lk not in L:
                grp = 'cups' if any(w in key for w in CUP_WORDS) else 'more'
                comp(lk, s.get('title') or key, s.get('description') or '', grp, -0.5 if grp == 'more' else 0, oa=[key])
            plan[key] = lk
    if not sports:   # sports list unavailable: fall back to our mapped keys
        plan = dict(OA_KEY)
    missing = [k for k in OA_KEY if sports and k not in listed]
    if missing and not quiet:
        log('not currently listed by The Odds API (off-season or different key):', ', '.join(missing))
    return plan


def parse_api_event(ev, keep=None):
    out = {'id': ev.get('id'), 'ko': ev.get('commence_time'), 'h': ev.get('home_team'), 'a': ev.get('away_team'),
           'books': (keep or {}).get('books') or {}, 'ou': (keep or {}).get('ou') or {}, 'upd': (keep or {}).get('upd')}
    if 'bookmakers' in ev:
        books, ous, last = {}, {}, None
        for b in ev.get('bookmakers') or []:
            bk = b.get('key')
            for mk in b.get('markets') or []:
                lu = mk.get('last_update') or b.get('last_update')
                if lu and (last is None or lu > last):
                    last = lu
                oc = mk.get('outcomes') or []
                if mk.get('key') == 'h2h':
                    pr = {}
                    for o in oc:
                        nm = (o.get('name') or '').strip()
                        if nm == out['h']:
                            pr['H'] = o.get('price')
                        elif nm == out['a']:
                            pr['A'] = o.get('price')
                        elif nm.lower() == 'draw':
                            pr['D'] = o.get('price')
                    if all(isinstance(pr.get(k), (int, float)) and pr[k] > 1 for k in 'HDA'):
                        books[bk] = [pr['H'], pr['D'], pr['A']]
                elif mk.get('key') == 'totals':
                    ov = {o.get('name', '').lower(): o.get('price') for o in oc if o.get('point') == 2.5}
                    if (ov.get('over') or 0) > 1 and (ov.get('under') or 0) > 1:
                        ous[bk] = [ov['over'], ov['under']]
        out['books'], out['ou'], out['upd'] = books, ous, last or iso(NOW)
    return out


def api_market(e):
    books = e.get('books') or {}
    if not books:
        return None
    acc = [0.0, 0.0, 0.0]
    wsum = 0.0
    best = [0.0, 0.0, 0.0]
    bk = ['', '', '']
    for name, pr in books.items():
        q = demargin(pr)
        w = SHARP.get(name, 1.0)
        for i in range(3):
            acc[i] += w * q[i]
            if name not in EXCHANGES and pr[i] > best[i]:
                best[i], bk[i] = pr[i], name
        wsum += w
    p = [x / wsum for x in acc]
    s = sum(p)
    avg = [round(sum(pr[i] for pr in books.values()) / len(books), 3) for i in range(3)]
    for i in range(3):
        if best[i] == 0:
            best[i], bk[i] = max(pr[i] for pr in books.values()), 'exchange'
    m = {'p': [round(x / s, 4) for x in p], 'avg': avg, 'best': best, 'bk': bk, 'n': len(books),
         'ou': None, 'src': 'odds-api', 'upd': e.get('upd')}
    ous = e.get('ou') or {}
    if ous:
        po = 0.0
        ws = 0.0
        bo = [0.0, 0.0]
        for name, (o, u) in ous.items():
            w = SHARP.get(name, 1.0)
            po += w * demargin([o, u])[0]
            ws += w
            if name not in EXCHANGES:
                bo = [max(bo[0], o), max(bo[1], u)]
        if bo[0] == 0:
            bo = [max(v[0] for v in ous.values()), max(v[1] for v in ous.values())]
        m['ou'] = {'line': 2.5, 'pO': round(po / ws, 4),
                   'avg': [round(sum(v[0] for v in ous.values()) / len(ous), 3), round(sum(v[1] for v in ous.values()) / len(ous), 3)],
                   'best': bo}
    return m


# How often each competition's prices are refreshed, by how close its next game is (hours ahead, minutes between
# refreshes), as they would be with credits to spare. Prices move most in the last hours (team news, line-ups, big
# money), so those get the most refreshes; a game a week away barely moves. With fewer credits every interval is
# stretched by the same factor until the day's budget covers it, so the shape holds on any plan.
ODDS_TIERS = ((2, 30), (6, 60), (24, 180), (72, 360), (168, 720), (1e9, 1440))
ODDS_FINAL_H = 1.5      # one last refresh this close to a competition's next kick-off: its closing price
ODDS_RESERVE = 0.02     # the share of the month's credits never spent, so the key is never run dry
ODDS_FASTEST = 1 / 3    # with credits to spare, intervals shrink to a third of the table's (never below ODDS_MIN_INTERVAL)
ODDS_IMP = {'big': 1.5, 'cups': 1.3, 'intl': 1.25, 'second': 1.0, 'more': 0.85, 'women': 0.85}   # bigger competitions refresh more often


def fmt_min(m):
    return f'{m:.0f} min' if m < 90 else f'{m / 60:.1f} h'.replace('.0 h', ' h')


def odds_plan(active, plan, api, daily, cost, state):
    """Each competition's refresh interval right now: the tier for its next kick-off, shortened for bigger
    competitions, then stretched by one factor so the expected spend over a day fits the day's budget."""
    imp, base = {}, {}
    for key, nxt in active:
        lk = plan.get(key)
        imp[key] = ODDS_IMP.get((L.get(lk) or {}).get('group'), 1.0)
        h = (nxt - NOW).total_seconds() / 3600
        base[key] = next(m for lim, m in ODDS_TIERS if h <= lim) / imp[key]
    need = sum(cost * 1440 / v for v in base.values())   # credits a day at full speed
    # stretched to fit the budget; with credits to spare (paid plans), up to three times faster than the table
    scale = max(ODDS_FASTEST, need / daily) if daily > 0 else 1e6
    by = {k: max(MIN_INTERVAL, v * scale) for k, v in base.items()}
    iv = [round(max(MIN_INTERVAL, m * scale), 1) for _, m in ODDS_TIERS]   # each tier, for an average competition
    state['odds_plan'] = {'at': iso(NOW), 'scale': round(scale, 1), 'iv': iv, 'tiers': [t[0] for t in ODDS_TIERS], 'budget': round(daily, 1),
                          'need': round(need, 1), 'n': len(active)}
    return {'by': by, 'imp': imp, 'iv': iv, 'scale': scale}


def refresh_odds_api(state):
    if not API_KEY:
        return 'no key'
    if state.get('odds_block_until') and parse_iso(state['odds_block_until']) > NOW and not FORCE:
        return 'paused'
    last = state.setdefault('last', {})
    if FORCE or not last.get('sports') or NOW - parse_iso(last['sports']) > timedelta(hours=SPORTS_EVERY):
        sp = oa_get('/sports', {}, state)
        if isinstance(sp, list):
            state['oa_sports'] = sp
            last['sports'] = iso(NOW)
    plan = sport_plan(state)
    api = state.setdefault('api', {})
    frm = (NOW - timedelta(hours=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
    to = (NOW + timedelta(days=DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')

    # free fixture lists
    if FORCE or not last.get('events') or NOW - parse_iso(last['events']) >= timedelta(minutes=EVENTS_EVERY - 1):
        for key in plan:
            evs = oa_get(f'/sports/{key}/events', {'commenceTimeFrom': frm, 'commenceTimeTo': to, 'dateFormat': 'iso'}, state)
            if not isinstance(evs, list):
                continue
            old = {e['id']: e for e in (api.get(key) or {}).get('events', [])}
            api.setdefault(key, {})['events'] = [parse_api_event(e, old.get(e.get('id'))) for e in evs]
        last['events'] = iso(NOW)

    # paid price refreshes: a daily budget weighted by each day's games (unused credits roll forward), spent by the
    # refresh plan (odds_plan): each competition is due by how close its next game is, scaled to the budget, most
    # overdue first, with one last refresh in the final 90 minutes before each kick-off
    cost = len(MARKETS) * len(REGIONS)
    active = []
    for key in plan:
        evs = (api.get(key) or {}).get('events') or []
        future = [parse_iso(e['ko']) for e in evs if e.get('ko') and parse_iso(e['ko']) > NOW]
        if future:
            active.append((key, min(future)))
    if not active:
        return 'no upcoming events'
    q = state.get('quota') or {}
    remaining = q.get('remaining')
    if remaining is None:
        remaining = MONTHLY
    y, m = NOW.year, NOW.month
    reset = datetime(y, m, min(RESET_DAY, 28), tzinfo=UTC)
    if reset <= NOW:
        reset = datetime(y + (m == 12), (m % 12) + 1, min(RESET_DAY, 28), tzinfo=UTC)
    day0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    days_left = max(1.0, (reset - day0).total_seconds() / 86400)
    today = day0.strftime('%Y-%m-%d')
    spend = state.get('spend') or {}
    spent = float(spend.get(today, 0))
    # weight each day by its games (plus half of the next day's), so busy weekends get more credits
    games = {}
    for key in plan:
        for e in (api.get(key) or {}).get('events') or []:
            if e.get('ko'):
                d = parse_iso(e['ko']).strftime('%Y-%m-%d')
                games[d] = games.get(d, 0) + 1
    def weight(d):
        nxt = (datetime.strptime(d, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        return games.get(d, 0) + 0.5 * games.get(nxt, 0)
    known = [weight((day0 + timedelta(days=i)).strftime('%Y-%m-%d')) for i in range(min(8, int(days_left) + 1))]
    avg_w = sum(known) / len(known) if known and sum(known) > 0 else 1.0
    total_w = sum(known) + avg_w * max(0.0, days_left - len(known))
    share = weight(today) / total_w if total_w > 0 else 1 / days_left
    share = min(1.0, max(share, 0.25 / days_left))
    daily = max(0.0, remaining - max(5.0, ODDS_RESERVE * MONTHLY) + spent) * min(share, 3.0 / days_left)   # never more than 3x an even day
    if spend.get('budget') is not None and spend.get(today) is not None:
        daily = min(float(spend['budget']), max(0.0, remaining - max(5.0, ODDS_RESERVE * MONTHLY) + spent))   # fixed for the day once set
    elapsed = ((NOW - day0).total_seconds() / 60 + 90) / 1440          # pro-rata through the day
    allowed = daily * min(1.0, elapsed) - spent

    reserve = max(5.0, ODDS_RESERVE * MONTHLY)
    if remaining < cost + reserve and not FORCE:
        state['odds_plan'] = None
        return 'out of credits'
    plan_now = odds_plan(active, plan, api, daily, cost, state)
    state['odds_interval'] = plan_now['iv'][0]
    allowed_final = daily * min(1.0, elapsed + 0.5) - spent   # the last call before a kick-off may run ahead of the day's pace
    # and the routine refreshes leave room for the last calls still to come today
    end_day = day0 + timedelta(days=1, hours=ODDS_FINAL_H)   # games just after midnight get their last call today
    to_come = 0
    for key, _ in active:   # every kick-off time whose last call falls today, one call each
        blob = api.get(key) or {}
        kos = {e['ko'] for e in blob.get('events') or [] if e.get('ko') and NOW < parse_iso(e['ko']) < end_day}
        to_come += sum(1 for k in kos if blob.get('final') != iso(parse_iso(k)))
    allowed -= cost * to_come
    news = state.get('lineup_lk') or {}
    cands = []
    for key, nxt in active:
        blob = api.get(key) or {}
        last = blob.get('odds_at')
        age = (NOW - parse_iso(last)).total_seconds() / 60 if last else None
        h = (nxt - NOW).total_seconds() / 3600
        iv = plan_now['by'][key]
        lu = news.get(plan.get(key))
        final = h <= ODDS_FINAL_H and (age is None or age >= 55) and blob.get('final') != iso(nxt)
        if lu and (not last or parse_iso(last) < parse_iso(lu)) and h <= 2 and (age is None or age >= MIN_INTERVAL - 1):
            cands.append((0, -9.0, nxt, key, 'news'))   # line-ups just landed: prices move on team news
        elif final:
            cands.append((1, -plan_now['imp'][key], nxt, key, 'final'))   # the closing price: the sharpest, and what value bets are measured against
        elif FORCE or age is None or age >= iv - 1:
            due = (3.0 if h <= 72 else 1.2) if age is None else age / iv   # how overdue, as a share of its interval
            cands.append((2, -due * plan_now['imp'][key], nxt, key, 'due'))
    cands.sort()
    done, kinds = 0, {}
    to_ = to
    for _, _, nxt, key, why in cands:
        room = allowed_final if why in ('final', 'news') else allowed
        if not FORCE and room < cost:
            continue
        if (state.get('quota') or {}).get('remaining') is not None and state['quota']['remaining'] < cost + reserve:
            break
        # games already under way aren't asked for again: their last price before kick-off stays as the closing price
        data = oa_get(f'/sports/{key}/odds', {'regions': ','.join(REGIONS), 'markets': ','.join(MARKETS),
                                              'oddsFormat': 'decimal', 'dateFormat': 'iso',
                                              'commenceTimeFrom': NOW.strftime('%Y-%m-%dT%H:%M:%SZ'), 'commenceTimeTo': to_}, state)
        allowed -= cost
        allowed_final -= cost
        spent += cost
        if not isinstance(data, list):
            continue
        cur = {e['id']: e for e in (api.get(key) or {}).get('events', [])}
        for ev in data:
            cur[ev.get('id')] = parse_api_event(ev)
        blob = api.setdefault(key, {})
        blob['events'] = list(cur.values())
        blob['odds_at'] = iso(NOW)
        if why == 'final' or (nxt - NOW).total_seconds() / 3600 <= ODDS_FINAL_H:
            blob['final'] = iso(nxt)
        done += 1
        kinds[why] = kinds.get(why, 0) + 1
    state['spend'] = {today: spent, 'budget': round(daily, 2)}   # the day's budget stays as set at its first run
    pl = state.get('odds_plan') or {}
    note = f"{done} competitions priced" + (f" ({', '.join(f'{v} {k}' for k, v in kinds.items())})" if kinds else '')
    if pl.get('scale'):
        note += f"; plan x{pl['scale']}: games within 2 h every {fmt_min(pl['iv'][0])}, within a day every {fmt_min(pl['iv'][2])}"
    return note


# ----------------------------------------------------------------------------------------------
# Live scores and fast results from ESPN's public scoreboards: free, no key, no daily allowance, and
# updated within a minute, with the minute of every goal and red card. One request per competition that
# has a game in play or awaiting its result. The page reads the same scoreboards itself every 30 s while
# games are on; this copy keeps live.json, results.json and the record going when nobody has it open, and
# pairs each match with ESPN's game id so the page needn't guess.
# ----------------------------------------------------------------------------------------------
ESPN_SB = 'https://site.api.espn.com/apis/site/v2/sports/soccer/{}/scoreboard'
ESPN_SUM = 'https://site.api.espn.com/apis/site/v2/sports/soccer/{}/summary'
ESPN_SLUG = {'EPL': 'eng.1', 'ENG2': 'eng.2', 'ENG3': 'eng.3', 'ENG4': 'eng.4', 'LaLiga': 'esp.1', 'ESP2': 'esp.2',
             'BUN': 'ger.1', 'GER2': 'ger.2', 'SA': 'ita.1', 'ITA2': 'ita.2', 'L1': 'fra.1', 'FRA2': 'fra.2',
             'ERE': 'ned.1', 'NED2': 'ned.2', 'POR': 'por.1', 'BEL': 'bel.1', 'SCO1': 'sco.1', 'SCO2': 'sco.2',
             'TUR1': 'tur.1', 'GRE1': 'gre.1', 'AUT1': 'aut.1', 'DEN1': 'den.1', 'NOR1': 'nor.1',
             'SWE1': 'swe.1', 'IRL1': 'irl.1', 'BRA1': 'bra.1', 'ARG1': 'arg.1', 'MEX1': 'mex.1',
             'USA1': 'usa.1', 'JPN1': 'jpn.1', 'CHN1': 'chn.1', 'KSA': 'ksa.1', 'UCL': 'uefa.champions',
             'UEL': 'uefa.europa', 'UECL': 'uefa.europa.conf', 'FAC': 'eng.fa', 'EFLC': 'eng.league_cup',
             'DFB': 'ger.dfb_pokal', 'CDR': 'esp.copa_del_rey', 'CI': 'ita.coppa_italia', 'CDF': 'fra.coupe_de_france',
             'UNL': 'uefa.nations', 'INTF': 'fifa.friendly'}
ESPN_SLUG.update({lk: c['es'] for lk, c in L.items() if c.get('es')})
ESPN_OFF = {'STATUS_POSTPONED', 'STATUS_CANCELED', 'STATUS_CANCELLED', 'STATUS_ABANDONED', 'STATUS_FORFEIT'}
ESPN_BEFORE = 180       # minutes before kick-off from which a match is paired with ESPN's game
ESPN_AFTER = 135        # minutes after kick-off it's followed live
ESPN_BUDGET_S = 45      # seconds of ESPN requests per run, at most


def espn_minute(s):
    """"67'" -> 67, "45'+2'" -> 47, "90'+5'" -> 95."""
    m = re.match(r"\s*(\d+)'?(?:\s*\+\s*(\d+))?", s or '')
    return int(m.group(1)) + int(m.group(2) or 0) if m else 0


def espn_row(e):
    """One ESPN game in a compact form, or None."""
    try:
        c = (e.get('competitions') or [{}])[0]
        side = {x.get('homeAway'): x for x in c.get('competitors') or []}
        h, a = side['home'], side['away']
        names = lambda t: sorted({v for n in (t.get('displayName'), t.get('shortDisplayName'), t.get('name'), t.get('location')) if n
                                  for v in (n, n.replace('.', ''))})   # "D.C. United" is our "DC United"
        st = (c.get('status') or e.get('status') or {})
        typ = st.get('type') or {}
        hid = (h.get('team') or {}).get('id')
        goals, reds = [], []
        for d in c.get('details') or []:
            if d.get('shootout'):
                continue
            mn = espn_minute((d.get('clock') or {}).get('displayValue'))
            s = 'h' if (d.get('team') or {}).get('id') == hid else 'a'
            if d.get('scoringPlay'):
                goals.append([mn, s, bool(d.get('ownGoal')), (d.get('clock') or {}).get('displayValue') or ''])
            elif d.get('redCard'):
                reds.append([mn, s])
        return {'id': str(e.get('id')), 'hid': str((h.get('team') or {}).get('id') or ''), 'aid': str((a.get('team') or {}).get('id') or ''),
                'ko': parse_iso((e.get('date') or c.get('date') or '').replace('Z', ':00Z') if re.search(r'T\d\d:\d\dZ$', e.get('date') or '') else (e.get('date') or c.get('date'))),
                'h': names(h.get('team') or {}), 'a': names(a.get('team') or {}),
                'hs': int(h.get('score') or 0), 'as': int(a.get('score') or 0),
                'st': typ.get('name') or '', 'state': typ.get('state') or '', 'done': bool(typ.get('completed')),
                'period': int(st.get('period') or 0), 'clock': st.get('displayClock') or '', 'goals': goals, 'reds': reds,
                'mkt': espn_market(c) if typ.get('state') == 'pre' else None}
    except Exception:
        return None


def espn_goals(g):
    """Goals and red cards with minutes, trusted only when the goals add up to the score (feeds differ on
    which side an own goal is listed under, so both readings are tried). Also the score after 90 minutes."""
    for flip in (False, True):
        gs = sorted(([m, ('a' if s == 'h' else 'h') if (o and flip) else s, lab] for m, s, o, lab in g['goals']), key=lambda x: x[0])   # feed order within a minute
        if sum(1 for x in gs if x[1] == 'h') == g['hs'] and sum(1 for x in gs if x[1] == 'a') == g['as']:
            reg = [[m, s] for m, s, lab in gs if int((re.match(r'\s*(\d+)', lab or '0') or [0, '0'])[1]) <= 90]   # 90'+3' is still normal time
            return [[m, s] for m, s, _ in gs], sorted(g['reds']), 1, reg
    return None, sorted(g['reds']), 0, None


def espn_days(kos):
    """ESPN files games by US Eastern day: ask for that day, and the UTC day too when it differs."""
    out = set()
    for ko in kos:
        out.add((ko - timedelta(hours=5)).strftime('%Y%m%d'))
        out.add(ko.strftime('%Y%m%d'))
    return sorted(out)


def espn_board(slug, kos, cache):
    """Every game of one competition on the days of these kick-offs (one request per day; None if none came)."""
    rows, got = {}, False
    for day in espn_days(kos):
        key = f'{slug}:{day}'
        if key not in cache:
            r = fetch(ESPN_SB.format(slug), {'dates': day, 'limit': 300}, tries=2, timeout=20, headers={'Accept-Encoding': 'gzip'})
            got_rows = None
            if r.status == 200 and r.body:
                try:
                    body = gzip.decompress(r.body) if r.body[:2] == b'\x1f\x8b' else r.body
                    got_rows = [x for x in (espn_row(e) for e in json.loads(text_of(body)).get('events') or []) if x and x['ko']]
                except Exception as e:
                    log('ESPN', slug, 'unreadable:', repr(e)[:80])
            cache[key] = got_rows
        if cache[key] is not None:
            got = True
            for x in cache[key]:
                rows[x['id']] = x
    return list(rows.values()) if got else None


def espn_pair(lk, ko, h, a, rows):
    """ESPN's game for one of our matches: same kick-off (within 3 h) and both names close, or the same
    kick-off and one side plainly the same club that no other game on the board could be (a club can't
    play twice at once, and feeds name reserve and renamed sides differently)."""
    near = []
    for g in rows or []:
        dt = abs((g['ko'] - ko).total_seconds()) / 60
        if dt <= 180:
            near.append((g, dt, max((team_sim(base_name(h), n) for n in g['h']), default=0), max((team_sim(base_name(a), n) for n in g['a']), default=0)))
    best, best_s = None, 0.0
    for g, dt, sh, sa in near:
        ok = (sh >= 0.8 and sa >= 0.8) or (dt <= 15 and ((sh == 1 and sa >= 0.5) or (sa == 1 and sh >= 0.5)))
        if not ok and dt <= 15 and max(sh, sa) >= 0.9:
            i = 2 if sh >= sa else 3
            ok = not any(x[0] is not g and (x[i] >= 0.6 or x[5 - i] >= 0.9) for x in near)
        if ok and sh + sa - dt / 600 > best_s:
            best, best_s = g, sh + sa - dt / 600
    return best


def espn_tuple(g, prev=None):
    """The live-file entry for a game: [home, away, minute, stage, checked, goals, reds, exact]. Once extra
    time starts the score shown is the 90-minute one, since that's what bets are settled on."""
    goals, reds, exact, reg = espn_goals(g)   # reg: the goals of normal time
    if goals is None and prev and len(prev) > 5:
        goals = [list(x) for x in prev[5]]   # the feed's goal list doesn't add up yet: keep the last good one
    if g['state'] == 'post':
        stage, minute = 'FT', 90
    elif g['st'] == 'STATUS_HALFTIME':
        stage, minute = 'HT', 45
    elif g['period'] >= 5:
        stage, minute = 'P', 120
    elif g['period'] >= 3 or g['st'] == 'STATUS_END_OF_REGULATION':
        stage, minute = 'ET', 90 + max(0, espn_minute(g['clock']) - 90)
    else:
        stage, minute = ('2H' if g['period'] == 2 else '1H'), espn_minute(g['clock'])
    hs, as_ = g['hs'], g['as']
    if stage in ('ET', 'P') or (stage == 'FT' and g['st'] in ('STATUS_FINAL_AET', 'STATUS_FINAL_PEN')):
        if reg is None:
            return None
        hs, as_ = sum(1 for x in reg if x[1] == 'h'), sum(1 for x in reg if x[1] == 'a')
        goals = reg
    return [hs, as_, minute, stage, iso(NOW), goals or [], reds, exact]


def refresh_espn(state, matches):
    """Follow every match in play (and grade those awaiting a result) from ESPN's scoreboards."""
    af = state.setdefault('af', {})
    lv = af.setdefault('live', {})
    games = lv.setdefault('games', {})
    done = af.setdefault('scores', {})
    off = af.setdefault('void', {})
    ev = state.setdefault('espn_ev', {})      # our match id -> [competition, ESPN game id, kick-off]
    miss = state.setdefault('espn_miss', {})  # our match id -> kick-off (ESPN has no such game)
    for blob in (ev, miss):
        for k in [k for k, v in blob.items() if parse_iso(v[-1] if isinstance(v, list) else v) < NOW - timedelta(days=4)]:
            del blob[k]
    want = {}
    for m in list(matches) + list((state.get('track_pending') or {}).values()):
        if m['lk'] not in ESPN_SLUG or m['id'] in done or m['id'] in off or m['id'] in miss:
            continue
        ko = parse_iso(m['ko'])
        mins = (NOW - ko).total_seconds() / 60
        if -ESPN_BEFORE <= mins <= ESPN_AFTER or (ESPN_AFTER < mins <= 3 * 1440 and m['id'] in (state.get('track_pending') or {})):
            want[m['id']] = (m['lk'], ko, m['h'], m['a'])
    if not want:
        return {'live': 0, 'results': 0, 'calls': 0, 'boards': 0}
    by_lk = {}
    for mid, w in want.items():
        by_lk.setdefault(w[0], []).append((mid, w))
    cache, t0, calls, n_live, n_res, boards = {}, time.time(), 0, 0, 0, 0
    for lk, items in sorted(by_lk.items(), key=lambda kv: min((abs((NOW - w[1]).total_seconds()) for _, w in kv[1]))):
        if time.time() - t0 > ESPN_BUDGET_S:
            break
        rows = espn_board(ESPN_SLUG[lk], [w[1] for _, w in items], cache)
        calls = len(cache)
        if rows is None:
            continue
        boards += 1
        for mid, (lk_, ko, h, a) in items:
            known = ev.get(mid)
            g = next((x for x in rows if known and x['id'] == known[1]), None) or espn_pair(lk_, ko, h, a, rows)
            if not g:
                if NOW - ko > timedelta(minutes=ESPN_AFTER):
                    miss[mid] = iso(ko)   # a board from after the game doesn't list it: stop asking ESPN for it
                continue
            ev[mid] = [ESPN_SLUG[lk_], g['id'], iso(ko)]
            es_learn(state, h, a, g)
            if g['st'] in ESPN_OFF:
                off[mid] = [g['st'].replace('STATUS_', '')[:4], iso(ko)]
                games.pop(mid, None)
                continue
            if g['state'] == 'pre':
                continue
            t = espn_tuple(g, games.get(mid))
            if g['state'] == 'post' and g['done']:
                if t:
                    done[mid] = [t[0], t[1], iso(ko)]
                    n_res += 1
                    if mid in games or NOW - ko < timedelta(hours=4):
                        games[mid] = t
                continue
            if t:
                games[mid] = t
                n_live += 1
    for mid in [k for k, v in games.items() if NOW - parse_iso(v[4]) > timedelta(hours=4)]:
        del games[mid]
    if calls:
        lv['at'] = iso(NOW)
        lv['src'] = 'ESPN'
    return {'live': n_live, 'results': n_res, 'calls': calls, 'boards': boards}


# ----------------------------------------------------------------------------------------------
# Commentary xG. ESPN's play-by-play (Opta's feed) gives every shot's spot on the pitch and the commentary's words
# (header, free kick, fast break, cross, through ball, corner, set piece). A shot-quality model (logistic regression
# on distance, the angle of the goal mouth and those words) turns each shot into the chance it was a goal; a side's
# expected goals are the sum. Fitted offline on 368,847 shots from the 8 leagues below, 2021/22 to 2026/27: on the
# seasons it wasn't fitted on its log loss per shot was 0.280 (0.345 knowing only the average). ESPN lists shots
# this way only for these leagues (elsewhere just the goals). Collected a few dozen games a run, newest first, back
# to the seasons the monthly tuning trains on; the tuning decides whether the ratings use them (setting 'sx').
# ----------------------------------------------------------------------------------------------
XG_SLUGS = {'EPL': 'eng.1', 'ENG2': 'eng.2', 'LaLiga': 'esp.1', 'BUN': 'ger.1', 'SA': 'ita.1', 'L1': 'fra.1', 'ERE': 'ned.1', 'POR': 'por.1'}
XG_COEF = [-5.4477, 2.6147, -0.1742, 1.7543, -0.0141, -0.111, 3.0893, 1.625, -0.3691, 1.0148, 0.3231, 0.5702, 2.9029]
XG_WORDS = ('header', 'free kick', 'fast break', 'with a cross', 'through ball', 'following a corner', 'set piece')
XG_PER_RUN = int(ENV('STATPAD_XG_PER_RUN') or 40)   # games fetched a run (about 350 KB each)
XG_BUDGET_S = 45
XG_BACK = 4             # seasons back from the current one (the tuning's oldest season needs the one before it)
ESPN_PLAYS = 'https://sports.core.api.espn.com/v2/sports/soccer/leagues/{0}/events/{1}/competitions/{1}/plays'
_XG = None


def xg_zone(txt):
    """A spot (fraction of half the pitch's length from goal, fraction of its width) from the commentary's words,
    for shots the feed gave no spot for."""
    t = txt.lower()
    side = 0.32 if 'left side' in t else 0.68 if 'right side' in t else 0.5
    if 'very close range' in t:
        return 0.06, 0.5
    if 'six yard box' in t:
        return 0.085, 0.5 + (side - 0.5) * 0.25
    if 'difficult angle' in t:
        return 0.12, 0.2 if 'left' in t else 0.8
    if 'more than 35 yards' in t or 'long range' in t:
        return 0.68, 0.5
    if 'outside the box' in t:
        return 0.42, 0.5
    if 'centre of the box' in t:
        return 0.2, 0.5
    if 'side of the box' in t:
        return 0.24, side
    return None


def shot_xg(kind, X, Y, pen_flag, txt):
    """(the chance a shot was a goal, was it one), or None for plays that aren't shots (own goals aren't)."""
    tl = (kind or '').lower()
    if 'own goal' in tl or tl == 'goal kick' or not (tl.startswith('shot') or tl.startswith('goal') or tl.startswith('penalty')):
        return None
    goal = tl.startswith('goal') or tl == 'penalty - scored'
    pen = tl.startswith('penalty') or bool(pen_flag)
    if X and (X > 1.5 or (Y or 0) > 1.5):   # from August 2026 the feed gives percentages along the pitch, attacking towards 100
        X, Y = (100 - X) / 50, (Y if Y else 50) / 100
    if not X:
        z = xg_zone(txt or '')
        if not z and not pen:
            return None
        X, Y = z if z else (0.105, 0.5)
    x, y = X * 52.5, ((Y if Y else 0.5) - 0.5) * 68
    d = math.hypot(x, y)
    a = math.atan2(7.32 * x, x * x + y * y - 3.66 ** 2)
    if a < 0:
        a += math.pi
    t = (txt or '').lower()
    f = [1.0 if w in t else 0.0 for w in XG_WORDS]
    v = [1.0, a, d, math.log(max(1.0, d)), f[0], f[0] * d, f[1], f[2], f[3], f[4], f[5], f[6], 1.0 if pen else 0.0]
    z = sum(c * u for c, u in zip(XG_COEF, v))
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z)))), goal


def plays_xg(items, hid, aid):
    """(home xG, away xG, shots) from a game's plays, or None when the feed lists no shots for it."""
    xh = xa = 0.0
    n = 0
    for p in items or []:
        if p.get('shootout'):
            continue
        tid = re.search(r'/teams/(\d+)', ((p.get('team') or {}).get('$ref')) or '')
        tid = tid.group(1) if tid else None
        r = shot_xg((p.get('type') or {}).get('text'), p.get('fieldPositionX'), p.get('fieldPositionY'), p.get('penaltyKick'), p.get('text'))
        if not r or tid not in (hid, aid):
            continue
        n += 1
        if tid == hid:
            xh += r[0]
        else:
            xa += r[0]
    return (round(xh, 3), round(xa, 3), n) if n >= 6 else None


def xg_state():
    global _XG
    if _XG is None:
        _XG = load_json(STATE_DIR / 'xg.json') or {}
        _XG.setdefault('g', {})    # ESPN game id -> [slug, date, home names, away names, home xG, away xG, shots]
        _XG.setdefault('ev', {})   # slug -> month -> [[id, date, home id, away id, home names, away names], ...] (finished games)
        _XG.setdefault('at', {})   # slug -> month -> when it was listed
        _XG.setdefault('miss', {})  # ESPN game id -> tries without shots
    return _XG


def refresh_xg(state):
    """Collect commentary xG: list each league's finished games month by month (past months once), then fetch the
    play-by-play of up to XG_PER_RUN games not yet read, newest first."""
    X = xg_state()
    t0, listed, got = time.time(), 0, 0
    first = season_code(NOW, XG_BACK)
    start = datetime(2000 + int(first[:2]), 7, 1, tzinfo=UTC)
    cur_ym = NOW.strftime('%Y%m')
    for ym, slug in sorted(((ym, slug) for slug in XG_SLUGS.values() for ym in months_between(start, NOW)), reverse=True):
        if time.time() - t0 > XG_BUDGET_S / 3:
            break
        evm, atm = X['ev'].setdefault(slug, {}), X['at'].setdefault(slug, {})
        last = atm.get(ym)
        past = ym < cur_ym and last and last[:7].replace('-', '') > ym   # listed after the month ended
        if past or (last and NOW - parse_iso(last) < timedelta(hours=6)):
            continue
        evs, ok = espn_month(slug, ym)
        listed += 1
        if not ok:
            continue
        rows = []
        for e in evs:
            r = espn_row(e)
            if r and r['done'] and r['ko']:
                rows.append([r['id'], r['ko'].strftime('%Y-%m-%d'), r['hid'], r['aid'], r['h'], r['a']])
        evm[ym] = rows
        atm[ym] = iso(NOW)
    queue = []
    for slug, evm in X['ev'].items():
        for ym, rows in evm.items():
            for r in rows:
                if r[0] not in X['g'] and X['miss'].get(r[0], 0) < 3:
                    queue.append((r[1], slug, r))
    queue.sort(key=lambda q: q[0], reverse=True)
    queue = queue[:XG_PER_RUN]

    def one(q):
        _, slug, r = q
        f = fetch(ESPN_PLAYS.format(slug, r[0]), {'limit': 600}, tries=1, timeout=25, headers={'Accept-Encoding': 'gzip'})
        if f.status != 200 or not f.body:
            return q, None
        try:
            body = gzip.decompress(f.body) if f.body[:2] == b'\x1f\x8b' else f.body
            return q, plays_xg(json.loads(text_of(body)).get('items'), r[2], r[3])
        except Exception:
            return q, None
    if queue and time.time() - t0 < XG_BUDGET_S:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            for q, res in ex.map(one, queue):
                _, slug, r = q
                if res:
                    X['g'][r[0]] = [slug, r[1], r[4], r[5], res[0], res[1], res[2]]
                    got += 1
                else:
                    X['miss'][r[0]] = X['miss'].get(r[0], 0) + 1
    if listed or got:
        (STATE_DIR / 'xg.json').write_text(json.dumps(X, ensure_ascii=False, separators=(',', ':')))
        _XG_IX.clear()
        _XG_NM.clear()
    left = sum(1 for evm in X['ev'].values() for rows in evm.values() for r in rows if r[0] not in X['g'] and X['miss'].get(r[0], 0) < 3)
    return f"{got} games read, {len(X['g'])} kept, {left} to go"


_XG_IX, _XG_NM = {}, {}


def xg_for(lk, d, h, a):
    """The commentary's expected goals (home, away) for one of football-data's games, or None. Same day (a day
    either side for late kick-offs) and both names matching ESPN's."""
    slug = XG_SLUGS.get(lk)
    if not slug:
        return None
    X = xg_state()
    if not X['g']:
        return None
    if slug not in _XG_IX:
        ix, names = {}, {}
        for v in X['g'].values():
            if v[0] == slug:
                ix.setdefault(v[1], []).append(v)
                names[v[2][0]], names[v[3][0]] = v[2], v[3]
        _XG_IX[slug] = (ix, names)
    ix, names = _XG_IX[slug]

    def sims(t):   # each ESPN name's likeness to ours, worked out once per team
        k = (slug, t)
        if k not in _XG_NM:
            b_ = base_name(t)
            _XG_NM[k] = {key: max((team_sim(b_, n) for n in ns), default=0) for key, ns in names.items()}
        return _XG_NM[k]
    sh_, sa_ = sims(h), sims(a)
    best, bs = None, 0.0
    for dd in (0, -1, 1):
        for v in ix.get((d + timedelta(days=dd)).strftime('%Y-%m-%d'), ()):
            x, y = sh_.get(v[2][0], 0), sa_.get(v[3][0], 0)
            if x >= 0.75 and y >= 0.75 and x + y - abs(dd) * 0.1 > bs:
                best, bs = v, x + y - abs(dd) * 0.1
    return (best[4], best[5]) if best else None


def espn_lineup(slug, eid):
    """Confirmed starting XIs and formations from ESPN's game summary, in the shape team news uses."""
    r = fetch(ESPN_SUM.format(slug), {'event': eid}, tries=1, timeout=20, headers={'Accept-Encoding': 'gzip'})
    if r.status != 200 or not r.body:
        return None
    try:
        body = gzip.decompress(r.body) if r.body[:2] == b'\x1f\x8b' else r.body
        d = json.loads(text_of(body))
    except Exception:
        return None
    def pos(p):   # "Center Left Defender" -> D, as API-Football writes it
        n = ((p.get('position') or {}).get('name') or '').lower()
        return 'G' if 'goal' in n else 'D' if 'defend' in n or 'back' in n else 'M' if 'midfield' in n else 'F' if re.search(r'forward|striker|wing', n) else None

    side = {}
    for t in d.get('rosters') or []:
        starters = sorted((p for p in t.get('roster') or [] if p.get('starter')),   # keeper, defence, midfield, attack
                          key=lambda p: ('GDMF'.find(pos(p) or 'F') % 4, int(fnum(p.get('formationPlace')) or 99)))
        xi = [[p.get('jersey'), (p.get('athlete') or {}).get('displayName'), pos(p)] for p in starters]
        if t.get('homeAway') in ('home', 'away') and len(xi) >= 7:
            side['h' if t['homeAway'] == 'home' else 'a'] = {'fm': t.get('formation'), 'xi': xi, 'co': None}
    return side if 'h' in side and 'a' in side else None


# ----------------------------------------------------------------------------------------------
# Competitions read from ESPN: fixtures with DraftKings prices, and (for those rated from them) every result
# of the last two seasons, a month per request. Past months are read once; the current ones every few hours.
# ----------------------------------------------------------------------------------------------
ES_FIX_EVERY_H = 2        # hours between fixture reads
ES_HIST_EVERY_H = 6       # hours between re-reads of this month's and last month's results
ES_HIST_DAYS = 600        # results kept for the ratings
ES_BUDGET_S = float(ENV('STATPAD_ES_BUDGET') or 40)   # seconds of these reads per run, at most


def american(x):
    """'+550' -> 6.5, '-300' -> 1.333, 'EVEN' -> 2.0"""
    try:
        t = str(x).strip().upper()
        if t in ('EVEN', 'EV', 'PK'):
            return 2.0
        v = float(t.replace('+', ''))
        return round(1 + v / 100, 4) if v > 0 else round(1 + 100 / -v, 4) if v < 0 else None
    except (TypeError, ValueError):
        return None


def espn_market(c):
    """DraftKings' prices from an ESPN game: 1X2 and the goals line, with the margin taken off; or None."""
    for o in c.get('odds') or []:
        if not o:
            continue
        ml = o.get('moneyline') or {}
        pick = lambda side: american(((side or {}).get('close') or (side or {}).get('open') or {}).get('odds'))
        pr = [pick(ml.get('home')), pick(ml.get('draw')), pick(ml.get('away'))]
        if not all(x and x > 1 for x in pr):
            hm, aw, dr = (o.get('homeTeamOdds') or {}).get('moneyLine'), (o.get('awayTeamOdds') or {}).get('moneyLine'), (o.get('drawOdds') or {}).get('moneyLine')
            pr = [american(hm), american(dr), american(aw)]
        if not all(x and x > 1 for x in pr) or sum(1 / x for x in pr) < 0.98 or sum(1 / x for x in pr) > 1.25:
            continue
        name = ((o.get('provider') or {}).get('name')) or 'ESPN BET'
        m = {'p': [round(x, 4) for x in demargin(pr)], 'avg': pr, 'best': pr, 'bk': [name] * 3, 'n': 1, 'ou': None,
             'src': 'espn', 'upd': iso(NOW)}
        tot = o.get('total') or {}
        ov, un = (tot.get('over') or {}).get('close') or (tot.get('over') or {}).get('open') or {}, (tot.get('under') or {}).get('close') or (tot.get('under') or {}).get('open') or {}
        po, pu_ = american(ov.get('odds')), american(un.get('odds'))
        try:
            line = float(re.sub(r'[^0-9.]', '', ov.get('line') or '') or o.get('overUnder') or 0)
        except ValueError:
            line = 0
        if po and pu_ and line in (1.5, 2.5, 3.5, 4.5):
            m['ou'] = {'line': line, 'pO': round(demargin([po, pu_])[0], 4), 'avg': [po, pu_], 'best': [po, pu_]}
        return m
    return None


def espn_month(slug, ym):
    """Every game of one competition in one month (YYYYMM) -> (events, ok)."""
    r = fetch(ESPN_SB.format(slug), {'dates': ym, 'limit': 500}, tries=2, timeout=20, headers={'Accept-Encoding': 'gzip'})
    if r.status != 200 or not r.body:
        return [], False
    try:
        body = gzip.decompress(r.body) if r.body[:2] == b'\x1f\x8b' else r.body
        return json.loads(text_of(body)).get('events') or [], True
    except Exception:
        return [], False


def months_between(a, b):
    y, m, out = a.year, a.month, []
    while (y, m) <= (b.year, b.month):
        out.append(f'{y}{m:02d}')
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def refresh_espn_data(state):
    """Fixtures (with prices) for the ESPN-read competitions, and results for the ones rated from ESPN."""
    t0, calls = time.time(), 0
    fixl = state.get('es_fixtures') or []
    hist = state.setdefault('es_hist', {})
    logos = state.setdefault('es_logo', {})
    last = state.setdefault('last', {})
    comps = [(lk, c) for lk, c in L.items() if c.get('es')]
    new_hist = False
    fix_due = due(state, 'es_fix', ES_FIX_EVERY_H) or 'es_fixtures' not in state

    def read(lk, slug, ym):
        nonlocal calls, new_hist
        evs, ok = espn_month(slug, ym)
        calls += 1
        if not ok:
            return None
        rows = []
        for e in evs:
            g = espn_row(e)
            if not g:
                continue
            c = (e.get('competitions') or [{}])[0]
            side = {x.get('homeAway'): x for x in c.get('competitors') or []}
            th, ta = (side.get('home') or {}).get('team') or {}, (side.get('away') or {}).get('team') or {}
            h, a = th.get('displayName') or g['h'][0], ta.get('displayName') or g['a'][0]
            for t in (th, ta):
                if t.get('id') and t.get('displayName'):
                    logos[t['displayName']] = logos[pretty(t['displayName'])] = str(t['id'])
            rows.append((g, h, a, c, (e.get('season') or {}).get('year')))
        if L[lk].get('hist'):
            hs = hist.setdefault(lk, {'m': {}, 'g': {}})
            for g, h, a, c, yr in rows:
                if g['done'] and g['st'] not in ESPN_OFF and g['state'] == 'post':
                    rec = [g['ko'].strftime('%Y-%m-%d'), h, a, g['hs'], g['as'], yr]
                    if hs['g'].get(g['id']) != rec:
                        hs['g'][g['id']] = rec
                        new_hist = True
            hs['m'][ym] = iso(NOW)
        return rows

    # 1) fixtures: this month and the next, for every ESPN-read competition
    if fix_due:
        lo, hi = NOW - timedelta(hours=3), NOW + timedelta(days=DAYS)
        out, ok_any = [], False
        for lk, c in comps:
            if time.time() - t0 > ES_BUDGET_S:
                break
            for ym in months_between(lo, hi):
                rows = read(lk, c['es'], ym)
                if rows is None:
                    continue
                ok_any = True
                for g, h, a, cc, _yr in rows:
                    if g['st'] in ESPN_OFF or g['done'] or not (lo <= g['ko'] <= hi):
                        continue
                    out.append({'lk': lk, 'ko': iso(g['ko']), 'h': h, 'a': a, 'eid': g['id'], 'mkt': espn_market(cc)})
        if ok_any:
            keep = [f for f in fixl if f['lk'] not in {f2['lk'] for f2 in out} and parse_iso(f['ko']) >= lo]
            state['es_fixtures'] = sorted(out + keep, key=lambda f: f['ko'])
            last['es_fix'] = iso(NOW)
    # 2) results history: past months once, recent months every few hours
    oldest = NOW - timedelta(days=ES_HIST_DAYS)
    for lk, c in comps:
        if not c.get('hist'):
            continue
        hs = hist.setdefault(lk, {'m': {}, 'g': {}})
        for ym in reversed(months_between(oldest, NOW)):
            if time.time() - t0 > ES_BUDGET_S:
                break
            at = hs['m'].get(ym)
            y, m = int(ym[:4]), int(ym[4:])
            month_end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
            if at and (parse_iso(at) > month_end + timedelta(days=2) or NOW - parse_iso(at) < timedelta(hours=ES_HIST_EVERY_H)):
                continue
            read(lk, c['es'], ym)
        for gid in [k for k, v in hs['g'].items() if v[0] < oldest.strftime('%Y-%m-%d')]:
            del hs['g'][gid]
    if new_hist:
        state['es_hist_new'] = True
    n_fix = len(state.get('es_fixtures') or [])
    n_res = sum(len(v['g']) for v in hist.values())
    todo = sum(1 for lk, c in comps if c.get('hist') for ym in months_between(oldest, NOW) if ym not in hist.get(lk, {}).get('m', {}))
    return f"{n_fix} fixtures, {n_res} results{f' ({todo} months still to read)' if todo else ''}, {calls} reads this run"


ES_PX_HOURS = 36         # games this close to kick-off get DraftKings' prices from ESPN, every two hours
ES_PX_BUDGET_S = 25


def refresh_espn_prices(state, matches):
    """Fresh prices for the games in the next day and a half in competitions The Odds API prices: DraftKings' prices
    via ESPN cost nothing, and prices closer to kick-off predict better (on five past seasons, closing prices beat
    earlier ones by more than any change of method), so they freshen a consensus that's hours old."""
    if not due(state, 'es_px', ES_FIX_EVERY_H):
        return None
    state.setdefault('last', {})['es_px'] = iso(NOW)
    t0, cache, out = time.time(), {}, {}
    want = {}
    for m in matches:
        lk, ko = m.get('lk'), parse_iso(m['ko'])
        if lk in ESPN_SLUG and not (L.get(lk) or {}).get('es') and NOW < ko < NOW + timedelta(hours=ES_PX_HOURS):
            want.setdefault(ESPN_SLUG[lk], []).append((m['id'], lk, ko, base_name(m['h']), base_name(m['a']), m['h'], m['a']))
    for slug, items in sorted(want.items(), key=lambda x: min(i[2] for i in x[1])):
        if time.time() - t0 > ES_PX_BUDGET_S:
            break
        rows = espn_board(slug, [i[2] for i in items], cache)
        for mid, lk, ko, h, a, h0, a0 in items:
            g = espn_pair(lk, ko, h, a, rows) if rows else None
            if g:
                es_learn(state, h0, a0, g)
            if g and g.get('mkt'):
                out[mid] = g['mkt']
    keep = {k: v for k, v in (state.get('es_prices') or {}).items() if k not in out and NOW - parse_iso(v.get('upd') or iso(NOW)) < timedelta(hours=12)}
    state['es_prices'] = dict(keep, **out)
    return len(out)


def es_learn(state, h, a, g):
    """A game paired with ESPN's tells us both clubs' ESPN ids exactly, whatever the names: their crests."""
    ce = state.setdefault('crest_es', {})
    for name, tid in ((h, g.get('hid')), (a, g.get('aid'))):
        if tid and str(tid).isdigit() and ce.get(name) != str(tid):
            ce[name] = str(tid)


def espn_results(state):
    """The ESPN-rated competitions' results in the ratings' form: lk -> (games, season label)."""
    out = {}
    for lk, hs in (state.get('es_hist') or {}).items():
        if lk not in L or not L[lk].get('hist') or not hs.get('g'):
            continue
        rows = sorted(hs['g'].values())
        latest = max((r[5] or 0) for r in rows)
        games = [(parse_date(r[0]), r[1], r[2], int(r[3]), int(r[4]), (r[5] or 0) == latest, None, None) for r in rows]
        if len(games) >= 20:
            out[lk] = (games, f'{latest}/{str(latest + 1)[2:]}' if L[lk]['country'] in ('England', 'Spain', 'France', 'Saudi Arabia', 'Netherlands', 'Australia', 'South Africa') else str(latest))
    return out


ESPN_LU_FROM = 75       # minutes before kick-off to start asking ESPN for line-ups
ESPN_LU_EVERY = 8       # minutes between line-up checks for a game
ESPN_LU_PER_RUN = 16    # line-up checks per run, at most


def espn_lineups(state):
    """Confirmed line-ups from ESPN for the games it follows, from 75 minutes before kick-off."""
    elu = state.setdefault('elu', {})
    for k in [k for k, v in elu.items() if parse_iso(v['ko']) < NOW - timedelta(hours=6)]:
        del elu[k]
    due = []
    for mid, (slug, eid, kos) in (state.get('espn_ev') or {}).items():
        r = elu.setdefault(mid, {'ko': kos})
        mins = (parse_iso(kos) - NOW).total_seconds() / 60
        if r.get('lu') or not (-20 <= mins <= ESPN_LU_FROM):
            continue
        if r.get('try') and (NOW - parse_iso(r['try'])).total_seconds() / 60 < ESPN_LU_EVERY - 1 and not FORCE:
            continue
        due.append((kos, mid, slug, eid))
    found = 0
    for kos, mid, slug, eid in sorted(due)[:ESPN_LU_PER_RUN]:
        r = elu[mid]
        r['try'] = iso(NOW)
        lu = espn_lineup(slug, eid)
        if lu:
            r['lu'], r['at'] = lu, iso(NOW)
            lk = next((k for k, v in ESPN_SLUG.items() if v == slug), None)
            if lk:
                state.setdefault('lineup_lk', {})[lk] = iso(NOW)
            found += 1
    for k in [k for k, v in elu.items() if not v.get('lu') and not v.get('try')]:
        del elu[k]
    return found


def espn_publish(state, doc):
    """Tell the site which ESPN scoreboard carries each competition, and each match's ESPN game, so phones
    can follow the games in play straight from ESPN between the site's updates."""
    ev = state.get('espn_ev') or {}
    doc['espn'] = {k: ESPN_SLUG[k] for k in sorted(doc.get('leagues') or {}) if k in ESPN_SLUG}
    for m in doc['matches']:
        if m['id'] in ev:
            m['ev'] = ev[m['id']][1]
        else:
            m.pop('ev', None)


def refresh_live(state, matches):
    """Live scores, fast results and line-ups from ESPN's public scoreboards (no key, no daily limit)."""
    r = refresh_espn(state, matches)
    n_lu = espn_lineups(state)
    lv = (state.get('af') or {}).get('live') or {}
    n_play = sum(1 for v in (lv.get('games') or {}).values() if v[3] != 'FT')
    today = NOW.strftime('%Y-%m-%d')
    n_res = sum(1 for v in ((state.get('af') or {}).get('scores') or {}).values() if v[2][:10] == today)
    ok = not r['calls'] or r['boards'] > 0
    note = (f"ESPN · {n_play} in play · {n_res} results today{f' · {n_lu} new line-ups' if n_lu else ''} · "
            f"{r['calls']} scoreboard{'s' if r['calls'] != 1 else ''} read" if r['calls'] else f"ESPN · no games in play{f' · {n_res} results today' if n_res else ''}")
    if r['calls'] and not r['boards']:
        note = 'ESPN did not answer this run'
    return ok, note


# ----------------------------------------------------------------------------------------------
# Team news (optional): injuries, suspensions and confirmed line-ups from API-Football.
# The free plan allows 100 calls a day, so they are rationed: the fixture list for today and
# tomorrow every 6 h, injuries for each day every 4 h, and line-ups from about an hour before
# kick-off, checked every 15 min until both teams are in. The news is shown on the site; the
# probabilities move with the bookmakers' prices, which react to team news, and a confirmed
# line-up asks for a fresh price for that competition.
# ----------------------------------------------------------------------------------------------
AF_KEY = (ENV('API_FOOTBALL_KEY') or '').strip()
AF = 'https://v3.football.api-sports.io'
AF_DAILY = int(float(ENV('API_FOOTBALL_DAILY') or 100))
AF_FIX_EVERY = 6        # hours between fixture-list downloads, per day
AF_INJ_EVERY = 4        # hours between injury downloads, per day
AF_LU_FROM = 60         # minutes before kick-off to start asking for line-ups
AF_LU_EVERY = 20        # minutes between line-up checks for a match
AF_BATCH = 20           # fixtures per call (the API's maximum)
AF_RESERVE = 20         # calls line-ups leave unused each day, so results always get through
AF_DONE = ('FT', 'AET', 'PEN')                              # finished (scores are the 90-minute result)
AF_OFF = ('PST', 'CANC', 'ABD', 'AWD', 'WO', 'SUSP')        # postponed, cancelled, abandoned or awarded
AF_LIVE_MAX = int(float(ENV('API_FOOTBALL_LIVE') or 40))     # live-score checks a day, at most
AF_LIVE_MIN = 10        # minutes between live-score checks, at the fastest
AF_LIVE_RESERVE = 25    # live scores stop when this many of the day's calls are left (line-ups and results come first)


def af_blocked(state):
    b = (state.get('af') or {}).get('block_until')
    return bool(b and parse_iso(b) > NOW)


def af_kind(path, params):
    """A kind of request, by endpoint and parameter names: '/fixtures?ids', '/injuries?date'."""
    return path + '?' + ','.join(sorted(k for k in params if k != 'timezone'))


def af_get(path, params, state, reserve=2):
    """One API-Football call -> (response list or None, error text or None). Keeps to the daily allowance,
    leaving `reserve` calls unused (line-ups keep a bigger reserve so results always get through)."""
    af = state.setdefault('af', {})
    day = NOW.strftime('%Y-%m-%d')
    if af.get('day') != day:
        af.update({'day': day, 'used': 0, 'left': None})
    if af_blocked(state):
        return None, 'paused'
    nope = (af.get('nope') or {}).get(af_kind(path, params))
    if nope and parse_iso(nope[0]) > NOW:
        return None, 'not on your plan'
    if af['used'] >= AF_DAILY - reserve or (af.get('left') is not None and af['left'] <= reserve):
        return None, 'daily allowance used'
    if af['used'] and not OFFLINE:
        time.sleep(0.4)   # stay well inside the per-minute limit
    r = fetch(AF + path, params, headers={'x-apisports-key': AF_KEY})
    af['used'] += 1
    left = r.headers.get('x-ratelimit-requests-remaining')
    if left is not None:
        af['left'] = fnum(left)
    if r.status != 200:
        log('API-Football', path, 'status', r.status)
        return None, f'status {r.status}'
    try:
        data = json.loads(text_of(r.body))
    except ValueError:
        return None, 'unreadable reply'
    err = data.get('errors')
    if err:
        msg = '; '.join(f'{k}: {v}' for k, v in err.items()) if isinstance(err, dict) else '; '.join(map(str, err))
        log('API-Football', path, params, '->', msg[:300])
        keys = set(err) if isinstance(err, dict) else set()
        if 'token' in keys or 'access' in keys:
            af['block_until'], af['note'] = iso(NOW + timedelta(hours=6)), 'the key was refused'
        elif 'requests' in keys:
            tomorrow = (NOW + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            af['block_until'], af['note'] = iso(tomorrow), "today's allowance is used up"
        elif 'plan' in keys and re.search(r'this date|from \d{4}-\d{2}-\d{2} to', msg):
            pass   # the free plan only serves yesterday, today and tomorrow: just this date is out of reach
        elif 'plan' in keys or re.search(r'parameter|season', msg, re.I):
            # one kind of request the plan doesn't serve (a parameter, a season): only that kind waits a week,
            # everything else carries on
            af.setdefault('nope', {})[af_kind(path, params)] = [iso(NOW + timedelta(days=7)), msg[:160]]
        return None, msg
    return data.get('response') or [], None


def af_names(s):
    """Loose team-name forms for matching another provider's names: normalised, and its word set."""
    n = norm(s)
    return n, set(n.split())


SIDE_MARKS = {'w', 'women', 'woman', 'fem', 'femenino', 'feminin', 'frauen', 'u17', 'u18', 'u19', 'u20', 'u21', 'u23',
              'ii', 'b', 'c', 'reserves', 'res', 'youth', 'academy', 'jong'}


def team_sim(a, b):
    if same_team(a, b):
        return 1.0
    (na, ta), (nb, tb) = af_names(a), af_names(b)
    if not na or not nb or (ta ^ tb) & SIDE_MARKS:   # never pair a club with its women's, youth or reserve side
        return 0.0
    if ta and tb and (ta <= tb or tb <= ta):
        return 0.9
    return difflib.SequenceMatcher(None, na, nb).ratio()


def af_link(state, matches):
    """Pair Statpad's matches in the next ~40 h with API-Football fixture ids (for team news)."""
    tn = state.setdefault('tn', {})
    fxs = af_rows(state)
    linked = 0
    for m in matches:
        ko = parse_iso(m['ko'])
        if not (NOW - timedelta(hours=3) < ko < NOW + timedelta(hours=40)):
            continue
        r = tn.get(m['id'])
        if r and r.get('fid'):
            linked += 1
            continue
        f = af_match(m['lk'], ko, m['h'], m['a'], fxs)
        if f:
            tn[m['id']] = {'fid': f[0], 'hid': f[6], 'aid': f[7], 'ko': m['ko'], 'lk': m['lk'], 'h': m['h'], 'a': m['a']}
            linked += 1
    return linked


def af_injuries(state, days):
    af, tn = state['af'], state.setdefault('tn', {})
    at, store = af.setdefault('inj_at', {}), af.setdefault('inj', {})
    for ds in days:
        if not any(r.get('fid') and r['ko'][:10] == ds for r in tn.values()):
            continue
        if at.get(ds) and NOW - parse_iso(at[ds]) < timedelta(hours=AF_INJ_EVERY) and not FORCE:
            continue
        rows, err = af_get('/injuries', {'date': ds}, state)
        if rows is None:
            continue
        at[ds] = iso(NOW)
        by = {}
        for it in rows:
            fid = (it.get('fixture') or {}).get('id')
            pl = it.get('player') or {}
            if not fid or not pl.get('name'):
                continue
            typ = 'q' if 'question' in (pl.get('type') or '').lower() else 'x'
            by.setdefault(str(fid), []).append([(it.get('team') or {}).get('id'), pl['name'], typ, (pl.get('reason') or '').strip()])
        store[ds] = by
    for ds in [d for d in store if d < (NOW - timedelta(days=1)).strftime('%Y-%m-%d')]:
        store.pop(ds, None)
        at.pop(ds, None)


def af_lineups(state):
    """Confirmed line-ups from API-Football, one fixture per call (the free plan refuses lists of ids), for the
    games ESPN doesn't carry or hasn't given line-ups for 15 minutes before kick-off. Two tries a game at most."""
    tn, elu, ev = state.setdefault('tn', {}), state.get('elu') or {}, state.get('espn_ev') or {}
    due = []
    for mid, r in tn.items():
        if not r.get('fid') or r.get('lu') or (elu.get(mid) or {}).get('lu') or r.get('lu_n', 0) >= 2:
            continue
        mins = (parse_iso(r['ko']) - NOW).total_seconds() / 60
        if not (-20 <= mins <= (15 if mid in ev else AF_LU_FROM)):
            continue
        if r.get('lu_try') and (NOW - parse_iso(r['lu_try'])).total_seconds() / 60 < AF_LU_EVERY - 1 and not FORCE:
            continue
        due.append(r)
    due.sort(key=lambda r: r['ko'])
    found = 0
    for r in due[:6]:
        resp, err = af_get('/fixtures/lineups', {'fixture': r['fid']}, state, reserve=AF_RESERVE)
        if resp is None:
            break
        r['lu_try'], r['lu_n'] = iso(NOW), r.get('lu_n', 0) + 1
        side = {}
        for x in resp:
            tid = (x.get('team') or {}).get('id')
            key = 'h' if tid == r['hid'] else 'a' if tid == r['aid'] else None
            xi = [[(p.get('player') or {}).get('number'), (p.get('player') or {}).get('name'), (p.get('player') or {}).get('pos')]
                  for p in x.get('startXI') or []]
            if key and len(xi) >= 7:
                side[key] = {'fm': x.get('formation'), 'xi': xi, 'co': (x.get('coach') or {}).get('name')}
        if 'h' in side and 'a' in side:
            r['lu'], r['lu_at'] = side, iso(NOW)
            state.setdefault('lineup_lk', {})[r['lk']] = iso(NOW)
            found += 1
    return found


def af_fixture_row(f):
    fi, te, lg, sc = f.get('fixture') or {}, f.get('teams') or {}, f.get('league') or {}, f.get('score') or {}
    h, a = te.get('home') or {}, te.get('away') or {}
    if not (fi.get('id') and fi.get('date') and h.get('name') and a.get('name')):
        return None
    st = (fi.get('status') or {}).get('short') or ''
    ft = sc.get('fulltime') or {}
    gl = f.get('goals') or {}
    hs, as_ = ft.get('home'), ft.get('away')
    if hs is None and st == 'FT':
        hs, as_ = gl.get('home'), gl.get('away')
    return [fi['id'], fi['date'], h['name'], a['name'], lg.get('name'), lg.get('country'), h.get('id'), a.get('id'), st, hs, as_, lg.get('id')]


def af_window(ds):
    """API-Football's free plan serves fixture lists for yesterday, today and tomorrow (UTC) only."""
    return (NOW - timedelta(days=1)).strftime('%Y-%m-%d') <= ds <= (NOW + timedelta(days=1)).strftime('%Y-%m-%d')


def af_day(state, ds, why):
    if not af_window(ds):
        return False
    fail = state['af'].setdefault('fail', {})
    if fail.get(ds) and NOW - parse_iso(fail[ds]) < timedelta(minutes=60) and not FORCE:
        return False   # a failed download waits an hour before it's tried again
    resp, err = af_get('/fixtures', {'date': ds, 'timezone': 'UTC'}, state)
    if resp is None:
        if err not in ('paused', 'daily allowance used'):
            fail[ds] = iso(NOW)
        return False
    fail.pop(ds, None)
    lst = [r for r in (af_fixture_row(f) for f in resp) if r]
    state['af'].setdefault('fx', {})[ds] = {'at': iso(NOW), 'list': lst}
    log(f'API-Football: {len(lst)} fixtures on {ds} ({why})')
    return True


def af_match(lk, ko, h, a, fxs):
    """The API-Football fixture for one of our matches. fxs: [(kick-off, row)] sorted by time."""
    import bisect
    times = [x[0] for x in fxs]
    best, best_s = None, 0.0
    country = (L.get(lk) or {}).get('country') or ''
    for fko, f in fxs[bisect.bisect_left(times, ko - timedelta(hours=3)):bisect.bisect_right(times, ko + timedelta(hours=3))]:
        dt = abs((fko - ko).total_seconds()) / 60
        sh, sa = team_sim(f[2], h), team_sim(f[3], a)
        if not (sh == 1 and sa == 1) and country and f[5] and f[5].lower() not in ('world', 'europe') and f[5].lower() != country.lower():
            continue   # loose name matches must at least be in the same country
        ok = (sh == 1 and sa == 1) or (dt <= 15 and ((sh == 1 and sa >= 0.5) or (sa == 1 and sh >= 0.5) or (sh >= 0.8 and sa >= 0.8)))
        if ok and sh + sa - dt / 600 > best_s:
            best, best_s = f, sh + sa - dt / 600
    return best


def af_rows(state):
    fxs = []
    for rec in ((state.get('af') or {}).get('fx') or {}).values():
        fxs.extend((parse_iso(f[1]), f) for f in rec['list'] if f[1])
    fxs.sort(key=lambda x: x[0])
    return fxs


def af_results(state):
    """Fast results: grade finished games from API-Football's day lists, refetching a day's list
    (at most hourly for today, every 3 h for the two days before) while its games await a result."""
    af = state['af']
    fx = af.setdefault('fx', {})
    done = af.setdefault('scores', {})      # our match id -> [home, away, kick-off]
    off = af.setdefault('void', {})         # our match id -> [status, kick-off]
    miss = af.setdefault('nomatch', {})     # our match id -> kick-off (not in API-Football's lists)
    ev = state.get('espn_ev') or {}
    pend = {k: r for k, r in (state.get('track_pending') or {}).items()
            if r['id'] not in ev or parse_iso(r['ko']) < NOW - timedelta(hours=4)}   # ESPN grades the games it follows
    lo, hi = NOW - timedelta(days=3), NOW - timedelta(minutes=110)

    def waiting():
        out = {}
        for r in pend.values():
            ko = parse_iso(r['ko'])
            if lo <= ko <= hi and r['id'] not in done and r['id'] not in off and r['id'] not in miss:
                out.setdefault(ko.strftime('%Y-%m-%d'), []).append(ko)
        return out

    today = NOW.strftime('%Y-%m-%d')
    pulled = 0
    for ds, kos in sorted(waiting().items(), reverse=True):
        if not af_window(ds):
            continue   # older days are left to football-data
        rec = fx.get(ds)
        ended = max(kos) + timedelta(minutes=110)
        gap = timedelta(minutes=60 if ds == today else 180)
        if pulled < 3 and (not rec or (parse_iso(rec['at']) < ended and NOW - parse_iso(rec['at']) >= gap) or FORCE):
            pulled += af_day(state, ds, 'results')
    fxs = af_rows(state)
    graded = 0
    for r in pend.values():
        ko = parse_iso(r['ko'])
        if not (lo <= ko <= hi) or r['id'] in done or r['id'] in off or r['id'] in miss:
            continue
        rec = fx.get(r['ko'][:10])
        f = af_match(r['lk'], ko, r['h'], r['a'], fxs)
        if not f:
            if rec and parse_iso(rec['at']) >= ko + timedelta(minutes=110):
                miss[r['id']] = r['ko']   # a list from after the game doesn't have it: stop waiting for it here
            continue
        if f[8] in AF_DONE and f[9] is not None and f[10] is not None:
            done[r['id']] = [int(f[9]), int(f[10]), r['ko']]
            graded += 1
        elif f[8] in AF_OFF:
            off[r['id']] = [f[8], r['ko']]
    for blob in (done, off, miss):
        for k in [k for k, v in blob.items() if parse_iso(v[-1] if isinstance(v, list) else v) < NOW - timedelta(days=30)]:
            del blob[k]
    return graded


def af_live(state):
    """Live scores for the games in play that ESPN doesn't follow, from one call that lists every live game. The
    day's live checks are spread over the time games are still in play, so a busy Saturday gets fewer per hour."""
    af = state['af']
    ev = state.get('espn_ev') or {}
    tn = {mid: r for mid, r in (state.get('tn') or {}).items() if mid not in ev}
    lv = af.setdefault('live', {})
    day = NOW.strftime('%Y-%m-%d')
    if lv.get('day') != day:
        lv.update({'day': day, 'used': 0})
    games = lv.setdefault('games', {})
    for mid in [k for k, v in games.items() if NOW - parse_iso(v[4]) > timedelta(hours=4)]:
        del games[mid]
    done = af.get('scores') or {}
    off = af.get('void') or {}
    span = lambda r: (parse_iso(r['ko']), parse_iso(r['ko']) + timedelta(minutes=125))
    inplay = {mid: r for mid, r in tn.items() if r.get('fid') and span(r)[0] <= NOW <= span(r)[1] and mid not in done and mid not in off}
    if not inplay:
        return 0
    end = (NOW + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    ivs = sorted((max(NOW, a), min(end, b)) for a, b in (span(r) for r in tn.values() if r.get('fid')) if b > NOW and a < end)
    mins, cur = 0.0, None
    for a, b in ivs:   # minutes of play still to come today (overlaps counted once)
        if cur and a <= cur[1]:
            cur[1] = max(cur[1], b)
        else:
            if cur:
                mins += (cur[1] - cur[0]).total_seconds() / 60
            cur = [a, b]
    if cur:
        mins += (cur[1] - cur[0]).total_seconds() / 60
    left = AF_LIVE_MAX - lv.get('used', 0)
    if left <= 0:
        return 0
    gap = max(AF_LIVE_MIN, mins / left)
    if lv.get('af_at') and (NOW - parse_iso(lv['af_at'])).total_seconds() / 60 < gap - 1 and not FORCE:
        return 0
    resp, err = af_get('/fixtures', {'live': 'all'}, state, reserve=AF_LIVE_RESERVE)
    if resp is None:
        return 0
    lv['used'] = lv.get('used', 0) + 1
    lv['af_at'] = lv['at'] = iso(NOW)
    by_fid = {(f.get('fixture') or {}).get('id'): f for f in resp}
    n = 0
    for mid, r in inplay.items():
        f = by_fid.get(r['fid'])
        if not f:
            continue
        st = (f.get('fixture') or {}).get('status') or {}
        gl = f.get('goals') or {}
        if gl.get('home') is None or gl.get('away') is None:
            continue
        minute = int(st.get('elapsed') or 0) + int(st.get('extra') or 0)
        hs, as_ = int(gl['home']), int(gl['away'])
        prev = games.get(mid)
        goals, reds, exact = live_events(f, hs, as_)
        if goals is None:
            # no event list: each new goal is placed halfway between the last check and this one
            goals = [list(g) for g in (prev[5] if prev and len(prev) > 5 else [])]
            reds = [list(x) for x in (prev[6] if prev and len(prev) > 6 else [])]
            ph, pa_, pm = (prev[0], prev[1], prev[2]) if prev else (0, 0, 0)
            when = round((pm + minute) / 2)
            goals += [[when, 'h']] * max(0, hs - ph) + [[when, 'a']] * max(0, as_ - pa_)
            for side, want in (('h', hs), ('a', as_)):   # a goal ruled out: drop the latest
                while sum(1 for g in goals if g[1] == side) > want:
                    goals.remove(max((g for g in goals if g[1] == side), key=lambda g: g[0]))
            goals.sort()
        games[mid] = [hs, as_, minute, st.get('short') or '', iso(NOW), goals, reds, exact]
        n += 1
    return n


def live_events(f, hs, as_):
    """Goals and red cards with their minutes, from the fixture's event list when the live feed includes one.
    Trusted only if the goals add up to the score."""
    ev = f.get('events')
    if not isinstance(ev, list) or not ev:
        return None, [], 0
    hid = ((f.get('teams') or {}).get('home') or {}).get('id')
    gs, rs = [], []
    for e in ev:
        tm = e.get('time') or {}
        mn = int(tm.get('elapsed') or 0) + int(tm.get('extra') or 0)
        side = 'h' if (e.get('team') or {}).get('id') == hid else 'a'
        typ, det = (e.get('type') or '').lower(), (e.get('detail') or '').lower()
        if typ == 'goal' and 'missed' not in det:
            gs.append([mn, side, 'own' in det])
        elif typ == 'card' and ('red' in det or 'second yellow' in det):
            rs.append([mn, side])
    for flip in (False, True):   # feeds differ on which team an own goal is listed under
        g2 = [[m, ('a' if s == 'h' else 'h') if (o and flip) else s] for m, s, o in gs]
        if sum(1 for g in g2 if g[1] == 'h') == hs and sum(1 for g in g2 if g[1] == 'a') == as_:
            return sorted(g2), sorted(rs), 1
    return None, [], 0


def publish_live(state):
    """Scores of games in play (and the last seen state of games that just ended), for the cash-out adviser."""
    lv = ((state.get('af') or {}).get('live') or {})
    done = (state.get('af') or {}).get('scores') or {}
    games = {}
    for mid, v in (lv.get('games') or {}).items():
        if mid in done:   # finished: the final score, with the goal times seen while it was live
            v = [done[mid][0], done[mid][1], max(90, v[2]), 'FT', v[4]] + list(v[5:])
        games[mid] = v
    return {'app': 'Statpad', 'generated': iso(NOW), 'checked': lv.get('at'), 'games': games}


def refresh_team_news(state, matches):
    if not AF_KEY:
        return 'no key'
    af = state.setdefault('af', {})
    note0 = af.get('note') or ''
    if 'this date' in note0 or note0.startswith('your plan does not cover this'):
        # older versions paused every API-Football call for a day when the plan refused one kind of request
        # (a date, or lists of fixture ids): undo that; now only the refused kind waits
        af.pop('block_until', None)
        af.pop('note', None)
    af.pop('inj_mode', None)
    for k in [k for k, v in (af.get('nope') or {}).items() if parse_iso(v[0]) < NOW]:
        del af['nope'][k]
    if af_blocked(state):
        return 'paused: ' + (af.get('note') or '')
    fx = af.setdefault('fx', {})
    days = [NOW.strftime('%Y-%m-%d'), (NOW + timedelta(days=1)).strftime('%Y-%m-%d')]
    graded = af_results(state)    # results first: they matter more than news
    for ds in days:
        rec = fx.get(ds)
        if rec and NOW - parse_iso(rec['at']) < timedelta(hours=AF_FIX_EVERY) and not FORCE:
            continue
        af_day(state, ds, 'team news')
    for ds in [d for d in fx if d < (NOW - timedelta(days=3)).strftime('%Y-%m-%d')]:
        del fx[ds]
    linked = af_link(state, matches)
    af_injuries(state, days)
    got = af_lineups(state)
    n_live = af_live(state)
    tn = state.setdefault('tn', {})
    for mid in [k for k, r in tn.items() if parse_iso(r['ko']) < NOW - timedelta(hours=6)]:
        del tn[mid]
    for lk in [k for k, v in (state.get('lineup_lk') or {}).items() if parse_iso(v) < NOW - timedelta(hours=6)]:
        del state['lineup_lk'][lk]
    n_lu = sum(1 for r in tn.values() if r.get('lu'))
    note = (f"{linked} matches linked, {n_lu} line-ups in{f' ({got} new)' if got else ''}, "
            f"{graded} new results{f', {n_live} live scores' if n_live else ''}, {af.get('used', 0)} calls today")
    words = {'/fixtures?live': 'live scores', '/fixtures/lineups?fixture': 'line-ups', '/injuries?date': 'injuries',
             '/fixtures?date': 'fixture lists', '/teams?country': 'club lists for crests'}
    nope = sorted(words.get(k, k) for k, v in (af.get('nope') or {}).items() if parse_iso(v[0]) > NOW)
    if nope:
        note += ' · your plan refuses ' + ', '.join(nope) + ' (asked again weekly)'
    if af.get('note') and af_blocked(state):
        note += ' · paused: ' + af['note']
    return note


def team_news_for(state, mid, lk, ko, h, a):
    """The news for one match, in the compact form the site reads, or None."""
    tn = state.get('tn') or {}
    r = tn.get(mid)
    if not r:
        for x in tn.values():
            if x.get('lk') == lk and abs((parse_iso(x['ko']) - ko).total_seconds()) < 3 * 3600 and same_team(x['h'], h) and same_team(x['a'], a):
                r = x
                break
    elu = ((state.get('elu') or {}).get(mid) or {}).get('lu')
    if (not r or not r.get('fid')) and not elu:
        return None
    r = r if r and r.get('fid') else {}
    inj = (((state.get('af') or {}).get('inj') or {}).get(r['ko'][:10]) or {}).get(str(r['fid'])) or [] if r else []
    out = {'h': [], 'a': []}
    for tid, name, typ, why in inj:
        side = 'h' if tid == r['hid'] else 'a' if tid == r['aid'] else None
        if side and len(out[side]) < 14 and [name, why, typ] not in out[side]:
            out[side].append([name, why, typ])
    lu = r.get('lu') or elu or {}
    if not (out['h'] or out['a'] or lu):
        return None
    news = {'h': {'out': out['h']}, 'a': {'out': out['a']}, 'lu': bool(lu)}
    for side in ('h', 'a'):
        if lu.get(side):
            news[side].update({'fm': lu[side].get('fm'), 'xi': lu[side].get('xi'), 'co': lu[side].get('co')})
    return news


# ----------------------------------------------------------------------------------------------
# Self-tuning: the track record recalibrates the chances Statpad states.
# ----------------------------------------------------------------------------------------------
CAL_MIN = 150        # graded top picks before it switches on
CAL_PRIOR = 0.3      # spread of the prior around "no change", on the log-odds scale


def fit_calib(picks):
    """Fit log-odds(landed) = log-odds(stated) + c + (b - 1) * (log-odds(stated) - log-odds(75%)) on graded top picks
    from the last 90 days: c is an overall shift at 75%, b stretches or squeezes around it. It uses the chance
    Statpad stated before any correction, so it never feeds on itself, and priors centred on "no change"
    keep it modest until the evidence is strong. Published as a, b with log-odds(landed) = a + b * log-odds(stated)."""
    x0 = math.log(3)   # log-odds of 75%
    xs, ys = [], []
    for p in picks:
        raw = p[18] if len(p) > 18 and p[18] is not None else p[9]
        if raw is None:
            continue
        q = min(0.98, max(0.02, float(raw)))
        xs.append(math.log(q / (1 - q)))
        ys.append(1 if p[13] else 0)
    n = len(xs)
    out = {'on': False, 'n': n, 'need': CAL_MIN}
    if n < CAL_MIN:
        return out
    c, b, sc, sb = 0.0, 1.0, CAL_PRIOR ** 2, (CAL_PRIOR * 0.8) ** 2
    for _ in range(40):
        gc, gb, hcc, hcb, hbb = -c / sc, -(b - 1) / sb, -1 / sc, 0.0, -1 / sb
        for x, y in zip(xs, ys):
            u = x - x0
            pr = 1 / (1 + math.exp(-(x0 + c + b * u)))
            w = pr * (1 - pr)
            gc += y - pr
            gb += (y - pr) * u
            hcc -= w
            hcb -= w * u
            hbb -= w * u * u
        det = hcc * hbb - hcb * hcb
        if abs(det) < 1e-12:
            break
        dc, db = (hbb * gc - hcb * gb) / det, (hcc * gb - hcb * gc) / det
        c, b = c - dc, b - db
        if abs(dc) + abs(db) < 1e-9:
            break
    c, b = max(-0.6, min(0.6, c)), max(0.7, min(1.3, b))
    out.update({'on': True, 'a': round(x0 + c - b * x0, 4), 'b': round(b, 4), 'shift': round(c, 4)})
    return out


# ----------------------------------------------------------------------------------------------
# Step 5: build matches.json
# ----------------------------------------------------------------------------------------------
def expected_goals(lk, h, a, leagues, teams):
    """Expected goals (home, away) from ratings; cross-league when the teams play in different leagues."""
    lg = leagues.get(lk)
    if lg and lg.get('ratings') and h in lg['ratings'] and a in lg['ratings']:
        ah, dh = lg['ratings'][h]
        aa, da = lg['ratings'][a]
        lh, la = math.exp(lg['mu'] + lg['home'] + ah + da), math.exp(lg['mu'] + aa + dh)
        el = lg.get('elo') or {}
        if ELO and h in el and a in el and lg.get('gm'):   # the tuned mix with Elo, same league only
            lh, la = elo_mix(lh, la, elo_lams(el[h] + ELO['hfa'] - el[a], lg['gm'][0], lg['gm'][1], ELO), ELO['v'])
        kl = lg.get('kal') or {}
        if KAL and h in kl and a in kl and lg.get('kbase'):   # and with the game-by-game ratings
            st_h = kl[h][:4] + [datetime.strptime(kl[h][4], '%Y-%m-%d').replace(tzinfo=UTC)]
            st_a = kl[a][:4] + [datetime.strptime(kl[a][4], '%Y-%m-%d').replace(tzinfo=UTC)]
            kh, ka, _, _ = kal_lams(st_h, st_a, lg['kbase'], KAL['q'], NOW)
            lh, la = elo_mix(lh, la, (kh, ka), KAL['v'])
        return [round(lh, 4), round(la, 4)], False
    # cross-league: each side's own league ratings plus a league-strength offset
    rh = teams.resolve_global(h, pool_=pool(lk))
    ra = teams.resolve_global(a, pool_=pool(lk))
    if not rh or not ra:
        return None, False
    lh_, th = rh
    la_, ta = ra
    gh, ga = leagues.get(lh_), leagues.get(la_)
    if not gh or not ga or th not in gh.get('ratings', {}) or ta not in ga.get('ratings', {}):
        return None, False
    sh, sa = L[lh_]['strength'], L[la_]['strength']
    ah, dh = gh['ratings'][th]
    aa, da = ga['ratings'][ta]
    mu = (gh['mu'] + ga['mu']) / 2
    home = 0.22 if is_cup(lk) else (gh['home'] + ga['home']) / 2
    lam_h = math.exp(mu + home + ah + sh / 2 + da - sa / 2)
    lam_a = math.exp(mu + aa + sa / 2 + dh - sh / 2)
    return [round(lam_h, 4), round(lam_a, 4)], True


def lam_sd(lk, h, a, leagues, teams, cross):
    """Standard error of log expected goals (home, away) from the ratings' standard errors."""
    if not cross:
        se = (leagues.get(lk) or {}).get('se') or {}
        if h in se and a in se:
            return [round(math.hypot(se[h][0], se[a][1]), 3), round(math.hypot(se[a][0], se[h][1]), 3)]
        return None
    rh, ra = teams.resolve_global(h, pool_=pool(lk)), teams.resolve_global(a, pool_=pool(lk))
    if not rh or not ra:
        return None
    sh_ = ((leagues.get(rh[0]) or {}).get('se') or {}).get(rh[1])
    sa_ = ((leagues.get(ra[0]) or {}).get('se') or {}).get(ra[1])
    if not sh_ or not sa_:
        return None
    return [round(math.hypot(sh_[0], sa_[1]), 3), round(math.hypot(sa_[0], sh_[1]), 3)]   # the engine adds the league-gap part


# ----------------------------------------------------------------------------------------------
# Club crests and competition logos. API-Football's fixture lists (already fetched for team news and
# results) carry each team's id; pairing them with our matches teaches Statpad which crest belongs to
# which name. Crests are downloaded once from API-Football's public image server, shrunk to two small
# sizes with the pure-Python PNG code below (the runner has no Pillow), kept with the state and
# published to crests/ on the site. Names whose crest isn't downloaded yet get a negative id, which tells
# the page to load it straight from the image server meanwhile.
# ----------------------------------------------------------------------------------------------
CREST_TEAM = 'https://media.api-sports.io/football/teams/{}.png'
CREST_LEAGUE = 'https://media.api-sports.io/football/leagues/{}.png'
CREST_SMALL, CREST_BIG = 60, 120      # px: list rows / match pages
CREST_PER_RUN = 60
# Crests for well-known clubs, so they show from the first run (each id checked against its picture);
# ids learnt from API-Football's fixtures take precedence.
CREST_SEED = {
    'Arsenal': 42, 'Aston Villa': 66, 'Bournemouth': 35, 'Brentford': 55, 'Brighton': 51, 'Chelsea': 49,
    'Coventry': 1346, 'Crystal Palace': 52, 'Everton': 45, 'Fulham': 36, 'Hull': 64, 'Ipswich': 57, 'Leeds': 63,
    'Liverpool': 40, 'Manchester City': 50, 'Manchester United': 33, 'Newcastle': 34, 'Nottingham Forest': 65,
    'Sunderland': 746, 'Tottenham': 47, 'Alavés': 542, 'Athletic Club': 531, 'Atlético Madrid': 530,
    'Barcelona': 529, 'Celta Vigo': 538, 'Elche': 797, 'Espanyol': 540, 'Getafe': 546, 'Levante': 539,
    'Osasuna': 727, 'Rayo Vallecano': 728, 'Real Betis': 543, 'Real Madrid': 541, 'Real Sociedad': 548,
    'Sevilla': 536, 'Valencia': 532, 'Villarreal': 533, '1. FC Köln': 192, 'Augsburg': 170, 'Bayern Munich': 157,
    "Borussia M'gladbach": 163, 'Dortmund': 165, 'Eintracht Frankfurt': 169, 'Freiburg': 160, 'Hamburger SV': 175,
    'Hoffenheim': 167, 'Leverkusen': 168, 'Mainz': 164, 'RB Leipzig': 173, 'Schalke 04': 174, 'Stuttgart': 172,
    'Union Berlin': 182, 'Werder Bremen': 162, 'Atalanta': 499, 'Bologna': 500, 'Cagliari': 490, 'Como': 895,
    'Fiorentina': 502, 'Frosinone': 512, 'Genoa': 495, 'Inter': 505, 'Juventus': 496, 'Lazio': 487, 'Lecce': 867,
    'Milan': 489, 'Monza': 1579, 'Napoli': 492, 'Parma': 523, 'Roma': 497, 'Sassuolo': 488, 'Torino': 503,
    'Udinese': 494, 'Venezia': 517, 'Angers': 77, 'Auxerre': 108, 'Brest': 106, 'Le Havre': 111, 'Lens': 116,
    'Lille': 79, 'Lorient': 97, 'Lyon': 80, 'Marseille': 81, 'Monaco': 91, 'Nice': 84, 'Paris FC': 114,
    'Paris Saint-Germain': 85, 'Rennes': 94, 'Strasbourg': 95, 'Toulouse': 96, 'Troyes': 110, 'Ajax': 194,
    'PSV Eindhoven': 197, 'Feyenoord': 209, 'AZ Alkmaar': 201, 'Twente': 415, 'Benfica': 211, 'Porto': 212,
    'Sporting CP': 228, 'Braga': 217, 'Galatasaray': 645, 'Fenerbahce': 611, 'Besiktas': 549, 'Trabzonspor': 998,
    'Celtic': 247, 'Rangers': 257, 'Aberdeen': 252, 'Hearts': 254, 'Hibernian': 249, 'Club Brugge': 569,
    'Anderlecht': 554, 'Boca Juniors': 451, 'Inter Miami': 9568, 'Los Angeles FC': 1616,
    'Salzburg': 571, 'Olympiakos': 553, 'Union Saint-Gilloise': 1393, 'CFR Cluj': 2246,
    'Swansea': 76, 'Cardiff': 43, 'West Brom': 60, 'Preston': 59, 'Wrexham': 1837, 'Lincoln': 1379, 'Port Vale': 1351,
    'Peterboro': 1350, 'Gillingham': 1347, 'Swindon': 1353, 'Tranmere': 1381, 'Eibar': 545, 'Oviedo': 718, 'Santander': 4665,
    'Burgos': 9580, 'Almería': 723, 'Nijmegen': 413, 'Waregem': 600, 'AEK': 575, 'Shamrock Rovers': 652,
    'Flamengo RJ': 127, 'Atletico-MG': 1062, 'Botafogo RJ': 120, 'Athletico-PR': 134, 'Chapecoense-SC': 132,
    'Estudiantes L.P.': 450, 'Gimnasia L.P.': 434, 'Toronto FC': 1601, 'Vancouver Whitecaps': 1603, 'CF Montreal': 1614,
    'St. Louis City': 20787, 'Urawa Reds': 287, 'Cerezo Osaka': 291}
# API-Football's competition ids for Statpad's leagues (checked against their logos)
AF_LEAGUE = {'EPL': 39, 'LaLiga': 140, 'BUN': 78, 'SA': 135, 'L1': 61, 'POR': 94, 'ERE': 88, 'BEL': 144, 'KSA': 307, 'ENG2': 40, 'GER2': 79, 'ITA2': 136, 'ESP2': 141, 'FRA2': 62, 'NED2': 89, 'ENG3': 41, 'ENG4': 42, 'SCO1': 179, 'SCO2': 180, 'TUR1': 203, 'GRE1': 197, 'AUT1': 218, 'SUI1': 207, 'DEN1': 119, 'NOR1': 103, 'SWE1': 113, 'POL1': 106, 'ROU1': 283, 'CZE1': 345, 'SRB1': 286, 'IRL1': 357, 'FIN1': 244, 'BRA1': 71, 'ARG1': 128, 'MEX1': 262, 'USA1': 253, 'JPN1': 98, 'CHN1': 169, 'UCL': 2, 'UEL': 3, 'UECL': 848, 'FAC': 45, 'EFLC': 48, 'DFB': 81, 'CDR': 143, 'CI': 137, 'CDF': 66, 'UNL': 5, 'INTF': 10,
             'WSL': 44, 'LIGAF': 142, 'D1F': 64, 'FBL': 82, 'NWSL': 254, 'UWCL': 525, 'AUS1': 188, 'KOR1': 292, 'BRA2': 72, 'CHI1': 265,
             'COL1': 239, 'RSA1': 288, 'LIB': 13, 'SUD': 11}
CREST_BUDGET_S = 35
CREST_ES = 'https://a.espncdn.com/combiner/i?img=/i/teamlogos/soccer/500/{}.png&w=120&h=120'
ES_TEAMS = 'https://site.api.espn.com/apis/site/v2/sports/soccer/{}/teams'
ES_TEAMS_EVERY_D = 30   # days between refreshes of a competition's club list from ESPN
ES_TEAMS_PER_RUN = 20   # club lists per run, at most
# spellings that no rule can link: football-data's (or another feed's) name -> the club's name at ESPN
CREST_ALIAS = {'los angeles galaxy': 'LA Galaxy', 'inverness c': 'Inverness Caledonian Thistle', 'celta b': 'Celta Fortuna',
               'sociedad b': 'Real Sociedad II', 'guadalajara chivas': 'Guadalajara', 'santos laguna': 'Santos',
               'sint truiden': 'Sint-Truidense', 'hamkam': 'Hamarkameratene', 'henan songshan longmen': 'Henan',
               'buyuksehyr': 'Istanbul Basaksehir', 'erzurumspor': 'Erzurum BB', 'amedspor': 'Amed SFK'}
CREST_WORDS = {'utd': 'united', 'rvs': 'rovers', 'jrs': 'juniors', 'jnrs': 'juniors', 'ath': 'athletic', 'wed': 'wednesday', 'st': 'saint'}
AF_TEAMS_EVERY_D = 30   # days between refreshes of a country's club list
AF_TEAMS_PER_RUN = 4    # club-list calls per run, at most
AF_TEAMS_RESERVE = 35   # club lists wait while fewer than this many of the day's calls are left (team news and scores first)
FL_TREE = 'https://data.jsdelivr.com/v1/package/gh/luukhopman/football-logos@master'
FL_RAW = 'https://raw.githubusercontent.com/luukhopman/football-logos/master/'
FL_EVERY_D = 7

PNG_SIG = b'\x89PNG\r\n\x1a\n'


def png_decode(data):
    if data[:8] != PNG_SIG:
        raise ValueError('not a png')
    pos, idat, plte, trns, ihdr = 8, [], None, None, None
    while pos + 8 <= len(data):
        n, typ = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b'IHDR':
            ihdr = struct.unpack('>IIBBBBB', body)
        elif typ == b'PLTE':
            plte = body
        elif typ == b'tRNS':
            trns = body
        elif typ == b'IDAT':
            idat.append(body)
        elif typ == b'IEND':
            break
    if not ihdr:
        raise ValueError('no header')
    w, h, bd, ct, _, _, il = ihdr
    if il:
        raise ValueError('interlaced')
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    if w * h > 1_500_000:
        raise ValueError('too big')
    raw = zlib.decompress(b''.join(idat))
    bits = ch * bd
    stride = (w * bits + 7) // 8
    bpp = max(1, bits // 8)
    out = bytearray(stride * h)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    # to RGBA, 8 bits per channel
    px = bytearray(w * h * 4)
    if ct == 3:
        pal = [tuple(plte[i:i + 3]) for i in range(0, len(plte or b''), 3)]
        alpha = list(trns or b'')
        per = 8 // bd
        mask = (1 << bd) - 1
        for y in range(h):
            row = out[y * stride:(y + 1) * stride]
            for x in range(w):
                if bd == 8:
                    k = row[x]
                else:
                    byte = row[x // per]
                    k = (byte >> (8 - bd * (x % per + 1))) & mask
                r, g, b = pal[k] if k < len(pal) else (0, 0, 0)
                o = (y * w + x) * 4
                px[o:o + 4] = bytes((r, g, b, alpha[k] if k < len(alpha) else 255))
        return w, h, px
    step = ch * (bd // 8) if bd >= 8 else None
    if bd < 8:   # low bit-depth greyscale
        if ct != 0:
            raise ValueError('unsupported')
        per, mask, scale = 8 // bd, (1 << bd) - 1, 255 // ((1 << bd) - 1)
        for y in range(h):
            row = out[y * stride:(y + 1) * stride]
            for x in range(w):
                v = ((row[x // per] >> (8 - bd * (x % per + 1))) & mask) * scale
                o = (y * w + x) * 4
                px[o:o + 4] = bytes((v, v, v, 255))
        return w, h, px
    hi = 2 if bd == 16 else 1
    for y in range(h):
        row = out[y * stride:(y + 1) * stride]
        for x in range(w):
            s = row[x * step:(x + 1) * step:hi]
            o = (y * w + x) * 4
            if ct == 6:
                px[o:o + 4] = s
            elif ct == 2:
                px[o:o + 4] = bytes((s[0], s[1], s[2], 255))
            elif ct == 4:
                px[o:o + 4] = bytes((s[0], s[0], s[0], s[1]))
            else:
                px[o:o + 4] = bytes((s[0], s[0], s[0], 255))
    if trns and ct in (0, 2) and bd == 8:   # single transparent colour
        key = tuple(trns[1::2][:3]) if ct == 2 else (trns[1],) * 3
        for i in range(0, len(px), 4):
            if tuple(px[i:i + 3]) == key:
                px[i + 3] = 0
    return w, h, px


def _weights(n_in, n_out):
    """Area-average weights: for each output cell, [(input index, weight)]."""
    sc = n_in / n_out
    res = []
    for o in range(n_out):
        a, b = o * sc, (o + 1) * sc
        ws, i = [], int(a)
        while i < b and i < n_in:
            ws.append((i, min(b, i + 1) - max(a, i)))
            i += 1
        t = sum(w for _, w in ws) or 1
        res.append([(i, w / t) for i, w in ws])
    return res


def png_shrink(w, h, px, box):
    if w <= box and h <= box:
        return w, h, px
    k = box / max(w, h)
    W, H = max(1, round(w * k)), max(1, round(h * k))
    # premultiply
    pre = [0.0] * (w * h * 4)
    for i in range(0, w * h * 4, 4):
        a = px[i + 3] / 255
        pre[i], pre[i + 1], pre[i + 2], pre[i + 3] = px[i] * a, px[i + 1] * a, px[i + 2] * a, px[i + 3]
    wx, wy = _weights(w, W), _weights(h, H)
    tmp = [0.0] * (W * h * 4)
    for y in range(h):
        base = y * w * 4
        for X, ws in enumerate(wx):
            r = g = b = a = 0.0
            for i, f in ws:
                j = base + i * 4
                r += pre[j] * f; g += pre[j + 1] * f; b += pre[j + 2] * f; a += pre[j + 3] * f
            o = (y * W + X) * 4
            tmp[o], tmp[o + 1], tmp[o + 2], tmp[o + 3] = r, g, b, a
    out = bytearray(W * H * 4)
    for Y, ws in enumerate(wy):
        for X in range(W):
            r = g = b = a = 0.0
            for i, f in ws:
                j = (i * W + X) * 4
                r += tmp[j] * f; g += tmp[j + 1] * f; b += tmp[j + 2] * f; a += tmp[j + 3] * f
            o = (Y * W + X) * 4
            if a > 0.5:
                m = 255 / a
                out[o:o + 4] = bytes((min(255, round(r * m)), min(255, round(g * m)), min(255, round(b * m)), min(255, round(a))))
    return W, H, out


def png_encode(w, h, px):
    stride, rows, prev = w * 4, [], bytearray(w * 4)
    for y in range(h):
        line = px[y * stride:(y + 1) * stride]
        best, best_s = None, None
        for f in range(5):
            o = bytearray(stride)
            for i in range(stride):
                a = line[i - 4] if i >= 4 else 0
                b = prev[i]
                c = prev[i - 4] if i >= 4 else 0
                if f == 0:
                    v = line[i]
                elif f == 1:
                    v = line[i] - a
                elif f == 2:
                    v = line[i] - b
                elif f == 3:
                    v = line[i] - ((a + b) >> 1)
                else:
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    v = line[i] - (a if pa <= pb and pa <= pc else b if pb <= pc else c)
                o[i] = v & 255
            s = sum(v if v < 128 else 256 - v for v in o)
            if best_s is None or s < best_s:
                best, best_s = bytes([f]) + bytes(o), s
        rows.append(best)
        prev = line
    def chunk(t, b):
        return struct.pack('>I', len(b)) + t + b + struct.pack('>I', zlib.crc32(t + b) & 0xffffffff)
    return PNG_SIG + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b''.join(rows), 9)) + chunk(b'IEND', b'')


def small_png(data, box):
    Image = pillow()
    if Image:   # much faster, and reads every PNG flavour
        im = Image.open(io.BytesIO(data)).convert('RGBA')
        im.thumbnail((box, box), Image.LANCZOS)
        b = io.BytesIO()
        im.save(b, 'PNG', optimize=True)
        return b.getvalue()
    w, h, px = png_decode(data)
    return png_encode(*png_shrink(w, h, px, box))


def crest_names(doc):
    """(name, competition) for every team this board shows: match teams by kick-off first, then league tables."""
    seen, out = set(), []
    for m in sorted(doc['matches'], key=lambda m: m['ko']):
        for n in (m['h'], m['a']):
            if n not in seen:
                seen.add(n)
                out.append((n, m['lk']))
    for lk, lg in doc['leagues'].items():
        for r in lg.get('table') or []:
            if r[0] not in seen:
                seen.add(r[0])
                out.append((r[0], lk))
    return out


CREST_GENERIC = {'if', 'ik', 'bk', 'ff', 'aif', 'gif', 'boldklub', 'fotball', 'elite', 'bois', 'jk', 'fotbal', 'futebol',
                 'clube', 'sad'}


def crest_words(s):
    n = norm(s)
    return n, {t for t in n.split() if t not in CREST_GENERIC} or set(n.split())


def crest_sim(ours, theirs):
    """How surely another provider's club name is ours: 1 = same name, 0.9 = theirs is ours plus more words
    ('Salzburg' / 'Red Bull Salzburg', 'Djurgarden' / 'Djurgardens IF'), else a close spelling whose every word
    matches ('Goztep' / 'Göztepe'). Never the other way round ('Dundee United' is not 'Dundee')."""
    if same_team(ours, theirs):
        return 1.0
    (na, ta), (nb, tb) = crest_words(ours), crest_words(theirs)
    if not na or not nb or (set(na.split()) ^ set(nb.split())) & SIDE_MARKS:
        return 0.0
    if ta == tb:
        return 1.0
    abbr = {norm(w) for w in re.findall(r'([^\W\d_]+)\.', ours)}   # 'Univ.', 'Atl.', 'U.': shortened words
    def has(x, ys, pre):
        return any(x == y or (pre and min(len(x), len(y)) >= 4 and abs(len(x) - len(y)) <= 2 and (x.startswith(y) or y.startswith(x)))
                   or (pre and x in abbr and y.startswith(x))
                   or (not pre and difflib.SequenceMatcher(None, x, y).ratio() >= 0.8) for y in ys)
    if all(has(x, tb, True) for x in ta):
        return 0.9 if len(tb) > len(ta) or not all(has(y, ta, True) for y in tb) else 0.95
    r = difflib.SequenceMatcher(None, ' '.join(sorted(ta)), ' '.join(sorted(tb))).ratio()
    if r >= 0.86 and all(has(x, tb, True) or has(x, tb, False) for x in ta) and all(has(y, ta, True) or has(y, ta, False) for y in tb):
        return r
    return 0.0


def best_team(name, cands):
    """The one candidate [their name, value] that is surely this team, or None. Several seasons or spellings of
    the same club count once; two different clubs that fit equally well mean no match."""
    sc = sorted(((crest_sim(name, c[0]), i) for i, c in enumerate(cands)), key=lambda x: (-x[0], x[1]))   # ties: the first listed (newest)
    if not sc or sc[0][0] < 0.86:
        return None
    top, ti = sc[0]
    tn, tv = cands[ti]
    def same(i):
        n, v = cands[i]
        return v == tv or norm(n) == norm(tn) or max(crest_sim(n, tn), crest_sim(tn, n)) >= 0.86
    second = next((x for x, i in sc[1:] if x >= 0.86 and not same(i)), 0.0)
    if top >= 0.999 or (top >= 0.9 and second < 0.9) or second < top - 0.06:
        return tv
    return None


def cnorm(c):
    k = re.sub(r'[^a-z ]', ' ', unicodedata.normalize('NFKD', c or '').encode('ascii', 'ignore').decode().lower().replace('-', ' '))
    k = ' '.join(k.split())
    return {'turkiye': 'turkey', 'czech republic': 'czechia', 'united states': 'usa', 'korea republic': 'south korea'}.get(k, k)


# clubs that play in a neighbour's league (Welsh clubs in England's, Vaduz in Switzerland's, Canadian clubs in MLS...)
CREST_NEIGHBOURS = {'england': ['wales'], 'switzerland': ['liechtenstein'], 'spain': ['andorra'], 'france': ['monaco'], 'usa': ['canada']}


def team_country(lk):
    c = (L.get(lk) or {}).get('country') or ''
    return cnorm(c) if not knockout(lk) and c not in ('Europe', 'World', 'South America') else None


def af_country_teams(state, doc):
    """Every club in each country Statpad covers, with its API-Football id: one call per country (about 30),
    refreshed every few weeks, so a team's crest shows without waiting for it to play."""
    af = state.setdefault('af', {})
    ct = af.setdefault('ct', {})               # country -> {'at', 'ok', 'teams': [[name, id], ...]}
    need = {}
    have = state.get('crest') or {}
    for n, lk in crest_names(doc):
        c = team_country(lk)
        for cc in ([c] + CREST_NEIGHBOURS.get(c, [])) if c else []:
            need[cc] = need.get(cc, 0) + (n not in have)
    # the API's own spelling of each country, from its country list (fetched rarely)
    cl = af.get('countries')
    if not cl or NOW - parse_iso(cl['at']) > timedelta(days=60):
        rows, err = af_get('/teams/countries', {}, state, reserve=AF_TEAMS_RESERVE)
        if rows:
            af['countries'] = cl = {'at': iso(NOW), 'names': {cnorm(r.get('name')): r.get('name') for r in rows if r.get('name')}}
        elif err not in ('paused', 'daily allowance used'):
            af['countries'] = cl = {'at': iso(NOW - timedelta(days=59)), 'names': {}}   # try again tomorrow; use our spellings meanwhile
    spell = (cl or {}).get('names') or {}
    calls = 0
    for c in sorted(need, key=lambda c: -need[c]):
        rec = ct.get(c)
        if rec and NOW - parse_iso(rec['at']) < timedelta(days=AF_TEAMS_EVERY_D if rec.get('ok') else 2) and not FORCE:
            continue
        if calls >= AF_TEAMS_PER_RUN:
            break
        api = spell.get(c) or {'czechia': 'Czech-Republic', 'saudi arabia': 'Saudi-Arabia', 'usa': 'USA'}.get(c) or c.title().replace(' ', '-')
        rows, err = af_get('/teams', {'country': api}, state, reserve=AF_TEAMS_RESERVE)
        calls += 1
        if rows is None:
            if err in ('paused', 'daily allowance used'):
                break
            ct[c] = {'at': iso(NOW), 'ok': False, 'err': (err or '')[:120]}
            continue
        teams = [[(r.get('team') or {}).get('name'), (r.get('team') or {}).get('id')] for r in rows if not (r.get('team') or {}).get('national')]
        ct[c] = {'at': iso(NOW), 'ok': bool(teams), 'teams': [t for t in teams if t[0] and isinstance(t[1], int)]}
        log(f'API-Football: {len(ct[c]["teams"])} clubs listed for {api}')
    return calls


def fl_refresh(state):
    """The list of crests in the open football-logos collection (European top flights, this season and four
    before), as a fallback for clubs API-Football hasn't given an id for. Refreshed weekly; one small request."""
    fl = state.get('fl') or {}
    if fl.get('at') and NOW - parse_iso(fl['at']) < timedelta(days=FL_EVERY_D) and not FORCE:
        return False
    r = fetch(FL_TREE, tries=1, timeout=20)
    try:
        tree = json.loads(text_of(r.body)) if r.status == 200 else None
    except ValueError:
        tree = None
    if not tree:
        state['fl'] = dict(fl, at=iso(NOW - timedelta(days=FL_EVERY_D - 1)))   # try again tomorrow
        return False
    state['fl'] = fl = dict(fl, at=iso(NOW))
    out = {}
    def walk(node, path, cur):
        for f in node.get('files') or []:
            p = path + [f['name']]
            if f.get('type') == 'directory':
                walk(f, p, cur)
            elif f['name'].lower().endswith('.png') and len(p) >= 2 and ' - ' in p[-2]:
                country = cnorm(p[-2].split(' - ')[0])
                out.setdefault(country, []).append([f['name'][:-4], '/'.join(p), cur])
    for top in tree.get('files') or []:
        if top.get('name') in ('logos', 'history'):
            walk(top, [top['name']], 1 if top['name'] == 'logos' else 0)
    for c in out:   # this season's picture first, then newest history
        out[c].sort(key=lambda x: (-x[2], [-ord(ch) for ch in x[1]]))
    fl['c'] = out
    return True


def es_team_lists(state, doc):
    """Every club in each competition ESPN follows, with ESPN's id (one small request per competition, monthly,
    no key): the crest for any club on the board that the other sources don't name."""
    est = state.setdefault('es_teams', {})
    t0, calls = time.time(), 0
    lks = sorted({lk for _, lk in crest_names(doc)} | set(doc['leagues']), key=lambda k: (k not in {m['lk'] for m in doc['matches']}, k))
    for lk in lks:
        slug = ESPN_SLUG.get(lk)
        if not slug or (L.get(lk) or {}).get('group') == 'intl':
            continue
        rec = est.get(lk)
        if rec and NOW - parse_iso(rec['at']) < timedelta(days=ES_TEAMS_EVERY_D if rec.get('teams') else 3) and not FORCE:
            continue
        if calls >= ES_TEAMS_PER_RUN or time.time() - t0 > 15:
            break
        r = fetch(ES_TEAMS.format(slug), tries=1, timeout=15)
        calls += 1
        try:
            ts = json.loads(text_of(r.body))['sports'][0]['leagues'][0].get('teams') or [] if r.status == 200 else None
        except (ValueError, KeyError, IndexError, TypeError):
            ts = None
        if ts is None:
            est[lk] = {'at': iso(NOW - timedelta(days=ES_TEAMS_EVERY_D - 1)), 'teams': (rec or {}).get('teams') or []}   # try again tomorrow
            continue
        teams = []
        for t in ts:
            t = t.get('team') or {}
            if t.get('id') and t.get('logos'):
                teams.append([t.get('displayName'), t.get('shortDisplayName'), t.get('name'), t.get('location'), str(t['id'])])
        est[lk] = {'at': iso(NOW), 'teams': teams}
    if calls:
        log(f'ESPN club lists: {calls} competitions checked')
    return calls


def crest_alts(name):
    """Our name and the other ways another feed may write it ('Raith Rvs' -> 'Raith Rovers')."""
    out = [name]
    n = norm(name)
    if n in CREST_ALIAS:
        out.append(CREST_ALIAS[n])
    w = n.split()
    x = ' '.join(CREST_WORDS.get(t, t) for t in w)
    if x != n:
        out.append(x)
    return out


def es_crest_id(name, lk, state):
    """The ESPN id of our club, from that competition's ESPN club list."""
    teams = ((state.get('es_teams') or {}).get(lk) or {}).get('teams') or []
    if not teams:
        return None
    cands = [[x, t[4]] for t in teams for x in {t[0], t[1], t[2], t[3], (t[0] or '').replace('.', '')} if x]
    for alt in crest_alts(base_name(name)):
        tid = best_team(alt, cands)
        if tid:
            return tid
    return None


def learn_crests(state, doc):
    """Work out each team's crest: API-Football ids from paired fixtures and the country club lists; the
    football-logos collection for anything still missing."""
    cr = state.setdefault('crest', {})
    lc = state.setdefault('lgcrest', {})
    fx = state.setdefault('crest_fl', {})
    for lk, lid in AF_LEAGUE.items():
        lc.setdefault(lk, lid)
    for name, tid in CREST_SEED.items():
        cr.setdefault(name, tid)
    n = 0
    fxs = af_rows(state)
    if fxs:
        lo, hi = fxs[0][0] - timedelta(hours=3), fxs[-1][0] + timedelta(hours=3)
        for m in doc['matches']:
            if (L.get(m['lk']) or {}).get('group') == 'intl':
                continue   # national teams show their flag
            if m['h'] in cr and m['a'] in cr and m['lk'] in lc:
                continue
            ko = parse_iso(m['ko'])
            if not (lo <= ko <= hi):
                continue
            f = af_match(m['lk'], ko, m['h'], m['a'], fxs)
            if not f:
                continue
            for name, tid in ((m['h'], f[6]), (m['a'], f[7])):
                if isinstance(tid, int) and tid > 0 and cr.get(name) != tid:
                    cr[name] = tid
                    n += 1
            lid = f[11] if len(f) > 11 else None
            if m['lk'] not in AF_LEAGUE and isinstance(lid, int) and lid > 0 and lc.get(m['lk']) != lid:
                lc[m['lk']] = lid
    ct = (state.get('af') or {}).get('ct') or {}
    flc = (state.get('fl') or {}).get('c') or {}
    every = [t for c in ct.values() for t in c.get('teams') or []]
    bad = state.get('crest_bad') or {}
    for name in [n for n, p in fx.items() if crest_key(p) in bad]:
        del fx[name]   # that picture didn't download: try the club's picture from another season instead
    for name, lk in crest_names(doc):
        if name in cr or (L.get(lk) or {}).get('group') == 'intl':
            continue
        c = team_country(lk)
        cands = [t for cc in [c] + CREST_NEIGHBOURS.get(c, []) for t in (ct.get(cc) or {}).get('teams') or []] if c else every
        tid = best_team(name, cands) if cands else None
        if tid:
            cr[name] = tid
            n += 1
            continue
        if name not in fx:   # cup games: any country's list, but only a clear, unique match counts
            pool = [c] + CREST_NEIGHBOURS.get(c, []) if c else list(flc)
            path = best_team(name, [[x[0], x[1]] for cc in pool for x in flc.get(cc) or [] if crest_key(x[1]) not in bad])
            if path:
                fx[name] = path
                n += 1
    # ESPN: ids learnt from paired games are exact; otherwise the club lists of the competitions the club plays in
    ce = state.setdefault('crest_es', {})
    logos = state.get('es_logo') or {}
    where = {}
    for m in doc['matches']:
        for x in (m['h'], m['a']):
            where.setdefault(x, []).append(m['lk'])
    for lk, lg in doc['leagues'].items():
        for r in lg.get('table') or []:
            where.setdefault(r[0], []).append(lk)
    for name, lk in crest_names(doc):
        if name in ce or (L.get(lk) or {}).get('group') == 'intl':
            continue
        if name in logos:
            ce[name] = str(logos[name]); n += 1
            continue
        if name in cr or name in fx:
            continue   # another source has it; ESPN is only asked for clubs nobody else names
        for lk2 in dict.fromkeys(where.get(name) or [lk]):
            tid = es_crest_id(name, lk2, state)
            if tid:
                ce[name] = tid; n += 1
                break
    return n


def crest_key(path):
    return 'f' + hashlib.sha1(path.encode()).hexdigest()[:10]


def crest_dir():
    d = STATE_DIR / 'crests'
    (d / 'b').mkdir(parents=True, exist_ok=True)
    return d


def fetch_crests(state, doc):
    """Download and shrink crests not on file yet, the board's next games first, giving up early if a server is unhappy."""
    d = crest_dir()
    bad = state.setdefault('crest_bad', {})
    cr, fx, lc = state.get('crest') or {}, state.get('crest_fl') or {}, state.get('lgcrest') or {}
    want, seen = [], set()
    for k in sorted(lc, key=lambda k: k not in doc['leagues']):
        want.append(('l', lc[k]))
    ce = state.get('crest_es') or {}
    for name, lk in crest_names(doc):
        if name in cr:
            want.append(('t', cr[name]))
        if name in fx:
            want.append(('f', fx[name]))
        if name in ce and not (name in cr and (d / f'{cr[name]}.png').exists()) and not (name in fx and (d / (crest_key(fx[name]) + '.png')).exists()):
            want.append(('e', ce[name]))   # only while no other picture is on file
    want += [('t', i) for i in sorted(set(cr.values()))]
    t0, got, fails = time.time(), 0, {'api': 0, 'fl': 0, 'es': 0}
    for kind, i in want:
        name = f'{i}.png' if kind == 't' else f'l{i}.png' if kind == 'l' else f'e{i}.png' if kind == 'e' else crest_key(i) + '.png'
        if name in seen or (d / name).exists():
            continue
        seen.add(name)
        key, src = name[:-4], 'fl' if kind == 'f' else 'es' if kind == 'e' else 'api'
        if bad.get(key) and NOW - parse_iso(bad[key]) < timedelta(days=2) and not FORCE:
            continue
        if got >= CREST_PER_RUN * (2 if src == 'es' else 1) or time.time() - t0 > CREST_BUDGET_S:
            break
        if fails[src] >= 3:
            continue   # that server is unhappy this run; the other may still answer
        url = CREST_TEAM.format(i) if kind == 't' else CREST_LEAGUE.format(i) if kind == 'l' else CREST_ES.format(i) if kind == 'e' else FL_RAW + quote(i)
        r = fetch(url, tries=1, timeout=12)
        try:
            if r.status != 200 or not r.body:
                raise ValueError(f'HTTP {r.status}')
            big = small_png(r.body, CREST_BIG) if kind != 'l' else None
            small = small_png(r.body, CREST_SMALL)
        except Exception as e:
            bad[key] = iso(NOW)
            fails[src] += 1
            log('crest', name, 'not saved:', repr(e)[:80])
            continue
        if big:
            (d / 'b' / name).write_bytes(big)
        (d / name).write_bytes(small)
        bad.pop(key, None)
        got += 1
        time.sleep(0.1)
    return got


def publish_crests(state, doc):
    """Copy every crest on file to the site and list the ones this page needs: name -> API-Football id (negative =
    not on file yet, so the page loads it from the image server), or a file key for a football-logos crest."""
    d = crest_dir()
    out = OUT_DIR / 'crests'
    (out / 'b').mkdir(parents=True, exist_ok=True)
    for p in list(d.glob('*.png')) + list((d / 'b').glob('*.png')):
        q = out / p.relative_to(d)
        if not q.exists() or q.stat().st_size != p.stat().st_size:
            shutil.copyfile(p, q)
    cr, fx, lc = state.get('crest') or {}, state.get('crest_fl') or {}, state.get('lgcrest') or {}
    ce, logos = state.get('crest_es') or {}, state.get('es_logo') or {}
    sign = lambda i, f: i if (d / f).exists() else -i
    crests = {}
    for n0, _ in sorted(crest_names(doc)):
        n = n0 if (n0 in cr or n0 in fx or n0 in ce or n0 in logos) else base_name(n0)
        opts = []   # (key, on file?) in order of preference: API-Football, football-logos, ESPN
        if n in cr:
            opts.append((cr[n], (d / f'{cr[n]}.png').exists()))
        if n in fx:
            opts.append((crest_key(fx[n]), (d / (crest_key(fx[n]) + '.png')).exists()))
        es = ce.get(n0) or ce.get(n) or logos.get(n)
        if es:
            opts.append(('e' + str(es), (d / f'e{es}.png').exists()))
        have = next((k for k, ok in opts if ok), None)
        if have is not None:
            crests[n0] = have
        elif n in cr:
            crests[n0] = -cr[n]   # not on file yet: the page loads it from API-Football's image server meanwhile
        elif es:
            crests[n0] = 'E' + str(es)   # not on file yet: the page loads it from ESPN meanwhile
    doc['crests'] = crests
    doc['lgcrests'] = {k: sign(lc[k], f'l{lc[k]}.png') for k in sorted(doc['leagues']) if k in lc}
    return len(doc['crests'])


# ----------------------------------------------------------------------------------------------
# Crest sheets. A board shows hundreds of clubs; one picture each meant hundreds of requests, and crests popping
# in one by one on a slow connection. Each competition's clubs are put on one sheet (WebP, 64 px a crest, about
# 2 KB each), named after its content so a phone keeps it until it changes: a competition's crests arrive in one
# request and show together. Bigger crests (match pages) stay single pictures. The sheets need Pillow, installed
# once into the kept state folder if the runner lacks it; without it they're PNG, built with the code above.
# ----------------------------------------------------------------------------------------------
SHEET_CELL, SHEET_COLS = 64, 8
_PIL = None


def pillow():
    global _PIL
    if _PIL is not None:
        return _PIL or None
    lib = STATE_DIR / 'pylib'
    for attempt in (0, 1):
        try:
            if lib.exists() and str(lib) not in sys.path:
                sys.path.insert(0, str(lib))
            from PIL import Image
            Image.new('RGBA', (2, 2)).save(io.BytesIO(), 'WEBP')
            _PIL = Image
            return Image
        except Exception:
            if attempt:
                break
            try:
                import importlib
                import subprocess
                subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--upgrade', '--target', str(lib), 'pillow'],
                               timeout=150, capture_output=True)
                importlib.invalidate_caches()
                for k in [k for k in sys.modules if k == 'PIL' or k.startswith('PIL.')]:
                    del sys.modules[k]
            except Exception:
                break
    _PIL = False
    log('crest sheets: Pillow unavailable, using PNG')
    return None


def crest_colour(px):
    """A crest's main colour from 20x20 RGBA pixels: the most saturated common hue (white, black, grey ignored)."""
    bins = {}
    for i in range(0, len(px), 4):
        r, g, b, a = px[i], px[i + 1], px[i + 2], px[i + 3]
        if a < 160:
            continue
        mx, mn = max(r, g, b), min(r, g, b)
        dl = mx - mn
        sat = dl / mx if mx else 0
        if mx < 40 or sat < 0.28:
            continue
        h = 0 if not dl else ((g - b) / dl + 6) % 6 if mx == r else (b - r) / dl + 2 if mx == g else (r - g) / dl + 4
        e = bins.setdefault(int(h * 2.5 + 0.5) % 15, [0.0, 0, 0, 0, 0])
        e[0] += sat; e[1] += 1; e[2] += r; e[3] += g; e[4] += b
    best = max(bins.values(), key=lambda e: e[0], default=None)
    if not best or best[1] < 6:
        return None
    return '#' + ''.join(f'{min(255, round(v / best[1])):02x}' for v in best[2:5])


def sheet_build(files):
    """files: crest pictures in order -> (sheet bytes, extension, cols, rows, colours)."""
    n = len(files)
    cols = min(SHEET_COLS, n)
    rows = (n + cols - 1) // cols
    C = SHEET_CELL
    Image = pillow()
    cols_out = []
    if Image:
        sheet = Image.new('RGBA', (cols * C, rows * C), (0, 0, 0, 0))
        for i, f in enumerate(files):
            try:
                im = Image.open(f).convert('RGBA')
            except Exception:
                cols_out.append(None)
                continue
            cols_out.append(crest_colour(im.resize((20, 20), Image.BILINEAR).tobytes()))
            im.thumbnail((C - 4, C - 4), Image.LANCZOS)
            sheet.alpha_composite(im, ((i % cols) * C + (C - im.width) // 2, (i // cols) * C + (C - im.height) // 2))
        b = io.BytesIO()
        sheet.save(b, 'WEBP', quality=80, method=6, alpha_quality=90)
        return b.getvalue(), 'webp', cols, rows, cols_out
    W = cols * C
    px = bytearray(W * rows * C * 4)
    for i, f in enumerate(files):
        try:
            w, h, p = png_shrink(*png_decode(f.read_bytes()), C - 4)
        except Exception:
            cols_out.append(None)
            continue
        _, _, small = png_shrink(w, h, p, 20)
        cols_out.append(crest_colour(small))
        x0, y0 = (i % cols) * C + (C - w) // 2, (i // cols) * C + (C - h) // 2
        for y in range(h):
            o = ((y0 + y) * W + x0) * 4
            px[o:o + w * 4] = p[y * w * 4:(y + 1) * w * 4]
    return png_encode(W, rows * C, px), 'png', cols, rows, cols_out


def crest_file(key):
    d = crest_dir()
    if isinstance(key, int):
        return d / f'{key}.png' if key > 0 else None
    if isinstance(key, str) and key and key[0] != 'E':
        return d / f'{key}.png'
    return None


def crest_sheets(state, doc):
    """One sheet per competition on the board (each club on its own league's sheet, cup-only clubs on the cup's),
    plus one for the competition logos. Rebuilt only when a sheet's clubs or pictures change."""
    crests = doc.get('crests') or {}
    home = {}
    for lk, lg in sorted(doc['leagues'].items()):
        if not knockout(lk):
            for r in lg.get('table') or []:
                home.setdefault(r[0], lk)
    ms = sorted(doc['matches'], key=lambda m: m['ko'])
    for cup in (False, True):
        for m in ms:
            if knockout(m['lk']) == cup and (L.get(m['lk']) or {}).get('group') != 'intl':
                for x in (m['h'], m['a']):
                    home.setdefault(x, m['lk'])
    members = {}
    for name in sorted(crests):
        f = crest_file(crests[name])
        if name in home and f and f.exists():
            members.setdefault(home[name], []).append((name, f))
    lc = doc.get('lgcrests') or {}
    lg_files = [(lk, crest_dir() / f'l{v}.png') for lk, v in sorted(lc.items()) if v > 0 and (crest_dir() / f'l{v}.png').exists()]
    if lg_files:
        members['_L'] = lg_files
    sd = crest_dir() / 's'
    sd.mkdir(exist_ok=True)
    meta = state.setdefault('csheets', {})
    tcol = state.setdefault('tcol', {})
    built = 0
    t0 = time.time()
    for k, items in sorted(members.items()):
        sig = hashlib.sha1(json.dumps([[n, f.name, f.stat().st_size] for n, f in items]).encode()).hexdigest()[:12]
        cur = meta.get(k)
        if cur and cur.get('sig') == sig and (sd / cur['file']).exists():
            continue
        if time.time() - t0 > 40:
            break   # the rest next run; their old sheets (or single pictures) serve meanwhile
        data, ext, cols, rows, colours = sheet_build([f for _, f in items])
        fname = f"{k}.{hashlib.sha1(data).hexdigest()[:10]}.{ext}"
        (sd / fname).write_bytes(data)
        meta[k] = {'sig': sig, 'file': fname, 'cols': cols, 'rows': rows, 'names': [n for n, _ in items]}
        if k != '_L':
            for (n, _), c in zip(items, colours):
                if c:
                    tcol[n] = c
        built += 1
    for k in [k for k in meta if k not in members]:
        del meta[k]
    keep = {v['file'] for v in meta.values()}
    for f in sd.iterdir():
        if f.name not in keep:
            f.unlink()
    out = OUT_DIR / 'crests' / 's'
    out.mkdir(parents=True, exist_ok=True)
    for name in keep:
        if not (out / name).exists():
            shutil.copyfile(sd / name, out / name)
    # the page's index: sheet -> [file, cols, rows]; club -> [sheet, cell]; competition -> logo cell
    doc['csh'] = {k: [v['file'], v['cols'], v['rows']] for k, v in meta.items()}
    csx, lsx = {}, {}
    for k, v in meta.items():
        for i, n in enumerate(v['names']):
            if k == '_L':
                lsx[n] = i
            elif crests.get(n) is not None:
                csx[n] = [k, i]
    doc['csx'], doc['lsx'] = csx, lsx
    doc['tcol'] = {n: tcol[n] for n in crests if n in tcol}
    if len(tcol) > 4000:
        for n in list(tcol)[:len(tcol) - 4000]:
            del tcol[n]
    return built


# The shock map's earth: NASA's Blue Marble and city lights (public domain), from the three-globe project on jsDelivr,
# resized once to 2048 x 1024 WebP (about 200 KB for both; 4096 px versions for close-ups) and published in globe/. Uploaded copies (earth-day.webp,
# earth-night.webp next to update.py) are used instead if present. Without Pillow the page loads them from jsDelivr.
GLOBE_CDN = 'https://cdn.jsdelivr.net/gh/vasturiano/three-globe@2.41.4/example/img/'
GLOBE_SRC = {'earth-day.webp': ('earth-blue-marble.jpg', 72, 2048), 'earth-night.webp': ('earth-night.jpg', 72, 2048),
             'earth-day-4k.webp': ('earth-blue-marble.jpg', 68, 4096), 'earth-night-4k.webp': ('earth-night.jpg', 68, 4096)}   # 4k: close-ups


def globe_textures(state):
    d = STATE_DIR / 'globe'
    d.mkdir(parents=True, exist_ok=True)
    tries = state.setdefault('globe_try', {})
    out = OUT_DIR / 'globe'
    n = 0
    got = {}
    for name, (src, q, w) in GLOBE_SRC.items():
        f = d / name
        if not f.exists() and (ROOT / name).exists():
            shutil.copyfile(ROOT / name, f)
        if not f.exists() and not (tries.get(name) and NOW - parse_iso(tries[name]) < timedelta(hours=12)):
            tries[name] = iso(NOW)
            Image = pillow()
            r = got.get(src) or (fetch(GLOBE_CDN + src, tries=1, timeout=60) if Image else None)
            if r is not None and r.status == 200 and r.body:
                got[src] = r
                try:
                    im = Image.open(io.BytesIO(r.body)).convert('RGB')
                    if im.width != w:
                        im = im.resize((w, w // 2), Image.LANCZOS)
                    b = io.BytesIO()
                    im.save(b, 'WEBP', quality=q, method=6)
                    f.write_bytes(b.getvalue())
                    log(f'globe: {name} saved ({len(b.getvalue()) // 1024} KB)')
                except Exception as e:
                    log('globe:', name, 'not saved:', repr(e)[:80])
        if f.exists():
            out.mkdir(parents=True, exist_ok=True)
            if not (out / name).exists():
                shutil.copyfile(f, out / name)
            n += 1
    return n


def refresh_crests(state, doc):
    def step(fn, default):
        try:
            return fn()
        except Exception as e:   # one source failing must not cost the crests already on file
            log('crests:', fn.__name__ if hasattr(fn, '__name__') else 'step', 'failed ->', repr(e)[:160])
            return default
    if AF_KEY:
        step(lambda: af_country_teams(state, doc), 0)
    step(lambda: fl_refresh(state), False)
    step(lambda: es_team_lists(state, doc), 0)
    n_new = step(lambda: learn_crests(state, doc), 0)
    got = step(lambda: fetch_crests(state, doc), 0)
    n = publish_crests(state, doc)
    sheets = step(lambda: crest_sheets(state, doc), 0)
    clubs = [x for x, lk in crest_names(doc) if (L.get(lk) or {}).get('group') != 'intl']
    own = sum(1 for x in clubs if isinstance(doc['crests'].get(x), str) and doc['crests'][x][0] != 'E' or (isinstance(doc['crests'].get(x), int) and doc['crests'][x] > 0))
    if n_new or got or sheets:
        log(f'crests: {n_new} teams matched, {got} downloaded, {own} of {len(clubs)} clubs on this board have one on file ({n} with any), {sheets} sheets rebuilt')
    return n


def table_pos(leagues):
    pos = {}
    for lk, lg in leagues.items():
        n = len(lg.get('table') or [])
        for i, row in enumerate(lg.get('table') or [], 1):
            pos[(lk, row[0])] = (i, n, row[8])
    return pos


def same_team(x, y):
    return norm(x) == norm(y) or norm(pretty(x)) == norm(y) or norm(x) == norm(pretty(y))


def pick_market(main, es, old):
    """The prices to use: the bookmakers' consensus, with DraftKings' (via ESPN) as a fresher voice when the consensus
    is more than six hours old, or alone when nothing else prices the game."""
    if main and es:
        try:
            age = (NOW - parse_iso(main['upd'])).total_seconds() / 3600 if main.get('upd') else None
        except Exception:
            age = None
        if age is not None and age > 6:
            m = dict(main, p=[round((a + b) / 2, 4) for a, b in zip(main['p'], es['p'])], upd=es.get('upd'), fresh='espn')
            if main.get('ou') and es.get('ou') and es['ou'].get('line') == main['ou'].get('line', 2.5):
                m['ou'] = dict(main['ou'], pO=round((main['ou']['pO'] + es['ou']['pO']) / 2, 4))
            return m
        return main
    return main or es or old


def build(state, prev, seed=None):
    leagues = state.get('leagues') or {}
    teams = Teams()
    for lk, lg in leagues.items():
        for row in lg.get('table') or []:
            teams.add(lk, row[0])
    pos = table_pos(leagues)
    it = IntlTeams(state.get('intl'))
    lo, hi = NOW - timedelta(hours=2.5), NOW + timedelta(days=DAYS)

    fx = []   # {'lk','ko','h','a','fd_mkt','api_mkt','api_id'}

    def find(lk, h, a, ko, tbc=False):
        # a team can't play twice in one competition within 36 hours, so one matching side is enough;
        # schedule dates without a time are provisional, so those get a wider window
        t = parse_iso(ko)
        for f in fx:
            win = 96 if (tbc or f.get('tbc')) else 36
            if f['lk'] == lk and abs((parse_iso(f['ko']) - t).total_seconds()) < win * 3600 and (same_team(f['h'], h) or same_team(f['a'], a)):
                return f
        return None

    for f in state.get('fd_fixtures') or []:
        ko = parse_iso(f['ko'])
        if not (lo <= ko <= hi):
            continue
        h = teams.resolve(f['lk'], f['h']) or f['h']
        a = teams.resolve(f['lk'], f['a']) or f['a']
        if not find(f['lk'], h, a, f['ko']):
            fx.append({'lk': f['lk'], 'ko': f['ko'], 'h': h, 'a': a, 'fd_mkt': f.get('mkt'), 'api_mkt': None, 'api_id': None, 'ks': 'fd'})

    for f in state.get('of_fixtures') or []:
        ko = parse_iso(f['ko'])
        if f['lk'] not in L or not (lo <= ko <= hi):
            continue
        h = teams.resolve(f['lk'], f['h']) or f['h']
        a = teams.resolve(f['lk'], f['a']) or f['a']
        if not find(f['lk'], h, a, f['ko'], f.get('tbc')):
            fx.append({'lk': f['lk'], 'ko': f['ko'], 'h': h, 'a': a, 'fd_mkt': None, 'api_mkt': None, 'api_id': None, 'tbc': f.get('tbc'), 'ks': 'of'})

    def cup_names(lk, h, a):
        rh, ra = teams.resolve_global(h, pool_=pool(lk)), teams.resolve_global(a, pool_=pool(lk))
        return (rh[1] if rh else h), (ra[1] if ra else a)

    for f in state.get('es_fixtures') or []:   # ESPN: competitions no other source lists, and DraftKings prices
        if f['lk'] not in L:
            continue
        ko = parse_iso(f['ko'])
        if not (lo <= ko <= hi):
            continue
        if is_cup(f['lk']):
            h, a = cup_names(f['lk'], f['h'], f['a'])
        else:
            h = teams.resolve(f['lk'], f['h']) or f['h']
            a = teams.resolve(f['lk'], f['a']) or f['a']
        g = find(f['lk'], h, a, f['ko'])
        if g:
            g['es_mkt'] = f.get('mkt')
            if g.get('ks') in ('of', 'fd'):
                g['ko'], g['ks'], g['tbc'] = iso(ko), 'es', False   # ESPN's kick-off time is exact
        else:
            fx.append({'lk': f['lk'], 'ko': iso(ko), 'h': h, 'a': a, 'fd_mkt': None, 'api_mkt': None, 'api_id': None, 'es_mkt': f.get('mkt'), 'ks': 'es'})

    plan = sport_plan(state, quiet=True) if state.get('api') else {}
    for key, blob in (state.get('api') or {}).items():
        lk = plan.get(key) or OA_KEY.get(key)
        if not lk or lk not in L:
            continue
        for e in blob.get('events') or []:
            if not e.get('ko') or not e.get('h') or not e.get('a'):
                continue
            ko = parse_iso(e['ko'])
            if not (lo <= ko <= hi):
                continue
            if is_cup(lk):
                h, a = cup_names(lk, e['h'], e['a'])
            elif L[lk]['group'] == 'intl':
                h = it.resolve(e['h']) or e['h']
                a = it.resolve(e['a']) or e['a']
            else:
                h = teams.resolve(lk, e['h']) or e['h']
                a = teams.resolve(lk, e['a']) or e['a']
            f = find(lk, h, a, e['ko'])
            if f:
                f['ko'] = iso(ko)   # the API kickoff is exact
                f['ks'] = 'api'
                f['tbc'] = False
            else:
                f = {'lk': lk, 'ko': iso(ko), 'h': h, 'a': a, 'fd_mkt': None, 'api_mkt': None, 'api_id': None, 'ks': 'api'}
                fx.append(f)
            f['api_mkt'] = api_market(e)
            f['api_id'] = e.get('id')

    prev_list, ids = [], set()
    for doc in (prev, seed):
        for m in (doc or {}).get('matches', []):
            if m.get('id') not in ids and m.get('ko') and m.get('lk') in L:
                ids.add(m['id'])
                prev_list.append(m)
    prev_by_id = {m['id']: m for m in prev_list}
    prev_by_lk = {}
    for m in prev_list:
        prev_by_lk.setdefault(m['lk'], []).append(m)

    def prev_for(lk, h, a, ko, wide=False):
        for m in prev_by_lk.get(lk, []):
            win = 96 if wide else 36
            if abs((parse_iso(m['ko']) - ko).total_seconds()) < win * 3600 and (same_team(m['h'], h) or same_team(m['a'], a)):
                return m
        return {}
    matches = []
    seen = set()
    for f in sorted(fx, key=lambda f: (f['ko'], f['lk'], f['h'])):
        ko = parse_iso(f['ko'])
        mid = f"{f['lk']}-{ko:%Y%m%d}-{slug(f['h'])}-{slug(f['a'])}"
        old = prev_by_id.get(mid) or prev_for(f['lk'], f['h'], f['a'], ko, wide=bool(f.get('tbc')))
        ks = f.get('ks', 'fd')
        tbc = bool(f.get('tbc'))
        # season schedules can carry provisional kick-off times; trust an earlier time from a firmer source
        if ks == 'of' and old and old.get('ks', 'seed') != 'of' and old.get('ko'):
            ko = parse_iso(old['ko'])
            ks, tbc = old.get('ks', 'seed'), bool(old.get('tbc'))
            mid = f"{f['lk']}-{ko:%Y%m%d}-{slug(f['h'])}-{slug(f['a'])}"
        if mid in seen:
            continue
        seen.add(mid)
        mkt = pick_market(f['api_mkt'] or f['fd_mkt'], f.get('es_mkt') or (state.get('es_prices') or {}).get(mid), old.get('mkt'))
        om = old.get('mkt') or {}
        if mkt and mkt.get('src') == 'football-data' and om.get('src') == 'football-data' and om.get('avg') == mkt.get('avg') and om.get('best') == mkt.get('best'):
            mkt = dict(mkt, upd=om.get('upd'))   # same prices re-read: keep when they were first seen
        lam, cross = expected_goals(f['lk'], f['h'], f['a'], leagues, teams)
        ls = lam_sd(f['lk'], f['h'], f['a'], leagues, teams, cross) if lam else None
        ph = pos.get((f['lk'], f['h']))
        pa = pos.get((f['lk'], f['a']))
        dom_h = dom_a = None
        if is_cup(f['lk']):
            gh, ga = teams.resolve_global(f['h'], pool_=pool(f['lk'])), teams.resolve_global(f['a'], pool_=pool(f['lk']))
            dom_h, dom_a = (gh[0] if gh else None), (ga[0] if ga else None)
            ph = pos.get((dom_h, f['h'])) if dom_h else None
            pa = pos.get((dom_a, f['a'])) if dom_a else None
        clash = bool(ph and pa and ph[0] <= 6 and pa[0] <= 6 and ph[1] >= 10 and pa[1] >= 10)
        if clash and is_cup(f['lk']):
            clash = all(d and L[d]['strength'] >= -0.40 for d in (dom_h, dom_a))
        fr = False
        if L[f['lk']]['group'] == 'intl':
            lam, cross = it.expected(f['lk'], f['h'], f['a']), False
            rk_h, rk_a = it.rank.get(f['h']), it.rank.get(f['a'])
            form = (state.get('intl') or {}).get('form') or {}
            ph = (rk_h, 300, form.get(f['h'])) if rk_h else None     # position = world rank in Statpad's ratings
            pa = (rk_a, 300, form.get(f['a'])) if rk_a else None
            clash = bool(f['lk'] == 'UNL' and rk_h and rk_a and rk_h <= 12 and rk_a <= 12)
            fr = f['lk'] == 'INTF'
        hist = list(old.get('hist') or [])
        if mkt and mkt.get('avg'):
            pt = [iso(NOW)[:16] + 'Z', *[round(x, 2) for x in mkt['avg']]]
            if not hist or hist[-1][1:] != pt[1:] or NOW - parse_iso(hist[-1][0]) > timedelta(hours=6):
                hist.append(pt)
            hist = hist[-40:]
        if L[f['lk']]['group'] == 'intl':
            ls = None
        news = team_news_for(state, mid, f['lk'], ko, f['h'], f['a'])
        matches.append({
            'id': mid, 'lk': f['lk'], 'ko': iso(ko), 'h': disp(f['lk'], f['h']), 'a': disp(f['lk'], f['a']),
            'ph': ph[0] if ph else None, 'pa': pa[0] if pa else None,
            'fh': ph[2] if ph else None, 'fa': pa[2] if pa else None,
            'dh': dom_h, 'da': dom_a,
            'lm': lam, 'xl': cross, 'mkt': mkt, 'hist': hist, 'clash': clash, 'tbc': tbc, 'ks': ks, 'fr': fr,
        })
        if news:
            matches[-1]['tn'] = news
        if ls:
            matches[-1]['ls'] = ls
        if MODEL_W and not knockout(f['lk']) and not cross and not (mkt and (mkt.get('n') or 0) == 1):
            matches[-1]['wm'] = MODEL_W   # tuned where many bookmakers price the games; one bookmaker alone keeps the default blend
        if RHO_L:
            matches[-1]['rho'] = rho_for(f['lk'])   # the draw correction fitted for this league
        lgr = leagues.get(f['lk']) or {}
        if (L[f['lk']].get('hist') and (lgr.get('games') or 0) < 150) or (not lam and (not mkt or mkt.get('src') == 'espn')):
            matches[-1]['thin'] = 1   # few games behind the ratings, or one bookmaker's prices alone

    # competitions no source covered this run keep their previous (or starter) fixtures until kick-off
    covered = {m['lk'] for m in matches}
    carried = [m for m in prev_list if m['lk'] not in covered and lo <= parse_iso(m['ko']) <= hi]
    for m in carried:
        news = team_news_for(state, m['id'], m['lk'], parse_iso(m['ko']), m['h'], m['a'])
        if news:
            m['tn'] = news
        else:
            m.pop('tn', None)
    if carried:
        log('kept', len(carried), 'earlier fixtures for', len({m['lk'] for m in carried}), 'competitions with no fresh fixtures')
        matches.extend(carried)
        matches.sort(key=lambda m: (m['ko'], m['lk'], m['h']))

    used = {m['lk'] for m in matches}
    out_leagues = {}
    for lk, c in L.items():
        lg = leagues.get(lk)
        if lk not in used and not lg:
            continue
        rec = {'name': c['name'], 'country': c['country'], 'group': c['group']}
        old_lg = ((seed or {}).get('leagues') or {}).get(lk) or {}
        if not lg and old_lg.get('table'):
            rec.update({'season': old_lg.get('season'), 'mu': None, 'home': None, 'table': old_lg['table']})
        if lg:
            rec.update({'season': lg.get('season'), 'mu': lg.get('mu'), 'home': lg.get('home'),
                        'table': [[disp(lk, r[0])] + r[1:] for r in lg.get('table') or []]})
            if lg.get('shots'):
                rec['sh'] = 1   # ratings use shots on target as well as goals
        out_leagues[lk] = rec

    q = state.get('quota') or {}
    return {
        'app': 'Statpad', 'schema': 2, 'generated': iso(NOW),
        'window': {'from': iso(lo), 'to': iso(hi)},
        'sources': state.get('sources') or {},
        'odds': {'enabled': bool(API_KEY), 'remaining': q.get('remaining'), 'used': q.get('used'),
                 'interval_min': state.get('odds_interval'), 'regions': REGIONS, 'markets': MARKETS,
                 'plan': {k: v for k, v in (state.get('odds_plan') or {}).items() if k in ('iv', 'tiers', 'scale')} or None},
        'leagues': out_leagues,
        'matches': matches,
    }


# ----------------------------------------------------------------------------------------------
# National teams: UEFA Nations League and international friendlies. Fixtures and prices come
# from The Odds API (fixture lists are free with a key); ratings and results come from the open
# international results dataset (martj42/international_results on GitHub).
# ----------------------------------------------------------------------------------------------
INTL_URL = 'https://raw.githubusercontent.com/martj42/international_results/master/results.csv'
INTL_LKS = ('UNL', 'INTF')
INTL_TOURN = {'UEFA Nations League': 'UNL', 'Friendly': 'INTF'}
INTL_HALF_LIFE = 540.0      # days: national teams play ~10 games a year
INTL_YEARS = 6
INTL_K = 4.0
INTL_ALIAS = {'czechia': 'czech republic', 'ireland': 'republic of ireland', 'eire': 'republic of ireland',
              'usa': 'united states', 'korea republic': 'south korea', 'republic of korea': 'south korea',
              'korea dpr': 'north korea', 'cote divoire': 'ivory coast', 'turkiye': 'turkey', 'holland': 'netherlands',
              'bosnia herzegovina': 'bosnia and herzegovina', 'macedonia': 'north macedonia', 'fyr macedonia': 'north macedonia',
              'congo dr': 'dr congo', 'democratic republic of congo': 'dr congo', 'cape verde islands': 'cape verde',
              'chinese taipei': 'taiwan', 'china pr': 'china', 'ir iran': 'iran', 'curacao': 'curacao',
              'united states of america': 'united states', 'swaziland': 'eswatini'}


def intl_key(name):
    k = norm(name)
    return INTL_ALIAS.get(k, k)


def intl_lk(key, title=''):
    """Map an Odds API competition to UNL / INTF (men's senior national teams only)."""
    k = (key + ' ' + (title or '')).lower()
    if any(w in k for w in ('women', 'u17', 'u19', 'u20', 'u21', 'u23', 'youth', 'olympic', 'club')):
        return None
    if 'nations' in k and 'uefa' in k:
        return 'UNL'
    if 'friendl' in k:
        return 'INTF'
    return None


def fit_intl(games):
    """games: (date, home, away, hg, ag, neutral, weight). Same Poisson model as the leagues, with a
    home bonus only when the game isn't at a neutral venue."""
    xi = math.log(2) / INTL_HALF_LIFE
    teams = sorted({g[1] for g in games} | {g[2] for g in games})
    ix = {t: i for i, t in enumerate(teams)}
    W = []
    for d, h, a, hg, ag, neu, wt in games:
        age = max(0.0, (NOW - d).total_seconds() / 86400)
        W.append((ix[h], ix[a], hg, ag, 0.0 if neu else 1.0, math.exp(-xi * age) * wt))
    n = len(teams)
    att, dfn = [0.0] * n, [0.0] * n
    sw = sum(w for *_, w in W)
    mu = math.log(max(0.3, sum(w * ag for _, _, _, ag, _, w in W) / sw))
    home = 0.25
    for _ in range(150):
        g0 = math.exp(mu + home / 2)
        num, den = [0.0] * n, [0.0] * n
        for h, a, hg, ag, ho, w in W:
            num[h] += w * hg; den[h] += w * math.exp(mu + home * ho + dfn[a])
            num[a] += w * ag; den[a] += w * math.exp(mu + dfn[h])
        na = [math.log((num[i] + INTL_K * g0) / (den[i] + INTL_K * g0)) for i in range(n)]
        num, den = [0.0] * n, [0.0] * n
        for h, a, hg, ag, ho, w in W:
            num[a] += w * hg; den[a] += w * math.exp(mu + home * ho + na[h])
            num[h] += w * ag; den[h] += w * math.exp(mu + na[a])
        nd = [math.log((num[i] + INTL_K * g0) / (den[i] + INTL_K * g0)) for i in range(n)]
        ma, md = sum(na) / n, sum(nd) / n
        na = [x - ma for x in na]
        nd = [x - md for x in nd]
        # mu from neutral + away goals, home bonus from the remaining home goals
        e_away = sum(w * math.exp(na[a] + nd[h]) for h, a, _, _, _, w in W)
        g_away = sum(w * ag for _, _, _, ag, _, w in W)
        e_hn = sum(w * math.exp(na[h] + nd[a]) for h, a, _, _, ho, w in W if not ho)
        g_hn = sum(w * hg for _, _, hg, _, ho, w in W if not ho)
        new_mu = math.log((g_away + g_hn) / (e_away + e_hn))
        e_hh = sum(w * math.exp(new_mu + na[h] + nd[a]) for h, a, _, _, ho, w in W if ho)
        g_hh = sum(w * hg for _, _, hg, _, ho, w in W if ho)
        new_home = math.log(g_hh / e_hh) if e_hh > 0 and g_hh > 0 else 0.25
        delta = max(max(abs(x - y) for x, y in zip(na, att)), max(abs(x - y) for x, y in zip(nd, dfn)), abs(new_mu - mu), abs(new_home - home))
        att, dfn, mu, home = na, nd, new_mu, new_home
        if delta < 1e-6:
            break
    return {'mu': round(mu, 5), 'home': round(home, 5), 'ratings': {t: [round(att[ix[t]], 4), round(dfn[ix[t]], 4)] for t in teams}}


def load_intl():
    r = fetch(INTL_URL)
    if r.status != 200 or not r.body:
        return None
    rows = list(csv.DictReader(io.StringIO(text_of(r.body))))
    since = NOW - timedelta(days=365 * INTL_YEARS)
    games, recent, forms = [], {}, {}
    for row in rows:
        d = parse_date(row.get('date'))
        hg, ag = fnum(row.get('home_score')), fnum(row.get('away_score'))
        h, a, tour = (row.get('home_team') or '').strip(), (row.get('away_team') or '').strip(), (row.get('tournament') or '').strip()
        if not d or d < since or d > NOW or hg is None or ag is None or not h or not a:
            continue
        neutral = (row.get('neutral') or '').strip().upper() == 'TRUE'
        games.append((d, h, a, int(hg), int(ag), neutral, 0.6 if tour == 'Friendly' else 1.0))
        lk = INTL_TOURN.get(tour)
        if lk and NOW - d < timedelta(days=45):
            recent.setdefault(lk, []).append([d.strftime('%Y-%m-%d'), h, a, int(hg), int(ag)])
    if len(games) < 500:
        return None
    games.sort(key=lambda g: g[0])
    for d, h, a, hg, ag, _n, _w in games:
        for t, gf, ga in ((h, hg, ag), (a, ag, hg)):
            forms[t] = (forms.get(t, '') + ('W' if gf > ga else 'D' if gf == ga else 'L'))[-5:]
    fit = fit_intl(games)
    fit['form'] = forms
    fit['games'] = len(games)
    fit['last'] = games[-1][0].strftime('%Y-%m-%d')
    return fit, recent


class IntlTeams:
    def __init__(self, fit):
        self.fit = fit or {}
        self.by_key = {intl_key(t): t for t in (self.fit.get('ratings') or {})}
        strength = sorted((self.fit.get('ratings') or {}).items(), key=lambda kv: kv[1][1] - kv[1][0])
        self.rank = {t: i + 1 for i, (t, _) in enumerate(strength)}

    def resolve(self, raw):
        k = intl_key(raw)
        if k in self.by_key:
            return self.by_key[k]
        m = difflib.get_close_matches(k, list(self.by_key), n=1, cutoff=0.88)
        return self.by_key[m[0]] if m else None

    def expected(self, lk, h, a):
        r = self.fit.get('ratings') or {}
        if h not in r or a not in r:
            return None
        home = self.fit['home'] * (0.6 if lk == 'INTF' else 1.0)   # friendlies are often at neutral or half-empty venues
        return [round(math.exp(self.fit['mu'] + home + r[h][0] + r[a][1]), 4), round(math.exp(self.fit['mu'] + r[a][0] + r[h][1]), 4)]


# ----------------------------------------------------------------------------------------------
# Track record: every prediction is saved before kick-off (using the site's own engine, run with
# Node from index.html, so the record is exactly what the page showed), graded once the result is
# in, and kept in track.json. The workflow commits that file, which also keeps GitHub's schedule
# alive (GitHub pauses scheduled runs after 60 days without a commit).
# ----------------------------------------------------------------------------------------------
TRACK_FILE = Path(ENV('STATPAD_TRACK_FILE') or ROOT / 'track.json')
INDEX_FILE = Path(ENV('STATPAD_INDEX') or ROOT / 'index.html')
TRACK_TZ = (ENV('STATPAD_TIMEZONE') or 'UTC').strip() or 'UTC'
TRACK_TARGET = 10          # slips are recorded at the site's default 10-odds target
TRACK_KEEP_DAYS = 90       # detail rows kept; the summary counts are all-time
SAVE_EVERY_H = 6           # commit new grades at most this often
KEEPALIVE_DAYS = 20        # commit at least this often even when nothing changed

NODE_ENGINE = r"""
const fs = require('fs');
const [, , htmlPath, inPath] = process.argv;
const html = fs.readFileSync(htmlPath, 'utf8');
const s = html.indexOf('/* Statpad engine'), e = html.indexOf('module.exports = ENGINE;', s);
if (s < 0 || e < 0) { console.error('engine not found in index.html'); process.exit(2); }
const mod = { exports: null };
new Function('module', html.slice(s, e) + 'module.exports = ENGINE;')(mod);
const E = mod.exports;
const inp = JSON.parse(fs.readFileSync(inPath, 'utf8'));
if (E.setCalib) E.setCalib(inp.calib && inp.calib.on ? inp.calib : null);
if (E.setMcal) E.setMcal(inp.mcal || null);
const now = Date.parse(inp.now);
let fmt;
try { fmt = new Intl.DateTimeFormat('en-CA', { timeZone: inp.tz, year: 'numeric', month: '2-digit', day: '2-digit' }); }
catch (err) { fmt = new Intl.DateTimeFormat('en-CA', { timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit' }); }
const out = { picks: {}, slips: {} }, byDay = {};
for (const m of inp.matches) {
  let a = null;
  try { a = E.analyse(m, now); } catch (err) { a = null; }
  if (!a) continue;
  out.picks[m.id] = {
    src: a.src, p: [a.p.H, a.p.D, a.p.A], pm: a.pMod ? [a.pMod.H, a.pMod.D, a.pMod.A] : null, m: a.mkt ? a.mkt.p : null, x: [a.lh, a.la],
    k: a.top ? a.top.key : null, pp: a.top ? a.top.pAdj : null, pr: a.top ? a.top.price : null, r: a.top ? !!a.top.real : false,
    pu: a.top ? (a.top.pRaw != null ? a.top.pRaw : a.top.pAdj) : null,
    t: a.top && a.top.pLo != null ? a.tier : undefined, pl: a.top && a.top.pLo != null ? a.top.pLo : undefined,
    vb: E.value([{ m, a }], inp.vb_min || 0.02).map((v) => [v.s.key, v.s.price, +v.s.p.toFixed(4), +v.s.edge.toFixed(4), v.s.bk || null]),
  };
  if (Date.parse(m.ko) > now) { const d = fmt.format(new Date(m.ko)); (byDay[d] = byDay[d] || []).push({ m, a }); }
}
for (const [d, list] of Object.entries(byDay)) {
  out.slips[d] = ['C', 'B', 'A'].map((t) => {
    const sl = E.buildSlip(list, t, inp.target);
    return { t, f: sl.feasible, o: sl.odds, p: sl.prob,
      legs: sl.legs.map((l) => ({ id: l.m.id, k: l.s.key, pr: l.s.price, pp: l.s.pAdj, r: !!l.s.real, ko: l.m.ko, h: l.m.h, a: l.m.a })) };
  });
}
out.ver = E.VERSION || null;
process.stdout.write(JSON.stringify(out));
"""


def run_engine(doc):
    import shutil
    import subprocess
    import tempfile
    node = shutil.which('node')
    if not node or not INDEX_FILE.exists():
        log('track: skipped (needs node and index.html)')
        return None
    with tempfile.TemporaryDirectory() as td:
        js, inp = Path(td) / 'engine_run.js', Path(td) / 'in.json'
        js.write_text(NODE_ENGINE)
        inp.write_text(json.dumps({'now': iso(NOW), 'tz': TRACK_TZ, 'target': TRACK_TARGET, 'matches': doc['matches'],
                                   'calib': doc.get('calib'), 'mcal': doc.get('mcal'), 'vb_min': VB_MIN}))
        r = subprocess.run([node, str(js), str(INDEX_FILE), str(inp)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log('track: engine failed', (r.stderr or '')[-300:])
            return None
        return json.loads(r.stdout)


def settle(k, hs, as_):
    t = hs + as_
    return {'H': hs > as_, 'D': hs == as_, 'A': as_ > hs, '1X': hs >= as_, 'X2': as_ >= hs, 'O15': t > 1, 'O25': t > 2,
            'U25': t < 3, 'U35': t < 4, 'BTTS': hs > 0 and as_ > 0, 'BTTS_N': not (hs > 0 and as_ > 0),
            'H_O05': hs > 0, 'A_O05': as_ > 0, 'H_O15': hs > 1, 'A_O15': as_ > 1}.get(k)


def tier_of(p):
    return None if p is None else 'A' if p >= 0.82 else 'B' if p >= 0.70 else 'C' if p >= 0.58 else None


def recent_results(res):
    """Last three weeks of results per league from the football-data download: [date, home, away, hg, ag]."""
    out = {}
    for lk, (games, _) in res.items():
        rows = [[d.strftime('%Y-%m-%d'), h, a, hg, ag] for d, h, a, hg, ag, _c, *_ in games if NOW - d < timedelta(days=21)]
        if rows:
            out[lk] = rows
    return out


def refresh_scores(state):
    """Results for competitions football-data doesn't cover (UEFA, cups, Saudi...), from The Odds API.
    Costs 2 credits per competition, so it only runs on paid plans (plenty of credits left)."""
    rem = (state.get('quota') or {}).get('remaining')
    if not API_KEY or rem is None or rem < 1500:
        return 0
    pend = state.get('track_pending') or {}
    covered = set(state.get('recent_results') or {})
    need = {r['lk'] for r in pend.values() if r['lk'] not in covered
            and NOW - timedelta(days=3) < parse_iso(r['ko']) < NOW - timedelta(hours=2)}
    plan = sport_plan(state, quiet=True)
    by_lk = {}
    for key, lk in plan.items():
        by_lk.setdefault(lk, key)
    done = 0
    sc_at = state.setdefault('scores_at', {})
    store = state.setdefault('api_scores', {})
    for lk in need:
        key = by_lk.get(lk)
        if not key or (sc_at.get(key) and NOW - parse_iso(sc_at[key]) < timedelta(hours=12)):
            continue
        data = oa_get(f'/sports/{key}/scores', {'daysFrom': 3, 'dateFormat': 'iso'}, state)
        sc_at[key] = iso(NOW)
        if not isinstance(data, list):
            continue
        rows = [r for r in store.get(lk, []) if NOW - parse_iso(r[0] + 'T00:00:00Z') < timedelta(days=10)]
        for ev in data:
            if not ev.get('completed') or not ev.get('scores'):
                continue
            sc = {s.get('name'): s.get('score') for s in ev['scores']}
            try:
                hg, ag = int(sc.get(ev['home_team'])), int(sc.get(ev['away_team']))
            except (TypeError, ValueError):
                continue
            rows.append([ev['commence_time'][:10], ev['home_team'], ev['away_team'], hg, ag])
        store[lk] = rows
        done += 1
    return done


def find_result(rec, state):
    fast = ((state.get('af') or {}).get('scores') or {}).get(rec.get('id'))
    if fast:
        return int(fast[0]), int(fast[1])
    d0 = parse_iso(rec['ko']).date()
    for source in ((state.get('recent_results') or {}).get(rec['lk']) or [], (state.get('intl_recent') or {}).get(rec['lk']) or [],
                   (state.get('api_scores') or {}).get(rec['lk']) or []):
        for dd, h, a, hg, ag in source:
            dt = datetime.strptime(dd[:10], '%Y-%m-%d').date()
            if abs((dt - d0).days) <= 1 and same_team(h, base_name(rec['h'])) and same_team(a, base_name(rec['a'])):
                return int(hg), int(ag)
    return None


VB_MIN = 0.02             # the Value board's lowest setting: every value bet it shows is tracked


def vb_band(e):
    """The Value board's edge settings: 2-4%, 4-7%, 7% and up."""
    return '7' if e >= 0.07 else '4' if e >= 0.04 else '2'


def new_vb():
    # bets, won, units profit, sum of squared profits (for the error range), sum of CLV, bets with a closing
    # price, bets that beat it, and the largest fall from a peak in the running total (units)
    return {'n': 0, 'w': 0, 'u': 0.0, 'u2': 0.0, 'c': 0.0, 'nc': 0, 'b': 0, 'pk': 0.0, 'dd': 0.0}


def vb_add(a, won, price, clv):
    pr = price - 1 if won else -1.0
    a['n'] += 1; a['w'] += int(won)
    a['u'] = round(a['u'] + pr, 4); a['u2'] = round(a['u2'] + pr * pr, 4)
    if clv is not None:
        a['c'] = round(a['c'] + clv, 4); a['nc'] += 1; a['b'] += int(clv > 0)
    a['pk'] = max(a['pk'], a['u']); a['dd'] = round(max(a['dd'], a['pk'] - a['u']), 4)
    return pr


def thin_curve(c, n=90):
    """A running total cut down to about n points for a small chart (always keeps the last)."""
    if len(c) <= n:
        return [round(x, 2) for x in c]
    step = (len(c) - 1) / (n - 1)
    return [round(c[round(i * step)], 2) for i in range(n)]


def new_ver():
    return {'n': 0, 'll': [0, 0.0, 0.0], 'br': [0, 0.0, 0.0], 't': {t: [0, 0, 0.0] for t in ('A', 'B', 'C')}, 'vb': new_vb(),
            'from': None, 'to': None}


def new_agg():
    return {'n': 0, 'nr': 0, 'void': 0,
            'tiers': {t: [0, 0, 0.0] for t in ('A', 'B', 'C', '-')},          # picks, wins, sum of stated chance
            'cal': [[0, 0.0, 0] for _ in range(10)],                          # 1X2 outcomes: count, sum p, happened
            'll': [0, 0.0, 0.0, 0.0],                                         # games, log loss: Statpad, market, model
            'clv': [0, 0.0, 0],                                               # picks, sum CLV, beat the close
            'slips': {t: [0, 0, 0.0, 0.0] for t in ('A', 'B', 'C')},          # played, won, units profit, sum chance
            'br': [0, 0.0, 0.0, 0.0],                                         # games, Brier score: Statpad, market, model
            'vb': new_vb(), 'vbb': {},                                        # value bets, and by edge setting
            'ver': {}}                                                        # the same, per engine version


# The ledger: every graded prediction and value bet, one row each, appended to a CSV per month of kick-off in
# the repo's ledger folder. Rows are only ever added, never changed, so any engine version can be checked later.
LEDGER_DIR = Path(ENV('STATPAD_LEDGER') or ROOT / 'ledger')
LEDGER_COLS = ['graded_utc', 'engine', 'kind', 'match_id', 'competition', 'kickoff_utc', 'home', 'away', 'home_goals', 'away_goals',
               'pick', 'market', 'level', 'chance', 'cautious_chance', 'price', 'real_price', 'bookmaker', 'edge',
               'first_seen_utc', 'first_pick', 'first_chance', 'first_price', 'closing_fair_chance', 'won', 'profit_units', 'clv',
               'statpad_home', 'statpad_draw', 'statpad_away', 'market_home', 'market_draw', 'market_away',
               'model_home', 'model_draw', 'model_away']
PICK_TEXT = {'H': '{h} to win', 'D': 'Draw', 'A': '{a} to win', '1X': '{h} or draw', 'X2': '{a} or draw', 'O15': 'Over 1.5 goals',
             'O25': 'Over 2.5 goals', 'U25': 'Under 2.5 goals', 'U35': 'Under 3.5 goals', 'BTTS': 'Both teams to score',
             'BTTS_N': 'Both teams to score: no', 'H_O05': '{h} to score', 'A_O05': '{a} to score', 'H_O15': '{h} 2+ goals',
             'A_O15': '{a} 2+ goals'}


def pick_text(k, h, a):
    return PICK_TEXT.get(k, k or '').replace('{h}', h).replace('{a}', a)


def ledger_row(kind, rec, hs, as_, ver, key, chance, cautious, price, real, bk, edge, first, won, clv, level):
    num = lambda x, n=4: '' if x is None else round(x, n)
    tri = lambda q: [num(x) for x in q] if q and len(q) == 3 else ['', '', '']
    f = first or [None, None, None, None]
    cp = (rec.get('cp') or {}).get(key)
    profit = (round(price - 1, 4) if won else -1.0) if price and price > 1 else ''
    return [iso(NOW), ver or '', kind, rec['id'], (L.get(rec['lk']) or {}).get('name', rec['lk']), rec['ko'], rec['h'], rec['a'], hs, as_,
            pick_text(key, rec['h'], rec['a']), key, level or '', num(chance), num(cautious), num(price, 3), int(bool(real)), bk or '', num(edge),
            f[0] or '', f[1] or '', num(f[2]), num(f[3], 3), num(cp), int(bool(won)), profit, num(clv),
            *tri(rec.get('p')), *tri(rec.get('m')), *tri(rec.get('pm'))]


def ledger_flush(state):
    buf = state.get('ledger_buf') or []
    if not buf:
        return 0
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    by = {}
    for r in buf:
        by.setdefault(str(r[5])[:7], []).append(r)
    for ym, rows in sorted(by.items()):
        f = LEDGER_DIR / f'{ym}.csv'
        new = not f.exists()
        with f.open('a', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(LEDGER_COLS)
            w.writerows(rows)
    state['ledger_buf'] = []
    log(f'ledger: {len(buf)} rows added')
    return len(buf)


def ledger_index():
    """The ledger's months and row counts, newest first, for the page's download links."""
    out = []
    if LEDGER_DIR.is_dir():
        for f in sorted(LEDGER_DIR.glob('*.csv'), reverse=True):
            if re.fullmatch(r'\d{4}-\d{2}', f.stem):
                with f.open(encoding='utf-8') as fh:
                    out.append([f.stem, max(0, sum(1 for _ in fh) - 1)])
    return out


def publish_ledger():
    if not LEDGER_DIR.is_dir():
        return
    dst = OUT_DIR / 'ledger'
    dst.mkdir(parents=True, exist_ok=True)
    for f in LEDGER_DIR.glob('*.csv'):
        shutil.copyfile(f, dst / f.name)


def load_track(state):
    tr = state.get('track')
    repo = load_json(TRACK_FILE)
    if repo and (not tr or (repo.get('updated') or '') > (tr.get('updated') or '')):
        tr = {'since': repo.get('since') or iso(NOW), 'updated': repo.get('updated') or iso(NOW),
              'saved_at': repo.get('saved_at'), 'agg': repo.get('agg') or new_agg(),
              'picks': repo.get('picks') or [], 'slips': repo.get('slips') or [], 'vbets': repo.get('vbets') or [], 'new': 0}
        if not state.get('slips_open') and repo.get('open'):
            state['slips_open'] = repo['open']
    if not tr:
        tr = {'since': iso(NOW), 'updated': iso(NOW), 'saved_at': None, 'agg': new_agg(), 'picks': [], 'slips': [], 'vbets': [], 'new': 0}
    # records made before value bets, Brier scores and engine versions were kept
    for k, v in (('br', [0, 0.0, 0.0, 0.0]), ('vb', new_vb()), ('vbb', {}), ('ver', {})):
        tr['agg'].setdefault(k, v)
    tr.setdefault('vbets', [])
    state['track'] = tr
    return tr, repo


def goal_times(state, mid, hs, as_):
    """The goal minutes seen while the game was live ([[minute, 'h' or 'a'], ...]), when they add up to the final
    score: the slip autopsy and the weekend miracles on the site use them."""
    g = (((state.get('af') or {}).get('live') or {}).get('games') or {}).get(mid)
    try:
        goals = [[int(x[0]), x[1]] for x in (g[5] or [])] if g and len(g) > 5 else []
    except (TypeError, ValueError, IndexError):
        return None
    if goals and sum(1 for x in goals if x[1] == 'h') == hs and sum(1 for x in goals if x[1] == 'a') == as_:
        return sorted(goals)
    return [] if hs == 0 and as_ == 0 else None


# ----------------------------------------------------------------------------------------------
# The live check. Every graded game keeps Statpad's, the bookmakers' and the model's home/draw/away chances. Each
# update compares them on the games graded since the last tuning and over the last 90 days, per kind of league,
# with a 95% range on the difference (paired game by game, so it is far tighter than either error alone). It is the
# early warning for drift: if, on enough games, Statpad's learned blend is clearly worse than the bookmakers' own
# prices in a kind of league, the blend is switched off there until the next monthly tuning has looked again.
# ----------------------------------------------------------------------------------------------
MON_KEEP_D = 400
GUARD_MIN_N = 300
LC_GROUPS = (('big', 'Top leagues'), ('second', 'Second tiers'), ('more', 'Other leagues'), ('other', 'Cups, Europe, women, internationals'))


def lc_group(lk):
    g = (L.get(lk) or {}).get('group')
    return g if g in ('big', 'second', 'more') and not knockout(lk) and not (L.get(lk) or {}).get('w') else 'other'


def lc_stats(rows):
    """[games, Statpad's log loss, the bookmakers', the model's, Statpad minus bookmakers: mean, low, high (95%)]."""
    n = len(rows)
    if not n:
        return None
    lp = [-math.log(max(1e-6, r[3][r[2]])) for r in rows]
    lm = [-math.log(max(1e-6, r[4][r[2]])) for r in rows]
    lo = [-math.log(max(1e-6, r[5][r[2]])) for r in rows]
    d = [a - b for a, b in zip(lp, lm)]
    md = sum(d) / n
    sd = math.sqrt(sum((x - md) ** 2 for x in d) / max(1, n - 1))
    se = sd / math.sqrt(n)
    r5 = lambda x: round(x, 5)
    return [n, r5(sum(lp) / n), r5(sum(lm) / n), r5(sum(lo) / n), r5(md), r5(md - 1.96 * se), r5(md + 1.96 * se)]


def live_check(state):
    mon = state.get('mon') or []
    cut = (NOW - timedelta(days=MON_KEEP_D)).strftime('%Y-%m-%d')
    mon[:] = [r for r in mon if r[0] >= cut]
    since = ((state.get('tune') or {}).get('at') or '')[:10]
    d90 = (NOW - timedelta(days=90)).strftime('%Y-%m-%d')
    out = {'at': iso(NOW), 'since': since or None, 'groups': {}}
    guard = {}
    for g, name in LC_GROUPS:
        rows = [r for r in mon if lc_group(r[1]) == g]
        rs = [r for r in rows if since and r[0] >= since]
        st_rows = [r for r in rs if r[7]]
        e = {'name': name, 'since': lc_stats(rs), 'd90': lc_stats([r for r in rows if r[0] >= d90]), 'blend': lc_stats(st_rows)}
        b = e['blend']
        if b and b[0] >= GUARD_MIN_N and b[5] > 0:   # the learned blend's games: Statpad clearly behind the bookmakers
            guard[g] = True
            e['guard'] = True
        out['groups'][g] = {k: v for k, v in e.items() if v is not None}
    out['all'] = lc_stats([r for r in mon if since and r[0] >= since])
    out['all90'] = lc_stats([r for r in mon if r[0] >= d90])
    if guard != (state.get('guard') or {}):
        log('live check: learned blend switched off for', ', '.join(guard) or 'no league kind')
    state['guard'] = guard
    state['live_check'] = out
    return out


def track_update(doc, state):
    tr, repo = load_track(state)
    agg = tr['agg']
    pend = state.setdefault('track_pending', {})
    open_ = state.setdefault('slips_open', [])
    days = state.setdefault('slip_days', {})
    eng = run_engine(doc)
    by_id = {m['id']: m for m in doc['matches']}
    r4 = lambda xs: [round(x, 4) for x in xs] if xs else None

    # 1) snapshot every open match (the last snapshot before kick-off is the one that gets graded)
    ver = (eng or {}).get('ver')
    if eng:
        for mid, e in eng['picks'].items():
            m = by_id.get(mid)
            if not m or not e.get('k') or parse_iso(m['ko']) <= NOW:
                continue
            best = (m.get('mkt') or {}).get('best')
            rec = pend.get(mid) or {'id': mid, 'op': None}
            rec.update({'lk': m['lk'], 'ko': m['ko'], 'h': m['h'], 'a': m['a'], 'src': e['src'], 'p': r4(e['p']),
                        'pm': r4(e['pm']), 'm': r4(e['m']), 'k': e['k'], 'pp': round(e['pp'], 4), 'pr': e['pr'],
                        'r': bool(e['r']), 'cl': best, 'pu': round(e['pu'], 4) if e.get('pu') is not None else None,
                        'x': [round(v, 3) for v in e['x']] if e.get('x') else None})
            if 't' in e:
                rec['t'] = e['t']
                rec['pl'] = round(e['pl'], 4)
            if rec.get('op') is None and best:
                rec['op'] = best
            if ver:
                rec['v'] = ver
            rec['st'] = 1 if m.get('st') else 0   # the learned blend was used (for the live check)
            if 'f' not in rec:   # the first prediction, kept as it was: time, pick, chance, price
                rec['f'] = [iso(NOW), e['k'], round(e['pp'], 4), e['pr']]
            # the consensus now; at kick-off this is the closing consensus that value bets are measured against
            mk, cp = m.get('mkt') or {}, {}
            if mk.get('p') and len(mk['p']) == 3:
                cp = {'H': mk['p'][0], 'D': mk['p'][1], 'A': mk['p'][2]}
            ou = mk.get('ou') or {}
            if ou.get('pO') and ou.get('line') in (None, 2.5):
                cp['O25'], cp['U25'] = ou['pO'], round(1 - ou['pO'], 4)
            rec['cp'] = cp
            # value bets: taken once, at the price when the Value board first shows them
            vbs = rec.setdefault('vb', {})
            for key, price, p_, edge, bk in e.get('vb') or []:
                if key not in vbs:
                    vbs[key] = [iso(NOW), price, p_, edge, bk]
            pend[mid] = rec

        # 2) slips: keep the latest version for each day, freeze it once its first game kicks off
        for d in sorted(set(days) | set(eng['slips'])):
            rec = days.setdefault(d, {'latest': None, 'frozen': False})
            if rec['frozen']:
                continue
            lat = rec.get('latest')
            begun = lat and any(parse_iso(l['ko']) <= NOW for s in lat for l in s['legs'])
            if lat and (begun or d not in eng['slips']):
                for s in lat:
                    if s['f'] and s['legs']:
                        open_.append({'d': d, 't': s['t'], 'o': round(s['o'], 3), 'p': round(s['p'], 4),
                                      'legs': [[l['id'], l['k'], l['h'], l['a'], l['ko'], l['pr'], None] for l in s['legs']]})
                rec['frozen'] = True
            elif d in eng['slips']:
                rec['latest'] = eng['slips'][d]
        cutoff = (NOW - timedelta(days=12)).strftime('%Y-%m-%d')
        for d in [d for d in days if d < cutoff]:
            del days[d]

    # 3) grade finished matches
    changed = False
    af_void = (state.get('af') or {}).get('void') or {}
    fast = (state.get('af') or {}).get('scores') or {}     # ESPN's finals: grade at once, no 2-hour wait
    scores = state.setdefault('scores', {})
    for mid, rec in list(pend.items()):
        ko = parse_iso(rec['ko'])
        if ko > NOW - timedelta(hours=2) and mid not in fast:
            continue
        res = find_result(rec, state)
        if res is None:
            if mid in af_void or NOW - ko > timedelta(days=30 if rec['lk'] in INTL_LKS else 10):
                del pend[mid]
                agg['nr'] += 1
                changed = True
            continue
        hs, as_ = res
        won = bool(settle(rec['k'], hs, as_))
        t = (rec['t'] if 't' in rec else tier_of(rec['pp'])) or '-'
        agg['n'] += 1
        tt = agg['tiers'][t]
        tt[0] += 1; tt[1] += int(won); tt[2] = round(tt[2] + rec['pp'], 4)
        oc = 0 if hs > as_ else 1 if hs == as_ else 2
        for i in range(3):
            b = agg['cal'][min(9, int(rec['p'][i] * 10))]
            b[0] += 1; b[1] = round(b[1] + rec['p'][i], 4); b[2] += int(i == oc)
        if rec.get('m') and rec.get('pm'):
            mon = state.setdefault('mon', [])   # every graded game's three 1X2 forecasts, for the live check
            mon.append([rec['ko'][:10], rec['lk'], oc, rec['p'], rec['m'], rec['pm'], rec.get('v') or '', rec.get('st', 0)])
            ll = agg['ll']
            ll[0] += 1
            ll[1] = round(ll[1] - math.log(max(1e-6, rec['p'][oc])), 5)
            ll[2] = round(ll[2] - math.log(max(1e-6, rec['m'][oc])), 5)
            ll[3] = round(ll[3] - math.log(max(1e-6, rec['pm'][oc])), 5)
        ver_r = rec.get('v') or 'pre'
        V = agg['ver'].setdefault(ver_r, new_ver())
        V['n'] += 1
        V['from'] = min(V['from'] or rec['ko'], rec['ko']); V['to'] = max(V['to'] or rec['ko'], rec['ko'])
        if t in V['t']:
            vt = V['t'][t]
            vt[0] += 1; vt[1] += int(won); vt[2] = round(vt[2] + rec['pp'], 4)
        if rec.get('m') and rec.get('pm'):
            bs = lambda q: sum((q[i] - (1 if i == oc else 0)) ** 2 for i in range(3))
            br = agg['br']
            br[0] += 1; br[1] = round(br[1] + bs(rec['p']), 5); br[2] = round(br[2] + bs(rec['m']), 5); br[3] = round(br[3] + bs(rec['pm']), 5)
            V['ll'][0] += 1
            V['ll'][1] = round(V['ll'][1] - math.log(max(1e-6, rec['p'][oc])), 5)
            V['ll'][2] = round(V['ll'][2] - math.log(max(1e-6, rec['m'][oc])), 5)
            V['br'][0] += 1; V['br'][1] = round(V['br'][1] + bs(rec['p']), 5); V['br'][2] = round(V['br'][2] + bs(rec['m']), 5)
        buf = state.setdefault('ledger_buf', [])
        for key, (t0, vpr, vp, vedge, vbk) in (rec.get('vb') or {}).items():
            okv = settle(key, hs, as_)
            if okv is None:
                continue
            cpk = (rec.get('cp') or {}).get(key)
            vclv = round(vpr * cpk - 1, 4) if cpk else None
            vb_add(agg['vb'], okv, vpr, vclv)
            vb_add(agg['vbb'].setdefault(vb_band(vedge), new_vb()), okv, vpr, vclv)
            vb_add(V['vb'], okv, vpr, vclv)
            tr['vbets'].append([mid, rec['lk'], rec['ko'], rec['h'], rec['a'], hs, as_, key, vpr, vbk, vedge, vp, int(bool(okv)), vclv, t0, ver_r])
            buf.append(ledger_row('value', rec, hs, as_, ver_r, key, vp, None, vpr, True, vbk, vedge, [t0, key, vp, vpr], okv, vclv, None))
        clv = None
        side = {'H': 0, 'D': 1, 'A': 2}.get(rec['k'])
        if side is not None and rec.get('r') and rec.get('op') and rec.get('cl') and rec['cl'][side] > 1:
            clv = round(rec['op'][side] / rec['cl'][side] - 1, 4)
            c = agg['clv']
            c[0] += 1; c[1] = round(c[1] + clv, 4); c[2] += int(clv > 0)
        tr['picks'].append([mid, rec['lk'], rec['ko'], rec['h'], rec['a'], hs, as_, rec['src'], rec['k'], rec['pp'],
                            rec['pr'], int(bool(rec['r'])), t, int(won), clv] + rec['p'] + [rec.get('pu') or rec['pp']]
                           + (rec.get('x') or [None, None]) + [goal_times(state, mid, hs, as_)])
        cpk = (rec.get('cp') or {}).get(rec['k'])
        buf.append(ledger_row('pick', rec, hs, as_, ver_r, rec['k'], rec['pp'], rec.get('pl'), rec['pr'], rec['r'], None, None, rec.get('f'), won,
                              round(rec['pr'] * cpk - 1, 4) if cpk and rec.get('r') and rec['pr'] and rec['pr'] > 1 else None, t if t != '-' else None))
        scores[mid] = [hs, as_, rec['ko']]
        del pend[mid]
        changed = True

    # 4) settle slips leg by leg
    for s in list(open_):
        legs = s['legs']
        for l in legs:
            if l[6] is None and l[0] in scores:
                l[6] = int(bool(settle(l[1], scores[l[0]][0], scores[l[0]][1])))
        res = None
        dead = lambda l: l[6] is None and (l[0] in af_void or NOW - parse_iso(l[4]) > timedelta(days=30 if l[0].split('-')[0] in INTL_LKS else 10))
        if any(l[6] == 0 for l in legs):
            res = 'lost'
        elif all(l[6] == 1 for l in legs):
            res = 'won'
        elif all(l[6] == 1 or dead(l) for l in legs) or any(dead(l) and NOW - parse_iso(l[4]) > timedelta(days=10) for l in legs):
            res = 'void'
        if res:
            s['res'] = res
            open_.remove(s)
            tr['slips'].append(s)
            if res == 'void':
                agg['void'] += 1
            else:
                a = agg['slips'][s['t']]
                a[0] += 1; a[1] += int(res == 'won'); a[2] = round(a[2] + (s['o'] - 1 if res == 'won' else -1), 3)
                a[3] = round(a[3] + s['p'], 4)
            changed = True

    for k in [k for k, v in scores.items() if NOW - parse_iso(v[2]) > timedelta(days=30)]:
        del scores[k]
    keep_from = iso(NOW - timedelta(days=TRACK_KEEP_DAYS))
    tr['picks'] = [p for p in tr['picks'] if p[2] >= keep_from]
    keep_day = (NOW - timedelta(days=TRACK_KEEP_DAYS)).strftime('%Y-%m-%d')
    tr['slips'] = [s for s in tr['slips'] if s['d'] >= keep_day]
    tr['vbets'] = [v for v in tr['vbets'] if v[2] >= keep_from]
    if changed:
        tr['updated'] = iso(NOW)
        tr['new'] = tr.get('new', 0) + 1

    # each entry level's picks waiting for a result: from 4 hours ago (in play) to a day and a half ahead
    pend_t, waiting = {'A': 0, 'B': 0, 'C': 0, '-': 0}, []
    w_lo, w_hi = NOW - timedelta(hours=4), NOW + timedelta(hours=36)
    for rec in pend.values():
        t = (rec['t'] if 't' in rec else tier_of(rec['pp'])) or '-'
        pend_t[t] = pend_t.get(t, 0) + 1
        if t != '-' and w_lo <= parse_iso(rec['ko']) <= w_hi:
            waiting.append([rec['id'], rec['lk'], rec['ko'], rec['h'], rec['a'], rec['k'], rec['pp'], rec['pr'], int(bool(rec['r'])), t])
    waiting.sort(key=lambda w: (w[2], w[0]))
    # value bets taken and not yet settled: games from 4 hours ago to a day and a half ahead
    vwait, vopen = [], 0
    for rec in pend.values():
        for key, (t0, vpr, vp, vedge, vbk) in (rec.get('vb') or {}).items():
            vopen += 1
            if w_lo <= parse_iso(rec['ko']) <= w_hi:
                vwait.append([rec['id'], rec['lk'], rec['ko'], rec['h'], rec['a'], key, vpr, vedge, vbk, t0, vp])
    vwait.sort(key=lambda w: (w[2], w[0]))
    pub = {'app': 'Statpad', 'schema': 1, 'since': tr['since'], 'updated': tr['updated'], 'saved_at': tr.get('saved_at'),
           'tz': TRACK_TZ, 'target': TRACK_TARGET, 'agg': agg, 'picks': tr['picks'], 'slips': tr['slips'],
           'open': open_, 'pending': len(pend), 'pend_t': pend_t, 'waiting': waiting[:150],
           'vbets': tr['vbets'], 'vwait': vwait[:120], 'vopen': vopen, 'ver': ver, 'vb_min': VB_MIN}
    # commit to the repo: new grades at most every few hours, and a keep-alive commit regardless
    saved = parse_iso(tr['saved_at']) if tr.get('saved_at') else None
    keepalive = repo is None or saved is None or NOW - saved >= timedelta(days=KEEPALIVE_DAYS)
    if keepalive or (tr.get('new') and NOW - saved >= timedelta(hours=SAVE_EVERY_H)):
        tr['saved_at'] = pub['saved_at'] = iso(NOW)
        tr['new'] = 0
        ledger_flush(state)
        TRACK_FILE.write_text(json.dumps(pub, ensure_ascii=False, separators=(',', ':')))
        log('track: saved track.json for commit' + (' (keep-alive)' if keepalive else ''))
    pub['ledger'] = ledger_index()
    log(f"track: {len(pend)} predictions waiting, {agg['n']} graded, {len(open_)} slips in play, {vopen} value bets open")
    return pub


def publish_results(state):
    """Final scores (90 minutes) for the last three weeks, by Statpad match id, so the site can settle saved bets."""
    cut = NOW - timedelta(days=21)
    sc = {mid: [v[0], v[1]] for mid, v in (state.get('scores') or {}).items() if parse_iso(v[2]) >= cut}
    for mid, v in ((state.get('af') or {}).get('scores') or {}).items():
        if parse_iso(v[2]) >= cut:
            sc.setdefault(mid, [v[0], v[1]])
    void = {mid: v[0] for mid, v in ((state.get('af') or {}).get('void') or {}).items() if parse_iso(v[1]) >= cut}
    return {'app': 'Statpad', 'generated': iso(NOW), 'scores': sc, 'void': void}



# ----------------------------------------------------------------------------------------------
# Backtest: last season replayed. Each week's ratings come only from games played before that
# week, prices are football-data's pre-match averages and best prices, and the picks, tiers and
# slips come from the same engine the site runs. Recomputed weekly; published as backtest.json.
# ----------------------------------------------------------------------------------------------
BT_EVERY_H = 7 * 24
BT_SKIP_WEEKS = 4          # the first weeks of a season are left out while ratings settle

ENGINE_LOAD = r"""
const fs = require('fs');
const [, , htmlPath, inPath] = process.argv;
const html = fs.readFileSync(htmlPath, 'utf8');
const s = html.indexOf('/* Statpad engine'), e = html.indexOf('module.exports = ENGINE;', s);
const mod = { exports: null };
new Function('module', html.slice(s, e) + 'module.exports = ENGINE;')(mod);
const E = mod.exports;
const inp = JSON.parse(fs.readFileSync(inPath, 'utf8'));
"""

NODE_BT = ENGINE_LOAD + r"""
if (E.setCalib) E.setCalib(null);   // the replay shows the engine's own chances, without the track record's correction
if (E.setMcal) E.setMcal(inp.mcal || null);
const out = { picks: {}, slips: {}, value: [] }, byDay = {};
for (const m of inp.matches) {
  let a = null;
  try { a = E.analyse(m, Date.parse(m.ko) - 3600e3); } catch (err) { a = null; }
  if (!a) continue;
  // the pick the engine made before tiers used the cautious chance: highest stated chance
  let ob = null;
  for (const s of a.sels) { if (s.x || s.key === 'D' || s.price < 1.05) continue; if (!ob || s.pAdj > ob.pAdj) ob = s; }
  out.picks[m.id] = { k: a.top ? a.top.key : null, pp: a.top ? a.top.pAdj : null, pr: a.top ? a.top.price : null,
    t: a.top ? a.tier : null, ok: ob ? ob.key : null, op: ob ? ob.pAdj : null,
    p: [a.p.H, a.p.D, a.p.A], m: a.mkt ? a.mkt.p : null, pm: a.pMod ? [a.pMod.H, a.pMod.D, a.pMod.A] : null };
  for (const v of E.value([{ m, a }], inp.vb_min || 0.02)) out.value.push([m.id, v.s.key, v.s.price, v.s.p, +v.s.edge.toFixed(4)]);
  const d = m.ko.slice(0, 10); (byDay[d] = byDay[d] || []).push({ m, a });
}
for (const [d, list] of Object.entries(byDay)) {
  out.slips[d] = ['C', 'B', 'A'].map((t) => { const sl = E.buildSlip(list, t, inp.target);
    return { t, f: sl.feasible, o: sl.odds, p: sl.prob, legs: sl.legs.map((l) => [l.m.id, l.s.key]) }; });
}
out.ver = E.VERSION || null;
process.stdout.write(JSON.stringify(out));
"""

NODE_FIT = ENGINE_LOAD + r"""
const out = inp.map(([h, a, o]) => { const f = E.fit({ H: h, A: a, O: o }, [1.4, 1.1]); return [+f[0].toFixed(4), +f[1].toFixed(4)]; });
process.stdout.write(JSON.stringify(out));
"""


def run_node(script, payload, timeout=600):
    import shutil
    import subprocess
    import tempfile
    node = shutil.which('node')
    if not node or not INDEX_FILE.exists():
        return None
    with tempfile.TemporaryDirectory() as td:
        js, inp = Path(td) / 'run.js', Path(td) / 'in.json'
        js.write_text(script)
        inp.write_text(json.dumps(payload))
        r = subprocess.run([node, str(js), str(INDEX_FILE), str(inp)], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            log('engine run failed', (r.stderr or '')[-300:])
            return None
        return json.loads(r.stdout)


# ----------------------------------------------------------------------------------------------
# Past seasons of football-data's main leagues, shared by the tuning and the replay
# ----------------------------------------------------------------------------------------------
_FD_SEASONS = {}


def fd_season(lk, code):
    """(date, home, away, home goals, away goals, home shots on target, away SoT, prices, kick-off, commentary xG or
    None) for one season."""
    key = (lk, code)
    if key not in _FD_SEASONS:
        rows = []
        r = fetch(f'{FD}/mmz4281/{code}/{L[lk]["fd"]}.csv')
        if r.status == 200 and r.body:
            for row in read_csv(r.body):
                d = parse_date(col(row, 'Date'))
                h, a = col(row, 'HomeTeam', 'Home'), col(row, 'AwayTeam', 'Away')
                hg, ag = fnum(col(row, 'FTHG', 'HG')), fnum(col(row, 'FTAG', 'AG'))
                if d and h and a and hg is not None and ag is not None:
                    mk = fd_market(row, None)
                    if mk:
                        mk['cl'] = fd_close(row)   # closing consensus, for closing-line value in the replay
                    rows.append((d, h, a, int(hg), int(ag), fnum(col(row, 'HST')), fnum(col(row, 'AST')),
                                 mk, uk_to_utc(d, col(row, 'Time')), xg_for(lk, d, h, a)))   # football-data times are UK time
        _FD_SEASONS[key] = rows
    return _FD_SEASONS[key]


def season_name(code):
    return f'20{code[:2]}/{code[2:]}'


def season_fits(cur_rows, prev_rows, P, skip=None):
    """The ratings at the start of each week of a season, each built only from earlier games."""
    skip = BT_SKIP_WEEKS if skip is None else skip
    cur = sorted(cur_rows, key=lambda r: r[0])
    if len(cur) < 60:
        return None
    games = [(r[0], r[1], r[2], r[3], r[4], False, r[5], r[6], r[9] if len(r) > 9 else None) for r in prev_rows] + \
            [(r[0], r[1], r[2], r[3], r[4], True, r[5], r[6], r[9] if len(r) > 9 else None) for r in cur]
    start = cur[0][0]
    teams = sorted({r[1] for r in cur} | {r[2] for r in cur})
    weeks = [w for w in sorted({(r[0] - start).days // 7 for r in cur}) if w >= skip]
    got = fit_series(games, teams, [start + timedelta(days=7 * w) for w in weeks], P)
    return cur, start, {weeks[i]: rt for i, rt in got.items()}


def season_preds(cur_rows, prev_rows, P, skip=None, sf=None):
    """Every game of a season with the expected goals, and their spread, from the ratings as they stood
    that week: built only from earlier games."""
    sf = sf or season_fits(cur_rows, prev_rows, P, skip)
    if not sf:
        return []
    cur, start, fits = sf
    out = []
    for r in cur:
        rt = fits.get((r[0] - start).days // 7)
        if not rt or r[1] not in rt['teams'] or r[2] not in rt['teams']:
            continue
        (ah, dh), (aa, da) = rt['teams'][r[1]], rt['teams'][r[2]]
        (sah, sdh), (saa, sda) = rt['se'][r[1]], rt['se'][r[2]]
        out.append((r, math.exp(rt['mu'] + rt['home'] + ah + da), math.exp(rt['mu'] + aa + dh),
                    math.hypot(sah, sda), math.hypot(saa, sdh)))
    return out


RHO_DC = -0.07   # the engine's Dixon-Coles correction (its default)
# The draw correction fitted per league by the monthly tuning (see fit_rho): {'_': pooled value, league: value}, or None
RHO_L = None


def rho_for(lk):
    """The draw correction the engine uses for a league's games."""
    if not RHO_L:
        return RHO_DC
    return RHO_L.get(lk, RHO_L.get('_', RHO_DC))


def dc_probs(lh, la, n=11, rho=None):
    """Home, draw, away and over 2.5 from the Dixon-Coles scoreline grid (as the engine builds it)."""
    rho = RHO_DC if rho is None else rho
    ph, pa = [math.exp(-lh)], [math.exp(-la)]
    for k in range(1, n):
        ph.append(ph[-1] * lh / k)
        pa.append(pa[-1] * la / k)
    c00, c01, c10, c11 = 1 - lh * la * rho, 1 + lh * rho, 1 + la * rho, 1 - rho
    H = D = A = U = 0.0
    for i in range(n):
        pi = ph[i]
        for j in range(n):
            v = pi * pa[j]
            if i < 2 and j < 2:
                v *= c00 if i == 0 and j == 0 else c01 if i == 0 else c10 if j == 0 else c11
            if i > j:
                H += v
            elif i == j:
                D += v
            else:
                A += v
            if i + j < 3:
                U += v
    t = H + D + A
    return H / t, D / t, A / t, 1 - U / t


def dc_exact(lh, la, hg, ag, n=11, rho=None):
    """The chance of one exact score on the engine's grid (past its edge, plain Poisson)."""
    rho = RHO_DC if rho is None else rho
    po = lambda l, k: math.exp(-l) * l ** k / math.factorial(k)
    if hg >= n or ag >= n:
        return po(lh, hg) * po(la, ag)
    c00, c01, c10, c11 = 1 - lh * la * rho, 1 + lh * rho, 1 + la * rho, 1 - rho
    tot = 0.0
    for i in range(n):
        for j in range(n):
            v = po(lh, i) * po(la, j)
            if i < 2 and j < 2:
                v *= c00 if i == 0 and j == 0 else c01 if i == 0 else c10 if j == 0 else c11
            tot += v
            if i == hg and j == ag:
                hit = v
    return hit / tot


def lam_from_1x2(ph, pa):
    """The expected goals that give these home and away chances on the engine's grid (Newton's method on the logs),
    for graded games saved before the record kept expected goals."""
    lg = lambda p: math.log(max(1e-6, p) / max(1e-6, 1 - p))
    def f(u, v):
        H, _, A, _ = dc_probs(math.exp(u), math.exp(v))
        return lg(H) - lg(ph), lg(A) - lg(pa)
    u, v = math.log(1.4), math.log(1.1)
    for _ in range(14):
        F0, F1 = f(u, v)
        if abs(F0) + abs(F1) < 1e-6:
            break
        e = 1e-4
        Fu, Fv = f(u + e, v), f(u, v + e)
        a, b, c, d = (Fu[0] - F0) / e, (Fv[0] - F0) / e, (Fu[1] - F1) / e, (Fv[1] - F1) / e
        det = a * d - b * c
        if not det:
            break
        u = max(-3, min(1.9, u - max(-1, min(1, (d * F0 - b * F1) / det))))
        v = max(-3, min(1.9, v - max(-1, min(1, (a * F1 - c * F0) / det))))
    return math.exp(u), math.exp(v)


MIR_KEEP = 15


def publish_miracles(tr):
    """Weekend miracles: the latest weekend's graded games (Friday to Monday, in the record's time zone), the rarest
    final scores first, each with how often Statpad's grid gave that exact score and that result before kick-off."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TRACK_TZ)
    except Exception:
        tz = timezone.utc
    loc = NOW.astimezone(tz)
    fri = (loc - timedelta(days=(loc.weekday() - 4) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    t0, t1 = fri.astimezone(timezone.utc), (fri + timedelta(days=4)).astimezone(timezone.utc)
    items = []
    for p in tr.get('picks') or []:
        ko = parse_iso(p[2])
        if not (t0 <= ko < t1) or p[5] is None or p[6] is None:
            continue
        hs, as_ = int(p[5]), int(p[6])
        lh, la = (p[19], p[20]) if len(p) > 20 and p[19] and p[20] else lam_from_1x2(p[15], p[17])
        o = 0 if hs > as_ else 1 if hs == as_ else 2
        goals = p[21] if len(p) > 21 and isinstance(p[21], list) else None
        items.append([p[0], p[1], p[2], p[3], p[4], hs, as_, round(dc_exact(lh, la, hs, as_, rho=rho_for(p[1])), 6), round(p[15 + o], 4), round(lh, 3), round(la, 3), goals])
    if not items:
        return None
    items.sort(key=lambda x: x[7])
    wins = [x for x in items if x[5] != x[6]]
    shock = min(wins, key=lambda x: x[8]) if wins else None
    keep = items[:MIR_KEEP] + ([shock] if shock and shock not in items[:MIR_KEEP] else [])
    return {'app': 'Statpad', 'generated': iso(NOW), 'tz': TRACK_TZ, 'from': iso(t0), 'to': iso(t1), 'n': len(items),
            'live': NOW < t1, 'shock': shock[0] if shock else None,
            'cols': ['id', 'lk', 'ko', 'home', 'away', 'home goals', 'away goals', 'exact-score chance', 'result chance', 'xg home', 'xg away', 'goal minutes'],
            'items': keep}


def game_loss(lh, la, hg, ag, rho=None):
    """Log loss on the result (1X2) and on over/under 2.5 for one game."""
    H, D, A, O = dc_probs(lh, la, rho=rho)
    r = H if hg > ag else D if hg == ag else A
    o = O if hg + ag > 2 else 1 - O
    return -math.log(max(1e-9, r)), -math.log(max(1e-9, o))


_GRID_NP = {}


def dc_grid_np(np, lh, la, rho=None, n=11):
    """The engine's scoreline grid for many games at once: (games, home goals, away goals), each grid adding up to 1."""
    rho = RHO_DC if rho is None else rho
    if n not in _GRID_NP:
        k = np.arange(n)
        _GRID_NP[n] = (k, np.array([math.lgamma(i + 1) for i in range(n)]), np.subtract.outer(k, k), np.add.outer(k, k))
    k, lgf, df, sm = _GRID_NP[n]
    lh, la = np.atleast_1d(np.asarray(lh, dtype=float)), np.atleast_1d(np.asarray(la, dtype=float))
    ph = np.exp(-lh[:, None] + np.log(lh)[:, None] * k - lgf)
    pa = np.exp(-la[:, None] + np.log(la)[:, None] * k - lgf)
    G = ph[:, :, None] * pa[:, None, :]
    G[:, 0, 0] *= 1 - lh * la * rho
    G[:, 0, 1] *= 1 + lh * rho
    G[:, 1, 0] *= 1 + la * rho
    G[:, 1, 1] *= 1 - rho
    return G / G.sum(axis=(1, 2))[:, None, None]


def dc_probs_np(np, lh, la, rho=None, n=11):
    """dc_probs for many games at once (arrays of expected goals; rho a number or an array). (H, D, A, over 2.5)."""
    G = dc_grid_np(np, lh, la, rho, n)
    _, _, df, sm = _GRID_NP[n]
    H = (G * (df > 0)).sum(axis=(1, 2))
    D = (G * (df == 0)).sum(axis=(1, 2))
    U = (G * (sm < 3)).sum(axis=(1, 2))
    return H, D, 1 - H - D, 1 - U


def fit_market_np(np, pH, pA, pO=None, rho=None, iters=14, start=None):
    """The engine's fit (expected goals that give the market's home, away and over 2.5 chances on the grid, the
    over/under counting half) for many games at once, by Gauss-Newton steps. Arrays of (home, away) expected goals."""
    pH, pA = np.asarray(pH, dtype=float), np.asarray(pA, dtype=float)
    hasO = None if pO is None else np.array([o is not None for o in pO])
    tO = None if pO is None else np.array([o if o is not None else 0.0 for o in pO], dtype=float)
    u, v = np.full(len(pH), math.log(1.4)), np.full(len(pH), math.log(1.1))
    if start is not None:
        u, v = np.log(start[0]), np.log(start[1])

    def res(u_, v_):
        H, _, A, O = dc_probs_np(np, np.exp(u_), np.exp(v_), rho)
        r = [H - pH, A - pA]
        r.append(np.where(hasO, math.sqrt(0.5) * (O - tO), 0.0) if hasO is not None else np.zeros(len(pH)))
        return np.stack(r, 1)
    e = 1e-5
    for _ in range(iters):
        r0 = res(u, v)
        Ju, Jv = (res(u + e, v) - r0) / e, (res(u, v + e) - r0) / e
        a, b, c = (Ju * Ju).sum(1) + 1e-12, (Ju * Jv).sum(1), (Jv * Jv).sum(1) + 1e-12
        gu, gv = (Ju * r0).sum(1), (Jv * r0).sum(1)
        det = a * c - b * b
        du, dv = -(c * gu - b * gv) / det, -(a * gv - b * gu) / det
        u = np.clip(u + np.clip(du, -1, 1), -3, 1.9)
        v = np.clip(v + np.clip(dv, -1, 1), -3, 1.9)
        if float(np.abs(du).max() + np.abs(dv).max()) < 1e-9:
            break
    return np.exp(u), np.exp(v)


def losses_np(np, lh, la, hg, ag, rho=None):
    """game_loss for many games at once: arrays of the 1X2 and the over/under 2.5 log losses."""
    H, D, A, O = dc_probs_np(np, lh, la, rho)
    hg, ag = np.asarray(hg), np.asarray(ag)
    r = np.where(hg > ag, H, np.where(hg == ag, D, A))
    o = np.where(hg + ag > 2, O, 1 - O)
    return -np.log(np.maximum(1e-9, r)), -np.log(np.maximum(1e-9, o))


# ----------------------------------------------------------------------------------------------
# Fatigue and motivation. Rest: days since each team's previous league game (football-data only has league
# games, so cup and European games aren't counted). Motivation: from the season simulation, a team whose
# title, European, promotion and relegation chances barely move whatever the result has nothing left to
# play for. Both are fitted on past seasons and stay off unless they also help on a season the fit never saw.
# ----------------------------------------------------------------------------------------------
FX = {'rest': None, 'dead': None, 'open': None}
# rest: (strength, days below which a side counts as short of rest); a negative strength means short-rested
# sides did better than the prices expected. dead: (strength, swing threshold): a side with nothing to play
# for scores less and concedes more. open: (strength, threshold): games where neither side has anything to
# play for have more goals.
FX_REST_GRID = [(b, t0) for t0 in (4, 5) for b in (-0.2, -0.15, -0.1, -0.06, -0.03, 0.03, 0.06, 0.1, 0.15, 0.2)]
FX_DEAD_GRID = [(b, thr) for thr in (0.005, 0.01, 0.02) for b in (-0.1, -0.05, 0.05, 0.1, 0.15, 0.2)]
FX_OPEN_GRID = [(b, thr) for thr in (0.005, 0.01, 0.02) for b in (0.03, 0.06, 0.1, 0.15)]
FX_SIMS = 2000          # simulated seasons per past week (the live stakes use 10,000)
FX_MIN_GAIN = 1e-5      # on the unseen season, the error must drop by at least this much a game


def tired(rest, t0):
    """0 when rested; 1 after only two days."""
    if rest is None:
        return 0.0
    return max(0.0, min(1.0, (t0 - rest) / (t0 - 2)))


def is_dead(swing, thr):
    return swing is not None and swing < thr


def fx_shift(rh, ra, dh, da, F=None):
    """The shifts in log expected goals for the home and the away side."""
    F = FX if F is None else F
    x = y = 0.0
    if F.get('rest'):
        b, t0 = F['rest']
        x -= b * (tired(rh, t0) - tired(ra, t0))
    if F.get('dead'):
        b, thr = F['dead']
        x -= b * (int(is_dead(dh, thr)) - int(is_dead(da, thr)))
    if F.get('open'):
        b, thr = F['open']
        if is_dead(dh, thr) and is_dead(da, thr):
            y += b
    return x + y, -x + y


def rest_days(cur_rows, prev_rows):
    """Days since each side's previous league game, for every game of a season."""
    last, out = {}, {}
    for r in sorted(list(prev_rows) + list(cur_rows), key=lambda r: r[0]):
        d, h, a = r[0], r[1], r[2]
        out[(d, h, a)] = ((d - last[h]).days if h in last else None, (d - last[a]).days if a in last else None)
        last[h] = last[a] = d
    return out


def past_stakes_jobs(lk, sf, n_sims=FX_SIMS):
    """For each week of the second half of a past season: the table then, the games left (from that week's
    ratings) and that week's games to measure."""
    if not sf or lk not in SIM_LEAGUES:
        return [], []
    cur, start, fits = sf
    if not round_robin(cur):
        return [], []
    teams = sorted({r[1] for r in cur} | {r[2] for r in cur})
    n, idx = len(teams), {t: i for i, t in enumerate(teams)}
    zn = zones_for(lk)
    jobs, keys = [], []
    for w, rt in sorted(fits.items()):
        cut = start + timedelta(days=7 * w)
        played = [r for r in cur if r[0] < cut]
        if len(played) < 0.5 * n * (n - 1):
            continue
        later = [r for r in cur if r[0] >= cut]
        tg = [k for k, r in enumerate(later) if r[0] < cut + timedelta(days=7)]
        if not tg:
            continue
        base = [[0, 0, 0] for _ in teams]
        for r in played:
            i, j = idx[r[1]], idx[r[2]]
            base[i][1] += r[3]; base[i][2] += r[4]; base[j][1] += r[4]; base[j][2] += r[3]
            if r[3] > r[4]:
                base[i][0] += 3
            elif r[3] < r[4]:
                base[j][0] += 3
            else:
                base[i][0] += 1; base[j][0] += 1
        T = rt['teams']
        fx = []
        for r in later:
            ah, dh = T.get(r[1], (0.0, 0.0))
            aa, da = T.get(r[2], (0.0, 0.0))
            fx.append([idx[r[1]], idx[r[2]], round(math.exp(rt['mu'] + rt['home'] + ah + da), 4), round(math.exp(rt['mu'] + aa + dh), 4)])
        jobs.append({'n': n_sims, 'sig': SIM_SIG, 'seed': sim_seed(lk, cut.strftime('%Y-%m-%d')), 'zones': zn, 'base': base, 'fx': fx, 'tgt': tg})
        keys.append([(lk, later[k][0].strftime('%Y-%m-%d'), later[k][1], later[k][2]) for k in tg])
    return jobs, keys


def run_past_stakes(jobs, keys):
    """The largest swing (any zone) for each side of each measured game: (lk, date, home, away) -> (home, away)."""
    out = {}
    for i in range(0, len(jobs), 150):   # in batches, to keep each node run's input small
        got = run_node(NODE_SIM, {'engine': False, 'jobs': jobs[i:i + 150]}, timeout=600)
        if got is None:
            return None
        for ks, res in zip(keys[i:i + 150], got):
            if res:
                for k, sides in zip(ks, res):
                    out[k] = (sides[0][1], sides[1][1])
    return out


# ----------------------------------------------------------------------------------------------
# Tuning: the model's settings (memory, last season's weight, shrinkage, shots) are searched on two past
# seasons, then checked on the last completed season, which the search never saw. They are switched on
# only if they beat the defaults there, after blending with the market as the site does.
# ----------------------------------------------------------------------------------------------
TUNE_EVERY_D = 30
TUNE_NOW = ENV('STATPAD_TUNE_NOW') != '0'   # include this season's games so far in the check (testing can turn it off)
TUNE_V = 2          # 2: prices read by the power method, market share up to 100%
TUNE_BUDGET_S = float(ENV('STATPAD_TUNE_BUDGET') or 420)
TUNE_GRID = {'sw': [0.0, 0.15, 0.3, 0.45, 0.6, 0.75], 'hl': [120.0, 180.0, 270.0, 365.0, 540.0],
             'pw': [0.4, 0.6, 0.8, 1.0], 'k': [1.5, 2.5, 3.5, 5.0, 6.0, 8.0]}
W_GRID = [0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.97, 1.0]
W_DEFAULT = 0.8
_TUNE_JOBS = []   # (this season's rows, last season's rows); read by forked worker processes
_TUNE_LKS = []    # the league of each


def _tune_job(arg):
    i, P, full = arg
    cur_rows, prev_rows = _TUNE_JOBS[i]
    n = l1 = l2 = 0.0
    rows = []
    sf = season_fits(cur_rows, prev_rows, P)
    lk = _TUNE_LKS[i] if i < len(_TUNE_LKS) else None
    rd = rest_days(cur_rows, prev_rows) if full else None
    if full:
        both = list(prev_rows) + list(cur_rows)
        eg = [elo_run(both, hfa, k)[1] for hfa, k in ELO_GRID]
        gm = goal_means(prev_rows) if prev_rows else goal_means(cur_rows)
    np = numpy_lib()
    if np and not full:
        pr = season_preds(cur_rows, prev_rows, P, sf=sf)
        if not pr:
            return (0, 0.0, 0.0)
        x, y = losses_np(np, [p[1] for p in pr], [p[2] for p in pr], [p[0][3] for p in pr], [p[0][4] for p in pr])
        return (len(pr), float(x.sum()), float(y.sum()))
    start = sf[1] if sf else None
    for r, lh, la, _sh, _sa in season_preds(cur_rows, prev_rows, P, sf=sf):
        if full:
            mk = r[7]
            if mk and mk.get('p'):
                rh, ra = rd.get((r[0], r[1], r[2]), (None, None))
                key = (r[0], r[1], r[2])
                # 12: the ratings' spread (log expected goals), 13: the market's draw chance, 14: the season's week,
                # 15: home and away shots on target, 16: the closing consensus (measuring only, never a feature)
                rows.append((lh, la, r[3], r[4], mk['p'][0], mk['p'][2], (mk.get('ou') or {}).get('pO'),
                             rh, ra, (lk, r[0].strftime('%Y-%m-%d'), r[1], r[2]), [g.get(key) for g in eg], gm,
                             (_sh, _sa), mk['p'][1], (r[0] - start).days // 7, (r[5], r[6]), mk.get('cl')))
            continue
        x, y = game_loss(lh, la, r[3], r[4])
        n += 1
        l1 += x
        l2 += y
    if not full:
        return (n, l1, l2)
    jobs, keys = past_stakes_jobs(lk, sf) if lk else ([], [])
    return rows, jobs, keys


def _pool():
    try:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        cores = os.cpu_count() or 1
        if cores < 2:
            return None
        return ProcessPoolExecutor(max_workers=min(4, cores), mp_context=multiprocessing.get_context('fork'))
    except Exception:
        return None


def _pmap(pool, fn, args):
    if pool:
        try:
            return list(pool.map(fn, args))
        except Exception as e:
            log('tuning: parallel run failed, going on in one process', repr(e))
    return [fn(x) for x in args]


# The search itself. With NumPy: a Bayesian search over all the settings at once. A Gaussian-process stand-in for the
# error surface is fitted to every setting tried so far, and the next one tried is where the expected improvement
# is largest; it stops when nothing is expected to improve or the time is up. Without NumPy: the old search, one
# setting at a time over a fixed grid.
TUNE_SPACE = [('sw', 0.0, 0.85, False), ('hl', 90.0, 1600.0, True), ('pw', 0.2, 1.0, False), ('k', 0.8, 20.0, True), ('nt', 0.0, 0.35, False)]
TUNE_ROUND = {'sw': 3, 'hl': 0, 'pw': 3, 'k': 2, 'nt': 3, 'sx': 3, 'q': 7, 'p0': 4, 'ps': 3, 'qs': 4}
XG_MIN_COVER = 0.8      # share of the xG leagues' training games with commentary xG before the search tries it
XG_MIN_GAIN = 2e-4      # model alone, a game, on the check season's xG leagues
TUNE_MAX_EVALS = 110
TUNE_EI_STOP = 4e-7     # stop when the best expected improvement (log loss a game) stays below this


def _unit(np, P, space):
    return np.array([(math.log(P[k]) - math.log(lo)) / (math.log(hi) - math.log(lo)) if lg else (P[k] - lo) / (hi - lo)
                     for k, lo, hi, lg in space]).clip(0, 1)


def _from_unit(u, space, base):
    P = dict(base)
    for x, (k, lo, hi, lg) in zip(u, space):
        v = math.exp(math.log(lo) + float(x) * (math.log(hi) - math.log(lo))) if lg else lo + float(x) * (hi - lo)
        P[k] = round(v, TUNE_ROUND[k]) if TUNE_ROUND[k] else float(round(v))
    return P


def _ncdf(np, z):
    """The normal distribution's CDF (Abramowitz and Stegun 7.1.26: error below 2e-7)."""
    x = np.abs(z) / math.sqrt(2)
    t = 1 / (1 + 0.3275911 * x)
    e = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-x * x)
    return 0.5 * (1 + np.sign(z) * e)


def _matern(np, A, B, ls):
    d = np.sqrt((((A[:, None, :] - B[None, :, :]) / ls) ** 2).sum(-1))
    s = math.sqrt(5) * d
    return (1 + s + s * s / 3) * np.exp(-s)


def _gp(np, X, y):
    """A Gaussian process fitted to the settings tried: Matérn 5/2, one length scale per setting, chosen by the
    marginal likelihood. Returns predict(points) -> (mean, sd), in log loss."""
    ym, ys = float(y.mean()), float(y.std()) or 1.0
    z = (y - ym) / ys
    d = X.shape[1]

    def nll(ls, nz):
        K = _matern(np, X, X, ls) + nz * np.eye(len(X))
        try:
            Lc = np.linalg.cholesky(K)
        except np.linalg.LinAlgError:
            return None
        al = np.linalg.solve(Lc.T, np.linalg.solve(Lc, z))
        return 0.5 * float(z @ al) + float(np.log(np.diag(Lc)).sum()), Lc, al
    best = None
    for l0 in (0.1, 0.16, 0.25, 0.4, 0.65, 1.0):
        for nz in (1e-6, 1e-4, 1e-2):
            r = nll(np.full(d, l0), nz)
            if r and (best is None or r[0] < best[0]):
                best = (r[0], np.full(d, l0), nz, r[1], r[2])
    for _ in range(2):   # then each setting's own length scale
        for j in range(d):
            for f in (0.5, 2.0, 4.0):
                ls = best[1].copy()
                ls[j] = min(8.0, ls[j] * f)
                r = nll(ls, best[2])
                if r and r[0] < best[0] - 1e-9:
                    best = (r[0], ls, best[2], r[1], r[2])
    _, ls, nz, Lc, al = best

    def predict(P):
        Ks = _matern(np, P, X, ls)
        mu = Ks @ al
        v = np.linalg.solve(Lc, Ks.T)
        var = np.maximum(1e-12, 1 - (v * v).sum(0))
        return ym + ys * mu, ys * np.sqrt(var)
    return predict


def bayes_search(np, score, starts, space, base, t0, budget, max_evals, seed, tag):
    """Minimise score(settings) over the space; starts are tried first. Returns (best settings, its score, evals, finished)."""
    rng = np.random.default_rng(seed)
    d = len(space)
    X, y, Ps, seen = [], [], [], set()

    def run(P):
        key = json.dumps(P, sort_keys=True)
        if key in seen:
            return
        seen.add(key)
        X.append(_unit(np, P, space))
        y.append(score(P))
        Ps.append(P)
    for P in starts:
        run(dict(base, **{k: P.get(k, base[k]) for k, *_ in space}))
    # a spread of first guesses (Latin hypercube), so the stand-in sees the whole space
    n0 = max(6, 2 * d)
    lhs = (np.argsort(rng.random((d, n0)), axis=1).T + rng.random((n0, d))) / n0
    for u in lhs:
        if time.time() - t0 > budget:
            i = int(np.argmin(y))
            return Ps[i], y[i], len(y), False
        run(_from_unit(u, space, base))
    quiet, finished = 0, True
    while len(y) < max_evals:
        if time.time() - t0 > budget:
            finished = False
            break
        Xa, ya = np.array(X), np.array(y)
        pred = _gp(np, Xa, ya)
        b = int(ya.argmin())
        top = np.argsort(ya)[:3]
        cand = np.vstack([rng.random((3000, d))] + [np.clip(Xa[i] + rng.normal(0, s, (400, d)), 0, 1) for i in top for s in (0.03, 0.1)])
        mu, sd = pred(cand)
        imp = ya[b] - mu
        zz = imp / sd
        ei = imp * _ncdf(np, zz) + sd * np.exp(-0.5 * zz * zz) / math.sqrt(2 * math.pi)
        order = np.argsort(-ei)
        picked = None
        for i in order[:50]:
            P = _from_unit(cand[i], space, base)
            if json.dumps(P, sort_keys=True) not in seen:
                picked = (P, float(ei[i]))
                break
        if not picked:
            break
        quiet = quiet + 1 if picked[1] < TUNE_EI_STOP else 0
        if quiet >= 3 and len(y) >= n0 + len(starts) + 8:
            break   # nothing left that's expected to help
        run(picked[0])
    i = int(np.argmin(y))
    why = 'out of time' if not finished else 'tried the most it may' if len(y) >= max_evals else 'stopped: nothing left to gain'
    log(f'tuning ({tag}): {len(y)} settings tried, best {Ps[i]} ({y[i]:.5f}), {why}')
    return Ps[i], y[i], len(y), finished


def blend_loss(rows, lams, w):
    """Average 1X2 + over/under log loss after blending model and market as the engine does."""
    n = l1 = l2 = 0.0
    for (lh, la, hg, ag, *_), (mh, ma) in zip(rows, lams):
        bh, ba = math.exp(w * math.log(mh) + (1 - w) * math.log(lh)), math.exp(w * math.log(ma) + (1 - w) * math.log(la))
        x, y = game_loss(bh, ba, hg, ag)
        n += 1
        l1 += x
        l2 += y
    return (l1 / n, l2 / n) if n else (None, None)


def run_tuning(state):
    global _TUNE_JOBS
    t0 = time.time()
    numpy_lib(True)   # before the worker processes are made, so they have it too
    c0, c1, c2, c3, c4 = (season_code(NOW, i) for i in (0, 1, 2, 3, 4))
    global _TUNE_LKS
    train, check, tl, cl = [], [], [], []
    n_now = 0
    for lk, c in L.items():
        if not c['fd']:
            continue
        s1, s2, s3, s4 = (fd_season(lk, code) for code in (c1, c2, c3, c4))
        if len(s2) >= 60 and s3:
            train.append((s2, s3)); tl.append(lk)
        if len(s3) >= 60 and s4:
            train.append((s3, s4)); tl.append(lk)
        if len(s1) >= 60 and s2:
            check.append((s1, s2)); cl.append(lk)
            # this season's games so far join the check, so every switch-on decision also answers to the most
            # recent football: a change that helped last season but hurts now stays off (drift)
            s0 = fd_season(lk, c0) if TUNE_NOW else []
            if len(s0) >= 60:
                check.append((s0, s1)); cl.append(lk)
                n_now += len(s0)
    if len(train) < 6 or len(check) < 3:
        log('tuning: not enough past seasons', len(train), len(check))
        return None
    _TUNE_JOBS = train + check
    _TUNE_LKS = tl + cl
    ti, ci = list(range(len(train))), list(range(len(train), len(train) + len(check)))
    pool = _pool()
    evals, cache = 0, {}
    try:
        def score(P):
            nonlocal evals
            key = json.dumps(P, sort_keys=True)
            if key not in cache:
                res = _pmap(pool, _tune_job, [(i, P, False) for i in ti])
                n = sum(r[0] for r in res)
                cache[key] = (sum(r[1] for r in res) + sum(r[2] for r in res)) / max(1, n)
                evals += 1
            return cache[key]

        best = dict(DEFAULT_MODEL)
        bs = score(best)
        finished = True
        np = numpy_lib()
        prev_t = (state or {}).get('tune') or {}
        # commentary xG: tried only once most of the covered leagues' training games have it
        xl = [i for i in ti if _TUNE_LKS[i] in XG_SLUGS]
        n_x = sum(1 for i in xl for rows in _TUNE_JOBS[i] for r in rows if len(r) > 9 and r[9])
        n_a = sum(len(rows) for i in xl for rows in _TUNE_JOBS[i])
        xg_cover = n_x / n_a if n_a else 0.0
        if np:
            seed = int(c1) * 7 + 1
            starts = [DEFAULT_MODEL] + ([dict(DEFAULT_MODEL, **{k: v for k, v in prev_t['params'].items() if k != 'sx'})] if prev_t.get('params') else [])
            best, bs, _, f1 = bayes_search(np, score, starts, TUNE_SPACE, DEFAULT_MODEL, t0, TUNE_BUDGET_S * 0.7, TUNE_MAX_EVALS, seed, 'shots and goals')
            sp0 = [s_ for s_ in TUNE_SPACE if s_[0] != 'sw']
            starts0 = [dict(best, sw=0.0), dict(DEFAULT_MODEL)] + ([dict(DEFAULT_MODEL, **dict(prev_t['params0'], sw=0.0))] if prev_t.get('params0') else [])
            best0, bs0, _, f2 = bayes_search(np, score, starts0, sp0, dict(best, sw=0.0), t0, TUNE_BUDGET_S, TUNE_MAX_EVALS // 2, seed + 1, 'goals only')
            finished = f1 and f2
        # commentary xG: the leagues it covers get their own settings, searched with its share on their games alone (so
        # the other leagues are untouched), and kept only if they also beat the common ones on the check season there
        xg = {'cover': round(xg_cover, 3), 'on': False}
        best_x = None
        xc = [i for i in ci if _TUNE_LKS[i] in XG_SLUGS]
        if np and xg_cover >= XG_MIN_COVER and xl and xc:
            def on_jobs(idx, P):
                res = _pmap(pool, _tune_job, [(i, P, False) for i in idx])
                n_ = sum(r[0] for r in res)
                return (sum(r[1] for r in res) + sum(r[2] for r in res)) / max(1, n_), n_
            sp_x = TUNE_SPACE + [('sx', 0.0, 0.9, False)]
            bx, sx_tr, _, _ = bayes_search(np, lambda P: on_jobs(xl, P)[0], [dict(best, sx=0.15), dict(best, sx=0.5)], sp_x, dict(best, sx=0.0),
                                           time.time(), TUNE_BUDGET_S * 0.25, TUNE_MAX_EVALS // 2, seed + 2, 'with commentary xG')
            l_tr0 = on_jobs(xl, best)[0]
            l_x, n_ck = on_jobs(xc, bx)
            l_0, _ = on_jobs(xc, best)
            on_x = bool(bx.get('sx')) and sx_tr < l_tr0 and l_x < l_0 - XG_MIN_GAIN
            xg.update({'sx': bx.get('sx'), 'on': on_x, 'params': bx, 'll': [round(l_0, 5), round(l_x, 5)], 'train': [round(l_tr0, 5), round(sx_tr, 5)],
                       'games': n_ck, 'leagues': sorted({_TUNE_LKS[i] for i in xc})})
            log(f"tuning: the {len(xg['leagues'])} leagues with commentary xG, own settings {bx}: model {l_0:.4f} -> {l_x:.4f} on the check season there "
                f"({n_ck} games; training {l_tr0:.4f} -> {sx_tr:.4f}); {'on' if on_x else 'kept off'}")
            if on_x:
                best_x = bx
        for _round in range(2 if not np else 0):
            moved = False
            for name in ('sw', 'hl', 'pw', 'k'):
                for v in TUNE_GRID[name]:
                    if time.time() - t0 > TUNE_BUDGET_S:
                        finished = False
                        break
                    P = dict(best, **{name: v})
                    sc = score(P)
                    if sc < bs - 1e-5:
                        best, bs, moved = P, sc, True
            if not moved or not finished:
                break
        # leagues without shot counts: the same search with goals only
        if not np:
            best0 = dict(best, sw=0.0)
            bs0 = score(best0)
        for name in (('k', 'hl', 'pw') if not np else ()):
            for v in TUNE_GRID[name]:
                if time.time() - t0 > TUNE_BUDGET_S * 1.25:
                    finished = False
                    break
                P = dict(best0, **{name: v})
                sc = score(P)
                if sc < bs0 - 1e-5:
                    best0, bs0 = P, sc
        t_search = time.time() - t0
        # the market's share: model expected goals from the tuned settings, market expected goals fitted by the engine
        flat = lambda res: ([r for x in res for r in x[0]], [j for x in res for j in x[1]], [k for x in res for k in x[2]])
        Pj = lambda i: best_x if best_x and _TUNE_LKS[i] in XG_SLUGS else best   # the settings each league uses
        tr_rows, tr_jobs, tr_keys = flat(_pmap(pool, _tune_job, [(i, Pj(i), True) for i in ti]))
        ck_def, cd_jobs, cd_keys = flat(_pmap(pool, _tune_job, [(i, DEFAULT_MODEL, True) for i in ci]))
        ck_new, cn_jobs, cn_keys = flat(_pmap(pool, _tune_job, [(i, Pj(i), True) for i in ci]))
        ck_0 = flat(_pmap(pool, _tune_job, [(i, best0, True) for i in ci]))[0]
    finally:
        if pool:
            pool.shutdown()
    lam_tr = run_node(NODE_FIT, [[r[4], r[5], r[6]] for r in tr_rows])
    lam_ck = run_node(NODE_FIT, [[r[4], r[5], r[6]] for r in ck_new])
    if not lam_tr or not lam_ck or len(ck_def) != len(ck_new):
        log('tuning: market fit failed')
        return None
    if ENV('STATPAD_TUNE_DUMP'):   # testing: keep the rows for experiments
        import pickle
        with open(ENV('STATPAD_TUNE_DUMP'), 'wb') as fh:
            pickle.dump({'tr': tr_rows, 'lam_tr': lam_tr, 'ck_new': ck_new, 'ck_def': ck_def, 'ck_0': ck_0, 'lam_ck': lam_ck,
                         'best': best, 'best0': best0, 'train_lks': _TUNE_LKS, 'c': [c1, c2, c3, c4]}, fh)
    w_scores = {w: sum(blend_loss(tr_rows, lam_tr, w)) for w in W_GRID}
    w_best = min(w_scores, key=w_scores.get)
    # the check season: never seen by the search
    m_def = blend_loss(ck_def, lam_ck, 0.0)
    m_new = blend_loss(ck_new, lam_ck, 0.0)
    b_old = blend_loss(ck_def, lam_ck, W_DEFAULT)
    b_p = blend_loss(ck_new, lam_ck, W_DEFAULT)
    p_on = best != DEFAULT_MODEL and sum(b_p) < sum(b_old) - 1e-4
    rows_on = ck_new if p_on else ck_def
    b_w = blend_loss(rows_on, lam_ck, w_best)
    b_base = b_p if p_on else b_old
    w_on = w_best != W_DEFAULT and sum(b_w) < sum(b_base) - 1e-4
    b_after = b_w if w_on else b_base
    mkt = blend_loss(ck_new, lam_ck, 1.0)
    m_0 = blend_loss(ck_0, lam_ck, 0.0) if len(ck_0) == len(ck_def) else (9, 9)
    on0 = p_on and best0 != dict(DEFAULT_MODEL) and sum(m_0) < sum(m_def) - 1e-4
    w_live = w_best if w_on else W_DEFAULT
    try:
        elo = fit_elo(tr_rows, rows_on, lam_ck, w_live)
    except Exception as e:
        log('tuning: Elo fit failed', repr(e))
        elo = None
    # the newer stages, each on top of the ones before it
    P_live = best if p_on else DEFAULT_MODEL
    mod_tr, mod_ck = elo_rows(tr_rows, elo), elo_rows(rows_on, elo)
    kal = rho = stack = None
    rho_tr, rho_ck = [RHO_DC] * len(tr_rows), [RHO_DC] * len(rows_on)
    try:
        kal, mod_tr, mod_ck = fit_kalman(ti, ci, P_live, tr_rows, rows_on, lam_ck, w_live, mod_tr, mod_ck, best_x if p_on else None)
    except Exception as e:
        log('tuning: game-by-game ratings failed', repr(e))
    try:
        rho, rt_, rc_ = fit_rho(tr_rows, rows_on, mod_tr, mod_ck, w_live)
        if rho and rho['on']:
            rho_tr, rho_ck = rt_, rc_
    except Exception as e:
        log('tuning: draw correction failed', repr(e))
    site_tr, site_ck = blend_lams(mod_tr, lam_tr, w_live), blend_lams(mod_ck, lam_ck, w_live)
    try:
        st_ = fit_stack(tr_rows, rows_on, mod_tr, mod_ck, rho_tr, rho_ck, w_live)
        if st_:
            stack, site_tr, site_ck = st_
    except Exception as e:
        log('tuning: learned blend failed', repr(e))
    fin = split_loss(site_ck, rows_on, rho_ck)
    fx = safe_fx(site_tr, tr_rows, tr_jobs, tr_keys, site_ck, rows_on, (cn_jobs, cn_keys) if p_on else (cd_jobs, cd_keys),
                 rho_tr, rho_ck, bool(stack and stack['on']), t0)
    r5 = lambda x: round(x, 5)
    out = {'at': iso(NOW), 'v': TUNE_V, 'train': [season_name(c3), season_name(c2)],
           'check': season_name(c1) + (f' and {season_name(c0)} so far' if n_now else ''), 'check_code': c1, 'check_now': n_now,
           'leagues': len(check), 'games': len(ck_new), 'evals': evals, 'secs': round(time.time() - t0),
           'search_secs': round(t_search), 'finished': finished, 'search': 'bayes' if numpy_lib() else 'grid',
           'default': DEFAULT_MODEL, 'params': best, 'on': p_on, 'w': w_best, 'w_on': w_on,
           'params0': best0, 'on0': on0, 'params_x': best_x, 'fx': fx, 'elo': elo, 'kal': kal, 'rho': rho, 'stack': stack, 'xg': xg,
           'll': {'model': [r5(m_def[0]), r5(m_new[0])], 'model_ou': [r5(m_def[1]), r5(m_new[1])], 'final': [r5(fin[0]), r5(fin[1])],
                  'site': [r5(b_old[0]), r5(b_after[0])], 'site_ou': [r5(b_old[1]), r5(b_after[1])],
                  'market': [r5(mkt[0]), r5(mkt[1])], 'model0': [r5(m_0[0]), r5(m_0[1])]}}
    log(f"tuning: {evals} settings tried in {out['search_secs']} s on {season_name(c3)}-{season_name(c2)}; best {best}, market share {w_best}. "
        f"Checked on {out['check']}: model {m_def[0]:.4f} -> {m_new[0]:.4f}, site {b_old[0]:.4f} -> {b_after[0]:.4f} (1X2 log loss); "
        f"settings {'on' if p_on else 'kept old'}, market share {'on' if w_on else 'kept 0.8'}; after the newer stages {fin[0]:.4f} (1X2), {fin[1]:.4f} (over/under)")
    return out


def split_loss(lams, rows, rhos):
    """(1X2, over/under 2.5) average log losses."""
    np = numpy_lib()
    if np:
        x, y = losses_np(np, [l[0] for l in lams], [l[1] for l in lams], [r[2] for r in rows], [r[3] for r in rows], np.array(rhos))
        return float(x.mean()), float(y.mean())
    L_ = [game_loss(l[0], l[1], r[2], r[3], rh) for l, r, rh in zip(lams, rows, rhos)]
    return sum(x for x, _ in L_) / len(L_), sum(y for _, y in L_) / len(L_)


def fit_elo(tr_rows, ck_rows, lam_ck, w):
    """The Elo mix: for each setting, the map from Elo gap to expected goals and the ratings' share, fitted on the
    training seasons by the goals' likelihood; the best is checked on the latest season (model alone, and after the
    market blend as the site does). On only if the model alone improves there and the site's numbers don't get worse."""
    def ll_goals(rows, gi, b, c, v):
        tot = 0.0
        for r in rows:
            dr = r[10][gi]
            if dr is None:
                continue
            el = elo_lams(dr, r[11][0], r[11][1], {'b': b, 'c': c})
            lh, la = elo_mix(r[0], r[1], el, v)
            tot += lh - r[2] * math.log(lh) + la - r[3] * math.log(la)
        return tot
    best = None
    for gi in range(len(ELO_GRID)):
        rows = [r for r in tr_rows if len(r) > 11 and r[10][gi] is not None]
        if len(rows) < 2000:
            continue
        bc = min(((ll_goals(rows, gi, b / 100, c / 100, 0.0), b / 100, c / 100) for b in range(30, 101, 5) for c in range(-8, 9, 4)))
        bv = min(((ll_goals(rows, gi, bc[1], bc[2], v / 10), v / 10) for v in range(3, 10)))
        if best is None or bv[0] < best[0]:
            best = (bv[0], gi, bc[1], bc[2], bv[1])
    if not best:
        return None
    _, gi, b, c, v = best
    E = {'hfa': ELO_GRID[gi][0], 'k': ELO_GRID[gi][1], 'b': b, 'c': c, 'v': v}

    def check(wm):
        n = l0 = l1 = 0.0
        for r, (mh, ma) in zip(ck_rows, lam_ck):
            if len(r) <= 11 or r[10][gi] is None:
                continue
            lh, la = elo_mix(r[0], r[1], elo_lams(r[10][gi], r[11][0], r[11][1], E), v)
            for k, (xh, xa) in enumerate(((r[0], r[1]), (lh, la))):
                bh, ba = math.exp(wm * math.log(mh) + (1 - wm) * math.log(xh)), math.exp(wm * math.log(ma) + (1 - wm) * math.log(xa))
                x = sum(game_loss(bh, ba, r[2], r[3]))
                if k:
                    l1 += x
                else:
                    l0 += x
            n += 1
        return (l0 / n, l1 / n) if n else (None, None)
    m0, m1 = check(0.0)
    s0, s1 = check(w)
    if m0 is None:
        return None
    E['on'] = m1 < m0 - 1e-3 and s1 <= s0 + 1e-4
    E['ll'] = {'model': [round(m0, 5), round(m1, 5)], 'site': [round(s0, 5), round(s1, 5)]}
    log(f"tuning: Elo mix (home advantage {E['hfa']:.0f}, K {E['k']:.0f}, ratings' share {v}): model {m0:.4f} -> {m1:.4f}, "
        f"site {s0:.4f} -> {s1:.4f} on the check season; {'on' if E['on'] else 'kept off'}")
    return E


# ----------------------------------------------------------------------------------------------
# Three newer stages of the monthly tuning, run after the settings, the market's share and Elo. Each is fitted on
# the training seasons and switched on only if it also lowers the error on the check season, which it never saw:
#  1. game-by-game ratings (kalman_run) mixed with the model's;
#  2. the draw correction per league (the engine used one fixed value for every league);
#  3. the learned blend: how far to lean from the bookmakers' numbers towards (or away from) the model's, depending
#     on how thin the model's data is, the time of season, the kind of league and short rest. Fitted on the error
#     itself (1X2 and over/under 2.5 log loss), with its strength chosen walk-forward: fitted on the older training
#     season, measured on the newer one.
# ----------------------------------------------------------------------------------------------
KAL_MIN_GAIN = 1e-3     # model alone, 1X2 + over/under log loss a game, as for Elo
RHO_MIN_GAIN = 1e-4     # Statpad's numbers, a game
STACK_MIN_GAIN = 1e-4
RHO_GRID = [round(-0.20 + 0.01 * i, 2) for i in range(24)]   # -0.20 .. +0.03
RHO_TAU = 1000          # a league's own games weigh against this many games of the pooled value
STACK_RIDGES = (0.003, 0.01, 0.03, 0.1)
STACK_NAMES = ['model', 'model x thin data', 'model x early season', 'model x second tier', 'model x other leagues', 'short rest', 'goals level']
STACK = None            # {'b': [...]} when the learned blend is on
KAL_SHARES = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


def elo_rows(rows, E):
    """The model's expected goals for tuning rows after the Elo mix, when it's on."""
    if not E or not E.get('on') or (E['hfa'], E['k']) not in ELO_GRID:
        return [(r[0], r[1]) for r in rows]
    gi = ELO_GRID.index((E['hfa'], E['k']))
    return [elo_mix(r[0], r[1], elo_lams(r[10][gi], r[11][0], r[11][1], E), E['v']) if r[10][gi] is not None else (r[0], r[1]) for r in rows]


def _kal_job(i, K, sw, nt, sx=0.0):
    cur_rows, prev_rows = _TUNE_JOBS[i]
    lk = _TUNE_LKS[i] if i < len(_TUNE_LKS) else None
    games = [(r[0], r[1], r[2], r[3], r[4], False, r[5], r[6], r[9] if len(r) > 9 else None) for r in prev_rows] + \
            [(r[0], r[1], r[2], r[3], r[4], True, r[5], r[6], r[9] if len(r) > 9 else None) for r in cur_rows]
    pre = kalman_run(games, K, sw, nt, sx)[0]
    return {(lk, d.strftime('%Y-%m-%d'), h, a): v for (d, h, a), v in pre.items()}


def mean_loss(lams, rows, rhos=None):
    """Average 1X2 + over/under 2.5 log loss of expected goals [(home, away)] against the rows' results."""
    np = numpy_lib()
    if not lams:
        return None
    if np:
        x, y = losses_np(np, [l[0] for l in lams], [l[1] for l in lams], [r[2] for r in rows], [r[3] for r in rows], None if rhos is None else np.array(rhos))
        return float((x + y).mean())
    return sum(sum(game_loss(l[0], l[1], r[2], r[3], None if rhos is None else rhos[k])) for k, (l, r) in enumerate(zip(lams, rows))) / len(rows)


def blend_lams(mod, lam_mkt, w):
    return [(math.exp(w * math.log(mh) + (1 - w) * math.log(lh)), math.exp(w * math.log(ma) + (1 - w) * math.log(la))) for (lh, la), (mh, ma) in zip(mod, lam_mkt)]


def fit_kalman(ti, ci, P, tr_rows, ck_rows, lam_ck, w, mod_tr, mod_ck, P_x=None):
    """Fit the filter's settings by how well it predicts each next game's goals on the training seasons, then the
    share it gets next to the model by the error on them. mod_tr / mod_ck: the model's numbers so far (after Elo)."""
    t0 = time.time()
    keys_t = [r[9] for r in tr_rows]

    def preds(idx, K):
        out = {}
        for i in idx:
            Q = P_x if P_x and _TUNE_LKS[i] in XG_SLUGS else P
            out.update(_kal_job(i, K, Q.get('sw') or 0.0, Q.get('nt', NEW_TEAM_PRIOR[1]), Q.get('sx') or 0.0))
        return out

    def score(K):
        kp = preds(ti, K)
        tot = n = 0
        for k, r in zip(keys_t, tr_rows):
            v = kp.get(k)
            if v:
                tot += v[0] - r[2] * math.log(v[0]) + v[1] - r[3] * math.log(v[1])
                n += 1
        return tot / max(1, n)
    np = numpy_lib()
    if np:
        K, _, evals, _ = bayes_search(np, score, [KAL_DEF], KAL_SPACE, KAL_DEF, t0, 90, 40, 11, 'game-by-game ratings')
    else:
        cands = [dict(KAL_DEF, q=q, p0=p0, ps=ps) for q in (0.00005, 0.0001, 0.0002) for p0 in (0.05, 0.1) for ps in (0.8, 1.0)]
        sc = [score(K_) for K_ in cands]
        K, evals = cands[sc.index(min(sc))], len(cands)
    kt, kc = preds(ti, K), preds(ci, K)

    def mixed(mod, rows, kp, v):
        out = []
        for (lh, la), r in zip(mod, rows):
            x = kp.get(r[9])
            out.append(elo_mix(lh, la, x[:2], v) if x else (lh, la))
        return out
    tr_l = {v: mean_loss(mixed(mod_tr, tr_rows, kt, v), tr_rows) for v in KAL_SHARES}
    v = min(tr_l, key=tr_l.get)
    m0, m1 = mean_loss(mod_ck, ck_rows), mean_loss(mixed(mod_ck, ck_rows, kc, v), ck_rows)
    s0, s1 = mean_loss(blend_lams(mod_ck, lam_ck, w), ck_rows), mean_loss(blend_lams(mixed(mod_ck, ck_rows, kc, v), lam_ck, w), ck_rows)
    on = v < 1.0 and m1 < m0 - KAL_MIN_GAIN and s1 <= s0 + 1e-4
    out = dict(K, v=v, on=on, evals=evals, ll={'model': [round(m0, 5), round(m1, 5)], 'site': [round(s0, 5), round(s1, 5)]})
    log(f"tuning: game-by-game ratings {K}, model's share {v}: model {m0:.4f} -> {m1:.4f}, site {s0:.4f} -> {s1:.4f} on the check season; "
        f"{'on' if on else 'kept off'} ({time.time() - t0:.0f} s)")
    return out, (mixed(mod_tr, tr_rows, kt, v) if on else mod_tr), (mixed(mod_ck, ck_rows, kc, v) if on else mod_ck)


def _mkt_arrays(np, rows):
    return np.array([r[4] for r in rows]), np.array([r[5] for r in rows]), [r[6] for r in rows]


def fit_rho(tr_rows, ck_rows, mod_tr, mod_ck, w):
    """The draw correction per league: for each value, the bookmakers' expected goals are refitted on the engine's
    grid and blended with the model's as the site does; each league takes the value with the lowest error on the
    training seasons, pulled towards the value that's best over all leagues when it has few games."""
    np = numpy_lib()
    if not np or not tr_rows or not ck_rows:
        return None, None, None
    t0 = time.time()
    hg_t, ag_t = np.array([r[2] for r in tr_rows]), np.array([r[3] for r in tr_rows])
    hg_c, ag_c = np.array([r[2] for r in ck_rows]), np.array([r[3] for r in ck_rows])
    lk_t, lk_c = [r[9][0] for r in tr_rows], [r[9][0] for r in ck_rows]
    mt, mc = np.array(mod_tr), np.array(mod_ck)
    Tm, Cm = _mkt_arrays(np, tr_rows), _mkt_arrays(np, ck_rows)
    LT, LC, st_t, st_c = {}, {}, None, None
    for rho in sorted(RHO_GRID, key=lambda x: abs(x - RHO_DC)):   # nearest first, each fit starting from the last
        fh, fa = fit_market_np(np, *Tm, rho, start=st_t)
        st_t = (fh, fa)
        bh, ba = np.exp(w * np.log(fh) + (1 - w) * np.log(mt[:, 0])), np.exp(w * np.log(fa) + (1 - w) * np.log(mt[:, 1]))
        x, y = losses_np(np, bh, ba, hg_t, ag_t, rho)
        LT[rho] = x + y
        fh, fa = fit_market_np(np, *Cm, rho, start=st_c)
        st_c = (fh, fa)
        bh, ba = np.exp(w * np.log(fh) + (1 - w) * np.log(mc[:, 0])), np.exp(w * np.log(fa) + (1 - w) * np.log(mc[:, 1]))
        x, y = losses_np(np, bh, ba, hg_c, ag_c, rho)
        LC[rho] = x + y
    pooled = min(RHO_GRID, key=lambda r: float(LT[r].mean()))
    lks = sorted(set(lk_t))
    per = {}
    for lk in lks:
        m = np.array([x == lk for x in lk_t])
        per[lk] = min(RHO_GRID, key=lambda r: float(LT[r][m].sum()) + RHO_TAU * float(LT[r].mean()))
    rc = [per.get(lk, pooled) for lk in lk_c]
    base = float(LC[RHO_DC].mean()) if RHO_DC in LC else None
    new = float(np.array([LC[r][i] for i, r in enumerate(rc)]).mean())
    on = base is not None and new < base - RHO_MIN_GAIN
    # what it does to both teams to score and exact scores (not in the switch-on test: the record's reading only)
    fh, fa = fit_market_np(np, *Cm, np.array(rc))
    bh, ba = np.exp(w * np.log(fh) + (1 - w) * np.log(mc[:, 0])), np.exp(w * np.log(fa) + (1 - w) * np.log(mc[:, 1]))
    f0h, f0a = fit_market_np(np, *Cm, RHO_DC)
    b0h, b0a = np.exp(w * np.log(f0h) + (1 - w) * np.log(mc[:, 0])), np.exp(w * np.log(f0a) + (1 - w) * np.log(mc[:, 1]))
    def side(G, hg, ag):
        n = G.shape[1]
        B = G[:, 1:, 1:].sum((1, 2))
        bt = -np.log(np.maximum(1e-9, np.where((hg > 0) & (ag > 0), B, 1 - B))).mean()
        cs = -np.log(np.maximum(1e-12, G[np.arange(len(hg)), np.minimum(hg, n - 1), np.minimum(ag, n - 1)])).mean()
        return float(bt), float(cs)
    bt0, cs0 = side(dc_grid_np(np, b0h, b0a, RHO_DC), hg_c, ag_c)
    bt1, cs1 = side(dc_grid_np(np, bh, ba, np.array(rc)), hg_c, ag_c)
    out = {'on': on, 'pooled': pooled, 'lk': per, 'll': [round(base, 5), round(new, 5)], 'btts': [round(bt0, 5), round(bt1, 5)],
           'cs': [round(cs0, 5), round(cs1, 5)], 'train': [round(float(LT[RHO_DC].mean()), 5), round(float(LT[pooled].mean()), 5)]}
    log(f"tuning: draw correction, best over all leagues {pooled:+.2f} (was {RHO_DC:+.2f}), per league {per}: site {base:.4f} -> {new:.4f} "
        f"on the check season (both teams to score {bt0:.4f} -> {bt1:.4f}, exact score {cs0:.4f} -> {cs1:.4f}); {'on' if on else 'kept off'} ({time.time() - t0:.0f} s)")
    rt = [per.get(lk, pooled) for lk in lk_t] if on else [RHO_DC] * len(tr_rows)
    return out, rt, (rc if on else [RHO_DC] * len(ck_rows))


def tired4(rest):
    return tired(rest, 4)


def stack_coefs(b, se, week, group, rest_own, rest_opp):
    """The learned blend for one side of one game: (weight on log(model / market), shift in log expected goals)."""
    th = (se - 0.2) * 5 if se is not None else 0.0
    beta = b[0] + b[1] * th + b[2] * (1.0 if week is not None and week < 8 else 0.0) + b[3] * (group == 'second') + b[4] * (group == 'more')
    return beta, b[5] * (tired4(rest_own) - tired4(rest_opp)) + b[6]


def _stack_data(np, rows, mod, rhos):
    fh, fa = fit_market_np(np, *_mkt_arrays(np, rows), np.array(rhos))
    md = np.array(mod)
    oh, oa = np.log(fh), np.log(fa)
    dh, da = np.log(md[:, 0]) - oh, np.log(md[:, 1]) - oa
    seh = np.array([r[12][0] if r[12][0] is not None else 0.2 for r in rows])
    sea = np.array([r[12][1] if r[12][1] is not None else 0.2 for r in rows])
    early = np.array([1.0 if r[14] < 8 else 0.0 for r in rows])
    sec = np.array([1.0 if L[r[9][0]]['group'] == 'second' else 0.0 for r in rows])
    mor = np.array([1.0 if L[r[9][0]]['group'] == 'more' else 0.0 for r in rows])
    rest = np.array([tired4(r[7]) - tired4(r[8]) for r in rows])
    one = np.ones(len(rows))
    Zh = np.stack([dh, dh * (seh - 0.2) * 5, dh * early, dh * sec, dh * mor, rest, one], 1)
    Za = np.stack([da, da * (sea - 0.2) * 5, da * early, da * sec, da * mor, -rest, one], 1)
    return {'oh': oh, 'oa': oa, 'Zh': Zh, 'Za': Za, 'hg': np.array([r[2] for r in rows]), 'ag': np.array([r[3] for r in rows]), 'rho': np.array(rhos)}


def _stack_eval(np, F, b, idx=None):
    sl = slice(None) if idx is None else idx
    lh, la = np.exp(F['oh'][sl] + F['Zh'][sl] @ b), np.exp(F['oa'][sl] + F['Za'][sl] @ b)
    x, y = losses_np(np, lh, la, F['hg'][sl], F['ag'][sl], F['rho'][sl])
    return lh, la, x + y


def _stack_fit(np, F, idx, ridge, b0):
    """BFGS on the mean log loss plus ridge * |b - b0|^2. The gradient comes from each game's sensitivity to its own
    two expected goals, so it costs three grid evaluations whatever the number of weights."""
    Zh, Za = F['Zh'][idx], F['Za'][idx]
    hg, ag, rh = F['hg'][idx], F['ag'][idx], F['rho'][idx]
    n = len(hg)

    def fg(b):
        lh, la, l0 = _stack_eval(np, F, b, idx)
        e = 1e-5
        x, y = losses_np(np, lh * math.exp(e), la, hg, ag, rh)
        gh = (x + y - l0) / e
        x, y = losses_np(np, lh, la * math.exp(e), hg, ag, rh)
        ga = (x + y - l0) / e
        d = b - b0
        return float(l0.mean()) + ridge * float(d @ d), (Zh.T @ gh + Za.T @ ga) / n + 2 * ridge * d
    b = b0.copy()
    f, g = fg(b)
    Hi = np.eye(len(b))
    for _ in range(80):
        p = -Hi @ g
        t = 1.0
        while True:
            fn, gn = fg(b + t * p)
            if fn <= f + 1e-4 * t * float(g @ p) or t < 1e-4:
                break
            t *= 0.5
        s_, yv = t * p, gn - g
        b, f_old, f, g = b + s_, f, fn, gn
        sy = float(s_ @ yv)
        if sy > 1e-12:
            r_ = 1 / sy
            I = np.eye(len(b))
            Hi = (I - r_ * np.outer(s_, yv)) @ Hi @ (I - r_ * np.outer(yv, s_)) + r_ * np.outer(s_, s_)
        if abs(f_old - f) < 1e-9 or float(np.abs(g).max()) < 1e-7:
            break
    return b


def fit_stack(tr_rows, ck_rows, mod_tr, mod_ck, rho_tr, rho_ck, w):
    np = numpy_lib()
    if not np or len(tr_rows) < 3000 or not ck_rows:
        return None
    t0 = time.time()
    Ft, Fc = _stack_data(np, tr_rows, mod_tr, rho_tr), _stack_data(np, ck_rows, mod_ck, rho_ck)
    b0 = np.zeros(len(STACK_NAMES))
    b0[0] = 1 - w   # the plain blend: the starting point, and what the ridge pulls towards
    seas = np.array([season_code(datetime.strptime(r[9][1], '%Y-%m-%d')) for r in tr_rows])
    older, newer = np.nonzero(seas == min(seas))[0], np.nonzero(seas != min(seas))[0]
    wf = {}
    if len(older) > 1500 and len(newer) > 1500:   # walk forward: fitted on the older season, measured on the newer one
        for rg in STACK_RIDGES:
            b = _stack_fit(np, Ft, older, rg, b0)
            wf[rg] = float(_stack_eval(np, Ft, b, newer)[2].mean())
        ridge = min(wf, key=wf.get)
    else:
        ridge = 0.01
    b = _stack_fit(np, Ft, np.arange(len(tr_rows)), ridge, b0)
    base = float(_stack_eval(np, Fc, b0)[2].mean())
    new = float(_stack_eval(np, Fc, b)[2].mean())
    tr0, tr1 = float(_stack_eval(np, Ft, b0)[2].mean()), float(_stack_eval(np, Ft, b)[2].mean())
    on = new < base - STACK_MIN_GAIN
    out = {'on': on, 'b': [round(float(x), 4) for x in b], 'names': STACK_NAMES, 'ridge': ridge, 'wf': {str(k): round(v, 5) for k, v in wf.items()},
           'll': [round(base, 5), round(new, 5)], 'train': [round(tr0, 5), round(tr1, 5)]}
    log(f"tuning: learned blend {dict(zip(STACK_NAMES, out['b']))} (strength {ridge}, chosen walk-forward {out['wf']}): site {base:.4f} -> {new:.4f} "
        f"on the check season; {'on' if on else 'kept off'} ({time.time() - t0:.0f} s)")
    lh, la, _ = _stack_eval(np, Fc, b if on else b0)
    lh_t, la_t, _ = _stack_eval(np, Ft, b if on else b0)
    return out, list(zip(lh_t.tolist(), la_t.tolist())), list(zip(lh.tolist(), la.tolist()))


def apply_tuning(state):
    global MODEL, MODEL0, MODEL_X, MODEL_W, ELO, KAL, RHO_L, STACK
    t = state.get('tune') or {}
    e = t.get('elo') or {}
    ELO = {k: e[k] for k in ('hfa', 'k', 'b', 'c', 'v')} if e.get('on') and all(k in e for k in ('hfa', 'k', 'b', 'c', 'v')) else None
    MODEL = dict(t['params']) if t.get('on') and t.get('params') else dict(DEFAULT_MODEL)
    MODEL0 = dict(t['params0']) if t.get('on0') and t.get('params0') else dict(DEFAULT_MODEL)
    MODEL_X = dict(t['params_x']) if t.get('on') and t.get('params_x') and (t.get('xg') or {}).get('on') else None
    MODEL_W = t.get('w') if t.get('w_on') else None
    f = t.get('fx') or {}
    FX['rest'] = tuple(f['rest']['p']) if (f.get('rest') or {}).get('on') else None
    FX['dead'] = tuple(f['dead']['p']) if (f.get('dead') or {}).get('on') else None
    FX['open'] = tuple(f['open']['p']) if (f.get('open') or {}).get('on') else None
    k = t.get('kal') or {}
    KAL = {x: k[x] for x in ('q', 'p0', 'ps', 'qs', 'v')} if k.get('on') and all(x in k for x in ('q', 'p0', 'ps', 'qs', 'v')) else None
    r = t.get('rho') or {}
    RHO_L = dict(r.get('lk') or {}, _=r['pooled']) if r.get('on') and r.get('pooled') is not None else None
    sk = t.get('stack') or {}
    STACK = {'b': list(sk['b'])} if sk.get('on') and len(sk.get('b') or []) == len(STACK_NAMES) else None
    if STACK:
        FX['rest'] = None   # short rest is part of the learned blend


def safe_fx(*a):
    try:
        return fit_fx(*a)
    except Exception as e:
        log('tuning: fatigue and motivation fit failed', repr(e))
        return None


def fit_fx(site_tr, tr_rows, tr_jobs, tr_keys, site_ck, ck_rows, ck_sj, rho_tr, rho_ck, skip_rest, t_start):
    """Fit fatigue and motivation on the training seasons, on top of Statpad's own numbers (site_*: expected goals
    after the blend with the market and the newer stages); each goes on only if it also lowers the error on the
    check season. With the learned blend on, short rest is part of it and isn't fitted again here."""
    t0 = time.time()
    late = time.time() - t_start > TUNE_BUDGET_S * 1.4   # the run is long already: motivation waits for next month
    st_tr = {} if late else run_past_stakes(tr_jobs, tr_keys) or {}
    st_ck = {} if late or not st_tr else run_past_stakes(*ck_sj) or {}

    def pre(rows, lams, st, rhos):
        out = []
        for row, (bh, ba), rho in zip(rows, lams, rhos):
            hg, ag = row[2], row[3]
            rh, ra, key = row[7], row[8], row[9]
            dh, da = st.get(key, (None, None))
            out.append((bh, ba, hg, ag, rh, ra, dh, da, sum(game_loss(bh, ba, hg, ag, rho)), rho))
        return out
    P_tr, P_ck = pre(tr_rows, site_tr, st_tr, rho_tr), pre(ck_rows, site_ck, st_ck, rho_ck)

    def loss(P, F):
        tot, n_aff = 0.0, 0
        for bh, ba, hg, ag, rh, ra, dh, da, base, rho in P:
            xh, xa = fx_shift(rh, ra, dh, da, F)
            if xh or xa:
                tot += sum(game_loss(bh * math.exp(xh), ba * math.exp(xa), hg, ag, rho))
                n_aff += 1
            else:
                tot += base
        return tot / max(1, len(P)), n_aff

    out, F = {'games': [len(P_tr), len(P_ck)], 'measured': [len(st_tr), len(st_ck)]}, {}
    for name, grid in (('rest', FX_REST_GRID), ('dead', FX_DEAD_GRID), ('open', FX_OPEN_GRID)):
        if name != 'rest' and not st_tr:
            continue
        if name == 'rest' and skip_rest:
            out['rest'] = {'p': None, 'on': False, 'in_blend': True}
            continue
        base_tr, base_ck = loss(P_tr, F)[0], loss(P_ck, F)[0]
        best, best_l = None, base_tr
        for p in grid:
            l_ = loss(P_tr, dict(F, **{name: p}))[0]
            if l_ < best_l - 1e-7:
                best, best_l = p, l_
        if not best:
            out[name] = {'p': None, 'on': False, 'll': [round(base_ck, 5), round(base_ck, 5)], 'n': 0}
            continue
        ck_l, n_aff = loss(P_ck, dict(F, **{name: best}))
        on = ck_l < base_ck - FX_MIN_GAIN
        out[name] = {'p': list(best), 'on': on, 'll': [round(base_ck, 5), round(ck_l, 5)], 'n': n_aff,
                     'train': [round(base_tr, 5), round(best_l, 5)]}
        if on:
            F[name] = best
    log(f"tuning: rest {out.get('rest')}, motivation {out.get('dead')}, open games {out.get('open')} ({time.time() - t0:.0f} s)")
    return out


def tune_due(state):
    t = state.get('tune')
    if not t:
        return due(state, 'tune', 6)   # first time: retry every 6 hours if the downloads fail
    return due(state, 'tune', TUNE_EVERY_D * 24) or t.get('check_code') != season_code(NOW, 1) or not t.get('finished', True) \
        or ('fx' not in t and due(state, 'tune', 6)) \
        or (t.get('v', 1) < TUNE_V and due(state, 'tune', 6))   # the way prices are read changed: tune again once


def public_tune(state):
    t = state.get('tune')
    if not t:
        return None
    return {k: t[k] for k in ('at', 'train', 'check', 'leagues', 'games', 'default', 'params', 'on', 'params0', 'on0', 'w', 'w_on', 'll', 'fx', 'elo',
                              'kal', 'rho', 'stack', 'xg', 'params_x', 'evals', 'search_secs', 'search', 'check_now') if k in t}


# ----------------------------------------------------------------------------------------------
# Replay: last season through today's engine
# ----------------------------------------------------------------------------------------------
def load_bt_season():
    code, prev = season_code(NOW, 1), season_code(NOW, 2)
    out = {}
    for lk, c in L.items():
        if c['fd']:
            cur = fd_season(lk, code)
            if cur:
                out[lk] = (cur, fd_season(lk, prev))
    return out, season_name(code)


def bt_matches(seasons):
    """Engine input for every replayed game: the model as it stood that week, plus the pre-match prices
    (and fatigue and motivation, when the tuning switched them on)."""
    ms, res, pend, jobs, keys = [], {}, [], [], []
    use_fx = bool(FX['rest'] or FX['dead'] or FX['open'])
    for lk, (cur_rows, prev_rows) in seasons.items():
        games = [(r[0], r[1], r[2], r[3], r[4], False, r[5], r[6], r[9] if len(r) > 9 else None) for r in prev_rows] + \
                [(r[0], r[1], r[2], r[3], r[4], True, r[5], r[6], r[9] if len(r) > 9 else None) for r in cur_rows]
        P = model_for(lk, games, True)
        sf = season_fits(cur_rows, prev_rows, P)
        rd = rest_days(cur_rows, prev_rows) if use_fx or STACK else {}
        kp = {}
        if KAL:
            kp = kalman_run(games, KAL, P.get('sw') or 0.0, P.get('nt', NEW_TEAM_PRIOR[1]), P.get('sx') or 0.0)[0]
        start = sf[1] if sf else None
        if FX['dead'] or FX['open']:
            j, k = past_stakes_jobs(lk, sf)
            jobs += j
            keys += k
        eg = elo_run(list(prev_rows) + list(cur_rows), ELO['hfa'], ELO['k'])[1] if ELO else {}
        gm = goal_means(prev_rows or cur_rows) if ELO else None
        for r, lh, la, sh, sa in season_preds(cur_rows, prev_rows, P, sf=sf):
            d, h, a, hg, ag, _hs, _as, mkt, ko = r[:9]
            if ELO and (d, h, a) in eg:
                lh, la = elo_mix(lh, la, elo_lams(eg[(d, h, a)], gm[0], gm[1], ELO), ELO['v'])
            if KAL and (d, h, a) in kp:
                lh, la = elo_mix(lh, la, kp[(d, h, a)][:2], KAL['v'])
            if not mkt:
                continue
            mid = f"{lk}-{ko:%Y%m%d}-{slug(h)}-{slug(a)}"
            m = {'id': mid, 'lk': lk, 'ko': iso(ko), 'h': h, 'a': a, 'lm': [round(lh, 4), round(la, 4)],
                 'ls': [round(sh, 3), round(sa, 3)], 'mkt': mkt, 'xl': False, 'fr': False, 'clash': False}
            if MODEL_W:
                m['wm'] = MODEL_W
            if RHO_L:
                m['rho'] = rho_for(lk)
            if stack_ok(m):
                rh_, ra_ = rd.get((d, h, a), (None, None))
                wk = (d - start).days // 7 if start else None
                bh_, ch_ = stack_coefs(STACK['b'], sh, wk, L[lk]['group'], rh_, ra_)
                ba_, ca_ = stack_coefs(STACK['b'], sa, wk, L[lk]['group'], ra_, rh_)
                m['st'] = [round(bh_, 4), round(ch_, 4), round(ba_, 4), round(ca_, 4)]
                m.pop('wm', None)
            if use_fx:
                pend.append((m, rd.get((d, h, a), (None, None)), (lk, d.strftime('%Y-%m-%d'), h, a)))
            ms.append(m)
            res[mid] = (hg, ag)
    st = (run_past_stakes(jobs, keys) or {}) if jobs else {}
    for m, (rh, ra), key in pend:
        dh, da = st.get(key, (None, None))
        xh, xa = fx_shift(rh, ra, dh, da)
        if abs(xh) > 1e-4 or abs(xa) > 1e-4:
            m['fx'] = [round(math.exp(xh), 4), round(math.exp(xa), 4)]
    return ms, res


VH_SEASONS = 5           # past seasons for the value-bet history


def value_history():
    """Every value bet the Value board's rule would have flagged on the last five full seasons of football-data's
    main leagues: the best listed price at least 2% above the consensus's fair price (margin taken off, Pinnacle
    weighted three times), chance 18% or more, on home, draw, away and over/under 2.5. 1 unit each, in kick-off
    order, with closing-line value against the closing consensus. Needs no model, so it covers every game."""
    numpy_lib(True)   # heavy fitting ahead: fast maths if it can be had
    t0 = time.time()
    bets, seasons = [], []
    for k in range(1, VH_SEASONS + 1):
        code = season_code(NOW, k)
        n0 = len(bets)
        for lk, c in L.items():
            if not c['fd']:
                continue
            for d, h, a, hg, ag, _hs, _as, mk, ko in (r[:9] for r in fd_season(lk, code)):
                if not mk:
                    continue
                cand = [(key, mk['p'][i], mk['best'][i]) for i, key in enumerate(('H', 'D', 'A'))]
                ou = mk.get('ou')
                if ou and ou.get('pO') and ou.get('best'):
                    cand += [('O25', ou['pO'], ou['best'][0]), ('U25', 1 - ou['pO'], ou['best'][1])]
                cl = mk.get('cl') or {}
                for key, p, pr in cand:
                    if p >= 0.18 and pr and pr > 1 and p * pr - 1 >= VB_MIN:
                        bets.append((ko or d, code, key, p * pr - 1, pr, bool(settle(key, hg, ag)), cl.get(key)))
        if len(bets) > n0:
            seasons.append(code)
    if len(bets) < 200:
        log('value history: not enough games', len(bets))
        return None
    bets.sort(key=lambda b: b[0])
    allv, bands, by_s, by_k, curve = new_vb(), {}, {}, {}, [0.0]
    for ko, code, key, edge, pr, won, clp in bets:
        clv = round(pr * clp - 1, 4) if clp else None
        vb_add(allv, won, pr, clv)
        vb_add(bands.setdefault(vb_band(edge), new_vb()), won, pr, clv)
        vb_add(by_s.setdefault(season_name(code), new_vb()), won, pr, clv)
        vb_add(by_k.setdefault('1X2' if key in ('H', 'D', 'A') else 'Goals', new_vb()), won, pr, clv)
        curve.append(allv['u'])
    log(f'value history: {len(bets)} bets on {len(seasons)} seasons, {allv["u"] / allv["n"]:+.3f} a unit ({time.time() - t0:.0f} s)')
    return {'at': iso(NOW), 'seasons': [season_name(c) for c in sorted(seasons)], 'min': VB_MIN, 'all': allv, 'bands': bands,
            'by_season': by_s, 'by_kind': by_k, 'curve': thin_curve(curve), 'leagues': sum(1 for c in L.values() if c['fd'])}


def run_backtest(state):
    numpy_lib(True)   # heavy fitting ahead: fast maths if it can be had
    t0 = time.time()
    seasons, label = load_bt_season()
    ms, res = bt_matches(seasons)
    t1 = time.time()
    if len(ms) < 200:
        log('backtest: not enough games with prices', len(ms))
        return None
    eng = run_node(NODE_BT, {'target': TRACK_TARGET, 'matches': ms, 'mcal': public_mcal(state), 'vb_min': VB_MIN})
    if not eng:
        return None
    agg = new_agg()
    agg['value'] = [0, 0, 0.0]                 # bets, won, units at the best price
    agg['tiers_old'] = {t: [0, 0, 0.0] for t in ('A', 'B', 'C', '-')}   # tiers from the stated chance alone
    by_lg = {}
    for mid, e in eng['picks'].items():
        hs, as_ = res[mid]
        oc = 0 if hs > as_ else 1 if hs == as_ else 2
        lk = mid.split('-')[0]
        if e['k']:
            won = bool(settle(e['k'], hs, as_))
            t = (e['t'] if 't' in e else tier_of(e['pp'])) or '-'
            tt = agg['tiers'][t]
            tt[0] += 1; tt[1] += int(won); tt[2] = round(tt[2] + e['pp'], 4)
            if t != '-':
                bl = by_lg.setdefault(lk, [0, 0, 0.0])
                bl[0] += 1; bl[1] += int(won); bl[2] = round(bl[2] + e['pp'], 4)
        if e.get('ok'):
            to = tier_of(e['op']) or '-'
            tt = agg['tiers_old'][to]
            tt[0] += 1; tt[1] += int(bool(settle(e['ok'], hs, as_))); tt[2] = round(tt[2] + e['op'], 4)
        agg['n'] += 1
        for i in range(3):
            b = agg['cal'][min(9, int(e['p'][i] * 10))]
            b[0] += 1; b[1] = round(b[1] + e['p'][i], 4); b[2] += int(i == oc)
        if e.get('m') and e.get('pm'):
            ll = agg['ll']
            ll[0] += 1
            ll[1] = round(ll[1] - math.log(max(1e-6, e['p'][oc])), 4)
            ll[2] = round(ll[2] - math.log(max(1e-6, e['m'][oc])), 4)
            ll[3] = round(ll[3] - math.log(max(1e-6, e['pm'][oc])), 4)
    # value bets: 1 unit on everything the Value board would have shown, at its best price, in kick-off order;
    # closing-line value against the closing consensus (football-data's closing prices)
    by_mid = {m['id']: m for m in ms}
    vb, vbb, curve = new_vb(), {}, [0.0]
    for row in sorted(eng['value'], key=lambda r: (by_mid[r[0]]['ko'], r[0])):
        mid, key, price, p = row[:4]
        edge = row[4] if len(row) > 4 else p * price - 1
        hs, as_ = res[mid]
        won = bool(settle(key, hs, as_))
        v = agg['value']
        v[0] += 1; v[1] += int(won); v[2] = round(v[2] + (price - 1 if won else -1), 3)
        cl = (by_mid[mid]['mkt'] or {}).get('cl') or {}
        clv = round(price * cl[key] - 1, 4) if cl.get(key) else None
        vb_add(vb, won, price, clv)
        vb_add(vbb.setdefault(vb_band(edge), new_vb()), won, price, clv)
        curve.append(vb['u'])
    agg['vb'], agg['vbb'] = vb, vbb
    agg['vb_curve'] = thin_curve(curve)
    for d, sl in eng['slips'].items():
        for s_ in sl:
            if not s_['f'] or not s_['legs']:
                continue
            won = all(settle(k, *res[i]) for i, k in s_['legs'])
            a = agg['slips'][s_['t']]
            a[0] += 1; a[1] += int(won); a[2] = round(a[2] + (s_['o'] - 1 if won else -1), 3); a[3] = round(a[3] + s_['p'], 4)
    out = {'app': 'Statpad', 'season': label, 'at': iso(NOW), 'games': len(ms), 'leagues': len({m['lk'] for m in ms}),
           'skip_weeks': BT_SKIP_WEEKS, 'target': TRACK_TARGET, 'model': MODEL, 'wm': MODEL_W or W_DEFAULT, 'agg': agg, 'ver': eng.get('ver'),
           'vb_min': VB_MIN,
           'by_league': dict(sorted(by_lg.items(), key=lambda x: -x[1][0]))}
    log(f"backtest: {label}, {len(ms)} games in {out['leagues']} leagues (ratings {t1 - t0:.0f} s, total {time.time() - t0:.0f} s)")
    return out

# ----------------------------------------------------------------------------------------------
# Per-market correction. The replay keeps every market's chance for two past seasons; for each kind of market
# (home/away win, draw, double chance, goals lines, both teams to score, team goals) a correction
# log-odds(landed) = a + b * log-odds(stated) is fitted on the older season and kept only if it predicts the
# newer one, which it never saw, better. The page and the track record use the kept ones.
# ----------------------------------------------------------------------------------------------
NODE_SELS = ENGINE_LOAD + r"""
if (E.setCalib) E.setCalib(null);
if (E.setMcal) E.setMcal(null);
const out = [];
for (const m of inp.matches) {
  let a = null; try { a = E.analyse(m, Date.parse(m.ko) - 3600e3); } catch (err) { a = null; }
  if (!a) continue;
  out.push([m.id, a.sels.filter((s) => !s.x).map((s) => [s.key, +s.p.toFixed(4)])]);
}
process.stdout.write(JSON.stringify(out));
"""
MCAL_MIN_GAIN = 2e-5     # log loss per selection on the unseen season


def mcal_group(k):
    return ('HA' if k in ('H', 'A') else 'D' if k == 'D' else 'DC' if k in ('1X', 'X2') else 'OU' if k in ('O15', 'O25', 'U25', 'U35')
            else 'BTTS' if k in ('BTTS', 'BTTS_N') else 'TG' if re.match(r'^[HA]_O\d5$', k) else 'CB')


def fit_logit(xs, a0=0.0, b0=1.0, va=4.0, vb=1.0):
    """Maximum-likelihood a, b in P(landed) = 1 / (1 + exp(-(a + b x))), held a little towards (a0, b0)
    (prior variances va, vb)."""
    a, b = a0, b0
    for _ in range(40):
        ga, gb, haa, hab, hbb = -(a - a0) / va, -(b - b0) / vb, -1 / va, 0.0, -1.0 / vb
        for x, y in xs:
            p = 1 / (1 + math.exp(-(a + b * x)))
            w = p * (1 - p)
            ga += y - p
            gb += (y - p) * x
            haa -= w
            hab -= w * x
            hbb -= w * x * x
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da, db = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-10:
            break
    return a, b


def logit_ll(xs, a=0.0, b=1.0):
    t = 0.0
    for x, y in xs:
        p = 1 / (1 + math.exp(-(a + b * x)))
        t -= math.log(max(1e-12, p if y else 1 - p))
    return t / max(1, len(xs))


def fit_mcal(state):
    t0 = time.time()
    data, seg = {}, {}
    labels = []
    for back, tag in ((2, 'fit'), (1, 'check')):
        code, prev = season_code(NOW, back), season_code(NOW, back + 1)
        seasons = {}
        for lk, c in L.items():
            if c['fd']:
                cur = fd_season(lk, code)
                if cur:
                    seasons[lk] = (cur, fd_season(lk, prev))
        ms, res = bt_matches(seasons)
        if len(ms) < 500:
            log('market correction: not enough replayed games for', season_name(code))
            return None
        out = run_node(NODE_SELS, {'matches': ms})
        if not out:
            return None
        lk_of = {m['id']: m['lk'] for m in ms}
        for mid, sels in out:
            hs, as_ = res[mid]
            for k, p in sels:
                w = settle(k, hs, as_)
                if w is None:
                    continue
                q = min(0.995, max(0.005, p))
                data.setdefault(mcal_group(k), {}).setdefault(tag, []).append((math.log(q / (1 - q)), 1 if w else 0))
                seg.setdefault(tag, []).append((mcal_group(k), k, lc_group(lk_of.get(mid)), q, 1 if w else 0))
        labels.append(season_name(code))
    groups = {}
    for g, d in sorted(data.items()):
        fit_rows, ck = d.get('fit') or [], d.get('check') or []
        if len(fit_rows) < 2000 or len(ck) < 1000:
            continue
        a, b = fit_logit(fit_rows)
        l0, l1 = logit_ll(ck), logit_ll(ck, a, b)
        on = l1 < l0 - MCAL_MIN_GAIN
        if on:   # it helps on the unseen season: refit on both for the live correction
            a, b = fit_logit(fit_rows + ck)
        a, b = max(-0.3, min(0.3, a)), max(0.85, min(1.25, b))
        groups[g] = {'on': on, 'a': round(a, 4), 'b': round(b, 4), 'n': len(fit_rows) + len(ck), 'll': [round(l0, 5), round(l1, 5)]}
    log(f"market correction: fitted on {labels[0]}, checked on {labels[1]} in {time.time() - t0:.0f} s: "
        + ', '.join(f"{g} {'on' if v['on'] else 'off'} ({v['ll'][0]:.4f} -> {v['ll'][1]:.4f})" for g, v in groups.items()))
    # the same for each kind of market in each kind of league, held towards the market's own correction, and kept
    # only where it beats that on the unseen season
    for (g, tier), rows in sorted(_by(seg.get('fit') or [], lambda r: (r[0], r[2])).items()):
        ck = [r for r in seg.get('check') or [] if r[0] == g and r[2] == tier]
        if tier == 'other' or g not in groups or len(rows) < 3000 or len(ck) < 1500:
            continue
        base = groups[g]
        a0, b0 = (base['a'], base['b']) if base['on'] else (0.0, 1.0)
        xs = [(math.log(r[3] / (1 - r[3])), r[4]) for r in rows]
        xc = [(math.log(r[3] / (1 - r[3])), r[4]) for r in ck]
        a, b = fit_logit(xs, a0, b0, 0.05, 0.02)
        l0, l1 = logit_ll(xc, a0, b0), logit_ll(xc, a, b)
        on = l1 < l0 - MCAL_MIN_GAIN
        if on:
            a, b = fit_logit(xs + xc, a0, b0, 0.05, 0.02)
        a, b = max(-0.3, min(0.3, a)), max(0.85, min(1.25, b))
        groups[f'{g}|{tier}'] = {'on': on, 'a': round(a, 4), 'b': round(b, 4), 'n': len(rows) + len(ck), 'll': [round(l0, 5), round(l1, 5)]}
    log('market correction by kind of league: ' + ', '.join(f"{g} {'on' if v['on'] else 'off'} ({v['ll'][0]:.4f} -> {v['ll'][1]:.4f})"
                                                           for g, v in groups.items() if '|' in g))
    state['autopsy'] = autopsy(seg.get('check') or [], groups, labels[1], state)
    return {'at': iso(NOW), 'fit': labels[0], 'check': labels[1], 'groups': groups}


def _by(rows, key):
    out = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out


AU_MIN_N = 300
AU_Z = 2.5
AU_NAMES = {'HA': 'Home or away win', 'D': 'Draw', 'DC': 'Double chance', 'OU': 'Goals over/under', 'BTTS': 'Both teams to score',
            'TG': 'Team goals', 'CB': 'Combos'}
AU_TIER = {'big': 'top leagues', 'second': 'second tiers', 'more': 'other leagues', 'other': 'cups and others'}


def _corr(groups, g, tier):
    c = groups.get(f'{g}|{tier}')
    c = c if c and c.get('on') else groups.get(g)
    return (c['a'], c['b']) if c and c.get('on') else None


def au_rows(rows, groups, label_of):
    """For each segment: [label, selections, stated, landed, z before, stated after the corrections, z after]."""
    out = []
    for key, rs in _by(rows, label_of).items():
        if key is None or len(rs) < AU_MIN_N:
            continue
        n = len(rs)
        st = sum(r[3] for r in rs) / n
        ld = sum(r[4] for r in rs) / n
        v = sum(r[3] * (1 - r[3]) for r in rs)
        z = (ld - st) * n / math.sqrt(v) if v else 0.0
        adj = []
        for r in rs:
            c = _corr(groups, r[0], r[2])
            if c:
                x = math.log(r[3] / (1 - r[3]))
                adj.append(1 / (1 + math.exp(-(c[0] + c[1] * x))))
            else:
                adj.append(r[3])
        st2 = sum(adj) / n
        v2 = sum(q * (1 - q) for q in adj)
        z2 = (ld - st2) * n / math.sqrt(v2) if v2 else 0.0
        out.append([key, n, round(st, 4), round(ld, 4), round(z, 2), round(st2, 4), round(z2, 2)])
    return out


def autopsy(rows, groups, label, state):
    """Where Statpad's chances have been systematically off: on the replayed season the corrections never saw, every
    market by kind of market, kind of league, chance band and side; and on the live record's graded top picks.
    Segments with at least AU_MIN_N selections whose landed rate is more than AU_Z standard errors from what was stated."""
    band = lambda q: None if q < 0.5 else f'{int(q * 10) * 10}–{int(q * 10) * 10 + 10}%' if q < 0.9 else '90%+'
    segs = []
    segs += au_rows(rows, groups, lambda r: f"{AU_NAMES.get(r[0], r[0])}, {AU_TIER[r[2]]}")
    segs += au_rows(rows, groups, lambda r: (f"Chances of {band(r[3])}" if band(r[3]) else None))
    segs += au_rows(rows, groups, lambda r: {'H': 'Home wins', 'A': 'Away wins', 'D': 'Draws'}.get(r[1]))
    segs += au_rows(rows, groups, lambda r: (f"{AU_NAMES.get(r[0], r[0])}, chances of {band(r[3])}" if band(r[3]) and r[0] in ('HA', 'OU', 'BTTS', 'DC') else None))
    flagged = sorted([x for x in segs if abs(x[4]) >= AU_Z], key=lambda x: -abs(x[4]))
    # the live record: graded top picks (chance as stated before any correction, and whether it landed)
    live = []
    try:
        tr = load_track(state)[0]
        picks = [p for p in tr.get('picks') or [] if p[13] is not None and p[9]]
        lrows = [(mcal_group(p[8]), p[8], lc_group(p[1]), min(0.995, max(0.005, p[18] if len(p) > 18 and p[18] else p[9])), 1 if p[13] else 0) for p in picks]
        live = au_rows(lrows, {}, lambda r: f"{AU_NAMES.get(r[0], r[0])}, {AU_TIER[r[2]]}") + au_rows(lrows, {}, lambda r: (f"Chances of {band(r[3])}" if band(r[3]) else None))
        live = sorted(live, key=lambda x: -abs(x[4]))[:8]
    except Exception as e:
        log('autopsy: live record unreadable', repr(e)[:80])
    fixed = sum(1 for x in flagged if abs(x[6]) < AU_Z)
    log(f'autopsy: {len(segs)} segments of {label}, {len(flagged)} off by more than {AU_Z} standard errors, {fixed} of them back in line after the corrections')
    top = sorted(segs, key=lambda x: -abs(x[4]))[:6]
    return {'at': iso(NOW), 'season': label, 'n': len(rows), 'segs': len(segs), 'flagged': flagged[:14], 'top': top, 'live': live, 'z': AU_Z, 'min_n': AU_MIN_N}


def public_mcal(state):
    g = ((state.get('mcal') or {}).get('groups')) or {}
    out = {k: [v['a'], v['b']] for k, v in g.items() if v.get('on')}
    if any('|' in k for k in out):   # which kind each league is, for the corrections by kind of league
        out['_g'] = {lk: lc_group(lk) for lk in L if lc_group(lk) != 'other'}
    return out or None


# ----------------------------------------------------------------------------------------------
def load_json(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def stable(doc):
    d = dict(doc)
    d.pop('generated', None)
    d.pop('sources', None)
    d.pop('window', None)
    d['odds'] = {k: v for k, v in (d.get('odds') or {}).items() if k not in ('remaining', 'used', 'interval_min', 'plan')}
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def due(state, key, hours):
    t = (state.get('last') or {}).get(key)
    return FORCE or not t or NOW - parse_iso(t) >= timedelta(hours=hours) - timedelta(minutes=2)


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = load_json(STATE_DIR / 'state.json') or {}
    seed = load_json(SEED)
    prev = load_json(STATE_DIR / 'last.json') or seed
    last = state.setdefault('last', {})
    src = state.setdefault('sources', {})

    def safe(fn, default):
        try:
            return fn()
        except Exception as e:   # one bad source must never stop the run
            log('source error in', fn.__name__, '->', repr(e))
            return default

    retuned = False
    if tune_due(state):
        last['tune'] = iso(NOW)
        tn = safe(lambda: run_tuning(state), None)
        if tn:
            state['tune'] = tn
            retuned = True
            src['tuning'] = {'ok': True, 'at': iso(NOW), 'note': f"{'tuned settings on' if tn['on'] else 'default settings kept'}"
                                                              f"{', market share ' + str(tn['w']) if tn['w_on'] else ''}"}
        elif not state.get('tune'):
            src['tuning'] = {'ok': False, 'at': iso(NOW), 'note': 'past seasons not available yet'}
    apply_tuning(state)
    model_sig = json.dumps([MODEL, MODEL0, MODEL_W, FX] + ([MODEL_X, KAL, RHO_L, STACK] if MODEL_X or KAL or RHO_L or STACK else []), sort_keys=True)
    if model_sig != state.get('model_sig'):
        state['model_sig'] = model_sig
        retuned = True

    xg_note = safe(lambda: refresh_xg(state), 'error')
    src['xg'] = {'ok': xg_note != 'error', 'at': iso(NOW), 'note': xg_note}
    log('commentary xG:', xg_note)
    es_note = safe(lambda: refresh_espn_data(state), 'error')
    n_px = safe(lambda: refresh_espn_prices(state, (prev or {}).get('matches') or []), None)
    if n_px is not None:
        es_note += f', fresh DraftKings prices for {n_px} games'
        log('ESPN prices:', n_px, 'games')
    src['espn_data'] = {'ok': es_note != 'error', 'at': iso(NOW), 'note': es_note}
    log('ESPN competitions:', es_note)
    if due(state, 'results', RESULTS_EVERY) or not state.get('leagues') or 'recent_results' not in state or 'teamfiles' not in state or 'last_played' not in state or retuned:
        res = safe(load_results, {})
        if res:
            state['leagues'] = safe(lambda: compute_leagues(res, Teams()), state.get('leagues') or {})
            state['recent_results'] = safe(lambda: recent_results(res), state.get('recent_results') or {})
            tf = safe(lambda: team_files(res), None)
            state.setdefault('teamfiles', {})   # set even when nothing was made, so a failure doesn't reload results every run
            if tf:
                state['teamfiles'] = dict(state['teamfiles'], **tf)
                state['teams_changed'] = True
                state['teams_sig'] = iso(NOW)
            lp_new, lp_old = safe(lambda: last_played(res), {}), state.get('last_played') or {}
            for lk_, d_ in lp_old.items():   # keep later dates seen at kick-off, before football-data lists the game
                for t_, ds_ in d_.items():
                    if ds_ > lp_new.setdefault(lk_, {}).get(t_, ''):
                        lp_new[lk_][t_] = ds_
            state['last_played'] = lp_new
            last['results'] = iso(NOW)
            src['results'] = {'ok': True, 'at': iso(NOW), 'leagues': len(res)}
        else:
            src['results'] = {'ok': False, 'at': iso(NOW)}
        log('results:', len(res), 'leagues')
        state['es_hist_new'] = True
    if state.pop('es_hist_new', False) or retuned:   # the competitions rated from ESPN's results
        eres = safe(lambda: espn_results(state), {})
        if eres:
            state.setdefault('leagues', {}).update(safe(lambda: compute_leagues(eres, Teams()), {}))
            state.setdefault('recent_results', {}).update(safe(lambda: recent_results(eres), {}))
            tf = safe(lambda: team_files(eres), None)
            if tf:
                state['teamfiles'] = dict(state.get('teamfiles') or {}, **tf)
                state['teams_changed'] = True
            log('ESPN ratings:', ', '.join(f"{lk} {len(g)} games" for lk, (g, _) in sorted(eres.items())))

    if due(state, 'intl', RESULTS_EVERY):
        last['intl'] = iso(NOW)   # on its own timer, so a failed download can't trigger reloads every run
        got = safe(load_intl, None)
        if got:
            state['intl'], state['intl_recent'] = got
            src['intl'] = {'ok': True, 'at': iso(NOW), 'note': f"{len(state['intl']['ratings'])} national teams, results to {state['intl']['last']}"}
            log('national teams:', len(state['intl']['ratings']), 'rated, results to', state['intl']['last'])
        else:
            src['intl'] = {'ok': False, 'at': iso(NOW)}

    if due(state, 'fixtures', FIXTURES_EVERY) or 'of_fixtures' not in state:
        fx, ok = safe(load_fd_fixtures, ([], False))
        if ok:
            state['fd_fixtures'] = fx
            last['fixtures'] = iso(NOW)
        ofx, ok2 = safe(load_openfootball, ([], False))
        state['of_fixtures'] = ofx if ok2 else state.get('of_fixtures') or []
        src['fixtures'] = {'ok': ok or ok2, 'at': iso(NOW), 'count': len(fx) + len(ofx)}
        log('football-data fixtures:', len(fx), '| openfootball schedule fixtures:', len(ofx))

    prev_matches = (prev or {}).get('matches') or []
    lv_ok, lv_note = safe(lambda: refresh_live(state, prev_matches), (False, 'error'))
    src['live'] = {'ok': lv_ok, 'at': iso(NOW), 'note': lv_note}
    log('live:', lv_note)
    if AF_KEY:
        note = safe(lambda: refresh_team_news(state, prev_matches), 'error')
        src['team_news'] = {'ok': note != 'error' and not af_blocked(state), 'at': iso(NOW), 'note': note}
        log('team news:', note)
    else:
        src.pop('team_news', None)

    msg = safe(lambda: refresh_odds_api(state), 'error')
    src['odds_api'] = {'ok': msg not in ('no key', 'paused', 'out of credits'), 'at': iso(NOW), 'note': msg}
    log('odds:', msg, '| credits left:', (state.get('quota') or {}).get('remaining'))

    doc = build(state, prev, seed)
    if RHO_L:
        doc['rho'] = RHO_L   # the draw correction per league, for the page's record of finished games
    safe(lambda: espn_publish(state, doc), None)
    safe(lambda: refresh_crests(state, doc), 0)
    safe(lambda: globe_textures(state), 0)
    safe(lambda: refresh_stakes(state, doc), None)
    n_fx = safe(lambda: apply_fx(state, doc), 0)
    if n_fx:
        log(f'fatigue and motivation: {n_fx} games adjusted')
    n_st = safe(lambda: apply_stack(state, doc), 0)
    if n_st:
        log(f'learned blend: {n_st} games')
    doc['calib'] = safe(lambda: fit_calib(load_track(state)[0]['picks']), {'on': False, 'n': 0, 'need': CAL_MIN})
    if public_mcal(state):
        doc['mcal'] = public_mcal(state)
    if doc['calib'].get('on'):
        log(f"self-tuning: on from {doc['calib']['n']} graded picks (a={doc['calib']['a']}, b={doc['calib']['b']})")
    changed = prev is None or stable(doc) != stable(prev) or FORCE
    if not doc['matches'] and prev and prev.get('matches') and not FORCE:
        changed = False   # never replace a live board with an empty one

    n_sc = safe(lambda: refresh_scores(state), 0)
    if n_sc:
        log('results from The Odds API for', n_sc, 'competitions')
    track = safe(lambda: track_update(doc, state), None)
    safe(lambda: live_check(state), None)
    if due(state, 'backtest', BT_EVERY_H) or not state.get('backtest') or retuned or 'mcal' not in state:
        last['backtest'] = iso(NOW)
        mc = safe(lambda: fit_mcal(state), None)
        if mc:
            state['mcal'] = mc
            changed = True
        else:
            state.setdefault('mcal', {})
        bt = safe(lambda: run_backtest(state), None)
        if bt:
            state['backtest'] = bt
            changed = True
    if due(state, 'vhist', BT_EVERY_H if state.get('vhist') else 6):   # weekly; every 6 hours until the first one works
        last['vhist'] = iso(NOW)
        vh = safe(lambda: value_history(), None)
        if vh:
            state['vhist'] = vh
            changed = True
    if state.get('backtest'):
        (OUT_DIR / 'backtest.json').write_text(json.dumps(dict(state['backtest'], tune=public_tune(state), vh=state.get('vhist'),
                                                               lc=state.get('live_check'), au=state.get('autopsy')),
                                                          ensure_ascii=False, separators=(',', ':')))
    # team pages and the season simulator read one small file per league
    if state.get('teamfiles'):
        (OUT_DIR / 'teams').mkdir(parents=True, exist_ok=True)
        for lk, data in state['teamfiles'].items():
            (OUT_DIR / 'teams' / f'{lk}.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
        if state.pop('teams_changed', False):
            changed = True
    live = safe(lambda: publish_live(state), None)
    if live is not None:
        (OUT_DIR / 'live.json').write_text(json.dumps(live, ensure_ascii=False, separators=(',', ':')))
        lsig = json.dumps(live['games'], sort_keys=True)
        if lsig != state.get('live_sig'):
            state['live_sig'] = lsig
            changed = True
    results = safe(lambda: publish_results(state), None)
    if results is not None:
        (OUT_DIR / 'results.json').write_text(json.dumps(results, ensure_ascii=False, separators=(',', ':')))
        rsig = json.dumps([len(results['scores']), len(results['void'])])
        if rsig != state.get('results_sig'):
            state['results_sig'] = rsig
            changed = True
    if track:
        (OUT_DIR / 'track.json').write_text(json.dumps(track, ensure_ascii=False, separators=(',', ':')))
        mir = safe(lambda: publish_miracles(track), None)
        if mir:
            (OUT_DIR / 'miracles.json').write_text(json.dumps(mir, ensure_ascii=False, separators=(',', ':')))
        publish_ledger()
        sig = json.dumps([track['agg'], len(track['picks']), len(track['slips']), track['open'], track['pending'], track.get('vopen'), track.get('ledger')])
        if sig != state.get('track_sig'):
            state['track_sig'] = sig
            changed = True
    body = json.dumps(doc, ensure_ascii=False, separators=(',', ':'))
    (OUT_DIR / 'matches.json').write_text(body)
    if changed:
        (STATE_DIR / 'last.json').write_text(body)
    # trim cached API events that have kicked off long ago
    for blob in (state.get('api') or {}).values():
        blob['events'] = [e for e in blob.get('events') or [] if e.get('ko') and parse_iso(e['ko']) > NOW - timedelta(hours=6)]
    (STATE_DIR / 'state.json').write_text(json.dumps(state, ensure_ascii=False, separators=(',', ':')))
    n_pred = sum(1 for m in doc['matches'] if m['lm'] or m['mkt'])
    log(f"{len(doc['matches'])} matches ({n_pred} with a prediction), {len(doc['leagues'])} competitions, changed={changed}")
    gh = ENV('GITHUB_OUTPUT')
    if gh:
        with open(gh, 'a') as fh:
            fh.write(f'changed={"true" if changed else "false"}\n')


if __name__ == '__main__':
    main()
