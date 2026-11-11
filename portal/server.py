import argparse,csv,io,json,threading,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
ROOT=Path(__file__).resolve().parent
class Portal(ThreadingHTTPServer):
    def __init__(self,address):super().__init__(address,Handler);self.sessions={};self.exports={};self.lock=threading.Lock()
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def respond(self,status,body,kind='application/json',headers=None):
        data=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)))
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(data)
    def session(self):
        cookies=SimpleCookie(self.headers.get('Cookie',''));key=cookies.get('portal')
        return self.server.sessions.get(key.value) if key else None
    def do_GET(self):
        u=urlsplit(self.path);q=parse_qs(u.query)
        if u.path=='/':self.respond(200,(ROOT/'index.html').read_bytes(),'text/html; charset=utf-8');return
        if u.path=='/options':
            time.sleep(0.12 if q.get('org',['a'])[0]=='a' else 0.02)
            self.respond(200,{'org':q.get('org',['a'])[0],'periods':['2026-10','2026-11'],'metrics':['net','gross']});return
        if u.path.startswith('/export/'):
            item=self.server.exports.get(u.path.rsplit('/',1)[-1]);session=self.session()
            if not item or not session or item['session']!=session['id']:self.respond(404,{'error':'导出不存在'});return
            if time.monotonic()<item['ready_at']:self.respond(202,{'state':'pending'});return
            row={k:item[k] for k in ('export_id','org','period','metric')};row.update({'record_id':'r1','amount':'125.50'})
            out=io.StringIO();w=csv.DictWriter(out,fieldnames=list(row));w.writeheader();w.writerow(row)
            self.respond(200,out.getvalue().encode('utf-8-sig'),'text/csv',{'Content-Disposition':'attachment; filename='+item['export_id']+'.csv'});return
        self.respond(404,{'error':'路径不存在'})
    def do_POST(self):
        u=urlsplit(self.path);body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
        if u.path=='/login':
            key=uuid.uuid4().hex;self.server.sessions[key]={'id':key,'org':'a','period':'2026-11','metric':'net'}
            self.respond(200,{'ok':True},headers={'Set-Cookie':'portal='+key+'; HttpOnly; SameSite=Lax; Path=/'});return
        session=self.session()
        if session is None:self.respond(401,{'error':'登录已失效'});return
        if u.path=='/select':
            if body.get('org') not in ['a','b'] or body.get('period') not in ['2026-10','2026-11'] or body.get('metric') not in ['net','gross']:self.respond(422,{'error':'筛选项已撤回'});return
            session.update(body);self.respond(200,session);return
        if u.path=='/expire':
            self.server.sessions.pop(session['id']);self.respond(200,{'ok':True});return
        if u.path=='/export':
            key=uuid.uuid4().hex
            item={**session,'session':session['id'],'export_id':key,'ready_at':time.monotonic()+float(body.get('delay',0.12))}
            self.server.exports[key]=item;self.respond(202,{k:item[k] for k in ['export_id','org','period','metric']});return
        self.respond(404,{'error':'路径不存在'})
def start():
    server=Portal(('127.0.0.1',0));threading.Thread(target=server.serve_forever,daemon=True).start();return server,'http://127.0.0.1:'+str(server.server_port)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8787);a=p.parse_args()
    with Portal(('127.0.0.1',a.port)) as s:print('合作方门户',s.server_address,flush=True);s.serve_forever()
