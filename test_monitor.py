from copy import deepcopy
import base64
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from monitor import empty_state, update_state, deliver, GmailSender, GitHubState, send_current, main, scan_report, summary, initialize_current
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
    def test_initialize_records_nonmatches_and_preserves_delivery_history_and_pause(self):
        state=empty_state();state['enabled']=False
        state['listings'][HOME['id']]={'status':'sent','sentAt':'earlier','item':HOME}
        state['listings']['athome:pending']={'status':'pending','item':{**HOME,'id':'athome:pending'}}
        outside={**HOME,'id':'athome:outside','rent':3000,'bedrooms':1}
        rows=[HOME,outside,{**HOME,'id':'athome:pending'}]
        initialize_current(state,{'athome':{'listings':rows},'woonzeker':{'error':'blocked'}})
        self.assertFalse(state['enabled'])
        self.assertEqual(state['listings'][HOME['id']]['status'],'sent')
        self.assertEqual(state['listings'][HOME['id']]['sentAt'],'earlier')
        self.assertEqual(state['listings'][outside['id']]['status'],'baseline')
        self.assertEqual(state['listings']['athome:pending']['status'],'baseline')
        self.assertNotIn('woonzeker',state['initialized'])

    def test_old_nonmatch_does_not_become_new_when_filters_or_details_change(self):
        state=empty_state();state['initialized']=['athome']
        update_state(state,{'athome':{'listings':[{**HOME,'rent':3000}]}})
        self.assertEqual(state['listings'][HOME['id']]['status'],'observed')
        update_state(state,{'athome':{'listings':[HOME]}})
        self.assertEqual(deliver(state,Store(),lambda:Sender()),0)

    def test_baseline_mode_runs_while_paused_without_opening_smtp_or_resuming(self):
        with patch('monitor.GitHubState') as store, patch('monitor.Collector'), patch('monitor.collect_all',return_value={'athome':{'listings':[HOME]}}), patch('monitor.GmailSender') as sender, patch('monitor.summary'):
            state={**empty_state(),'enabled':False};store.return_value.load.return_value=state
            self.assertEqual(main({'RUN_MODE':'baseline','GITHUB_TOKEN':'temporary'}),0)
            sender.assert_not_called()
            self.assertFalse(state['enabled'])
            self.assertEqual(state['listings'][HOME['id']]['status'],'baseline')

    def test_scan_report_separates_new_seen_and_first_scan_baseline(self):
        state=empty_state();state['initialized']=['athome']
        state['listings'][HOME['id']]={'status':'sent','item':HOME}
        scans={'athome':{'listings':[HOME,{**HOME,'id':'athome:2'},{**HOME,'id':'athome:student','studentOnly':True}]},
               'woonzeker':{'listings':[{**HOME,'id':'woonzeker:first'}]},'wonenhartje':{'error':'blocked'}}
        report=scan_report(scans,state)
        self.assertEqual(report['athome'],{'new':1,'seen':1,'baseline':0,'compared':True})
        self.assertEqual(report['woonzeker']['baseline'],1)
        self.assertNotIn('wonenhartje',report)
        update_state(state,scans)
        self.assertEqual(report['athome']['new'],1)

    def test_start_summary_does_not_imply_a_scan_and_live_summary_identifies_no_new_results(self):
        with patch('monitor.Path') as path, patch.dict('os.environ',{},clear=True):
            summary({},'Automatic checks enabled',empty_state())
            text=path.return_value.write_text.call_args.args[0]
            self.assertIn('This START command did not check any websites',text)
            self.assertIn('View automatic runs',text)
            self.assertNotIn('| Source |',text)
            state=empty_state();state['initialized']=['athome']
            state['listings'][HOME['id']]={'status':'sent','item':HOME}
            scans={'athome':{'listings':[HOME]},'wonenhartje':{'error':'blocked'}}
            summary(scans,'live',state,report=scan_report(scans,state))
            text=path.return_value.write_text.call_args.args[0]
            self.assertIn('No new candidates found on the websites successfully checked',text)
            self.assertIn('PARTIAL COVERAGE',text)
            self.assertIn('Previously seen candidates: **1**',text)
            self.assertIn('Report generated:',text)

    def test_normal_live_check_sends_every_new_candidate_including_more_than_five(self):
        state=empty_state();state['initialized']=['athome']
        homes=[{**HOME,'id':f'athome:{n}'} for n in range(10,17)]
        student={**HOME,'id':'athome:student','studentOnly':True}
        scans={'athome':{'listings':homes+[student]}}
        sender=Sender()
        with patch('monitor.GitHubState') as store, patch('monitor.Collector'), patch('monitor.collect_all',return_value=scans), patch('monitor.GmailSender',return_value=sender), patch('monitor.summary'):
            store.return_value.load.return_value=state
            self.assertEqual(main({'RUN_MODE':'live','SEND_ALERTS':'true','GITHUB_TOKEN':'temporary'}),0)
        self.assertEqual(sender.items,homes)
        self.assertEqual(state['listings'][student['id']]['status'],'observed')
        self.assertTrue(all(state['listings'][home['id']]['status']=='sent' for home in homes))
        self.assertEqual(deliver(state,Store(),lambda:Sender()),0)

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

    def test_filters_exclude_known_bad_values_but_keep_unknown_details(self):
        self.assertTrue(is_match(HOME))
        self.assertTrue(is_match({**HOME, 'rent':2000}))
        for patch in [{'bedrooms':None},{'rent':None},{'available':None}]:
            self.assertTrue(is_match({**HOME,**patch}))
        for patch in [{'city':'Schiedam'},{'city':''},{'rent':2000.01},{'bedrooms':1},{'available':False}]:
            self.assertFalse(is_match({**HOME,**patch}))

    def test_unknown_details_are_explained_in_single_and_digest_email(self):
        messages=[]
        class SMTP:
            def __init__(self,*args,**kwargs): pass
            def login(self,*args): pass
            def send_message(self,msg,**kwargs): messages.append(msg);return {}
            def quit(self): pass
        env={'GMAIL_SENDER':'sender@gmail.com','ALERT_RECIPIENT':'friend@example.com','GMAIL_APP_PASSWORD':'abcdefghijklmnop'}
        sender=GmailSender(env,smtp_factory=SMTP)
        item={**HOME,'rent':None,'bedrooms':None,'available':None}
        sender.send(item);sender.send_digest([item]);sender.close()
        for msg in messages:
            body=msg.get_content()
            self.assertIn('Could not read the rent',body)
            self.assertIn('Could not read the bedroom count',body)
            self.assertIn('Could not verify current availability',body)
            self.assertIn(HOME['url'],body)
        self.assertIn('details need checking',messages[0]['Subject'])

    def test_incomplete_scan_neither_expires_nor_sends_an_unseen_pending_record(self):
        state=empty_state();state['initialized']=['athome']
        state['listings']['athome:missing']={'status':'pending','item':{**HOME,'id':'athome:missing'}}
        scans={'athome':{'listings':[HOME],'warnings':[{'url':'https://example.com','reason':'ValueError'}]}}
        sender=Sender()
        with patch('monitor.GitHubState') as store, patch('monitor.Collector'), patch('monitor.collect_all',return_value=scans), patch('monitor.GmailSender',return_value=sender), patch('monitor.summary'), patch('builtins.print'):
            store.return_value.load.return_value=state
            self.assertEqual(main({'RUN_MODE':'live','SEND_ALERTS':'true','GITHUB_TOKEN':'temporary'}),0)
        self.assertEqual(sender.items,[HOME])
        self.assertEqual(state['listings']['athome:missing']['status'],'pending')

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
