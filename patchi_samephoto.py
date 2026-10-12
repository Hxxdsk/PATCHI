# -*- coding: utf-8 -*-
"""帕琪工坊 v3.4 · 衣料特征定位与 Logo 轮廓迁移（本地，不使用生成式 AI）。
只返回透明的可叠加区域；不能确认对齐时抛错，绝不涂改目标底图。
"""
import base64
import io
import math
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_PIXELS = 32_000_000
MAX_INPUT_BYTES = 26 * 1024 * 1024


def _decode(value):
    if not isinstance(value, str) or ',' not in value or not value.startswith('data:image/'):
        raise ValueError('图片数据格式错误')
    raw = base64.b64decode(value.split(',', 1)[1], validate=True)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('单张图超过 26MB')
    with Image.open(io.BytesIO(raw)) as im:
        if im.width * im.height > MAX_PIXELS:
            raise ValueError('单张图超过 3200 万像素')
        im = ImageOps.exif_transpose(im)
        rgba = np.asarray(im.convert('RGBA')).copy()
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)


def _encode(arr):
    ok, encoded = cv2.imencode('.png', arr, [cv2.IMWRITE_PNG_COMPRESSION, 7])
    if not ok:
        raise ValueError('无法编码透明输出 PNG')
    return 'data:image/png;base64,' + base64.b64encode(encoded.tobytes()).decode('ascii')


def _work_size(image, longest=1850):
    h, w = image.shape[:2]
    scale = min(1.0, longest / max(h, w))
    if scale < 1:
        return cv2.resize(image, (max(1, round(w*scale)), max(1, round(h*scale))), interpolation=cv2.INTER_AREA), scale
    return image, 1.0


def _gray(bgra):
    return cv2.cvtColor(cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR), cv2.COLOR_BGR2GRAY)


def _image_gradient(gray):
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


@dataclass
class Candidate:
    transform: np.ndarray
    method: str
    matches: int = 0
    inliers: int = 0
    source_points: object = None
    target_points: object = None



def _template(source, target, hint=None):
    """无纹理衣物兜底：旋转不大的源图在目标中做多尺度定位。"""
    src, ss = _work_size(source, 1000)
    dst, ts = _work_size(target, 1000)
    a = _gray(src)
    b = _gray(dst)
    alpha = src[:, :, 3]
    ys, xs = np.where(alpha > 245)
    if len(xs) < 250:
        return []
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max()+1, ys.max()+1
    part = a[y0:y1, x0:x1]
    mask = alpha[y0:y1, x0:x1]
    if min(part.shape) < 18:
        return []
    # 对完整原照片也适用；仅做整体平移/缩放，不修改服装结构。
    candidates = []
    ratios = np.geomspace(.22, 1.9, 25)
    for k in ratios:
        ww, hh = max(1, round(part.shape[1]*k)), max(1, round(part.shape[0]*k))
        if ww > b.shape[1] or hh > b.shape[0] or min(ww, hh) < 35:
            continue
        pa = cv2.resize(part, (ww, hh), interpolation=cv2.INTER_AREA if k<1 else cv2.INTER_LINEAR)
        ma = cv2.resize(mask, (ww, hh), interpolation=cv2.INTER_NEAREST)
        if int((ma>245).sum()) < 200:
            continue
        try:
            # Normalized cross correlation with mask handles transparent photo backgrounds.
            corr = cv2.matchTemplate(b, pa, cv2.TM_CCORR_NORMED, mask=ma)
            corr = np.nan_to_num(corr, nan=0., posinf=0., neginf=0.)
            _, v, _, (tx, ty) = cv2.minMaxLoc(corr)
            if v < .70:
                continue
            scale_full = k*ss/ts
            H = np.array([[scale_full,0,(tx-k*x0)/ts], [0,scale_full,(ty-k*y0)/ts], [0,0,1]], dtype=np.float64)
            candidates.append((v, Candidate(H, '模板', int(v*100), int(v*100))))
        except cv2.error:
            pass
    return [c for _, c in sorted(candidates, key=lambda t: -t[0])[:5]]



def _protect_mask(w, h, protections):
    excluded = np.zeros((h, w), np.uint8)
    for stroke in (protections or [])[:120]:
        if not isinstance(stroke, dict): continue
        radius = int(np.clip(round(float(stroke.get('size', 25))/2), 1, 300))
        pts = stroke.get('points', [])
        if not isinstance(pts, list): continue
        prev = None
        for pt in pts[:2500]:
            if not isinstance(pt, list) or len(pt) != 2: continue
            current = (int(np.clip(pt[0], 0, w-1)), int(np.clip(pt[1], 0, h-1)))
            cv2.circle(excluded, current, radius, 255, -1)
            if prev is not None: cv2.line(excluded, prev, current, 255, radius*2)
            prev = current
    return excluded



ENGINE_VERSION = '3.4-balanced-detail'


def _occlusion_mask(warped, target):
    """Protect large foreign regions entering the garment silhouette.

    Interior differences are the newly added logos and must remain in the copy.
    Use original target pixels to infer occlusion, never an earlier replacement.
    """
    support = warped[:, :, 3] > 200
    count = int(support.sum())
    excluded = np.zeros(support.shape, np.uint8)
    if count < 250:
        return excluded
    a = cv2.GaussianBlur(warped[:, :, :3].astype(np.float32), (0, 0), 1.0)
    b = cv2.GaussianBlur(target[:, :, :3].astype(np.float32), (0, 0), 1.0)
    difference = np.abs(a - b).mean(axis=2)
    changed = ((difference > 38) & support).astype(np.uint8)
    changed = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    changed[~support] = 0
    inside = cv2.erode(support.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    boundary = support & ~inside
    # The picture frame is not a garment boundary.
    boundary[:2] = boundary[-2:] = False
    boundary[:, :2] = boundary[:, -2:] = False
    n, labels, stats, _ = cv2.connectedComponentsWithStats(changed, 8)
    for index in range(1, n):
        if stats[index, cv2.CC_STAT_AREA] < max(100, count * .018):
            continue
        region = labels == index
        if np.count_nonzero(region & boundary) < 8:
            continue
        excluded[region] = 255
    if excluded.any():
        # Include the mixed pixels along the foreground edge, avoiding a light halo.
        excluded = cv2.dilate(excluded, np.ones((3, 3), np.uint8))
        excluded[warped[:, :, 3] == 0] = 0
    return excluded


def _support(source):
    """Registration support only; NEVER used as the output alpha."""
    alpha = source[:, :, 3]
    mask = alpha > 245
    if not np.any(alpha < 250):
        # Ignore white studio background for scoring (not for cutting pixels).
        mask &= np.min(source[:, :, :3], axis=2) < 242
    return mask.astype(np.uint8)*255


def _features(source, target):
    src, ss = _work_size(source, 1800)
    dst, ts = _work_size(target, 1800)
    sm = cv2.erode(_support(src), np.ones((3,3), np.uint8))
    result = []
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8,8))
    for enhanced in (False, True):
        a, b = _gray(src), _gray(dst)
        if enhanced:
            a, b = clahe.apply(a), clahe.apply(b)
        detector = cv2.SIFT_create(nfeatures=6500, contrastThreshold=.008, edgeThreshold=16)
        ka, da = detector.detectAndCompute(a, sm)
        kb, db = detector.detectAndCompute(b, (dst[:,:,3]>200).astype(np.uint8)*255)
        if da is None or db is None or min(len(da),len(db))<10:
            continue
        # Target -> source permits multiple instances of one photo in a layout.
        pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(db, da, k=2)
        good = [m for pair in pairs if len(pair)==2 for m,n in [pair] if m.distance<.74*n.distance]
        if len(good)<10: continue
        aa = np.float32([ka[m.trainIdx].pt for m in good])
        bb = np.float32([kb[m.queryIdx].pt for m in good])
        for _ in range(4):
            if len(aa)<10: break
            H, flags = cv2.findHomography(aa,bb,cv2.RANSAC,2.6,maxIters=4500,confidence=.998)
            if H is None or flags is None or flags.sum()<10: break
            goodmask = flags.ravel().astype(bool)
            # Prefer similarity transform: typical page placement is scale/rotation/translation.
            A, af = cv2.estimateAffinePartial2D(aa[goodmask],bb[goodmask],method=cv2.RANSAC,ransacReprojThreshold=1.8)
            models = [(H, '透视', int(goodmask.sum()))]
            F, ff = cv2.estimateAffine2D(aa[goodmask], bb[goodmask], method=cv2.RANSAC, ransacReprojThreshold=2.2)
            if F is not None and ff is not None and ff.sum() >= 12:
                models.append((np.vstack((F,[0,0,1])), '轻微姿态', int(ff.sum())))
            if A is not None and af is not None and af.sum()>=10:
                models.insert(0,(np.vstack((A,[0,0,1])), '等比',int(af.sum())))
            for M, kind, n in models:
                M = np.diag([1/ts,1/ts,1.]) @ M @ np.diag([ss,ss,1.])
                M = M/M[2,2]
                pa, pb = aa/ss, bb/ts
                predicted = cv2.perspectiveTransform(pa[:,None,:], M).reshape(-1,2)
                nearby = np.linalg.norm(predicted-pb,axis=1) < max(4/ts, min(dst.shape[:2])*.015/ts)
                result.append(Candidate(M, ('暗部增强' if enhanced else '纹理')+kind,len(aa),n, pa[nearby],pb[nearby]))
            aa,bb=aa[~goodmask],bb[~goodmask]
    return result


def _warp(source, H, size):
    """Premultiplied alpha resampling avoids black fringes from transparent PNG RGB."""
    if source.shape[1::-1] == tuple(size) and np.allclose(H,np.eye(3),atol=1e-10):
        return source.copy()
    a=source[:,:,3].astype(np.float32)/255
    pre=np.dstack((source[:,:,:3].astype(np.float32)*a[:,:,None],a))
    warped=cv2.warpPerspective(pre,H,tuple(size),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
    wa=warped[:,:,3]
    out=np.zeros((*wa.shape,4),np.uint8)
    out[:,:,:3]=np.clip(np.rint(warped[:,:,:3]/np.maximum(wa[:,:,None],1e-7)),0,255).astype(np.uint8)
    out[:,:,3]=np.clip(np.rint(wa*255),0,255).astype(np.uint8)
    return out


def _geometry(source,target,H):
    h,w=target.shape[:2]; sh,sw=source.shape[:2]
    if H.shape!=(3,3) or not np.isfinite(H).all() or abs(np.linalg.det(H))<1e-10:
        raise ValueError('配准矩阵无效')
    corners=np.float32([[0,0],[sw,0],[sw,sh],[0,sh]])
    den=H[2,0]*corners[:,0]+H[2,1]*corners[:,1]+H[2,2]
    if np.min(den)*np.max(den)<=0: raise ValueError('变换穿过无穷远')
    pts=cv2.perspectiveTransform(corners[:,None,:],H).reshape(-1,2)
    area=cv2.contourArea(pts,oriented=True)
    if not cv2.isContourConvex(pts) or area<300 or area>w*h*12:
        raise ValueError('变换范围异常，请重新选择对应点')
    return pts


def _color_model(layer, target, valid=None):
    """Robust per-channel exposure/color fit; logo outliers do not set the fit."""
    if valid is None:
        valid = (layer[:,:,3]>245) & (target[:,:,3]>245)
        valid = cv2.erode(valid.astype(np.uint8), np.ones((5,5),np.uint8))>0
    flat = np.flatnonzero(valid)
    if len(flat)<100: return np.ones(3), np.zeros(3)
    flat = flat[::max(1,len(flat)//40000)]
    a=layer[:,:,:3].reshape(-1,3)[flat].astype(np.float64)
    b=target[:,:,:3].reshape(-1,3)[flat].astype(np.float64)
    gains=np.ones(3); offsets=np.median(a-b,axis=0)
    for c in range(3):
        x,y=b[:,c],a[:,c]; keep=np.ones(len(x),bool)
        for _ in range(4):
            if np.std(x[keep])>5:
                slope,intercept=np.linalg.lstsq(np.c_[x[keep],np.ones(keep.sum())],y[keep],rcond=None)[0]
                gains[c]=np.clip(slope,.65,1.55)
                offsets[c]=np.median(y[keep]-gains[c]*x[keep])
            else:
                offsets[c]=np.median(y[keep]-x[keep])
            residual=y-(x*gains[c]+offsets[c])
            med=np.median(residual); mad=np.median(np.abs(residual-med))
            keep=np.abs(residual-med)<max(6.,3*1.4826*mad)
            if keep.sum()<100: break
    return gains, np.clip(offsets,-100,100)


def _comparison_layer(layer, target):
    gain,offset=_color_model(layer,target)
    normalized=layer.copy()
    normalized[:,:,:3]=np.clip(np.rint((layer[:,:,:3].astype(np.float32)-offset)/gain),0,255).astype(np.uint8)
    return normalized,gain,offset


def _point_evidence(source, target, candidate):
    a,b=candidate.source_points,candidate.target_points
    if a is None or len(a)<12: return {'strong':False,'coverage':0.,'unique':0}
    # Deduplicate SIFT orientations; repeated descriptors at one button do not
    # count as landmarks distributed over the garment.
    cell=max(5., min(target.shape[:2])/80)
    _,idx=np.unique(np.floor(b/cell).astype(int),axis=0,return_index=True)
    aa,bb=a[idx],b[idx]
    area=cv2.contourArea(cv2.convexHull(aa.astype(np.float32))) if len(aa)>=3 else 0
    support=max(1,np.count_nonzero(_support(source)))
    coverage=min(1.,area/support)
    ratio=candidate.inliers/max(1,candidate.matches)
    distributed=(len(aa)>=18 and coverage>=.08) or (len(aa)>=12 and coverage>=.14 and ratio>=.4)
    strong=distributed and candidate.inliers>=14 and ratio>=.25
    return {'strong':strong,'coverage':coverage,'unique':len(aa)}


def _deformation(candidate, target_shape):
    """Small smooth residual adjustment from nearby verified SIFT landmarks.

    No unconstrained flow on logos: displacement comes only from cloth matches.
    Bound amplitude and strain, and require spatial support before enabling it.
    """
    a,b=candidate.source_points,candidate.target_points
    if a is None or len(a)<24:return None
    h,w=target_shape[:2]
    projected=cv2.perspectiveTransform(a[:,None,:].astype(np.float32),candidate.transform).reshape(-1,2)
    residual=b-projected
    extent=np.ptp(b,axis=0); span=max(40.,float(min(extent)))
    limit=min(24.,max(3.,span*.018))
    valid=np.linalg.norm(residual,axis=1)<limit
    b,residual=b[valid],residual[valid]
    cell=max(5.,span/40)
    _,idx=np.unique(np.floor(b/cell).astype(int),axis=0,return_index=True)
    b,residual=b[idx],residual[idx]
    if len(b)<24 or np.percentile(np.linalg.norm(residual,axis=1),85)<.65:return None
    # Fixed small grid avoids memory proportional to landmark count * pixels.
    gh,gw=max(3,min(60,round(h/24))),max(3,min(60,round(w/24)))
    yy,xx=np.meshgrid(np.linspace(0,h-1,gh),np.linspace(0,w-1,gw),indexing='ij')
    grid=np.c_[xx.ravel(),yy.ravel()]; field=np.zeros((len(grid),2),np.float32)
    radius=max(20.,span*.12)
    for start in range(0,len(grid),96):
        points=grid[start:start+96]
        distances=np.sum((points[:,None,:]-b[None,:,:])**2,axis=2)
        ids=np.argsort(distances,axis=1)[:,:min(12,len(b))]
        ds=np.take_along_axis(distances,ids,axis=1)
        rr=residual[ids]
        # Suppress a single mismatched descriptor through neighborhood consensus.
        med=np.median(rr,axis=1)
        good=np.linalg.norm(rr-med[:,None,:],axis=2)<max(1.8,limit*.45)
        weights=np.exp(-ds/(2*radius**2))*good
        nearest=np.sqrt(ds[:,0]);confidence=np.clip((radius*2.5-nearest)/radius,0,1)
        field[start:start+len(points)]=(rr*weights[:,:,None]).sum(axis=1)/np.maximum(weights.sum(axis=1)[:,None],1e-8)*confidence[:,None]
    field=field.reshape(gh,gw,2)
    dx=np.max(np.abs(np.diff(field,axis=1)))/max(1,(w-1)/(gw-1))
    dy=np.max(np.abs(np.diff(field,axis=0)))/max(1,(h-1)/(gh-1))
    if max(dx,dy)>.08 or np.max(np.linalg.norm(field,axis=2))>limit:return None
    field=cv2.resize(field,(w,h),interpolation=cv2.INTER_LINEAR)
    # Only keep a warp if it actually explains the measured residuals.
    ix=np.clip(np.rint(b[:,0]).astype(int),0,w-1);iy=np.clip(np.rint(b[:,1]).astype(int),0,h-1)
    before=np.mean(np.linalg.norm(residual,axis=1)); after=np.mean(np.linalg.norm(residual-field[iy,ix],axis=1))
    if after>before*.82:return None
    return field


def _evaluate(source,target,candidate,hint=None):
    H=candidate.transform
    try: pts=_geometry(source,target,H)
    except ValueError: return None
    evidence=_point_evidence(source,target,candidate)
    view,f=_work_size(target,1000)
    registration,sf=_work_size(source,1800)
    M=np.diag([f,f,1.])@H@np.diag([1/sf,1/sf,1.])
    warped=_warp(registration,M,view.shape[1::-1])
    small_candidate=Candidate(np.diag([f,f,1.])@H,candidate.method,candidate.matches,candidate.inliers,
                              candidate.source_points, None if candidate.target_points is None else candidate.target_points*f)
    field=_deformation(small_candidate,view.shape) if evidence['strong'] else None
    if field is not None:
        yy,xx=np.indices(view.shape[:2],dtype=np.float32)
        warped=cv2.remap(warped,xx-field[:,:,0],yy-field[:,:,1],cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
    support=_support(warped)
    valid=cv2.erode(((support>250)&(warped[:,:,3]>250)).astype(np.uint8),np.ones((5,5),np.uint8)).astype(bool)
    n=int(valid.sum())
    if n<250:return None
    normalized,gain,offset=_comparison_layer(warped,view)
    occluded=_occlusion_mask(normalized,view)>0
    visible=valid&~occluded;visible_count=int(visible.sum());partial=visible_count<n*.95
    if visible_count<max(250,n*.22):return None
    if partial and candidate.inliers<14:return None
    if hint is not None:
        hx,hy=map(float,hint)
        if not(0<=hx<target.shape[1] and 0<=hy<target.shape[0]):return None
        near=cv2.dilate((support>0).astype(np.uint8),np.ones((21,21),np.uint8))
        ix,iy=min(near.shape[1]-1,int(hx*f)),min(near.shape[0]-1,int(hy*f))
        if not near[iy,ix] or occluded[iy,ix]:return None
    diff=np.abs(normalized[:,:,:3].astype(np.float32)-view[:,:,:3]).mean(axis=2)
    p50=float(np.median(diff[visible]));clean=float(np.mean(diff[visible]<25))
    if p50>(34 if evidence['strong'] else 25) or clean<(.48 if evidence['strong'] else .58):return None
    a=_gray(normalized).astype(np.float32);b=_gray(view).astype(np.float32)
    a-=cv2.GaussianBlur(a,(0,0),2);b-=cv2.GaussianBlur(b,(0,0),2)
    reliable=visible&(diff<35)
    av,bv=a[reliable],b[reliable];denom=float(np.linalg.norm(av)*np.linalg.norm(bv))
    corr=float(np.dot(av,bv)/denom) if denom>1 else 0.
    if corr<(.48 if partial else (.38 if evidence['strong'] else .5)):return None
    # Color tolerance is conditional on distributed landmarks and actual texture.
    color_shift=float(max(np.max(np.abs(offset)),np.max(np.abs(gain-1))*80))
    if color_shift>28 and not evidence['strong']:return None
    score=100*(.40*clean+.25*max(0,1-p50/40)+.25*max(0,corr)+.10*min(1,evidence['coverage']/.25))
    if not evidence['strong'] and candidate.method!='原尺寸' and score<78:return None
    return {'score':float(score),'corr':corr,'p50':p50,'bounds':pts.tolist(),'clean':clean,
            'visibleFraction':visible_count/n,'colorShift':color_shift,'localAdjusted':field is not None,
            'landmarks':evidence['unique'],'coverage':round(float(evidence['coverage']),3)}


def _manual_transform(source,target,points):
    if not isinstance(points,dict):raise ValueError('需要三组对应点')
    a=np.asarray(points.get('source'),dtype=np.float64)
    b=np.asarray(points.get('target'),dtype=np.float64)
    if a.shape!=(3,2) or b.shape!=(3,2) or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError('请在两张图按相同顺序选择三个对应点')
    for p,im in ((a,source),(b,target)):
        h,w=im.shape[:2]
        if np.any(p<0) or np.any(p[:,0]>=w) or np.any(p[:,1]>=h):raise ValueError('对应点超出图片')
        u,v=p[1]-p[0],p[2]-p[0]
        area=abs(u[0]*v[1]-u[1]*v[0])/2
        if area<max(20,w*h*.0003):raise ValueError('三个点太近或接近一条直线，请选择分散的纽扣、肩部、下摆等点')
    H=np.vstack((cv2.getAffineTransform(a.astype(np.float32),b.astype(np.float32)),[0,0,1]))
    _geometry(source,target,H)
    return H



def _output_scale(H, shape):
    h, w = shape[:2]
    p = np.float32([[[w/2, h/2]], [[w/2+1, h/2]], [[w/2, h/2+1]]])
    q = cv2.perspectiveTransform(p, H.astype(np.float64)).reshape(3, 2)
    return max(1e-4, float(min(np.linalg.norm(q[1]-q[0]), np.linalg.norm(q[2]-q[0]))))

def _warp_output(source, H, size, displacement=None):
    """Final pixels use original input, with antialiasing and premultiplied alpha.

    Registration deliberately keeps its existing sampling path.
    """
    if displacement is None and source.shape[1::-1] == tuple(size) and np.allclose(H, np.eye(3), atol=1e-10):
        return source.copy()
    alpha = source[:, :, 3].astype(np.float32) / 255
    pre = np.dstack((source[:, :, :3].astype(np.float32)*alpha[:, :, None], alpha))
    scale = _output_scale(H, source.shape)
    M = H.copy()
    if scale < .7:
        # Reduce once with an area filter, instead of blurring the entire source
        # and then shrinking it again. Pixel-center compensation is essential.
        sh, sw = source.shape[:2]
        nw, nh = max(1, round(sw*scale)), max(1, round(sh*scale))
        sx, sy = nw/sw, nh/sh
        pre = cv2.resize(pre, (nw, nh), interpolation=cv2.INTER_AREA)
        centers = np.array([[1/sx, 0, .5/sx-.5],
                            [0, 1/sy, .5/sy-.5], [0, 0, 1.]])
        M = H @ centers
    if displacement is None:
        warped = cv2.warpPerspective(pre, M, tuple(size), flags=cv2.INTER_LANCZOS4,
                                     borderMode=cv2.BORDER_CONSTANT)
    else:
        yy,xx=np.indices((size[1],size[0]),dtype=np.float32)
        xx-=displacement[:,:,0]; yy-=displacement[:,:,1]
        inv=np.linalg.inv(M);den=inv[2,0]*xx+inv[2,1]*yy+inv[2,2]
        mx=((inv[0,0]*xx+inv[0,1]*yy+inv[0,2])/den).astype(np.float32)
        my=((inv[1,0]*xx+inv[1,1]*yy+inv[1,2])/den).astype(np.float32)
        warped=cv2.remap(pre,mx,my,cv2.INTER_LANCZOS4,borderMode=cv2.BORDER_CONSTANT)
    a = np.clip(warped[:, :, 3], 0, 1)
    out = np.zeros((*a.shape, 4), np.uint8)
    out[:, :, :3] = np.clip(np.rint(warped[:, :, :3]/np.maximum(a[:, :, None], 1e-7)), 0, 255).astype(np.uint8)
    out[:, :, 3] = np.clip(np.rint(a*255), 0, 255).astype(np.uint8)
    return out

def _logo_changes(layer, target, excluded, scale):
    """Follow changed pixels at target resolution, preserving holes and cloth.

    Detection may blur comparison copies; final RGB never uses these copies.
    Hysteresis keeps faint edges attached to strong logo pixels. Components are
    copied by their actual contours, never by their rectangular bounding boxes.
    """
    mask = np.zeros(layer.shape[:2], np.uint8)
    support = layer[:, :, 3] > 245
    rows = np.flatnonzero(support.any(axis=1))
    cols = np.flatnonzero(support.any(axis=0))
    if not len(rows) or not len(cols): return mask
    y0, y1, x0, x1 = rows[0], rows[-1]+1, cols[0], cols[-1]+1
    part = layer[y0:y1, x0:x1]
    dest = target[y0:y1, x0:x1]
    hidden = excluded[y0:y1, x0:x1]
    valid = (part[:, :, 3] > 245) & (hidden == 0)
    valid = cv2.erode(valid.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    if np.count_nonzero(valid) < 50: return mask
    a = cv2.GaussianBlur(part[:, :, :3].astype(np.float32), (0, 0), .65)
    b = cv2.GaussianBlur(dest[:, :, :3].astype(np.float32), (0, 0), .65)
    gain, offset = _color_model(part, dest, valid)
    a = (a-offset)/gain
    delta = a-b
    offset = np.median(delta[valid], axis=0)
    # Subpixel registration and JPEG ringing must not turn seams/buttons into logos.
    kernel = np.ones((3, 3), np.uint8)
    aligned = a-offset
    low, high = cv2.erode(b, kernel), cv2.dilate(b, kernel)
    outside = np.maximum(np.maximum(low-aligned, aligned-high), 0)
    diff = np.max(np.maximum(outside, np.abs(delta-offset)*.20), axis=2)
    values = diff[valid]
    median = float(np.median(values))
    mad = float(np.median(np.abs(values-median)))
    threshold = max(6., median+4.5*1.4826*mad)
    seeds = (diff > threshold) & valid
    low_threshold = max(2.5, median+2*1.4826*mad)
    weak = ((diff > low_threshold) & valid).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(weak, 8)
    seed_count = np.bincount(labels[seeds], minlength=count)
    keep = np.zeros(count, bool)
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] >= 3 and seed_count[i] >= 2:
            keep[i] = True
    contour = keep[labels]
    # A subpixel soft edge prevents pasted outlines; logo interiors stay opaque.
    local = cv2.GaussianBlur(contour.astype(np.float32), (0, 0), .55)
    local[seeds & contour] = 1.
    local[(diff <= low_threshold*.6) | ~valid] = 0.
    mask[y0:y1, x0:x1] = np.clip(np.rint(local*255), 0, 255).astype(np.uint8)
    mask[excluded > 0] = 0
    return mask

def _copy_layer(source,target,H,roi=None,protections=None,candidate=None):
    h,w=target.shape[:2]
    evidence=_point_evidence(source,target,candidate) if candidate is not None else {'strong':False}
    field=_deformation(candidate,target.shape) if evidence['strong'] else None
    layer=_warp_output(source,H,(w,h),field)
    comparison,_,_=_comparison_layer(layer,target)
    excluded = _occlusion_mask(comparison, target)
    changes = _logo_changes(layer, target, excluded, _output_scale(H, source.shape))
    layer[:, :, 3] = np.minimum(layer[:, :, 3], changes)
    if roi is not None:
        x,y,rw,rh=map(float,roi)
        mask=np.zeros((h,w),np.uint8)
        x0,y0=max(0,int(np.floor(x))),max(0,int(np.floor(y)))
        x1,y1=min(w,int(np.ceil(x+rw))),min(h,int(np.ceil(y+rh)))
        if x1<=x0 or y1<=y0:raise ValueError('框选区域不在详情图内')
        mask[y0:y1,x0:x1]=255
        layer[:,:,3]=np.minimum(layer[:,:,3],mask)
    layer[:,:,3][_protect_mask(w,h,protections)>0]=0
    visible=layer[:,:,3]>0
    if visible.sum()<10:return np.zeros_like(layer), []
    y,x=np.where(visible)
    region={'x':int(x.min()),'y':int(y.min()),'w':int(x.max()-x.min()+1),'h':int(y.max()-y.min()+1),'pixels':int(visible.sum()),'logo':'Logo 变化区域'}
    return layer,[region]


def analyze(source_data,target_data,hint=None,logo_box=None,protections=None,logos=None,manual_points=None):
    source,target=_decode(source_data),_decode(target_data)
    if hint is None and logo_box is not None:
        hint=[logo_box[0]+logo_box[2]/2,logo_box[1]+logo_box[3]/2]
    # No color segmentation and no forced Logo catalog completeness gate.
    # Opaque photos need an explicit crop, avoiding accidental background/text replacement.
    transparent=float(np.mean(source[:,:,3]<250))>.005
    if not transparent and logo_box is None:
        from patchi_pose import cut_flat_background
        source=cut_flat_background(source)
        transparent=True
    if manual_points is None and logo_box is None:
        from patchi_pose import anatomy_first
        independent=anatomy_first(source,target,hint,protections)
        if independent is not None:return independent
    warnings=[]
    if manual_points is not None:
        H=_manual_transform(source,target,manual_points)
        quality={'score':0.,'corr':0.,'p50':0.,'bounds':_geometry(source,target,H).tolist()}
        candidate=Candidate(H,'三点人工校准',3,3)
        warnings.append('人工校准：未通过自动同源验证，请在预览中核对位置、颜色与边缘。')
    else:
        candidates=[]
        if source.shape[:2]==target.shape[:2]:candidates.append(Candidate(np.eye(3),'原尺寸',0,0))
        candidates+=_features(source,target)
        scored=[]
        def add_candidates(items):
            for c in items:
                v=_evaluate(source,target,c,hint)
                if v is not None and v['score']>=70:
                    scored.append((v['score'] + (.25 if '等比' in c.method else 0),c,v))
        add_candidates(candidates)
        if not scored:add_candidates(_template(source,target,hint))
        if not scored:raise ValueError('未找到可靠匹配。请选择同一张原照片的已贴标素材，并点击未被遮住的衣料位置。')
        scored.sort(key=lambda x:-x[0]);_,candidate,quality=scored[0]
        # A second distinct placement means this page has repeated same-photo instances.
        distinct=[]
        for _,c,v in scored[1:]:
            if np.mean(np.linalg.norm(np.asarray(v['bounds'])-np.asarray(quality['bounds']),axis=1))>35:
                distinct.append(v)
        if distinct:warnings.append('本页有多个匹配位置；其他位置请再次点击套图。')
        if quality['score']<80:warnings.append('匹配需复核：请查看原图/效果对比，确认后保留。')
    if quality.get('colorShift',0)>20:warnings.append('已适配衣料色差；请核对 Logo 边缘和颜色。')
    if quality.get('localAdjusted'):warnings.append('已按衣料特征校准轻微姿势变化；请核对 Logo 位置。')
    layer,regions=_copy_layer(source,target,candidate.transform,logo_box,protections,candidate)
    if not transparent:warnings.append('不透明素材：仅复制您框选的范围；框内原有文案/前景需要用保护画笔保留。')
    return {'ok':True,'engineVersion':ENGINE_VERSION,'overlay':_encode(layer),
            'score':round(quality['score'],1),'method':candidate.method+' · 原图保真套图',
            'matchCount':candidate.matches,'inlierCount':candidate.inliers,'regions':regions,'missingLogos':[],
            'pixels':int(np.count_nonzero(layer[:,:,3])),'sourceSize':[source.shape[1],source.shape[0]],
            'targetSize':[target.shape[1],target.shape[0]],'bounds':quality['bounds'],
            'transform':candidate.transform.tolist(),'warnings':warnings,
            'reviewRequired':bool(warnings),'alignment':{k:quality.get(k) for k in ('landmarks','coverage','colorShift','localAdjusted')},'note':'保留详情图原始像素，只叠加可见 Logo 变化区域。'}


def handle_payload(payload):
    if not isinstance(payload,dict):raise ValueError('请求格式错误')
    hint=payload.get('hint');roi=payload.get('logoBox')
    if hint is not None and (not isinstance(hint,list) or len(hint)!=2 or not np.isfinite(hint).all()):raise ValueError('点击参数错误')
    if roi is not None and (not isinstance(roi,list) or len(roi)!=4 or not np.isfinite(roi).all() or roi[2]<=1 or roi[3]<=1):raise ValueError('框选参数错误')
    if payload.get('mode')=='pose' or payload.get('posePoints') is not None:
        from patchi_pose import handle as pose_handle
        body=dict(payload)
        if payload.get('posePoints') is not None:body['manualPoints']=payload['posePoints']
        return pose_handle(body)
    try:
        result=analyze(payload.get('source'),payload.get('target'),hint=hint,logo_box=roi,
                       protections=payload.get('protections'),manual_points=payload.get('manualPoints'))
        if payload.get('sourceLogoBoxes') and result.get('transform') is None:
            from patchi_pose import handle as pose_handle
            return pose_handle(payload)
        if payload.get('sourceLogoBoxes'):
            from patchi_pose import analyze_images
            isolated=analyze_images(_decode(payload.get('source')),_decode(payload.get('target')),source_boxes=payload['sourceLogoBoxes'],
                                    protections=payload.get('protections'),registration_transform=result['transform'])
            isolated['score']=result['score'];return isolated
        return result
    except ValueError as original:
        from patchi_pose import handle as pose_handle
        try:return pose_handle(payload)
        except ValueError as cross_pose:
            raise ValueError(str(cross_pose)+'；同源匹配：'+str(original)) from None
