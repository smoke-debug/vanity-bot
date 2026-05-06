import os, json, asyncio, logging
from pathlib import Path
from datetime import datetime, timezone
import discord
from discord.ext import commands, tasks

TOKEN=os.getenv('DISCORD_TOKEN')
PREFIX=os.getenv('BOT_PREFIX','!')
WORD_DELAY_SECONDS=float(os.getenv('WORD_DELAY_SECONDS','5'))
BATCH_SIZE=int(os.getenv('BATCH_SIZE','10'))
BATCH_COOLDOWN_SECONDS=float(os.getenv('BATCH_COOLDOWN_SECONDS','20'))
LIST_COOLDOWN_SECONDS=float(os.getenv('LIST_COOLDOWN_SECONDS','90'))
MAX_RETRIES=int(os.getenv('MAX_RETRIES','3'))
BACKOFF_SECONDS=float(os.getenv('BACKOFF_SECONDS','45'))
MAX_CODES_PER_LIST=int(os.getenv('MAX_CODES_PER_LIST','5000'))
MIN_AUTO_MINUTES=int(os.getenv('MIN_AUTO_MINUTES','10'))

BASE_DIR=Path(__file__).resolve().parent
DATA_DIR=BASE_DIR/'data'
TXT_DIR=DATA_DIR/'txt_files'
CONFIG_FILE=DATA_DIR/'vanity_config.json'

logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
log=logging.getLogger('vanity_checker')

intents=discord.Intents.default(); intents.message_content=True; intents.guilds=True
bot=commands.Bot(command_prefix=PREFIX,intents=intents,help_command=None)
check_lock=asyncio.Lock()
check_state={'running':False,'stop_requested':False,'current':0,'total':0,'list':None}
config={'auto_enabled':False,'auto_minutes':60,'lists':{}}

def ensure_dirs():
    DATA_DIR.mkdir(parents=True,exist_ok=True); TXT_DIR.mkdir(parents=True,exist_ok=True)

def save_config():
    ensure_dirs(); CONFIG_FILE.write_text(json.dumps(config,indent=4),encoding='utf-8')

def load_config():
    global config
    ensure_dirs()
    if not CONFIG_FILE.exists(): save_config(); return
    try:
        loaded=json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        config['auto_enabled']=loaded.get('auto_enabled',False)
        config['auto_minutes']=loaded.get('auto_minutes',60)
        config['lists']=loaded.get('lists',{})
    except Exception:
        log.exception('Broken config, resetting')
        try: CONFIG_FILE.rename(DATA_DIR/f'vanity_config_broken_{int(datetime.now().timestamp())}.json')
        except Exception: pass
        config={'auto_enabled':False,'auto_minutes':60,'lists':{}}
        save_config()

def now_iso(): return datetime.now(timezone.utc).isoformat()
def utc_now(): return datetime.now(timezone.utc)

def format_time(iso):
    if not iso: return 'Unknown'
    try:
        ts=int(datetime.fromisoformat(iso).timestamp())
        return f'<t:{ts}:F> • <t:{ts}:R>'
    except Exception: return 'Unknown'

def format_duration(seconds):
    seconds=int(seconds); h=seconds//3600; m=(seconds%3600)//60; s=seconds%60
    return f'{h}h {m}m {s}s' if h else (f'{m}m {s}s' if m else f'{s}s')

def clean_code(x):
    s=str(x)
    for p in ['https://discord.gg/','http://discord.gg/','discord.gg/','https://discord.com/invite/','http://discord.com/invite/','discord.com/invite/']:
        s=s.replace(p,'')
    return s.strip().strip('/').lower()

def parse_words(words):
    seen=set(); out=[]
    for item in words.replace('\n',',').split(','):
        c=clean_code(item)
        if c and c not in seen:
            seen.add(c); out.append(c)
    return out

def list_ready(data):
    for k in ['valid_channel_id','invalid_channel_id','summary_channel_id','log_channel_id','words']:
        if k not in data or data[k] in (None,'',[]): return False,f'Missing `{k}`'
    return True,'Ready'

async def get_channel_safe(cid):
    if not cid: return None
    try: cid=int(cid)
    except Exception: return None
    return bot.get_channel(cid) or await bot.fetch_channel(cid)

async def safe_send(ch,content=None,embed=None,file=None):
    if not ch: return None
    for _ in range(2):
        try: return await ch.send(content=content,embed=embed,file=file)
        except discord.HTTPException as e:
            log.warning('send http error: %s',e); await asyncio.sleep(5)
        except Exception as e:
            log.warning('send failed: %s',e); return None
    return None

async def sleep_with_stop(sec):
    waited=0.0
    while waited<sec:
        if check_state['stop_requested']: return True
        step=min(.5,sec-waited); await asyncio.sleep(step); waited+=step
    return False

def chunks(words,limit=3900):
    res=[]; cur=''
    for w in words:
        add=w if not cur else ', '+w
        if len(cur)+len(add)>limit:
            if cur: res.append(cur)
            cur=w
        else: cur+=add
    if cur: res.append(cur)
    return res

async def fetch_invite_status(code):
    for attempt in range(1,MAX_RETRIES+1):
        if check_state['stop_requested']: return 'stopped',None
        try:
            inv=await bot.fetch_invite(code)
            return 'valid',inv
        except discord.NotFound: return 'invalid',None
        except discord.Forbidden as e: return 'error',f'Forbidden: {e}'
        except discord.HTTPException as e:
            log.warning('HTTP error %s attempt %s/%s: %s',code,attempt,MAX_RETRIES,e)
            if attempt<MAX_RETRIES:
                if await sleep_with_stop(BACKOFF_SECONDS*attempt): return 'stopped',None
                continue
            return 'error',f'HTTPException: {e}'
        except Exception as e:
            log.exception('unexpected error checking %s',code)
            if attempt<MAX_RETRIES:
                if await sleep_with_stop(BACKOFF_SECONDS*attempt): return 'stopped',None
                continue
            return 'error',f'{type(e).__name__}: {e}'
    return 'error','Unknown error'

def write_category_files(valid_words,invalid_words):
    ensure_dirs()
    (TXT_DIR/'valid_all.txt').write_text(', '.join(valid_words),encoding='utf-8')
    (TXT_DIR/'invalid_all.txt').write_text(', '.join(invalid_words),encoding='utf-8')
    for length in sorted({len(w) for w in valid_words}):
        ws=[w for w in valid_words if len(w)==length]
        (TXT_DIR/f'valid_{length}_letters.txt').write_text(', '.join(ws),encoding='utf-8')
    for length in sorted({len(w) for w in invalid_words}):
        ws=[w for w in invalid_words if len(w)==length]
        (TXT_DIR/f'invalid_{length}_letters.txt').write_text(', '.join(ws),encoding='utf-8')

async def send_words_output(ch,title,words,color,filename):
    if not words:
        await safe_send(ch,f'No {title.lower()} found.'); return
    for i,chunk in enumerate(chunks(words),1):
        e=discord.Embed(title=f'{title}{f" Part {i}" if len(chunks(words))>1 else ""}',description=f'```txt\n{chunk}\n```',color=color)
        await safe_send(ch,embed=e); await asyncio.sleep(1)
    path=TXT_DIR/filename; path.write_text(', '.join(words),encoding='utf-8')
    await safe_send(ch,content=f'Full `{title}` txt file:',file=discord.File(str(path),filename=path.name))

async def run_list_unlocked(name,data,ctx=None):
    ready,reason=list_ready(data)
    if not ready:
        if ctx: await ctx.send(f'`{name}` is not ready. {reason}. Use `{PREFIX}status {name}`.')
        return
    try:
        valid_ch=await get_channel_safe(data['valid_channel_id']); invalid_ch=await get_channel_safe(data['invalid_channel_id'])
        summary_ch=await get_channel_safe(data['summary_channel_id']); log_ch=await get_channel_safe(data['log_channel_id'])
    except Exception:
        if ctx: await ctx.send(f'`{name}` has a channel I cannot access. Check bot permissions.')
        return
    if not all([valid_ch,invalid_ch,summary_ch,log_ch]):
        if ctx: await ctx.send(f'`{name}` has a channel I cannot access. Check bot permissions.')
        return
    codes=[]; seen=set()
    for w in data.get('words',[]):
        c=clean_code(w)
        if c and c not in seen: seen.add(c); codes.append(c)
    if not codes: await safe_send(log_ch,f'`{name}` has no usable words.'); return
    codes=codes[:MAX_CODES_PER_LIST]
    check_state.update({'running':True,'stop_requested':False,'current':0,'total':len(codes),'list':name})
    valid_words=[]; invalid_words=[]; error_words=[]; start=utc_now(); started=start.isoformat()
    await safe_send(log_ch,f'🔍 Started `{name}` with `{len(codes)}` invite(s). Speed: `{WORD_DELAY_SECONDS}s` between words, `{BATCH_COOLDOWN_SECONDS}s` every `{BATCH_SIZE}` words.')
    try:
        for i,code in enumerate(codes,1):
            if check_state['stop_requested']: break
            check_state['current']=i
            result,payload=await fetch_invite_status(code)
            if result=='stopped': break
            if result=='valid':
                valid_words.append(code); await safe_send(valid_ch,f'discord.gg/{code}')
            elif result=='invalid':
                invalid_words.append(code); await safe_send(invalid_ch,f'discord.gg/{code}')
            else:
                error_words.append(code); await safe_send(log_ch,f'⚠️ Error checking `{code}`: `{payload}`')
            if i<len(codes) and i%BATCH_SIZE!=0:
                if await sleep_with_stop(WORD_DELAY_SECONDS): break
            if i%BATCH_SIZE==0 and i<len(codes):
                await safe_send(log_ch,f'Progress `{i}/{len(codes)}` | Valid: `{len(valid_words)}` | Invalid: `{len(invalid_words)}` | Errors: `{len(error_words)}`\nBatch cooldown: `{BATCH_COOLDOWN_SECONDS}s`...')
                if await sleep_with_stop(BATCH_COOLDOWN_SECONDS): break
    except Exception as e:
        log.exception('list failure')
        await safe_send(log_ch,f'⚠️ `{name}` had an unexpected error but stayed online: `{type(e).__name__}: {e}`')
    finally:
        end=utc_now(); elapsed=format_duration((end-start).total_seconds()); stopped=check_state['stop_requested']
        write_category_files(valid_words,invalid_words)
        e=discord.Embed(title=f'{"Stopped" if stopped else "Done"} Checking: {name}',description=f'Check completed in **{elapsed}**.',color=discord.Color.orange() if stopped else discord.Color.green())
        e.add_field(name='Processed',value=f'{check_state["current"]}/{len(codes)}',inline=True); e.add_field(name='Valid / On Server',value=str(len(valid_words)),inline=True); e.add_field(name='Invalid / Not On Server',value=str(len(invalid_words)),inline=True); e.add_field(name='Errors',value=str(len(error_words)),inline=True)
        e.add_field(name='Started',value=format_time(started),inline=False); e.add_field(name='Finished',value=format_time(end.isoformat()),inline=False); e.add_field(name='List Last Updated',value=format_time(data.get('updated_at')),inline=False); e.set_footer(text='Valid = on a server • Invalid = not on a server')
        await safe_send(summary_ch,embed=e)
        await send_words_output(summary_ch,'Valid / On Server Words',valid_words,discord.Color.blurple(),f'{name}_valid_on_server.txt')
        await send_words_output(summary_ch,'Invalid / Not On Server Words',invalid_words,discord.Color.dark_gray(),f'{name}_invalid_not_on_server.txt')
        if error_words: await send_words_output(summary_ch,'Error Words',error_words,discord.Color.red(),f'{name}_errors.txt')
        await safe_send(log_ch,f'✅ `{name}` check is done. Time taken: `{elapsed}` | Processed: `{check_state["current"]}/{len(codes)}` | Valid: `{len(valid_words)}` | Invalid: `{len(invalid_words)}` | Errors: `{len(error_words)}`')
        check_state.update({'running':False,'stop_requested':False,'current':0,'total':0,'list':None})

async def run_one(name,ctx=None):
    if check_lock.locked():
        if ctx: await ctx.send('List is already running, please wait.')
        return
    name=name.lower()
    if name not in config['lists']:
        if ctx: await ctx.send(f'No list named `{name}` exists.')
        return
    async with check_lock: await run_list_unlocked(name,config['lists'][name],ctx)

async def run_all(ctx=None):
    if check_lock.locked():
        if ctx: await ctx.send('List is already running, please wait.')
        return
    async with check_lock:
        if not config['lists']:
            if ctx: await ctx.send('No saved lists found.')
            return
        items=list(config['lists'].items())
        for idx,(name,data) in enumerate(items,1):
            if check_state['stop_requested']: break
            await run_list_unlocked(name,data,ctx)
            if idx<len(items):
                try: log_ch=await get_channel_safe(data.get('log_channel_id'))
                except Exception: log_ch=None
                await safe_send(log_ch,f'⏳ Waiting `{format_duration(LIST_COOLDOWN_SECONDS)}` before checking `{items[idx][0]}`...')
                if await sleep_with_stop(LIST_COOLDOWN_SECONDS): break

@tasks.loop(minutes=1)
async def auto_loop():
    if not config.get('auto_enabled',False): return
    if not hasattr(auto_loop,'counter'): auto_loop.counter=0
    auto_loop.counter+=1
    if auto_loop.counter<int(config.get('auto_minutes',60)): return
    auto_loop.counter=0
    if not check_lock.locked(): await run_all()

@bot.command(name='help')
async def help_cmd(ctx):
    e=discord.Embed(title='Vanity Bot Help',description=f'`#valid` = on server\n`#invalid` = not on server\n`#summary` = complete embeds + txt files\n`#log` = progress/errors\n\nSpeed: `{WORD_DELAY_SECONDS}s` per word, `{BATCH_COOLDOWN_SECONDS}s` every `{BATCH_SIZE}`, `{format_duration(LIST_COOLDOWN_SECONDS)}` between lists.',color=discord.Color.blurple())
    e.add_field(name='Setup',value=f'`{PREFIX}setup <list> #valid #invalid #summary #log <words>`\n`{PREFIX}setup 3letters #valid #invalid #summary #log abc, lol, pmo`',inline=False)
    e.add_field(name='Run',value=f'`{PREFIX}run <list>`\n`{PREFIX}runall`\n`{PREFIX}stop`',inline=False)
    e.add_field(name='Manage Words',value=f'`{PREFIX}words <list> <words>` = replace\n`{PREFIX}append <list> <words>` = add more\n`{PREFIX}remove_words <list> <words>` = remove',inline=False)
    e.add_field(name='Lists',value=f'`{PREFIX}lists`\n`{PREFIX}status <list>`\n`{PREFIX}remove_list <list>`',inline=False)
    e.add_field(name='Auto',value=f'`{PREFIX}autocheck <minutes>`\n`{PREFIX}autostop`\n`{PREFIX}autostatus`',inline=False)
    e.add_field(name='Txt Files',value=f'`{PREFIX}gettxt valid <length>`\n`{PREFIX}gettxt invalid <length>`\n`{PREFIX}gettxt valid`\n`{PREFIX}gettxt invalid`\n`{PREFIX}gettxt all`',inline=False)
    await ctx.send(embed=e)

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx,name:str,valid_channel:discord.TextChannel,invalid_channel:discord.TextChannel,summary_channel:discord.TextChannel,log_channel:discord.TextChannel,*,words:str):
    cleaned=parse_words(words)
    if not cleaned: await ctx.send('No usable words found.'); return
    if len(cleaned)>MAX_CODES_PER_LIST: await ctx.send(f'Too many words. Max is `{MAX_CODES_PER_LIST}`.'); return
    name=name.lower(); ts=now_iso(); created=config['lists'].get(name,{}).get('created_at',ts)
    config['lists'][name]={'valid_channel_id':valid_channel.id,'invalid_channel_id':invalid_channel.id,'summary_channel_id':summary_channel.id,'log_channel_id':log_channel.id,'words':cleaned,'created_at':created,'updated_at':ts}
    save_config(); await ctx.send(f'✅ Setup saved for `{name}` with `{len(cleaned)}` word(s).')

@bot.command(name='run')
@commands.has_permissions(administrator=True)
async def run_cmd(ctx,name:str): await run_one(name,ctx)
@bot.command()
@commands.has_permissions(administrator=True)
async def runall(ctx): await run_all(ctx)
@bot.command()
async def stop(ctx):
    if not check_state['running']: await ctx.send('No list is currently running.'); return
    check_state['stop_requested']=True; await ctx.send(f'Stop requested for `{check_state["list"]}`. Progress: `{check_state["current"]}/{check_state["total"]}`.')
@bot.command()
async def lists(ctx):
    if not config['lists']: await ctx.send('No saved lists yet.'); return
    e=discord.Embed(title='Saved Lists',color=discord.Color.blurple())
    for n,d in config['lists'].items():
        ready,reason=list_ready(d); e.add_field(name=n,value=f'Status: `{"Ready" if ready else reason}`\nWords: `{len(d.get("words",[]))}`\nValid: <#{d.get("valid_channel_id")}>\nInvalid: <#{d.get("invalid_channel_id")}>\nSummary: <#{d.get("summary_channel_id")}>\nLog: <#{d.get("log_channel_id")}>\nUpdated: {format_time(d.get("updated_at"))}',inline=False)
    await ctx.send(embed=e)
@bot.command()
async def status(ctx,name:str):
    name=name.lower()
    if name not in config['lists']: await ctx.send(f'No list named `{name}` exists.'); return
    d=config['lists'][name]; ready,reason=list_ready(d); preview=', '.join(d.get('words',[])[:30]);
    if len(d.get('words',[]))>30: preview+='...'
    e=discord.Embed(title=f'Status: {name}',color=discord.Color.green() if ready else discord.Color.orange())
    e.add_field(name='Ready',value='Yes' if ready else reason,inline=False); e.add_field(name='Words',value=str(len(d.get('words',[]))),inline=True); e.add_field(name='Valid',value=f'<#{d.get("valid_channel_id")}>',inline=True); e.add_field(name='Invalid',value=f'<#{d.get("invalid_channel_id")}>',inline=True); e.add_field(name='Summary',value=f'<#{d.get("summary_channel_id")}>',inline=True); e.add_field(name='Log',value=f'<#{d.get("log_channel_id")}>',inline=True); e.add_field(name='Updated',value=format_time(d.get('updated_at')),inline=False); e.add_field(name='Preview',value=preview or 'None',inline=False)
    await ctx.send(embed=e)
@bot.command()
@commands.has_permissions(administrator=True)
async def words(ctx,name:str,*,words:str):
    name=name.lower()
    if name not in config['lists']: await ctx.send(f'No list named `{name}` exists.'); return
    cleaned=parse_words(words)
    if not cleaned: await ctx.send('No usable words found.'); return
    config['lists'][name]['words']=cleaned; config['lists'][name]['updated_at']=now_iso(); save_config(); await ctx.send(f'✅ Replaced `{name}` with `{len(cleaned)}` word(s).')
@bot.command()
@commands.has_permissions(administrator=True)
async def append(ctx,name:str,*,words:str):
    name=name.lower()
    if name not in config['lists']: await ctx.send(f'No list named `{name}` exists.'); return
    cur=config['lists'][name].get('words',[]); seen=set(cur); added=[]
    for w in parse_words(words):
        if w not in seen: cur.append(w); seen.add(w); added.append(w)
    if len(cur)>MAX_CODES_PER_LIST: await ctx.send(f'This would exceed max `{MAX_CODES_PER_LIST}`.'); return
    config['lists'][name]['words']=cur; config['lists'][name]['updated_at']=now_iso(); save_config(); await ctx.send(f'✅ Added `{len(added)}` word(s). Total: `{len(cur)}`.')
@bot.command()
@commands.has_permissions(administrator=True)
async def remove_words(ctx,name:str,*,words:str):
    name=name.lower()
    if name not in config['lists']: await ctx.send(f'No list named `{name}` exists.'); return
    rem=set(parse_words(words)); old=config['lists'][name].get('words',[]); new=[w for w in old if w not in rem]
    config['lists'][name]['words']=new; config['lists'][name]['updated_at']=now_iso(); save_config(); await ctx.send(f'✅ Removed `{len(old)-len(new)}` word(s). Total: `{len(new)}`.')
@bot.command()
@commands.has_permissions(administrator=True)
async def remove_list(ctx,name:str):
    name=name.lower()
    if name not in config['lists']: await ctx.send(f'No list named `{name}` exists.'); return
    del config['lists'][name]; save_config(); await ctx.send(f'✅ Removed `{name}`.')
@bot.command()
async def gettxt(ctx,category:str,length:int=None):
    category=category.lower(); ensure_dirs()
    if category=='all': files=sorted(TXT_DIR.glob('*.txt'))
    elif category in ('valid','invalid') and length is None: files=sorted(TXT_DIR.glob(f'{category}_*_letters.txt'))
    elif category in ('valid','invalid'): files=[TXT_DIR/f'{category}_{length}_letters.txt']
    else: await ctx.send('Use `valid`, `invalid`, or `all`. Example: `!gettxt invalid 3`'); return
    files=[p for p in files if p.exists()]
    if not files: await ctx.send('No matching txt files found yet. Run a check first.'); return
    for p in files:
        await ctx.send(file=discord.File(str(p),filename=p.name)); await asyncio.sleep(1)
@bot.command()
@commands.has_permissions(administrator=True)
async def autocheck(ctx,minutes:int):
    if minutes<MIN_AUTO_MINUTES: await ctx.send(f'Use at least `{MIN_AUTO_MINUTES}` minutes.'); return
    config['auto_enabled']=True; config['auto_minutes']=minutes; save_config(); auto_loop.counter=0; await ctx.send(f'✅ Auto checks enabled every `{minutes}` minute(s).')
@bot.command()
@commands.has_permissions(administrator=True)
async def autostop(ctx): config.update(auto_enabled=False); save_config(); await ctx.send('✅ Auto checks disabled.')
@bot.command()
async def autostatus(ctx): await ctx.send(f'Auto checks: `{"Enabled" if config["auto_enabled"] else "Disabled"}`\nInterval: `{config["auto_minutes"]}` minute(s)\nSaved lists: `{len(config["lists"])}`\nRunning: `{"Yes" if check_state["running"] else "No"}`\nLocked: `{"Yes" if check_lock.locked() else "No"}`\nWord delay: `{WORD_DELAY_SECONDS}s`\nBatch cooldown: `{BATCH_COOLDOWN_SECONDS}s every {BATCH_SIZE}`\nList cooldown: `{format_duration(LIST_COOLDOWN_SECONDS)}`')

@bot.event
async def on_command_error(ctx,error):
    if isinstance(error,commands.CommandNotFound): return
    if isinstance(error,commands.MissingPermissions): await ctx.send('You need administrator permission to use that.'); return
    if isinstance(error,commands.MissingRequiredArgument): await ctx.send(f'Missing something. Use `{PREFIX}help` for syntax.'); return
    if isinstance(error,commands.BadArgument): await ctx.send(f'Bad format. Mention channels like `#valid`. Use `{PREFIX}help`.'); return
    log.exception('Command error: %s',error); await ctx.send('An error happened, but the bot is still running. Check logs or use `!help`.')
@bot.event
async def on_ready():
    ensure_dirs(); load_config()
    if not auto_loop.is_running(): auto_loop.start()
    log.info('Logged in as %s | saved lists: %s',bot.user,len(config['lists']))
if not TOKEN: raise RuntimeError('DISCORD_TOKEN is missing. Add it to Railway variables.')
bot.run(TOKEN)
