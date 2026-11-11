#!/usr/bin/env python3
"""
Statpad data pipeline. Python 3.9+ standard library only, so GitHub Actions needs no installs.

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

The browser (index.html) turns that into probabilities for every market, value picks and slips.

Environment (all optional):
  ODDS_API_KEY          The Odds API key (store it as a GitHub Actions secret, never in the repo)
  ODDS_REGIONS          bookmaker regions, default "eu"            (each region costs credits)
  ODDS_MARKETS          markets, default "h2h"  (add ",totals" for over/under; doubles the cost)
  ODDS_MIN_INTERVAL     fastest refresh per competition in minutes, default 5 (the credit budget
                        decides the real pace; games kicking off soonest are refreshed first)
  ODDS_MONTHLY_CREDITS  your plan's monthly credits, default 500 (the free plan)
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
import io
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
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


def comp(lk, name, country, group, strength, fd=None, x=None, oa=()):
    L[lk] = dict(name=name, country=country, group=group, strength=strength, fd=fd, x=x, oa=list(oa))


comp('EPL', 'Premier League', 'England', 'big', 0.00, fd='E0', oa=['soccer_epl'])
comp('LaLiga', 'La Liga', 'Spain', 'big', -0.10, fd='SP1', oa=['soccer_spain_la_liga'])
comp('BUN', 'Bundesliga', 'Germany', 'big', -0.12, fd='D1', oa=['soccer_germany_bundesliga'])
comp('SA', 'Serie A', 'Italy', 'big', -0.12, fd='I1', oa=['soccer_italy_serie_a'])
comp('L1', 'Ligue 1', 'France', 'big', -0.20, fd='F1', oa=['soccer_france_ligue_one'])
comp('POR', 'Primeira Liga', 'Portugal', 'big', -0.30, fd='P1', oa=['soccer_portugal_primeira_liga'])
comp('ERE', 'Eredivisie', 'Netherlands', 'big', -0.32, fd='N1', oa=['soccer_netherlands_eredivisie'])
comp('BEL', 'Belgian Pro League', 'Belgium', 'big', -0.38, fd='B1', oa=['soccer_belgium_first_div'])
comp('KSA', 'Saudi Pro League', 'Saudi Arabia', 'big', -0.45, oa=['soccer_saudi_arabia_pro_league'])
comp('ENG2', 'Championship', 'England', 'second', -0.45, fd='E1', oa=['soccer_efl_champ'])
comp('GER2', '2. Bundesliga', 'Germany', 'second', -0.50, fd='D2', oa=['soccer_germany_bundesliga2'])
comp('ITA2', 'Serie B', 'Italy', 'second', -0.55, fd='I2', oa=['soccer_italy_serie_b'])
comp('ESP2', 'LaLiga 2', 'Spain', 'second', -0.52, fd='SP2', oa=['soccer_spain_segunda_division'])
comp('FRA2', 'Ligue 2', 'France', 'second', -0.60, fd='F2', oa=['soccer_france_ligue_two'])
comp('NED2', 'Eerste Divisie', 'Netherlands', 'second', -0.80)
comp('ENG3', 'League One', 'England', 'second', -0.75, fd='E2', oa=['soccer_england_league1'])
comp('ENG4', 'League Two', 'England', 'second', -0.95, fd='E3', oa=['soccer_england_league2'])
comp('SCO1', 'Scottish Premiership', 'Scotland', 'more', -0.50, fd='SC0', oa=['soccer_spl'])
comp('SCO2', 'Scottish Championship', 'Scotland', 'second', -0.95, fd='SC1')
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


SHARP = {'pinnacle': 3.0, 'betfair_ex_eu': 2.0, 'betfair_ex_uk': 2.0, 'matchbook': 2.0, 'smarkets': 2.0}
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
        self.global_ = {}     # normkey -> (lk, canonical)

    def add(self, lk, name):
        k = norm(name)
        self.by_league.setdefault(lk, {})[k] = name
        self.global_.setdefault(k, (lk, name))

    def resolve(self, lk, raw, cutoff=0.80):
        k = norm(raw)
        table = self.by_league.get(lk) or {}
        if k in table:
            return table[k]
        hit = self._fuzzy(k, table, cutoff)
        return hit

    def resolve_global(self, raw, cutoff=0.86):
        k = norm(raw)
        if k in self.global_:
            return self.global_[k]
        keys = {kk: kk for kk in self.global_}
        hit = self._fuzzy(k, keys, cutoff)
        return self.global_[hit] if hit else None

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


def fetch(url, params=None, tries=3, headers=None):
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
            with urlopen(req, timeout=40) as r:
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


def fit_ratings(games, current_teams):
    if len(games) < 20:
        return None
    xi = math.log(2) / HALF_LIFE
    teams = sorted({g[1] for g in games} | {g[2] for g in games})
    ix = {t: i for i, t in enumerate(teams)}
    prev_teams = {g[1] for g in games if not g[5]} | {g[2] for g in games if not g[5]}
    W = []
    for d, h, a, hg, ag, cur in games:
        age = max(0.0, (NOW - d).total_seconds() / 86400)
        W.append((ix[h], ix[a], hg, ag, math.exp(-xi * age) * (1.0 if cur else PREV_W)))
    n = len(teams)
    pa = [0.0] * n
    pd = [0.0] * n
    for t, i in ix.items():
        if prev_teams and t not in prev_teams:
            pa[i], pd[i] = NEW_TEAM_PRIOR
    sh = sum(w * hg for _, _, hg, _, w in W)
    sa = sum(w * ag for _, _, _, ag, w in W)
    sw = sum(w for *_, w in W)
    mu = math.log(max(0.3, sa / sw))
    home = math.log(max(0.3, sh / sw)) - mu
    att = pa[:]
    dfn = pd[:]
    cur_ix = [ix[t] for t in current_teams if t in ix] or list(range(n))
    for _ in range(200):
        g0 = math.exp(mu + home / 2)
        num = [0.0] * n
        den = [0.0] * n
        for h, a, hg, ag, w in W:
            num[h] += w * hg
            den[h] += w * math.exp(mu + home + dfn[a])
            num[a] += w * ag
            den[a] += w * math.exp(mu + dfn[h])
        new_att = [math.log((num[i] + K_SHRINK * g0 * math.exp(pa[i])) / (den[i] + K_SHRINK * g0)) for i in range(n)]
        num = [0.0] * n
        den = [0.0] * n
        for h, a, hg, ag, w in W:
            num[a] += w * hg
            den[a] += w * math.exp(mu + home + new_att[h])
            num[h] += w * ag
            den[h] += w * math.exp(mu + new_att[a])
        new_dfn = [math.log((num[i] + K_SHRINK * g0 * math.exp(pd[i])) / (den[i] + K_SHRINK * g0)) for i in range(n)]
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
    return {'mu': round(mu, 5), 'home': round(home, 5), 'games': len(games),
            'teams': {t: [round(att[ix[t]], 5), round(dfn[ix[t]], 5)] for t in teams}}


def build_table(games):
    T = {}
    for d, h, a, hg, ag, cur in sorted(games, key=lambda g: g[0]):
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
                if d and h and a and hg is not None and ag is not None:
                    games.append((d, h, a, int(hg), int(ag), cur))
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
        games = [(d, h, a, hg, ag, s == latest) for d, h, a, hg, ag, s in parsed]
        out[lk] = (games, latest)
    return out


def compute_leagues(results, teams):
    leagues = {}
    for lk, (games, season) in results.items():
        table = build_table(games)
        cur_teams = [t for t, _ in table]
        rt = fit_ratings(games, cur_teams)
        for t in cur_teams:
            teams.add(lk, t)
        rows = []
        for pos, (t, r) in enumerate(table, 1):
            ar = (rt or {}).get('teams', {}).get(t)
            rows.append([t, r['p'], r['w'], r['d'], r['l'], r['gf'], r['ga'], r['pts'], r['form'],
                         ar[0] if ar else None, ar[1] if ar else None])
        leagues[lk] = {'season': season, 'table': rows,
                       'mu': rt['mu'] if rt else None, 'home': rt['home'] if rt else None,
                       'ratings': {t: v for t, v in (rt or {}).get('teams', {}).items() if t in cur_teams},
                       'games': len(games)}
    return leagues


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
    inv = [1 / x for x in avg]
    s = sum(inv)
    m = {'p': [round(x / s, 4) for x in inv], 'avg': avg, 'best': best, 'bk': None, 'n': None,
         'ou': None, 'src': 'football-data', 'upd': upd}
    o, u = fnum(col(row, 'Avg>2.5', 'AvgC>2.5', 'BbAv>2.5', 'B365>2.5')), fnum(col(row, 'Avg<2.5', 'AvgC<2.5', 'BbAv<2.5', 'B365<2.5'))
    if o and u and o > 1 and u > 1:
        mo, mu_ = fnum(col(row, 'Max>2.5', 'MaxC>2.5', 'BbMx>2.5')), fnum(col(row, 'Max<2.5', 'MaxC<2.5', 'BbMx<2.5'))
        m['ou'] = {'line': 2.5, 'pO': round((1 / o) / (1 / o + 1 / u), 4), 'avg': [o, u],
                   'best': [round(max(mo or 0, o), 3), round(max(mu_ or 0, u), 3)]}
    return m


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
        inv = [1 / x for x in pr]
        s = sum(inv)
        w = SHARP.get(name, 1.0)
        for i in range(3):
            acc[i] += w * inv[i] / s
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
            po += w * (1 / o) / (1 / o + 1 / u)
            ws += w
            if name not in EXCHANGES:
                bo = [max(bo[0], o), max(bo[1], u)]
        if bo[0] == 0:
            bo = [max(v[0] for v in ous.values()), max(v[1] for v in ous.values())]
        m['ou'] = {'line': 2.5, 'pO': round(po / ws, 4),
                   'avg': [round(sum(v[0] for v in ous.values()) / len(ous), 3), round(sum(v[1] for v in ous.values()) / len(ous), 3)],
                   'best': bo}
    return m


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

    # paid price refreshes: an even daily budget (unused credits roll forward), spent on the
    # competitions whose next game is closest; nothing refreshes more often than ODDS_MIN_INTERVAL
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
    daily = max(0.0, remaining - 5 + spent) * min(share, 3.0 / days_left)   # never more than 3x an even day
    elapsed = ((NOW - day0).total_seconds() / 60 + 90) / 1440          # pro-rata through the day
    allowed = daily * min(1.0, elapsed) - spent

    def tier(nxt):   # how much less often than the base interval
        h = (nxt - NOW).total_seconds() / 3600
        return 1 if h <= 6 else 3 if h <= 24 else 6 if h <= 48 else 18

    near = sum(1 for _, nxt in active if tier(nxt) == 1)
    state['odds_interval'] = round(max(MIN_INTERVAL, 1440 * max(1, near) * cost / daily), 1) if daily > 0 else None
    if remaining < cost + 5 and not FORCE:
        return 'out of credits'
    cands = []
    news = state.get('lineup_lk') or {}
    for key, nxt in active:
        last = (api.get(key) or {}).get('odds_at')
        age = (NOW - parse_iso(last)).total_seconds() / 60 if last else 1e9
        lu = news.get(plan.get(key))
        fresh_news = lu and (not last or parse_iso(last) < parse_iso(lu)) and (nxt - NOW) <= timedelta(hours=2) and age >= MIN_INTERVAL - 1
        if fresh_news:
            cands.append((0, nxt, -age, key))   # line-ups just landed: prices move on team news
        elif FORCE or age >= MIN_INTERVAL * tier(nxt) - 1:
            cands.append((tier(nxt), nxt, -age, key))
    cands.sort()
    done = 0
    for _, nxt, _, key in cands:
        if not FORCE and allowed < cost:
            break
        if (state.get('quota') or {}).get('remaining') is not None and state['quota']['remaining'] < cost + 5:
            break
        data = oa_get(f'/sports/{key}/odds', {'regions': ','.join(REGIONS), 'markets': ','.join(MARKETS),
                                              'oddsFormat': 'decimal', 'dateFormat': 'iso',
                                              'commenceTimeFrom': frm, 'commenceTimeTo': to}, state)
        allowed -= cost
        spent += cost
        if not isinstance(data, list):
            continue
        cur = {e['id']: e for e in (api.get(key) or {}).get('events', [])}
        for ev in data:
            cur[ev.get('id')] = parse_api_event(ev)
        blob = api.setdefault(key, {})
        blob['events'] = list(cur.values())
        blob['odds_at'] = iso(NOW)
        done += 1
    state['spend'] = {today: spent}
    return f'{done} competitions priced'


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


def af_blocked(state):
    b = (state.get('af') or {}).get('block_until')
    return bool(b and parse_iso(b) > NOW)


def af_get(path, params, state, reserve=2):
    """One API-Football call -> (response list or None, error text or None). Keeps to the daily allowance,
    leaving `reserve` calls unused (line-ups keep a bigger reserve so results always get through)."""
    af = state.setdefault('af', {})
    day = NOW.strftime('%Y-%m-%d')
    if af.get('day') != day:
        af.update({'day': day, 'used': 0, 'left': None})
    if af_blocked(state):
        return None, 'paused'
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
        elif 'plan' in keys:
            af['block_until'], af['note'] = iso(NOW + timedelta(hours=24)), 'your plan does not cover this: ' + msg[:160]
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
        rows = None
        if af.get('inj_mode', 'date') == 'date':
            rows, err = af_get('/injuries', {'date': ds}, state)
            if rows is None and err and not af_blocked(state) and 'allowance' not in err and 'status' not in err:
                af['inj_mode'] = 'ids'   # this plan or API version wants fixture ids instead of a date
        if af.get('inj_mode') == 'ids':
            fids = sorted({r['fid'] for r in tn.values() if r.get('fid') and r['ko'][:10] == ds})
            rows = []
            for i in range(0, len(fids), AF_BATCH):
                got, err = af_get('/injuries', {'ids': '-'.join(map(str, fids[i:i + AF_BATCH]))}, state)
                if got is None:
                    rows = None
                    break
                rows.extend(got)
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
    tn = state.setdefault('tn', {})
    due = []
    for r in tn.values():
        if not r.get('fid') or r.get('lu'):
            continue
        mins = (parse_iso(r['ko']) - NOW).total_seconds() / 60
        if not (-20 <= mins <= AF_LU_FROM):
            continue
        if r.get('lu_try') and (NOW - parse_iso(r['lu_try'])).total_seconds() / 60 < AF_LU_EVERY - 1 and not FORCE:
            continue
        due.append(r)
    due.sort(key=lambda r: r['ko'])
    found = 0
    for i in range(0, len(due), AF_BATCH):
        chunk = due[i:i + AF_BATCH]
        resp, err = af_get('/fixtures', {'ids': '-'.join(str(r['fid']) for r in chunk)}, state, reserve=AF_RESERVE)
        if resp is None:
            break
        by = {(f.get('fixture') or {}).get('id'): f for f in resp}
        for r in chunk:
            r['lu_try'] = iso(NOW)
            side = {}
            for x in (by.get(r['fid']) or {}).get('lineups') or []:
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
    return [fi['id'], fi['date'], h['name'], a['name'], lg.get('name'), lg.get('country'), h.get('id'), a.get('id'), st, hs, as_]


def af_day(state, ds, why):
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
    pend = state.get('track_pending') or {}
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


def refresh_team_news(state, matches):
    if not AF_KEY:
        return 'no key'
    af = state.setdefault('af', {})
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
    tn = state.setdefault('tn', {})
    for mid in [k for k, r in tn.items() if parse_iso(r['ko']) < NOW - timedelta(hours=6)]:
        del tn[mid]
    for lk in [k for k, v in (state.get('lineup_lk') or {}).items() if parse_iso(v) < NOW - timedelta(hours=6)]:
        del state['lineup_lk'][lk]
    n_lu = sum(1 for r in tn.values() if r.get('lu'))
    note = (f"{linked} matches linked, {n_lu} line-ups in{f' ({got} new)' if got else ''}, "
            f"{graded} new results, {af.get('used', 0)} calls today")
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
    if not r or not r.get('fid'):
        return None
    inj = (((state.get('af') or {}).get('inj') or {}).get(r['ko'][:10]) or {}).get(str(r['fid'])) or []
    out = {'h': [], 'a': []}
    for tid, name, typ, why in inj:
        side = 'h' if tid == r['hid'] else 'a' if tid == r['aid'] else None
        if side and len(out[side]) < 14 and [name, why, typ] not in out[side]:
            out[side].append([name, why, typ])
    lu = r.get('lu') or {}
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
        return [round(math.exp(lg['mu'] + lg['home'] + ah + da), 4), round(math.exp(lg['mu'] + aa + dh), 4)], False
    # cross-league: each side's own league ratings plus a league-strength offset
    rh = teams.resolve_global(h)
    ra = teams.resolve_global(a)
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
    home = 0.22 if L[lk]['group'] == 'cups' else (gh['home'] + ga['home']) / 2
    lam_h = math.exp(mu + home + ah + sh / 2 + da - sa / 2)
    lam_a = math.exp(mu + aa + sa / 2 + dh - sh / 2)
    return [round(lam_h, 4), round(lam_a, 4)], True


def table_pos(leagues):
    pos = {}
    for lk, lg in leagues.items():
        n = len(lg.get('table') or [])
        for i, row in enumerate(lg.get('table') or [], 1):
            pos[(lk, row[0])] = (i, n, row[8])
    return pos


def same_team(x, y):
    return norm(x) == norm(y) or norm(pretty(x)) == norm(y) or norm(x) == norm(pretty(y))


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
            if L[lk]['group'] == 'cups':
                rh, ra = teams.resolve_global(e['h']), teams.resolve_global(e['a'])
                h = rh[1] if rh else e['h']
                a = ra[1] if ra else e['a']
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
        mkt = f['api_mkt'] or f['fd_mkt'] or old.get('mkt')
        om = old.get('mkt') or {}
        if mkt and mkt.get('src') == 'football-data' and om.get('src') == 'football-data' and om.get('avg') == mkt.get('avg') and om.get('best') == mkt.get('best'):
            mkt = dict(mkt, upd=om.get('upd'))   # same prices re-read: keep when they were first seen
        lam, cross = expected_goals(f['lk'], f['h'], f['a'], leagues, teams)
        ph = pos.get((f['lk'], f['h']))
        pa = pos.get((f['lk'], f['a']))
        dom_h = dom_a = None
        if L[f['lk']]['group'] == 'cups':
            gh, ga = teams.resolve_global(f['h']), teams.resolve_global(f['a'])
            dom_h, dom_a = (gh[0] if gh else None), (ga[0] if ga else None)
            ph = pos.get((dom_h, f['h'])) if dom_h else None
            pa = pos.get((dom_a, f['a'])) if dom_a else None
        clash = bool(ph and pa and ph[0] <= 6 and pa[0] <= 6 and ph[1] >= 10 and pa[1] >= 10)
        if clash and L[f['lk']]['group'] == 'cups':
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
        news = team_news_for(state, mid, f['lk'], ko, f['h'], f['a'])
        matches.append({
            'id': mid, 'lk': f['lk'], 'ko': iso(ko), 'h': pretty(f['h']), 'a': pretty(f['a']),
            'ph': ph[0] if ph else None, 'pa': pa[0] if pa else None,
            'fh': ph[2] if ph else None, 'fa': pa[2] if pa else None,
            'dh': dom_h, 'da': dom_a,
            'lm': lam, 'xl': cross, 'mkt': mkt, 'hist': hist, 'clash': clash, 'tbc': tbc, 'ks': ks, 'fr': fr,
        })
        if news:
            matches[-1]['tn'] = news

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
                        'table': [[pretty(r[0])] + r[1:] for r in lg.get('table') or []]})
        out_leagues[lk] = rec

    q = state.get('quota') or {}
    return {
        'app': 'Statpad', 'schema': 2, 'generated': iso(NOW),
        'window': {'from': iso(lo), 'to': iso(hi)},
        'sources': state.get('sources') or {},
        'odds': {'enabled': bool(API_KEY), 'remaining': q.get('remaining'), 'used': q.get('used'),
                 'interval_min': state.get('odds_interval'), 'regions': REGIONS, 'markets': MARKETS},
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
    src: a.src, p: [a.p.H, a.p.D, a.p.A], pm: a.pMod ? [a.pMod.H, a.pMod.D, a.pMod.A] : null, m: a.mkt ? a.mkt.p : null,
    k: a.top ? a.top.key : null, pp: a.top ? a.top.pAdj : null, pr: a.top ? a.top.price : null, r: a.top ? !!a.top.real : false,
    pu: a.top ? (a.top.pRaw != null ? a.top.pRaw : a.top.pAdj) : null,
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
                                   'calib': doc.get('calib')}))
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
        rows = [[d.strftime('%Y-%m-%d'), h, a, hg, ag] for d, h, a, hg, ag, _c in games if NOW - d < timedelta(days=21)]
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
            if abs((dt - d0).days) <= 1 and same_team(h, rec['h']) and same_team(a, rec['a']):
                return int(hg), int(ag)
    return None


def new_agg():
    return {'n': 0, 'nr': 0, 'void': 0,
            'tiers': {t: [0, 0, 0.0] for t in ('A', 'B', 'C', '-')},          # picks, wins, sum of stated chance
            'cal': [[0, 0.0, 0] for _ in range(10)],                          # 1X2 outcomes: count, sum p, happened
            'll': [0, 0.0, 0.0, 0.0],                                         # games, log loss: Statpad, market, model
            'clv': [0, 0.0, 0],                                               # picks, sum CLV, beat the close
            'slips': {t: [0, 0, 0.0, 0.0] for t in ('A', 'B', 'C')}}          # played, won, units profit, sum chance


def load_track(state):
    tr = state.get('track')
    repo = load_json(TRACK_FILE)
    if repo and (not tr or (repo.get('updated') or '') > (tr.get('updated') or '')):
        tr = {'since': repo.get('since') or iso(NOW), 'updated': repo.get('updated') or iso(NOW),
              'saved_at': repo.get('saved_at'), 'agg': repo.get('agg') or new_agg(),
              'picks': repo.get('picks') or [], 'slips': repo.get('slips') or [], 'new': 0}
        if not state.get('slips_open') and repo.get('open'):
            state['slips_open'] = repo['open']
    if not tr:
        tr = {'since': iso(NOW), 'updated': iso(NOW), 'saved_at': None, 'agg': new_agg(), 'picks': [], 'slips': [], 'new': 0}
    state['track'] = tr
    return tr, repo


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
    if eng:
        for mid, e in eng['picks'].items():
            m = by_id.get(mid)
            if not m or not e.get('k') or parse_iso(m['ko']) <= NOW:
                continue
            best = (m.get('mkt') or {}).get('best')
            rec = pend.get(mid) or {'id': mid, 'op': None}
            rec.update({'lk': m['lk'], 'ko': m['ko'], 'h': m['h'], 'a': m['a'], 'src': e['src'], 'p': r4(e['p']),
                        'pm': r4(e['pm']), 'm': r4(e['m']), 'k': e['k'], 'pp': round(e['pp'], 4), 'pr': e['pr'],
                        'r': bool(e['r']), 'cl': best, 'pu': round(e['pu'], 4) if e.get('pu') is not None else None})
            if rec.get('op') is None and best:
                rec['op'] = best
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
    scores = state.setdefault('scores', {})
    for mid, rec in list(pend.items()):
        ko = parse_iso(rec['ko'])
        if ko > NOW - timedelta(hours=2):
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
        t = tier_of(rec['pp']) or '-'
        agg['n'] += 1
        tt = agg['tiers'][t]
        tt[0] += 1; tt[1] += int(won); tt[2] = round(tt[2] + rec['pp'], 4)
        oc = 0 if hs > as_ else 1 if hs == as_ else 2
        for i in range(3):
            b = agg['cal'][min(9, int(rec['p'][i] * 10))]
            b[0] += 1; b[1] = round(b[1] + rec['p'][i], 4); b[2] += int(i == oc)
        if rec.get('m') and rec.get('pm'):
            ll = agg['ll']
            ll[0] += 1
            ll[1] = round(ll[1] - math.log(max(1e-6, rec['p'][oc])), 5)
            ll[2] = round(ll[2] - math.log(max(1e-6, rec['m'][oc])), 5)
            ll[3] = round(ll[3] - math.log(max(1e-6, rec['pm'][oc])), 5)
        clv = None
        side = {'H': 0, 'D': 1, 'A': 2}.get(rec['k'])
        if side is not None and rec.get('r') and rec.get('op') and rec.get('cl') and rec['cl'][side] > 1:
            clv = round(rec['op'][side] / rec['cl'][side] - 1, 4)
            c = agg['clv']
            c[0] += 1; c[1] = round(c[1] + clv, 4); c[2] += int(clv > 0)
        tr['picks'].append([mid, rec['lk'], rec['ko'], rec['h'], rec['a'], hs, as_, rec['src'], rec['k'], rec['pp'],
                            rec['pr'], int(bool(rec['r'])), t, int(won), clv] + rec['p'] + [rec.get('pu') or rec['pp']])
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
    if changed:
        tr['updated'] = iso(NOW)
        tr['new'] = tr.get('new', 0) + 1

    pub = {'app': 'Statpad', 'schema': 1, 'since': tr['since'], 'updated': tr['updated'], 'saved_at': tr.get('saved_at'),
           'tz': TRACK_TZ, 'target': TRACK_TARGET, 'agg': agg, 'picks': tr['picks'], 'slips': tr['slips'],
           'open': open_, 'pending': len(pend)}
    # commit to the repo: new grades at most every few hours, and a keep-alive commit regardless
    saved = parse_iso(tr['saved_at']) if tr.get('saved_at') else None
    keepalive = repo is None or saved is None or NOW - saved >= timedelta(days=KEEPALIVE_DAYS)
    if keepalive or (tr.get('new') and NOW - saved >= timedelta(hours=SAVE_EVERY_H)):
        tr['saved_at'] = pub['saved_at'] = iso(NOW)
        tr['new'] = 0
        TRACK_FILE.write_text(json.dumps(pub, ensure_ascii=False, separators=(',', ':')))
        log('track: saved track.json for commit' + (' (keep-alive)' if keepalive else ''))
    log(f"track: {len(pend)} predictions waiting, {agg['n']} graded, {len(open_)} slips in play")
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
    d['odds'] = {k: v for k, v in (d.get('odds') or {}).items() if k not in ('remaining', 'used', 'interval_min')}
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

    if due(state, 'results', RESULTS_EVERY) or not state.get('leagues') or 'recent_results' not in state:
        res = safe(load_results, {})
        if res:
            state['leagues'] = safe(lambda: compute_leagues(res, Teams()), state.get('leagues') or {})
            state['recent_results'] = safe(lambda: recent_results(res), state.get('recent_results') or {})
            last['results'] = iso(NOW)
            src['results'] = {'ok': True, 'at': iso(NOW), 'leagues': len(res)}
        else:
            src['results'] = {'ok': False, 'at': iso(NOW)}
        log('results:', len(res), 'leagues')

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

    if AF_KEY:
        prev_matches = (prev or {}).get('matches') or []
        note = safe(lambda: refresh_team_news(state, prev_matches), 'error')
        src['team_news'] = {'ok': note != 'error' and not af_blocked(state), 'at': iso(NOW), 'note': note}
        log('team news:', note)
    else:
        src.pop('team_news', None)

    msg = safe(lambda: refresh_odds_api(state), 'error')
    src['odds_api'] = {'ok': msg not in ('no key', 'paused', 'out of credits'), 'at': iso(NOW), 'note': msg}
    log('odds:', msg, '| credits left:', (state.get('quota') or {}).get('remaining'))

    doc = build(state, prev, seed)
    doc['calib'] = safe(lambda: fit_calib(load_track(state)[0]['picks']), {'on': False, 'n': 0, 'need': CAL_MIN})
    if doc['calib'].get('on'):
        log(f"self-tuning: on from {doc['calib']['n']} graded picks (a={doc['calib']['a']}, b={doc['calib']['b']})")
    changed = prev is None or stable(doc) != stable(prev) or FORCE
    if not doc['matches'] and prev and prev.get('matches') and not FORCE:
        changed = False   # never replace a live board with an empty one

    n_sc = safe(lambda: refresh_scores(state), 0)
    if n_sc:
        log('results from The Odds API for', n_sc, 'competitions')
    track = safe(lambda: track_update(doc, state), None)
    results = safe(lambda: publish_results(state), None)
    if results is not None:
        (OUT_DIR / 'results.json').write_text(json.dumps(results, ensure_ascii=False, separators=(',', ':')))
        rsig = json.dumps([len(results['scores']), len(results['void'])])
        if rsig != state.get('results_sig'):
            state['results_sig'] = rsig
            changed = True
    if track:
        (OUT_DIR / 'track.json').write_text(json.dumps(track, ensure_ascii=False, separators=(',', ':')))
        sig = json.dumps([track['agg'], len(track['picks']), len(track['slips']), track['open'], track['pending']])
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
