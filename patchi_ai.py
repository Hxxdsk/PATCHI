"""Shared Gemini configuration, quota-aware failover and idempotent local jobs."""
import contextvars
import hashlib
import io
import socket
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
FEATURES={'product','size','detail-caption','main-image','detail-current','garment-contour'}
REPORT = contextvars.ContextVar('patchi_report', default=None)
REQUEST_SECONDS = 75
REQUEST_SLOTS = threading.BoundedSemaphore(4)

class AIRequestError(RuntimeError):
    def __init__(self,code,message):super().__init__(message);self.code=code

def report(kind,message,**fields):
    callback=REPORT.get()
    if callback:callback({'kind':kind,'message':message,**fields})

def check_cancel():
    cancel=CANCEL.get()
    if cancel and cancel.is_set():raise AIRequestError('canceled','任务已停止')

def interrupt_response(response):
    try:
        sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
        if sock:sock.shutdown(socket.SHUT_RDWR)
    except (OSError,ValueError,AttributeError):pass

def request_bytes(req,timeout=REQUEST_SECONDS,limit=4*1024*1024):
    """Cancelable local wait around bounded urllib workers. Never resend ambiguous requests."""
    started=time.monotonic()
    while not REQUEST_SLOTS.acquire(timeout=.1):
        check_cancel()
        if time.monotonic()-started>15:raise AIRequestError('transport_busy','已有请求仍在结束连接，请稍后重试；当前图未改动')
        report('waiting','等待可用 API 连接槽位')
    done=threading.Event();state={};context=contextvars.copy_context()
    def work():
        try:
            check_cancel()
            with urllib.request.urlopen(req,timeout=timeout) as response:
                state['response']=response;state['raw']=response.read(limit+1)
        except urllib.error.HTTPError as exc:
            state['response']=exc
            try:raw=exc.read(65536)
            except (OSError,ValueError):raw=b''
            finally:exc.close()
            state['error']=urllib.error.HTTPError(req.full_url,exc.code,exc.reason,exc.headers,io.BytesIO(raw))
        except Exception as exc:state['error']=exc
        finally:done.set();REQUEST_SLOTS.release()
    threading.Thread(target=lambda:context.run(work),daemon=True).start()
    deadline=time.monotonic()+timeout
    while not done.wait(.1):
        try:check_cancel()
        except AIRequestError:
            interrupt_response(state.get('response'));raise
        if time.monotonic()>deadline:
            interrupt_response(state.get('response'));raise AIRequestError('timeout','API 响应超时，已停止本地等待；为避免重复计费，本次不自动重发')
    check_cancel()
    if 'error' in state:raise state['error']
    raw=state['raw']
    if len(raw)>limit:raise AIRequestError('response_size','AI 返回数据过大，当前图保留')
    return raw

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
        self.feature_models={}; self.apis=[]; self.model='gemini-2.5-flash'; self.saved=False
        self.cooldowns={}; self.denied={}; self.usage={}; self.models={}; self.checks={}
        self.last_issues={}; self.api_errors={}; self.cursor=0; self.revision=0; self.limits={}
        self.scan={'id':'','status':'idle','done':0,'total':0,'apisDone':0,'apisTotal':0}
        self.scan_cancel=threading.Event()
        try:
            settings=json.loads((self.directory/'ai_settings.json').read_text('utf-8')); self.model=settings['model']
            self.feature_models={k:v for k,v in settings.get('featureModels',{}).items() if k in FEATURES and isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',v)}
        except (OSError,ValueError,KeyError,TypeError): pass
        try:
            data=json.loads(self.crypt((self.directory/'ai_pool.dpapi').read_bytes(),False))
            self.apis=[{'id':a['id'],'name':a['name'],'key':a['key']} for a in data['apis']]
            self.model=data['model']; self.saved=True
        except (OSError, ValueError, RuntimeError, KeyError, TypeError): pass

    def model_for(self,feature):
        with self.lock:return self.feature_models.get(feature,self.model)

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
                identity=a['id']; usage=self.usage.get((identity,self.model),[]); session_usage=[u for (api_id,_),records in self.usage.items() if api_id==identity for u in records]; recent=[u for u in usage if now-u[0]<60]
                available=[m['id'] for m in self.models.get(identity,[]) if self._available(identity,m['id'])]
                check=self.checks.get((identity,self.model),{})
                apis.append({'id':identity,'name':a['name'],'cooldown':int(self._remaining(identity,self.model,now)+.999),
                    'limits':self.limits.get((identity,self.model)),'requests':len(recent),'tokens':sum(u[1] for u in recent),
                    'sessionRequests':len(session_usage),'sessionTokens':sum(u[1] for u in session_usage),
                    'availableModels':available,'modelStatus':check.get('status','unchecked'),
                    'lastIssue':self.last_issues.get(identity),'checkError':self.api_errors.get(identity,{}).get('message','')})
            details=self._details()
            return {'configured':bool(self.apis),'saved':self.saved,'model':self.model,'apis':apis,
                'featureModels':dict(self.feature_models),'models':[m['id'] for m in details],'modelDetails':details,'revision':self.revision,'scan':dict(self.scan)}

    def configure(self,data):
        if not isinstance(data,dict): raise ValueError('配置格式错误')
        with self.lock:
            if 'revision' in data and data['revision']!=self.revision: raise ValueError('设置已变化，请重新打开 AI 设置')
            apis=[dict(a) for a in self.apis]; model=str(data.get('model') or self.model)
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',model): raise ValueError('模型名称无效')
            feature_models=dict(self.feature_models)
            supplied=data.get('featureModels',{})
            if not isinstance(supplied,dict):raise ValueError('功能模型设置无效')
            for feature,value in supplied.items():
                if feature not in FEATURES or not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',value):raise ValueError('功能模型名称无效')
                feature_models[feature]=value
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
            public=self.directory/'ai_settings.json'; tmp=public.with_suffix('.tmp'); tmp.write_text(json.dumps({'model':model,'featureModels':feature_models}),encoding='utf-8'); tmp.replace(public)
            if [a['id'] for a in apis]!=[a['id'] for a in self.apis]:
                self.scan_cancel.set(); self.scan={**self.scan,'status':'idle'}
            self.apis=apis; self.model=model; self.feature_models=feature_models; self.saved=bool(encrypted); self.revision+=1
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
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',model):raise ValueError('模型名称无效')
        encoded=json.dumps(body,ensure_ascii=False).encode();attempted=set();deadline=time.monotonic()+90
        attempt=0;waited=0;compatible=False;last_api=None
        while True:
            check_cancel()
            if time.monotonic()>deadline:raise AIRequestError('timeout','本阶段等待超时，当前图未改动；可换模型或稍后重试')
            with self.lock:
                apis=[dict(a) for a in self.apis]
                if apis:
                    start=self.cursor%len(apis);apis=apis[start:]+apis[:start]
                ready=[];waiting=[]
                for a in apis:
                    if not self._offered(a,model):continue
                    remaining=self._remaining(a['id'],model)
                    if remaining>0:waiting.append(remaining)
                    elif a['id'] not in attempted:ready.append(a)
            if not ready:
                if waiting and min(waiting)<=15-waited and CANCEL.get():
                    seconds=min(waiting);until=time.time()+seconds
                    report('cooldown','所有兼容 API 暂时限流，短暂冷却后继续',waitUntil=until,model=model)
                    while time.time()<until:
                        check_cancel();CANCEL.get().wait(min(.2,until-time.time()))
                    waited+=seconds;attempted.clear();continue
                report('unavailable','没有可用 API；停止本批次，保留原贴标',model=model)
                raise AIRequestError('no_api','所选模型没有可用 API'+('，约 %d 秒后再试'%max(1,min(waiting)) if waiting else '；请查看 API 实时记录或选择其他视觉模型'))
            a=ready[0];identity=a['id'];attempt+=1
            if attempt>len(apis)+2:raise AIRequestError('no_api','API 轮询已达到本阶段上限，请稍后重试')
            attempted.add(identity)
            if last_api is not None:report('switch',('重试 ' if last_api==identity else '轮询切换到 ')+a['name']+'，保持模型和图片不变',api=a['name'],apiId=identity,model=model,attempt=attempt)
            last_api=identity
            report('request','已提交 '+a['name']+'，等待模型响应',api=a['name'],apiId=identity,model=model,attempt=attempt,requestStarted=time.time(),waitUntil=None)
            req=urllib.request.Request(BASE+'models/'+model+':generateContent',data=encoded,headers={'Content-Type':'application/json','x-goog-api-key':a['key']})
            try:
                raw=request_bytes(req);response=json.loads(raw)
                tokens=int(response.get('usageMetadata',{}).get('totalTokenCount',0))
                with self.lock:
                    self.usage.setdefault((identity,model),[]).append((time.time(),tokens));self.cursor=(next((i for i,x in enumerate(self.apis) if x['id']==identity),0)+1)%max(1,len(self.apis))
                    self.checks[(identity,model)]={'status':'available','checkedAt':time.time()};self.last_issues.pop(identity,None)
                report('response','已收到 '+a['name']+' 响应，正在检查结果',api=a['name'],apiId=identity,model=model,requestStarted=None)
                return response
            except urllib.error.HTTPError as exc:
                if exc.code==429:
                    result=self._quota_error(exc,identity,model);message='额度为零/耗尽' if result.get('zeroQuota') else '达到速率或当日额度限制'
                    message=a['name']+'：'+message+'，正在检查其他 API'
                elif exc.code in (401,403,404):
                    reason={401:'API 认证失效',403:'此模型权限或地区受限',404:'此 API 不提供所选模型'}[exc.code];message=a['name']+'：'+reason+'（HTTP '+str(exc.code)+'）'
                    with self.lock:
                        self.denied[(identity,model)]=time.time()+300;self.checks[(identity,model)]={'status':'unavailable','checkedAt':time.time()}
                        if exc.code==401:self.api_errors[identity]={'message':'API 认证失效，请更新密钥','retryAt':time.time()+300}
                elif exc.code==400:
                    try:error=json.loads(exc.read(65536)).get('error',{});detail=str(error.get('message','')).lower()
                    except (ValueError,OSError):detail=''
                    if not compatible and 'responseSchema' in body.get('generationConfig',{}) and any(word in detail for word in ('schema','structured','generationconfig','responsemime','json mode')):
                        body=json.loads(json.dumps(body));schema=body['generationConfig'].pop('responseSchema',{})
                        body['generationConfig'].pop('responseMimeType',None);body['contents'][-1]['parts'].append({'text':'Return JSON only, following this output contract: '+json.dumps(schema)})
                        encoded=json.dumps(body,ensure_ascii=False).encode();compatible=True;attempted.discard(identity)
                        report('compatibility',a['name']+' 拒绝结构化约束，改用兼容 JSON 提示重试',api=a['name'],apiId=identity,model=model);continue
                    message='所选模型拒绝图片或请求格式（HTTP 400），请更换支持视觉与 JSON 输出的模型'
                    with self.lock:self.last_issues[identity]={'model':model,'code':400,'message':message,'at':time.time()}
                    report('invalid_request',message,api=a['name'],apiId=identity,model=model)
                    raise AIRequestError('unsupported_model',message) from None
                elif exc.code in (500,502,503,504):
                    message=a['name']+'：服务端暂时失败（HTTP '+str(exc.code)+'），检查其他 API'
                    with self.lock:self.cooldowns[(identity,model)]=time.time()+10
                else:
                    message='API 拒绝请求（HTTP %d），当前图保留'%exc.code
                    report('api_error',message,api=a['name'],apiId=identity,model=model)
                    raise AIRequestError('api_error',message) from None
                with self.lock:self.last_issues[identity]={'model':model,'code':exc.code,'message':message,'at':time.time()}
                report('api_error',message,api=a['name'],apiId=identity,model=model,code=exc.code)
            except AIRequestError:raise
            except (urllib.error.URLError,TimeoutError,OSError,ValueError):
                message='API 连接超时、中断或返回无效；本次不自动重发，请检查后重试'
                report('network_error',message,api=a['name'],apiId=identity,model=model)
                raise AIRequestError('network_error',message) from None

class Jobs:
    def __init__(self):
        self.lock=threading.RLock();self.items={};self.tombstones={};self.slots=threading.Semaphore(2);self.storage=tempfile.TemporaryDirectory(prefix='patchi_ai_jobs_');self.session=uuid.uuid4().hex
    def reset(self):
        with self.lock:
            for j in self.items.values():j['cancel'].set()
            self.items.clear();self.tombstones.clear()
            for path in Path(self.storage.name).glob('*.json'):path.unlink(missing_ok=True)
            self.session=uuid.uuid4().hex;return self.session
    def event(self,job,data):
        with self.lock:
            if job['status'] in ('canceled','done','failed'):return
            now=time.time()
            if job['events'] and now-job['events'][-1]['at']<1 and all(job['events'][-1].get(k)==v for k,v in data.items()):return
            if data.get('kind')=='stage':job['progress'].update(requestStarted=None,waitUntil=None,api=None,apiId=None)
            elif data.get('kind') in ('response','api_error','network_error','invalid_request','cooldown','unavailable','done','failed','canceled'):job['progress']['requestStarted']=None
            event={'at':now,**data};job['events'].append(event);job['events']=job['events'][-60:]
            job['progress'].update(event);job['updatedAt']=event['at']
    def cancel(self,identity,session):
        if session!=self.session:raise ValueError('本次工作已结束')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',identity):raise ValueError('任务标识无效')
        with self.lock:
            self.tombstones={k:v for k,v in self.tombstones.items() if v>time.time()}
            if len(self.tombstones)>=512:self.tombstones.pop(next(iter(self.tombstones)))
            self.tombstones[identity]=time.time()+600
            job=self.items.get(identity)
            if job and job['status'] not in ('done','failed','canceled'):
                self.event(job,{'kind':'canceled','message':'用户停止：立即结束本地等待，迟到结果不会覆盖图片'})
                job['cancel'].set();job['status']='canceled';job['error']='任务已停止';job['errorCode']='canceled'
            return self.get(identity) if job else {'id':identity,'session':session,'status':'canceled','errorCode':'canceled','error':'任务已停止'}
    def submit(self,data,dispatch,model):
        if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}',model):raise ValueError('任务模型名称无效')
        identity=str(data.get('id',''));session=data.get('session')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',identity):raise ValueError('任务标识无效')
        if session!=self.session:raise ValueError('本次工作已结束，请刷新界面')
        digest=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.lock:
            if identity in self.items:
                if self.items[identity]['digest']!=digest:raise ValueError('任务标识与图片不一致')
                return self.get(identity)
            if self.tombstones.get(identity,0)>time.time():return {'id':identity,'session':session,'status':'canceled','errorCode':'canceled','error':'任务已停止'}
            if len(self.items)>=300:raise ValueError('本次 AI 任务数量已达上限，请重新启动工坊')
            job={'id':identity,'digest':digest,'session':session,'status':'queued','model':model,'cancel':threading.Event(),'events':[],'progress':{},'createdAt':time.time(),'updatedAt':time.time()}
            self.items[identity]=job;self.event(job,{'kind':'queued','phase':'排队','message':'任务已登记，等待处理槽位'})
        def run():
            mt=MODEL.set(model);ct=CANCEL.set(job['cancel']);rt=REPORT.set(lambda event:self.event(job,event));acquired=False
            try:
                while not self.slots.acquire(timeout=.1):check_cancel()
                acquired=True;check_cancel()
                with self.lock:job['status']='running'
                report('stage','开始处理',phase='准备图片');result=dispatch(data['feature'],data['payload'],model);check_cancel()
                with self.lock:
                    check_cancel();path=Path(self.storage.name)/(identity+'.json');path.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');job['resultPath']=str(path)
                    report('done','任务已完成');job['status']='done'
            except Exception as exc:
                with self.lock:
                    if job['cancel'].is_set():job['status']='canceled';job['error']='任务已停止';job['errorCode']='canceled'
                    else:
                        report('failed',str(exc));job['error']=str(exc);job['errorCode']=getattr(exc,'code','processing_error');job['status']='failed'
            finally:
                if acquired:self.slots.release()
                REPORT.reset(rt);MODEL.reset(mt);CANCEL.reset(ct)
        threading.Thread(target=run,daemon=True).start();return self.get(identity)
    def get(self,identity):
        with self.lock:
            job=self.items.get(identity)
            if not job:raise ValueError('任务不存在')
            result={k:v for k,v in job.items() if k not in ('cancel','digest','resultPath')};result['events']=[dict(e) for e in job['events']];result['progress']=dict(job['progress']);result['elapsed']=round((job['updatedAt'] if job['status'] in ('done','failed','canceled') else time.time())-job['createdAt'],1)
            if job.get('resultPath') and job['status']=='done':result['result']=json.loads(Path(job['resultPath']).read_text('utf-8'))
            return result
