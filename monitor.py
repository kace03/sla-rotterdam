"""GitHub-hosted Rotterdam rental alerts. Preview by default; secrets stay out of state."""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import random
import smtplib
import ssl
import sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sources import Collector, is_match, listing_warnings, valid_rent, valid_bedrooms

SOURCES = ('athome', 'wonenhartje', 'woonzeker')
STATE_BRANCH = 'monitor-state'
STATE_PATH = '.monitor/state.json'


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def empty_state():
    return {'version': 1, 'initialized': [], 'listings': {}}


class GitHubState:
    def __init__(self, environ=os.environ, request=None):
        self.repo = environ.get('GITHUB_REPOSITORY', '')
        self.token = environ.get('GITHUB_TOKEN', '')
        self.commit = environ.get('GITHUB_SHA', '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repo) or not self.token or not re.fullmatch(r'[a-f0-9]{40}', self.commit):
            raise ValueError('Run live mode from the prepared GitHub workflow')
        self.sha = None
        self.last_encoded = None
        self.request = request or self._request

    def _request(self, method, path, body=None, missing_ok=False):
        req = Request('https://api.github.com/repos/' + self.repo + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json',
                     'User-Agent': 'RotterdamRentalAlerts'}, method=method)
        try:
            with urlopen(req, timeout=25) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            raise RuntimeError(f'GitHub state request failed (HTTP {error.code}); no automatic retry') from None

    def load(self):
        ref = self.request('GET', '/git/ref/heads/' + STATE_BRANCH, missing_ok=True)
        if not ref:
            return empty_state()
        obj = self.request('GET', '/contents/' + STATE_PATH + '?ref=' + STATE_BRANCH, missing_ok=True)
        if not obj:
            raise ValueError('State branch exists but its state file is missing; refusing to reset history')
        value = json.loads(base64.b64decode(obj['content']))
        if value.get('version') != 1 or not isinstance(value.get('listings'), dict) or not isinstance(value.get('initialized'), list):
            raise ValueError('Invalid stored state; refusing to reset history')
        if 'enabled' in value and not isinstance(value['enabled'], bool):
            raise ValueError('Invalid monitor switch; refusing to check or send')
        self.sha = obj['sha']
        self.last_encoded = json.dumps(value, sort_keys=True)
        return value

    def save(self, state):
        encoded = json.dumps(state, sort_keys=True)
        if encoded == self.last_encoded:
            return
        if not self.sha:
            self.request('POST', '/git/refs', {'ref': 'refs/heads/' + STATE_BRANCH, 'sha': self.commit})
        body = {'message': 'Record rental alert progress [skip ci]', 'branch': STATE_BRANCH,
                'content': base64.b64encode(encoded.encode()).decode()}
        if self.sha:
            body['sha'] = self.sha
        result = self.request('PUT', '/contents/' + STATE_PATH, body)
        self.sha = result['content']['sha']
        self.last_encoded = encoded


def validate_address(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value):
        raise ValueError('Set a single valid email address in the account secrets')
    return value


def listing_lines(item):
    rent = f"EUR {item['rent']:.2f} per month" if valid_rent(item.get('rent')) else 'Unknown (could not read)'
    beds = str(item['bedrooms']) if valid_bedrooms(item.get('bedrooms')) else 'Unknown (could not read)'
    notes = listing_warnings(item)
    return [str(item.get('title') or 'Property listing'), f"City: {item.get('city') or 'Unknown'}",
            'Advertised rent: ' + rent, 'Bedrooms: ' + beds,
            *(['WARNING: ' + note for note in notes]),
            *(['SCAN NOTICE: ' + note for note in item.get('scanWarnings', [])]),
            str(item.get('costNote') or ''), str(item.get('conditionsNote') or ''),
            'Service charges and utilities may be extra. Check eligibility and availability with the agent.',
            '', item['url'], '', 'Source: ' + item['source']]


class GmailSender:
    def __init__(self, environ=os.environ, smtp_factory=smtplib.SMTP_SSL):
        self.sender = validate_address(environ.get('GMAIL_SENDER'))
        self.recipient = validate_address(environ.get('ALERT_RECIPIENT'))
        if not self.sender.lower().endswith('@gmail.com'):
            raise ValueError('Use the dedicated Gmail account as the sender')
        password = re.sub(r'\s+', '', environ.get('GMAIL_APP_PASSWORD', ''))
        if not re.fullmatch(r'[A-Za-z0-9]{16}', password):
            raise ValueError('Set the 16-character Gmail app password, not the normal account password')
        self.connection = smtp_factory('smtp.gmail.com', 465, context=ssl.create_default_context(), timeout=25)
        self.connection.login(self.sender, password)

    def send(self, item=None):
        msg = EmailMessage()
        msg['From'], msg['To'] = self.sender, self.recipient
        msg['Subject'] = ('Rotterdam rental alerts: connection test' if item is None else
                          'Rotterdam rental listing: details need checking' if listing_warnings(item) else 'New Rotterdam rental listing')
        if item is None:
            body = 'The rental alert program can send email. This is a connection test, not a property listing.'
        else:
            body = '\n'.join(listing_lines(item))
        msg.set_content(body)
        refused = self.connection.send_message(msg, from_addr=self.sender, to_addrs=[self.recipient])
        if refused:
            raise RuntimeError('Gmail did not accept the recipient')

    def send_digest(self, items, failed_sources=()):
        msg = EmailMessage()
        msg['From'], msg['To'] = self.sender, self.recipient
        msg['Subject'] = f'Rotterdam rental alerts: {len(items)} current matches'
        lines = ['Current candidate listings, including properties previously emailed. Unknown details are flagged below.', '']
        if failed_sources:
            lines.extend(['Incomplete scan. Could not check: ' + ', '.join(failed_sources), ''])
        for item in items:
            lines.extend([*listing_lines(item), ''])
        if not items:
            lines.append('No current matches found on the sources successfully checked.')
        lines.append('Utilities/service charges may be extra. Confirm availability and eligibility with the agent.')
        msg.set_content('\n'.join(lines))
        if self.connection.send_message(msg, from_addr=self.sender, to_addrs=[self.recipient]):
            raise RuntimeError('Gmail did not accept the recipient')

    def close(self):
        try:
            self.connection.quit()
        except Exception:
            self.connection.close()


def collect_all(collector):
    scans = {}
    for source in SOURCES:
        try:
            issue_start = len(collector.issues)
            listings = getattr(collector, source)()
            scans[source] = {'listings': listings, 'warnings': collector.issues[issue_start:]}
            print(f'{source}: {len(listings)} listings, {sum(is_match(item) for item in listings)} candidates (including unverified details)', flush=True)
        except Exception as error:
            # Exception text from mail libraries can contain private addresses. Only source errors/types are recorded here.
            detail = str(error)[:250] if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__
            scans[source] = {'error': detail}
            print(f'{source}: source check failed ({detail})', flush=True)
    coverage_notes = [f'Could not check {source} during this scan; results do not cover that website.'
                      for source, scan in scans.items() if 'error' in scan]
    for source, scan in scans.items():
        if scan.get('warnings'):
            coverage_notes.append(f"{source}: {len(scan['warnings'])} property pages could not be refreshed.")
    for scan in scans.values():
        for item in scan.get('listings', []):
            item['scanWarnings'] = list(coverage_notes)
    collector.save_cache()
    return scans


def send_current(scans, mode, sender_factory=GmailSender, choose=random.choice):
    # These manual replays intentionally ignore history and never change it.
    items = {item['id']: item for scan in scans.values() for item in scan.get('listings', []) if is_match(item)}
    failed = [source for source, scan in scans.items() if 'error' in scan]
    failed.extend(f"{source} ({len(scan['warnings'])} property pages could not be refreshed)"
                  for source, scan in scans.items() if scan.get('warnings'))
    if not items and (mode == 'send_random' or all('error' in scan for scan in scans.values())):
        return 0
    sender = sender_factory()
    try:
        if mode == 'send_all':
            sender.send_digest(list(items.values()), failed)
        else:
            sender.send(choose(list(items.values())))
    finally:
        sender.close()
    return 1


def update_state(state, scans):
    for source, scan in scans.items():
        if 'error' in scan:
            continue
        first = source not in state['initialized']
        for item in scan['listings']:
            if not is_match(item) or item['id'] in state['listings']:
                continue
            # Stored records contain only public listing data, never mail addresses, passwords or tokens.
            state['listings'][item['id']] = {'status': 'baseline' if first else 'pending',
                'foundAt': utc_now(), 'item': item}
        if first:
            state['initialized'].append(source)
    return state


def deliver(state, store, sender_factory=GmailSender, limit=None, allowed_sources=None, allowed_ids=None):
    pending = [(key, value) for key, value in state['listings'].items()
               if value['status'] == 'pending' and (allowed_sources is None or key.split(':', 1)[0] in allowed_sources)
               and (allowed_ids is None or key in allowed_ids)][:limit]
    if not pending:
        return 0
    sender = sender_factory()  # Authenticate before claiming any messages.
    sent = 0
    try:
        for key, entry in pending:
            entry['status'] = 'sending'
            store.save(state)  # Durable claim before SMTP; an ambiguous failure is held for review.
            sender.send(entry['item'])
            entry['status'], entry['sentAt'] = 'sent', utc_now()
            store.save(state)
            sent += 1
    finally:
        sender.close()
    return sent


def scan_report(scans, state):
    report = {}
    for source, scan in scans.items():
        if 'error' in scan:
            continue
        ids = {item['id'] for item in scan['listings'] if is_match(item)}
        known = set(state['listings']) if state is not None else set()
        baseline = state is not None and source not in state['initialized']
        report[source] = {'new': len(ids-known) if state is not None and not baseline else 0,
                          'seen': len(ids & known), 'baseline': len(ids-known) if baseline else 0,
                          'compared': state is not None}
    return report


def summary(scans, mode, state=None, sent=0, report=None):
    now = datetime.now(timezone.utc)
    try:
        checked_at = now.astimezone(ZoneInfo('Europe/Amsterdam')).strftime('%d %B %Y at %H:%M:%S %Z')
    except ZoneInfoNotFoundError:
        checked_at = now.strftime('%d %B %Y at %H:%M:%S UTC')
    lines = ['# Rotterdam rental monitor', '', f'Report generated: **{checked_at}**.', '',
             f'Mode: **{mode}**. Emails accepted by Gmail in this run: **{sent}**.']
    if not scans:
        if mode == 'Automatic checks enabled':
            lines.extend(['', '**Monitoring is enabled. This START command did not check any websites.**',
                          'Each scheduled check creates a separate Apartment alerts run. Open that run to confirm what was checked.',
                          '[View automatic runs](https://github.com/kace03/sla-rotterdam/actions/workflows/monitor.yml?query=event%3Aschedule)',
                          '[Check for new listings now](https://github.com/kace03/sla-rotterdam/actions/workflows/email-check.yml)'])
        elif mode == 'test_email':
            lines.extend(['', 'Email connection test only. No property websites were checked.'])
        else:
            lines.extend(['', 'No property websites were checked in this run.'])
    if scans:
        report = report if report is not None else scan_report(scans, state)
        checked = sum('error' not in scan for scan in scans.values())
        lines.extend(['', f'Websites checked successfully: **{checked}/{len(scans)}**.'])
        if mode == 'preview':
            lines.extend(['', '**Preview only:** websites were checked, but no listing emails were sent and history was not changed.'])
        if mode in ('live', 'preview') and all(row['compared'] for row in report.values()) and report:
            new = sum(row['new'] for row in report.values())
            seen = sum(row['seen'] for row in report.values())
            baseline = sum(row['baseline'] for row in report.values())
            lines.extend(['', f'**New candidates found: {new}.** Previously seen candidates: **{seen}**.',
                          f'Existing candidates establishing a first-scan baseline: **{baseline}**.'])
            if not new:
                lines.append('**No new candidates found on the websites successfully checked.**')
        lines.extend(['', '| Source | Listings read | Candidates | New | Previously seen | Student-only excluded | Result |',
                      '| --- | ---: | ---: | ---: | ---: | ---: | --- |'])
    for name, scan in scans.items():
        rows = scan.get('listings', [])
        if 'error' in scan:
            lines.append(f'| {name} | Unknown | Unknown | Unknown | Unknown | Unknown | Unavailable; not checked successfully |')
        else:
            status = f"Partial details: {len(scan['warnings'])} property pages could not be refreshed" if scan.get('warnings') else 'Read successfully'
            counts = report.get(name, {})
            new = counts.get('new', 'Not compared') if counts.get('compared') else 'Not compared'
            seen = counts.get('seen', 'Not compared') if counts.get('compared') else 'Not compared'
            excluded = sum(row.get('studentOnly') is True for row in rows)
            lines.append(f'| {name} | {len(rows)} | {sum(is_match(row) for row in rows)} | {new} | {seen} | {excluded} | {status} |')
    unverified = sum(bool(listing_warnings(row)) for scan in scans.values() for row in scan.get('listings', []) if is_match(row))
    if unverified:
        lines.extend(['', f'Candidates with unverified details: **{unverified}**. Emails identify what could not be checked.'])
    failed = [name for name, scan in scans.items() if 'error' in scan]
    if failed:
        outcome = 'PARTIAL COVERAGE' if len(failed) < len(scans) else 'NO SOURCE COVERAGE'
        lines.extend(['', f'**{outcome}:** could not check ' + ', '.join(failed) + '.',
                      'Successful sources can still produce alerts. Failed sources are retried on the next normal check; their old pending alerts are not sent.'])
    if state:
        held = sum(row['status'] == 'sending' for row in state['listings'].values())
        pending = sum(row['status'] == 'pending' for row in state['listings'].values())
        lines.extend(['', f'Pending listing emails: **{pending}**. Uncertain deliveries held for review: **{held}**.'])
    lines.extend(['', 'Funda is not connected. A five-minute schedule is best effort. Source access from this runner must be verified.',
                  'Candidates must be in Rotterdam and have no known closed/unavailable status. Known rent must be at most EUR 2,000 and known bedrooms at least two; missing details are emailed with warnings.',
                  'The first successful live scan of each source establishes a baseline and sends no existing listings.'])
    text = '\n'.join(lines) + '\n'
    Path('run-summary.md').write_text(text, encoding='utf8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf8') as stream:
            stream.write(text)


def scan_exit_code(scans):
    failed = [name for name, scan in scans.items() if 'error' in scan]
    for name in failed:
        print(f'::warning title=Rental source unavailable::{name} could not be checked. Coverage is incomplete; see the run summary and source log.', flush=True)
    for name, scan in scans.items():
        if scan.get('warnings'):
            print(f"::warning title=Property details incomplete::{name}: {len(scan['warnings'])} property pages could not be refreshed; see the run summary. Other properties were still processed.", flush=True)
    # A source outage is not a crash of the entire monitor. Complete outages still fail.
    return int(not scans or len(failed) == len(scans))


def main(environ=os.environ):
    mode = environ.get('RUN_MODE', 'preview')
    if mode == 'scheduled':
        mode = 'live' if environ.get('SEND_ALERTS') == 'true' else 'preview'
    if mode not in ('preview', 'live', 'test_email', 'send_all', 'send_random', 'start', 'stop'):
        raise ValueError('Unknown run mode')
    # Every hosted command reads the same persistent switch before scraping or SMTP.
    store = GitHubState(environ) if environ.get('GITHUB_TOKEN') else None
    if mode != 'preview' and not store:
        raise ValueError('Use the hosted controls for email and start/stop commands')
    state = store.load() if store else None
    if mode in ('start', 'stop'):
        state['enabled'] = mode == 'start'
        store.save(state)
        summary({}, 'Automatic checks enabled' if state['enabled'] else 'Stopped: no website checks or email', state)
        return 0
    if state is not None and not state.get('enabled', True):
        summary({}, 'Stopped: no website checks or email', state)
        return 0
    if mode == 'test_email':
        sender = GmailSender(environ)
        try:
            sender.send()
        finally:
            sender.close()
        summary({}, mode, sent=1)
        return 0
    if mode == 'live' and environ.get('SEND_ALERTS') != 'true':
        raise ValueError('Live alerts are disabled. Enable SEND_ALERTS only after reviewing the preview and receiving a test email.')
    collector = Collector()
    if mode in ('send_all', 'send_random'):
        collector.cache = {}  # Manual requests recheck details rather than reuse the 15-minute cache.
    scans = collect_all(collector)
    report = scan_report(scans, state)  # Count against history before this run changes it.
    sent = 0
    try:
        if mode in ('send_all', 'send_random'):
            sent = send_current(scans, mode, lambda: GmailSender(environ))
        if mode == 'live':
            update_state(state, scans)
            store.save(state)
            # Revalidate pending matches against the current successful source scan before emailing.
            current = {item['id']: item for scan in scans.values() for item in scan.get('listings', [])}
            for key, row in state['listings'].items():
                if row['status'] != 'pending':
                    continue
                source = key.split(':', 1)[0]
                if 'error' in scans.get(source, {}):
                    continue
                if key not in current and scans.get(source, {}).get('warnings'):
                    continue  # Absence from an incomplete scan does not prove expiration.
                if key not in current or not is_match(current[key]):
                    row['status'] = 'expired'
                else:
                    row['item'] = current[key]
            store.save(state)
            # Only send pending records from sources that were checked successfully this run.
            # Held records stay untouched until their Sent-folder outcome is reviewed.
            allowed = {name for name, scan in scans.items() if 'error' not in scan}
            pending = [row for key, row in state['listings'].items()
                       if row['status'] == 'pending' and key.split(':', 1)[0] in allowed and key in current]
            if pending:
                sent = deliver(state, store, lambda: GmailSender(environ), allowed_sources=allowed, allowed_ids=set(current))
    finally:
        summary(scans, mode, state, sent, report=report)
    return scan_exit_code(scans)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # Prevent SMTP exceptions from printing a recipient address or authentication detail into public logs.
        print(f'Run stopped ({type(error).__name__}). Check the setup and held-delivery status; no blind retries.', file=sys.stderr)
        raise SystemExit(1)
