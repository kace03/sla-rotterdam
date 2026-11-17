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

ATHOME = 'https://athomevastgoed.nl/woningaanbod'
PARARIUS = 'https://www.pararius.com/apartments/rotterdam/0-1800'


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
    return (item.get('available', True) and item.get('city', '').strip().casefold() == 'rotterdam'
            and isinstance(item.get('rent'), (int, float)) and 0 < item['rent'] <= 1800
            and isinstance(item.get('bedrooms'), int) and item['bedrooms'] >= 2)


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
            costNote=f"EUR {raw.get('ah_price', '?')} {raw.get('ah_price_includes_short', '')} / month"))
    next_url = value.get('next_page_url')
    if next_url:
        checked_url(next_url, 'athomevastgoed.nl')
        if urlparse(next_url).path != '/woningaanbod':
            raise ValueError('Unexpected At Home pagination path')
    return result, next_url


def ld_objects(html):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        parsed = json.loads(script.string or script.get_text())
        roots = parsed if isinstance(parsed, list) else [parsed]
        for root in roots:
            if isinstance(root, dict):
                yield root
                yield from (node for node in root.get('@graph', []) if isinstance(node, dict))


def pararius_search(html):
    for root in ld_objects(html):
        entity = root.get('mainEntity', {})
        if isinstance(entity, dict) and entity.get('@type') == 'ItemList':
            result = []
            for row in entity.get('itemListElement', []):
                item = row.get('item', {})
                url = checked_url(item.get('url', ''), 'www.pararius.com')
                # Restrict to the Rotterdam rental URLs from this actual search response.
                match = re.match(r'^/(?:apartment|house|studio|room)-for-rent/rotterdam/([a-f0-9]+)/', urlparse(url).path)
                if not match:
                    continue
                result.append({'id': f'pararius:{match[1]}', 'url': url,
                               'rent': amount(item.get('offers', {}).get('price'))})
            return result
    raise ValueError('Pararius search listing data missing; source was not checked')


def pararius_detail(html, url):
    for root in ld_objects(html):
        types = root.get('@type', [])
        types = [types] if isinstance(types, str) else types
        if 'RealEstateListing' not in types:
            continue
        address, offer = root.get('address', {}), root.get('offers', {})
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        value = amount(offer.get('price')) if offer.get('priceCurrency') == 'EUR' else None
        identifier = re.search(r'/rotterdam/([a-f0-9]+)/', urlparse(url).path)
        if not identifier:
            raise ValueError('Unexpected Pararius detail URL')
        return dict(id=f'pararius:{identifier[1]}', source='Pararius',
            title=root.get('name') or address.get('streetAddress') or '',
            city=address.get('addressLocality') or '', rent=value,
            bedrooms=bedrooms(root.get('numberOfBedrooms')), url=url,
            available=offer.get('availability') not in ('https://schema.org/OutOfStock', 'https://schema.org/SoldOut', 'https://schema.org/Discontinued'),
            costNote=f'EUR {value} per month as listed; additional costs may apply')
    raise ValueError('Pararius detail listing data missing')


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
        if len(response.content) > 3_000_000 or re.search(r'<title>\s*(Just a moment|Access Denied)', response.text, re.I):
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
        return list({item['id']: item for item in all_items}.values())

    def pararius(self, detail_limit=30):
        search = pararius_search(self.get(PARARIUS))
        result, fetched = [], 0
        for row in search:
            if row['rent'] is None or row['rent'] > 1800:
                continue
            saved = self.cache.get(row['url'])
            fresh = (isinstance(saved, dict) and isinstance(saved.get('item'), dict)
                     and time.time() - saved.get('time', 0) < 21600)
            if fresh:
                item = dict(saved['item'])
            else:
                if fetched >= detail_limit:
                    raise RuntimeError('Detail request limit reached; source scan incomplete')
                item = pararius_detail(self.get(row['url']), row['url'])
                self.cache[row['url']] = {'time': int(time.time()), 'item': item}
                fetched += 1
            # Detail and search may momentarily disagree; use the higher advertised price.
            if item['rent'] is not None:
                item['rent'] = max(item['rent'], row['rent'])
                item['costNote'] = f"EUR {item['rent']} per month; check the page for service charges and utilities"
            result.append(item)
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
        if isinstance(saved, dict) and isinstance(saved.get('item'), dict) and time.time() - saved.get('time', 0) < ttl:
            return dict(saved['item'])
        item = parser(self.get(url), url)
        self.cache[url] = {'time': int(time.time()), 'item': item}
        return item

    def wonenhartje(self):
        from agencies import hartje_detail
        from urllib.parse import urljoin
        url = 'https://wonenhartje.nl/rotterdam/aanbod-rotterdam/'
        soup = BeautifulSoup(self.get(url), 'html.parser')
        links = list(dict.fromkeys(urljoin(url, a['href']) for a in soup.select('a[href]')
            if re.match(r'^https://wonenhartje\.nl/object/[^/]+-rotterdam-h\d+/$', urljoin(url, a['href']))))
        if not links or len(links) > 70:
            raise ValueError('Wonen Hartje discovery changed; no complete scan')
        result = []
        for link in links:
            row = self.cached_detail(link, hartje_detail)
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
            row = self.cached_detail(link, zeker_detail)
            row.update(id='woonzeker:' + urlparse(link).path.rstrip('/').split('/')[-1],
                source='Woonzeker', available=row['openForResponses'],
                costNote=f"EUR {row['rent']} per month; check service charges and utilities")
            result.append(row)
        return result



