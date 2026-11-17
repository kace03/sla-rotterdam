from copy import deepcopy
import base64
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from monitor import empty_state, update_state, deliver, GmailSender, GitHubState, send_current, main
from sources import Collector, is_match

HOME = dict(id='athome:1', source='At Home Vastgoed', title='Example', city='Rotterdam',
            rent=1750, bedrooms=2, url='https://athomevastgoed.nl/woningaanbod/huren-example-te-huur-1', costNote='Excluding utilities')


class Store:
    def __init__(self): self.saved=[]
    def save(self,state): self.saved.append(deepcopy(state))


class Sender:
    def __init__(self, fail=False): self.items=[]; self.fail=fail; self.closed=False
    def send(self,item):
        self.items.append(item)
        if self.fail: raise TimeoutError('Ambiguous submission')
    def close(self): self.closed=True


class MonitorTests(unittest.TestCase):
    def test_partial_source_failure_keeps_live_alerts_from_successful_sources(self):
        state=empty_state()
        state['initialized']=['athome','wonenhartje','woonzeker']
        state['listings']['wonenhartje:held']={'status':'pending','item':{**HOME,'id':'wonenhartje:held'}}
        scans={'athome':{'listings':[HOME]},'wonenhartje':{'error':'Access challenge'},'woonzeker':{'listings':[]}}
        sender=Sender()
        with patch('monitor.GitHubState') as store, patch('monitor.Collector'), patch('monitor.collect_all',return_value=scans), patch('monitor.GmailSender',return_value=sender), patch('monitor.summary'), patch('builtins.print'):
            store.return_value.load.return_value=state
            self.assertEqual(main({'RUN_MODE':'live','SEND_ALERTS':'true','GITHUB_TOKEN':'temporary'}),0)
        self.assertEqual(sender.items,[HOME])
        self.assertEqual(state['listings']['athome:1']['status'],'sent')
        self.assertEqual(state['listings']['wonenhartje:held']['status'],'pending')

    def test_complete_source_outage_still_fails_without_sending(self):
        scans={name:{'error':'Access challenge'} for name in ['athome','wonenhartje','woonzeker']}
        with patch('monitor.GitHubState') as store, patch('monitor.Collector'), patch('monitor.collect_all',return_value=scans), patch('monitor.GmailSender') as sender, patch('monitor.summary'), patch('builtins.print'):
            store.return_value.load.return_value=empty_state()
            self.assertEqual(main({'RUN_MODE':'live','SEND_ALERTS':'true','GITHUB_TOKEN':'temporary'}),1)
            sender.assert_not_called()

    def test_replays_filter_and_deduplicate_without_using_history(self):
        scans={'athome': {'listings': [HOME, HOME, {**HOME, 'id':'athome:closed', 'available':False}]},
               'woonzeker': {'error':'blocked'}}
        class DigestSender(Sender):
            def send_digest(self,items,failed): self.items.append((items,failed))
        sender=DigestSender()
        self.assertEqual(send_current(scans,'send_all',lambda:sender),1)
        self.assertEqual(sender.items,[([HOME],['woonzeker'])])
        self.assertTrue(sender.closed)
        sender=Sender()
        self.assertEqual(send_current(scans,'send_random',lambda:sender,choose=lambda rows:rows[0]),1)
        self.assertEqual(sender.items,[HOME])

    def test_no_random_mail_without_matches_or_digest_when_all_sources_failed(self):
        def forbidden(): self.fail('SMTP must not open')
        self.assertEqual(send_current({'athome':{'listings':[]}},'send_random',forbidden),0)
        self.assertEqual(send_current({'athome':{'error':'blocked'}},'send_all',forbidden),0)

    def test_stopped_monitor_blocks_all_checks_and_email_commands(self):
        state={**empty_state(),'enabled':False}
        for mode in ('scheduled','live','preview','test_email','send_all','send_random'):
            with self.subTest(mode=mode), patch('monitor.GitHubState') as store, patch('monitor.Collector') as collector, patch('monitor.GmailSender') as sender, patch('monitor.summary'):
                store.return_value.load.return_value=deepcopy(state)
                self.assertEqual(main({'RUN_MODE':mode,'SEND_ALERTS':'true','GITHUB_TOKEN':'temporary'}),0)
                collector.assert_not_called(); sender.assert_not_called();store.return_value.save.assert_not_called()

    def test_start_and_stop_preserve_history_and_do_not_scan_or_send(self):
        state=empty_state();update_state(state,{'athome':{'listings':[HOME]}})
        for mode,enabled in [('stop',False),('start',True)]:
            with patch('monitor.GitHubState') as store, patch('monitor.Collector') as collector, patch('monitor.GmailSender') as sender, patch('monitor.summary'):
                store.return_value.load.return_value=deepcopy(state)
                self.assertEqual(main({'RUN_MODE':mode,'GITHUB_TOKEN':'temporary'}),0)
                saved=store.return_value.save.call_args.args[0]
                self.assertIs(saved['enabled'],enabled)
                self.assertEqual(saved['listings'],state['listings'])
                collector.assert_not_called();sender.assert_not_called()

    def test_digest_is_one_message_to_configured_recipient(self):
        messages=[]
        class SMTP:
            def __init__(self,*args,**kwargs): pass
            def login(self,*args): pass
            def send_message(self,msg,**kwargs): messages.append((msg,kwargs));return {}
            def quit(self): pass
        env={'GMAIL_SENDER':'sender@gmail.com','ALERT_RECIPIENT':'friend@example.com','GMAIL_APP_PASSWORD':'abcdefghijklmnop'}
        sender=GmailSender(env,smtp_factory=SMTP)
        sender.send_digest([HOME,{**HOME,'title':'Second property'}],['woonzeker']);sender.close()
        self.assertEqual(len(messages),1)
        self.assertEqual(messages[0][1]['to_addrs'],['friend@example.com'])
        self.assertIn('Second property',messages[0][0].get_content())
        self.assertIn('Incomplete scan',messages[0][0].get_content())

    def test_each_source_initializes_independently_and_failure_does_not_reset_history(self):
        state=empty_state()
        update_state(state,{'athome':{'listings':[HOME]},'woonzeker':{'error':'blocked'}})
        self.assertEqual(state['initialized'],['athome'])
        self.assertEqual(state['listings'][HOME['id']]['status'],'baseline')
        new={**HOME,'id':'athome:2'}
        update_state(state,{'athome':{'listings':[HOME,new]}})
        update_state(state,{'athome':{'error':'blocked'}})
        self.assertEqual(state['listings'][new['id']]['status'],'pending')
        self.assertEqual(len(state['listings']),2)

    def test_filters_include_known_closed_status_and_unknown_bedrooms(self):
        self.assertTrue(is_match(HOME))
        self.assertTrue(is_match({**HOME, 'rent':2000}))
        for patch in [{'city':'Schiedam'},{'rent':2000.01},{'bedrooms':None},{'available':False}]:
            self.assertFalse(is_match({**HOME,**patch}))

    def test_durable_claim_precedes_send_and_accepted_items_are_not_resent(self):
        state=empty_state(); state['listings']['athome:1']={'status':'pending','item':HOME}
        store,sender=Store(),Sender()
        self.assertEqual(deliver(state,store,lambda:sender),1)
        self.assertEqual(store.saved[0]['listings']['athome:1']['status'],'sending')
        self.assertEqual(store.saved[-1]['listings']['athome:1']['status'],'sent')
        self.assertEqual(deliver(state,store,lambda:sender),0)
        self.assertEqual(len(sender.items),1)

    def test_uncertain_send_is_held_and_failed_source_cannot_send(self):
        state=empty_state(); state['listings']['athome:1']={'status':'pending','item':HOME}
        store,sender=Store(),Sender(fail=True)
        self.assertEqual(deliver(state,store,lambda:sender,allowed_sources={'woonzeker'}),0)
        with self.assertRaises(TimeoutError): deliver(state,store,lambda:sender)
        self.assertEqual(state['listings']['athome:1']['status'],'sending')
        self.assertTrue(sender.closed)
        self.assertEqual(deliver(state,store,lambda:Sender()),0)

    def test_no_smtp_submission_after_state_write_fails(self):
        state=empty_state();state['listings']['athome:1']={'status':'pending','item':HOME}
        class BrokenStore:
            def save(self,state): raise RuntimeError('State unavailable')
        sender=Sender()
        with self.assertRaises(RuntimeError): deliver(state,BrokenStore(),lambda:sender)
        self.assertEqual(sender.items,[])

    def test_gmail_sends_only_to_configured_recipient_and_rejects_header_injection(self):
        calls=[]
        class SMTP:
            def __init__(self,*args,**kwargs): calls.append(('connect',args))
            def login(self,*args): pass
            def send_message(self,msg,**kwargs): calls.append(('send',msg,kwargs));return {}
            def quit(self): pass
        env={'GMAIL_SENDER':'sender@gmail.com','ALERT_RECIPIENT':'friend@example.com','GMAIL_APP_PASSWORD':'abcdefghijklmnop'}
        sender=GmailSender(env,smtp_factory=SMTP);sender.send(HOME);sender.close()
        self.assertEqual(calls[0][1],('smtp.gmail.com',465))
        self.assertEqual(calls[1][2]['to_addrs'],['friend@example.com'])
        self.assertIn('1750.00',calls[1][1].get_content())
        with self.assertRaises(ValueError): GmailSender({**env,'ALERT_RECIPIENT':'a@b.com\r\nBcc: c@d.com'},smtp_factory=SMTP)

    def test_github_state_roundtrip_never_needs_a_personal_access_token(self):
        env={'GITHUB_REPOSITORY':'owner/repo','GITHUB_TOKEN':'ephemeral','GITHUB_SHA':'a'*40}
        calls=[]
        def api(method,path,body=None,missing_ok=False):
            calls.append((method,path,body))
            if method=='GET': return None
            if method=='POST': return {'ref':'refs/heads/monitor-state'}
            return {'content':{'sha':'stored-sha'}}
        store=GitHubState(env,request=api);state=store.load()
        update_state(state,{'athome':{'listings':[HOME]}});store.save(state);store.save(state)
        self.assertEqual([x[0] for x in calls],['GET','POST','PUT'])
        data=json.loads(base64.b64decode(calls[-1][2]['content']))
        self.assertNotIn('GMAIL_SENDER',json.dumps(data))
        self.assertEqual(data['listings']['athome:1']['status'],'baseline')

if __name__=='__main__': unittest.main()
