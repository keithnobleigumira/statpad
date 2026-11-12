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
import shutil
import struct
import sys
import time
import unicodedata
import zlib
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
DEFAULT_MODEL = {'hl': HALF_LIFE, 'pw': PREV_W, 'k': K_SHRINK, 'sw': 0.0}
MODEL = dict(DEFAULT_MODEL)
MODEL0 = dict(DEFAULT_MODEL)   # for leagues without shot counts: goals only, so they need their own shrinkage
MODEL_W = None      # the market's share in the blend when tuned; the engine's own default is 0.8


def fit_ratings(games, current_teams, asof=None, init=None, P=None):
    """games: (date, home, away, home goals, away goals, this season?, home shots on target, away shots on target).
    With a shot weight, each side's goals count as (1 - w) x goals + w x shots on target x the league's
    goals-per-shot-on-target, which is steadier than goals alone. Returns ratings plus standard errors."""
    if len(games) < 20:
        return None
    P = P or MODEL
    ref = asof or NOW
    xi = math.log(2) / P['hl']
    K = P['k']
    sw = P.get('sw') or 0.0
    teams = sorted({g[1] for g in games} | {g[2] for g in games})
    ix = {t: i for i, t in enumerate(teams)}
    prev_teams = {g[1] for g in games if not g[5]} | {g[2] for g in games if not g[5]}
    conv = None
    if sw > 0:
        gs = ss = 0
        for g in games:
            if len(g) > 7 and g[6] is not None and g[7] is not None:
                gs += g[3] + g[4]
                ss += g[6] + g[7]
        conv = gs / ss if ss > 100 else None
    W = []
    for g in games:
        d, h, a, hg, ag, cur = g[:6]
        if conv and len(g) > 7 and g[6] is not None and g[7] is not None:
            hg = (1 - sw) * hg + sw * conv * g[6]
            ag = (1 - sw) * ag + sw * conv * g[7]
        age = max(0.0, (ref - d).total_seconds() / 86400)
        W.append((ix[h], ix[a], hg, ag, math.exp(-xi * age) * (1.0 if cur else P['pw'])))
    n = len(teams)
    pa = [0.0] * n
    pd = [0.0] * n
    for t, i in ix.items():
        if prev_teams and t not in prev_teams:
            pa[i], pd[i] = NEW_TEAM_PRIOR
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
                    games.append((d, h, a, int(hg), int(ag), cur, hst, ast))
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
        rt = fit_ratings(games, cur_teams, P=MODEL if cur_n and with_shots >= 0.8 * cur_n else MODEL0)
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
                       'se': {t: v for t, v in (rt or {}).get('se', {}).items() if t in cur_teams},
                       'shots': sum(1 for g in games if g[5] and len(g) > 7 and g[6] is not None),
                       'games': len(games)}
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
    P = MODEL if shots >= 0.8 * len(cur) else MODEL0
    teams = sorted({g[1] for g in cur} | {g[2] for g in cur})
    start = cur[0][0].replace(hour=0, minute=0, second=0, microsecond=0)
    init, fits = None, {}
    w = 0
    while True:   # the ratings at the start of each week of the season, from earlier games only
        cut = min(NOW, start + timedelta(days=7 * w))
        rt = fit_ratings([g for g in games if g[0] < cut], teams, asof=cut, init=init, P=P)
        if rt:
            fits[w], init = rt, rt
        if cut >= NOW:
            break
        w += 1
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
            pH, pD, pA, _ = dc_probs(math.exp(rt['mu'] + rt['home'] + ah + da), math.exp(rt['mu'] + aa + dh))
        hs, as_ = (g[6], g[7]) if len(g) > 7 else (None, None)
        hs, as_ = (None if hs is None else int(hs)), (None if as_ is None else int(as_))
        ds = d.strftime('%Y-%m-%d')
        rows[h].append([ds, pretty(a), 1, hg, ag, r3(pH), r3(pD), r3(pA), hs, as_])
        rows[a].append([ds, pretty(h), 0, ag, hg, r3(pA), r3(pD), r3(pH), as_, hs])
    out = {'app': 'Statpad', 'lk': lk, 'season': season, 'gen': iso(NOW), 'sh': 1 if P is MODEL and shots else 0,
           't': {pretty(t): {'s': series[t], 'r': rows[t]} for t in teams}}
    if lk in SIM_LEAGUES:
        played = {(g[1], g[2]) for g in cur}
        n = len(teams)
        if len(played) == len(cur) and len(cur) <= n * (n - 1):   # no pair has met twice at the same ground
            idx = {t: i for i, t in enumerate(teams)}
            out['teams'] = [pretty(t) for t in teams]
            out['rem'] = [[idx[h], idx[a]] for h in teams for a in teams if h != a and (h, a) not in played]
    return out


def team_files(res):
    t0, out = time.time(), {}
    for lk, (games, season) in res.items():
        if L[lk]['group'] in ('cups', 'intl'):
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
        if L[lk]['group'] in ('cups', 'intl'):
            continue
        d = out.setdefault(lk, {})
        for g in games:
            if g[5]:
                for t in (g[1], g[2]):
                    k = pretty(t)
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


def apply_fx(state, doc):
    """Fatigue and motivation for the coming league games, when the tuning switched them on."""
    lp = state.get('last_played') or {}
    for m in doc['matches']:   # games that have kicked off count as played, before football-data lists them
        if L.get(m['lk'], {}).get('group') in ('cups', 'intl') or parse_iso(m['ko']) > NOW:
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
        if L.get(m['lk'], {}).get('group') in ('cups', 'intl') or parse_iso(m['ko']) <= NOW:
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
AF_LIVE_MAX = int(float(ENV('API_FOOTBALL_LIVE') or 40))     # live-score checks a day, at most
AF_LIVE_MIN = 10        # minutes between live-score checks, at the fastest
AF_LIVE_RESERVE = 25    # live scores stop when this many of the day's calls are left (line-ups and results come first)


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
        elif 'plan' in keys and re.search(r'this date|from \d{4}-\d{2}-\d{2} to', msg):
            pass   # the free plan only serves yesterday, today and tomorrow: just this date is out of reach
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
    """Live scores for Statpad's matches that are in play, from one call that lists every live game. The day's
    live checks are spread over the time games are still in play, so a busy Saturday gets fewer per hour."""
    af = state['af']
    tn = state.get('tn') or {}
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
    if lv.get('at') and (NOW - parse_iso(lv['at'])).total_seconds() / 60 < gap - 1 and not FORCE:
        return 0
    resp, err = af_get('/fixtures', {'live': 'all'}, state, reserve=AF_LIVE_RESERVE)
    if resp is None:
        return 0
    lv['used'] = lv.get('used', 0) + 1
    lv['at'] = iso(NOW)
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
    if 'this date' in (af.get('note') or ''):   # an older version paused on the free plan's date limit: undo that
        af.pop('block_until', None)
        af.pop('note', None)
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


def lam_sd(lk, h, a, leagues, teams, cross):
    """Standard error of log expected goals (home, away) from the ratings' standard errors."""
    if not cross:
        se = (leagues.get(lk) or {}).get('se') or {}
        if h in se and a in se:
            return [round(math.hypot(se[h][0], se[a][1]), 3), round(math.hypot(se[a][0], se[h][1]), 3)]
        return None
    rh, ra = teams.resolve_global(h), teams.resolve_global(a)
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
CREST_PER_RUN = 40
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
    'Anderlecht': 554, 'Boca Juniors': 451, 'Inter Miami': 9568, 'Los Angeles FC': 1616}
# API-Football's competition ids for Statpad's leagues (checked against their logos)
AF_LEAGUE = {'EPL': 39, 'LaLiga': 140, 'BUN': 78, 'SA': 135, 'L1': 61, 'POR': 94, 'ERE': 88, 'BEL': 144, 'KSA': 307, 'ENG2': 40, 'GER2': 79, 'ITA2': 136, 'ESP2': 141, 'FRA2': 62, 'NED2': 89, 'ENG3': 41, 'ENG4': 42, 'SCO1': 179, 'SCO2': 180, 'TUR1': 203, 'GRE1': 197, 'AUT1': 218, 'SUI1': 207, 'DEN1': 119, 'NOR1': 103, 'SWE1': 113, 'POL1': 106, 'ROU1': 283, 'CZE1': 345, 'SRB1': 286, 'IRL1': 357, 'FIN1': 244, 'BRA1': 71, 'ARG1': 128, 'MEX1': 262, 'USA1': 253, 'JPN1': 98, 'CHN1': 169, 'UCL': 2, 'UEL': 3, 'UECL': 848, 'FAC': 45, 'EFLC': 48, 'DFB': 81, 'CDR': 143, 'CI': 137, 'CDF': 66, 'UNL': 5, 'INTF': 10}
CREST_BUDGET_S = 25

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
    w, h, px = png_decode(data)
    return png_encode(*png_shrink(w, h, px, box))


def learn_crests(state, matches):
    """Remember API-Football's team (and competition) id for our names, from fixtures paired with theirs."""
    cr = state.setdefault('crest', {})
    lc = state.setdefault('lgcrest', {})
    for lk, lid in AF_LEAGUE.items():
        lc.setdefault(lk, lid)
    for name, tid in CREST_SEED.items():
        cr.setdefault(name, tid)
    fxs = af_rows(state)
    if not fxs:
        return 0
    lo, hi = fxs[0][0] - timedelta(hours=3), fxs[-1][0] + timedelta(hours=3)
    n = 0
    for m in matches:
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
    return n


def crest_dir():
    d = STATE_DIR / 'crests'
    (d / 'b').mkdir(parents=True, exist_ok=True)
    return d


def fetch_crests(state):
    """Download and shrink crests not on file yet, a few per run, giving up early if the server is unhappy."""
    d = crest_dir()
    bad = state.setdefault('crest_bad', {})
    want = [('t', i) for i in sorted(set((state.get('crest') or {}).values()))] + [('l', i) for i in sorted(set((state.get('lgcrest') or {}).values()))]
    t0, got, fails = time.time(), 0, 0
    for kind, i in want:
        name = f'{i}.png' if kind == 't' else f'l{i}.png'
        if (d / name).exists():
            continue
        key = name[:-4]
        if bad.get(key) and NOW - parse_iso(bad[key]) < timedelta(days=2) and not FORCE:
            continue
        if got >= CREST_PER_RUN or fails >= 3 or time.time() - t0 > CREST_BUDGET_S:
            break
        r = fetch((CREST_TEAM if kind == 't' else CREST_LEAGUE).format(i), tries=1, timeout=12)
        try:
            if r.status != 200 or not r.body:
                raise ValueError(f'HTTP {r.status}')
            big = small_png(r.body, CREST_BIG) if kind == 't' else None
            small = small_png(r.body, CREST_SMALL)
        except Exception as e:
            bad[key] = iso(NOW)
            fails += 1
            log('crest', name, 'not saved:', repr(e)[:80])
            continue
        if big:
            (d / 'b' / name).write_bytes(big)
        (d / name).write_bytes(small)
        bad.pop(key, None)
        got += 1
        time.sleep(0.15)
    return got


def publish_crests(state, doc):
    """Copy every crest on file to the site and list the ones this page needs: name -> id (negative = not on file yet)."""
    d = crest_dir()
    out = OUT_DIR / 'crests'
    (out / 'b').mkdir(parents=True, exist_ok=True)
    for p in list(d.glob('*.png')) + list((d / 'b').glob('*.png')):
        q = out / p.relative_to(d)
        if not q.exists() or q.stat().st_size != p.stat().st_size:
            shutil.copyfile(p, q)
    cr, lc = state.get('crest') or {}, state.get('lgcrest') or {}
    names = set()
    for m in doc['matches']:
        names.update((m['h'], m['a']))
    for lg in doc['leagues'].values():
        names.update(r[0] for r in lg.get('table') or [])
    sign = lambda i, f: i if (d / f).exists() else -i
    doc['crests'] = {n: sign(cr[n], f'{cr[n]}.png') for n in sorted(names) if n in cr}
    doc['lgcrests'] = {k: sign(lc[k], f'l{lc[k]}.png') for k in sorted(doc['leagues']) if k in lc}
    return len(doc['crests'])


def refresh_crests(state, doc):
    n_new = learn_crests(state, doc['matches'])
    got = fetch_crests(state)
    n = publish_crests(state, doc)
    if n_new or got:
        log(f'crests: {n_new} new team ids, {got} downloaded, {n} on this board')
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
        ls = lam_sd(f['lk'], f['h'], f['a'], leagues, teams, cross) if lam else None
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
        if L[f['lk']]['group'] == 'intl':
            ls = None
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
        if ls:
            matches[-1]['ls'] = ls
        if MODEL_W and L[f['lk']]['group'] not in ('cups', 'intl') and not cross:
            matches[-1]['wm'] = MODEL_W

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
            if lg.get('shots'):
                rec['sh'] = 1   # ratings use shots on target as well as goals
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
    t: a.top && a.top.pLo != null ? a.tier : undefined, pl: a.top && a.top.pLo != null ? a.top.pLo : undefined,
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
            if 't' in e:
                rec['t'] = e['t']
                rec['pl'] = round(e['pl'], 4)
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
        t = (rec['t'] if 't' in rec else tier_of(rec['pp'])) or '-'
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
if (E.setCalib) E.setCalib(null);   // the replay shows the engine's own, uncorrected chances
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
  for (const v of E.value([{ m, a }], 0.03)) out.value.push([m.id, v.s.key, v.s.price, v.s.p]);
  const d = m.ko.slice(0, 10); (byDay[d] = byDay[d] || []).push({ m, a });
}
for (const [d, list] of Object.entries(byDay)) {
  out.slips[d] = ['C', 'B', 'A'].map((t) => { const sl = E.buildSlip(list, t, inp.target);
    return { t, f: sl.feasible, o: sl.odds, p: sl.prob, legs: sl.legs.map((l) => [l.m.id, l.s.key]) }; });
}
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
    """(date, home, away, home goals, away goals, home shots on target, away SoT, prices, kick-off) for one season."""
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
                    rows.append((d, h, a, int(hg), int(ag), fnum(col(row, 'HST')), fnum(col(row, 'AST')),
                                 fd_market(row, None), uk_to_utc(d, col(row, 'Time'))))   # football-data times are UK time
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
    games = [(r[0], r[1], r[2], r[3], r[4], False, r[5], r[6]) for r in prev_rows] + \
            [(r[0], r[1], r[2], r[3], r[4], True, r[5], r[6]) for r in cur]
    start = cur[0][0]
    teams = sorted({r[1] for r in cur} | {r[2] for r in cur})
    init, fits = None, {}
    for w in sorted({(r[0] - start).days // 7 for r in cur}):
        if w < skip:
            continue
        cut = start + timedelta(days=7 * w)
        rt = fit_ratings([g for g in games if g[0] < cut], teams, asof=cut, init=init, P=P)
        if rt:
            fits[w], init = rt, rt
    return cur, start, fits


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


RHO_DC = -0.07   # the engine's Dixon-Coles correction


def dc_probs(lh, la, n=11):
    """Home, draw, away and over 2.5 from the Dixon-Coles scoreline grid (as the engine builds it)."""
    ph, pa = [math.exp(-lh)], [math.exp(-la)]
    for k in range(1, n):
        ph.append(ph[-1] * lh / k)
        pa.append(pa[-1] * la / k)
    c00, c01, c10, c11 = 1 - lh * la * RHO_DC, 1 + lh * RHO_DC, 1 + la * RHO_DC, 1 - RHO_DC
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


def game_loss(lh, la, hg, ag):
    """Log loss on the result (1X2) and on over/under 2.5 for one game."""
    H, D, A, O = dc_probs(lh, la)
    r = H if hg > ag else D if hg == ag else A
    o = O if hg + ag > 2 else 1 - O
    return -math.log(max(1e-9, r)), -math.log(max(1e-9, o))


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
TUNE_BUDGET_S = float(ENV('STATPAD_TUNE_BUDGET') or 420)
TUNE_GRID = {'sw': [0.0, 0.15, 0.3, 0.45, 0.6, 0.75], 'hl': [120.0, 180.0, 270.0, 365.0, 540.0],
             'pw': [0.4, 0.6, 0.8, 1.0], 'k': [1.5, 2.5, 3.5, 5.0, 6.0, 8.0]}
W_GRID = [0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]
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
    for r, lh, la, _sh, _sa in season_preds(cur_rows, prev_rows, P, sf=sf):
        if full:
            mk = r[7]
            if mk and mk.get('p'):
                rh, ra = rd.get((r[0], r[1], r[2]), (None, None))
                rows.append((lh, la, r[3], r[4], mk['p'][0], mk['p'][2], (mk.get('ou') or {}).get('pO'),
                             rh, ra, (lk, r[0].strftime('%Y-%m-%d'), r[1], r[2])))
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
    c1, c2, c3, c4 = (season_code(NOW, i) for i in (1, 2, 3, 4))
    global _TUNE_LKS
    train, check, tl, cl = [], [], [], []
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
        for _round in range(2):
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
        best0 = dict(best, sw=0.0)
        bs0 = score(best0)
        for name in ('k', 'hl', 'pw'):
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
        tr_rows, tr_jobs, tr_keys = flat(_pmap(pool, _tune_job, [(i, best, True) for i in ti]))
        ck_def, cd_jobs, cd_keys = flat(_pmap(pool, _tune_job, [(i, DEFAULT_MODEL, True) for i in ci]))
        ck_new, cn_jobs, cn_keys = flat(_pmap(pool, _tune_job, [(i, best, True) for i in ci]))
        ck_0 = flat(_pmap(pool, _tune_job, [(i, best0, True) for i in ci]))[0]
    finally:
        if pool:
            pool.shutdown()
    lam_tr = run_node(NODE_FIT, [[r[4], r[5], r[6]] for r in tr_rows])
    lam_ck = run_node(NODE_FIT, [[r[4], r[5], r[6]] for r in ck_new])
    if not lam_tr or not lam_ck or len(ck_def) != len(ck_new):
        log('tuning: market fit failed')
        return None
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
    fx = safe_fx(tr_rows, lam_tr, tr_jobs, tr_keys, rows_on, lam_ck, (cn_jobs, cn_keys) if p_on else (cd_jobs, cd_keys), w_live, t0)
    r5 = lambda x: round(x, 5)
    out = {'at': iso(NOW), 'train': [season_name(c3), season_name(c2)], 'check': season_name(c1), 'check_code': c1,
           'leagues': len(check), 'games': len(ck_new), 'evals': evals, 'secs': round(time.time() - t0),
           'search_secs': round(t_search), 'finished': finished,
           'default': DEFAULT_MODEL, 'params': best, 'on': p_on, 'w': w_best, 'w_on': w_on,
           'params0': best0, 'on0': on0, 'fx': fx,
           'll': {'model': [r5(m_def[0]), r5(m_new[0])], 'model_ou': [r5(m_def[1]), r5(m_new[1])],
                  'site': [r5(b_old[0]), r5(b_after[0])], 'site_ou': [r5(b_old[1]), r5(b_after[1])],
                  'market': [r5(mkt[0]), r5(mkt[1])], 'model0': [r5(m_0[0]), r5(m_0[1])]}}
    log(f"tuning: {evals} settings tried in {out['search_secs']} s on {season_name(c3)}-{season_name(c2)}; best {best}, market share {w_best}. "
        f"Checked on {season_name(c1)}: model {m_def[0]:.4f} -> {m_new[0]:.4f}, site {b_old[0]:.4f} -> {b_after[0]:.4f} (1X2 log loss); "
        f"settings {'on' if p_on else 'kept old'}, market share {'on' if w_on else 'kept 0.8'}")
    return out


def apply_tuning(state):
    global MODEL, MODEL0, MODEL_W
    t = state.get('tune') or {}
    MODEL = dict(t['params']) if t.get('on') and t.get('params') else dict(DEFAULT_MODEL)
    MODEL0 = dict(t['params0']) if t.get('on0') and t.get('params0') else dict(DEFAULT_MODEL)
    MODEL_W = t.get('w') if t.get('w_on') else None
    f = t.get('fx') or {}
    FX['rest'] = tuple(f['rest']['p']) if (f.get('rest') or {}).get('on') else None
    FX['dead'] = tuple(f['dead']['p']) if (f.get('dead') or {}).get('on') else None
    FX['open'] = tuple(f['open']['p']) if (f.get('open') or {}).get('on') else None


def safe_fx(*a):
    try:
        return fit_fx(*a)
    except Exception as e:
        log('tuning: fatigue and motivation fit failed', repr(e))
        return None


def fit_fx(tr_rows, lam_tr, tr_jobs, tr_keys, ck_rows, lam_ck, ck_sj, w, t_start):
    """Fit fatigue and motivation on the training seasons, after blending with the market as the site does;
    each goes on only if it also lowers the error on the check season."""
    t0 = time.time()
    late = time.time() - t_start > TUNE_BUDGET_S * 1.4   # the run is long already: motivation waits for next month
    st_tr = {} if late else run_past_stakes(tr_jobs, tr_keys) or {}
    st_ck = {} if late or not st_tr else run_past_stakes(*ck_sj) or {}

    def pre(rows, lams, st):
        out = []
        for row, (mh, ma) in zip(rows, lams):
            lh, la, hg, ag = row[:4]
            rh, ra, key = row[7], row[8], row[9]
            dh, da = st.get(key, (None, None))
            bh, ba = math.exp(w * math.log(mh) + (1 - w) * math.log(lh)), math.exp(w * math.log(ma) + (1 - w) * math.log(la))
            out.append((bh, ba, hg, ag, rh, ra, dh, da, sum(game_loss(bh, ba, hg, ag))))
        return out
    P_tr, P_ck = pre(tr_rows, lam_tr, st_tr), pre(ck_rows, lam_ck, st_ck)

    def loss(P, F):
        tot, n_aff = 0.0, 0
        for bh, ba, hg, ag, rh, ra, dh, da, base in P:
            xh, xa = fx_shift(rh, ra, dh, da, F)
            if xh or xa:
                tot += sum(game_loss(bh * math.exp(xh), ba * math.exp(xa), hg, ag))
                n_aff += 1
            else:
                tot += base
        return tot / max(1, len(P)), n_aff

    out, F = {'games': [len(P_tr), len(P_ck)], 'measured': [len(st_tr), len(st_ck)]}, {}
    for name, grid in (('rest', FX_REST_GRID), ('dead', FX_DEAD_GRID), ('open', FX_OPEN_GRID)):
        if name != 'rest' and not st_tr:
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
        or ('fx' not in t and due(state, 'tune', 6))   # fatigue and motivation came later: fit them once


def public_tune(state):
    t = state.get('tune')
    if not t:
        return None
    return {k: t[k] for k in ('at', 'train', 'check', 'leagues', 'games', 'default', 'params', 'on', 'params0', 'on0', 'w', 'w_on', 'll', 'fx') if k in t}


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
        sf = season_fits(cur_rows, prev_rows, MODEL)
        rd = rest_days(cur_rows, prev_rows) if use_fx else {}
        if FX['dead'] or FX['open']:
            j, k = past_stakes_jobs(lk, sf)
            jobs += j
            keys += k
        for r, lh, la, sh, sa in season_preds(cur_rows, prev_rows, MODEL, sf=sf):
            d, h, a, hg, ag, _hs, _as, mkt, ko = r
            if not mkt:
                continue
            mid = f"{lk}-{ko:%Y%m%d}-{slug(h)}-{slug(a)}"
            m = {'id': mid, 'lk': lk, 'ko': iso(ko), 'h': h, 'a': a, 'lm': [round(lh, 4), round(la, 4)],
                 'ls': [round(sh, 3), round(sa, 3)], 'mkt': mkt, 'xl': False, 'fr': False, 'clash': False}
            if MODEL_W:
                m['wm'] = MODEL_W
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


def run_backtest(state):
    t0 = time.time()
    seasons, label = load_bt_season()
    ms, res = bt_matches(seasons)
    t1 = time.time()
    if len(ms) < 200:
        log('backtest: not enough games with prices', len(ms))
        return None
    eng = run_node(NODE_BT, {'target': TRACK_TARGET, 'matches': ms})
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
    for mid, key, price, p in eng['value']:
        hs, as_ = res[mid]
        won = bool(settle(key, hs, as_))
        v = agg['value']
        v[0] += 1; v[1] += int(won); v[2] = round(v[2] + (price - 1 if won else -1), 3)
    for d, sl in eng['slips'].items():
        for s_ in sl:
            if not s_['f'] or not s_['legs']:
                continue
            won = all(settle(k, *res[i]) for i, k in s_['legs'])
            a = agg['slips'][s_['t']]
            a[0] += 1; a[1] += int(won); a[2] = round(a[2] + (s_['o'] - 1 if won else -1), 3); a[3] = round(a[3] + s_['p'], 4)
    out = {'app': 'Statpad', 'season': label, 'at': iso(NOW), 'games': len(ms), 'leagues': len({m['lk'] for m in ms}),
           'skip_weeks': BT_SKIP_WEEKS, 'target': TRACK_TARGET, 'model': MODEL, 'wm': MODEL_W or W_DEFAULT, 'agg': agg,
           'by_league': dict(sorted(by_lg.items(), key=lambda x: -x[1][0]))}
    log(f"backtest: {label}, {len(ms)} games in {out['leagues']} leagues (ratings {t1 - t0:.0f} s, total {time.time() - t0:.0f} s)")
    return out

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
    model_sig = json.dumps([MODEL, MODEL0, MODEL_W, FX], sort_keys=True)
    if model_sig != state.get('model_sig'):
        state['model_sig'] = model_sig
        retuned = True

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
    safe(lambda: refresh_crests(state, doc), 0)
    safe(lambda: refresh_stakes(state, doc), None)
    n_fx = safe(lambda: apply_fx(state, doc), 0)
    if n_fx:
        log(f'fatigue and motivation: {n_fx} games adjusted')
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
    if due(state, 'backtest', BT_EVERY_H) or not state.get('backtest') or retuned:
        last['backtest'] = iso(NOW)
        bt = safe(lambda: run_backtest(state), None)
        if bt:
            state['backtest'] = bt
            changed = True
    if state.get('backtest'):
        (OUT_DIR / 'backtest.json').write_text(json.dumps(dict(state['backtest'], tune=public_tune(state)), ensure_ascii=False, separators=(',', ':')))
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
