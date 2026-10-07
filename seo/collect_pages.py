# -*- coding: utf-8 -*-
"""Сборщик показов/кликов/позиций по страницам suvvy.ai для SEO-раздела дашборда.

Источники:
  Яндекс.Вебмастер — query-analytics с разрезом по URL: отдаёт только последние
    ~14 дней, поэтому история копится в файле при каждом запуске (день перезаписывается).
  Google Search Console — searchAnalytics dimensions [date, page]: 16 месяцев,
    первый запуск забирает всё, дальше — последние 10 дней.

Выход: pages.json (компактно), формат:
  {"updated": iso, "urls": [..], "yandex": {date: [[i, impr, clicks, pos], ...]},
   "google": {date: [[i, impr, clicks, pos], ...]}}
pos — средняя позиция страницы за день (у Яндекса — взвешенная им самим).

Ключи из окружения:
  YANDEX_WEBMASTER_TOKEN
  GSC_SERVICE_ACCOUNT_JSON (содержимое JSON) или GSC_CLIENT_ID+GSC_CLIENT_SECRET+GSC_REFRESH_TOKEN
Запуск: python collect_pages.py <путь к прежнему pages.json или ''> <куда писать>
"""
import json, os, sys, time, base64, datetime as dt, urllib.request, urllib.parse, urllib.error

HOST = 'suvvy.ai'
WM_HOST_ID = 'https:suvvy.ai:443'
GSC_SITE = 'https://suvvy.ai/'


def http(method, url, body=None, headers=None, form=None):
    h = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode()
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or '{}')
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1)); continue
            raise RuntimeError(f'{method} {url}: {e.code} {e.read()[:300]!r}')


def norm(url):
    """URL Вебмастера приходит путём (/x), GSC — полным адресом. Приводим к пути."""
    if url.startswith('http'):
        url = urllib.parse.urlparse(url).path or '/'
    return url.split('#')[0]


# ---------------- Яндекс ----------------
def yandex():
    tok = os.environ.get('YANDEX_WEBMASTER_TOKEN')
    if not tok:
        print('yandex: нет YANDEX_WEBMASTER_TOKEN, пропуск'); return {}
    H = {'Authorization': 'OAuth ' + tok}
    uid = http('GET', 'https://api.webmaster.yandex.net/v4/user', headers=H)['user_id']
    base = f'https://api.webmaster.yandex.net/v4/user/{uid}/hosts/{WM_HOST_ID}/query-analytics/list'
    out, offset = {}, 0
    while True:
        r = http('POST', base, headers=H, body={
            'offset': offset, 'limit': 500, 'device_type_indicator': 'ALL',
            'text_indicator': 'URL', 'search_location': 'WEB_LOCATION'})
        rows = r.get('text_indicator_to_statistics', [])
        for row in rows:
            u = norm(row['text_indicator']['value'])
            per = {}
            for s in row.get('statistics', []):
                per.setdefault(s['date'], {})[s['field']] = s['value']
            for d, v in per.items():
                out.setdefault(d, {})[u] = (v.get('IMPRESSIONS', 0), v.get('CLICKS', 0), v.get('POSITION', 0))
        offset += len(rows)
        if not rows or offset >= r.get('count', 0):
            break
    print('yandex: дней', len(out), 'адресов', len({u for d in out.values() for u in d}))
    return out


# ---------------- Google ----------------
def gsc_token():
    sa = os.environ.get('GSC_SERVICE_ACCOUNT_JSON')
    if sa:
        from google.oauth2 import service_account  # pip install google-auth
        import google.auth.transport.requests
        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa), scopes=['https://www.googleapis.com/auth/webmasters.readonly'])
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token
    if os.environ.get('GSC_REFRESH_TOKEN'):
        return http('POST', 'https://oauth2.googleapis.com/token', form={
            'client_id': os.environ['GSC_CLIENT_ID'], 'client_secret': os.environ['GSC_CLIENT_SECRET'],
            'refresh_token': os.environ['GSC_REFRESH_TOKEN'], 'grant_type': 'refresh_token'})['access_token']
    return None


def google(since):
    t = gsc_token()
    if not t:
        print('google: нет ключей, пропуск'); return {}
    H = {'Authorization': 'Bearer ' + t}
    url = f'https://www.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(GSC_SITE, safe="")}/searchAnalytics/query'
    end = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    out, start_row = {}, 0
    while True:
        r = http('POST', url, headers=H, body={
            'startDate': since, 'endDate': end, 'dimensions': ['date', 'page'],
            'rowLimit': 25000, 'startRow': start_row, 'dataState': 'all'})
        rows = r.get('rows', [])
        for row in rows:
            d, page = row['keys']
            out.setdefault(d, {})[norm(page)] = (row['impressions'], row['clicks'], round(row['position'], 1))
        if len(rows) < 25000:
            break
        start_row += 25000
    print('google: дней', len(out), 'адресов', len({u for d in out.values() for u in d}))
    return out


def main():
    prev_path, out_path = sys.argv[1], sys.argv[2]
    prev = json.load(open(prev_path, encoding='utf-8')) if prev_path and os.path.exists(prev_path) else {}
    urls = list(prev.get('urls', []))
    idx = {u: i for i, u in enumerate(urls)}

    def unpack(src):
        res = {}
        for d, rows in prev.get(src, {}).items():
            res[d] = {urls[i]: (a, b, c) for i, a, b, c in rows}
        return res

    hist = {'yandex': unpack('yandex'), 'google': unpack('google')}
    since = '2025-06-01' if not hist['google'] else (dt.date.today() - dt.timedelta(days=10)).isoformat()
    for src, fresh in (('yandex', yandex()), ('google', google(since))):
        for d, m in fresh.items():
            hist[src][d] = m   # свежие данные за день целиком заменяют прежние

    packed = {'updated': dt.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'}
    for src in ('yandex', 'google'):
        packed[src] = {}
        for d in sorted(hist[src]):
            arr = []
            for u, (a, b, c) in hist[src][d].items():
                if u not in idx:
                    idx[u] = len(urls); urls.append(u)
                arr.append([idx[u], round(a), round(b), round(c, 1)])
            packed[src][d] = arr
    packed['urls'] = urls
    json.dump(packed, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print('записано', out_path, os.path.getsize(out_path) // 1024, 'КБ')


if __name__ == '__main__':
    main()
