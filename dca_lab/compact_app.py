"""Minimal comparison dashboard with a local, authenticated Binance gateway."""

import hashlib
import http.server
import json
import secrets
import threading
from http.cookies import SimpleCookie
from pathlib import Path

from .binance_gateway import Gateway, GatewayError


OPENAPI = {
    'openapi':'3.1.0','info':{'title':'DCA Local Binance Gateway','version':'1.0.0'},
    'servers':[{'url':'/'}],
    'components':{'securitySchemes':{'csrf':{'type':'apiKey','in':'header','name':'X-DCA-Token'},
                                   'session':{'type':'apiKey','in':'cookie','name':'dca_session'}}},
    'security':[{'csrf':[],'session':[]}],
    'paths':{
        '/api/session':{'get':{'summary':'Get local session and CSRF token','security':[]}},
        '/api/state':{'get':{'summary':'Redacted connection / cash / Earn state'}},
        '/api/operations':{'get':{'summary':'Last20 journaled fund operations; no credentials or signatures'}},
        '/api/connect':{'post':{'summary':'Read-only connection test; credentials live only in process memory',
            'requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','required':['api_key','api_secret'],
                'properties':{'api_key':{'type':'string'},'api_secret':{'type':'string','writeOnly':True}}}}}}}},
        '/api/disconnect':{'post':{'summary':'Drop credentials and disable automatic Earn'}},
        '/api/settings':{'post':{'summary':'Set mode, auto_earn, reserve_usdt and explicit product_id'}},
        '/api/refresh':{'post':{'summary':'Refresh actual spot USDT and selected Earn position'}},
        '/api/earn/products':{'get':{'summary':'Query USDT flexible products and availability'}},
        '/api/earn/preview':{'post':{'summary':'Preview min(free-reserve,quota), respecting product minimum'}},
        '/api/earn/run':{'post':{'summary':'Run one journaled sweep; body: request_id. Paper default; live requires server flag and mode'}},
        '/api/earn/redeem':{'post':{'summary':'Redeem to spot; body: amount, request_id. Never assume immediate availability'}},
        '/api/order/preview':{'post':{'summary':'Validate seven-asset BUY LIMIT IOC; body: asset, amount_usdt, limit_price'}},
        '/api/order/test':{'post':{'summary':'Binance order/test, no matching-engine submission; body adds request_id'}},
        '/api/order':{'post':{'summary':'Journaled BUY LIMIT IOC; body: asset, amount_usdt, limit_price, request_id. No replay of historical report orders'}},
    },
}
for path,methods in OPENAPI['paths'].items():
    for method,operation in methods.items():
        operation['responses']={'200':{'description':'Success'},'400':{'description':'Invalid settings or parameters'},
                                '403':{'description':'Local session or origin rejected'},'502':{'description':'Upstream unavailable'}}
        if method=='post' and 'requestBody' not in operation:
            properties={}
            if path=='/api/settings':
                properties={'mode':{'enum':['paper','read_only','live']},'auto_earn':{'type':'boolean'},
                            'reserve_usdt':{'type':'string'},'product_id':{'type':'string'}}
            elif path.startswith('/api/order'):
                properties={'asset':{'enum':['GOOG','XAU','SOL','BTC','ETH','SPY','QQQ']},
                            'amount_usdt':{'type':'string'},'limit_price':{'type':'string'},'request_id':{'type':'string'}}
            elif path=='/api/earn/run':
                properties={'request_id':{'type':'string'}}
            elif path=='/api/earn/redeem':
                properties={'request_id':{'type':'string'},'amount':{'type':'string'}}
            operation['requestBody']={'required':True,'content':{'application/json':{'schema':{'type':'object','properties':properties}}}}


def build(source,output):
    source=Path(source)
    raw=source.read_bytes()
    report=json.loads(raw)
    if report.get('scope')!='portfolio':
        raise ValueError('需要组合回测报告')
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    root=Path(__file__).parent
    template=(root/'compact_template.html').read_text(encoding='utf-8')
    payload=json.dumps(report,ensure_ascii=False,allow_nan=False,separators=(',',':')).replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    html=template.replace('__COMPACT_CSS__',(root/'compact.css').read_text(encoding='utf-8'))
    html=html.replace('__COMPACT_JS__',(root/'compact.js').read_text(encoding='utf-8')).replace('__REPORT__',payload)
    (output/'index.html').write_text(html,encoding='utf-8')
    vendor=root/'vendor'
    meta=json.loads((vendor/'source.json').read_text(encoding='utf-8'))
    library=(vendor/'lightweight-charts.js').read_bytes()
    if hashlib.sha256(library).hexdigest()!=meta['js_sha256']:
        raise ValueError('图表库哈希不符')
    (output/'charts.js').write_bytes(library)
    (output/'source.json').write_text(json.dumps({'report':str(source.resolve()),'sha256':hashlib.sha256(raw).hexdigest(),
        'note':'Presentation only. Backtest numbers unchanged; no Earn interest added.'},ensure_ascii=False,indent=2),encoding='utf-8')
    return output


def handler_factory(root,gateway):
    root=Path(root)
    session=secrets.token_urlsafe(32)
    csrf=secrets.token_urlsafe(32)
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version='DCA-Local/1.0'
        def log_message(self,*args):
            pass

        def _send(self,payload,status=200,kind='application/json; charset=utf-8',cookie=False):
            data=payload if isinstance(payload,bytes) else json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            if cookie:
                self.send_header('Set-Cookie','dca_session='+session+'; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            self.wfile.write(data)

        def _origin_ok(self):
            expected='127.0.0.1:'+str(self.server.server_address[1])
            return (self.headers.get('Host')==expected
                and self.headers.get('Origin') in (None,'http://'+expected)
                and self.headers.get('Sec-Fetch-Site') in (None,'none','same-origin'))

        def _authorized(self):
            cookie=SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie',''))
                value=cookie['dca_session'].value if 'dca_session' in cookie else ''
            except Exception:
                return False
            return (self._origin_ok() and secrets.compare_digest(value,session)
                    and secrets.compare_digest(self.headers.get('X-DCA-Token',''),csrf))

        def do_GET(self):
            if not self._origin_ok():
                self._send({'error':'仅允许本机同源访问'},403)
                return
            if self.path=='/api/session':
                self._send({'csrf_token':csrf},cookie=True)
            elif self.path in ('/api/state','/api/earn/products','/api/operations'):
                if not self._authorized():
                    self._send({'error':'本机会话无效'},403)
                    return
                try:
                    self._send(gateway.status() if self.path=='/api/state' else {'operations':gateway.operations()} if self.path=='/api/operations' else {'products':gateway.products()})
                except (ValueError,GatewayError) as exc:
                    self._send({'error':str(exc)},400)
                except Exception:
                    self._send({'error':'接口暂不可用'},502)
            elif self.path=='/openapi.json':
                self._send(OPENAPI)
            elif self.path in ('/','/index.html','/charts.js'):
                name='charts.js' if self.path=='/charts.js' else 'index.html'
                self._send((root/name).read_bytes(),kind='application/javascript' if name.endswith('.js') else 'text/html; charset=utf-8')
            else:
                self._send({'error':'接口不存在'},404)

        def do_POST(self):
            if not self._authorized() or self.headers.get('Origin')!='http://127.0.0.1:'+str(self.server.server_address[1]):
                self._send({'error':'仅允许本机同源会话'},403)
                return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=16384 or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    self._send({'error':'需要小于16KB的JSON请求'},400)
                    return
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):
                    raise ValueError('需要JSON对象')
                if self.path=='/api/connect':
                    result=gateway.connect(body['api_key'],body['api_secret'])
                elif self.path=='/api/disconnect':
                    result=gateway.disconnect()
                elif self.path=='/api/settings':
                    result=gateway.configure(body)
                elif self.path=='/api/refresh':
                    result=gateway.refresh()
                elif self.path=='/api/earn/preview':
                    result=gateway.earn_preview()
                elif self.path=='/api/earn/run':
                    result=gateway.sweep(body['request_id'])
                elif self.path=='/api/earn/redeem':
                    result=gateway.redeem(body['amount'],body['request_id'])
                elif self.path in ('/api/order','/api/order/test','/api/order/preview'):
                    args=(body['asset'],body['amount_usdt'],body['limit_price'])
                    result=gateway.order_preview(*args) if self.path.endswith('/preview') else gateway.order(*args,body['request_id'],test=self.path.endswith('/test'))
                else:
                    self._send({'error':'接口不存在'},404)
                    return
                self._send(result)
            except (ValueError,KeyError,TypeError):
                # No submitted values or exception reprs, especially credentials, in errors.
                self._send({'error':'参数或账户状态无效，请检查连接、产品和执行模式'},400)
            except GatewayError as exc:
                self._send({'error':str(exc)},502)
            except Exception:
                self._send({'error':'接口暂不可用；资金操作不会自动重试'},502)
    return Handler


def serve(source,output,port=59755,allow_live=False):
    output=build(source,output)
    gateway=Gateway(output/'runtime'/'operations.sqlite',allow_live=allow_live)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',port),handler_factory(output,gateway))
    worker=threading.Thread(target=gateway.worker,daemon=True)
    worker.start()
    print(f'Local DCA: http://127.0.0.1:{server.server_address[1]} (paper default; live enabled={allow_live})',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gateway.stop.set()
        server.server_close()
        gateway.close()


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',default='reports/independent-assets-001/report.json')
    parser.add_argument('--output',default='reports/app-001')
    parser.add_argument('--port',type=int,default=59755)
    parser.add_argument('--enable-live',action='store_true',help='Explicitly allow live mode; default still paper until configured')
    args=parser.parse_args()
    serve(args.source,args.output,args.port,args.enable_live)
