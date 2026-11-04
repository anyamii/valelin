"""Per-tab capability, private in-memory SQLite and ephemeral attachment storage."""
import contextvars,itertools,json,re,secrets,shutil,sqlite3,tempfile,threading,time
from pathlib import Path
from urllib.parse import parse_qs

visitor=contextvars.ContextVar('demo_visitor',default='')
housekeeping=contextvars.ContextVar('demo_housekeeping',default=False)
rooms={};queue_visitors={};lock=threading.RLock();serial=itertools.count(secrets.randbits(27)+1)
TTL=1800
RAM_DIRECTORY="/dev/shm" if Path("/dev/shm").is_dir() else None

def _core():
    from backend import app
    return app

def _new_room(key):
    a=_core();uri='file:visit-'+key+'?mode=memory&cache=shared'
    keeper=sqlite3.connect(uri,uri=True,check_same_thread=False)
    with sqlite3.connect(a.DB_PATH) as source:
        schema=[r[0] for r in source.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND type IN ('table','index') ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END")]
    keeper.executescript(';\n'.join(schema)+';')
    keeper.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('messages',?)",(next(serial)*(2**20),));keeper.commit()
    try:temporary=tempfile.TemporaryDirectory(prefix='valelin-visit-',dir=RAM_DIRECTORY)
    except OSError:temporary=tempfile.TemporaryDirectory(prefix='valelin-visit-')
    root=Path(temporary.name);(root/'uploads/keeps').mkdir(parents=True);(root/'voice').mkdir();(root/'static/media').mkdir(parents=True)
    for source in Path(__file__).resolve().parent.parent.joinpath('static/media').iterdir():
        dest=root/'static/media'/source.name
        if source.is_file():
            if source.name=='edited.md':shutil.copyfile(source,dest)
            else:
                try:dest.symlink_to(source)
                except (OSError,NotImplementedError):shutil.copyfile(source,dest)
    room={'uri':uri,'keeper':keeper,'files':temporary,'root':root,'used':time.monotonic(),'active':0}
    rooms[key]=room
    from backend.demo_local import seed
    seed()
    return room

def room_for(key=None):
    key=key or visitor.get()
    if not key:return None
    with lock:
        room=rooms.get(key)
        if room is None:room=_new_room(key)
        if not housekeeping.get():room['used']=time.monotonic()
        return room

def database_path(default):
    room=room_for();return room['uri'] if room else default

def connect(default):
    path=database_path(default);conn=sqlite3.connect(path,uri=path.startswith('file:'));conn.row_factory=sqlite3.Row;return conn

class VisitPath:
    def __init__(self,default,relative):self.default=Path(default);self.relative=relative
    def _path(self):
        room=room_for();return room['root']/self.relative if room else self.default
    def __fspath__(self):return str(self._path())
    def __str__(self):return str(self._path())
    def __truediv__(self,other):return self._path()/other
    def __getattr__(self,name):return getattr(self._path(),name)

def reap_rooms():
    with lock:
        for key,room in list(rooms.items()):
            if room['active'] or time.monotonic()-room['used']<=TTL:continue
            a=_core()
            runs={'replay-'+str(row[0]) for row in room['keeper'].execute("SELECT id FROM messages WHERE direction='in'")}
            for run in runs:
                future=a._sdk_provider.pending.pop(run,None)
                if future is not None and not future.done():future.cancel()
                for draft_key in list(a.stream_drafts):
                    if draft_key[0]==run:a.stream_drafts.pop(draft_key,None)
                a.voice_stream_locks.pop(run,None)
            a._sdk_provider.jobs[:]=[job for job in a._sdk_provider.jobs if job['id'] not in runs]
            room['keeper'].close();room['files'].cleanup();rooms.pop(key,None)

class VisitMiddleware:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        path=scope.get('path','');headers=dict(scope.get('headers',[]));query=parse_qs(scope.get('query_string',b'').decode())
        key=headers.get(b'x-demo-visit',b'').decode() or (query.get('demo_visit') or [''])[0]
        private=path.startswith(('/app/','/api/','/uploads/','/voice/','/demo/jobs')) or path=='/media/edited.md'
        if private and not re.fullmatch(r'[a-f0-9]{32}',key):
            from starlette.responses import JSONResponse
            return await JSONResponse({'detail':'Open the demo page to start a private visit'},status_code=400)(scope,receive,send)
        token=visitor.set(key if re.fullmatch(r'[a-f0-9]{32}',key) else '')
        room=None
        try:
            if visitor.get():
                reap_rooms()
                with lock:
                    room=room_for();room['active']+=1
                if path=='/media/edited.md':
                    from starlette.responses import FileResponse
                    return await FileResponse(room['root']/'static/media/edited.md')(scope,receive,send)
            async def private_send(message):
                if message['type']=='http.response.start' and private:
                    message['headers']=[(k,v) for k,v in message.get('headers',[]) if k.lower()!=b'cache-control']+[(b'cache-control',b'no-store')]
                await send(message)
            return await self.app(scope,receive,private_send)
        finally:
            if room:room['active']-=1;room['used']=time.monotonic()
            visitor.reset(token)
