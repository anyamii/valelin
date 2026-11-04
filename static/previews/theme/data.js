const USER = "Lin";
const AI = "Vale";
// 协议与线上一致，数据为录制回放。间隔 delay 为播放器元数据；payload 为 SSE JSON。
// agent_event 使用 runId/eventId/sequence/type；不复制线上业务逻辑。
const SCRIPT = [];
let sequence = 0;
const frame = (payload, delay = 500) => SCRIPT.push({delay, payload});
const agent = (type, fields = {}, delay = 500) => frame({type:'agent_event', event:{runId:'demo-run', eventId:`demo-${++sequence}`, sequence, type, ...fields}}, delay);
frame({type:'typing', active:true}, 350);
agent('block_start', {blockId:'thought', kind:'thinking'});
agent('thinking_delta', {blockId:'thought', text:'先回顾学习目标，把练习拆成可以验证的小步骤。'});
agent('block_start', {blockId:'read', kind:'tool', tool:{id:'read',name:'Read',source:'demo'}});
agent('tool_state', {toolUseId:'read',status:'running',input:{topic:'本周学习计划'}});
agent('tool_result', {toolUseId:'read',outputPreview:'已整理三个练习目标',error:false});
agent('thinking_delta', {blockId:'thought',text:'每天留出二十五分钟，先完成一个最小示例，再记录遇到的问题。'});
agent('block_stop', {blockId:'thought'});
agent('block_start', {blockId:'answer',kind:'text'});
for (const text of '我把计划拆成了三步：读懂示例、独立实现、写下复盘。先从一个小函数开始，今天只完成第一步。') agent('text_delta',{blockId:'answer',text},35);
frame({type:'file_msg',msg_id:1,seq:0,attachment:{name:'一周编程学习计划_练习与复盘清单.docx',size:24576,segment_seq:0},stream_id:'demo-run',ts:'2026-10-26T09:00:00',api_session:''});
frame({type:'sdk_permission_request',permission_id:'demo-approval',session_key:'demo-session',tool_name:'SavePlan',input:{topic:'本周学习计划'},title:'Save plan',display_name:'Save plan',description:'是否将这份练习计划加入演示日历？仅在本页模拟。'});
for (const text of '接下来，完成练习后记下一个问题，明天从这里继续。') agent('text_delta',{blockId:'answer',text},40);
agent('block_stop',{blockId:'answer'});
agent('agent_done');
frame({type:'typing',active:false},0);
const NAV = ['Chat','Dispatch','Memory','Architecture','Calendar','Letters','Keeps','Command'];
const SUMMARIES = ['Replay · 1 turn','Done · 108s','4 layers · 28 valid','5 nodes · overview','Today · 2 things','Sealed · 1 letter','Saved · 2 notes','Running · 25 min'];
