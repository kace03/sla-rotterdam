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
import smtplib
import ssl
import sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from sources import Collector, is_match

SOURCES = ('athome', 'pararius', 'wonenhartje', 'woonzeker')
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
        msg['Subject'] = 'Rotterdam rental alerts: connection test' if item is None else 'New Rotterdam rental listing'
        if item is None:
            body = 'The rental alert program can send email. This is a connection test, not a property listing.'
        else:
            body = '\n'.join([item['title'], f"City: {item['city']}",
                f"Advertised rent: EUR {item['rent']:.2f} per month", f"Bedrooms: {item['bedrooms']}",
                item.get('costNote', ''), item.get('conditionsNote', ''),
                'Service charges and utilities may be extra. Check eligibility and availability with the agent.',
                '', item['url'], '', 'Source: ' + item['source']])
        msg.set_content(body)
        refused = self.connection.send_message(msg, from_addr=self.sender, to_addrs=[self.recipient])
        if refused:
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
            listings = getattr(collector, source)()
            scans[source] = {'listings': listings}
            print(f'{source}: {len(listings)} listings, {sum(is_match(item) for item in listings)} eligible matches', flush=True)
        except Exception as error:
            # Exception text from mail libraries can contain private addresses. Only source errors/types are recorded here.
            detail = str(error)[:250] if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__
            scans[source] = {'error': detail}
            print(f'{source}: source check failed ({detail})', flush=True)
    collector.save_cache()
    return scans


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


def deliver(state, store, sender_factory=GmailSender, limit=5, allowed_sources=None):
    pending = [(key, value) for key, value in state['listings'].items()
               if value['status'] == 'pending' and (allowed_sources is None or key.split(':', 1)[0] in allowed_sources)][:limit]
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


def summary(scans, mode, state=None, sent=0):
    lines = ['# Rotterdam rental monitor', '', f'Mode: **{mode}**. Emails accepted by Gmail in this run: **{sent}**.', '',
             '| Source | Listings read | Eligible matches | Result |', '| --- | ---: | ---: | --- |']
    for name, scan in scans.items():
        rows = scan.get('listings', [])
        lines.append(f"| {name} | {len(rows)} | {sum(is_match(row) for row in rows)} | {'Failed' if 'error' in scan else 'Read successfully'} |")
    if state:
        held = sum(row['status'] == 'sending' for row in state['listings'].values())
        lines.extend(['', f'Uncertain deliveries held for review: **{held}**.'])
    lines.extend(['', 'Funda is not connected. A five-minute schedule is best effort. Source access from this runner must be verified.',
                  'Matching means Rotterdam, advertised rent up to EUR 1,800, at least two bedrooms, and no known closed/unavailable status.',
                  'The first successful live scan of each source establishes a baseline and sends no existing listings.'])
    text = '\n'.join(lines) + '\n'
    Path('run-summary.md').write_text(text, encoding='utf8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf8') as stream:
            stream.write(text)


def main(environ=os.environ):
    mode = environ.get('RUN_MODE', 'preview')
    if mode == 'scheduled':
        mode = 'live' if environ.get('SEND_ALERTS') == 'true' else 'preview'
    if mode not in ('preview', 'live', 'test_email'):
        raise ValueError('Unknown run mode')
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
    store = GitHubState(environ) if mode == 'live' else None
    state = store.load() if store else None  # Never scrape/send with missing or unreadable saved state.
    scans = collect_all(Collector())
    sent = 0
    try:
        if store:
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
                if key not in current or not is_match(current[key]):
                    row['status'] = 'expired'
                else:
                    row['item'] = current[key]
            store.save(state)
            # Only send pending records from sources that were checked successfully this run.
            # Held records stay untouched until their Sent-folder outcome is reviewed.
            allowed = {name for name, scan in scans.items() if 'error' not in scan}
            pending = [row for key, row in state['listings'].items()
                       if row['status'] == 'pending' and key.split(':', 1)[0] in allowed]
            if pending:
                sent = deliver(state, store, lambda: GmailSender(environ), allowed_sources=allowed)
    finally:
        summary(scans, mode, state, sent)
    return int(any('error' in scan for scan in scans.values()))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # Prevent SMTP exceptions from printing a recipient address or authentication detail into public logs.
        print(f'Run stopped ({type(error).__name__}). Check the setup and held-delivery status; no blind retries.', file=sys.stderr)
        raise SystemExit(1)
