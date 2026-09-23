"""Background observation notifications; never a strategy or trading executor."""
import copy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import secrets as tokens
import threading
import uuid

from .market_data import SHANGHAI, market_session_state, MarketHealthClassifier
from .notification_config import ConfigStore, SecretStore, default_config, validate_config
from .notification_mail import SmtpTransport
from .notification_rules import build_alerts, evaluate_anomaly
from .notification_replay import audited_replay
from .notification_store import NotificationStore, RuntimeLock


class NotificationService:
    def __init__(self, root, monitor, swing, *, clock=None, transport=None, secrets=None, read_only=False):
        self.root = Path(root)
        self.monitor, self.swing = monitor, swing
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.transport = transport or SmtpTransport()
        self.secrets = secrets or SecretStore(self.root / 'smtp.secret')
        self.config_store = ConfigStore(self.root / 'settings.json')
        self.read_only = read_only
        self.csrf_token = tokens.token_urlsafe(32)
        self._mutex = threading.RLock()
        self._delivery = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads = []
        self.error = None
        self._items = []
        self.last_checked_at = None
        self._last_clock = None
        self.runtime_lock = RuntimeLock(self.root / 'worker.lock')
        self.runtime_lock.acquire()
        try:
            self.store = NotificationStore(self.root / 'events.sqlite3')
            self.store.recover()
            self.config = self.config_store.load()
            self.epoch = self.store.state('epoch') or uuid.uuid4().hex
            self.store.set_state('epoch', self.epoch)
            self.checks = self.store.state('checks', {'connection':False, 'email':False, 'epoch':self.epoch})
            if self.checks.get('epoch') != self.epoch or self.checks.get('fingerprint') != self._fingerprint():
                self.checks = self._empty_checks()
            if not self._verified():
                self.config['enabled'] = False
        except BaseException:
            self.runtime_lock.release()
            raise

    @property
    def closed_dates(self):
        return self.monitor.health_classifier.closed_dates

    def _now(self):
        value = self.clock()
        if value.tzinfo is None: raise ValueError('通知时钟缺少时区')
        return value.astimezone(SHANGHAI)

    def _checked_now(self):
        now=self._now()
        if self._last_clock is not None and now<self._last_clock:
            raise ValueError('系统时钟回拨，自动邮件暂停，需重启后重新核验')
        self._last_clock=now
        return now

    def _fingerprint(self):
        # Bind verification to both settings and encrypted credential revision.
        # Never decrypt merely to render settings or perform a detector tick.
        settings = {k:v for k,v in self.config.items() if k != 'enabled'}
        digest = hashlib.sha256(json.dumps(settings,sort_keys=True,allow_nan=False).encode())
        path = getattr(self.secrets, 'path', None)
        if path is not None and Path(path).exists():
            with Path(path).open('rb') as stream:
                ciphertext = stream.read(65537)
            if len(ciphertext)>65536: raise ValueError('授权码存储无效')
            digest.update(ciphertext)
        return digest.hexdigest()

    def _empty_checks(self):
        return {'connection':False,'email':False,'epoch':self.epoch,'fingerprint':self._fingerprint()}

    def _verified(self):
        return (self.secrets.exists() and self.checks.get('epoch')==self.epoch
                and self.checks.get('fingerprint')==self._fingerprint()
                and all(self.checks.get(k) is True for k in ('connection','email')))

    @staticmethod
    def _masked(value):
        if not value: return ''
        local, _, domain = value.partition('@')
        return local[:1] + '***' + ('@' + domain if domain else '')

    def snapshot(self, limit=50, offset=0):
        with self._mutex:
            config = copy.deepcopy(self.config)
            for key in ('sender','recipient','username'): config[key] = self._masked(config[key])
            mode = ('ERROR' if self.error else 'READ_ONLY' if self.read_only else
                    'ENABLED' if config['enabled'] else 'PAUSED' if self.store.state('paused',False) else 'OBSERVATION')
            return dict(config=config, secret_configured=self.secrets.exists(), mode=mode,
                        csrf_token=self.csrf_token, checks={k:self.checks.get(k) is True for k in ('connection','email')},
                        items=copy.deepcopy(self._items), events=self.store.events(limit,offset),
                        last_checked_at=self.last_checked_at,
                        session=market_session_state(self._now(), self.closed_dates).phase, error=self.error)

    def configure(self, payload):
        allowed = set(default_config()) - {'schema_version','enabled'} | {'secret'}
        if type(payload) is not dict or not payload.keys() <= allowed:
            raise ValueError('邮件设置字段无效')
        with self._mutex:
            candidate = {**self.config, **{k:v for k,v in payload.items() if k != 'secret'}, 'enabled':False}
            candidate = validate_config(candidate)
            if 'secret' in payload:
                self.secrets.save(payload['secret'])
            self.config_store.save(candidate)
            self.config = candidate
            self.epoch = uuid.uuid4().hex
            self.checks = self._empty_checks()
            with self.store.transaction():
                self.store.set_state('epoch', self.epoch)
                self.store.set_state('checks', self.checks)
                self.store.cancel_pending('设置变化，取消旧通知并重新测试')
            return {'saved':True}

    def set_enabled(self, enabled):
        if type(enabled) is not bool: raise ValueError('启用状态无效')
        with self._mutex:
            if enabled and (self.read_only or self.error or not self._verified()):
                raise ValueError('请在实时服务中配置授权码，并完成连接及测试邮件后启用')
            candidate = {**self.config,'enabled':enabled}
            self.config_store.save(candidate)
            self.config = candidate
            self.store.set_state('paused',not enabled)
            if not enabled: self.store.cancel_pending('通知已暂停')
            return {'enabled':enabled}

    def _queue_test(self, kind, key):
        with self._mutex, self.store.transaction():
            if self.read_only or self.error: raise ValueError('只读或异常模式不允许外发测试')
            if type(key) is not str or not 1 <= len(key) <= 128 or any(ord(c)<33 for c in key):
                raise ValueError('请求标识无效')
            if not self.secrets.exists() or not all(self.config[k] for k in ('sender','recipient','username')):
                raise ValueError('请先填写邮箱并保存新的授权码')
            request_key = 'request:' + hashlib.sha256(key.encode()).hexdigest()
            fingerprint = kind + ':' + self.epoch
            existing = self.store.state(request_key)
            if existing:
                if existing['fingerprint'] != fingerprint: raise ValueError('请求标识已用于其他配置或操作')
                return existing['result']
            now = self._now()
            last = self.store.state('test-last:'+kind)
            if last and (now-datetime.fromisoformat(last)).total_seconds() < 60:
                raise ValueError('测试过于频繁，请一分钟后重试')
            event = self._event(kind, '', '', {}, now, test=True)
            self.store.add_event(event)
            result = {'queued':True,'event_id':event['id']}
            self.store.set_state(request_key, {'fingerprint':fingerprint,'result':result})
            self.store.set_state('test-last:'+kind, now.isoformat())
            self._wake.set()
            return result

    def test_connection(self, key): return self._queue_test('CONNECTION_TEST', key)
    def test_email(self, key): return self._queue_test('TEST_EMAIL', key)

    def _inputs(self, *, remember_connected=True):
        account = self.swing.portfolio()
        view = account.get('holdings_snapshot') or {}
        if view.get('status') == 'INVALID': raise ValueError('持仓快照无效，提醒范围无法核验')
        positions = (view.get('snapshot') or {}).get('positions', [])
        if not positions:
            # A validated strategy projection may also supply reconciled ETF
            # quantities. Never infer holdings from the watchlist or balances.
            projection = account.get('projection') or {}
            raw = projection.get('positions', {})
            positions = [dict(p, symbol=s, asset_type='ETF') for s,p in raw.items()] if isinstance(raw,dict) else []
        metadata = self.monitor.metadata_store.load()
        summary = self.monitor.snapshot()
        published = {item['symbol']:item for item in summary.get('items',[])}
        result = []
        now = self._now()
        classifier = MarketHealthClassifier(self.closed_dates)
        for position in positions:
            if position.get('asset_type') != 'ETF' or position.get('shares',0) <= 0: continue
            symbol = position['symbol']
            base = published.get(symbol)
            connected_key='connected:'+symbol
            connected=self.store.state(connected_key,False) is True
            if symbol in metadata and base is not None and base.get('timestamp') and not connected:
                if remember_connected: self.store.set_state(connected_key,True)
                connected=True
            eligible = (symbol in metadata and base is not None and connected
                        and symbol not in self.config['excluded_symbols'])
            item = dict(symbol=symbol, name=position.get('name',symbol), eligible=eligible,
                        health_status='MISSING', reason='行情未接通或未启用', points=[])
            if symbol not in metadata: item['reason']='标的身份或交易元数据尚未核验'
            if base and eligible:
                item.update({k:base.get(k) for k in ('timestamp','timestamp_basis','health_status')})
                try:
                    minute_view = self.monitor.quotes(symbol,0)
                    if (minute_view.get('symbol')!=symbol or minute_view.get('reset') is not True
                            or type(summary.get('revision')) is not int
                            or minute_view.get('revision')!=summary.get('revision')):
                        raise ValueError('缺少权威分钟全集')
                    item['points'] = minute_view['upserts']
                    stamp = datetime.fromisoformat(item['timestamp'])
                    health = classifier.classify(now, stamp, None, completed_minute=item['timestamp_basis']=='MINUTE_START')
                    if item['health_status'] == 'REALTIME': item['health_status'] = health.status
                    item['reason'] = health.reason if item['health_status']==health.status else base.get('health_reason') or '行情质量或采集异常'
                except (ValueError, KeyError, TypeError):
                    item.update(health_status='OUTAGE',reason='分钟行情无法核验')
            if symbol in self.config['excluded_symbols']: item['reason'] = '已暂停该标的订阅'
            result.append(item)
        return result

    def _rule(self, symbol):
        return {'threshold_pct':1.0,'cooldown_minutes':30, **self.config['rules'].get(symbol,{})}

    def _healthy(self, item, now):
        # Health recovery needs two valid new minutes, not six-bar warm-up.
        points=item.get('points',[])
        latest=evaluate_anomaly({**item,'points':points[-1:]},now,1.0,self.closed_dates)
        return latest['status']=='INSUFFICIENT' and latest['timestamp'] is not None

    def _event(self, kind, symbol, direction, payload, now, *, test=False):
        ttl = 120 if kind == 'ANOMALY' else 300
        identity_key='lifecycle:'+symbol+':'+kind+':'+direction
        lifecycle=self.store.state(identity_key,0)+1
        self.store.set_state(identity_key,lifecycle)
        return dict(id=uuid.uuid4().hex, kind=kind, symbol=symbol, direction=direction,
                    created_at=now.isoformat(), expires_at=(now+timedelta(seconds=ttl)).isoformat(),
                    payload={**payload,'epoch':self.epoch,'cycle':now.isoformat(),
                             'trading_date':now.date().isoformat(),'lifecycle':lifecycle,
                             'rule_version':1,'rule_revision':self.epoch},
                    status='PENDING' if test or (self.config['enabled'] and not self.read_only) else 'OBSERVED',
                    attempts=0,next_attempt_at=now.isoformat(),reason='')

    def _observe(self, item, now, session):
        symbol = item['symbol']
        key = 'detector:'+symbol
        state = self.store.state(key, {})
        session_id = now.date().isoformat()+':'+session.phase
        if state.get('session') != session_id:
            state = {'session':session_id, 'last_notice':state.get('last_notice',{}), 'active':'NONE'}
        rule = self._rule(symbol)
        evaluation = evaluate_anomaly(item,now,rule['threshold_pct'],self.closed_dates)
        output = {k:v for k,v in item.items() if k != 'points'}
        output.update(evaluation)
        if not item['eligible'] or item['health_status']!='REALTIME':
            output['reason']=item['reason']
        if not item['eligible'] or not session.active:
            state['active']='NONE'
            self.store.set_state(key,state)
            return output
        healthy = self._healthy(item,now)
        fault_event=self.store.get_event(state['fault_event']) if state.get('fault_event') else None
        if not healthy:
            state['active']='NONE'
            state['recovery_count']=0
            category=item['health_status'] if item['health_status']!='REALTIME' else 'DATA_ERROR'
            if state.get('fault_category')!=category:
                state.update(fault_category=category,fault_since=now.isoformat(),fault_notice=False)
            if fault_event and fault_event['status']=='CANCELLED':
                state['fault_notice']=False
            state.setdefault('fault_since',now.isoformat())
            if self.config['health_enabled'] and not state.get('fault_notice') and (now-datetime.fromisoformat(state['fault_since'])).total_seconds()>=180:
                event = self._event('FAULT',symbol,'',{'name':item['name'],'reason':item['reason'],
                    'timestamp':item.get('timestamp'),'price':evaluation.get('price'),'fault_category':category},now)
                self.store.add_event(event)
                state['fault_notice']=True
                state['fault_event']=event['id']
        else:
            if fault_event:
                if state.get('recovery_minute')!=item.get('timestamp'):
                    previous = state.get('recovery_minute')
                    adjacent = previous and (datetime.fromisoformat(item['timestamp'])-datetime.fromisoformat(previous)).total_seconds()==60
                    state['recovery_minute']=item.get('timestamp')
                    state['recovery_count']=state.get('recovery_count',0)+1 if adjacent else 1
                if state.get('recovery_count',0)>=2:
                    if self.config['health_enabled'] and (fault_event['status']=='SERVER_ACCEPTED'
                            or (fault_event['status']=='OBSERVED' and not self.config['enabled'])):
                        self.store.add_event(self._event('RECOVERY',symbol,'',{'name':item['name'],
                            'timestamp':item.get('timestamp'),'price':evaluation.get('price')},now))
                    state.pop('fault_notice',None)
                    state.pop('fault_since',None)
                    state.pop('fault_event',None)
                    state.pop('fault_category',None)
            else: state.pop('fault_since',None)
            if evaluation['status']=='READY' and state.get('last_minute')!=evaluation['timestamp']:
                state['last_minute']=evaluation['timestamp']
                change=evaluation['change_pct']
                sign='UP' if change>0 else 'DOWN' if change<0 else 'NONE'
                active=state.get('active','NONE')
                if sign!=active or abs(Decimal(str(change)))<Decimal(str(rule['threshold_pct']))*Decimal('.8'): state['active']='NONE'
                direction=evaluation['direction']
                if direction!='NONE' and state.get('active')!=direction:
                    state['active']=direction
                    last=state.setdefault('last_notice',{}).get(direction)
                    due=not last or (now-datetime.fromisoformat(last)).total_seconds()>=rule['cooldown_minutes']*60
                    if due and self.config['anomaly_enabled']:
                        self.store.add_event(self._event('ANOMALY',symbol,direction,
                            {**evaluation,'name':item['name'],'threshold_pct':rule['threshold_pct']},now))
                        state['last_notice'][direction]=now.isoformat()
            elif evaluation['status']!='READY': state['active']='NONE'
        self.store.set_state(key,state)
        return output

    def _shadow_snapshot(self):
        getter = getattr(self.swing, 'snapshot', None)
        if not callable(getter):
            return {}
        snapshot = getter()
        if not isinstance(snapshot, dict):
            return {}
        items = snapshot.get('items', [])
        return {
            item.get('symbol'): item
            for item in items
            if isinstance(item, dict) and isinstance(item.get('symbol'), str)
        }

    def _observe_shadow(self, now, session):
        """Create research-only shadow events; formal alerts stay untouched."""
        items = self._shadow_snapshot()
        for symbol, item in items.items():
            state_key = 'shadow-detector:' + symbol
            state = self.store.state(state_key, {})
            try:
                shadow = item.get('shadow')
                if not isinstance(shadow, dict) or shadow.get('status') != 'AVAILABLE':
                    raise ValueError('shadow snapshot unavailable')
                if shadow.get('strategy_version') != 'SWING_V2_SHADOW':
                    raise ValueError('shadow strategy version invalid')
                variants = shadow.get('variants')
                if not isinstance(variants, dict):
                    raise ValueError('shadow variants invalid')
                candidates = []
                for variant, decision in variants.items():
                    if variant not in {'V2_A', 'V2_B', 'V2_C'} or not isinstance(decision, dict):
                        continue
                    if decision.get('strategy_version') != 'SWING_V2_SHADOW':
                        continue
                    data_version = decision.get('data_version')
                    indicator_version = decision.get('indicator_version')
                    as_of = decision.get('evidence', {}).get('as_of_trading_date') if isinstance(
                        decision.get('evidence'), dict
                    ) else None
                    if not all(isinstance(value, str) and value for value in (
                        data_version, indicator_version, as_of,
                    )):
                        continue
                    alerts = build_alerts(
                        symbol=symbol,
                        shadow_state=decision.get('state'),
                        data_status=str(shadow.get('data_quality_status', 'UNKNOWN')),
                        snapshot_only=shadow.get('snapshot_only') is True,
                        data_healthy=shadow.get('data_healthy') is True,
                        account_known=shadow.get('account_known') is True,
                        cost_ok=shadow.get('cost_ok') is True,
                        risk_ok=shadow.get('risk_ok') is True,
                        opportunity_id=decision.get('opportunity_id') or (
                            shadow.get('opportunity') or {}
                        ).get('opportunity_id')
                        if isinstance(shadow.get('opportunity'), dict) else None,
                        blocked_reasons=decision.get('blocked_reasons', ()),
                    )
                    candidates.extend((variant, alert, decision, data_version,
                                       indicator_version, as_of) for alert in alerts.shadow)
                if not session.active or not candidates:
                    state['active'] = False
                    state['identities'] = []
                    self.store.set_state(state_key, state)
                    continue
                identities = {
                    value for value in state.get('identities', []) if isinstance(value, str)
                }
                emitted = False
                for variant, alert, decision, data_version, indicator_version, as_of in candidates:
                    identity = '|'.join((
                        symbol, variant, alert.opportunity_id or '', data_version,
                        indicator_version, as_of,
                    ))
                    if identity in identities:
                        continue
                    event = self._event(
                        'SHADOW_RESEARCH', symbol, variant,
                        {
                            'name': item.get('name', symbol),
                            'strategy_version': alert.strategy_version,
                            'variant': variant,
                            'state': alert.state,
                            'opportunity_id': alert.opportunity_id,
                            'blocked_reasons': list(alert.blocked_reasons),
                            'data_version': data_version,
                            'indicator_version': indicator_version,
                            'as_of_trading_date': as_of,
                            'executable': False,
                            'reason': '影子策略技术候选，仅供研究核对',
                        }, now,
                    )
                    self.store.add_event(event)
                    identities.add(identity)
                    state.update(active=True, event_id=event['id'])
                    emitted = True
                state['identities'] = sorted(identities)
                if not emitted:
                    state['active'] = True
                self.store.set_state(state_key, state)
            except Exception:
                # Shadow research is isolated: malformed shadow data must not
                # disable formal V1 fault/anomaly/recovery notifications.
                state['active'] = False
                state['identities'] = []
                self.store.set_state(state_key, state)

    def tick(self):
        with self._mutex:
            try:
                now=self._checked_now()
                if self.error: return
                session=market_session_state(now,self.closed_dates)
                inputs=self._inputs()
                with self.store.transaction():
                    self._items=[self._observe(item,now,session) for item in inputs]
                    self._observe_shadow(now, session)
                    # Include delayed retries, not just jobs whose retry is due.
                    indexed={item['symbol']:item for item in inputs}
                    for event in self.store.pending(datetime.max.replace(tzinfo=SHANGHAI)):
                        if not self._valid(event,indexed,now):
                            self.store.update_event(event['id'],status='CANCELLED',reason='已过期、条件失效或非交易时段')
                self.last_checked_at=now.isoformat()
                self._wake.set()
            except Exception:
                self.error='通知检测不可用，自动邮件已暂停；请检查数据、时钟或通知记录'
                self.config['enabled']=False

    def _valid(self,event,inputs,now):
        if self._stop.is_set() or self.read_only or self.error or event['payload'].get('epoch')!=self.epoch: return False
        if datetime.fromisoformat(event['expires_at'])<=now: return False
        if event['kind'] in {'CONNECTION_TEST','TEST_EMAIL'}: return True
        if not self.config['enabled'] or not self._verified() or not market_session_state(now,self.closed_dates).active: return False
        if event['kind'] == 'SHADOW_RESEARCH':
            return self._valid_shadow_event(event)
        item=inputs.get(event['symbol'])
        if not item or not item['eligible']: return False
        evaluation=evaluate_anomaly(item,now,self._rule(event['symbol'])['threshold_pct'],self.closed_dates)
        healthy=self._healthy(item,now)
        if event['kind']=='FAULT':
            category=item['health_status'] if item['health_status']!='REALTIME' else 'DATA_ERROR'
            return self.config['health_enabled'] and not healthy and event['payload'].get('fault_category')==category
        if event['kind']=='RECOVERY': return self.config['health_enabled'] and healthy
        if event['kind']=='ANOMALY' and self.config['anomaly_enabled']:
            result=evaluation
            return result['status']=='READY' and result['direction']==event['direction']
        return False

    def _valid_shadow_event(self, event):
        try:
            payload = event['payload']
            item = self._shadow_snapshot().get(event['symbol'])
            if not isinstance(item, dict):
                return False
            shadow = item.get('shadow')
            if not isinstance(shadow, dict):
                return False
            if (
                shadow.get('status') != 'AVAILABLE'
                or shadow.get('strategy_version') != 'SWING_V2_SHADOW'
                or shadow.get('executable') is not False
                or shadow.get('data_quality_status') != 'VERIFIED'
            ):
                return False
            if any(shadow.get(key) is not True for key in (
                'data_healthy', 'account_known', 'cost_ok', 'risk_ok',
            )) or shadow.get('snapshot_only') is True:
                return False
            variants = shadow.get('variants')
            if not isinstance(variants, dict):
                return False
            variant = payload.get('variant')
            decision = variants.get(variant)
            if not isinstance(decision, dict):
                return False
            evidence = decision.get('evidence')
            return (
                variant in {'V2_A', 'V2_B', 'V2_C'}
                and decision.get('strategy_version') == 'SWING_V2_SHADOW'
                and decision.get('state') == 'TECHNICAL_CANDIDATE'
                and decision.get('executable') is False
                and decision.get('opportunity_id') == payload.get('opportunity_id')
                and decision.get('data_version') == payload.get('data_version')
                and decision.get('indicator_version') == payload.get('indicator_version')
                and isinstance(evidence, dict)
                and evidence.get('as_of_trading_date') == payload.get('as_of_trading_date')
            )
        except Exception:
            return False

    @staticmethod
    def _message(events):
        names={'ANOMALY':'行情异动观察','FAULT':'监控受限','RECOVERY':'监控恢复','TEST_EMAIL':'测试邮件','SHADOW_RESEARCH':'波段影子研究通知'}
        title='本地ETF监控 · '+(names.get(events[0]['kind'],'通知') if len({e['kind'] for e in events})==1 else '观察与监控状态汇总')
        lines=[]
        for event in events:
            p=event['payload']
            lines += [names.get(event['kind'],'通知')+' '+p.get('name','')+' '+event['symbol'],
                      '检测时间：'+event['created_at'], '有效截止：'+event['expires_at']]
            if event['kind']=='ANOMALY':
                lines += ['行情时间：'+str(p.get('timestamp','')), '已完成分钟价格：'+str(p.get('price','')),
                          '五分钟涨跌幅：'+format(p['change_pct'],'.2f')+'%']
            elif event['kind']=='SHADOW_RESEARCH':
                lines += [
                    '策略版本：'+str(p.get('strategy_version','SWING_V2_SHADOW')),
                    '变体：'+str(p.get('variant','')),
                    '机会ID：'+str(p.get('opportunity_id') or '无'),
                    '状态：技术候选（研究层）',
                    '执行标记：False（不会生成可执行买入订单）',
                ]
            elif event['kind']=='TEST_EMAIL': lines += ['这是一封连接验收测试邮件，不包含账户信息。']
            else: lines += ['行情时间：'+str(p.get('timestamp','')),p.get('reason','连续新分钟已确认行情恢复')]
            lines += ['']
        lines += ['仅观察，不是交易指令。邮件阅读时可能已失效，请核验最新行情。',
                  '本地通知中心需在运行服务的电脑上打开；不提供手机公网访问。']
        return title,'\n'.join(lines)

    def deliver_once(self):
        if not self._delivery.acquire(blocking=False): return
        try:
            with self._mutex:
                now=self._checked_now()
                pending=self.store.pending(now)
                if not pending: return
                try: inputs={item['symbol']:item for item in self._inputs()}
                except Exception: inputs={}
                valid=[]
                for event in pending:
                    if not self._valid(event,inputs,now):
                        self.store.update_event(event['id'],status='CANCELLED',reason='已过期、条件失效或通知暂停')
                    else: valid.append(event)
                if not valid: return
                first=valid[0]
                # Do not combine explicit tests with observations or mix cycles.
                tests={'CONNECTION_TEST','TEST_EMAIL'}
                group=([first] if first['kind'] in tests else
                       [e for e in valid if e['kind'] not in tests and e['payload']['cycle']==first['payload']['cycle']])
                claimed=[e for e in group if self.store.claim(e['id'])]
                if not claimed: return
                config=copy.deepcopy(self.config)
                epoch=self.epoch
                try: secret=self.secrets.load()
                except Exception: secret=None
            def cancelled():
                with self._mutex:
                    try:
                        check_time=self._checked_now()
                        current={item['symbol']:item for item in self._inputs()}
                        return any(not self._valid(event,current,check_time) for event in claimed)
                    except Exception:
                        return True
            if not secret:
                result={'status':'FAILED','reason':'授权码不可用','retryable':False}
            else:
                try:
                    if first['kind']=='CONNECTION_TEST': result=self.transport.check(config,secret,cancelled=cancelled)
                    else:
                        subject,body=self._message(claimed)
                        result=self.transport.send(config,secret,subject,body,'<'+first['id']+'@local-etf-monitor>',cancelled=cancelled)
                except Exception:
                    result={'status':'UNKNOWN','reason':'投递结果无法确认，不自动重发','retryable':False}
            with self._mutex:
                for event in claimed:
                    attempt=event['attempts']+1
                    status=result.get('status','UNKNOWN')
                    if status not in {'CONNECTION_OK','SERVER_ACCEPTED','FAILED','UNKNOWN','CANCELLED'}: status='UNKNOWN'
                    fields={'status':status,'reason':str(result.get('reason',''))[:200]}
                    if status=='FAILED' and result.get('retryable') is True and attempt<=3:
                        retry=self._now()+timedelta(seconds=(30,60,120)[attempt-1])
                        if retry<datetime.fromisoformat(event['expires_at']):
                            fields.update(status='PENDING',next_attempt_at=retry.isoformat())
                    self.store.update_event(event['id'],**fields)
                if self.epoch==epoch:
                    if first['kind']=='CONNECTION_TEST': self.checks['connection']=result.get('status')=='CONNECTION_OK'
                    if first['kind']=='TEST_EMAIL': self.checks['email']=result.get('status')=='SERVER_ACCEPTED'
                    self.store.set_state('checks',self.checks)
                    if self.config['enabled'] and not self._verified():
                        self.config={**self.config,'enabled':False}
                        self.config_store.save(self.config)
                        self.store.cancel_pending('邮箱验证失效，自动邮件已暂停')
        except Exception:
            with self._mutex:
                self.error='邮件队列不可用，自动邮件已暂停'
                self.config['enabled']=False
        finally: self._delivery.release()

    def start(self):
        if self._threads: return
        self._stop.clear()
        def detector():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(10)
        def sender():
            while not self._stop.is_set():
                self._wake.wait(5); self._wake.clear()
                if not self._stop.is_set(): self.deliver_once()
        self._threads=[threading.Thread(target=detector,daemon=True,name='notification-detector'),
                       threading.Thread(target=sender,daemon=True,name='notification-sender')]
        for thread in self._threads: thread.start()

    def stop(self):
        self._stop.set(); self._wake.set()
        for thread in self._threads: thread.join(timeout=12)
        if not any(t.is_alive() for t in self._threads): self.runtime_lock.release()

    def replay(self,symbol,trading_date):
        if type(symbol) is not str: raise ValueError('回放标的无效')
        held={item['symbol'] for item in self._inputs(remember_connected=False)}
        if symbol not in held: raise ValueError('只能回放已持有ETF')
        rule=self._rule(symbol)
        return audited_replay(self.monitor,symbol,trading_date,rule['threshold_pct'],rule['cooldown_minutes'])
