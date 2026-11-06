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
  ODDS_MIN_INTERVAL     minutes between price refreshes per competition, default 30
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
EVENTS_EVERY = 60      # minutes
API_KEY = (ENV('ODDS_API_KEY') or '').strip()
REGIONS = [r.strip() for r in (ENV('ODDS_REGIONS') or 'eu').split(',') if r.strip()]
MARKETS = [m.strip() for m in (ENV('ODDS_MARKETS') or 'h2h').split(',') if m.strip()]
MIN_INTERVAL = max(5.0, float(ENV('ODDS_MIN_INTERVAL') or 30))
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

FD_DIV = {v['fd']: k for k, v in L.items() if v['fd']}
X_COUNTRY = {'Austria': 'AUT1', 'Switzerland': 'SUI1', 'Denmark': 'DEN1', 'Norway': 'NOR1', 'Sweden': 'SWE1',
             'Poland': 'POL1', 'Romania': 'ROU1', 'Ireland': 'IRL1', 'Finland': 'FIN1', 'Brazil': 'BRA1',
             'Argentina': 'ARG1', 'Mexico': 'MEX1', 'USA': 'USA1', 'Japan': 'JPN1', 'China': 'CHN1'}
OA_KEY = {k: lk for lk, v in L.items() for k in v['oa']}
CUP_WORDS = ('uefa', 'cup', 'pokal', 'copa', 'coppa', 'coupe', 'taca', 'beker', 'trophy', 'shield', 'super_cup')
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


def fetch(url, params=None, tries=3):
    full = url + ('?' + urlencode(params) if params else '')
    if OFFLINE:
        p = Path(OFFLINE) / offline_name(url)
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
            req = Request(full, headers={'User-Agent': 'Statpad/1.0 (GitHub Actions data refresh)', 'Accept-Encoding': 'identity'})
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
        elif any(w in key for w in CUP_WORDS) or SPORTS_MODE == 'all':
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

    # paid price refreshes, paced to the budget
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
    # minutes until credits reset
    y, m = NOW.year, NOW.month
    reset = datetime(y, m, min(RESET_DAY, 28), tzinfo=UTC)
    if reset <= NOW:
        reset = datetime(y + (m == 12), (m % 12) + 1, min(RESET_DAY, 28), tzinfo=UTC)
    minutes_left = max(60.0, (reset - NOW).total_seconds() / 60)
    # demand at the base interval (competitions more than 2 days away refresh 4x less often)
    def interval_for(nxt, scale):
        far = (nxt - NOW) > timedelta(hours=48)
        return MIN_INTERVAL * scale * (4 if far else 1)
    demand = sum(cost * minutes_left / interval_for(nxt, 1.0) for _, nxt in active)
    usable = max(0.0, remaining - 5)
    scale = max(1.0, demand / usable) if usable > 0 else float('inf')
    state['odds_interval'] = round(MIN_INTERVAL * scale, 1) if math.isfinite(scale) else None
    if not math.isfinite(scale):
        return 'out of credits'
    done = 0
    for key, nxt in sorted(active, key=lambda kv: (L[plan[kv[0]]]['group'] not in ('big', 'cups'), kv[1])):
        due_at = (api.get(key) or {}).get('odds_at')
        if not FORCE and due_at and NOW - parse_iso(due_at) < timedelta(minutes=interval_for(nxt, scale) - 1):
            continue
        if (state.get('quota') or {}).get('remaining') is not None and state['quota']['remaining'] < cost + 5:
            break
        data = oa_get(f'/sports/{key}/odds', {'regions': ','.join(REGIONS), 'markets': ','.join(MARKETS),
                                              'oddsFormat': 'decimal', 'dateFormat': 'iso',
                                              'commenceTimeFrom': frm, 'commenceTimeTo': to}, state)
        if not isinstance(data, list):
            continue
        cur = {e['id']: e for e in (api.get(key) or {}).get('events', [])}
        for ev in data:
            cur[ev.get('id')] = parse_api_event(ev)
        blob = api.setdefault(key, {})
        blob['events'] = list(cur.values())
        blob['odds_at'] = iso(NOW)
        done += 1
    return f'{done} competitions priced'


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
        hist = list(old.get('hist') or [])
        if mkt and mkt.get('avg'):
            pt = [iso(NOW)[:16] + 'Z', *[round(x, 2) for x in mkt['avg']]]
            if not hist or hist[-1][1:] != pt[1:] or NOW - parse_iso(hist[-1][0]) > timedelta(hours=6):
                hist.append(pt)
            hist = hist[-60:]
        matches.append({
            'id': mid, 'lk': f['lk'], 'ko': iso(ko), 'h': pretty(f['h']), 'a': pretty(f['a']),
            'ph': ph[0] if ph else None, 'pa': pa[0] if pa else None,
            'fh': ph[2] if ph else None, 'fa': pa[2] if pa else None,
            'dh': dom_h, 'da': dom_a,
            'lm': lam, 'xl': cross, 'mkt': mkt, 'hist': hist, 'clash': clash, 'tbc': tbc, 'ks': ks,
        })

    # competitions no source covered this run keep their previous (or starter) fixtures until kick-off
    covered = {m['lk'] for m in matches}
    carried = [m for m in prev_list if m['lk'] not in covered and lo <= parse_iso(m['ko']) <= hi]
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

    if due(state, 'results', RESULTS_EVERY) or not state.get('leagues'):
        res = safe(load_results, {})
        if res:
            state['leagues'] = safe(lambda: compute_leagues(res, Teams()), state.get('leagues') or {})
            last['results'] = iso(NOW)
            src['results'] = {'ok': True, 'at': iso(NOW), 'leagues': len(res)}
        else:
            src['results'] = {'ok': False, 'at': iso(NOW)}
        log('results:', len(res), 'leagues')

    if due(state, 'fixtures', FIXTURES_EVERY) or 'of_fixtures' not in state:
        fx, ok = safe(load_fd_fixtures, ([], False))
        if ok:
            state['fd_fixtures'] = fx
            last['fixtures'] = iso(NOW)
        ofx, ok2 = safe(load_openfootball, ([], False))
        if ok2:
            state['of_fixtures'] = ofx
        src['fixtures'] = {'ok': ok or ok2, 'at': iso(NOW), 'count': len(fx) + len(ofx)}
        log('football-data fixtures:', len(fx), '| openfootball schedule fixtures:', len(ofx))

    msg = safe(lambda: refresh_odds_api(state), 'error')
    src['odds_api'] = {'ok': msg not in ('no key', 'paused', 'out of credits'), 'at': iso(NOW), 'note': msg}
    log('odds:', msg, '| credits left:', (state.get('quota') or {}).get('remaining'))

    doc = build(state, prev, seed)
    changed = prev is None or stable(doc) != stable(prev) or FORCE
    if not doc['matches'] and prev and prev.get('matches') and not FORCE:
        changed = False   # never replace a live board with an empty one
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
