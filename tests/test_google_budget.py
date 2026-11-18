import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import server

class Response:
    def raise_for_status(self): pass
    def json(self): return {'routes': [{'duration': '2400s'}]}

class MockClient:
    def __init__(self): self.calls=[]
    async def post(self, url, **kwargs):
        self.calls.append((url,kwargs))
        return Response()

class GoogleBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = server.DB
        server.DB = Path(self.tmp.name)/'scout.db'
        server.init_db()
        self.env = patch.dict(os.environ, {'GOOGLE_MAPS_API_KEY':'fake-not-a-real-key', 'GOOGLE_ROUTES_MONTHLY_LIMIT':'2', 'GOOGLE_ROUTES_DAILY_LIMIT':'2'})
        self.env.start()
    def tearDown(self):
        self.env.stop()
        server.DB = self.original_db
        self.tmp.cleanup()

    def test_atomic_parallel_cap_and_persistence(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            result=list(pool.map(lambda _: server.reserve_google_route(), range(25)))
        self.assertEqual(sum(result), 2)
        self.assertFalse(server.reserve_google_route())
        self.assertEqual(server.google_usage()['month'],2)
        self.assertEqual(server.google_usage()['day'],2)

    def test_routes_api_and_cache_dont_consume_extra_credit(self):
        listing={'id':'listing-1','address':'123 Broadway, New York, NY','lat':None,'lng':None}
        c=MockClient()
        minutes=asyncio.run(server.commute_minutes(listing,'309 W 49th St, New York, NY',9,c))
        self.assertEqual(minutes,40)
        self.assertEqual(len(c.calls),1)
        self.assertEqual(c.calls[0][0],server.GOOGLE_ROUTES_URL)
        self.assertEqual(c.calls[0][1]['json']['travelMode'],'TRANSIT')
        self.assertEqual(c.calls[0][1]['headers']['X-Goog-FieldMask'],'routes.duration')
        self.assertEqual(asyncio.run(server.commute_minutes(listing,'309 W 49th St, New York, NY',9,c)),40)
        self.assertEqual(server.google_usage()['month'],1)
        self.assertEqual(len(c.calls),1)

    def test_limit_prevents_api_calls(self):
        c=MockClient()
        self.assertTrue(server.reserve_google_route())
        self.assertTrue(server.reserve_google_route())
        r={'id':'other','address':'1 Broadway, New York, NY','lat':None,'lng':None}
        self.assertIsNone(asyncio.run(server.commute_minutes(r,'309 W 49th St',9,c)))
        self.assertEqual(len(c.calls),0)

    def test_invalid_config_fails_closed_and_ceiling_cannot_be_raised(self):
        with patch.dict(os.environ, {'GOOGLE_ROUTES_MONTHLY_LIMIT':'invalid'}):
            self.assertFalse(server.reserve_google_route())
        with patch.dict(os.environ, {'GOOGLE_ROUTES_MONTHLY_LIMIT':'999999','GOOGLE_ROUTES_DAILY_LIMIT':'999999'}):
            self.assertEqual(server.google_limits(),{'monthly':4000,'daily':100})

    def test_missing_location_does_not_spend(self):
        c=MockClient()
        r={'id':'missing','address':None,'lat':None,'lng':None}
        self.assertIsNone(asyncio.run(server.commute_minutes(r,'309 W 49th St',9,c)))
        self.assertEqual(server.google_usage()['month'],0)
