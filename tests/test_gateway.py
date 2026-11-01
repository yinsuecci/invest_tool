import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

from dca_lab.binance_gateway import BinanceClient, Gateway, GatewayError, FLEX, BASE, number
from dca_lab.compact_app import build, handler_factory, OPENAPI
from http.server import ThreadingHTTPServer


PRODUCT={'asset':'USDT','productId':'USDT_FLEX','canPurchase':True,'canRedeem':True,
         'isSoldOut':False,'status':'PURCHASING','minPurchaseAmount':'1','latestAnnualPercentageRate':'0.02'}


class FakeClient:
    def __init__(self,*args):
        self.free=Decimal('1000')
        self.balance=Decimal('100')
        self.quota='5000'
        self.rows=[dict(PRODUCT)]
        self.calls=[]
        self.fail=None

    def account(self):
        return {'accountType':'SPOT','balances':[{'asset':'USDT','free':str(self.free),'locked':'999'}]}

    def products(self):
        return self.rows

    def positions(self,product_id):
        return [{'asset':'USDT','productId':product_id,'totalAmount':str(self.balance)}]

    def request(self,method,path,params=None,signed=True):
        self.calls.append((method,path,dict(params or {})))
        if method=='POST' and self.fail:
            raise self.fail
        if path==FLEX+'personalLeftQuota':
            return {'leftPersonalQuota':self.quota}
        if path==FLEX+'subscribe':
            self.free-=Decimal(params['amount'])
            self.balance+=Decimal(params['amount'])
            return {'success':True,'purchaseId':123}
        if path==FLEX+'redeem':
            return {'success':True,'redeemId':456}
        if path=='/api/v3/exchangeInfo':
            return {'symbols':[{'symbol':params['symbol'],'status':'TRADING','isSpotTradingAllowed':True,'quoteAsset':'USDT',
                'filters':[{'filterType':'PRICE_FILTER','tickSize':'.01','minPrice':'.01','maxPrice':'1000000'},
                           {'filterType':'LOT_SIZE','stepSize':'.001','minQty':'.001','maxQty':'100000'},
                           {'filterType':'MIN_NOTIONAL','minNotional':'5'}]}]}
        if path=='/api/v3/order/test':
            return {}
        if path=='/api/v3/order':
            return {'orderId':77,'clientOrderId':params['newClientOrderId'],'status':'FILLED'}
        raise AssertionError((method,path))


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path.cwd())
        self.path=Path(self.tmp.name)/'operations.sqlite'
        self.gateway=Gateway(self.path,allow_live=True,client_factory=FakeClient)
        self.gateway.connect('fixture-key','fixture-secret')
        self.gateway.configure({'product_id':'USDT_FLEX'})

    def tearDown(self):
        self.gateway.close()
        self.tmp.cleanup()

    def test_paper_is_default_and_sweep_never_posts(self):
        result=self.gateway.sweep('paper-0001')
        self.assertEqual(result['state'],'simulated')
        self.assertFalse(any(c[0]=='POST' for c in self.gateway.client.calls))
        self.assertEqual(self.gateway.client.free,Decimal('1000'))
        self.assertEqual(result['result']['params']['amount'],'940.00000000')
        self.assertEqual(result,self.gateway.sweep('paper-0001'))

    def test_sweep_reserve_quota_minimum_and_no_platform_auto_subscribe(self):
        self.gateway.configure({'mode':'live'})
        self.gateway.client.quota='12.123456789'
        result=self.gateway.sweep('live-0001')
        self.assertEqual(result['state'],'completed')
        params=self.gateway.client.calls[-1][2]
        self.assertEqual(params['amount'],'12.12345678')
        self.assertEqual(params['autoSubscribe'],'false')
        self.assertEqual(params['sourceAccount'],'SPOT')
        self.assertGreaterEqual(self.gateway.client.free,self.gateway.reserve)
        self.gateway.sweep('live-0001')
        self.assertEqual(sum(c[0]=='POST' for c in self.gateway.client.calls),1)
        self.gateway.client.free=Decimal('60.5')
        self.assertEqual(self.gateway.sweep('live-0002')['state'],'skipped')

    def test_unavailable_products_and_read_only_fail_closed(self):
        for field,value in [('isSoldOut',True),('canPurchase',False),('canRedeem',False),('status','END'),('asset','BTC')]:
            self.gateway.client.rows=[{**PRODUCT,field:value}]
            with self.assertRaises(ValueError):
                self.gateway.sweep('invalid-0001')
        self.gateway.client.rows=[dict(PRODUCT)]
        self.gateway.configure({'mode':'read_only'})
        with self.assertRaises(ValueError):
            self.gateway.sweep('readonly-0001')
        self.assertFalse(any(c[0]=='POST' for c in self.gateway.client.calls))

    def test_live_requires_server_opt_in_and_auto_requires_product(self):
        self.gateway.allow_live=False
        with self.assertRaises(ValueError):
            self.gateway.configure({'mode':'live'})
        with self.assertRaises(ValueError):
            self.gateway.configure({'auto_earn':True,'product_id':'BAD'})
        self.gateway.configure({'auto_earn':True})
        self.assertTrue(self.gateway.auto_earn)
        self.gateway.disconnect()
        self.assertFalse(self.gateway.auto_earn)
        self.assertIsNone(self.gateway.client)

    def test_unknown_write_is_durable_blocks_retries_and_stops_auto(self):
        self.gateway.configure({'mode':'live','auto_earn':True})
        self.gateway.client.fail=GatewayError('timeout',uncertain=True)
        result=self.gateway.sweep('unknown-0001')
        self.assertEqual(result['state'],'unknown')
        self.assertFalse(self.gateway.auto_earn)
        self.assertTrue(self.gateway.status()['blocked'])
        self.assertEqual(self.gateway.sweep('unknown-0001'),result)
        with self.assertRaises(GatewayError):
            self.gateway.sweep('unknown-0002')
        self.assertEqual(sum(c[0]=='POST' for c in self.gateway.client.calls),1)
        self.gateway.close()
        self.gateway=Gateway(self.path,allow_live=True,client_factory=FakeClient)
        self.assertTrue(self.gateway.status()['blocked'])

    def test_pending_crash_blocks_and_failed_rejection_is_recorded(self):
        self.gateway.journal.start('crash-0001','earn_subscribe',{})
        self.assertTrue(self.gateway.journal.blocked())
        with self.assertRaises(GatewayError):
            self.gateway.journal.start('crash-0002','earn_subscribe',{})
        self.gateway.journal.finish('crash-0001','failed',{})
        self.gateway.configure({'mode':'live'})
        self.gateway.client.fail=GatewayError('rejected')
        self.assertEqual(self.gateway.sweep('reject-0001')['state'],'failed')
        self.assertFalse(self.gateway.journal.blocked())

    def test_spot_order_is_rounded_limit_ioc_and_repeated_once(self):
        self.gateway.configure({'mode':'live'})
        result=self.gateway.order('SOL','7.5','100.009','spot-order-0001')
        self.assertEqual(result['state'],'completed')
        params=self.gateway.client.calls[-1][2]
        self.assertEqual((params['side'],params['type'],params['timeInForce']),('BUY','LIMIT','IOC'))
        self.assertEqual(params['price'],'100.00')
        self.assertLessEqual(Decimal(params['quantity'])*Decimal(params['price'])*Decimal('1.001'),Decimal('7.5'))
        self.gateway.order('SOL','7.5','100.009','spot-order-0001')
        self.assertEqual(sum(c[0]=='POST' for c in self.gateway.client.calls),1)

    def test_spot_rejects_low_amount_insufficient_balance_and_unknown_assets(self):
        for args in [('SOL','1','100'),('NOT_ALLOWED','7.5','100'),('SOL','2000','100')]:
            with self.assertRaises(ValueError):
                self.gateway.order_preview(*args)
        self.assertFalse(any(c[0]=='POST' for c in self.gateway.client.calls))

    def test_test_order_is_not_a_real_submission(self):
        result=self.gateway.order('SOL','7.5','100','test-order-0001',test=True)
        self.assertFalse(result['submitted'])
        self.assertEqual(self.gateway.client.calls[-1][1],'/api/v3/order/test')
        self.assertIsNone(self.gateway.journal.get('test-order-0001'))

    def test_redeem_does_not_credit_unsettled_spot(self):
        self.gateway.configure({'mode':'live'})
        self.gateway.client.free=Decimal('0')
        self.assertEqual(self.gateway.redeem('20','redeem-0001')['state'],'completed')
        with self.assertRaises(ValueError):
            self.gateway.order_preview('SOL','7.5','100')
        with self.assertRaises(ValueError):
            self.gateway.redeem('200','redeem-0002')

    def test_money_inputs_reject_nonfinite_and_negative(self):
        for value in ('NaN','Infinity','-1','abc',True):
            with self.assertRaises(ValueError):
                number(value)


class FakeResponse:
    def __init__(self,payload,status=200):
        self.payload,self.status_code=payload,status
    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self):
        self.calls=[]
        self.error=None
    def request(self,method,url,**kwargs):
        self.calls.append((method,url,kwargs))
        if self.error:
            raise RuntimeError(self.error)
        if url.endswith('/time'):
            import time
            return FakeResponse({'serverTime':int(time.time()*1000)})
        return FakeResponse({'ok':True})


class SigningTests(unittest.TestCase):
    def test_production_transport_does_not_inherit_environment(self):
        client=BinanceClient('fixture-key','fixture-secret')
        self.assertFalse(client.transport.trust_env)
        self.assertTrue(client.transport.verify)
        client.transport.close()

    def test_hmac_headers_fixed_host_and_redirects_disabled(self):
        transport=FakeTransport()
        client=BinanceClient('fixture-key','fixture-secret',transport)
        client.request('GET','/api/v3/account',{'x':'a b'})
        method,url,kwargs=transport.calls[-1]
        self.assertEqual(url,BASE+'/api/v3/account')
        self.assertEqual(kwargs['headers']['X-MBX-APIKEY'],'fixture-key')
        encoded,signature=kwargs['params'].rsplit('&signature=',1)
        expected=hmac.new(b'fixture-secret',encoded.encode(),hashlib.sha256).hexdigest()
        self.assertEqual(signature,expected)
        self.assertEqual(parse_qs(encoded)['recvWindow'],['5000'])
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('fixture-secret',repr(client))
        with self.assertRaises(ValueError):
            client.request('POST','/sapi/v1/capital/withdraw/apply')

    def test_transport_errors_never_expose_key_secret_or_signed_url(self):
        transport=FakeTransport()
        client=BinanceClient('fixture-key','fixture-secret',transport)
        transport.error='fixture-secret https://api.binance.com/path?signature=secret'
        with self.assertRaises(GatewayError) as caught:
            client.request('POST',FLEX+'subscribe',{})
        self.assertNotIn('fixture',str(caught.exception))
        self.assertNotIn('signature',str(caught.exception))


class LocalApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(dir=Path.cwd())
        cls.root=build('reports/independent-assets-001/report.json',Path(cls.tmp.name)/'app')
        cls.gateway=Gateway(Path(cls.tmp.name)/'ops.sqlite',client_factory=FakeClient)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),handler_factory(cls.root,cls.gateway))
        cls.port=cls.server.server_address[1]
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.gateway.close();cls.tmp.cleanup()

    def request(self,path,method='GET',body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        connection.request(method,path,body=body,headers=headers or {})
        response=connection.getresponse()
        data=response.read()
        result=(response.status,dict(response.getheaders()),data)
        connection.close()
        return result

    def auth(self):
        status,headers,body=self.request('/api/session')
        self.assertEqual(status,200)
        return {'Cookie':headers['Set-Cookie'].split(';')[0],'X-DCA-Token':json.loads(body)['csrf_token'],
                'Origin':'http://127.0.0.1:'+str(self.port),'Content-Type':'application/json'}

    def test_csrf_origin_host_and_private_state(self):
        self.assertEqual(self.request('/api/state')[0],403)
        self.assertEqual(self.request('/api/operations')[0],403)
        headers=self.auth()
        self.assertEqual(self.request('/api/state',headers=headers)[0],200)
        self.assertEqual(self.request('/api/operations',headers=headers)[0],200)
        self.assertEqual(self.request('/api/settings','POST','{}',{**headers,'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/api/settings','POST','{}',{**headers,'X-DCA-Token':'bad'})[0],403)
        self.assertEqual(self.request('/api/session',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('/api/session',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.request('/api/settings','POST','{}',{**headers,'Content-Type':'text/plain'})[0],400)

    def test_connection_redacts_credentials_and_disconnect_disables_auto(self):
        headers=self.auth()
        status,response_headers,body=self.request('/api/connect','POST',json.dumps({'api_key':'fixture-key','api_secret':'fixture-secret'}),headers)
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)['connected'])
        self.assertNotIn(b'fixture',body)
        self.assertEqual(response_headers['Cache-Control'],'no-store')
        self.assertEqual(self.request('/api/disconnect','POST','{}',headers)[0],200)
        self.assertFalse(self.gateway.status()['connected'])

    def test_only_allowlisted_static_files_and_valid_schema(self):
        self.assertEqual(self.request('/../dca_lab/config.py')[0],404)
        self.assertEqual(self.request('/runtime/operations.sqlite')[0],404)
        status,headers,body=self.request('/')
        self.assertEqual(status,200)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin',headers)
        schema=json.loads(self.request('/openapi.json')[2])
        self.assertEqual(schema['openapi'],'3.1.0')
        for methods in schema['paths'].values():
            for operation in methods.values():
                self.assertIn('200',operation['responses'])

    def test_render_preserves_backtest_and_no_credential_storage(self):
        html=(self.root/'index.html').read_text(encoding='utf-8')
        payload=html.split('<script id="report" type="application/json">',1)[1].split('</script>',1)[0]
        prior=json.loads(Path('reports/independent-assets-001/report.json').read_text(encoding='utf-8'))
        self.assertEqual(json.loads(payload),prior)
        self.assertNotIn('localStorage',html)
        self.assertNotIn('sessionStorage',html)
        self.assertIn('type="password"',html)
        self.assertIn("$('api-secret').value=''",html)


if __name__=='__main__':
    unittest.main()
