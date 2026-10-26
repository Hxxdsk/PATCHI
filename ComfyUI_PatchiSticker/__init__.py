"""Independent PATCHI compositor node and per-run cancel endpoint; no upstream Transform code."""
import json
import threading
import time
from pathlib import Path
import numpy as np
import torch
import folder_paths
from server import PromptServer
from aiohttp import web
from .engine import process
from .semantic import predict,readiness

_lock=threading.RLock();_stops={};_progress={}

@PromptServer.instance.routes.get('/patchi/sticker/progress/{run}')
async def patchi_progress(request):
    with _lock:return web.json_response(_progress.get(request.match_info['run'],{'message':'等待模型处理'}))

def canceled(run):
    with _lock:return _stops.get(run,0)>time.time()

@PromptServer.instance.routes.get('/patchi/sticker/status')
async def patchi_status(request):
    return web.json_response({'protocol':1,'node':'PatchiSticker','engine':'cpu-depth-clipseg','models':readiness(),'device':'cpu','maxPixels':32000000})

@PromptServer.instance.routes.post('/patchi/sticker/cancel')
async def patchi_cancel(request):
    data=await request.json();run=data.get('runId','')
    if not isinstance(run,str) or not 1<=len(run)<=80:return web.json_response({'error':'Invalid runId'},status=400)
    with _lock:
        now=time.time()
        for key in list(_stops):
            if _stops[key]<=now:_stops.pop(key,None)
        if len(_stops)>=512:_stops.pop(next(iter(_stops)))
        _stops[run]=now+7200
    return web.json_response({'ok':True})

def read_image(name):
    from PIL import Image,ImageOps
    if not isinstance(name,str) or '\\' in name or '..' in Path(name).parts:raise ValueError('Invalid asset path')
    root=Path(folder_paths.get_input_directory()).resolve();path=(root/name).resolve()
    if not path.is_relative_to(root) or not path.is_file():raise ValueError('Asset not in ComfyUI input directory')
    with Image.open(path) as im:
        if im.width*im.height>32000000:raise ValueError('Image exceeds 32MP')
        if im.format not in ('PNG','JPEG','WEBP'):raise ValueError('Unsupported image')
        return ImageOps.exif_transpose(im).convert('RGBA')

class PatchiSticker:
    @classmethod
    def INPUT_TYPES(cls):return {'required':{'task_json':('STRING',{'multiline':True,'default':'{}'})}}
    RETURN_TYPES=('IMAGE','IMAGE','STRING');RETURN_NAMES=('贴标原图','透明贴标层','检查记录');FUNCTION='execute';CATEGORY='PATCHI'
    @classmethod
    def IS_CHANGED(cls,**kwargs):return float('nan')
    def execute(self,task_json):
        data=json.loads(task_json);run=data['runId']
        def check():
            if canceled(run):raise RuntimeError('PATCHI task canceled')
        def progress(message):
            with _lock:
                if len(_progress)>128:_progress.pop(next(iter(_progress)))
                _progress[run]={'runId':run,'message':message,'at':time.time()}
            PromptServer.instance.send_sync('patchi_progress',{'runId':run,'message':message})
        check();result,overlay,report=process(read_image(data['original']),read_image(data['reference']),data['logos'],read_image,predict,data.get('options',{}),check,progress)
        check();tensor=lambda im:torch.from_numpy(np.asarray(im).astype(np.float32)/255.)[None,...]
        return {'ui':{'text':[json.dumps(report,ensure_ascii=False)]},'result':(tensor(result),tensor(overlay),json.dumps(report,ensure_ascii=False))}

NODE_CLASS_MAPPINGS={'PatchiSticker':PatchiSticker}
NODE_DISPLAY_NAME_MAPPINGS={'PatchiSticker':'PATCHI · 批量服装贴标（CPU）'}
