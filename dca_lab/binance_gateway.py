"""Fixed-host Binance adapter and fail-closed, journaled cash-sweep service."""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlencode

from .config import ALL_ASSETS

BASE = 'https://api.binance.com'
FLEX = '/sapi/v1/simple-earn/flexible/'
ALLOWED = {('GET','/api/v3/time'),('GET','/api/v3/account'),('GET','/api/v3/exchangeInfo'),
           ('GET','/api/v3/order'),('POST','/api/v3/order'),('POST','/api/v3/order/test'),
           *(('GET',FLEX+p) for p in ('list','position','personalLeftQuota')),
           *(('POST',FLEX+p) for p in ('subscribe','redeem'))}


class GatewayError(Exception):
    def __init__(self,message,uncertain=False):
        super().__init__(message)
        self.uncertain=uncertain


def number(value,positive=False):
    try:
        result=Decimal(str(value))
    except (InvalidOperation,ValueError):
        raise ValueError('金额无效') from None
    if not result.is_finite() or result<0 or (positive and result<=0) or result>Decimal('1e15'):
        raise ValueError('金额无效')
    return result


def decimal_text(value):
    return format(value,'f')


class BinanceClient:
    def __init__(self,key,secret,transport=None):
        import requests
        if not isinstance(key,str) or not isinstance(secret,str) or not 8<=len(key)<=512 or not 8<=len(secret)<=512:
            raise ValueError('API Key / Secret无效')
        if any(c.isspace() for c in key+secret):
            raise ValueError('密钥不能包含空白')
        self.key,self.secret=key,secret
        self.transport=transport or requests.Session()
        if transport is None:
            # Credentials must not inherit environment proxies or .netrc auth.
            self.transport.trust_env=False
        self.offset=0
        self.synced=0.

    def __repr__(self):
        return '<BinanceClient credentials=redacted>'

    def request(self,method,path,params=None,signed=True):
        if (method,path) not in ALLOWED:
            raise ValueError('接口不在允许列表')
        values=dict(params or {})
        headers={}
        if signed:
            if time.monotonic()-self.synced>30:
                server=self.request('GET','/api/v3/time',signed=False)
                self.offset=int(server['serverTime'])-int(time.time()*1000)
                self.synced=time.monotonic()
            values.update(timestamp=int(time.time()*1000)+self.offset,recvWindow=5000)
            headers['X-MBX-APIKEY']=self.key
        encoded=urlencode(values)
        if signed:
            signature=hmac.new(self.secret.encode(),encoded.encode(),hashlib.sha256).hexdigest()
            encoded+='&signature='+signature
        try:
            response=self.transport.request(method,BASE+path,
                params=encoded if method=='GET' else None,data=encoded if method=='POST' else None,
                headers={**headers,**({'Content-Type':'application/x-www-form-urlencoded'} if method=='POST' else {})},
                timeout=(5,20),allow_redirects=False)
        except Exception:
            # Never expose a requests exception: it can contain a signed URL.
            raise GatewayError('币安连接超时或中断',uncertain=method=='POST') from None
        try:
            payload=response.json()
        except Exception:
            raise GatewayError('币安响应不可解析',uncertain=method=='POST') from None
        if response.status_code!=200 or (isinstance(payload,dict) and isinstance(payload.get('code'),int) and payload['code']<0):
            code=payload.get('code') if isinstance(payload,dict) else None
            known_rejection=response.status_code in (400,401,403,418,429) and isinstance(code,int) and code<0 and code not in (-1006,-1007)
            raise GatewayError('币安接口拒绝请求'+('（代码 '+str(code)+'）' if isinstance(code,int) else ''),
                uncertain=method=='POST' and not known_rejection)
        return payload

    def account(self):
        return self.request('GET','/api/v3/account')

    def products(self):
        rows=[]
        for page in range(1,11):
            payload=self.request('GET',FLEX+'list',{'asset':'USDT','current':page,'size':100})
            batch=payload.get('rows',[])
            rows.extend(p for p in batch if p.get('asset')=='USDT')
            if len(rows)>=payload.get('total',len(rows)) or len(batch)<100:
                return rows
        raise GatewayError('理财产品分页超限')

    def positions(self,product_id):
        return self.request('GET',FLEX+'position',{'asset':'USDT','productId':product_id,'size':100}).get('rows',[])


class OperationJournal:
    def __init__(self,path):
        path=Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, kind TEXT, state TEXT, request TEXT, result TEXT, created REAL)')
        self.db.commit()

    def get(self,key):
        row=self.db.execute('SELECT id,kind,state,result FROM operations WHERE id=?',(key,)).fetchone()
        return {'id':row[0],'kind':row[1],'state':row[2],'result':json.loads(row[3]) if row[3] else None} if row else None

    def blocked(self):
        return bool(self.db.execute("SELECT 1 FROM operations WHERE state IN ('pending','unknown') LIMIT 1").fetchone())

    def recent(self):
        ids=[row[0] for row in self.db.execute('SELECT id FROM operations ORDER BY created DESC LIMIT 20').fetchall()]
        return [self.get(key) for key in ids]

    def start(self,key,kind,request):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.blocked():
                raise GatewayError('存在未核对请求，禁止重复资金操作')
            self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',(key,kind,'pending',json.dumps(request),None,time.time()))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def finish(self,key,state,result):
        self.db.execute('UPDATE operations SET state=?, result=? WHERE id=?',(state,json.dumps(result),key))
        self.db.commit()
        return self.get(key)

    def close(self):
        self.db.close()


class Gateway:
    def __init__(self,journal_path,allow_live=False,client_factory=BinanceClient):
        self.journal=OperationJournal(journal_path)
        self.allow_live=allow_live
        self.client_factory=client_factory
        self.client=None
        self.mode='paper'
        self.auto_earn=False
        self.reserve=Decimal('60')
        self.product_id=''
        self.free=None
        self.earn=None
        self.last_action=None
        self.lock=threading.RLock()
        self.stop=threading.Event()

    def status(self):
        with self.lock:
            return {'connected':self.client is not None,'mode':self.mode,'live_enabled':self.allow_live,
                'auto_earn':self.auto_earn,'reserve_usdt':str(self.reserve),'product_id':self.product_id,
                'free_usdt':str(self.free) if self.free is not None else None,
                'earn_usdt':str(self.earn) if self.earn is not None else None,
                'blocked':self.journal.blocked(),'last_action':self.last_action}

    def operations(self):
        with self.lock:
            return self.journal.recent()

    def connect(self,key,secret):
        with self.lock:
            client=self.client_factory(key,secret)
            account=client.account()
            if account.get('accountType') not in (None,'SPOT'):
                raise ValueError('仅支持现货账户')
            self.client=client
            self.auto_earn=False
            self.mode='paper'
            self.free=self._free(account)
            self.earn=None
            self.product_id=''
            return self.status()

    def disconnect(self):
        with self.lock:
            self.client=None
            self.auto_earn=False
            self.mode='paper'
            self.free=self.earn=None
            self.product_id=''
            return self.status()

    def configure(self,body):
        with self.lock:
            if set(body)-{'mode','auto_earn','reserve_usdt','product_id'}:
                raise ValueError('未知设置')
            mode=body.get('mode',self.mode)
            if mode not in ('paper','read_only','live') or (mode=='live' and not self.allow_live):
                raise ValueError('当前服务未开放实盘')
            reserve=number(body.get('reserve_usdt',self.reserve))
            auto=body.get('auto_earn',self.auto_earn)
            if not isinstance(auto,bool):
                raise ValueError('auto_earn必须为布尔值')
            product=body.get('product_id',self.product_id)
            if not isinstance(product,str) or (product and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',product)):
                raise ValueError('产品ID无效')
            if auto and (self.client is None or not product or mode=='read_only'):
                raise ValueError('请先连接账户、选择产品和执行模式')
            if auto:
                candidates=[p for p in self._client().products() if p.get('productId')==product and self._eligible(p)]
                if len(candidates)!=1:
                    raise ValueError('所选USDT活期产品当前不可用')
            self.mode,self.reserve,self.auto_earn,self.product_id=mode,reserve,auto,product
            return self.status()

    @staticmethod
    def _free(account):
        values=[number(x['free']) for x in account.get('balances',[]) if x.get('asset')=='USDT']
        return sum(values,Decimal(0))

    def _client(self):
        if not self.client:
            raise ValueError('尚未连接币安API')
        return self.client

    def refresh(self):
        with self.lock:
            client=self._client()
            self.free=self._free(client.account())
            if self.product_id:
                rows=client.positions(self.product_id)
                self.earn=sum((number(x.get('totalAmount','0')) for x in rows if x.get('asset')=='USDT' and x.get('productId')==self.product_id),Decimal(0))
            return self.status()

    def products(self):
        with self.lock:
            return [{'product_id':p['productId'],'asset':'USDT','apr':p.get('latestAnnualPercentageRate'),
                     'minimum':p.get('minPurchaseAmount'),'available':self._eligible(p)} for p in self._client().products()]

    @staticmethod
    def _eligible(product):
        return (product.get('asset')=='USDT' and product.get('status')=='PURCHASING'
                and product.get('canPurchase') is True and product.get('canRedeem') is True and product.get('isSoldOut') is False)

    def _product(self):
        rows=[p for p in self._client().products() if p.get('productId')==self.product_id]
        if len(rows)!=1 or not self._eligible(rows[0]):
            raise ValueError('所选USDT活期产品当前不可用')
        return rows[0]

    def earn_preview(self):
        with self.lock:
            client=self._client()
            product=self._product()
            self.free=self._free(client.account())
            quota=number(client.request('GET',FLEX+'personalLeftQuota',{'productId':self.product_id})['leftPersonalQuota'])
            amount=min(max(Decimal(0),self.free-self.reserve),quota).quantize(Decimal('.00000001'),rounding=ROUND_DOWN)
            minimum=number(product['minPurchaseAmount'],positive=True)
            if amount<minimum:
                amount=Decimal(0)
            return {'kind':'earn_subscribe','asset':'USDT','product_id':self.product_id,
                'free_usdt':str(self.free),'reserve_usdt':str(self.reserve),'amount':str(amount),
                'minimum':str(minimum),'quota':str(quota),'mode':self.mode}

    @staticmethod
    def _op_id(value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}',value):
            raise ValueError('需要8–64位幂等请求ID')
        return value

    def _write(self,key,kind,params,path):
        key=self._op_id(key)
        existing=self.journal.get(key)
        if existing:
            if existing['kind']!=kind:
                raise ValueError('幂等ID已用于其他操作')
            return existing
        if self.mode=='read_only':
            raise ValueError('只读模式不允许资金操作')
        if self.journal.blocked():
            raise GatewayError('存在结果未知的请求，已暂停资金操作；需先核对币安记录')
        if self.mode=='paper':
            self.journal.start(key,kind,params)
            self.last_action=self.journal.finish(key,'simulated',{'mode':'paper','submitted':False,'params':params})
            return self.last_action
        if not self.allow_live:
            raise ValueError('实盘未开放')
        self.journal.start(key,kind,params)
        try:
            result=self._client().request('POST',path,params)
            if kind.startswith('earn_') and result.get('success') is not True:
                raise GatewayError('理财返回结果不确定',uncertain=True)
            if kind=='spot_buy' and (not isinstance(result,dict) or not result.get('orderId')):
                raise GatewayError('下单返回结果不确定',uncertain=True)
        except GatewayError as exc:
            state='unknown' if exc.uncertain else 'failed'
            self.last_action=self.journal.finish(key,state,{'message':str(exc)})
            if state=='unknown':
                self.auto_earn=False
            return self.last_action
        except Exception:
            self.auto_earn=False
            self.last_action=self.journal.finish(key,'unknown',{'message':'请求结果未知，禁止自动重试'})
            return self.last_action
        # Store identifiers/status, never account payloads or signed URLs.
        clean={k:result[k] for k in ('purchaseId','redeemId','success','orderId','clientOrderId','status','executedQty','cummulativeQuoteQty') if k in result}
        self.last_action=self.journal.finish(key,'completed',clean)
        return self.last_action

    def sweep(self,request_id):
        with self.lock:
            existing=self.journal.get(self._op_id(request_id))
            if existing:
                if existing['kind']!='earn_subscribe':
                    raise ValueError('幂等ID操作类型不符')
                return existing
            preview=self.earn_preview()
            if number(preview['amount'])==0:
                return {'state':'skipped','reason':'没有满足最低申购金额的闲置USDT','preview':preview}
            return self._write(request_id,'earn_subscribe',{'productId':self.product_id,
                'amount':preview['amount'],'autoSubscribe':'false','sourceAccount':'SPOT'},FLEX+'subscribe')

    def redeem(self,amount,request_id):
        with self.lock:
            existing=self.journal.get(self._op_id(request_id))
            if existing:
                if existing['kind']!='earn_redeem':
                    raise ValueError('幂等ID操作类型不符')
                return existing
            amount=number(amount,positive=True)
            self.refresh()
            if self.earn is None or amount>self.earn:
                raise ValueError('理财持仓不足')
            return self._write(request_id,'earn_redeem',{'productId':self.product_id,'amount':str(amount),
                'redeemAll':'false','destAccount':'SPOT'},FLEX+'redeem')

    def order_preview(self,asset,amount,limit_price):
        with self.lock:
            if asset not in ALL_ASSETS:
                raise ValueError('仅支持已配置的七个现货标的')
            client=self._client()
            amount,price=number(amount,positive=True),number(limit_price,positive=True)
            symbol=ALL_ASSETS[asset]['symbol']
            rows=client.request('GET','/api/v3/exchangeInfo',{'symbol':symbol},signed=False).get('symbols',[])
            if len(rows)!=1 or rows[0]['symbol']!=symbol or rows[0]['status']!='TRADING' or rows[0].get('isSpotTradingAllowed') is not True or rows[0].get('quoteAsset')!='USDT':
                raise ValueError('现货交易对当前不可用')
            filters={f['filterType']:f for f in rows[0]['filters']}
            tick=number(filters['PRICE_FILTER']['tickSize'],positive=True)
            step=number(filters['LOT_SIZE']['stepSize'],positive=True)
            price=(price/tick).to_integral_value(rounding=ROUND_DOWN)*tick
            if not number(filters['PRICE_FILTER']['minPrice'])<=price<=number(filters['PRICE_FILTER']['maxPrice']):
                raise ValueError('价格不满足交易规则')
            quantity=(amount/Decimal('1.001')/price/step).to_integral_value(rounding=ROUND_DOWN)*step
            lot=filters['LOT_SIZE']
            if not number(lot['minQty'])<=quantity<=number(lot['maxQty']) or quantity<=0:
                raise ValueError('数量不满足交易规则')
            notional=quantity*price
            for kind in ('MIN_NOTIONAL','NOTIONAL'):
                f=filters.get(kind,{})
                if notional<number(f.get('minNotional',0)) or notional>number(f.get('maxNotional','1e15')):
                    raise ValueError('金额不满足交易规则')
            self.free=self._free(client.account())
            if self.free<amount:
                raise ValueError('现货USDT不足；理财未到账金额不计入可用余额')
            return {'symbol':symbol,'side':'BUY','type':'LIMIT','timeInForce':'IOC',
                'quantity':decimal_text(quantity),'price':decimal_text(price),'newOrderRespType':'RESULT'}

    def order(self,asset,amount,limit_price,request_id,test=False):
        with self.lock:
            request_id=self._op_id(request_id)
            existing=self.journal.get(request_id)
            if existing:
                if existing['kind']!='spot_buy':
                    raise ValueError('幂等ID操作类型不符')
                return existing
            params=self.order_preview(asset,amount,limit_price)
            params['newClientOrderId']='dca_'+hashlib.sha256(request_id.encode()).hexdigest()[:28]
            if test:
                result=self._client().request('POST','/api/v3/order/test',params)
                return {'state':'validated','submitted':False,'result':result}
            return self._write(request_id,'spot_buy',params,'/api/v3/order')

    def worker(self):
        while not self.stop.wait(300):
            with self.lock:
                if not self.auto_earn or self.client is None:
                    continue
                try:
                    result=self.sweep('auto_'+secrets.token_hex(12))
                    if result.get('state') in ('failed','unknown'):
                        self.auto_earn=False
                except Exception:
                    self.auto_earn=False
                    self.last_action={'state':'paused','result':{'message':'自动理财暂停，需检查账户或产品状态'}}

    def close(self):
        self.stop.set()
        with self.lock:
            self.client=None
            self.journal.close()
