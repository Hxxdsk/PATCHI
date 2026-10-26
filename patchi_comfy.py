"""Isolated ComfyUI bridge. No Gemini calls and no changes to other project tools."""
import base64
import hashlib
import io
import json
import re
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import uuid
import zipfile
import tempfile
from pathlib import Path
from PIL import Image

MAX_BODY=300*1024*1024

def endpoint(value):
    u=urllib.parse.urlsplit(str(value).strip())
    if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment:raise ValueError('ComfyUI 地址需为 http(s)://主机:端口，可含代理路径；不要包含密码或查询参数')
    return urllib.parse.urlunsplit((u.scheme,u.netloc,u.path.rstrip('/'),'',''))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):raise ValueError('ComfyUI 地址发生重定向，请填写最终服务地址后重试')

class Client:
    def __init__(self,url,token=''):self.url=endpoint(url);self.token=token
    def call(self,path,data=None,content_type=None,timeout=12,limit=4*1024*1024):
        headers={'Accept':'image/png' if content_type=='view' else 'application/json'}
        if data is not None:headers['Content-Type']=content_type or 'application/json'
        if self.token:headers['Authorization']='Bearer '+self.token
        if data is not None and not isinstance(data,bytes):data=json.dumps(data).encode()
        req=urllib.request.Request(self.url+path,data=data,headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=timeout) as r:raw=r.read(limit+1)
        except urllib.error.HTTPError as e:raise ValueError('ComfyUI HTTP %d；检查地址、认证、节点及执行记录'%e.code) from None
        except (TimeoutError,OSError):raise ValueError('ComfyUI 连接中断或超时；未自动重复提交，请查看 ComfyUI 队列') from None
        if len(raw)>limit:raise ValueError('ComfyUI 响应超出限制')
        if content_type=='view':return raw
        try:return json.loads(raw)
        except ValueError:raise ValueError('ComfyUI 返回了非 JSON 数据，请检查地址是否是 ComfyUI API') from None
    def upload(self,name,raw,folder):
        boundary='patchi'+uuid.uuid4().hex
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\nContent-Type: image/png\r\n\r\n'.encode()+raw+f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\n{folder}\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\nfalse\r\n--{boundary}--\r\n'.encode())
        x=self.call('/upload/image',body,'multipart/form-data; boundary='+boundary,timeout=30,limit=8192)
        filename=x.get('name');sub=x.get('subfolder','')
        if not isinstance(filename,str) or not isinstance(sub,str) or any(k in filename+sub for k in ('..','\\','\x00')) or '/' in filename or sub.startswith('/'):raise ValueError('ComfyUI 返回无效素材路径')
        return (sub+'/' if sub else '')+filename
    def image(self,entry):
        if entry.get('type')!='output':raise ValueError('ComfyUI 返回的不是输出图片')
        return self.call('/view?'+urllib.parse.urlencode({k:entry[k] for k in ('filename','subfolder','type') if k in entry}),content_type='view',limit=100*1024*1024,timeout=30)


def read_data(value):
    from patchi_mainphoto import decode_image
    im=decode_image(value);out=io.BytesIO();im.save(out,format='PNG');return out.getvalue()

def workflow(task,prefix):
    return {'1':{'class_type':'PatchiSticker','inputs':{'task_json':json.dumps(task,ensure_ascii=False)}},'2':{'class_type':'SaveImage','inputs':{'images':['1',0],'filename_prefix':prefix+'/final'}},'3':{'class_type':'SaveImage','inputs':{'images':['1',1],'filename_prefix':prefix+'/overlay'}}}

class Bridge:
    def __init__(self,directory):
        self.directory=Path(directory);self.configfile=self.directory/'comfy_settings.json';self.lock=threading.RLock();self.slots=threading.Semaphore(1);self.tasks={};self.tombstones={};self.url='http://127.0.0.1:8188';self.token='';self.storage=tempfile.TemporaryDirectory(prefix='patchi_comfy_')
        try:self.url=endpoint(json.loads(self.configfile.read_text('utf-8'))['url'])
        except (OSError,ValueError,KeyError):pass
    def settings(self):return {'url':self.url,'hasToken':bool(self.token),'protocol':1}
    def configure(self,data):
        url=endpoint(data.get('url',self.url));token=data.get('token')
        if token is not None and (not isinstance(token,str) or len(token)>2048 or '\r' in token or '\n' in token):raise ValueError('认证令牌格式无效')
        with self.lock:
            self.directory.mkdir(parents=True,exist_ok=True);self.configfile.write_text(json.dumps({'url':url}),encoding='utf-8')
            if url!=self.url:self.token=''
            self.url=url
            if token is not None:self.token=token
        return self.settings()
    def check(self):
        client=Client(self.url,self.token);info=client.call('/patchi/sticker/status');nodes=client.call('/object_info/PatchiSticker')
        if info.get('protocol')!=1 or 'PatchiSticker' not in nodes:raise ValueError('请先安装本包 ComfyUI_PatchiSticker 节点并重启 ComfyUI')
        return {**info,'ok':all(info.get('models',{}).get(k) for k in ('depth','segment')),'url':self.url}
    def event(self,job,message,index=None):
        with self.lock:
            if job['stop'].is_set():return
            job['message']=message;job['updatedAt']=time.time();job['events'].append({'at':time.time(),'message':message,'index':index});job['events']=job['events'][-80:]
    def get(self,identity,session):
        with self.lock:
            job=self.tasks.get(identity)
            if not job or job['session']!=session:raise ValueError('本次外部贴标任务不存在')
            return {k:([dict(v) for v in value] if k in ('items','events') else value) for k,value in job.items() if k not in ('stop','payload','digest','client')}
    def submit(self,data,session):
        identity=data.get('id');pages=data.get('pages')
        if data.get('session')!=session:raise ValueError('本次工作已结束，请重新打开入口')
        if not isinstance(identity,str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',identity):raise ValueError('任务 ID 无效')
        if not isinstance(pages,list) or not 1<=len(pages)<=80:raise ValueError('一次上传 1–80 张同款图')
        for p in pages:
            if not isinstance(p,dict) or not isinstance(p.get('name'),str) or not isinstance(p.get('original'),str) or not isinstance(p.get('logos'),list) or not 1<=len(p['logos'])<=8:raise ValueError('每张图需要名称、原图和 1–8 个 Logo')
        digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
        with self.lock:
            if self.tombstones.get((session,identity),0)>time.time():return {'id':identity,'session':session,'status':'canceled','items':[],'events':[],'message':'任务已停止'}
            if identity in self.tasks:
                if self.tasks[identity]['digest']!=digest:raise ValueError('任务 ID 已用于其他内容')
                return self.get(identity,session)
            if len(self.tasks)>=30:raise ValueError('本次外部任务已达上限，请完成导出后重启启动器')
            job={'id':identity,'session':session,'digest':digest,'payload':data,'client':Client(self.url,self.token),'status':'queued','stop':threading.Event(),'createdAt':time.time(),'updatedAt':time.time(),'events':[],'message':'等待顺序处理','items':[{'name':p['name'],'status':'queued','index':i} for i,p in enumerate(pages)]}
            self.tasks[identity]=job
        threading.Thread(target=self._run,args=(job,),daemon=True).start();return self.get(identity,session)
    def cancel(self,identity,session):
        with self.lock:
            job=self.tasks.get(identity)
            if not isinstance(identity,str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',identity):raise ValueError('任务 ID 无效')
            self.tombstones={k:v for k,v in self.tombstones.items() if v>time.time()}
            if len(self.tombstones)>512:self.tombstones.pop(next(iter(self.tombstones)))
            self.tombstones[(session,identity)]=time.time()+2400
            if not job:return {'id':identity,'status':'canceled'}
            if job['session']!=session:raise ValueError('任务不存在')
            if job['status'] in ('done','failed','canceled'):return self.get(identity,session)
            job['stop'].set();job['status']='canceled';job['message']='已立即停止等待；已完成图片保留，迟到结果忽略';job['updatedAt']=time.time()
            for item in job['items']:
                if item['status'] in ('running','queued'):item['status']='canceled'
        threading.Thread(target=self._cancel_remote,args=(job,),daemon=True).start();return self.get(identity,session)
    def _cancel_remote(self,job):
        client=job['client']
        try:client.call('/patchi/sticker/cancel',{'runId':job['id']},timeout=3)
        except ValueError:pass
        prompt=job.get('promptId')
        if prompt:
            try:client.call('/queue',{'delete':[prompt]},timeout=3)
            except ValueError:pass
        # Deliberately never use /interrupt: it would cancel other ComfyUI users' tasks.
    def reset(self):
        for identity,job in list(self.tasks.items()):self.cancel(identity,job['session'])
        with self.lock:self.tasks.clear();self.tombstones.clear()
    def _run(self,job):
        def check():
            if job['stop'].is_set():raise InterruptedError('已停止')
        acquired=False;client=job['client'];cache={}
        def asset(value):
            check();digest=hashlib.sha256(value.encode()).hexdigest()
            if digest not in cache:
                raw=read_data(value);check();cache[digest]=client.upload(digest+'.png',raw,'patchi_'+job['id'])
            check();return cache[digest]
        try:
            while not self.slots.acquire(timeout=.15):check()
            acquired=True;check();job['status']='running';self.event(job,'检查外部节点与本地模型')
            state=client.call('/patchi/sticker/status')
            if state.get('protocol')!=1 or not all(state.get('models',{}).get(k) for k in ('depth','segment')):raise ValueError('外部节点或模型未就绪；请按部署教程安装并准备模型')
            data=job['payload'];reference=asset(data['reference']);pages=data['pages']
            for index,page in enumerate(pages):
                check();item=job['items'][index];item['status']='running';self.event(job,'上传原图和 Logo：'+page['name'],index)
                try:
                    task={'runId':job['id'],'original':asset(page['original']),'reference':reference,'logos':[],'options':data.get('options',{})}
                    for logo in page['logos']:
                        task['logos'].append({k:logo[k] for k in ('id','x','y','w','h','rotation','linked','role') if k in logo});task['logos'][-1]['image']=asset(logo['src'])
                    check();graph=workflow(task,'patchi_'+job['id']+'/'+str(index));self.event(job,'提交到 ComfyUI；CPU 顺序处理 '+page['name'],index)
                    reply=client.call('/prompt',{'prompt':graph,'client_id':'patchi_'+job['id']},timeout=15)
                    prompt=reply.get('prompt_id')
                    if not isinstance(prompt,str) or not prompt:raise ValueError('ComfyUI 拒绝工作流：'+json.dumps(reply.get('node_errors',{}),ensure_ascii=False)[:500])
                    job['promptId']=prompt
                    if job['stop'].is_set():self._cancel_remote(job);check()
                    started=time.monotonic();last=''
                    while True:
                        check()
                        if time.monotonic()-started>2400:raise ValueError('单张任务超过 40 分钟，停止等待；请检查 ComfyUI 队列')
                        record=client.call('/history/'+urllib.parse.quote(prompt,safe='')).get(prompt)
                        check()
                        if record:
                            if record.get('status',{}).get('status_str')=='error':
                                errors=[m[1].get('exception_message','节点执行失败') for m in record.get('status',{}).get('messages',[]) if isinstance(m,list) and len(m)>1 and m[0]=='execution_error'];raise ValueError('ComfyUI：'+str(errors[-1] if errors else '工作流执行失败')[:600])
                            outputs=record.get('outputs',{})
                            if outputs.get('2',{}).get('images') and outputs.get('3',{}).get('images'):break
                            if record.get('status',{}).get('completed'):raise ValueError('ComfyUI 未返回最终图与贴标层，请检查节点版本')
                        try:
                            progress=client.call('/patchi/sticker/progress/'+job['id'],timeout=4);message=progress.get('message','排队/等待模型处理')
                        except ValueError:message='排队/等待模型处理'
                        if message!=last:self.event(job,message,index);last=message
                        with self.lock:
                            if not job['stop'].is_set():job['elapsedImage']=round(time.monotonic()-started)
                        job['stop'].wait(1)
                    raw=client.image(outputs['2']['images'][0]);check();overlay=client.image(outputs['3']['images'][0]);check()
                    with Image.open(io.BytesIO(raw)) as im,Image.open(io.BytesIO(overlay)) as mask:
                        if im.width*im.height>32_000_000 or im.size!=mask.size:raise ValueError('外部输出尺寸异常')
                        with Image.open(io.BytesIO(read_data(page['original']))) as source:
                            if im.size!=source.size:raise ValueError('外部输出未保留原图尺寸')
                        im.verify();mask.verify()
                    report={}
                    try:report=json.loads(outputs.get('1',{}).get('text',['{}'])[0])
                    except (ValueError,TypeError,IndexError):pass
                    with self.lock:
                        check();folder=Path(self.storage.name)/job['id'];folder.mkdir(exist_ok=True);(folder/(str(index)+'.png')).write_bytes(raw);(folder/(str(index)+'_overlay.png')).write_bytes(overlay)
                        item.update(status='done',report=report);self.event(job,'已完成，待逐图复核：'+page['name'],index)
                except InterruptedError:raise
                except Exception as exc:
                    with self.lock:
                        check();item.update(status='failed',error=str(exc)[:800]);self.event(job,item['error'],index)
                    # Ambiguous network failures are never resubmitted or followed by more work.
                    if '连接' in str(exc) or '超时' in str(exc):raise
            check()
            with self.lock:job['status']='done';self.event(job,'批次完成，请逐图复核后导出')
        except Exception as exc:
            with self.lock:
                if job['stop'].is_set():job['status']='canceled'
                else:
                    self.event(job,str(exc)[:800]);job['status']='failed'
                    for item in job['items']:
                        if item['status'] in ('queued','running'):item.update(status='failed',error=str(exc)[:800])
        finally:
            if acquired:self.slots.release()
            with self.lock:job.pop('payload',None)
    def result(self,identity,index,session,overlay=False):
        job=self.get(identity,session)
        if index<0 or index>=len(job['items']) or job['items'][index]['status']!='done':raise ValueError('此张暂无完成结果')
        return (Path(self.storage.name)/identity/(str(index)+('_overlay' if overlay else '')+'.png')).read_bytes()
    def export(self,identity,session,accepted):
        job=self.get(identity,session)
        if not isinstance(accepted,list) or not accepted or any(isinstance(i,bool) or not isinstance(i,int) for i in accepted):raise ValueError('请先复核并勾选要导出的图片')
        if len(set(accepted))!=len(accepted):raise ValueError('导出序号重复')
        out=io.BytesIO();used=set()
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
            for i in accepted:
                raw=self.result(identity,i,session);item=job['items'][i];name=re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',item['name'])[:180] or 'image.png';ext=Path(name).suffix.lower()
                if ext not in ('.jpg','.jpeg','.webp','.png'):name=Path(name).stem+'.png';ext='.png'
                stem=Path(name).stem;n=2
                while name.casefold() in used:name=stem+' ('+str(n)+')'+ext;n+=1
                used.add(name.casefold())
                with Image.open(io.BytesIO(raw)) as im:
                    if ext in ('.jpg','.jpeg'):
                        rgba=im.convert('RGBA');im=Image.new('RGB',im.size,'white');im.paste(rgba,mask=rgba.getchannel('A'))
                    f=io.BytesIO();im.save(f,format={'.jpg':'JPEG','.jpeg':'JPEG','.webp':'WEBP','.png':'PNG'}[ext],**({'quality':97} if ext!='.png' else {}));z.writestr(name,f.getvalue())
        return out.getvalue()
