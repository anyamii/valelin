"""Local data and disabled-integration responses for the copied relay."""
import json,uuid,time
from datetime import datetime,timezone,timedelta
from fastapi.responses import JSONResponse

def core():
    from backend import app
    return app

def add_memory(name,text):
    a=core();stamp=a.now_iso();bid='memory-'+uuid.uuid4().hex[:10]
    record=dict(id=bid,name=name,text=text,tags=['学习'],created=stamp,last_active=stamp,importance=3,pinned=False,resting=False,comments=[],comment_count=0,domain='study')
    with a.db() as c:c.execute('INSERT INTO demo_memories(id,payload) VALUES (?,?)',(bid,json.dumps(record,ensure_ascii=False)))
    return record

def save_keep(conn,mid):
    a=core()
    kid=a.keeps_store.save_message(conn,message_id=mid,saved_by='Vale',tags=[],upload_dir=a.UPLOAD_DIR,voice_dir=a.VOICE_DIR,sticker_dir=a.STICKER_DIR,keep_media_dir=a.KEEP_MEDIA_DIR,media_public_path=a.KEEP_MEDIA_PUBLIC_PATH)
    return a.keeps_store.item_payload(conn,kid)

def seed():
    a=core()
    with a.db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS demo_memories(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        if c.execute("SELECT 1 FROM letters WHERE id='welcome'").fetchone():return
    mids=[]  # Each new visit starts with no conversation history.
    for title,body in [('学习节奏','Lin 喜欢先看例子，再自己动手。'),('项目计划','Vale 和 Lin 把项目拆成每周能验证的小目标。'),('代码习惯','提交之前先运行相关检查。')]:add_memory(title,body)
    with a.db() as c:
        for mid in mids[:3]:save_keep(c,mid)
        stamp=a.now_iso();day=datetime.now().strftime('%Y-%m-%d')
        docs={'valelin_notes':{day:[{'id':'seed-review','t':'复习 Python 与项目回顾','author':'user','ts':int(time.time()*1000),'done':False}],(datetime.now()+timedelta(days=1)).strftime('%Y-%m-%d'):[{'id':'seed-code','t':'做一个小型代码练习','author':'user','ts':int(time.time()*1000),'done':False}],(datetime.now()+timedelta(days=3)).strftime('%Y-%m-%d'):[{'id':'seed-plan','t':'整理下周学习计划','author':'user','ts':int(time.time()*1000),'done':False}]},'valelin_anniv':[{'id':'new-year','date':'2026-01-01','name':'元旦','yearly':True},{'id':'spring','date':'2026-02-17','name':'春节','yearly':True},{'id':'valentine','date':'2026-02-14','name':'情人节','yearly':True},{'id':'midautumn','date':'2026-09-25','name':'中秋节','yearly':True}]}
        # Synthetic cycle fixture: relative dates, never copied from personal records.
        docs['valelin_period'] = {'cycle':28,'len':5,'manual':False,'logs':[
            {'start':(datetime.now()-timedelta(days=2)).strftime('%Y-%m-%d'),'end':None}
        ]}
        for key,data in docs.items():c.execute('INSERT OR REPLACE INTO daily_documents VALUES (?,?,?)',(key,json.dumps(data,ensure_ascii=False),stamp))
        c.execute("INSERT INTO letters(id,text,title,created_at,deliver_at,status,author,delivered_at,read_at) VALUES (?,?,?,?,?,'read','Vale',?,?)",('welcome','每次把目标缩小一点，就能更清楚地看到进展。','给正在学习的你',stamp,stamp,stamp,stamp))

def install(app):
    @app.middleware('http')
    async def local_only(request,call_next):
        a=core();path=request.url.path
        data=None; intercepted=True
        if path=='/config':
            data={'assistant_name':'Vale','user_name':'Lin','assistant_avatar':'/avatars/demo-v.svg','user_avatar':'/avatars/demo-l.svg','chat_background':'','chat_background_opacity':100}
            if request.method!='GET': data={'ok':True,'config':data}
        elif path=='/upload_avatar':
            return JSONResponse({'detail':'Avatar preferences are browser-local in this demo'},status_code=400)
        elif path in ('/app/voice_mode','/app/stt_config'):

            data={'ok':True,'segment_mode':True,'voice_mode':True,'sticker_mode':True,'tts':{},'stt_provider':'','stt_model':'','stt_lang':'auto','stt_keyterms':'Vale\nValelin'}
        elif path=='/demo/weather':data={'current_condition':[{'temp_C':'20','weatherDesc':[{'value':'晴'}]}]}
        elif path=='/demo/scenes':data=[scene for scene in a._sdk_provider.scenes[:-1] if scene.get('menu',True)]
        elif path=='/demo/jobs':
            with a.db() as c:ids={str(row[0]) for row in c.execute("SELECT id FROM messages WHERE direction='in'")}
            data=[job for job in a._sdk_provider.jobs if job['id'].removeprefix('replay-') in ids]
        elif path in ('/app/ob/buckets','/app/ob/search'):
            with a.db() as c:items=[json.loads(r[0]) for r in c.execute('SELECT payload FROM demo_memories')]
            query=request.query_params.get('q','');data=[i for i in items if query in i['text'] or query in i['name']]
        elif path.startswith('/app/ob/bucket/'):
            bid=path.split('/')[4]
            with a.db() as c:r=c.execute('SELECT payload FROM demo_memories WHERE id=?',(bid,)).fetchone()
            data=json.loads(r[0]) if r else {}
        elif path=='/app/sessions':data={'sessions':[],'active_session':''}
        elif path=='/app/brain':data={'target':'replay','model':'Replay · 离线回放'}
        elif path in ('/app/loop_config','/app/history-n','/app/sdk/context'):data={'provider':'replay'}
        elif path in ('/app/reroll','/app/voice'):return JSONResponse({'detail':'此功能仅在场景中回放'},status_code=400)
        elif path=='/app/sdk/config':data={'model':'replay','effort':'low','provider':'replay'}
        elif path=='/app/sdk/status':data={'provider':'replay','sessions':[],'enabled':True}
        elif path=='/app/sdk/models':data={'models':[]}
        elif path=='/app/sdk/usage':data={}
        elif path in ('/app/config','/app/status'):data={'connected':True,'provider':'replay'}
        elif path=='/previews/catalogue.json':data={'groups':[{'title':'预览','items':[{'title':'阅读','file':'reading.html'},{'title':'专注','file':'focus.html'},{'title':'主题','file':'theme.html'}]}]}
        elif path=='/app/notes':data={'notes':[{'id':'demo-md','title':'项目笔记.md','name':'项目笔记.md','format':'md','size':90},{'id':'demo-txt','title':'学习清单.txt','name':'学习清单.txt','format':'txt','size':70}]}
        elif path.startswith('/app/notes/'):
            data={'id':path.split('/')[-1],'title':'学习笔记','content':'先理解问题。\n写一个最小例子。\n验证后记录结果。','markdown':'先理解问题。\n写一个最小例子。\n验证后记录结果。','format':'md'}
        elif path=='/app/ledger':
            with a.db() as c:rows=[dict(r) for r in c.execute("SELECT * FROM command_cards WHERE status!='active' ORDER BY started_at DESC")]
            data={'entries':rows,'items':rows}
        elif path.startswith(('/app/lumen','/app/murmur','/app/persona','/app/ob/','/app/weather','/app/stations','/app/mcp','/app/push','/app/tts','/app/call','/app/stt','/api/lumen','/disabled-preview/','/api/mcp','/push/','/internal/')):data={}
        else:intercepted=False
        response=JSONResponse(data) if intercepted else await call_next(request)
        response.headers['Content-Security-Policy']="default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; media-src 'self' blob:; frame-src 'self'; object-src 'none'"
        return response

    from backend.demo_visits import VisitMiddleware
    app.add_middleware(VisitMiddleware)
