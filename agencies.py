"""Read-only live access test for Wonen Hartje and Woonzeker. Never sends email."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from eligibility import student_assessment, listing_page_text, property_text



def decode_nuxt(html):
    script = BeautifulSoup(html, 'html.parser').find('script', id='__NUXT_DATA__')
    if not script:
        raise ValueError('Woonzeker listing payload missing')
    table, cache, visiting = json.loads(script.get_text()), {}, set()

    def read(index):
        if not isinstance(index, int) or index < 0:
            return None
        if index in cache:
            return cache[index]
        if index in visiting or index >= len(table):
            raise ValueError('Invalid or cyclic Nuxt data')
        visiting.add(index)
        value = table[index]
        if isinstance(value, dict):
            result = {key: read(reference) for key, reference in value.items()}
        elif isinstance(value, list):
            if value and isinstance(value[0], str):
                if value[0] in ('Reactive', 'ShallowReactive', 'Ref', 'ShallowRef', 'EmptyRef', 'EmptyShallowRef'):
                    result = read(value[1])
                elif value[0] == 'Set':
                    result = [read(reference) for reference in value[1:]]
                else:
                    raise ValueError('Unknown Nuxt data wrapper')
            else:
                result = [read(reference) for reference in value]
        else:
            result = value
        visiting.remove(index)
        cache[index] = result
        return result
    return read(0)


def hartje_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    fields = {}
    for row in soup.select('.mkdf-property-content'):
        label, value = row.select_one('.mkdf-property-label'), row.select_one('.mkdf-property-value')
        if label and value:
            fields[label.get_text(' ', strip=True)] = value.get_text(' ', strip=True)
    specs = {}
    for label in soup.select('.mkdf-label-text'):
        name = label.get_text(' ', strip=True)
        text = label.parent.parent.get_text(' ', strip=True)
        if text.startswith(name):
            specs[name] = text[len(name):].strip()
    price = soup.select_one('.wh-property-price')
    title = soup.title or soup.find('h1') or soup.select_one('.mkdf-property-title-left')
    if not title:
        raise ValueError('Wonen Hartje property schema missing')
    digits = re.search(r'\d[\d.,]*', price.get_text()) if price else None
    rent = float(digits[0].replace('.', '').replace(',', '.')) if digits else None
    beds = re.match(r'^(\d+)\b', specs.get('Slaapkamers', fields.get('Slaapkamers', '')))
    status = fields.get('Status', 'Unknown')
    page_label = soup.select_one('.mkdf-property-title-right')
    if status in ('', 'Empty', 'Unknown') and page_label and page_label.get_text(' ', strip=True).casefold() == 'beschikbaar':
        status = 'Beschikbaar (page label; status field empty)'
    description = listing_page_text(html)
    return {'title': title.get_text(' ', strip=True).removesuffix(' - Wonen Hartje'), 'city': 'Rotterdam' if '-rotterdam-' in urlparse(url).path else '',
        'url': url, 'rent': rent, 'bedrooms': int(beds[1]) if beds else None,
        'rental': specs.get('Verhuurvorm', 'te huur').casefold() == 'te huur', 'status': status,
        'openForResponses': None if status in ('', 'Empty', 'Unknown') else status.casefold().startswith('beschikbaar'),
        **student_assessment(description), 'conditionsNote': ''}


def zeker_detail(html, url):
    root = decode_nuxt(html)
    candidates = [value for value in root.get('data', {}).values()
                  if isinstance(value, dict) and 'handover' in value and 'address' in value]
    if len(candidates) != 1:
        raise ValueError('Woonzeker property data missing or ambiguous')
    value = candidates[0]
    status = value.get('status') or {}
    rent = (value.get('handover') or {}).get('price')
    beds = ((value.get('characteristic') or {}).get('layout') or {}).get('number_of_bedrooms')
    availability = (value.get('stage'), status.get('code'))
    known_closed = any(part not in (None, '', 'unknown', 'available') for part in availability)
    available = False if known_closed else True if availability == ('available', 'available') else None
    return {'title': value.get('title', ''), 'city': value.get('address', {}).get('location', ''),
        'url': url, 'rent': rent, 'bedrooms': beds, 'rental': '/huur/woningen/' in urlparse(url).path,
        'status': status.get('label', value.get('stage', 'Unknown')),
        'openForResponses': available,
        **student_assessment(property_text(value)), 'conditionsNote': ''}
