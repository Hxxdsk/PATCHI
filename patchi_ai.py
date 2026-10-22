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
        self.apis=[]; self.model='gemini-2.5-flash'; self.saved=False
        self.cooldowns={}; self.denied={}; self.usage={}; self.models={}; self.checks={}
        self.api_errors={}; self.cursor=0; self.revision=0; self.limits={}
        self.scan={'id':'','status':'idle','done':0,'total':0,'apisDone':0,'apisTotal':0}
        self.scan_cancel=threading.Event()
        try: self.model=json.loads((self.directory/'ai_settings.json').read_text('utf-8'))['model']
        except (OSError,ValueError,KeyError,TypeError): pass
        try:
            data=json.loads(self.crypt((self.directory/'ai_pool.dpapi').read_bytes(),False))
            self.apis=[{'id':a['id'],'name':a['name'],'key':a['key']} for a in data['apis']]
            self.model=data['model']; self.saved=True
        except (OSError, ValueError, RuntimeError, KeyError, TypeError): pass

    def _remaining(self, identity, model, now=None):
        now=time.time() if now is None else now
        return max(0,self.cooldowns.get((identity,model),0)-now,self.denied.get((identity,model),0)-now)

    def _offered(self, a, model):
        catalog=self.models.get(a['id'])
        if catalog is not None and not any(m['id']==model and 'generateContent' in m['methods'] for m in catalog): return False
        check=self.checks.get((a['id'],model),{})
        if check.get('status') in ('unavailable','invalid','error','checking') or check.get('zeroQuota'): return False
        if self.api_errors.get(a['id'],{}).get('retryAt',0)>time.time(): return False
        return True

    def _available(self, identity, model):
        c=self.checks.get((identity,model),{})
        return c.get('status') in ('available','listed') and not c.get('zeroQuota') and not self._remaining(identity,model)

    def _details(self):
        merged={}
        for a in self.apis:
            for m in self.models.get(a['id'],[]):
                if not self._available(a['id'],m['id']): continue
                item=merged.setdefault(m['id'],dict(m,apiIds=[],checkedAt=0))
                item['apiIds'].append(a['id'])
                item['checkedAt']=max(item['checkedAt'],self.checks[(a['id'],m['id'])].get('checkedAt',0))
        return sorted(merged.values(),key=lambda m:m['id'])

    def public(self):
        with self.lock:
            now=time.time(); apis=[]
            for a in self.apis:
                identity=a['id']; usage=self.usage.get((identity,self.model),[]); recent=[u for u in usage if now-u[0]<60]
                available=[m['id'] for m in self.models.get(identity,[]) if self._available(identity,m['id'])]
                check=self.checks.get((identity,self.model),{})
                apis.append({'id':identity,'name':a['name'],'cooldown':int(self._remaining(identity,self.model,now)+.999),
                    'limits':self.limits.get((identity,self.model)),'requests':len(recent),'tokens':sum(u[1] for u in recent),
                    'sessionRequests':len(usage),'sessionTokens':sum(u[1] for u in usage),
                    'availableModels':available,'modelStatus':check.get('status','unchecked'),
                    'checkError':self.api_errors.get(identity,{}).get('message','')})
            details=self._details()
            return {'configured':bool(self.apis),'saved':self.saved,'model':self.model,'apis':apis,
                'models':[m['id'] for m in details],'modelDetails':details,'revision':self.revision,'scan':dict(self.scan)}

    def configure(self,data):
        if not isinstance(data,dict): raise ValueError('配置格式错误')
        with self.lock:
            if 'revision' in data and data['revision']!=self.revision: raise ValueError('设置已变化，请重新打开 AI 设置')
            apis=[dict(a) for a in self.apis]; model=str(data.get('model') or self.model)
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',model): raise ValueError('模型名称无效')
            if data.get('clear'): apis=[]
            if data.get('delete'): apis=[a for a in apis if a['id']!=data['delete']]
            key=str(data.get('key') or '').strip()
            if key:
                # API keys are opaque credentials. Accept dots/new AQ. format and visible
                # ASCII punctuation; reject whitespace/control characters before HTTP use.
                if not 12<=len(key)<=4096 or any(ord(c)<33 or ord(c)>126 for c in key): raise ValueError('API Key 格式无效，请删除空格或换行')
                if any(a['key']==key for a in apis): raise ValueError('此 API 已添加')
                apis.append({'id':uuid.uuid4().hex,'key':key,'name':str(data.get('name') or 'API '+str(len(apis)+1))[:60]})
                if len(apis)>20: raise ValueError('最多添加 20 个 API')
            remember=bool(data.get('remember',self.saved)); target=self.directory/'ai_pool.dpapi'
            encrypted=self.crypt(json.dumps({'apis':apis,'model':model}).encode(),True) if remember and apis else None
            self.directory.mkdir(parents=True,exist_ok=True)
            if encrypted is not None:
                tmp=target.with_suffix('.tmp'); tmp.write_bytes(encrypted); tmp.replace(target)
            else: target.unlink(missing_ok=True)
            public=self.directory/'ai_settings.json'; tmp=public.with_suffix('.tmp'); tmp.write_text(json.dumps({'model':model}),encoding='utf-8'); tmp.replace(public)
            if [a['id'] for a in apis]!=[a['id'] for a in self.apis]:
                self.scan_cancel.set(); self.scan={**self.scan,'status':'idle'}
            self.apis=apis; self.model=model; self.saved=bool(encrypted); self.revision+=1
            (self.directory/'gemini_api_key.dpapi').unlink(missing_ok=True)
            return self.public()

    def _catalog(self,a):
        entries=[]; token=''; seen=set(); pages=set()
        for _ in range(100):
            url=BASE+'models?pageSize=1000'+('&pageToken='+urllib.parse.quote(token,safe='') if token else '')
            req=urllib.request.Request(url,headers={'x-goog-api-key':a['key']})
            with urllib.request.urlopen(req,timeout=20) as r: data=json.loads(r.read(8*1024*1024))
            for m in data.get('models',[]):
                identity=str(m.get('name','')).removeprefix('models/')
                if identity in seen: continue
                seen.add(identity)
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',identity) or 'generateContent' not in m.get('supportedGenerationMethods',[]): continue
                entries.append({'id':identity,'name':m.get('displayName',identity),'methods':m.get('supportedGenerationMethods',[]),
                    'inputTokenLimit':m.get('inputTokenLimit'),'outputTokenLimit':m.get('outputTokenLimit')})
            token=data.get('nextPageToken','')
            if not token: break
            if token in pages: raise RuntimeError('模型列表分页异常')
            pages.add(token)
        else: raise RuntimeError('模型列表分页异常')
        return sorted(entries,key=lambda m:(m['id']!=self.model,m['id']))

    def _quota_error(self,exc,identity,model):
        try: error=json.loads(exc.read(65536)).get('error',{})
        except (ValueError,OSError): error={}
        try: retry=max(1,float(exc.headers.get('Retry-After',60)))
        except (ValueError,TypeError,AttributeError): retry=60
        limits={}
        for detail in error.get('details',[]):
            if 'retryDelay' in detail:
                try: retry=max(retry,float(str(detail['retryDelay']).rstrip('s')))
                except ValueError: pass
            for violation in detail.get('violations',[]):
                metric=str(violation.get('quotaId') or violation.get('quotaMetric','')).lower(); value=violation.get('quotaValue')
                if value is not None:
                    label='RPD' if 'perday' in metric else 'TPM' if 'token' in metric else 'RPM' if 'perminute' in metric else '配额'
                    limits[label]=str(value)
        text=json.dumps(error).lower()
        daily='perday' in text or 'per_day' in text
        if daily: retry=max(retry,next_daily_reset(time.time())-time.time())
        # A zero quota is not a short rate limit: this model/tier needs a quota change.
        zero=any(v=='0' for v in limits.values())
        if zero: retry=max(retry,300)
        with self.lock:
            if limits: self.limits.setdefault((identity,model),{}).update(limits)
            self.cooldowns[(identity,model)]=time.time()+min(retry,86400)
        result={'status':'quota','checkedAt':time.time(),'retryAt':time.time()+min(retry,86400),'zeroQuota':zero}
        if zero:
            with self.lock:self.checks[(identity,model)]=result
        return result

    def _probe(self,a,model):
        from patchi_ai_prompts import MODEL_CHECK_BODY
        body=dict(MODEL_CHECK_BODY)
        body['generationConfig']=dict(body['generationConfig'])
        if model.startswith('gemini-2.5-flash'): body['generationConfig']['thinkingConfig']={'thinkingBudget':0}
        req=urllib.request.Request(BASE+'models/'+model+':generateContent',data=json.dumps(body).encode(),
            headers={'Content-Type':'application/json','x-goog-api-key':a['key']})
        try:
            with urllib.request.urlopen(req,timeout=25) as r: answer=json.loads(r.read(1024*1024))
            candidates=answer.get('candidates',[])
            parts=[p for c in candidates for p in c.get('content',{}).get('parts',[]) if p.get('text') and not p.get('thought')]
            if not parts and not any(c.get('finishReason')=='MAX_TOKENS' for c in candidates): return {'status':'unavailable','checkedAt':time.time()}
            return {'status':'available','checkedAt':time.time()}
        except urllib.error.HTTPError as e:
            if e.code==429: return self._quota_error(e,a['id'],model)
            if e.code in (401,403,404):
                with self.lock:self.denied[(a['id'],model)]=time.time()+300
            return {'status':'unavailable' if e.code in (400,401,403,404) else 'error','checkedAt':time.time()}
        except (OSError,ValueError): return {'status':'error','checkedAt':time.time()}

    def start_check(self):
        with self.lock:
            if not self.apis: raise ValueError('请先添加 API')
            if self.scan['status']=='running': return self.public()
            self.scan_cancel=threading.Event(); cancel=self.scan_cancel
            scan={'id':uuid.uuid4().hex,'status':'running','done':0,'total':0,'apisDone':0,'apisTotal':len(self.apis)}
            self.scan=scan; apis=[dict(a) for a in self.apis]
        def one(a):
            try:
                entries=self._catalog(a)
                with self.lock:
                    if cancel.is_set(): return
                    self.models[a['id']]=entries; self.api_errors.pop(a['id'],None); scan['total']+=len(entries)
                    for entry in entries:
                        pair=(a['id'],entry['id']); old=self.checks.get(pair,{})
                        # Catalog visibility is not a paid inference or a quota guarantee.
                        if old.get('status')!='available' and not self._remaining(*pair):
                            self.checks[pair]={'status':'listed','checkedAt':time.time()}
                    scan['done']+=len(entries)
            except Exception as e:
                code=e.code if isinstance(e,urllib.error.HTTPError) else None
                with self.lock:
                    if cancel.is_set(): return
                    self.models[a['id']]=[]
                    self.api_errors[a['id']]={'message':'无法检测 API'+('（HTTP %s）'%code if code else '，请检查网络'),'retryAt':time.time()+300}
            finally:
                with self.lock: scan['apisDone']+=1
        def run():
            from concurrent.futures import ThreadPoolExecutor
            try:
                with ThreadPoolExecutor(max_workers=min(8,len(apis))) as executor: list(executor.map(one,apis))
            finally:
                with self.lock:
                    if not cancel.is_set(): scan['status']='done'; scan['completedAt']=time.time()
        threading.Thread(target=run,daemon=True).start()
        return self.public()

    def list_models(self):
        result=self.public()
        return {'models':result['models'],'details':result['modelDetails'],'selected':self.model,'scan':result['scan'],'checked':result['scan']['status']=='done'}

    def generate(self,body,model=None):
        model=MODEL.get() or model or self.model
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',model): raise ValueError('模型名称无效')
        encoded=json.dumps(body,ensure_ascii=False).encode(); attempted=set(); deadline=time.time()+125
        while True:
            cancel=CANCEL.get()
            if cancel and cancel.is_set(): raise RuntimeError('任务已取消')
            with self.lock:
                apis=[dict(a) for a in self.apis]
                if apis:
                    start=self.cursor%len(apis); apis=apis[start:]+apis[:start]
                ready=[]; waiting=[]
                for a in apis:
                    if not self._offered(a,model): continue
                    remaining=self._remaining(a['id'],model)
                    if remaining>0: waiting.append(remaining)
                    elif a['id'] not in attempted: ready.append(a)
            if not ready:
                if waiting and min(waiting)<=120 and cancel and time.time()<deadline:
                    cancel.wait(min(1,min(waiting))); attempted.clear(); continue
                raise RuntimeError('所选模型没有可用 API'+('，约 %d 秒后重试'%max(1,min(waiting)) if waiting else '，请检测全部 API 后选择可用模型'))
            a=ready[0]; identity=a['id']; attempted.add(identity)
            req=urllib.request.Request(BASE+'models/'+model+':generateContent',data=encoded,
                headers={'Content-Type':'application/json','x-goog-api-key':a['key']})
            try:
                with urllib.request.urlopen(req,timeout=150) as r:
                    raw=r.read(4*1024*1024+1)
                    if len(raw)>4*1024*1024: raise RuntimeError('AI 返回数据过大')
                    response=json.loads(raw)
                tokens=int(response.get('usageMetadata',{}).get('totalTokenCount',0))
                with self.lock:
                    self.usage.setdefault((identity,model),[]).append((time.time(),tokens)); self.cursor=(next((i for i,x in enumerate(self.apis) if x['id']==identity),0)+1)%max(1,len(self.apis))
                    self.checks[(identity,model)]={'status':'available','checkedAt':time.time()}
                return response
            except urllib.error.HTTPError as exc:
                if exc.code==429: self._quota_error(exc,identity,model); continue
                if exc.code in (401,403,404):
                    with self.lock:
                        self.denied[(identity,model)]=time.time()+300
                        self.checks[(identity,model)]={'status':'unavailable','checkedAt':time.time()}
                    continue
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
