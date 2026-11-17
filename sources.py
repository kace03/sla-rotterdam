"""Read public rental pages. Default behavior previews data and never sends email."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from curl_cffi import requests
from eligibility import STUDENT_CHECK_VERSION, at_home_restrictions, student_assessment, property_text

ATHOME = 'https://athomevastgoed.nl/woningaanbod'


def checked_url(url, host):
    parts = urlparse(url)
    if parts.scheme != 'https' or parts.hostname != host or parts.username or parts.password:
        raise ValueError('Unexpected listing or pagination URL')
    return url


def amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace('.', '').replace(',', '.')) if ',' in str(value) else float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def bedrooms(value):
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        return None
    return int(value)


def is_match(item):
    rent, beds = item.get('rent'), item.get('bedrooms')
    return (item.get('available') is not False and item.get('city', '').strip().casefold() == 'rotterdam'
            and item.get('studentOnly') is not True
            and (not valid_rent(rent) or rent <= 2000)
            and (not valid_bedrooms(beds) or beds >= 2))


def valid_rent(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def valid_bedrooms(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def listing_warnings(item):
    notes = list(item.get('collectionWarnings', []))
    if not valid_rent(item.get('rent')):
        notes.append('Could not read the rent: the EUR 2,000 maximum has not been verified.')
    if not valid_bedrooms(item.get('bedrooms')):
        notes.append('Could not read the bedroom count: the minimum of two bedrooms has not been verified.')
    if item.get('available', True) is None:
        notes.append('Could not verify current availability. Check the listing with the agent.')
    if item.get('studentOnly') is None and 'studentOnly' in item:
        notes.append('Could not verify whether this property is restricted to students.')
    return notes


def at_home_page(html):
    marker = "SET_PROPERTIES_COLLECTION',"
    index = html.find(marker)
    if index < 0:
        raise ValueError('At Home listing collection missing; this is not an empty successful scan')
    value, _ = json.JSONDecoder().raw_decode(html[index + len(marker):].lstrip())
    if not isinstance(value.get('data'), list):
        raise ValueError('At Home collection changed shape')
    result = []
    for raw in value['data']:
        slug = raw.get('status', {}).get('attribute', {}).get('slug')
        if slug not in ('new', 'active'):
            continue
        url = checked_url(raw['url'], 'athomevastgoed.nl')
        if not re.fullmatch(r'\d+', str(raw.get('id'))):
            raise ValueError('At Home listing ID missing')
        price = amount(raw.get('ah_price'))
        result.append(dict(id=f"athome:{raw['id']}", source='At Home Vastgoed',
            title=raw.get('listingAddressIn') or raw.get('street') or '',
            city=raw.get('location', {}).get('name') or '', rent=price,
            bedrooms=bedrooms(raw.get('no_bedrooms')), url=url,
            **student_assessment(property_text(raw)),
            costNote=f"EUR {raw.get('ah_price', '?')} {raw.get('ah_price_includes_short', '')} / month"))
    next_url = value.get('next_page_url')
    if next_url:
        checked_url(next_url, 'athomevastgoed.nl')
        if urlparse(next_url).path != '/woningaanbod':
            raise ValueError('Unexpected At Home pagination path')
    return result, next_url


class Collector:
    def __init__(self, cache_path=Path('.collector-cache.json'), fetcher=None):
        self.session = requests.Session(impersonate='chrome') if fetcher is None else None
        self.fetcher = fetcher
        self.cache_path = cache_path
        self.original_cache = cache_path.read_text(encoding='utf8') if cache_path.exists() else '{}'
        try:
            self.cache = json.loads(self.original_cache)
        except (ValueError, TypeError):
            self.cache = {}
        if not isinstance(self.cache, dict):
            self.cache = {}
        self.last_request = 0
        self.issues = []

    def get(self, url):
        if self.fetcher:
            return self.fetcher(url)
        wait = 1 - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.monotonic()
        response = self.session.get(url, timeout=20, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f'HTTP {response.status_code}; no successful scan')
        if 'text/html' not in response.headers.get('content-type', ''):
            raise RuntimeError('Unexpected response format')
        if len(response.content) > 3_000_000 or re.search(r'<title>\s*(Just a moment|One moment,?\s*please|Access Denied)', response.text, re.I):
            raise RuntimeError('Access challenge or oversized response')
        return response.text

    def athome(self):
        all_items, url, seen_pages = [], ATHOME, set()
        while url:
            if url in seen_pages or len(seen_pages) >= 10:
                raise ValueError('Unexpected pagination; scan incomplete')
            seen_pages.add(url)
            items, url = at_home_page(self.get(url))
            all_items.extend(items)
        result = list({item['id']: item for item in all_items}.values())
        for item in result:
            if not is_match(item):
                continue
            detail = self.detail_or_fallback(item['url'], at_home_restrictions, city=item['city'])
            item.update(studentOnly=detail.get('studentOnly'), studentRestriction=detail.get('studentRestriction', ''))
            if detail.get('collectionWarnings'):
                item['collectionWarnings'] = list(detail['collectionWarnings'])
        return result

    def save_cache(self):
        # Keep only current/recent data so a hosted runner cache stays small.
        cache = {key: val for key, val in self.cache.items()
                 if isinstance(val, dict) and time.time() - val.get('time', 0) < 86400 * 7}
        encoded = json.dumps(cache, sort_keys=True)
        changed = encoded != self.original_cache
        self.cache_path.write_text(encoded, encoding='utf8')
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf8') as stream:
                stream.write(f'cache_changed={str(changed).lower()}\n')
        return changed

    def cached_detail(self, url, parser, ttl=900):
        saved = self.cache.get(url)
        if (isinstance(saved, dict) and isinstance(saved.get('item'), dict)
                and saved['item'].get('studentCheckVersion') == STUDENT_CHECK_VERSION
                and time.time() - saved.get('time', 0) < ttl):
            return dict(saved['item'])
        item = parser(self.get(url), url)
        self.cache[url] = {'time': int(time.time()), 'item': item}
        return item

    def detail_or_fallback(self, url, parser, city=''):
        try:
            return self.cached_detail(url, parser)
        except Exception as error:
            # One inaccessible/broken property page must not discard the other properties.
            self.issues.append({'url': url, 'reason': type(error).__name__})
            saved = self.cache.get(url, {}).get('item')
            if isinstance(saved, dict):
                row = dict(saved)
                row['collectionWarnings'] = ['Could not refresh this property page; details below are from an older cached read.']
                return row
            return dict(title=urlparse(url).path.rstrip('/').split('/')[-1], city=city, rent=None,
                        bedrooms=None, url=url, rental=True, status='Unknown', openForResponses=None,
                        conditionsNote='', collectionWarnings=['Could not read this property page; only its discovery link is available.'])

    def wonenhartje(self):
        from agencies import hartje_detail
        from urllib.parse import urljoin
        url = 'https://wonenhartje.nl/rotterdam/aanbod-rotterdam/'
        soup = BeautifulSoup(self.get(url), 'html.parser')
        links = list(dict.fromkeys(urljoin(url, a['href']) for a in soup.select('a[href]')
            if re.match(r'^https://wonenhartje\.nl/object/[^/]+-rotterdam-h\d+/?$', urljoin(url, a['href']))))
        if not links or len(links) > 70:
            title = soup.title.get_text(' ', strip=True) if soup.title else 'No title'
            raise ValueError(f'Wonen Hartje discovery returned {len(links)} listing links ({title[:80]}); no complete scan')
        result = []
        for link in links:
            row = self.detail_or_fallback(link, hartje_detail, city='Rotterdam')
            if not row['rental']:
                continue
            identifier = re.search(r'-(h\d+)/?$', urlparse(link).path)[1]
            row.update(id=f'wonenhartje:{identifier}', source='Wonen Hartje',
                available=row['openForResponses'], costNote=f"EUR {row['rent']} per month; {row['status']}")
            result.append(row)
        return result

    def woonzeker(self):
        from agencies import zeker_detail
        from xml.etree import ElementTree
        url = 'https://woonzeker.com/__sitemap__/properties-0.xml'
        # The advertised public sitemap provides the rental links; details carry availability.
        if self.fetcher:
            xml = self.fetcher(url)
        else:
            wait = 1 - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            response = self.session.get(url, timeout=20, allow_redirects=False)
            if response.status_code != 200 or len(response.content) > 2_000_000:
                raise ValueError('Woonzeker sitemap unavailable')
            xml = response.text
        tree = ElementTree.fromstring(xml)
        links = list(dict.fromkeys(n.text for n in tree.iter() if n.tag.endswith('}loc') and n.text
            and re.match(r'^https://woonzeker\.com/huur/woningen/[^/]+/?$', n.text)))
        if not links or len(links) > 100:
            raise ValueError('Woonzeker discovery changed; no complete scan')
        result = []
        for link in links:
            row = self.detail_or_fallback(link, zeker_detail)
            row.update(id='woonzeker:' + urlparse(link).path.rstrip('/').split('/')[-1],
                source='Woonzeker', available=row['openForResponses'],
                costNote=f"EUR {row['rent']} per month; check service charges and utilities")
            result.append(row)
        return result
