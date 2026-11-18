import asyncio, csv, io, json, os, re, sqlite3, smtplib, time, imaplib, email, calendar, sys
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs, unquote
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
try:
    import feedparser
except ImportError:
    feedparser = None
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# Keep user settings and saved apartments outside the installed application.
# Prefer Downloads\RoostData, but support a custom directory for advanced users.
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('ROOST_DATA_DIR', str(Path.home() / 'Downloads' / 'RoostData'))).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# On first launch, copy legacy data without deleting/overwriting the original.
# Legacy layouts: next to server.py, next to Roost.exe, or directly in Downloads.
import shutil
legacy_dirs = [Path.cwd(), ROOT, Path(sys.executable).resolve().parent, Path.home() / 'Downloads']
for filename in ('.env', 'scout.db'):
    target = DATA_DIR / filename
    if not target.exists():
        for legacy_dir in legacy_dirs:
            candidate = legacy_dir / filename
            if candidate.is_file() and candidate.resolve() != target.resolve():
                shutil.copy2(candidate, target)
                break
load_dotenv(DATA_DIR / '.env')
DB = DATA_DIR / 'scout.db'

NY = ZoneInfo('America/New_York')
BOROUGHS = ['Manhattan', 'Brooklyn', 'Queens', 'Bronx', 'Staten Island']
SUBS = ['NYCapartments', 'nyc_sublets', 'LeaseTakeoverNYC', 'NYCroommates', 'subletting']
SOURCES = ['StreetEasy', 'StreetEasy email', 'Reddit', 'RSS', 'Authorized feed', 'Import', 'Demo']
SYNC_LOCK = asyncio.Lock()


class ManagedConnection(sqlite3.Connection):
    """Commit/rollback AND close SQLite handles on leaving a with block.

    sqlite3.Connection.__exit__ alone does not close the connection. Windows
    consequently prevents TemporaryDirectory.cleanup() from removing test DBs.
    """
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def conn():
    c = sqlite3.connect(DB, timeout=20, factory=ManagedConnection)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS listings (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, description TEXT, url TEXT NOT NULL, price INTEGER, beds REAL, borough TEXT, neighborhood TEXT, address TEXT, lat REAL, lng REAL, image TEXT, posted INTEGER, added INTEGER, no_fee INTEGER, laundry INTEGER, washer_dryer INTEGER, pet_friendly INTEGER, kind TEXT, hidden INTEGER DEFAULT 0, favorite INTEGER DEFAULT 0, demo INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS searches (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, filters TEXT NOT NULL, alerts INTEGER DEFAULT 0, created INTEGER);
        CREATE TABLE IF NOT EXISTS sent (search_id INTEGER NOT NULL, listing_id TEXT NOT NULL, PRIMARY KEY(search_id, listing_id));
        CREATE TABLE IF NOT EXISTS commute_cache (listing_id TEXT, destination TEXT, arrival_hour INTEGER, minutes INTEGER, checked INTEGER, PRIMARY KEY(listing_id,destination,arrival_hour));
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS feeds (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
        ''')
        c.execute("INSERT OR IGNORE INTO meta VALUES ('first_sync_complete','false')")
        c.execute('CREATE TABLE IF NOT EXISTS provider_usage (period TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0, last_check INTEGER NOT NULL DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS email_seen (uid TEXT PRIMARY KEY)')
        c.execute('CREATE TABLE IF NOT EXISTS google_usage (period TEXT NOT NULL, kind TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(period, kind))')


def ts(): return int(time.time())


def norm_borough(text):
    x = (text or '').lower()
    for b in BOROUGHS:
        if b.lower() in x: return b
    if re.search(r'\b(lic|astoria|sunnyside|flushing|ridgewood|jackson heights|forest hills|long island city)\b', x): return 'Queens'
    if re.search(r'\b(bushwick|williamsburg|greenpoint|bed.stuy|bed stuy|crown heights|park slope|bay ridge|downtown brooklyn|fort greene|prospect heights)\b', x): return 'Brooklyn'
    if re.search(r'\b(ues|uws|harlem|chelsea|hell.s kitchen|east village|west village|soho|midtown|lower east side|washington heights)\b', x): return 'Manhattan'
    return None


def extract_price(s):
    # Reject unlabeled deposits or historical amounts when possible; manual review is needed.
    m = re.search(r'\$\s*([1-9]\d{2,4})(?:\s*(?:/mo|per month|monthly))?', s or '', re.I)
    return int(m.group(1)) if m else None


def extract_beds(s):
    t = (s or '').lower()
    if re.search(r'\bstudio\b', t): return 0
    m = re.search(r'\b([1-6])\s*(?:br|bd|bed(?:room)?s?)\b', t)
    return float(m.group(1)) if m else None


def clean_item(item, source='Import'):
    title = str(item.get('title') or 'Apartment listing')[:250]
    desc = re.sub('<[^>]+>', ' ', str(item.get('description') or ''))[:7000]
    url = str(item.get('url') or '').strip()
    if not url.startswith(('http://','https://')): raise ValueError('Listing requires a public http(s) URL')
    blob = title + ' ' + desc
    raw_price = item.get('price')
    try: price = int(float(str(raw_price).replace(',', '').replace('$',''))) if raw_price not in (None, '') else extract_price(blob)
    except (TypeError, ValueError): price = extract_price(blob)
    try: beds = float(item['beds']) if item.get('beds') not in (None, '') else extract_beds(blob)
    except (TypeError, ValueError): beds = extract_beds(blob)
    def flag(key, pattern):
        if item.get(key) not in (None, ''): return int(str(item[key]).lower() in ('1','true','yes','y'))
        return int(bool(re.search(pattern, blob, re.I)))
    def num(key):
        try: return float(item[key]) if item.get(key) not in (None,'') else None
        except (TypeError, ValueError): return None
    return dict(id=str(item.get('id') or source + ':' + url)[:600], source=source, title=title, description=desc, url=url,
                price=price, beds=beds, borough=item.get('borough') or norm_borough(blob), neighborhood=item.get('neighborhood') or None,
                address=item.get('address') or None, lat=num('lat'), lng=num('lng'), image=item.get('image') or None,
                posted=int(item.get('posted') or ts()), added=ts(), no_fee=flag('no_fee', r'\b(no[ -]?fee|owner direct|by owner)\b'),
                laundry=flag('laundry',r'\b(laundry|washer|dryer)\b'), washer_dryer=flag('washer_dryer',r'\b(in[ -]?unit|washer/dryer in unit|w/d in unit)\b'),
                pet_friendly=flag('pet_friendly',r'\b(pet[s]? (?:ok|allowed|friendly)|dog[s]? allowed)\b'),
                kind=item.get('kind') or ('sublet' if re.search('sublet|sublease|lease takeover',blob,re.I) else 'rental'),
                demo=int(bool(item.get('demo',False))))


def upsert(items):
    inserted = 0
    keys = ['id','source','title','description','url','price','beds','borough','neighborhood','address','lat','lng','image','posted','added','no_fee','laundry','washer_dryer','pet_friendly','kind','demo']
    sql = f"INSERT OR IGNORE INTO listings ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})"
    with conn() as c:
        for raw, src in items:
            try:
                v = clean_item(raw, src)
                before = c.total_changes
                c.execute(sql,[v[k] for k in keys])
                inserted += c.total_changes - before
            except (ValueError, TypeError, OverflowError): continue
    return inserted


def filters_match(r,f,strict_commute=True):
    if r['demo'] and not f.get('include_demo',False): return False
    if f.get('min_price') is not None and (r['price'] is None or r['price'] < int(f['min_price'])): return False
    if f.get('max_price') is not None and (r['price'] is None or r['price'] > int(f['max_price'])): return False
    if f.get('min_beds') is not None and (r['beds'] is None or r['beds'] < float(f['min_beds'])): return False
    if f.get('max_beds') is not None and (r['beds'] is None or r['beds'] > float(f['max_beds'])): return False
    if f.get('boroughs') and r['borough'] not in f['boroughs']: return False
    if f.get('sources') and r['source'] not in f['sources']: return False
    if f.get('kinds') and r['kind'] not in f['kinds']: return False
    for k in ['no_fee','laundry','washer_dryer','pet_friendly']:
        if f.get(k) and not r[k]: return False
    q = str(f.get('keywords') or '').strip().lower()
    if q and not all(word in ((r['title'] or '')+' '+(r['description'] or '')).lower() for word in q.split()): return False
    if f.get('max_age_hours') and ts()-r['posted'] > float(f['max_age_hours'])*3600: return False
    if f.get('destination') and f.get('max_commute') and strict_commute:
        minutes = r['commute_minutes'] if 'commute_minutes' in r.keys() else None
        if minutes is None or minutes > int(f['max_commute']): return False
    return True


def next_arrival(hour=9):
    d = datetime.now(NY).replace(hour=hour, minute=0, second=0, microsecond=0)
    if d <= datetime.now(NY): d += timedelta(days=1)
    while d.weekday() >= 5: d += timedelta(days=1)
    return int(d.timestamp())


# Google Maps: fail-closed budget protection for this Roost deployment.
# Transit Compute Routes is charged at the Pro tier (5,000 free monthly events
# in the US as of Nov 2026). Do not allow env vars to raise these ceilings.
GOOGLE_MONTHLY_MAX = 4000
GOOGLE_DAILY_MAX = 100
GOOGLE_ROUTES_URL = 'https://routes.googleapis.com/directions/v2:computeRoutes'


def google_limits():
    def cap(name, default, ceiling):
        try: return max(0, min(ceiling, int(os.getenv(name, str(default)))))
        except (ValueError, TypeError): return 0  # invalid config disables outgoing calls
    return {'monthly': cap('GOOGLE_ROUTES_MONTHLY_LIMIT', 4000, GOOGLE_MONTHLY_MAX),
            'daily': cap('GOOGLE_ROUTES_DAILY_LIMIT', 100, GOOGLE_DAILY_MAX)}


def google_usage():
    # Both periods use UTC, with one shared SQLite DB across all app workers.
    now = datetime.now(timezone.utc)
    month, day = now.strftime('%Y-%m'), now.strftime('%Y-%m-%d')
    with conn() as c:
        m = c.execute('SELECT calls FROM google_usage WHERE period=? AND kind=?', (month, 'routes-month')).fetchone()
        d = c.execute('SELECT calls FROM google_usage WHERE period=? AND kind=?', (day, 'routes-day')).fetchone()
    return {'month': m['calls'] if m else 0, 'day': d['calls'] if d else 0,
            'monthly_limit': google_limits()['monthly'], 'daily_limit': google_limits()['daily'],
            'month_utc': month, 'day_utc': day}


def reserve_google_route():
    # Atomically reserve BEFORE sending network traffic. Failed/timeout calls
    # still consume a slot; this errs on the side of avoiding charges.
    limits = google_limits()
    if not limits['monthly'] or not limits['daily']: return False
    now = datetime.now(timezone.utc)
    month, day = now.strftime('%Y-%m'), now.strftime('%Y-%m-%d')
    with closing(conn()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        m = c.execute('SELECT calls FROM google_usage WHERE period=? AND kind=?', (month, 'routes-month')).fetchone()
        d = c.execute('SELECT calls FROM google_usage WHERE period=? AND kind=?', (day, 'routes-day')).fetchone()
        if (m['calls'] if m else 0) >= limits['monthly'] or (d['calls'] if d else 0) >= limits['daily']:
            return False
        for period, kind in ((month,'routes-month'), (day,'routes-day')):
            c.execute('INSERT INTO google_usage(period,kind,calls) VALUES(?,?,1) ON CONFLICT(period,kind) DO UPDATE SET calls=calls+1', (period,kind))
    return True


async def commute_minutes(r, destination, hour, client):
    key = os.getenv('GOOGLE_MAPS_API_KEY')
    if not key: return None
    dest = destination.strip().lower()
    if not dest: return None
    with conn() as c:
        cached = c.execute('SELECT minutes,checked FROM commute_cache WHERE listing_id=? AND destination=? AND arrival_hour=?', (r['id'],dest,hour)).fetchone()
    if cached and ts()-cached['checked'] < 24*3600: return cached['minutes']
    # No guessed neighborhood-centroid commute. Use true listing coordinates / exact address.
    if r['lat'] is not None and r['lng'] is not None:
        origin = {'location': {'latLng': {'latitude': float(r['lat']), 'longitude': float(r['lng'])}}}
    elif r['address']:
        origin = {'address': r['address']}
    else:
        return None
    # Routes API accepts full addresses directly: no Geocoding API calls needed.
    # Reserve immediately before the call, even if it ends up failing.
    if not reserve_google_route(): return None
    arrival = datetime.fromtimestamp(next_arrival(int(hour)), tz=timezone.utc).isoformat().replace('+00:00','Z')
    body = {'origin': origin, 'destination': {'address': destination}, 'travelMode': 'TRANSIT', 'arrivalTime': arrival}
    headers = {'X-Goog-Api-Key': key, 'X-Goog-FieldMask': 'routes.duration', 'Content-Type': 'application/json'}
    try:
        resp = await client.post(GOOGLE_ROUTES_URL, json=body, headers=headers, timeout=20)
        resp.raise_for_status()
        j = resp.json()
        routes = j.get('routes') or []
        if not routes: return None
        duration = routes[0]['duration']  # e.g. "2531s"
        if not isinstance(duration, str) or not duration.endswith('s'): return None
        mins = round(float(duration[:-1])/60)
        with conn() as c:
            c.execute('INSERT OR REPLACE INTO commute_cache VALUES (?,?,?,?,?)', (r['id'],dest,hour,mins,ts()))
        return mins
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, sqlite3.Error):
        return None


def smtp_ready():
    return all(os.getenv(k) for k in ('SMTP_HOST','SMTP_FROM','ALERT_TO'))


def email_matches(search, rows):
    if not rows or not smtp_ready(): return False
    msg=EmailMessage()
    msg['Subject']=f"NYC Apartment Scout: {len(rows)} new matches for {search['name']}"
    msg['From']=os.getenv('SMTP_FROM'); msg['To']=os.getenv('ALERT_TO')
    msg.set_content('New apartment listings matching your saved search:\n\n'+'\n\n'.join(
        f"{r['title']} — ${r['price'] or '?'} / month — {r['borough'] or 'location unverified'}\n{r['url']}" for r in rows))
    host=os.environ['SMTP_HOST']; port=int(os.getenv('SMTP_PORT','587'))
    try:
        with smtplib.SMTP(host,port,timeout=20) as s:
            if os.getenv('SMTP_TLS','true').lower()=='true': s.starttls()
            if os.getenv('SMTP_USER'): s.login(os.getenv('SMTP_USER'),os.getenv('SMTP_PASSWORD',''))
            s.send_message(msg)
        return True
    except (OSError,smtplib.SMTPException): return False


async def run_alerts():
    if not smtp_ready(): return 0
    with conn() as c:
        searches=c.execute('SELECT * FROM searches WHERE alerts=1').fetchall()
        listing_rows=c.execute('SELECT * FROM listings WHERE hidden=0 AND demo=0 ORDER BY posted DESC LIMIT 1500').fetchall()
    total=0
    async with httpx.AsyncClient() as client:
        for search in searches:
            f=json.loads(search['filters']); matching=[]
            for r in listing_rows:
                if not filters_match(r,f,False): continue
                with conn() as c:
                    sent=c.execute('SELECT 1 FROM sent WHERE search_id=? AND listing_id=?',(search['id'],r['id'])).fetchone()
                if sent: continue
                if f.get('destination') and f.get('max_commute'):
                    m=await commute_minutes(r,f['destination'],int(f.get('arrival_hour',9)),client)
                    if m is None or m>int(f['max_commute']): continue
                matching.append(r)
                if len(matching)>=25: break
            if matching and email_matches(search,matching):
                with conn() as c: c.executemany('INSERT OR IGNORE INTO sent VALUES (?,?)',[(search['id'],r['id']) for r in matching])
                total+=len(matching)
    return total


async def reddit_items(client):
    ident=os.getenv('REDDIT_CLIENT_ID'); secret=os.getenv('REDDIT_CLIENT_SECRET')
    if not ident or not secret: return [],'Reddit not configured (approved API access required)'
    ua=os.getenv('REDDIT_USER_AGENT','nyc-apartment-scout/0.1')
    r=await client.post('https://www.reddit.com/api/v1/access_token',auth=(ident,secret),data={'grant_type':'client_credentials'},headers={'User-Agent':ua},timeout=15)
    if r.status_code!=200: return [],f'Reddit auth returned HTTP {r.status_code}'
    token=r.json().get('access_token')
    if not token: return [],'Reddit did not issue access token'
    out=[]
    for sub in SUBS:
        try:
            r=await client.get(f'https://oauth.reddit.com/r/{sub}/new',headers={'Authorization':f'Bearer {token}','User-Agent':ua},params={'limit':60,'raw_json':1},timeout=15)
            if r.status_code!=200: continue
            for child in r.json().get('data',{}).get('children',[]):
                d=child['data']; title=d.get('title',''); body=d.get('selftext','')
                if d.get('removed_by_category') or d.get('is_self') and body in ('[deleted]','[removed]'): continue
                if re.search(r'\b(looking for|seeking|iso|wanted|need a room|searching for)\b',title,re.I): continue
                if not (re.search(r'\$\s*\d{3,5}|\b(?:sublet|sublease|lease takeover|available|renting|room for rent)\b',title+' '+body,re.I)): continue
                out.append(({'id':'reddit:'+d['id'],'title':title,'description':body,'url':'https://www.reddit.com'+d.get('permalink',''),
                             'posted':int(d.get('created_utc',ts())),'image':d.get('thumbnail') if str(d.get('thumbnail','')).startswith('https:') else None},'Reddit'))
        except (httpx.HTTPError,ValueError,KeyError): continue
    return out,f'Reddit fetched {len(out)} potential posts'


async def feed_items(client):
    with conn() as c: rows=c.execute('SELECT * FROM feeds').fetchall()
    result=[]; statuses=[]
    if feedparser is None: return [], ['Install feedparser for RSS integration']
    for row in rows:
        try:
            # Feed URLs are user-entered. Keep this application private/local; enforce allowlists in public deployment.
            resp=await client.get(row['url'],timeout=15,follow_redirects=True)
            resp.raise_for_status(); parsed=feedparser.parse(resp.content)
            for e in parsed.entries[:80]:
                link=e.get('link');
                if not link: continue
                date=e.get('published_parsed') or e.get('updated_parsed')
                posted=int(time.mktime(date)) if date else ts()
                result.append(({'id':'rss:'+str(e.get('id',link)),'title':e.get('title','Listing'),'description':e.get('summary',''), 'url':link,'posted':posted},'RSS'))
            statuses.append(f"{row['name']}: {len(parsed.entries)} items")
        except (httpx.HTTPError,ValueError) as e: statuses.append(f"{row['name']}: unavailable")
    return result,statuses


async def licensed_items(client):
    url=os.getenv('LICENSED_LISTING_FEED_URL','')
    if not url: return [],'Authorized listing feed not configured'
    headers={'Authorization':'Bearer '+os.environ['LICENSED_LISTING_FEED_BEARER']} if os.getenv('LICENSED_LISTING_FEED_BEARER') else {}
    try:
        resp=await client.get(url,headers=headers,timeout=25); resp.raise_for_status()
        data=resp.json(); data=data.get('listings',[]) if isinstance(data,dict) else data
        return [(x,'Authorized feed') for x in data if isinstance(x,dict)],f'Authorized feed fetched {len(data)} rows'
    except (httpx.HTTPError,ValueError,TypeError): return [],'Authorized feed unavailable'


# API.SH provides a documented, third-party StreetEasy API; free allowance is advertised
# as 1,000 credits / calendar month, one credit per search request. We deliberately
# stop well before the free allowance, and do not automatically buy any credits.
APISH_ENDPOINT = 'https://api.api.sh/streeteasy/v1/search'
APISH_AREAS = {'manhattan':'Manhattan','brooklyn':'Brooklyn','queens':'Queens','bronx':'Bronx','staten-island':'Staten Island'}


def apish_usage():
    month=datetime.now(ZoneInfo('UTC')).strftime('%Y-%m')
    with conn() as c:
        r=c.execute('SELECT calls,last_check FROM provider_usage WHERE period=?',(month,)).fetchone()
    return {'month':month,'calls':r['calls'] if r else 0,'last_check':r['last_check'] if r else 0}


def streeteasy_item(d):
    if not isinstance(d,dict) or not d.get('url'): return None
    street=str(d.get('street') or '').strip(); unit=str(d.get('unit') or '').strip()
    address=', '.join(x for x in [street+((' #'+unit) if unit else ''),'New York, NY'] if x) if street else None
    borough=norm_borough(str(d.get('area') or ''))
    # Search areas can be neighborhood names; infer borough only when known.
    title=' '.join(x for x in [street, ('#'+unit if unit else ''), ('— '+str(d.get('area')) if d.get('area') else '')] if x) or 'StreetEasy rental'
    return {'id':'streeteasy:'+str(d.get('id') or d['url']), 'title':title,
            'url':d['url'],'description':' '.join(filter(None,[str(d.get('area') or ''),str(d.get('listed_by') or '')])),
            'price':d.get('price'),'beds':d.get('bedrooms'),'address':address,'lat':d.get('latitude'),
            'lng':d.get('longitude'),'borough':borough,'neighborhood':d.get('area'),
            'image':d.get('image'),'no_fee':d.get('no_fee'),
            # Search endpoint does not contain verified amenity fields. Don't infer these.
            'kind':'rental'}


async def apish_items(client):
    key=os.getenv('APISH_KEY','').strip()
    if not key: return [],'StreetEasy API.SH not configured'
    usage=apish_usage(); budget=min(950,max(0,int(os.getenv('APISH_MONTHLY_BUDGET','900'))))
    areas=[x.strip().lower() for x in os.getenv('APISH_AREAS','manhattan,brooklyn,queens').split(',') if x.strip().lower() in APISH_AREAS]
    areas=list(dict.fromkeys(areas))
    if not areas: return [],'StreetEasy: no valid areas set'
    if usage['calls']+len(areas)>budget: return [],f'StreetEasy free budget reached ({usage["calls"]}/{budget} monthly calls)'
    delay=max(60,int(os.getenv('APISH_INTERVAL_MINUTES','180')))*60
    if ts()-usage['last_check']<delay: return [],f'StreetEasy next API check after {delay//60} min interval ({usage["calls"]}/{budget} calls this month)'
    out=[]; statuses=[]
    for area in areas:
        # Reserve locally before each attempt, so overlapping processes never exceed the cap.
        month=usage['month']
        with conn() as c:
            c.execute('INSERT OR IGNORE INTO provider_usage(period,calls,last_check) VALUES (?,0,0)',(month,))
            curr=c.execute('SELECT calls FROM provider_usage WHERE period=?',(month,)).fetchone()['calls']
            if curr>=budget: break
            c.execute('UPDATE provider_usage SET calls=calls+1,last_check=? WHERE period=?',(ts(),month))
        try:
            resp=await client.get(APISH_ENDPOINT,params={'type':'rental','area':area,'per_page':200,'page':1,'sort':'newest','fresh':1,'raw':0},headers={'x-api-key':key},timeout=60)
            if resp.status_code!=200:
                statuses.append(f'{area}: HTTP {resp.status_code}')
                continue
            data=resp.json()
            raw=data.get('listings',[])
            if not isinstance(raw,list): raw=[]
            for d in raw:
                x=streeteasy_item(d)
                if x:
                    x['borough']=x['borough'] or APISH_AREAS[area]
                    out.append((x,'StreetEasy'))
            statuses.append(f'{area}: {len(raw)} rentals')
        except (httpx.HTTPError,ValueError,TypeError) as e:
            statuses.append(f'{area}: temporarily unavailable')
    return out, 'StreetEasy API.SH '+', '.join(statuses)+f' (up to {budget} monthly calls, no paid fallback)'


class StreetEasyLinks(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.current=None; self.buffer=[]
    def handle_starttag(self,tag,attrs):
        if tag=='a':
            self.current=dict(attrs).get('href',''); self.buffer=[]
    def handle_data(self,data):
        if self.current is not None: self.buffer.append(data)
    def handle_endtag(self,tag):
        if tag=='a' and self.current is not None:
            self.links.append((self.current,' '.join(self.buffer).strip()))
            self.current=None; self.buffer=[]


def canonical_streeteasy_url(url):
    # Only directly exposed listing URLs, never visit or bypass StreetEasy pages.
    for _ in range(3):
        u=urlsplit(url)
        host=(u.hostname or '').lower()
        if host in ('streeteasy.com','www.streeteasy.com'):
            path=unquote(u.path)
            if re.match(r'^/(?:building/[^/]+/[^/?#]+|rental/\d+|rent/\d+)',path,re.I):
                return 'https://streeteasy.com'+path.rstrip('/')
            return None
        # Some legitimate emails wrap their own links in a url= query parameter.
        inner=parse_qs(u.query).get('url') or parse_qs(u.query).get('u')
        if inner: url=inner[0]; continue
        return None
    return None


def parse_streeteasy_message(raw):
    msg=email.message_from_bytes(raw,policy=email.policy.default)
    sender=email.utils.parseaddr(msg.get('From',''))[1].lower()
    if not (sender.endswith('@streeteasy.com') or sender.endswith('.streeteasy.com')): return []
    chunks=[]; plains=[]
    for part in msg.walk():
        if part.get_content_maintype()=='multipart': continue
        if part.get_content_type()=='text/html': chunks.append(part.get_content())
        if part.get_content_type()=='text/plain': plains.append(part.get_content())
    found=[]
    for markup in chunks:
        p=StreetEasyLinks(); p.feed(markup)
        found.extend(p.links)
    for plain in plains:
        found.extend((u,'') for u in re.findall(r'https?://[^\s<>"\']+',plain))
    dt=msg.get('Date','')
    try: timestamp=int(parsedate_to_datetime(dt).timestamp())
    except (ValueError,TypeError,OverflowError): timestamp=ts()
    seen=set(); results=[]
    for url,label in found:
        clean=canonical_streeteasy_url(url)
        if not clean or clean in seen: continue
        seen.add(clean)
        # Email text is often minimal; avoid guessing a price/beds from whole digest.
        label=re.sub(r'\s+',' ',label).strip()
        title=label[:180] if label and len(label)>5 else clean.rsplit('/',1)[-1].replace('-',' ').title()
        results.append(({'id':'streeteasy-email:'+clean,'url':clean,'title':title,
                         'description':label,'posted':timestamp},'StreetEasy email'))
    return results


def imap_ready(): return all(os.getenv(k) for k in ('IMAP_HOST','IMAP_USER','IMAP_PASSWORD'))


def imap_items_sync():
    if not imap_ready(): return [],'StreetEasy email IMAP not configured'
    out=[]
    try:
        with imaplib.IMAP4_SSL(os.environ['IMAP_HOST'],int(os.getenv('IMAP_PORT','993')),timeout=25) as im:
            im.login(os.environ['IMAP_USER'],os.environ['IMAP_PASSWORD'])
            status,_=im.select('INBOX',readonly=True)
            if status!='OK': return [],'StreetEasy inbox not accessible'
            status,data=im.uid('search',None,'SINCE',(datetime.now(ZoneInfo('UTC'))-timedelta(days=14)).strftime('%d-%b-%Y'))
            if status!='OK': return [],'StreetEasy inbox search failed'
            # Bounded reads; no write/delete/move actions on user's mailbox.
            for uid in (data[0] or b'').split()[-100:]:
                uidkey=os.environ['IMAP_USER']+':'+uid.decode('ascii')
                with conn() as c:
                    if c.execute('SELECT 1 FROM email_seen WHERE uid=?',(uidkey,)).fetchone(): continue
                status, message=im.uid('fetch',uid,'(RFC822)')
                if status!='OK': continue
                raw=next((v[1] for v in message if isinstance(v,tuple) and isinstance(v[1],bytes)),None)
                if not raw: continue
                out.extend(parse_streeteasy_message(raw))
                with conn() as c: c.execute('INSERT OR IGNORE INTO email_seen(uid) VALUES(?)',(uidkey,))
            im.logout()
        return out, f'StreetEasy email imported {len(out)} listing links'
    except (imaplib.IMAP4.error,OSError,ValueError) as e:
        return [],'StreetEasy email inbox connection failed (check IMAP settings)'


async def imap_items():
    return await asyncio.to_thread(imap_items_sync)


async def sync_all():
    if SYNC_LOCK.locked(): return {'status':'already running'}
    async with SYNC_LOCK:
        async with httpx.AsyncClient(headers={'User-Agent':'NYCApartmentScout/0.1 (private personal alerts)'}) as client:
            reddit,red_status=await reddit_items(client)
            rss,rss_status=await feed_items(client)
            authorized,auth_status=await licensed_items(client)
            street,street_status=await apish_items(client)
        streetmail,mail_status=await imap_items()
        inserted=upsert(reddit+rss+authorized+street+streetmail)
        with conn() as c:
            initial=c.execute("SELECT value FROM meta WHERE key='first_sync_complete'").fetchone()['value']=='false'
            c.execute("UPDATE meta SET value='true' WHERE key='first_sync_complete'")
            # On initial sync, mark existing matches as seen to avoid stale initial flood.
            if initial:
                for s in c.execute('SELECT * FROM searches WHERE alerts=1').fetchall():
                    c.executemany('INSERT OR IGNORE INTO sent VALUES (?,?)',[(s['id'],r['id']) for r in c.execute('SELECT id FROM listings WHERE demo=0').fetchall()])
        sent=0 if initial else await run_alerts()
        return {'inserted':inserted,'alerts_sent':sent,'reddit':red_status,'rss':rss_status,'licensed':auth_status,'streeteasy':street_status,'streeteasy_email':mail_status,'initial_sync':initial}


async def poller():
    while True:
        await asyncio.sleep(max(5,int(os.getenv('POLL_MINUTES','15')))*60)
        try: await sync_all()
        except Exception as e: print('Sync failed:',str(e))


@asynccontextmanager
async def lifespan(app):
    init_db()
    task=asyncio.create_task(poller())
    yield
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

app=FastAPI(title='NYC Apartment Scout',lifespan=lifespan)

@app.middleware('http')
async def protect(request:Request,call_next):
    token=os.getenv('ADMIN_TOKEN')
    if token and request.url.path.startswith('/api/') and request.headers.get('x-admin-token')!=token:
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail':'Unauthorized. Set your admin token in the browser settings.'},status_code=401)
    return await call_next(request)

@app.get('/')
def homepage(): return FileResponse(ROOT/'static/index.html')

@app.get('/static/{filename}')
def static(filename:str):
    if filename not in ['styles.css','app.js']: raise HTTPException(404)
    return FileResponse(ROOT/'static'/filename)

@app.get('/api/status')
def status():
    with conn() as c:
        counts={r['source']:r['n'] for r in c.execute('SELECT source,count(*) n FROM listings GROUP BY source')}
        feeds=[dict(x) for x in c.execute('SELECT * FROM feeds')]
    return {'sources':counts,'reddit_enabled':bool(os.getenv('REDDIT_CLIENT_ID') and os.getenv('REDDIT_CLIENT_SECRET')),
            'directions_enabled':bool(os.getenv('GOOGLE_MAPS_API_KEY')) and all(google_limits().values()),'google_routes_usage':google_usage(),'email_enabled':smtp_ready(),
            'authorized_feed_enabled':bool(os.getenv('LICENSED_LISTING_FEED_URL')),'streeteasy_api_enabled':bool(os.getenv('APISH_KEY')),'streeteasy_email_enabled':imap_ready(),'streeteasy_usage':apish_usage(),'streeteasy_budget':min(950,max(0,int(os.getenv('APISH_MONTHLY_BUDGET','900')))),'streeteasy_interval':max(60,int(os.getenv('APISH_INTERVAL_MINUTES','180'))),'feeds':feeds,'subreddits':SUBS,'poll_minutes':int(os.getenv('POLL_MINUTES','15'))}

@app.get('/api/listings')
async def listings(request:Request):
    q=request.query_params
    with conn() as c:
        rows=c.execute('SELECT * FROM listings WHERE hidden=0 ORDER BY posted DESC LIMIT 500').fetchall()
    f={}
    for k in ['min_price','max_price','min_beds','max_beds','max_age_hours','max_commute','arrival_hour']:
        if q.get(k):
            try: f[k]=float(q[k])
            except ValueError: pass
    for k in ['no_fee','laundry','washer_dryer','pet_friendly','include_demo']:
        f[k]=q.get(k)=='true'
    for k in ['boroughs','sources','kinds']:
        if q.get(k): f[k]=q[k].split(',')
    f['keywords']=q.get('keywords',''); f['destination']=q.get('destination','').strip()
    selected=[r for r in rows if filters_match(r,f,False)]
    results=[]
    async with httpx.AsyncClient() as client:
        for r in selected[:120]:
            d=dict(r)
            if f.get('destination') and f.get('max_commute'):
                d['commute_minutes']=await commute_minutes(r,f['destination'],int(f.get('arrival_hour',9)),client)
                if d['commute_minutes'] is None or d['commute_minutes']>f['max_commute']: continue
            results.append(d)
    return {'listings':results,'total':len(results),'considered':len(selected),'commute_requires_verified_address':True}

class Search(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    filters:dict
    alerts:bool=False

@app.get('/api/searches')
def get_searches():
    with conn() as c: return [dict(x) | {'filters':json.loads(x['filters'])} for x in c.execute('SELECT * FROM searches ORDER BY id DESC')]

@app.post('/api/searches')
def add_search(s:Search):
    with conn() as c:
        cur=c.execute('INSERT INTO searches(name,filters,alerts,created) VALUES(?,?,?,?)',(s.name,json.dumps(s.filters),int(s.alerts),ts()))
        sid=cur.lastrowid
        # Existing listings are not newly posted. This avoids alert spam for old results.
        c.executemany('INSERT OR IGNORE INTO sent VALUES (?,?)',[(sid,r['id']) for r in c.execute('SELECT id FROM listings').fetchall()])
    return {'id':sid,'saved':True,'email_configured':smtp_ready()}

@app.delete('/api/searches/{sid}')
def del_search(sid:int):
    with conn() as c:
        c.execute('DELETE FROM searches WHERE id=?',(sid,))
        c.execute('DELETE FROM sent WHERE search_id=?',(sid,))
    return {'deleted':True}

class ListingAction(BaseModel):
    field:str
    value:bool

@app.post('/api/listings/{lid:path}/action')
def action(lid:str,data:ListingAction):
    if data.field not in ('favorite','hidden'): raise HTTPException(400)
    with conn() as c: c.execute(f'UPDATE listings SET {data.field}=? WHERE id=?',(int(data.value),lid))
    return {'ok':True}

class Feed(BaseModel):
    name:str=Field(min_length=1,max_length=90)
    url:str

@app.post('/api/feeds')
def add_feed(d:Feed):
    u=urlparse(d.url)
    if u.scheme!='https' or not u.hostname or u.hostname in ('localhost','127.0.0.1'):
        raise HTTPException(400,'Only public HTTPS feeds are supported')
    with conn() as c: c.execute('INSERT OR IGNORE INTO feeds (name,url) VALUES (?,?)',(d.name,d.url))
    return {'ok':True}

@app.delete('/api/feeds/{fid}')
def del_feed(fid:int):
    with conn() as c: c.execute('DELETE FROM feeds WHERE id=?',(fid,))
    return {'ok':True}

@app.post('/api/import')
async def import_csv(request:Request):
    body=await request.body()
    if len(body)>2_000_000: raise HTTPException(413,'CSV too large')
    try:
        reader=csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
        if not {'title','url'}.issubset(set(reader.fieldnames or [])): raise ValueError('CSV requires title,url columns')
        records=[(d,'Import') for _,d in zip(range(2000),reader)]
    except (UnicodeError,csv.Error,ValueError) as e: raise HTTPException(400,str(e))
    return {'inserted':upsert(records)}

@app.post('/api/sync')
async def sync(): return await sync_all()

@app.post('/api/test-email')
def test_email():
    if not smtp_ready(): raise HTTPException(400,'Configure SMTP_HOST, SMTP_FROM and ALERT_TO in .env')
    row={'title':'Test email — NYC Apartment Scout','price':None,'borough':None,'url':'https://example.com'}
    if not email_matches({'name':'test'},[row]): raise HTTPException(502,'SMTP send failed; inspect SMTP settings')
    return {'sent':True}

@app.post('/api/demo')
def demo():
    now=ts()
    examples=[
      {'id':'demo:1','title':'Sunny 1BR · Astoria','description':'Illustrative only. No fee, in-unit washer and dryer. Not an actual available apartment.', 'url':'https://example.com/demo-astoria','price':2650,'beds':1,'borough':'Queens','neighborhood':'Astoria','no_fee':True,'washer_dryer':True,'laundry':True,'posted':now-300,'demo':True},
      {'id':'demo:2','title':'Loft studio · Williamsburg','description':'Illustrative only. Laundry in building, pets welcome. Not an actual available apartment.', 'url':'https://example.com/demo-williamsburg','price':2895,'beds':0,'borough':'Brooklyn','neighborhood':'Williamsburg','laundry':True,'pet_friendly':True,'posted':now-1200,'demo':True},
      {'id':'demo:3','title':'Quiet 1BR · Sunnyside','description':'Illustrative only. No fee. Not an actual available apartment.', 'url':'https://example.com/demo-sunnyside','price':2350,'beds':1,'borough':'Queens','neighborhood':'Sunnyside','no_fee':True,'posted':now-2200,'demo':True},
      {'id':'demo:4','title':'Park-side 2BR · Crown Heights','description':'Illustrative only. In-unit laundry. Not an actual available apartment.','url':'https://example.com/demo-crown-heights','price':3200,'beds':2,'borough':'Brooklyn','neighborhood':'Crown Heights','washer_dryer':True,'laundry':True,'posted':now-4000,'demo':True},
    ]
    return {'inserted':upsert([(x,'Demo') for x in examples])}
