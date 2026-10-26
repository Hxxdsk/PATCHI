"""PATCHI original pixel sticker compositor; no diffusion or brand re-drawing."""
import math
import cv2
import numpy as np
from PIL import Image


def number(value,low,high):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:raise ValueError('Invalid sticker geometry')
    return float(value)


def align(reference,target,entry):
    """Conservative local SIFT alignment on luminance; fail closed to user's placement."""
    rw,rh=reference.size;tw,th=target.size
    e=dict(entry);notes=[]
    if abs((rw/rh)/(tw/th)-1)>.18:return e,['画幅比例差异较大，保留手动位置']
    factor=min(1,900/rw,900/rh);size=(max(1,round(rw*factor)),max(1,round(rh*factor)))
    def gray(im):return cv2.createCLAHE(2,(8,8)).apply(cv2.cvtColor(np.asarray(im.convert('RGB').resize(size)),cv2.COLOR_RGB2GRAY))
    a,b=gray(reference),gray(target);x,y,w,h=[entry[k] for k in ('x','y','w','h')]
    # Target placement is already normalized to its own canvas. Reference coordinates differ.
    x*=rw/tw;y*=rh/th;w*=rw/tw;h*=rh/th
    padding=max(w,h)*1.8;mask=np.zeros(a.shape,np.uint8)
    l,t=max(0,round((x-padding)*factor)),max(0,round((y-padding)*factor));r,bot=min(size[0],round((x+w+padding)*factor)),min(size[1],round((y+h+padding)*factor));mask[t:bot,l:r]=255
    sift=cv2.SIFT_create(nfeatures=1200);ka,da=sift.detectAndCompute(a,mask);kb,db=sift.detectAndCompute(b,mask)
    if da is None or db is None or len(ka)<8 or len(kb)<8:return e,['局部纹理不足，保留手动位置']
    matches=[pair[0] for pair in cv2.BFMatcher().knnMatch(da,db,k=2) if len(pair)==2 and pair[0].distance<.72*pair[1].distance]
    if len(matches)<8:return e,['颜色图对应特征不足，保留手动位置']
    pa=np.float32([ka[m.queryIdx].pt for m in matches]);pb=np.float32([kb[m.trainIdx].pt for m in matches]);M,inliers=cv2.estimateAffinePartial2D(pa,pb,method=cv2.RANSAC,ransacReprojThreshold=2.5)
    if M is None or inliers.sum()<8 or inliers.mean()<.55:return e,['位置校准置信度不足，保留手动位置']
    scale=math.hypot(M[0,0],M[1,0]);angle=math.degrees(math.atan2(M[1,0],M[0,0]));cx,cy=(x+w/2)*factor,(y+h/2)*factor;nc=M@np.array([cx,cy,1.])
    if not .85<=scale<=1.18 or abs(angle)>12 or np.linalg.norm(nc-[cx,cy])>min(size)*.065:return e,['检测到过大位移/旋转，保留手动位置']
    # Inliers must cover a real area rather than a single zipper point.
    support=pa[inliers.ravel().astype(bool)]
    if np.ptp(support[:,0])<max(8,w*factor*.25) or np.ptp(support[:,1])<max(8,h*factor*.25):return e,['校准特征过于集中，保留手动位置']
    e.update(w=entry['w']*scale,h=entry['h']*scale,rotation=((entry.get('rotation',0)+angle+180)%360)-180)
    e.update(x=nc[0]/factor*tw/rw-e['w']/2,y=nc[1]/factor*th/rh-e['h']/2)
    return e,['已按首图局部纹理小幅校准']


def garment_mask(image):
    a=np.asarray(image);alpha=a[:,:,3]/255.
    if alpha.min()<.99:return alpha.astype(np.float32)
    # Only remove an edge-connected near-white background; white clothes remain connected.
    white=((a[:,:,:3].min(2)>242)&(a[:,:,:3].max(2)-a[:,:,:3].min(2)<12)).astype(np.uint8)
    count,labels,_,_=cv2.connectedComponentsWithStats(white,8)
    edge=np.unique(np.concatenate((labels[0],labels[-1],labels[:,0],labels[:,-1])));edge=edge[edge!=0]
    if not len(edge):return np.ones(alpha.shape,np.float32)
    out=(~np.isin(labels,edge)).astype(np.float32)
    # White garments indistinguishable from the background should not disappear.
    if out.mean()<.08:return np.ones(alpha.shape,np.float32)
    return out


def process(original,reference,entries,read_logo,predict,options,check=lambda:None,progress=lambda *a:None):
    original=original.convert('RGBA');reference=reference.convert('RGBA');W,H=original.size
    if W*H>32_000_000 or not 1<=len(entries)<=8:raise ValueError('Image/Logo limit exceeded')
    overlay=Image.new('RGBA',original.size);notes=[];placements=[]
    progress('检查衣物边缘');surface=garment_mask(original) if options.get('edge',True) else np.ones((H,W),np.float32)
    for index,item in enumerate(entries):
        check();entry=dict(item)
        for k,lo,hi in [('x',-W,2*W),('y',-H,2*H),('w',1,2*W),('h',1,2*H),('rotation',-180,180)]:entry[k]=number(entry.get(k,0),lo,hi)
        if entry['w']*entry['h']>16_000_000:raise ValueError('Logo output exceeds 16MP')
        if options.get('align') and item.get('linked',False):
            progress('校准第 %d 个 Logo'%(index+1));entry,n=align(reference,original,entry);notes+=n
        if entry['w']*entry['h']>16_000_000:raise ValueError('Aligned Logo output exceeds 16MP')
        logo=read_logo(item['image']).convert('RGBA')
        patch=logo.convert('RGBa').resize((round(entry['w']),round(entry['h'])),Image.Resampling.LANCZOS).rotate(-entry['rotation'],Image.Resampling.BICUBIC,expand=True).convert('RGBA')
        px=round(entry['x']+entry['w']/2-patch.width/2);py=round(entry['y']+entry['h']/2-patch.height/2)
        pad=max(24,round(max(patch.size)*.8));box=(max(0,px-pad),max(0,py-pad),min(W,px+patch.width+pad),min(H,py+patch.height+pad))
        if box[0]>=box[2] or box[1]>=box[3]:raise ValueError('Logo is outside image')
        crop=original.crop(box);tile=Image.new('RGBA',crop.size);tile.alpha_composite(patch,(px-box[0],py-box[1]));a=np.asarray(tile).astype(np.float32)/255
        check();progress('识别第 %d 个 Logo 的褶皱和遮挡'%(index+1))
        role=item.get('role','chest');occluders='ribbed collar, folded collar, collar band' if role=='neck' else 'drawstring, rope, zipper pull, strap'
        target='inside back neck lining fabric' if role=='neck' else 'flat surface of clothing fabric'
        depth,front,cloth=predict(crop,occluders,target,check)
        check();height,width=a.shape[:2]
        depth=cv2.resize(np.asarray(depth,np.float32),(width,height));front=cv2.resize(np.asarray(front,np.float32),(width,height));cloth=cv2.resize(np.asarray(cloth,np.float32),(width,height))
        if not all(np.isfinite(m).all() for m in (depth,front,cloth)):raise ValueError('Nonfinite model maps')
        # Depth gradients produce small continuous displacement; warp RGB premultiplied by alpha.
        smooth=cv2.GaussianBlur(depth,(0,0),max(1,min(width,height)*.012));lo,hi=np.percentile(smooth,[5,95]);smooth=((smooth-lo)/max(1e-5,hi-lo)).astype(np.float32)
        gx=cv2.Sobel(smooth,cv2.CV_32F,1,0,ksize=3);gy=cv2.Sobel(smooth,cv2.CV_32F,0,1,ksize=3)
        amplitude=number(options.get('warp',.5),0,1)*min(patch.size)*.025
        dx=np.clip(gx*amplitude,-amplitude,amplitude);dy=np.clip(gy*amplitude,-amplitude,amplitude)
        xx,yy=np.meshgrid(np.arange(width,dtype=np.float32),np.arange(height,dtype=np.float32));premul=a.copy();premul[:,:,:3]*=premul[:,:,3:4]
        a=cv2.remap(premul,xx-dx,yy-dy,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT);a[:,:,:3]=np.divide(a[:,:,:3],a[:,:,3:4],out=np.zeros_like(a[:,:,:3]),where=a[:,:,3:4]>1e-5)
        support=a[:,:,3]>.1
        if not support.any():raise ValueError('Logo has no visible pixels')
        occlude=(front>.58)&(front>cloth+.12)
        amount=float(occlude[support].mean())
        if amount>.8:
            occlude[:]=False;notes.append('第 %d 标遮挡范围过大，保留 Logo，请人工复核'%(index+1))
        elif amount==0:notes.append('第 %d 标未检出可靠遮挡，领口/细抽绳请复核'%(index+1))
        else:notes.append('第 %d 标自动擦除 %.1f%% 前景遮挡'%(index+1,amount*100))
        # Hard occlusion inside objects; subpixel feather only at the actual boundary.
        keep=1-cv2.GaussianBlur(occlude.astype(np.float32),(0,0),.45)
        a[:,:,3]*=keep*surface[box[1]:box[3],box[0]:box[2]]
        rgb=np.asarray(crop.convert('RGB'),np.float32)/255;luma=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
        fine=cv2.GaussianBlur(luma,(0,0),1.2);broad=cv2.GaussianBlur(luma,(0,0),max(4,min(patch.size)*.18))
        relief=np.clip((fine+.04)/(broad+.04),.68,1.22)
        light=number(options.get('light',.55),0,1);a[:,:,:3]*=(1+(relief-1)*light)[:,:,None]
        # Contact darkening follows the local height gradient without changing Logo opacity.
        shade=np.clip(1-(np.maximum(gx,0)+np.maximum(gy,0))*.12*light,.85,1.)
        a[:,:,:3]*=shade[:,:,None]
        tile=Image.fromarray(np.uint8(np.clip(a,0,1)*255));overlay.alpha_composite(tile,box[:2]);placements.append({k:entry[k] for k in ('id','x','y','w','h','rotation')})
    check();result=original.copy();result.alpha_composite(overlay)
    return result,overlay,{'notes':notes,'placements':placements,'requiresReview':True}
