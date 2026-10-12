# -*- coding: utf-8 -*-
"""Download protocol 13: durable files, early blob capture, bounded polling."""
import base64
import hashlib
import io
import json
import os
import time
import uuid
import zipfile
from pathlib import Path

CAPTURE_JS = r"""(() => {
  if (window.__patchiStableDownload) return;
  const s = {token:'', pending:new Map(), revoked:new Set(), blobs:new Map()};
  const click = HTMLAnchorElement.prototype.click;
  const dispatch = HTMLAnchorElement.prototype.dispatchEvent;
  const close = window.close.bind(window);
  const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = function(blob) {
    const url=create(blob);
    if(s.token && blob.size<=65*1024*1024) s.blobs.set(url,blob);
    return url;
  };
  URL.revokeObjectURL = function(url) {
    if(s.pending.has(url)) s.revoked.add(url);
    else {s.blobs.delete(url); revoke(url)}
  };
  window.close = function() { if(!s.token) return close() };
  function capture(a) {
    const url=a.href, token=s.token;
    if(!token || !a.hasAttribute('download') || !/^(blob:|data:)/i.test(url)) return null;
    if(s.pending.has(url)) return s.pending.get(url);
    if(s.pending.size>=3) return null;
    const blob=s.blobs.get(url);
    const task=(async()=>{
      try {
        const data=blob || await (await fetch(url)).blob();
        if(data.size<45 || data.size>65*1024*1024) return false;
        const value=await new Promise((yes,no)=>{const r=new FileReader();r.onload=()=>yes(r.result);r.onerror=no;r.readAsDataURL(data)});
        return await window.__patchiStorePNG(token,value);
      } catch(_) { return false }
    })();
    s.pending.set(url,task);
    return task;
  }
  function finish(url) {
    s.pending.delete(url); s.blobs.delete(url);
    if(s.revoked.delete(url)) revoke(url);
  }
  HTMLAnchorElement.prototype.click=function(...args) {
    const task=capture(this), a=this, url=this.href;
    if(!task) return click.apply(a,args);
    task.then(saved=>{if(!saved) click.apply(a,args)}).finally(()=>finish(url));
  };
  HTMLAnchorElement.prototype.dispatchEvent=function(event) {
    const task=event.type==='click' ? capture(this) : null, a=this, url=this.href;
    if(!task) return dispatch.call(a,event);
    task.then(saved=>{if(!saved) dispatch.call(a,event)}).finally(()=>finish(url));
    return true;
  };
  window.__patchiStableDownload={
    begin(token){s.token=token},
    end(){s.token='';for(const u of s.revoked)revoke(u);s.revoked.clear();s.blobs.clear()}
  };
})();"""


def read_png(data):
    from PIL import Image
    if not 45 <= len(data) <= 65*1024*1024:
        raise ValueError('下载文件大小无效')
    if data.startswith(b'PK\x03\x04'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files=[f for f in archive.infolist() if not f.is_dir() and f.filename.lower().endswith('.png')]
            if len(files)!=1 or files[0].file_size>60*1024*1024:
                raise ValueError('ZIP 必须只有一张 PNG')
            data=archive.read(files[0])
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('下载结果不是 PNG')
    with Image.open(io.BytesIO(data)) as image:
        if image.width*image.height>32_000_000:
            raise ValueError('图片尺寸超过限制')
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if 'A' not in image.getbands() and 'transparency' not in image.info:
            raise ValueError('图片没有透明背景')
        lo,hi=image.convert('RGBA').getchannel('A').getextrema()
        if lo==255 or hi==0:
            raise ValueError('图片不是有效抠图')
    return data


def atomic_write(path, data):
    path=Path(path)
    part=path.with_name(path.name+'.'+uuid.uuid4().hex[:8]+'.part')
    try:
        with part.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part,path)
    finally:
        part.unlink(missing_ok=True)


def install(g):
    if g.get('_patchi_stable_v13'):
        return
    g['_patchi_stable_v13']=True
    g['AUTOMATION_PROTOCOL']=13
    g['has_alpha_png']=lambda path: read_png(Path(path).read_bytes())
    original_launch=g['launch_saved_browser']
    original_guard=g['install_navigation_guard']
    original_alive=g['is_browser_context_alive']

    def ensure(context):
        if hasattr(context,'_patchi_stable_state'):
            return context._patchi_stable_state
        state={'active':None,'closed':False}
        context._patchi_stable_state=state
        context.on('close',lambda *_: state.update(closed=True))
        if context.browser:
            context.browser.on('disconnected',lambda *_: state.update(closed=True))

        def store(source, token, value):
            task=state['active']
            if not task or token!=task['token'] or not isinstance(value,str) or len(value)>92_000_000:
                return False
            pg=source['page']
            try:
                if pg!=task['page'] and pg.opener()!=task['page']:
                    return False
                data=read_png(base64.b64decode(value.split(',',1)[1],validate=True))
                digest=hashlib.sha256(data).hexdigest()
                atomic_write(task['folder']/('blob_'+digest+'.png'),data)
                print('[下载缓存] 网站 PNG 已完整写盘',flush=True)
                return True
            except Exception as exc:
                print('[下载缓存] 图片未通过验证：',str(exc)[:150],flush=True)
                return False

        context.expose_binding('__patchiStorePNG',store)
        context.add_init_script(CAPTURE_JS)
        return state

    def launch(playwright,visible=False):
        context=original_launch(playwright,visible=visible)
        ensure(context)
        return context

    def guard(context,page):
        original_guard(context,page)
        ensure(context)
        page.evaluate(CAPTURE_JS)

    def alive(context):
        state=getattr(context,'_patchi_stable_state',None)
        return not (state and state['closed']) and original_alive(context)

    def download(page,output_path,job):
        context=page.context
        state=ensure(context)
        token=uuid.uuid4().hex
        folder=g['APP_DATA']/'帕琪工坊'/'抠图下载缓存'/token
        folder.mkdir(parents=True,exist_ok=True)
        task={'token':token,'page':page,'folder':folder}
        state['active']=task
        own=getattr(context,'_patchi_download_dir',None)
        snapshots={str(p):g['_snapshot_downloads'](p) for p in (own,g['DOWNLOAD_DIR']) if p is not None}
        observed={}
        session=None
        activity={'started':False,'completed':False,'canceled':False}
        watched=[]
        nav_guard=getattr(page,'_patchi_download_guard',None)
        deadline=time.monotonic()+120
        rendered_checked=False
        acknowledged=False
        format_chosen=False

        def on_download(_):
            activity['started']=True

        def watch(pg):
            if pg not in watched:
                watched.append(pg)
                pg.on('download',on_download)

        def recover():
            # No Playwright call here: this still works after browser/context loss.
            values={}
            for item in folder.iterdir():
                if not item.is_file() or item.is_symlink() or item.suffix in ('.part','.crdownload'):
                    continue
                try:
                    raw=read_png(item.read_bytes())
                    values[hashlib.sha256(raw).hexdigest()]=raw
                except Exception:
                    continue
            if len(values)==1:
                atomic_write(output_path,next(iter(values.values())))
                g['set_job'](job,'downloaded','高清透明 PNG 已保存：'+Path(output_path).name)
                return True
            if g['_recover_download_file'](own,g['DOWNLOAD_DIR'],snapshots,observed,
                                            Path(output_path),acknowledged=acknowledged):
                read_png(Path(output_path).read_bytes())
                return True
            return False

        def pulse():
            try:
                if page.is_closed() and alive(context):
                    from playwright.sync_api import TimeoutError as PlaywrightTimeout
                    try: context.wait_for_event('close',timeout=250)
                    except PlaywrightTimeout: pass
                else:
                    page.wait_for_timeout(250)
            except Exception:
                # Disk recovery needs a second stable observation even after disconnect.
                if not alive(context):
                    for _ in range(15):
                        if recover():
                            return True
                        time.sleep(.1)
                    raise g['BrowserSessionLost']('下载连接中断，缓存尚无完整 PNG；保留登录状态重试')
                if page.is_closed():
                    return False
                raise
            return False

        def wait_result():
            nonlocal acknowledged,rendered_checked,format_chosen
            while time.monotonic()<deadline:
                with g['lock']:
                    if g['stop_flag']:
                        raise RuntimeError('用户停止任务')
                if recover():
                    return True
                if page.is_closed() or not alive(context):
                    if pulse(): return True
                    continue
                if not format_chosen and not activity['started']:
                    format_chosen=g['choose_png_format'](page)
                acknowledged=acknowledged or g['site_download_acknowledged'](page)
                reason=g['login_detected'](page)
                if reason and not acknowledged:
                    raise g['LoginRequired']('网站下载时要求登录：'+reason)
                if acknowledged and not rendered_checked and g.get('recover_rendered_png'):
                    rendered_checked=True
                    if g['recover_rendered_png'](page,Path(output_path),job):
                        return True
                if activity['canceled']:
                    if recover():
                        return True
                    raise RuntimeError('浏览器取消了下载，未生成完整 PNG')
                if pulse():
                    return True
            if recover():
                return True
            raise RuntimeError('下载等待超时，未生成完整透明 PNG；请检查网站下载权限和网络')

        try:
            # Chromium writes to a durable directory; Playwright context cleanup cannot delete it.
            session=context.new_cdp_session(page)
            info=session.send('Target.getTargetInfo')['targetInfo']
            options={'behavior':'allowAndName','downloadPath':str(folder),'eventsEnabled':True}
            if info.get('browserContextId'):
                options['browserContextId']=info['browserContextId']
            session.send('Browser.setDownloadBehavior',options)
            session.on('Browser.downloadWillBegin',lambda _:activity.update(started=True))
            session.on('Browser.downloadProgress',lambda e:activity.update(
                completed=e.get('state')=='completed',canceled=e.get('state')=='canceled'))
            print('[下载通道] 独立缓存，最长等待 120 秒',flush=True)
        except Exception as exc:
            print('[下载通道] 使用 PNG 数据与原下载目录备份：',str(exc)[:150],flush=True)

        try:
            page.evaluate(CAPTURE_JS)
            script='window.__patchiStableDownload.begin('+json.dumps(token)+');'
            context.add_init_script(CAPTURE_JS+'\n'+script)
            page.evaluate(script)
            for pg in context.pages:
                watch(pg)
            context.on('page',watch)
            if nav_guard is not None:
                nav_guard['until']=deadline+10
            g['assert_on_site'](page)
            g['check_login'](page)
            candidates,info=[],{}
            for tick in range(14):
                if recover():
                    return True
                if g['choose_png_format'](page):
                    format_chosen=True
                    return wait_result()
                candidates,info=g['probe_download_candidates'](page)
                if candidates:
                    break
                if g['result_visible'](page):
                    controls=list(g['visible_controls'](page,g['DOWNLOAD_LABEL']))
                    if controls:
                        candidates=[(controls[0],{'label':'下载高清大图'})]
                        break
                g['scroll_for_controls'](page,tick)
                if pulse():
                    return True
            if not candidates:
                raise RuntimeError('未定位结果下载按钮：'+json.dumps(info,ensure_ascii=False)[:280])
            print('[下载定位]',json.dumps(info.get('candidates',[])[:3],ensure_ascii=False)[:750],flush=True)
            locator,meta=candidates[0]
            g['set_job'](job,'running','下载高清 PNG，正在保存文件')
            try:
                locator.scroll_into_view_if_needed(timeout=4000)
                locator.click(timeout=6500)
            except Exception as exc:
                # A click can succeed and immediately close its page: recover before retrying.
                print('[下载点击]',str(exc)[:160],flush=True)
            return wait_result()
        except Exception:
            if recover():
                return True
            raise
        finally:
            state['active']=None
            for pg in watched:
                try:
                    pg.evaluate('window.__patchiStableDownload?.end()')
                    pg.remove_listener('download',on_download)
                except Exception:
                    pass
            try: context.remove_listener('page',watch)
            except Exception: pass
            if nav_guard is not None:
                nav_guard['until']=0
            if session is not None:
                try: session.detach()
                except Exception: pass
            # Keep failed transfers for diagnosis; remove only this successful job's cache.
            if Path(output_path).is_file():
                try:
                    read_png(Path(output_path).read_bytes())
                    import shutil
                    shutil.rmtree(folder)
                except Exception:
                    pass

    g.update(launch_saved_browser=launch,install_navigation_guard=guard,
             is_browser_context_alive=alive,try_download=download)
    original_log=g['Handler'].log_message
    def log_request(handler,fmt,*args):
        if handler.path.split('?',1)[0]!='/auto/status':
            original_log(handler,fmt,*args)
    g['Handler'].log_message=log_request

