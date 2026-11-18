import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch
from email.message import EmailMessage
from pathlib import Path

import server

class FakeResponse:
    status_code = 200
    def json(self):
        return {'listings':[{'id':'123','url':'https://streeteasy.com/building/demo-avenue/3a',
                            'street':'14 Demo Avenue','unit':'3A','price':2890,'bedrooms':1,
                            'latitude':40.7,'longitude':-73.9,'area':'Astoria','no_fee':True}]}

class FakeClient:
    def __init__(self): self.calls=[]
    async def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        return FakeResponse()

class StreetEasyTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.old_db=server.DB
        server.DB=Path(self.dir.name)/'test.sqlite'
        server.init_db()

    def tearDown(self):
        server.DB=self.old_db
        self.dir.cleanup()

    def test_search_response_and_monthly_budget_and_interval(self):
        c=FakeClient()
        with patch.dict(os.environ,{'APISH_KEY':'test','APISH_AREAS':'queens','APISH_INTERVAL_MINUTES':'180',
                                    'APISH_MONTHLY_BUDGET':'1'}):
            items,status=asyncio.run(server.apish_items(c))
            self.assertEqual(len(items),1)
            self.assertEqual(items[0][0]['price'],2890)
            self.assertEqual(items[0][0]['borough'],'Queens')
            self.assertEqual(server.apish_usage()['calls'],1)
            items2,status2=asyncio.run(server.apish_items(c))
            self.assertEqual(items2,[])
            self.assertEqual(len(c.calls),1)
            self.assertIn('budget reached',status2)

    def test_streeteasy_message_only_direct_listing_links(self):
        msg=EmailMessage()
        msg['From']='StreetEasy <notify@streeteasy.com>'
        msg['Date']='Tue, 17 Nov 2026 12:00:00 -0500'
        msg.set_content('New apartments')
        msg.add_alternative('''<a href="https://streeteasy.com/building/42-avenue/4d">42 Avenue 4D $2,950</a>
        <a href="https://streeteasy.com/for-rent/nyc">browse</a>''',subtype='html')
        rows=server.parse_streeteasy_message(msg.as_bytes())
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][0]['url'],'https://streeteasy.com/building/42-avenue/4d')
        self.assertEqual(rows[0][0]['price'] if 'price' in rows[0][0] else None,None)
        msg.replace_header('From','Imposter <a@notstreeteasy.com>')
        self.assertEqual(server.parse_streeteasy_message(msg.as_bytes()),[])

    def test_upsert_deduplicates(self):
        i=server.streeteasy_item(FakeResponse().json()['listings'][0])
        self.assertEqual(server.upsert([(i,'StreetEasy')]),1)
        self.assertEqual(server.upsert([(i,'StreetEasy')]),0)

if __name__=='__main__': unittest.main()
