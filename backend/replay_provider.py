"""Local scripted implementation of the formal BrainProvider boundary."""
import asyncio,json,time,uuid
from pathlib import Path

class ReplayProvider:
    name = 'replay'
    def __init__(self,sink):
        self.sink=sink; self.pending={}; self.tasks=set(); self.jobs=[]; self.keep_items={}
        self.scenes=json.loads((Path(__file__).parent/'scenes.json').read_text(encoding="utf-8"))
    async def dispatch(self,message):
        task=asyncio.create_task(self.play(message)); self.tasks.add(task); task.add_done_callback(self.tasks.discard)
    async def close(self):
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
    async def decide_permission(self,*,permission_id,allow,**kw):
        future=self.pending.get(permission_id)
        if future is None or future.done(): return False
        future.set_result(allow); return True
    def status(self): return {'provider':'replay','enabled':True,'sessions':[]}
    async def play(self,msg):
        from backend import app as a
        text=msg['text']; ordered=sorted(self.scenes,key=lambda s: s['id']!='codex_dispatch')
        scene=next((s for s in ordered if s['prompt'] and s['prompt']==text),None) or next((s for s in ordered if any(k.lower() in text.lower() for k in s['keywords'])),self.scenes[-1])
        if (msg.get('meta') or {}).get('attachments'): scene=next(s for s in self.scenes if s['id']=='upload')
        run='replay-'+str(msg['id']); common={'stream_id':run,'source_id':str(msg['id']),'api_session':(msg.get('meta') or {}).get('api_session','')}
        seq=0
        async def emit(kind,**kw): await self.sink(dict(common,type=kind,**kw))
        async def event(event_type,**kw):
            nonlocal seq
            seq+=1
            await emit('agent_event',event=dict(runId=a.run_id_for_stream(run),eventId=f'{run}-{seq}',sequence=seq,turnId=str(msg['id']),type=event_type,timestamp=int(time.time()*1000),**kw))
        async def say(paragraph):
            chunk=paragraph+'\n\n'
            for start in range(0,len(chunk),12):
                await emit('text_delta',text=chunk[start:start+12])
                await asyncio.sleep(.08)
        try:
            await asyncio.sleep(.5)
            await event('run_state',status='running')
            await event('block_start',blockId='thought',kind='thinking')
            await event('thinking_delta',blockId='thought',text=scene['thinking'])
            await emit('thinking_delta',text=scene['thinking'])
            await asyncio.sleep(.8)
            await event('block_stop',blockId='thought')
            tool=scene['tool']; toolid='tool-'+str(msg['id'])
            allowed=True
            if scene['id']=='run_check':
                await say('我先检查项目里的 Python 文件，看看有没有语法问题。你允许后我再开始。')
                await asyncio.sleep(1.6)
            await event('block_start',blockId=toolid,kind='tool',tool={'id':toolid,'name':tool,'source':'mcp' if scene['id']=='codex_dispatch' else 'replay'})
            tool_input={'task':'整理项目说明','acceptance':['文档结构清楚','检查通过']} if scene['id']=='codex_dispatch' else {'scene':scene['id']}
            if scene['id']=='run_check':
                tool_input={'command':'python -m compileall backend'}
                await event('tool_state',toolUseId=toolid,status='preparing',input=tool_input)
                future=asyncio.get_running_loop().create_future(); self.pending[run]=future
                await event('permission_required',toolUseId=toolid)
                await emit('permission_request',permission_id=run,session_key='demo',tool_name='run_check',title='允许检查项目语法吗？',description='只检查 Python 语法，不修改文件。',input=tool_input)
                try: allowed=await future
                finally: self.pending.pop(run,None)
            job=None
            if allowed:
                await event('tool_state',toolUseId=toolid,status='running',input=tool_input)
                if scene['id']=='codex_dispatch':
                    job={'id':run,'name':'整理项目说明','state':'queued','submitted':int(time.time()*1000),'timeline':['12:51:52 投出','12:52:04 领走','12:54:26 完成'],'summary':['文档已整理。','检查通过。','退出码 0。']}; self.jobs.append(job)
                    await asyncio.sleep(1.2); job.update(state='running',start=int(time.time()*1000))
                await asyncio.sleep(1.5)
                result=await self.tool(scene,msg,common)
                if job: job.update(state='done',duration=108,code=0,completed=int(time.time()*1000))
                await event('tool_result',toolUseId=toolid,outputPreview=result,error=False)
                if job:
                    status_id=toolid+'-status'
                    await event('block_start',blockId=status_id,kind='tool',tool={'id':status_id,'name':'codex_dispatch_status','source':'mcp'})
                    await event('tool_state',toolUseId=status_id,status='running',input={'task_id':run})
                    await asyncio.sleep(.5)
                    await event('tool_result',toolUseId=status_id,outputPreview=json.dumps({'task_id':run,'status':'completed','exit_code':0,'summary':'文档已整理，检查通过。'},ensure_ascii=False),error=False)
                    await emit('act',text='投递 Codex 任务',glyph='memory',steps=[{'tool':'mcp__demo__codex_dispatch','input':tool_input,'cmd':'整理项目说明；验收：文档结构清楚、检查通过','result':json.dumps({'task_id':run,'status':'queued'},ensure_ascii=False)}])
                    await emit('act',text='查询 Codex 任务',glyph='memory',steps=[{'tool':'mcp__demo__codex_dispatch_status','input':{'task_id':run},'cmd':json.dumps({'task_id':run},ensure_ascii=False),'result':json.dumps({'task_id':run,'status':'completed','exit_code':0,'summary':'文档已整理，检查通过。'},ensure_ascii=False)}])
                else:
                    await emit('act',text='用了 '+tool,glyph='memory',steps=[{'tool':tool,'cmd':scene['prompt'],'result':result}])
            else:
                await event('tool_state',toolUseId=toolid,status='canceled',input=tool_input)
            await asyncio.sleep(.3)
            reply=scene['reply'] if allowed else '好，不运行。等你想检查时再告诉我。'
            for paragraph in reply.split('\n\n'):
                await say(paragraph)
                await asyncio.sleep(.5)
            if scene['id'] in ('file','upload'):
                filename='edited.md' if scene['id']=='upload' else 'demo-guide.md'
                draft=a.stream_drafts[(run,'reply')]
                attachment={'kind':'file','name':filename,'url':'/media/'+filename,'download_url':'/media/'+filename,'size':(Path(a.STATIC_DIR)/'media'/filename).stat().st_size,'mime':'text/markdown'}
                await a._push_file_msg(run,draft,attachment)
            await emit('done')
            item=self.keep_items.pop(run,None)
            if item:
                with a.db() as conn:
                    activity=a.keeps_activity.record(conn,action='save',actor='Vale',item=item,turn_id=int(msg['id']),after_seg=len(reply.split('\n\n')))
                await a.broadcast(a.app_subs,a.app_payload(activity))
            await event('agent_done')
            await a.broadcast(a.app_subs,{'type':'typing','active':False})
        except asyncio.CancelledError: raise
        except Exception as exc:
            import traceback; traceback.print_exc()
            await emit('text_delta',text='回放失败：'+type(exc).__name__); await emit('done')
            await a.broadcast(a.app_subs,{'type':'typing','active':False})
        finally:
            self.keep_items.pop(run,None)
    async def tool(self,scene,msg,common):
        from backend import app as a
        from backend.demo_local import add_memory,save_keep
        from backend.command_card import push_command,finish_command
        from backend.daily_calendar import _handle_add_note
        from datetime import datetime,timezone
        sid=scene['id']
        if sid=='run_check': return '语法检查通过，没有发现问题。'
        if sid=='command_push':
            with a.db() as conn:
                active=conn.execute("SELECT id FROM command_cards WHERE status='active'").fetchone()
                if active: finish_command(conn,active['id'],'canceled')
                row=push_command(conn,'专注整理学习笔记',25)
            with a.db() as conn:
                r=conn.execute('SELECT * FROM messages WHERE id=?',(row['message_id'],)).fetchone()
            m=dict(r);m['meta']=json.loads(m['meta']); await a.broadcast(a.app_subs,a.app_payload(m))
            return '25 分钟番茄钟已开始。'
        if sid=='letter':
            stamp=a.now_iso()
            with a.db() as conn:
                conn.execute("INSERT INTO letters(id,text,title,created_at,deliver_at,status,author) VALUES (?,?,?,?,?,'sealed','Vale')",(uuid.uuid4().hex,'今天又完成了一个小步骤。把问题写清楚，再慢慢验证，你已经在前进。','给正在学习的你',stamp,stamp))
            return '已按正式信结构写入本地 letters，等待原信箱组件投递。'
        if sid=='calendar':
            with a.db() as conn: _handle_add_note(conn,{'date':datetime.now().strftime('%Y-%m-%d'),'text':'演示项目回顾'},datetime.now(timezone.utc))
            return '已写入本地日历：演示项目回顾。'
        if sid=='write_scene': add_memory('演示日印象','Lin 把复杂问题拆成三个小步骤，逐个验证。'); return 'Mind 新增一张日印象卡。'
        if sid=='keep':
            with a.db() as conn:
                item=save_keep(conn,msg['id'])
            self.keep_items[common['stream_id']]=item
            return '已收藏，可从灰色状态条打开 WORDS。'
        if sid=='codex_dispatch': return '12:51:52 投出 → 12:52:04 领走 → 12:54:26 完成；耗时 108 秒；退出码 0。\n文档已整理。\n检查通过。\n退出码 0。'
        if sid=='upload':
            attachments=(msg.get('meta') or {}).get('attachments') or []
            content='这是需要整理的示例。'
            for att in attachments:
                name=Path(att.get('url','')).name
                path=a.UPLOAD_DIR/name
                if path.is_file() and path.suffix.lower() in ('.txt','.md'): content=path.read_text(encoding="utf-8", errors="replace")[:10000]
            (Path(a.STATIC_DIR)/'media'/'edited.md').write_text('# 整理结果\n\n'+content.strip()+'\n\n- 下一步：检查并保存。\n', encoding="utf-8")
            return '已在本地读取文件并生成整理副本。'
        return '本地回放完成；没有调用模型或外部工具。'
