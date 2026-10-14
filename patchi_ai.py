"""Shared Gemini configuration, quota-aware failover and idempotent local jobs."""
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import uuid
import tempfile

MODEL = contextvars.ContextVar('patchi_model', default=None)
CANCEL = contextvars.ContextVar('patchi_cancel', default=None)
BASE = 'https://generativelanguage.googleapis.com/v1beta/'

def next_daily_reset(now):
    """Gemini daily quotas reset at midnight Pacific, including Windows without tzdata."""
    from datetime import datetime as DT, timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        zone=ZoneInfo('America/Los_Angeles')
        local=DT.fromtimestamp(now,zone); tomorrow=local.date()+timedelta(days=1)
        return DT.combine(tomorrow,DT.min.time(),zone).timestamp()
    except (ImportError,KeyError):
        def offset(at):
            year=DT.fromtimestamp(at,timezone.utc).year
            march=DT(year,3,1,10,tzinfo=timezone.utc); start=march+timedelta(days=(6-march.weekday())%7+7)
            nov=DT(year,11,1,9,tzinfo=timezone.utc); end=nov+timedelta(days=(6-nov.weekday())%7)
            return -7 if start.timestamp()<=at<end.timestamp() else -8
        local=DT.fromtimestamp(now+offset(now)*3600,timezone.utc)
        midnight=DT.combine(local.date()+timedelta(days=1),DT.min.time(),timezone.utc).timestamp()
        candidate=midnight-offset(now)*3600
        return midnight-offset(candidate)*3600


class Pool:
    def __init__(self, directory, crypt):
        self.directory=Path(directory); self.crypt=crypt; self.lock=threading.RLock()
        self.apis=[]; self.model='gemini-2.5-flash'; self.active=''; self.saved=False
        self.cooldowns={}; self.denied={}; self.usage={}; self.models={}; self.cursor=0; self.revision=0; self.limits={}
        try: self.model=json.loads((self.directory/'ai_settings.json').read_text('utf-8'))['model']
        except (OSError,ValueError,KeyError,TypeError): pass
        try:
            raw=self.crypt((self.directory/'ai_pool.dpapi').read_bytes(),False)
            data=json.loads(raw); self.apis=data['apis']; self.model=data['model']; self.active=data['active']; self.saved=True
        except (OSError, ValueError, RuntimeError, KeyError, TypeError): pass

    def public(self):
        with self.lock:
            now=time.time(); apis=[]
            for a in self.apis:
                group=a.get('group') or a['id']; usage=self.usage.get((group,self.model),[])
                recent=[u for u in usage if now-u[0]<60]
                cooldown=max(0,round(max(self.cooldowns.get((group,self.model),0),self.denied.get((a['id'],self.model),0))-now))
                apis.append({k:v for k,v in a.items() if k!='key'} | {'cooldown':cooldown,
                    'limits':self.limits.get((group,self.model)), 'requests':len(recent),'tokens':sum(u[1] for u in recent),
                    'sessionRequests':len(usage),'sessionTokens':sum(u[1] for u in usage)})
            return {'configured':bool(self.apis),'saved':self.saved,'model':self.model,
                'active':self.active,'apis':apis,'models':[m['id'] for m in self.models.get(self.active,[])],
                'modelDetails':self.models.get(self.active,[]),'revision':self.revision}

    def configure(self,data):
        with self.lock:
            if 'revision' in data and data['revision']!=self.revision: raise ValueError('设置已变化，请重新打开 AI 设置')
            apis=[dict(a) for a in self.apis]
            model=str(data.get('model') or self.model)
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',model): raise ValueError('模型名称无效')
            if data.get('clear'): apis=[]
            if data.get('delete'): apis=[a for a in apis if a['id']!=data['delete']]
            key=str(data.get('key') or '').strip()
            if key:
                if not re.fullmatch(r'[A-Za-z0-9_-]{12,512}',key): raise ValueError('API Key 格式无效')
                if any(a['key']==key for a in apis): raise ValueError('此 API 已添加')
                apis.append({'id':uuid.uuid4().hex,'key':key,'name':str(data.get('name') or 'API '+str(len(apis)+1))[:60],
                    'group':str(data.get('group') or '').strip()[:80]})
                if len(apis)>20: raise ValueError('最多添加 20 个 API')
            active=str(data.get('active') or (apis[-1]['id'] if key else self.active))
            if not any(a['id']==active for a in apis): active=apis[0]['id'] if apis else ''
            remember=data.get('remember',self.saved)
            target=self.directory/'ai_pool.dpapi'
            if remember and apis:
                encrypted=self.crypt(json.dumps({'apis':apis,'model':model,'active':active}).encode(),True)
                self.directory.mkdir(parents=True,exist_ok=True)
                tmp=target.with_suffix('.tmp'); tmp.write_bytes(encrypted); tmp.replace(target)
                self.saved=True
            else:
                target.unlink(missing_ok=True); self.saved=False
            self.directory.mkdir(parents=True,exist_ok=True)
            public=self.directory/'ai_settings.json'; tmp=public.with_suffix('.tmp'); tmp.write_text(json.dumps({'model':model}),encoding='utf-8'); tmp.replace(public)
            self.apis=apis; self.active=active; self.model=model; self.revision+=1
            # Remove old single-key persistence after successful migration/configuration.
            (self.directory/'gemini_api_key.dpapi').unlink(missing_ok=True)
            return self.public()

    def list_models(self):
        with self.lock:
            a=next((dict(x) for x in self.apis if x['id']==self.active),None)
        if not a: raise ValueError('请先添加 API')
        entries=[]; token=''; seen=set()
        for _ in range(100):
            url=BASE+'models?pageSize=1000'+('&pageToken='+urllib.parse.quote(token,safe='') if token else '')
            req=urllib.request.Request(url,headers={'x-goog-api-key':a['key']})
            try:
                with urllib.request.urlopen(req,timeout=25) as r: data=json.loads(r.read(8*1024*1024))
            except urllib.error.HTTPError as e: raise RuntimeError('模型列表请求失败（HTTP %d）'%e.code) from None
            except (OSError,ValueError): raise RuntimeError('模型列表连接失败，请检查网络') from None
            for m in data.get('models',[]):
                identity=str(m.get('name','')).removeprefix('models/')
                if identity in seen: continue
                seen.add(identity)
                entries.append({'id':identity,'name':m.get('displayName',identity),
                    'methods':m.get('supportedGenerationMethods',[]),
                    'inputTokenLimit':m.get('inputTokenLimit'),'outputTokenLimit':m.get('outputTokenLimit')})
            token=data.get('nextPageToken','')
            if not token: break
        else: raise RuntimeError('模型列表分页异常，请重试')
        with self.lock: self.models[a['id']]=entries
        return {'models':[m['id'] for m in entries],'details':entries,'api':a['id'],'selected':self.model,'checked':True}

    def generate(self,body,model=None):
        model=MODEL.get() or model or self.model
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',model): raise ValueError('模型名称无效')
        encoded=json.dumps(body,ensure_ascii=False).encode(); attempted=set(); switched=False; deadline=time.time()+125
        # A retry is allowed only after an explicit upstream rejection, never an ambiguous timeout.
        while True:
            cancel=CANCEL.get()
            if cancel and cancel.is_set(): raise RuntimeError('任务已取消')
            with self.lock:
                apis=[dict(a) for a in self.apis]
                start=next((i for i,a in enumerate(apis) if a['id']==self.active),0)
                if apis: start=(start+self.cursor)%len(apis); apis=apis[start:]+apis[:start]
                ready=[]; waiting=[]
                for a in apis:
                    group=a.get('group') or a['id']; remaining=max(self.cooldowns.get((group,model),0),self.denied.get((a['id'],model),0))-time.time()
                    known=self.models.get(a['id'])
                    if known is not None and not any(m['id']==model and 'generateContent' in m['methods'] for m in known): continue
                    if remaining>0: waiting.append(remaining)
                    elif a['id'] not in attempted: ready.append(a)
            if not ready:
                if waiting and min(waiting)<=120 and cancel and time.time()<deadline:
                    cancel.wait(min(1,min(waiting))); attempted.clear(); continue
                raise RuntimeError('所有兼容 API 暂不可用'+('，约 %d 秒后重试'%max(1,min(waiting)) if waiting else '，请检查模型与权限'))
            a=ready[0]; group=a.get('group') or a['id']; attempted.add(a['id'])
            req=urllib.request.Request(BASE+'models/'+model+':generateContent',data=encoded,
                headers={'Content-Type':'application/json','x-goog-api-key':a['key']})
            try:
                with urllib.request.urlopen(req,timeout=150) as r:
                    raw=r.read(4*1024*1024+1)
                    if len(raw)>4*1024*1024: raise RuntimeError('AI 返回数据过大')
                    response=json.loads(raw)
                tokens=int(response.get('usageMetadata',{}).get('totalTokenCount',0))
                with self.lock:
                    self.usage.setdefault((group,model),[]).append((time.time(),tokens)); self.cursor+=1
                return response
            except urllib.error.HTTPError as exc:
                if exc.code==429:
                    try: error=json.loads(exc.read(65536)).get('error',{})
                    except (ValueError,OSError): error={}
                    retry=60.0
                    try: retry=max(1,float(exc.headers.get('Retry-After',60)))
                    except (ValueError,TypeError): pass
                    for d in error.get('details',[]):
                        if 'retryDelay' in d:
                            try: retry=max(retry,float(str(d['retryDelay']).rstrip('s')))
                            except ValueError: pass
                    limits={}
                    for detail in error.get('details',[]):
                        for violation in detail.get('violations',[]):
                            metric=str(violation.get('quotaId') or violation.get('quotaMetric','')).lower()
                            value=violation.get('quotaValue')
                            if value is not None:
                                label='RPD' if 'perday' in metric else 'TPM' if 'token' in metric else 'RPM' if 'perminute' in metric else '配额'
                                limits[label]=str(value)
                    if limits:
                        with self.lock:self.limits.setdefault((group,model),{}).update(limits)
                    text=json.dumps(error).lower()
                    if 'perday' in text or 'per_day' in text or 'requestsperday' in text:
                        # Unknown reset time: do not hammer a depleted daily quota.
                        retry=max(retry,next_daily_reset(time.time())-time.time())
                    with self.lock: self.cooldowns[(group,model)]=time.time()+min(retry,86400)
                    switched=True; continue
                if exc.code in (401,403,404):
                    with self.lock: self.denied[(a['id'],model)]=time.time()+300
                    switched=True; continue
                raise RuntimeError('AI 请求被拒绝（HTTP %d），当前图保留'%exc.code) from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                raise RuntimeError('AI 连接中断或返回无效；本次不自动重发，请检查后重试') from None

class Jobs:
    def __init__(self):
        self.lock=threading.RLock(); self.items={}; self.slots=threading.Semaphore(2); self.storage=tempfile.TemporaryDirectory(prefix='patchi_ai_jobs_'); self.session=uuid.uuid4().hex
    def reset(self):
        with self.lock:
            for j in self.items.values(): j['cancel'].set()
            self.items.clear();
            for path in Path(self.storage.name).glob('*.json'): path.unlink(missing_ok=True)
            self.session=uuid.uuid4().hex
            return self.session
    def submit(self,data,dispatch,model):
        identity=str(data.get('id','')); session=data.get('session')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',identity): raise ValueError('任务标识无效')
        if session!=self.session: raise ValueError('本次工作已结束，请刷新界面')
        digest=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.lock:
            if identity in self.items:
                if self.items[identity]['digest']!=digest: raise ValueError('任务标识与图片不一致')
                return self.get(identity)
            if len(self.items)>=300: raise ValueError('本次 AI 任务数量已达上限，请重新启动工坊')
            job={'id':identity,'digest':digest,'session':session,'status':'queued','model':model,'cancel':threading.Event()}
            self.items[identity]=job
        def run():
            mt=MODEL.set(model); ct=CANCEL.set(job['cancel'])
            try:
                self.slots.acquire();
                if job['cancel'].is_set(): raise RuntimeError('任务已取消')
                job['status']='running'; result=dispatch(data['feature'],data['payload'],model)
                if job['cancel'].is_set(): job['status']='canceled'
                else:
                    path=Path(self.storage.name)/(identity+'.json'); path.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8'); job['resultPath']=str(path); job['status']='done'
            except Exception as e:
                job['error']=str(e); job['status']='canceled' if job['cancel'].is_set() else 'failed'
            finally: self.slots.release(); MODEL.reset(mt); CANCEL.reset(ct)
        threading.Thread(target=run,daemon=True).start()
        return self.get(identity)
    def get(self,identity):
        with self.lock:
            job=self.items.get(identity)
            if not job: raise ValueError('任务不存在')
            result={k:v for k,v in job.items() if k not in ('cancel','digest','resultPath')}
            if job.get('resultPath'): result['result']=json.loads(Path(job['resultPath']).read_text('utf-8'))
            return result
