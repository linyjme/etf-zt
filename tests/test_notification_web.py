from http.client import HTTPConnection
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests import test_swing_web as web_fixtures


class NotificationWebTests(unittest.TestCase):
    def setUp(self):
        self.fixture=web_fixtures.SwingWebTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base=self.fixture.base

    def post(self,path,payload,headers=None):
        request=Request(self.base+path,data=json.dumps(payload).encode(),method='POST',headers={
            'Content-Type':'application/json','Origin':self.base,
            'X-CSRF-Token':self.fixture.server.notifications.csrf_token,
            'Idempotency-Key':'synthetic-request', **(headers or {})})
        try:
            with urlopen(request,timeout=3) as response: return response.status,json.load(response)
        except HTTPError as error:
            with error: return error.code,json.load(error)

    def test_page_and_snapshot_read_only_mode(self):
        status,body,_=self.fixture._get('/notifications')
        self.assertEqual(status,200); self.assertIn('通知中心',body)
        status,data,_=self.fixture._get('/api/notifications')
        self.assertEqual(status,200); self.assertEqual(data['mode'],'READ_ONLY')
        self.assertFalse(data['config']['enabled']); self.assertFalse(data['secret_configured'])
        self.assertNotIn('secret',data['config'])

    def test_same_origin_and_csrf_required(self):
        for headers in ({'Origin':'https://evil.example'},{'X-CSRF-Token':'bad'},{'Sec-Fetch-Site':'cross-site'}, {'Host':'evil.example'}):
            with self.subTest(headers=headers):
                status,_=self.post('/api/notifications/enabled',{'enabled':False,'confirmed':True},headers)
                self.assertEqual(status,403)

        host,port=self.fixture.server.server_address
        body=json.dumps({'enabled':False,'confirmed':True}).encode()
        connection=HTTPConnection(host,port,timeout=3)
        try:
            connection.putrequest('POST','/api/notifications/enabled',skip_host=True)
            connection.putheader('Host',f'{host}:{port}')
            connection.putheader('Origin',f'http://{host}:{port}')
            connection.putheader('Content-Type','application/json')
            connection.putheader('Content-Length',str(len(body)))
            connection.putheader('X-CSRF-Token','é')
            connection.endheaders(body)
            response=connection.getresponse()
            self.assertEqual(response.status,403)
            self.assertEqual(json.load(response)['error'],'forbidden')
        finally:
            connection.close()

    def test_incomplete_setting_safe_but_no_auto_enable(self):
        status,result=self.post('/api/notifications/config',{'sender':'synthetic@163.com'})
        self.assertEqual(status,200); self.assertTrue(result['saved'])
        status,_=self.post('/api/notifications/enabled',{'enabled':True,'confirmed':True})
        self.assertEqual(status,422)

    def test_unknown_fields_and_queries_rejected(self):
        for path,payload in [('/api/notifications/config',{'password':'synthetic'}),
                             ('/api/notifications/enabled',{'enabled':False,'confirmed':False}),
                             ('/api/notifications/config?secret=bad',{}),
                             ('/api/notifications/missing',{})]:
            status,_=self.post(path,payload)
            self.assertIn(status,(400,404,422))

    def test_private_responses_no_cache_and_navigation(self):
        with urlopen(self.base+'/api/notifications') as response:
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))
        for path in ('/','/swing','/pr'):
            _,body,_=self.fixture._get(path)
            self.assertIn('href="/notifications"',body)

    def test_replay_failure_keeps_specific_safe_reason_without_private_errors(self):
        service=self.fixture.server.notifications
        for reason,expected in (
            ('存在未解除的分钟质量隔离问题，不能回放','存在未解除的分钟质量隔离问题，不能回放'),
            ('synthetic-private-error','回放不可用：历史缺失、质量隔离未解除或日期/分钟校验未通过。'),
        ):
            with patch.object(service,'replay',side_effect=ValueError(reason)):
                status,result=self.post('/api/notifications/replay',{'symbol':'510300','date':'2026-09-03'})
            self.assertEqual(status,422)
            self.assertEqual(result['error'],'replay_unavailable')
            self.assertEqual(result['message'],expected)


if __name__=='__main__': unittest.main()
