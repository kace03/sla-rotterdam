import json
from pathlib import Path
import tempfile
import unittest

from agencies import decode_nuxt, hartje_detail
from sources import Collector, is_match


def hartje_html(status='Bezichtiging vol'):
    return f'''<html><title>Example 1 A - Wonen Hartje</title>
      <div class="wh-property-price">€ 1.650</div>
      <div class="mkdf-property-content"><span class="mkdf-property-label">Status</span><span class="mkdf-property-value">{status}</span></div>
      <div class="mkdf-property-title-right">Beschikbaar</div>
      <div><span><span class="mkdf-label-text">Slaapkamers</span></span><span>2</span></div>
      <div><span><span class="mkdf-label-text">Verhuurvorm</span></span><span>Te huur</span></div></html>'''


class SourceTests(unittest.TestCase):
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
