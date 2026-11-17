import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agencies import decode_nuxt, hartje_detail, zeker_detail
from sources import Collector, is_match
from eligibility import student_restriction, student_assessment, at_home_restrictions


def hartje_html(status='Bezichtiging vol'):
    return f'''<html><title>Example 1 A - Wonen Hartje</title>
      <div class="wh-property-price">€ 1.650</div>
      <div class="mkdf-property-content"><span class="mkdf-property-label">Status</span><span class="mkdf-property-value">{status}</span></div>
      <div class="mkdf-property-title-right">Beschikbaar</div>
      <div><span><span class="mkdf-label-text">Slaapkamers</span></span><span>2</span></div>
      <div><span><span class="mkdf-label-text">Verhuurvorm</span></span><span>Te huur</span></div></html>'''


class SourceTests(unittest.TestCase):
    def test_at_home_collector_checks_property_descriptions_before_matching(self):
        base={'status':{'attribute':{'slug':'active'}},'ah_price':1500,'location':{'name':'Rotterdam'},'no_bedrooms':2}
        raw=[{**base,'id':n,'url':f'https://athomevastgoed.nl/woningaanbod/property-{n}'} for n in (1,2)]
        overview="SET_PROPERTIES_COLLECTION',"+json.dumps({'data':raw,'next_page_url':None})
        def fetch(url):
            if url=='https://athomevastgoed.nl/woningaanbod': return overview
            text='Alleen voor studenten.' if url.endswith('-1') else 'Geschikt voor studenten en werkenden.'
            return '<main><h1>Example apartment in Rotterdam</h1><p>'+text+' Two bedrooms available.</p></main>'
        with tempfile.TemporaryDirectory() as folder:
            rows=Collector(Path(folder)/'cache.json',fetcher=fetch).athome()
        self.assertEqual([row['id'] for row in rows if is_match(row)],['athome:2'])

    def test_legacy_detail_cache_is_refreshed_before_student_filter_is_used(self):
        url='https://wonenhartje.nl/object/example-rotterdam-h123/'
        html=hartje_html('Beschikbaar').replace('</html>','<p>Only for students.</p></html>')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cache.json'
            path.write_text(json.dumps({url:{'time':time.time(),'item':{'studentOnly':False}}}))
            row=Collector(path,fetcher=lambda url:html).cached_detail(url,hartje_detail)
        self.assertTrue(row['studentOnly'])

    def test_explicit_dutch_and_english_student_only_restrictions(self):
        for text in ['Alleen voor 2 studenten, die beide gaan studeren in Rotterdam.',
                     'Uitsluitend bestemd voor studenten.', 'Deze woning is uitsluitend te huur voor studenten.',
                     'Deze woning wordt alleen verhuurd aan studenten.', 'Je moet student zijn.',
                     'Only for two international students.', 'Students only.', 'A student-only apartment.',
                     'Only students can apply.', 'You must be a registered student.',
                     'This apartment can only be rented by students.']:
            with self.subTest(text=text): self.assertTrue(student_restriction(text))
        for text in ['Perfect for students and young professionals.', 'Ook geschikt voor studenten.',
                     'Niet alleen voor studenten.', 'Not exclusively for students.',
                     'Only for students or young professionals.', 'Not a student-only apartment.',
                     'Geen studenten toegestaan.', 'Near the university. Student discount on internet.']:
            with self.subTest(text=text): self.assertFalse(student_restriction(text))

    def test_student_restriction_is_read_by_all_three_adapters(self):
        restriction='Alleen voor 2 studenten die studeren in Rotterdam.'
        html=hartje_html('Beschikbaar').replace('</html>',f'<div class="mkdf-property-description">{restriction}</div></html>')
        row=hartje_detail(html,'https://wonenhartje.nl/object/example-rotterdam-h123/')
        self.assertTrue(row['studentOnly']);self.assertFalse(is_match({**row,'available':True}))
        self.assertTrue(at_home_restrictions('<main><h1>Example property in Rotterdam</h1><p>'+restriction+'</p></main>','')['studentOnly'])
        item={'title':'Example','handover':{'price':1500},'address':{'location':'Rotterdam'},
              'description':{'content':'This apartment is only for students.'},'stage':'available','status':{'code':'available'}}
        with patch('agencies.decode_nuxt',return_value={'data':{'property':item}}):
            row=zeker_detail('', 'https://woonzeker.com/huur/woningen/example')
        self.assertTrue(row['studentOnly']);self.assertFalse(is_match({**row,'available':True}))

    def test_related_properties_and_footer_do_not_create_student_only_false_positive(self):
        html='<main><h1>Rotterdam apartment</h1><p>For students or professionals. A spacious two bedroom home.</p><div class="related-properties">Only for students.</div></main><footer>Students only</footer>'
        self.assertFalse(at_home_restrictions(html,'')['studentOnly'])

    def test_woonzeker_missing_fields_are_unknown_but_explicit_closed_still_excludes(self):
        item={'title':'Example','handover':None,'characteristic':None,'address':{'location':'Rotterdam'},'stage':'available'}
        with patch('agencies.decode_nuxt',return_value={'data':{'property':item}}):
            row=zeker_detail('', 'https://woonzeker.com/huur/woningen/example')
            self.assertIsNone(row['rent']);self.assertIsNone(row['bedrooms']);self.assertIsNone(row['openForResponses'])
            self.assertTrue(is_match({**row,'available':row['openForResponses']}))
            item['status']={'code':'rented'}
            row=zeker_detail('', 'https://woonzeker.com/huur/woningen/example')
            self.assertFalse(is_match({**row,'available':row['openForResponses']}))

    def test_missing_bedrooms_or_price_does_not_discard_a_rotterdam_listing(self):
        html=hartje_html('Beschikbaar').replace('<div class="wh-property-price">€ 1.650</div>','').replace('<div><span><span class="mkdf-label-text">Slaapkamers</span></span><span>2</span></div>','')
        row=hartje_detail(html,'https://wonenhartje.nl/object/example-rotterdam-h123/')
        self.assertIsNone(row['rent']);self.assertIsNone(row['bedrooms'])
        self.assertTrue(is_match({**row,'available':row['openForResponses']}))

    def test_broken_detail_page_preserves_other_properties_and_warned_fallback(self):
        root='https://wonenhartje.nl/rotterdam/aanbod-rotterdam/'
        bad='https://wonenhartje.nl/object/bad-rotterdam-h123/'
        good='https://wonenhartje.nl/object/good-rotterdam-h456/'
        def fetch(url):
            if url==root: return f'<a href="{bad}">Bad</a><a href="{good}">Good</a>'
            if url==bad: raise TimeoutError('Public page unavailable')
            return hartje_html('Beschikbaar')
        with tempfile.TemporaryDirectory() as folder:
            collector=Collector(Path(folder)/'cache.json',fetcher=fetch)
            rows=collector.wonenhartje()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(is_match(row) for row in rows))
        self.assertIsNone(rows[0]['bedrooms']);self.assertTrue(rows[0]['collectionWarnings'])
        self.assertEqual(rows[1]['bedrooms'],2)
        self.assertEqual(len(collector.issues),1)

    def test_full_viewings_override_generic_available_label(self):
        url='https://wonenhartje.nl/object/example-rotterdam-h123/'
        row=hartje_detail(hartje_html(),url)
        self.assertFalse(row['openForResponses'])
        row=hartje_detail(hartje_html('Empty'),url)
        self.assertTrue(row['openForResponses'])
        self.assertEqual(row['rent'],1650)
        self.assertEqual(row['bedrooms'],2)

    def test_source_adapter_normalizes_closed_listing_for_the_matching_pipeline(self):
        root='https://wonenhartje.nl/rotterdam/aanbod-rotterdam/'
        detail='https://wonenhartje.nl/object/example-rotterdam-h123/'
        def fetch(url):
            return f'<a href="{detail}">Example</a>' if url==root else hartje_html()
        with tempfile.TemporaryDirectory() as folder:
            collector=Collector(Path(folder)/'cache.json',fetcher=fetch)
            rows=collector.wonenhartje()
        self.assertEqual(rows[0]['id'],'wonenhartje:h123')
        self.assertFalse(is_match(rows[0]))

    def test_nuxt_reference_decoder_preserves_explicit_bedrooms_and_false_values(self):
        table=[['ShallowReactive',1],{'data':2,'empty':6},{'listing':3},
               {'number_of_bedrooms':4,'closed':5},2,False,['EmptyRef',7],None]
        html='<script id="__NUXT_DATA__" type="application/json">'+json.dumps(table)+'</script>'
        root=decode_nuxt(html)
        self.assertEqual(root['data']['listing']['number_of_bedrooms'],2)
        self.assertIs(root['data']['listing']['closed'],False)
        self.assertIsNone(root['empty'])


if __name__=='__main__': unittest.main()
