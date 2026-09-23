from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from etf_rotation.market_data import SHANGHAI
from etf_rotation.notification_service import NotificationService


class FakeSecrets:
    def __init__(self): self.value = None
    def exists(self): return self.value is not None
    def save(self, value): self.value = value
    def load(self): return self.value


class FakeTransport:
    def __init__(self): self.calls = []; self.result = {'status':'SERVER_ACCEPTED','reason':'ok','retryable':False}
    def check(self, config, secret, *, cancelled=None):
        if cancelled and cancelled(): return {'status':'CANCELLED','retryable':False}
        self.calls.append('check')
        return {'status':'CONNECTION_OK','reason':'ok','retryable':False}
    def send(self, config, secret, subject, body, message_id, *, cancelled=None):
        if cancelled and cancelled(): return {'status':'CANCELLED','retryable':False}
        self.calls.append((config['recipient'], subject, body, message_id))
        return self.result


class FakeMonitor:
    def __init__(self, now):
        self.now = now
        self.health = 'REALTIME'
        self.metadata_store = SimpleNamespace(load=lambda:{'510300':object()})
        self.health_classifier = SimpleNamespace(closed_dates=frozenset())
        self.points = []
        self.set_price(1.02)
    def set_price(self, last):
        self.points = [{'timestamp':(self.now().replace(second=0,microsecond=0)-timedelta(minutes=6-i)).isoformat(), 'price':1.0 if i<5 else last} for i in range(6)]
    def snapshot(self):
        return {'revision':1,'items':[dict(symbol='510300',name='Synthetic',health_status=self.health,
                              timestamp_basis='MINUTE_START',timestamp=self.points[-1]['timestamp'])]}
    def quotes(self, symbol, since): return {'upserts':self.points,'revision':1,'reset':True,'symbol':symbol}


class NotificationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026,9,4,10,6,tzinfo=SHANGHAI)
        self.monitor = FakeMonitor(lambda:self.now)
        self.swing = SimpleNamespace(portfolio=lambda:{'holdings_snapshot':{'status':'SNAPSHOT_ONLY','snapshot':{'positions':[
            {'symbol':'510300','name':'Synthetic','asset_type':'ETF','shares':100},
            {'symbol':'600036','name':'Bank','asset_type':'STOCK','shares':100},
            {'symbol':'159792','name':'Pending','asset_type':'ETF','shares':100},
        ]}}})
        self.transport = FakeTransport()
        self.service = NotificationService(Path(self.temp.name), self.monitor, self.swing,
            clock=lambda:self.now, transport=self.transport, secrets=FakeSecrets())
        self.addCleanup(self.service.stop)

    def configure(self):
        self.service.configure({'sender':'sender@163.com','recipient':'recipient@163.com',
                                'username':'sender@163.com','secret':'synthetic-only'})

    def enable(self):
        self.configure()
        self.service.test_connection('connect')
        self.service.deliver_once()
        self.service.test_email('test')
        self.service.deliver_once()
        self.service.set_enabled(True)
        self.transport.calls.clear()

    def test_default_observation_filters_stocks_and_pending(self):
        self.service.tick()
        data = self.service.snapshot()
        self.assertEqual(data['mode'],'OBSERVATION')
        self.assertEqual(len(data['items']),2)
        self.assertFalse(next(i for i in data['items'] if i['symbol']=='159792')['eligible'])
        self.assertEqual(data['events'][0]['status'],'OBSERVED')
        self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])

    def test_shadow_candidate_is_observed_and_delivered_as_research_only(self):
        self.swing.portfolio = lambda: {
            'holdings_snapshot': {'status': 'ABSENT', 'snapshot': None},
            'projection': {'positions': {'510300': {'shares': 100}}},
        }
        self.swing.snapshot = lambda: {
            'items': [{
                'symbol': '510300',
                'name': 'Synthetic',
                'shadow': {
                    'status': 'AVAILABLE',
                    'data_quality_status': 'VERIFIED',
                    'data_healthy': True,
                    'account_known': True,
                    'cost_ok': True,
                    'risk_ok': True,
                    'snapshot_only': False,
                    'executable': False,
                    'strategy_version': 'SWING_V2_SHADOW',
                    'opportunity': {
                        'opportunity_id': 'op-1',
                        'status': 'TECHNICAL_CANDIDATE',
                    },
                    'variants': {
                        'V2_A': {
                            'strategy_version': 'SWING_V2_SHADOW',
                            'variant': 'V2_A',
                            'state': 'TECHNICAL_CANDIDATE',
                            'blocked_reasons': [],
                            'opportunity_id': 'op-1',
                            'executable': False,
                            'data_version': 'sha256:test',
                            'indicator_version': 'INDICATORS_V1',
                            'evidence': {'as_of_trading_date': '2026-09-03'},
                        },
                    },
                },
            }],
        }
        self.monitor.set_price(1.0)
        self.enable()
        self.service.tick()
        research = [e for e in self.service.store.events() if e['kind'] == 'SHADOW_RESEARCH']
        self.assertEqual(len(research), 1)
        self.assertEqual(research[0]['payload']['opportunity_id'], 'op-1')
        self.service.deliver_once()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertIn('研究通知', self.transport.calls[0][2])
        self.assertIn('不是交易指令', self.transport.calls[0][2])

    def test_explicit_tests_required_and_public_secret_masked(self):
        self.configure()
        with self.assertRaises(ValueError): self.service.set_enabled(True)
        payload = str(self.service.snapshot())
        self.assertNotIn('synthetic-only',payload)
        self.assertNotIn('recipient@163.com',payload)
        self.assertEqual(self.transport.calls,[])

    def test_same_signal_once_and_no_account_details_in_mail(self):
        self.enable()
        self.service.tick(); self.service.deliver_once()
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        body = self.transport.calls[0][2]
        self.assertIn('不是交易指令',body)
        self.assertNotIn('600036',body)

    def test_unhealthy_quote_cancels_pending(self):
        self.enable(); self.service.tick()
        self.monitor.health = 'DATA_ERROR'
        self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])
        self.assertEqual(self.service.snapshot()['events'][0]['status'],'CANCELLED')

    def test_lunch_cancels_pending(self):
        self.enable(); self.service.tick()
        self.now = self.now.replace(hour=12)
        self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])

    def test_queue_expires_and_does_not_retry_unknown(self):
        self.enable(); self.service.tick()
        self.transport.result = {'status':'UNKNOWN','reason':'uncertain','retryable':False}
        self.service.deliver_once(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(self.service.snapshot()['events'][0]['status'],'UNKNOWN')

    def test_config_change_cancels_and_invalidates_checks(self):
        self.enable(); self.service.tick()
        self.service.configure({'recipient':'another@163.com'})
        self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])
        self.assertFalse(self.service.snapshot()['checks']['email'])
        self.assertFalse(self.service.snapshot()['config']['enabled'])

    def test_fault_wait_and_recovery_requires_new_minutes(self):
        self.enable()
        self.monitor.health='OUTAGE'; self.service.tick(); self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])
        self.now += timedelta(seconds=181)
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        self.monitor.health='REALTIME'; self.monitor.set_price(1.0)
        self.service.tick(); self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        self.now += timedelta(minutes=1); self.monitor.set_price(1.0)
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),2)

    def test_test_idempotency_and_cooldown(self):
        self.configure()
        first = self.service.test_email('same')
        self.assertEqual(self.service.test_email('same'),first)
        with self.assertRaises(ValueError): self.service.test_email('other')
        self.service.deliver_once(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)

    def test_read_only_cannot_test_or_enable(self):
        self.service.read_only=True
        self.configure()
        with self.assertRaises(ValueError): self.service.test_email('blocked')
        with self.assertRaises(ValueError): self.service.set_enabled(True)

    def test_explicit_test_survives_out_of_session_detector(self):
        self.configure()
        self.now = self.now.replace(hour=20)
        self.service.test_email('evening-test')
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(self.service.snapshot()['events'][0]['status'],'SERVER_ACCEPTED')

    def test_fault_recovery_requires_adjacent_minutes(self):
        self.enable()
        self.monitor.health='OUTAGE'; self.service.tick()
        self.now += timedelta(minutes=3)
        self.service.tick(); self.service.deliver_once()
        self.monitor.health='REALTIME'; self.monitor.set_price(1.0)
        self.service.tick()
        self.now += timedelta(minutes=2); self.monitor.set_price(1.0)
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        self.now += timedelta(minutes=1); self.monitor.set_price(1.0)
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),2)

    def test_failed_retest_revokes_automatic_outbound(self):
        self.enable()
        self.now += timedelta(seconds=61); self.monitor.set_price(1.02)
        with patch.object(self.transport,'check',return_value={'status':'FAILED','retryable':False}):
            self.service.test_connection('retest'); self.service.deliver_once()
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])
        self.assertFalse(self.service.snapshot()['config']['enabled'])

    def test_changed_config_on_disk_invalidates_restart_verification(self):
        self.enable()
        self.service.config_store.save({**self.service.config,'recipient':'changed@163.com'})
        self.service.stop()
        restarted=NotificationService(Path(self.temp.name),self.monitor,self.swing,
            clock=lambda:self.now,transport=self.transport,secrets=self.service.secrets)
        self.addCleanup(restarted.stop)
        self.assertFalse(restarted.snapshot()['config']['enabled'])
        self.assertFalse(restarted.snapshot()['checks']['email'])

    def test_event_and_detector_are_atomic(self):
        self.enable()
        original=self.service.store.set_state
        def fail_state(key,value):
            if key.startswith('detector:'): raise OSError('synthetic failure')
            return original(key,value)
        with patch.object(self.service.store,'set_state',side_effect=fail_state):
            self.service.tick()
        self.assertFalse(any(e['kind']=='ANOMALY' for e in self.service.store.events()))

    def test_test_request_and_event_are_atomic(self):
        self.configure()
        original=self.service.store.set_state
        def fail_state(key,value):
            if key.startswith('request:'): raise OSError('synthetic failure')
            return original(key,value)
        with patch.object(self.service.store,'set_state',side_effect=fail_state):
            with self.assertRaises(ValueError): self.service.test_email('crash')
        self.assertEqual(self.service.store.events(),[])

    def test_detector_immediately_cancels_invalid_retry(self):
        self.enable(); self.service.tick()
        event=self.service.store.events()[0]
        self.service.store.update_event(event['id'],next_attempt_at=(self.now+timedelta(seconds=30)).isoformat())
        self.monitor.health='OUTAGE'; self.service.tick()
        self.assertEqual(self.service.store.events()[0]['status'],'CANCELLED')

    def test_restart_keeps_cooldown_and_never_replays_queue(self):
        self.enable(); self.service.tick()
        self.service.stop()
        restarted=NotificationService(Path(self.temp.name),self.monitor,self.swing,
            clock=lambda:self.now,transport=self.transport,secrets=self.service.secrets)
        self.addCleanup(restarted.stop)
        restarted.tick(); restarted.deliver_once()
        self.assertEqual(self.transport.calls,[])
        self.assertEqual(len([e for e in restarted.store.events() if e['kind']=='ANOMALY']),1)

    def test_auto_event_has_auditable_identity_and_rule_evidence(self):
        self.service.tick()
        event=self.service.store.events()[0]
        payload=event['payload']
        self.assertEqual(payload['trading_date'],'2026-09-04')
        self.assertGreaterEqual(payload['lifecycle'],1)
        self.assertEqual(payload['rule_version'],1)
        self.assertTrue(payload['rule_revision'])

    def test_pending_same_cycle_different_kinds_merge(self):
        self.enable()
        anomaly=self.service._event('ANOMALY','510300','UP',{'name':'Synthetic','change_pct':2},self.now)
        fault=self.service._event('FAULT','159792','',{'name':'Pending','reason':'synthetic'},self.now)
        self.service.store.add_event(anomaly); self.service.store.add_event(fault)
        with patch.object(self.service,'_valid',return_value=True): self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        self.assertIn('510300',self.transport.calls[0][2])
        self.assertIn('159792',self.transport.calls[0][2])

    def test_retry_schedule_is_bounded_and_canceled_after_expiry(self):
        self.configure()
        self.transport.result={'status':'FAILED','reason':'NETWORK_ERROR','retryable':True}
        self.service.test_email('retry')
        start=self.now
        for seconds in (0,30,90,210):
            self.now=start+timedelta(seconds=seconds)
            self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),4)
        self.assertEqual(self.service.store.events()[0]['status'],'FAILED')

    def test_clock_rollback_latches_outbound_off(self):
        self.enable(); self.service.tick()
        self.now-=timedelta(seconds=1)
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])
        self.assertEqual(self.service.snapshot()['mode'],'ERROR')

    def test_real_monitor_completed_minute_contract(self):
        from tests import test_runtime_api as fixtures
        fixture=fixtures.RuntimeTests(); fixture.setUp()
        self.addCleanup(fixture.tearDown)
        payload=fixtures.valid_completed_quote_payload()
        app=fixture.make_runtime_fixture(fixtures.StaticCollector(payload))
        self.assertTrue(app.refresh_once())
        self.now=app.clock()
        self.service.monitor=app
        self.service.tick()
        item=next(i for i in self.service.snapshot()['items'] if i['symbol']=='510300')
        self.assertEqual(item['health_status'],'REALTIME')
        self.assertEqual(item['status'],'READY')

    def test_replay_passes_held_symbol_and_saved_rules_to_audited_reader(self):
        expected={'sample_count':6,'raw_crossings':1,'merged_events':1,'invalid_samples':5,'events':[]}
        with patch('etf_rotation.notification_service.audited_replay',return_value=expected) as replay:
            self.assertEqual(self.service.replay('510300','2026-09-04'),expected)
            replay.assert_called_once_with(self.monitor,'510300','2026-09-04',1.0,30)
        with self.assertRaises(ValueError): self.service.replay('600036','2026-09-04')

    def test_expired_unsent_fault_is_reassessed(self):
        self.enable(); self.monitor.health='OUTAGE'; self.service.tick()
        self.now+=timedelta(seconds=181); self.service.tick()
        self.now+=timedelta(seconds=301); self.service.tick()
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(self.service.store.events()[0]['status'],'SERVER_ACCEPTED')

    def test_recovery_mail_requires_preceding_accepted_fault(self):
        self.enable(); self.monitor.health='OUTAGE'; self.service.tick()
        self.now+=timedelta(seconds=181); self.service.tick()
        self.monitor.health='REALTIME'; self.monitor.set_price(1)
        self.service.tick()
        self.now+=timedelta(minutes=1); self.monitor.set_price(1)
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])

    def test_new_fault_category_is_reconfirmed_and_sent_once(self):
        self.enable(); self.monitor.health='DELAYED'; self.service.tick()
        self.now+=timedelta(seconds=181); self.service.tick(); self.service.deliver_once()
        self.monitor.health='DATA_ERROR'; self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        self.now+=timedelta(seconds=181); self.service.tick(); self.service.deliver_once()
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),2)

    def test_never_connected_missing_item_is_pending_not_fault(self):
        self.monitor.health='MISSING'; self.monitor.points=[]
        self.monitor.snapshot=lambda:{'items':[{'symbol':'510300','health_status':'MISSING','timestamp':None}]}
        self.service.tick()
        self.now+=timedelta(minutes=5); self.service.tick()
        self.assertFalse(self.service.snapshot()['items'][0]['eligible'])
        self.assertEqual(self.service.store.events(),[])

    def test_two_recovery_minutes_do_not_require_six_minute_warmup(self):
        self.enable(); self.monitor.health='OUTAGE'; self.service.tick()
        self.now+=timedelta(seconds=181); self.service.tick(); self.service.deliver_once()
        self.monitor.health='REALTIME'; self.monitor.set_price(1)
        self.monitor.points=self.monitor.points[-1:]
        first=self.monitor.points[0]
        self.service.tick()
        self.now+=timedelta(minutes=1); self.monitor.set_price(1)
        self.monitor.points=[first,self.monitor.points[-1]]
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),2)

    def test_sender_alone_detects_clock_rollback(self):
        self.enable(); self.monitor.health='OUTAGE'; self.service.tick()
        self.now+=timedelta(seconds=181); self.service.tick()
        self.now+=timedelta(seconds=30); self.service.tick()
        self.now-=timedelta(seconds=15); self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])
        self.assertEqual(self.service.snapshot()['mode'],'ERROR')

    def test_explicit_pause_is_distinct_from_initial_observation(self):
        self.assertEqual(self.service.snapshot()['mode'],'OBSERVATION')
        self.service.set_enabled(False)
        self.assertEqual(self.service.snapshot()['mode'],'PAUSED')

    def test_excluded_item_preserves_specific_readiness_reason(self):
        self.service.configure({'excluded_symbols':['510300']})
        self.service.tick()
        item=self.service.snapshot()['items'][0]
        self.assertIn('暂停该标的订阅',item['reason'])

    def test_previously_connected_feed_disappears_still_reports_fault(self):
        self.enable(); self.monitor.set_price(1); self.service.tick()
        self.monitor.snapshot=lambda:{'items':[{'symbol':'510300','health_status':'OUTAGE','timestamp':None}]}
        self.monitor.quotes=lambda symbol,since:{'symbol':symbol,'reset':True,'revision':2,'upserts':[]}
        self.service.tick(); self.now+=timedelta(seconds=181)
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(len(self.transport.calls),1)
        self.assertEqual(self.service.store.events()[0]['kind'],'FAULT')

    def test_pending_fault_canceled_when_category_changes(self):
        self.enable(); self.monitor.health='DELAYED'; self.service.tick()
        self.now+=timedelta(seconds=181); self.service.tick()
        self.monitor.health='DATA_ERROR'; self.service.tick(); self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])
        self.assertEqual(self.service.store.events()[0]['status'],'CANCELLED')

    def test_publication_revision_race_blocks_anomaly(self):
        self.enable()
        self.monitor.quotes=lambda symbol,since:{'symbol':symbol,'reset':True,'revision':2,'upserts':self.monitor.points}
        self.service.tick(); self.service.deliver_once()
        self.assertEqual(self.transport.calls,[])
        self.assertNotEqual(self.service.snapshot()['items'][0]['status'],'READY')

    def test_background_without_browser_stops_and_releases_owner(self):
        import threading
        checked=threading.Event()
        original=self.service.tick
        def tick():
            original(); checked.set()
        with patch.object(self.service,'tick',side_effect=tick):
            self.service.start()
            self.assertTrue(checked.wait(2))
            self.service.stop()
        self.assertFalse(any(t.is_alive() for t in self.service._threads))
        self.assertIsNone(self.service.runtime_lock.handle)
        self.assertEqual(self.transport.calls,[])

    def test_shutdown_before_data_cancels_in_flight_without_sending(self):
        self.configure(); self.service.test_email('stopping')
        def handshake(config,secret,subject,body,message_id,*,cancelled=None):
            self.service.stop()
            self.assertIsNotNone(cancelled)
            self.assertTrue(cancelled())
            return {'status':'CANCELLED','reason':'CANCELLED_BEFORE_SUBMISSION','retryable':False}
        with patch.object(self.transport,'send',side_effect=handshake): self.service.deliver_once()
        self.assertEqual(self.service.store.events()[0]['status'],'CANCELLED')


if __name__=='__main__': unittest.main()
