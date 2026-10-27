"""Gemini vision plans occlusion/light; original logo pixels are composed locally."""
import copy
import base64
import io
import json
import math
import re
import urllib.request
import urllib.error
import numpy as np
from PIL import Image, ImageOps, ImageFilter, ImageDraw
from patchi_ai import report, check_cancel

MAX_PIXELS = 32_000_000
MAX_FILE = 128 * 1024 * 1024
MAX_LOGOS = 8


def decode_image(value, max_pixels=MAX_PIXELS, max_file=MAX_FILE):
    if not isinstance(value, str) or not re.match(r'^data:image/(png|jpeg|webp);base64,', value):
        raise ValueError('请上传 PNG、JPG 或 WebP 图片')
    try:
        encoded=value.split(',',1)[1]
        if len(encoded)>4*((max_file+2)//3): raise ValueError('图片超过 %dMB 输入上限'%(max_file//(1024*1024)))
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > max_file: raise ValueError('图片超过 %dMB 输入上限'%(max_file//(1024*1024)))
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > max_pixels: raise ValueError('图片超过像素限制')
            if image.format not in ('PNG', 'JPEG', 'WEBP'): raise ValueError('图片格式不支持')
            return ImageOps.exif_transpose(image).convert('RGBA')
    except (OSError, base64.binascii.Error) as exc:
        raise ValueError('图片解码失败') from exc


def png_data(image):
    buffer = io.BytesIO(); image.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def finite(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(name + '超出范围')
    return float(value)


def prepare(payload):
    check_cancel()
    original = decode_image(payload.get('original'))
    w, h = original.size
    logos = payload.get('logos')
    if not isinstance(logos, list) or not 1 <= len(logos) <= MAX_LOGOS: raise ValueError('每张主图需要 1–8 个 Logo')
    ids = set(); layers = []
    for entry in logos:
        check_cancel()
        if not isinstance(entry, dict): raise ValueError('Logo 数据错误')
        identity = str(entry.get('id', ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identity) or identity in ids: raise ValueError('Logo 标识重复或无效')
        ids.add(identity)
        image = decode_image(entry.get('src'), MAX_PIXELS, MAX_FILE)
        lw = finite(entry.get('w'), 1, 2*w, 'Logo 宽度'); lh = finite(entry.get('h'), 1, 2*h, 'Logo 高度')
        if lw*lh > 16_000_000: raise ValueError('Logo 输出面积超过 1600 万像素，请缩小尺寸')
        x = finite(entry.get('x'), -w, 2*w, 'Logo 横坐标'); y = finite(entry.get('y'), -h, 2*h, 'Logo 纵坐标')
        angle = finite(entry.get('rotation', 0), -180, 180, 'Logo 角度')
        # Premultiply before filtering: transparent padding cannot create black fringes.
        patch = image.convert('RGBa').resize((max(1, round(lw)), max(1, round(lh))), Image.Resampling.LANCZOS).convert('RGBA')
        if angle: patch = patch.convert('RGBa').rotate(-angle, Image.Resampling.BICUBIC, expand=True).convert('RGBA')
        px = round(x+lw/2-patch.width/2); py = round(y+lh/2-patch.height/2)
        inset = max(12, round(max(lw, lh)*.35))
        box = (max(0, px-inset), max(0, py-inset), min(w, px+patch.width+inset), min(h, py+patch.height+inset))
        if box[2] <= box[0] or box[3] <= box[1]: raise ValueError('Logo 完全在画布外，请调整位置')
        tile = Image.new('RGBA', (box[2]-box[0], box[3]-box[1]))
        tile.alpha_composite(patch, (px-box[0], py-box[1]))
        if not tile.getchannel('A').getbbox(): raise ValueError('Logo 没有可见像素')
        layers.append({'id':identity, 'name':str(entry.get('name','Logo'))[:120], 'box':box, 'tile':tile})
    composite = original.copy()
    for layer in layers: composite.alpha_composite(layer['tile'], layer['box'][:2])
    return original, composite, layers


def api_image(image, edge):
    image = image.copy(); image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    # Vision only: working/export originals are not reduced.
    background = Image.new('RGB', image.size, 'white'); background.paste(image, mask=image.getchannel('A'))
    buffer = io.BytesIO(); background.save(buffer, format='JPEG', quality=94)
    return {'inlineData':{'mimeType':'image/jpeg','data':base64.b64encode(buffer.getvalue()).decode('ascii')}}


from patchi_ai_prompts import MAIN_SCHEMA as SCHEMA, MAIN_PROMPT


def request_plan(original, composite, layers, key, model, transport=None, advanced=False):
    from patchi_ai_prompts import ADVANCED_MAIN_PROMPT
    parts = [{'text':ADVANCED_MAIN_PROMPT if advanced else MAIN_PROMPT}]
    parts += [api_image(original, 1400), api_image(composite, 1400)]
    for layer in layers:
        parts.append({'text':'REGION '+layer['id']+' — next images: ORIGINAL crop, then COMPOSITED crop. Coordinates are local to this full crop.'})
        parts += [api_image(original.crop(layer['box']), 1000), api_image(composite.crop(layer['box']), 1000)]
    schema=copy.deepcopy(SCHEMA)
    if advanced:
        item=schema['properties']['regions']['items'];item['properties']['occlusionConfidence']={'type':'NUMBER'};item['properties']['fullyOccluded']={'type':'BOOLEAN'};item['properties']['occlusionEvidence']={'type':'STRING'};item['required']+=['occlusionConfidence','fullyOccluded','occlusionEvidence','warp','material','textureStrength']
    body = {'contents':[{'role':'user','parts':parts}], 'generationConfig':{'temperature':.1,'maxOutputTokens':12288,'responseMimeType':'application/json','responseSchema':schema}}
    return request_json(body,key,model,transport)


def request_json(body,key,model,transport=None):
    encoded = json.dumps(body).encode('utf-8')
    if len(encoded) > 18*1024*1024: raise ValueError('AI 检查数据过大，请减少本张主图的 Logo 数量')
    if transport:
        data = transport(body)
    else:
        req = urllib.request.Request('https://generativelanguage.googleapis.com/v1beta/models/'+model+':generateContent', data=encoded,
                                     headers={'Content-Type':'application/json','x-goog-api-key':key})
        try:
            with urllib.request.urlopen(req, timeout=150) as response:
                raw = response.read(4*1024*1024+1)
                if len(raw) > 4*1024*1024: raise RuntimeError('AI 检查返回数据过大')
                data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Do not echo upstream body: requests may contain private photo/key details.
            if exc.code in (401,403): raise RuntimeError('Gemini 密钥、项目权限或地区不可用，请检查 API 设置') from None
            if exc.code == 429: raise RuntimeError('Gemini 配额或速率受限，本图未改动；稍后可继续处理') from None
            if exc.code == 404: raise RuntimeError('所选 Gemini 模型不可用，请在 API 设置检测并选择可用视觉模型') from None
            raise RuntimeError('Gemini 检查失败（HTTP %d），本图保留手动贴标'%exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError('Gemini 连接失败或超时，本图未改动；检查网络后重试') from None
    try:
        candidate = data['candidates'][0]
        text = ''.join(part.get('text','') for part in candidate['content']['parts'] if not part.get('thought'))
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
        if not text.startswith('{') and '{' in text and '}' in text:text=text[text.find('{'):text.rfind('}')+1]
        result = json.loads(text)
        if not isinstance(result, dict): raise ValueError()
        return result
    except (KeyError, IndexError, TypeError, ValueError):
        reason=str(data.get('candidates',[{}])[0].get('finishReason','')) if isinstance(data,dict) and data.get('candidates') else ''
        raise RuntimeError('Gemini 未返回有效检查结果'+('（输出被截断，请减少本图 Logo 数量或更换模型）' if reason=='MAX_TOKENS' else '（请查看模型是否支持视觉与 JSON 输出）')+'，本图保留手动贴标') from None


def fold_warp(tile, region):
    """Bounded, premultiplied one-pass deformation at export resolution."""
    warp=region.get('warp',[])
    if not warp: return tile
    if not isinstance(warp,list) or len(warp)!=25: raise ValueError('AI 褶皱网格无效')
    limit=120 if region.get('_advanced') else 40
    grid=np.array([[finite(v,-limit,limit,'AI 褶皱位移') for v in pair] if isinstance(pair,list) and len(pair)==2 else [float('nan')]*2 for pair in warp],dtype=np.float32).reshape(5,5,2)
    if not np.isfinite(grid).all(): raise ValueError('AI 褶皱坐标无效')
    grid-=grid.mean(axis=(0,1),keepdims=True)
    # Adjacent control cells must retain orientation and restrained local stretch.
    points=np.stack(np.meshgrid(np.linspace(0,1000,5),np.linspace(0,1000,5)),axis=-1)+grid
    for y in range(4):
        for x in range(4):
            dx=(points[y,x+1]-points[y,x])/250; dy=(points[y+1,x]-points[y,x])/250
            determinant=dx[0]*dy[1]-dx[1]*dy[0]
            if not (.4 if region.get('_advanced') else .65)<=determinant<=(1.8 if region.get('_advanced') else 1.4): raise ValueError('AI 褶皱变形过强，已保留原贴标')
    import cv2
    bounds=tile.getchannel('A').getbbox(); x,y,r,b=bounds; w=r-x; h=b-y
    dense=cv2.resize(grid,(w,h),interpolation=cv2.INTER_LINEAR)
    if region.get('_advanced'):
        px=math.ceil(float(np.max(np.abs(grid[:,:,0])))*w/1000)+2;py=math.ceil(float(np.max(np.abs(grid[:,:,1])))*h/1000)+2
        nx,ny=max(0,x-px),max(0,y-py);nr,nb=min(tile.width,r+px),min(tile.height,b+py)
        expanded=cv2.copyMakeBorder(dense,py,py,px,px,cv2.BORDER_REPLICATE)
        dense=expanded[py+ny-y:py+nb-y,px+nx-x:px+nr-x];x,y,r,b=nx,ny,nr,nb
    yy,xx=np.mgrid[y:b,x:r].astype(np.float32)
    mx,my=np.meshgrid(np.arange(tile.width,dtype=np.float32),np.arange(tile.height,dtype=np.float32))
    mx[y:b,x:r]=xx+dense[:,:,0]*w/1000; my[y:b,x:r]=yy+dense[:,:,1]*h/1000
    premul=np.array(tile.convert('RGBa'))
    result=cv2.remap(premul,mx,my,cv2.INTER_CUBIC,borderMode=cv2.BORDER_CONSTANT)
    # RGBa values may not exceed alpha after cubic interpolation.
    result[:,:,:3]=np.minimum(result[:,:,:3],result[:,:,3,None])
    return Image.fromarray(result,'RGBa').convert('RGBA')


def compose(original, layers, plan):
    regions = plan.get('regions')
    if not isinstance(regions, list) or len(regions) != len(layers): raise ValueError('AI 未检查全部 Logo，请重试')
    records = {}; notes=[]
    for region in regions:
        if not isinstance(region, dict) or region.get('id') in records: raise ValueError('AI 区域标识错误')
        records[region.get('id')] = region
    overlay = Image.new('RGBA', original.size)
    for layer in layers:
        region = records.get(layer['id'])
        if not region: raise ValueError('AI 遗漏 Logo，请重试')
        check_cancel()
        box=layer['box']
        try:tile=fold_warp(layer['tile'],region)
        except ValueError as exc:
            if not region.get('_recover'):raise
            tile=layer['tile'];region['note']=str(region.get('note',''))+'；褶皱建议无效，已保留 Logo 原形'
            report('warning','模型褶皱建议无效，保留 Logo 原形继续合成')
        tw,th=tile.size
        strength = finite(region.get('lightStrength'), 0, 1, 'AI 光照强度')
        gain = finite(region.get('gain'), .8, 1.2, 'AI 明度')
        # Two spatial scales keep fold relief instead of a flat color wash.
        crop=original.crop(box).convert('RGB')
        fine=np.asarray(crop.filter(ImageFilter.GaussianBlur(max(.7,min(tw,th)/350))),dtype=np.float32) @ np.array([.2126,.7152,.0722],np.float32)
        broad=np.asarray(crop.filter(ImageFilter.GaussianBlur(max(2,min(tw,th)/35))),dtype=np.float32) @ np.array([.2126,.7152,.0722],np.float32)
        arr=np.array(tile); footprint=arr[:,:,3]>128
        mid=np.median(broad[footprint]) if footprint.any() else np.median(broad)
        texture=finite(region.get('textureStrength',.25),0,.65,'AI 纹理强度')
        field=1+(np.clip((broad+24)/(mid+24),.65,1.35)-1)*strength
        detail=np.clip((fine+24)/(broad+24),.7,1.3)
        field=np.clip(field*(1+(detail-1)*texture)*gain,.55,1.45)
        arr[:,:,:3]=np.clip(arr[:,:,:3].astype(np.float32)*field[:,:,None],0,255).round().astype(np.uint8)
        lit = Image.fromarray(arr)
        dx = finite(region.get('shadowDx'), -15, 15, 'AI 阴影偏移')*tw/1000
        dy = finite(region.get('shadowDy'), -15, 15, 'AI 阴影偏移')*th/1000
        blur = finite(region.get('shadowBlur'), 0, 15, 'AI 阴影柔化')*max(tw,th)/1000
        opacity = finite(region.get('shadowOpacity'), 0, .2, 'AI 阴影强度')
        material=region.get('material')
        if material is not None and material not in ('print','woven','embroidered','patch'): raise ValueError('AI 材质标识无效')
        if material=='print': opacity=0
        elif material:
            bounds=tile.getchannel('A').getbbox(); edge=min(bounds[2]-bounds[0],bounds[3]-bounds[1])
            contact=max(.5,edge*.008); dx=max(-contact,min(contact,dx)); dy=max(-contact,min(contact,dy)); blur=min(blur,contact); opacity=min(opacity,.1)
        shadow = Image.new('L',tile.size)
        if opacity:
            shadow.paste(tile.getchannel('A'),(round(dx),round(dy)))
            shadow=shadow.filter(ImageFilter.GaussianBlur(blur)).point(lambda a:round(a*opacity))
        result = Image.new('RGBA', tile.size); result.putalpha(shadow); result.alpha_composite(lit)
        polygons = region.get('occlusions')
        if not isinstance(polygons, list) or len(polygons)>16: raise ValueError('AI 遮挡轮廓错误')
        mask = Image.new('L', tile.size); painter=ImageDraw.Draw(mask)
        for polygon in polygons:
            if not isinstance(polygon,list) or not 3 <= len(polygon) <= 80: raise ValueError('AI 遮挡轮廓点数错误')
            points=[]
            for pt in polygon:
                if not isinstance(pt,list) or len(pt)!=2: raise ValueError('AI 遮挡坐标错误')
                points.append((finite(pt[0],0,1000,'AI 遮挡坐标')*tw/1000,finite(pt[1],0,1000,'AI 遮挡坐标')*th/1000))
            painter.polygon(points, fill=255)
        hidden = (np.asarray(mask)>0)&footprint
        confidence=finite(region.get('occlusionConfidence',0),0,1,'AI 遮挡置信度')
        maximum=.98 if region.get('_advanced') and confidence>=.85 else .7
        if region.get('_advanced') and confidence>=.95 and region.get('fullyOccluded') is True and isinstance(region.get('occlusionEvidence'),str) and region['occlusionEvidence'].strip():maximum=1.
        if footprint.any() and hidden.sum()/footprint.sum()>maximum and region.get('_recover'):
            mask=Image.new('L',tile.size);hidden=np.zeros_like(footprint);region['note']=str(region.get('note',''))+'；遮挡范围异常，未应用此擦除建议，请用保护画笔复核'
            report('warning','模型建议的遮挡范围异常，保留原标识，仅应用可用光影')
        elif footprint.any() and hidden.sum()/footprint.sum()>maximum:
            raise ValueError('AI 建议遮盖超过 %d%% Logo 或置信度不足，已保留原贴标；请人工复核'%int(maximum*100))
        # Restore actual original foreground by zeroing only occluded overlay pixels.
        alpha=np.array(result.getchannel('A')); alpha[np.asarray(mask)>0]=0
        result.putalpha(Image.fromarray(alpha)); overlay.alpha_composite(result, box[:2])
        notes.append({'id':layer['id'],'name':layer['name'],'note':str(region.get('note',''))[:500], 'occludedPixels':int(hidden.sum())})
    return {'ok':True,'overlay':png_data(overlay),'width':original.width,'height':original.height,
            'summary':str(plan.get('summary','已逐个检查 Logo 遮挡与光照'))[:1200], 'regions':notes,
            'notice':'已保留原 Logo 轮廓与透明度；光照仅调整颜色。请核对领口、抽绳等细部遮挡。'}


def local_plan(layers):
    return {'summary':'本地光影融合：保留位置、图案与不透明度；遮挡请用保留原图画笔修正。',
            'regions':[{'id':l['id'],'note':'原始像素贴标，局部明暗与细纹理融合，不自动擦除或形变。',
             'occlusions':[],'lightStrength':.32,'textureStrength':.18,'gain':1,
             'material':'print','shadowDx':0,'shadowDy':0,'shadowBlur':0,'shadowOpacity':0} for l in layers]}


def align_to_reference(payload,original,composite,key,model,transport):
    from patchi_ai_prompts import ALIGN_SCHEMA,ALIGN_PROMPT
    entries=payload['logos'];reference=payload.get('reference')
    if not reference:return [dict(e) for e in entries],''
    ref,refcomp,reflayers=prepare(reference)
    mapping=[]
    for index,e in enumerate(entries):
        slot=e.get('referenceSlot',index)
        if slot is None:slot=index
        if not isinstance(slot,int) or isinstance(slot,bool) or not 0<=slot<len(reflayers):raise ValueError('首图 Logo 对应关系已变化，请重新批量贴标')
        mapping.append({'id':e['id'],'referenceSlot':slot,'initial':{k:e[k]*1000/(original.width if k in ('x','w') else original.height) for k in ('x','y','w','h')}|{'rotation':e.get('rotation',0)}})
    parts=[{'text':ALIGN_PROMPT},api_image(ref,1500),api_image(refcomp,1500),api_image(original,1500),api_image(composite,1500),{'text':json.dumps(mapping)}]
    body={'contents':[{'role':'user','parts':parts}],'generationConfig':{'temperature':.1,'maxOutputTokens':4096,'responseMimeType':'application/json','responseSchema':ALIGN_SCHEMA}}
    plan=request_json(body,key,model,transport);records=plan.get('placements')
    if not isinstance(records,list) or len(records)!=len(entries):raise ValueError('AI 位置校准未覆盖全部 Logo，保留原贴标')
    byid={e.get('id'):e for e in records if isinstance(e,dict)}
    if len(byid)!=len(entries) or set(byid)!={e['id'] for e in entries}:raise ValueError('AI 位置校准标识不一致，保留原贴标')
    adjusted=[];warnings=[]
    for e in entries:
        record=byid[e['id']];confidence=finite(record.get('confidence'),0,1,'AI 位置置信度');out=dict(e)
        if confidence>=.75:
            x=finite(record.get('x'),-200,1200,'AI 横坐标')*original.width/1000;y=finite(record.get('y'),-200,1200,'AI 纵坐标')*original.height/1000
            w=finite(record.get('w'),1,2000,'AI 宽度')*original.width/1000;h=finite(record.get('h'),1,2000,'AI 高度')*original.height/1000;angle=finite(record.get('rotation'),-180,180,'AI 旋转')
            center=math.hypot(x+w/2-e['x']-e['w']/2,y+h/2-e['y']-e['h']/2);turn=abs((angle-e.get('rotation',0)+180)%360-180)
            if center>max(min(original.size)*.12,max(e['w'],e['h'])) or turn>40 or not .6<=w/e['w']<=1.5 or abs((h/w)/(e['h']/e['w'])-1)>.08:
                warnings.append(e['id']+'：校准幅度异常，保留原位置')
            else:out.update(x=x,y=y,w=w,h=w*e['h']/e['w'],rotation=angle)
        else:warnings.append(e['id']+'：对应位置不确定，保留原位置')
        adjusted.append(out)
    return adjusted,str(plan.get('summary',''))[:800]+('；'+'；'.join(warnings) if warnings else '')


def process(payload, key=None, model=None, transport=None):
    if payload.get('assistantMode','local')!='local':raise ValueError('制作主图仅提供手动贴标与本地光影融合')
    report('stage','本地光影融合，不调用 API',phase='本地合成')
    original,_,layers=prepare(payload)
    return compose(original,layers,local_plan(layers))


def process_overlay(payload,key,model,transport=None):
    """Refine only a transparent replacement layer, never regenerate the source page."""
    report('stage','读取本页叠加图层',phase='准备当前详情页');check_cancel()
    original=decode_image(payload.get('original')); overlay=decode_image(payload.get('overlay'))
    if overlay.size!=original.size: raise ValueError('本页图层尺寸不一致')
    import cv2
    alpha=np.array(overlay.getchannel('A')); mask=(alpha>0).astype(np.uint8)
    if not mask.any(): raise ValueError('请先在当前页套图')
    if mask.mean()>.55: raise ValueError('替换层面积过大，请缩小到 Logo 附近')
    kernel=max(3,round(min(original.size)*.012))
    grouped=cv2.dilate(mask,np.ones((kernel,kernel),np.uint8))
    count,labels,stats,_=cv2.connectedComponentsWithStats(grouped,8)
    layers=[]
    for i in range(1,count):
        x,y,w,h,area=stats[i]
        if area<9: continue
        margin=max(12,round(max(w,h)*.35))
        box=(max(0,x-margin),max(0,y-margin),min(original.width,x+w+margin),min(original.height,y+h+margin))
        tile=overlay.crop(box)
        # Isolate this group where expanded crop intersects another nearby logo.
        own=Image.fromarray(((labels[y:y+h,x:x+w]==i)*255).astype('uint8'))
        full=Image.new('L',original.size); full.paste(own,(x,y))
        from PIL import ImageChops
        tile.putalpha(ImageChops.multiply(tile.getchannel('A'),full.crop(box)))
        layers.append({'id':'region_'+str(i),'name':'本页标识 '+str(i),'box':box,'tile':tile})
    if not layers or len(layers)>12: raise ValueError('本页图层过于分散，请人工调整后再处理')
    composite=original.copy(); composite.alpha_composite(overlay)
    report('stage','检查当前详情页的遮挡与光影',phase='详情页 AI 检查')
    return compose(original,layers,request_plan(original,composite,layers,key,model,transport))
