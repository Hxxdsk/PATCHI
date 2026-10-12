# -*- coding: utf-8 -*-
"""Transfer isolated print pixels across garment poses; never transfer cloth.
Automatic fallback uses front-jacket landmarks (collar, zipper and quilting).
Explicit logo boxes + three point correspondence also support other garments.
"""
from dataclasses import dataclass
import cv2
import numpy as np
import patchi_samephoto as engine


@dataclass
class Garment:
    image: np.ndarray
    mask: np.ndarray
    box: tuple
    axis: float
    collar: float
    seams: np.ndarray
    zip_contrast: float
    median: np.ndarray
    scale: float


def foreground(image):
    """Infer only flat edge-connected white/black backgrounds, or use alpha."""
    transparent=np.mean(image[:,:,3]<250)>.005
    if transparent:
        mask=(image[:,:,3]>180).astype(np.uint8)*255
    else:
        h,w=image.shape[:2];n=max(2,min(h,w)//30)
        corners=np.concatenate([image[:n,:n,:3].reshape(-1,3),image[:n,-n:,:3].reshape(-1,3),image[-n:,:n,:3].reshape(-1,3),image[-n:,-n:,:3].reshape(-1,3)])
        bg=np.median(corners,axis=0);noise=np.percentile(np.max(np.abs(corners-bg),axis=1),85)
        if noise>18 or not (np.max(bg)<40 or np.min(bg)>210):
            raise ValueError('跨姿势请使用透明图，或干净白底／黑底服装图')
        distance=np.max(np.abs(image[:,:,:3].astype(np.float32)-bg),axis=2)
        mask=(distance>max(9.,noise*2+4)).astype(np.uint8)*255
    kernel=max(3,min(9,round(max(image.shape[:2])*.004)))
    if kernel%2==0:kernel+=1
    mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((kernel,kernel),np.uint8))
    if not transparent:
        # Flat-colored pixels enclosed by the garment are lining/print details,
        # not the outer background (e.g. black letters inside a colored badge).
        count,labels=cv2.connectedComponents((mask==0).astype(np.uint8),8)
        edges=np.unique(np.r_[labels[0],labels[-1],labels[:,0],labels[:,-1]])
        enclosed=(labels>0)&~np.isin(labels,edges)
        mask[enclosed]=255
    return mask


def cut_flat_background(image):
    out=image.copy();out[:,:,3]=np.minimum(out[:,:,3],foreground(image));return out


def _peaks(values,minimum_gap,threshold):
    candidates=[i for i in range(2,len(values)-2) if values[i]>threshold and values[i]>=values[i-1] and values[i]>values[i+1]]
    selected=[]
    for i in sorted(candidates,key=lambda x:-values[x]):
        if all(abs(i-j)>=minimum_gap for j in selected):selected.append(i)
        if len(selected)>=5:break
    return np.array(sorted(selected),np.float32)


def profiles(image,hint=None):
    small,scale=engine._work_size(image,750);mask=foreground(small)
    count,labels,stats,_=cv2.connectedComponentsWithStats(mask,8);result=[]
    for index in range(1,count):
        x,y,w,h,area=map(int,stats[index])
        if h<45 or w<25 or area<small.shape[0]*small.shape[1]*.015 or not .85<h/w<2.7:continue
        if hint is not None:
            hx,hy=np.asarray(hint)*scale
            if not(x-8<=hx<=x+w+8 and y-8<=hy<=y+h+8):continue
        local=(labels[y:y+h,x:x+w]==index).astype(np.uint8)*255
        gray=engine._gray(small[y:y+h,x:x+w])
        yy0,yy1=int(h*.28),int(h*.85)
        column=np.mean(gray[yy0:yy1].astype(np.float32),axis=0)
        smooth=cv2.GaussianBlur(column[None,:],(0,0),max(1,w*.025)).ravel()
        contrast=smooth-column
        col=np.arange(w);center=np.exp(-((col-w*.5)/(w*.19))**2)
        score=contrast*center;left,right=int(w*.32),int(w*.68)
        axis=left+int(np.argmax(score[left:right]));zip_quality=float(contrast[axis])
        white=(gray>175)&(local>0)
        rows=np.flatnonzero(np.sum(white[:int(h*.38)],axis=1)>max(3,w*.12))
        collar=float(rows[-1]) if len(rows) else h*.21
        collar=float(np.clip(collar,h*.10,h*.32))
        # Quantized seam heights remain useful even on 230 px product photos.
        strips=[]
        for lo,hi in ((axis-int(w*.30),axis-int(w*.08)),(axis+int(w*.08),axis+int(w*.30))):
            lo,hi=max(0,lo),min(w,hi)
            if hi>lo:strips.append(gray[:,lo:hi].astype(np.float32))
        cloth=np.concatenate(strips,axis=1)
        brightness=cv2.GaussianBlur(np.mean(cloth,axis=1)[:,None],(0,0),max(.7,h*.003)).ravel()
        rise=np.diff(brightness)
        begin,end=int(collar+h*.025),min(int(h*.38),int(collar+h*.16))
        if end>begin:
            position=begin+int(np.argmax(rise[begin:end]))
            collar=float(position+1) if rise[position]>1.2 else min(h*.34,collar+h*.07)
        signal=np.mean(np.abs(np.diff(cloth,axis=0)),axis=1)
        signal=cv2.GaussianBlur(signal[:,None],(0,0),max(.7,h*.003)).ravel()
        lo,hi=int(collar+h*.08),int(h*.92)
        values=signal[lo:hi];median=float(np.median(values));mad=float(np.median(np.abs(values-median)))
        seams=_peaks(values,max(5,int(h*.09)),max(1.1,median+1.7*mad))+lo
        pixels=small[y:y+h,x:x+w,:3][(local>0)&(gray<175)]
        med=np.median(pixels,axis=0) if len(pixels) else np.zeros(3)
        result.append(Garment(small,labels==index,(x,y,w,h),x+float(axis),y+collar,seams+y,zip_quality,med,scale))
    return sorted(result,key=lambda p:-p.box[2]*p.box[3])[:8]


def _cloth_plane(image,box):
    x,y,w,h=box;ih,iw=image.shape[:2];pad=max(4,round(max(w,h)*.22))
    x0,y0=max(0,x-pad),max(0,y-pad);x1,y1=min(iw,x+w+pad),min(ih,y+h+pad)
    rgb=image[y0:y1,x0:x1,:3].astype(np.float32)
    yy,xx=np.indices(rgb.shape[:2]);xx=xx+x0;yy=yy+y0
    ring=((xx<x)|(xx>=x+w)|(yy<y)|(yy>=y+h))&(image[y0:y1,x0:x1,3]>180)
    if ring.sum()<12:raise ValueError('Logo 框边缘缺少可见衣料，请缩小框选范围')
    X=np.c_[np.ones(ring.sum()),(xx[ring]-x)/max(w,1),(yy[ring]-y)/max(h,1)];Y=rgb[ring]
    keep=np.ones(len(Y),bool)
    for _ in range(3):
        fit=np.linalg.lstsq(X[keep],Y[keep],rcond=None)[0];error=np.max(np.abs(Y-X@fit),axis=1)
        med=np.median(error);mad=np.median(np.abs(error-med));keep=error<max(12.,med+3*mad)
        if keep.sum()<12:break
    yy,xx=np.indices((h,w));plane=np.c_[np.ones(w*h),xx.ravel()/max(w,1),yy.ravel()/max(h,1)]@fit
    noise=float(np.median(error[keep])) if keep.any() else 3.
    return plane.reshape(h,w,3).clip(0,255).astype(np.float32),noise


def extract_logos(image,boxes=None):
    if not boxes:
        small,scale=engine._work_size(image,1000);mask=foreground(small)
        ys,xs=np.where(mask>0)
        if len(xs)<100:raise ValueError('未找到服装轮廓')
        hsv=cv2.cvtColor(small[:,:,:3],cv2.COLOR_BGR2HSV)
        interior=cv2.erode(mask,np.ones((7,7),np.uint8))>0
        seed=((hsv[:,:,1]>75)&(hsv[:,:,2]>70)&interior).astype(np.uint8)
        grouped=cv2.dilate(seed,np.ones((5,5),np.uint8))
        count,labels,stats,_=cv2.connectedComponentsWithStats(grouped,8)
        boxes=[];gw,gh=xs.max()-xs.min()+1,ys.max()-ys.min()+1
        for i in range(1,count):
            x,y,w,h,area=map(int,stats[i]);values=hsv[:,:,0][(labels==i)&(seed>0)]
            if len(values)<8 or area>mask.sum()/255*.025 or w>gw*.32 or h>gh*.22 or not .35<w/h<6:continue
            bins=np.bincount(values.astype(int)*6//180,minlength=6)
            if np.count_nonzero(bins>max(3,len(values)*.07))<2:continue
            if not ys.min()+gh*.14<y+h/2<ys.min()+gh*.78:continue
            pad=max(2,round(min(w,h)*.08))
            boxes.append([max(0,(x-pad)/scale),max(0,(y-pad)/scale),(w+pad*2)/scale,(h+pad*2)/scale])
        boxes=boxes[:8]
    ih,iw=image.shape[:2];logos=[]
    for box in boxes[:12]:
        if not isinstance(box,(list,tuple)) or len(box)!=4:raise ValueError('Logo 框参数错误')
        if not np.isfinite(box).all():raise ValueError('Logo 框参数错误')
        x,y,w,h=map(float,box);x0,y0=max(0,int(x)),max(0,int(y));x1,y1=min(iw,int(np.ceil(x+w))),min(ih,int(np.ceil(y+h)))
        w,h=x1-x0,y1-y0
        if w<3 or h<3 or w*h>iw*ih*.12:raise ValueError('请紧贴 Logo 框选，避免框入大片衣料')
        plane,noise=_cloth_plane(image,(x0,y0,w,h));part=image[y0:y1,x0:x1].copy()
        difference=np.max(np.abs(part[:,:,:3].astype(np.float32)-plane),axis=2)
        threshold=max(12.,noise*4)
        changed=(difference>threshold).astype(np.uint8)
        n,labels,stats,_=cv2.connectedComponentsWithStats(changed,8)
        keep=np.zeros(n,bool)
        for i in range(1,n):
            if stats[i,cv2.CC_STAT_AREA]>=max(2,w*h*.0004):keep[i]=True
        alpha=cv2.GaussianBlur(keep[labels].astype(np.float32),(0,0),.45)
        alpha[difference<threshold*.65]=0
        alpha[difference>threshold*2]=1
        alpha=np.minimum(alpha,part[:,:,3]/255.)
        if np.count_nonzero(alpha>.5)<5:continue
        # Unmix the source cloth at antialiased edges before placing on new cloth.
        rgb=(part[:,:,:3].astype(np.float32)-plane*(1-alpha[:,:,None]))/np.maximum(alpha[:,:,None],1e-5)
        part[:,:,:3]=np.clip(np.rint(rgb),0,255).astype(np.uint8);part[:,:,3]=np.clip(np.rint(alpha*255),0,255).astype(np.uint8)
        logos.append({'image':part,'box':[x0,y0,w,h]})
    if not logos:raise ValueError('未能独立提取 Logo；请用「标记素材 Logo」紧贴图案框选，单色图案尤其需要框选')
    return logos


def _seam_pairs(a,b):
    ay0,by0=a.collar,b.collar;ay1=a.box[1]+a.box[3]*.96;by1=b.box[1]+b.box[3]*.96
    aa=(a.seams-ay0)/max(1,ay1-ay0);bb=(b.seams-by0)/max(1,by1-by0)
    pairs=[];used=set()
    for i,value in enumerate(aa):
        if not len(bb):break
        j=int(np.argmin(np.abs(bb-value)))
        if j not in used and abs(bb[j]-value)<.08:pairs.append((float(a.seams[i]),float(b.seams[j])));used.add(j)
    return pairs


def _shape_overlap(a,b):
    x,y,w,h=a.box;xx,yy,ww,hh=b.box
    av=cv2.resize(a.mask[y:y+h,x:x+w].astype(np.uint8),(100,140),interpolation=cv2.INTER_NEAREST)>0
    bv=cv2.resize(b.mask[yy:yy+hh,xx:xx+ww].astype(np.uint8),(100,140),interpolation=cv2.INTER_NEAREST)>0
    return np.count_nonzero(av&bv)/max(1,np.count_nonzero(av|bv))


def _verify(a,b):
    if min(a.zip_contrast,b.zip_contrast)<3.5:raise ValueError('未自动确认拉链位置，请使用「跨姿势校准」')
    pairs=_seam_pairs(a,b)
    if len(pairs)<3:raise ValueError('衣身分区对应不足，请使用「跨姿势校准」')
    overlap=_shape_overlap(a,b)
    if overlap<.58 or np.max(np.abs(a.median-b.median))>65:raise ValueError('服装结构或颜色差异过大，未自动覆盖；请校准对应位置')
    return pairs,100*(.45*overlap+.35*min(1,len(pairs)/4)+.20)


def _row_edges(profile,y):
    row=int(np.clip(round(y),profile.box[1],profile.box[1]+profile.box[3]-1))
    values=np.flatnonzero(profile.mask[row])
    if not len(values):return float(profile.box[0]),float(profile.box[0]+profile.box[2])
    return float(values[0]),float(values[-1])


def _mapped_point(point,a,b,pairs):
    x,y=np.asarray(point)*a.scale
    source_y=np.array([a.box[1],a.collar]+[p[0] for p in pairs]+[a.box[1]+a.box[3]-1]);target_y=np.array([b.box[1],b.collar]+[p[1] for p in pairs]+[b.box[1]+b.box[3]-1])
    order=np.argsort(source_y);mapped_y=float(np.interp(y,source_y[order],target_y[order]))
    left,right=_row_edges(a,y);tl,tr=_row_edges(b,mapped_y)
    if x>=a.axis:mapped_x=b.axis+(x-a.axis)/max(1,right-a.axis)*(tr-b.axis)
    else:mapped_x=b.axis-(a.axis-x)/max(1,a.axis-left)*(b.axis-tl)
    return [mapped_x/b.scale,mapped_y/b.scale]


def preview(source_data,boxes=None):
    image=engine._decode(source_data);logos=extract_logos(image,boxes)
    x0=min(l['box'][0] for l in logos);y0=min(l['box'][1] for l in logos)
    x1=max(l['box'][0]+l['box'][2] for l in logos);y1=max(l['box'][1]+l['box'][3] for l in logos)
    layer=np.zeros((y1-y0,x1-x0,4),np.uint8)
    for logo in logos:
        x,y,w,h=logo['box'];layer[y-y0:y-y0+h,x-x0:x-x0+w]=logo['image']
    return {'ok':True,'overlay':engine._encode(layer),'boxes':[logo['box'] for logo in logos]}


def analyze_images(source,target,hint=None,source_boxes=None,manual_points=None,protections=None,prepared_logos=None,source_profiles=None,target_profiles=None,registration_transform=None):
    logos=prepared_logos if prepared_logos is not None else extract_logos(source,source_boxes)
    H=None
    if manual_points is not None or registration_transform is not None:
        H=engine._manual_transform(source,target,manual_points) if manual_points is not None else np.asarray(registration_transform,dtype=np.float64)
        engine._geometry(source,target,H);targets=[(None,None,[],0.)]
    else:
        sources=profiles(source) if source_profiles is None else source_profiles
        candidates=profiles(target,hint) if target_profiles is None else target_profiles
        if hint is not None and target_profiles is not None:
            candidates=[p for p in candidates if p.box[0]-8<=hint[0]*p.scale<=p.box[0]+p.box[2]+8 and p.box[1]-8<=hint[1]*p.scale<=p.box[1]+p.box[3]+8]
        targets=[]
        if not sources or not candidates:raise ValueError('未找到独立衣服；请使用「跨姿势校准」选择对应点')
        a=sources[0]
        for b in candidates:
            try:pairs,score=_verify(a,b)
            except ValueError:continue
            targets.append((a,b,pairs,score))
        if not targets:raise ValueError('跨姿势结构未自动确认；标记素材 Logo 后，使用「跨姿势校准」选择三个对应点')
    h,w=target.shape[:2];output=np.zeros_like(target);regions=[];bounds=[]
    for a,b,pairs,score in targets[:2]:
        for logo in logos:
            x,y,lw,lh=logo['box']
            if H is not None:M=H@np.array([[1,0,x],[0,1,y],[0,0,1.]])
            else:
                # Four print corners follow the garment's panel and center axis.
                original=np.float32([[x,y],[x+lw,y],[x+lw,y+lh],[x,y+lh]])
                mapped=np.float32([_mapped_point(p,a,b,pairs) for p in original])
                if cv2.contourArea(mapped)<3:continue
                M=cv2.getPerspectiveTransform(np.float32([[0,0],[lw,0],[lw,lh],[0,lh]]),mapped)
            layer=engine._warp_output(logo['image'],M,(w,h))
            if b is not None:
                support=cv2.resize(b.mask.astype(np.uint8),(w,h),interpolation=cv2.INTER_NEAREST)>0
                layer[:,:,3][~support]=0
                fy,fx=np.where(layer[:,:,3]>0)
                foreign=np.max(np.abs(target[fy,fx,:3].astype(np.float32)-b.median),axis=1)>100
                layer[fy[foreign],fx[foreign],3]=0
            layer[:,:,3][engine._protect_mask(w,h,protections)>0]=0
            visible=layer[:,:,3]>0
            if visible.sum()<3:continue
            yy,xx=np.where(visible)
            # Separate transparent print layers merge using premultiplied alpha.
            alpha=layer[:,:,3:4].astype(np.float32)/255;old=output[:,:,3:4].astype(np.float32)/255
            combined=alpha+old*(1-alpha)
            color=(layer[:,:,:3].astype(np.float32)*alpha+output[:,:,:3].astype(np.float32)*old*(1-alpha))/np.maximum(combined,1e-6)
            output[:,:,:3]=np.clip(np.rint(color),0,255).astype(np.uint8);output[:,:,3]=np.rint(combined[:,:,0]*255).astype(np.uint8)
            regions.append({'x':int(xx.min()),'y':int(yy.min()),'w':int(xx.max()-xx.min()+1),'h':int(yy.max()-yy.min()+1),'pixels':int(visible.sum()),'logo':'独立 Logo · 跨姿势'})
        if b is not None:
            x,y,bw,bh=b.box;bounds.append([[x/b.scale,y/b.scale],[(x+bw)/b.scale,y/b.scale],[(x+bw)/b.scale,(y+bh)/b.scale],[x/b.scale,(y+bh)/b.scale]])
    pixels=int(np.count_nonzero(output[:,:,3]))
    if not pixels:raise ValueError('Logo 映射后不可见，请检查校准点和保护画笔')
    warnings=['跨姿势：仅迁移独立 Logo，请核对印花位置、大小和遮挡；可以撤销并重新校准。']
    return {'ok':True,'engineVersion':'3.5-pose-logo','overlay':engine._encode(output),'score':round(max(t[3] for t in targets),1),
            'method':'三点跨姿势校准' if manual_points is not None else ('同源定位 · 独立 Logo' if registration_transform is not None else '领口／拉链／衣身分区跨姿势定位'),
            'pixels':pixels,'regions':regions,'bounds':bounds[0] if bounds else [],'sourceSize':[source.shape[1],source.shape[0]],
            'targetSize':[w,h],'transform':None if H is None else H.tolist(),'warnings':warnings,'reviewRequired':True,'sourceLogoBoxes':[l['box'] for l in logos],
            'note':'只叠加独立 Logo 像素，目标衣料和背景不参与覆盖。'}


def handle(payload):
    return analyze_images(engine._decode(payload.get('source')),engine._decode(payload.get('target')),payload.get('hint'),
                          payload.get('sourceLogoBoxes'),payload.get('manualPoints'),payload.get('protections'))


def anatomy_first(source,target,hint=None,protections=None):
    """Cheap low-res product-photo fallback before same-photo template search.
    Identical silhouettes/panel positions continue down the exact-pixel path.
    """
    if max(target.shape[:2])>650 or max(source.shape[:2])<max(target.shape[:2])*2:return None
    try:
        logos=extract_logos(source);sources=profiles(source);targets=profiles(target,hint)
        if not sources:return None
        a=sources[0];confirmed=[]
        for b in targets:
            try:pairs,_=_verify(a,b)
            except ValueError:continue
            delta=max(abs((sy-a.collar)/(a.box[1]+a.box[3]*.96-a.collar)-(ty-b.collar)/(b.box[1]+b.box[3]*.96-b.collar)) for sy,ty in pairs)
            if _shape_overlap(a,b)<.965 or delta>.018:confirmed.append(b)
        if not confirmed:return None
        return analyze_images(source,target,hint=hint,protections=protections,prepared_logos=logos,source_profiles=sources,target_profiles=confirmed)
    except ValueError:return None
