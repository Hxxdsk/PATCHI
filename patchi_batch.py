# -*- coding: utf-8 -*-
"""Fast, bounded batch registration. Upload each image once; reuse its features.
Uncertain pairs are skipped for the normal click-to-match path. Final pixels
always come from original files through patchi_samephoto's contour compositor.
"""
import base64
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import patchi_samephoto as engine

PAIR_SECONDS = 1.8  # A soft recognition budget; final original-size rendering is separate.
MAX_INSTANCES = 2
CACHE_IMAGES = 24
SESSION_TTL = 20 * 60


@dataclass
class Prepared:
    path: Path
    thumbnail_path: Path
    source: bool
    shape: tuple
    to_original: np.ndarray
    points: np.ndarray
    descriptors: object


class Session:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='patchi_batch_')
        self.directory = Path(self.temporary.name)
        self.records = {}
        self.cache = OrderedDict()
        self.lock = threading.RLock()
        self.used = time.monotonic()

    def thumbnail(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        image = cv2.imread(str(self.records[key].thumbnail_path), cv2.IMREAD_UNCHANGED)
        if image is None: raise ValueError('批量缓存已失效，请重新批量套图')
        self.cache[key] = image
        while len(self.cache) > CACHE_IMAGES: self.cache.popitem(last=False)
        return image


_sessions = {}
_sessions_lock = threading.RLock()


def _get(token):
    with _sessions_lock:
        session = _sessions.get(token)
        if session is None: raise ValueError('批量任务已结束或过期，请重新开始')
        session.used = time.monotonic()
        return session


def start():
    now = time.monotonic()
    with _sessions_lock:
        expired = [key for key, session in _sessions.items() if now-session.used > SESSION_TTL]
        for key in expired: close(key)
        if len(_sessions) >= 3: raise ValueError('已有批量任务，请先完成或停止')
        token = secrets.token_hex(20)
        _sessions[token] = Session()
    return {'ok': True, 'session': token, 'pairSeconds': PAIR_SECONDS, 'maxInstances': MAX_INSTANCES}


def close(token):
    with _sessions_lock: session = _sessions.pop(token, None)
    if session is not None:
        with session.lock:
            session.records.clear(); session.cache.clear()
            session.temporary.cleanup()
    return {'ok': True}


def prepare(payload):
    session = _get(payload.get('session'))
    source = payload.get('kind') == 'source'
    if payload.get('kind') not in ('source', 'target'): raise ValueError('图片类型错误')
    with session.lock:
        if len(session.records) >= 500: raise ValueError('本次图片数量超过 500 张')
        image = engine._decode(payload.get('image'))
        if source and np.mean(image[:,:,3] < 250) <= .005:
            return {'ok': True, 'usable': False, 'reason': '素材没有透明背景，请手动检查'}
        small, _ = engine._work_size(image, 1000)
        h, w = image.shape[:2]; sh, sw = small.shape[:2]
        sx, sy = sw/w, sh/h
        to_original = np.array([[1/sx,0,.5/sx-.5], [0,1/sy,.5/sy-.5], [0,0,1.]])
        mask = engine._support(small) if source else (small[:,:,3]>200).astype(np.uint8)*255
        mask = cv2.erode(mask, np.ones((3,3),np.uint8))
        gray = cv2.createCLAHE(clipLimit=2.5,tileGridSize=(8,8)).apply(engine._gray(small))
        detector = cv2.SIFT_create(nfeatures=2000,contrastThreshold=.012,edgeThreshold=16)
        points, descriptors = detector.detectAndCompute(gray, mask)
        xy = np.array([p.pt for p in points],dtype=np.float32).reshape(-1,2)
        if descriptors is None or len(xy)<12:
            return {'ok': True, 'usable': False, 'reason': '可靠纹理不足，留待手动点击匹配'}
        key = secrets.token_hex(12)
        path = session.directory/(key+'.image')
        # Store actual uploaded bytes, not an encoded/downsized replacement.
        path.write_bytes(base64.b64decode(payload['image'].split(',',1)[1],validate=True))
        thumb = session.directory/(key+'.png')
        if not cv2.imwrite(str(thumb),small,[cv2.IMWRITE_PNG_COMPRESSION,1]): raise ValueError('无法写入批量缓存')
        session.records[key] = Prepared(path,thumb,source,(h,w),to_original,xy,descriptors)
        session.cache[key] = small
        while len(session.cache)>CACHE_IMAGES: session.cache.popitem(last=False)
        return {'ok': True, 'usable': True, 'imageId': key, 'features': len(xy)}


def _candidates(source, target, deadline):
    pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(target.descriptors,source.descriptors,k=2)
    good = [m for pair in pairs if len(pair)==2 for m,n in [pair] if m.distance<.73*n.distance]
    if len(good)<14: return []
    aa=np.float32([source.points[m.trainIdx] for m in good])
    bb=np.float32([target.points[m.queryIdx] for m in good])
    results=[]
    for _ in range(MAX_INSTANCES):
        if len(aa)<14 or time.monotonic()>deadline: break
        H,flags=cv2.findHomography(aa,bb,cv2.RANSAC,2.5,maxIters=1200,confidence=.995)
        if H is None or flags is None or int(flags.sum())<14: break
        inliers=flags.ravel().astype(bool)
        A,af=cv2.estimateAffinePartial2D(aa[inliers],bb[inliers],method=cv2.RANSAC,ransacReprojThreshold=2.,maxIters=1000)
        models=[]
        if A is not None and af is not None and af.sum()>=14:
            models.append((np.vstack((A,[0,0,1.])), '快速等比',int(af.sum())))
        # One perspective alternative handles small pose/layout differences.
        models.append((H, '快速姿态',int(inliers.sum())))
        group=[]
        for M,method,count in models:
            predicted=cv2.perspectiveTransform(aa[:,None,:],M).reshape(-1,2)
            nearby=np.linalg.norm(predicted-bb,axis=1)<8
            group.append(engine.Candidate(M/M[2,2],method,len(aa),count,aa[nearby],bb[nearby]))
        results.append(group)
        aa,bb=aa[~inliers],bb[~inliers]
    return results


def _read_original(record):
    # Reuse Pillow's EXIF and format handling from the normal engine.
    data='data:image/png;base64,'+base64.b64encode(record.path.read_bytes()).decode('ascii')
    return engine._decode(data)


def match(payload):
    session=_get(payload.get('session'))
    with session.lock:
        source_id,target_id=payload.get('sourceId'),payload.get('targetId')
        a,b=session.records.get(source_id),session.records.get(target_id)
        if a is None or b is None or not a.source or b.source: raise ValueError('批量图片引用已失效')
        started=time.monotonic();deadline=started+PAIR_SECONDS
        src,dst=session.thumbnail(source_id),session.thumbnail(target_id)
        groups=_candidates(a,b,deadline)
        chosen=[]
        for group in groups:
            best=None
            for candidate in group:
                if time.monotonic()>deadline: break
                evidence=engine._point_evidence(src,dst,candidate)
                if not evidence['strong']: continue
                quality=engine._evaluate(src,dst,candidate)
                if quality is None or quality['score']<78 or quality['corr']<.45: continue
                if best is None or quality['score']>best[1]['score']: best=(candidate,quality)
                # A strong similarity fit needs no more expensive alternatives.
                if quality['score']>=88 and '等比' in candidate.method: break
            if best is not None: chosen.append(best)
        recognition=time.monotonic()-started
        if not chosen:
            return {'ok':True,'results':[],'reason':'未找到快速可靠匹配，留待手动补充','recognitionMs':round(recognition*1000)}
        source,target=_read_original(a),_read_original(b)
        results=[]
        for small_candidate,quality in chosen:
            H=b.to_original@small_candidate.transform@np.linalg.inv(a.to_original)
            sp=cv2.perspectiveTransform(small_candidate.source_points[:,None,:],a.to_original).reshape(-1,2)
            tp=cv2.perspectiveTransform(small_candidate.target_points[:,None,:],b.to_original).reshape(-1,2)
            candidate=engine.Candidate(H,small_candidate.method,small_candidate.matches,small_candidate.inliers,sp,tp)
            bounds=engine._geometry(source,target,H).tolist()
            layer,regions=engine._copy_layer(source,target,H,protections=payload.get('protections'),candidate=candidate)
            pixels=int(np.count_nonzero(layer[:,:,3]))
            if pixels<10: continue
            warnings=['快速批量结果：请检查位置；未识别位置可手动点击补充。']
            if quality.get('colorShift',0)>20: warnings.append('已适配衣料色差，请核对 Logo 边缘。')
            if quality.get('localAdjusted'): warnings.append('已校准轻微姿势变化，请核对位置。')
            results.append({'ok':True,'engineVersion':engine.ENGINE_VERSION,'overlay':engine._encode(layer),
                            'score':round(quality['score'],1),'method':candidate.method+' · 批量套图',
                            'pixels':pixels,'regions':regions,'bounds':bounds,'warnings':warnings,'reviewRequired':True,
                            'sourceSize':[source.shape[1],source.shape[0]],'targetSize':[target.shape[1],target.shape[0]]})
        return {'ok':True,'results':results,'reason':'没有可见 Logo 变化' if not results else '',
                'recognitionMs':round(recognition*1000),'totalMs':round((time.monotonic()-started)*1000)}


def handle(action,payload):
    if not isinstance(payload,dict): raise ValueError('批量请求格式错误')
    if action=='start': return start()
    if action=='prepare': return prepare(payload)
    if action=='match': return match(payload)
    if action=='close': return close(payload.get('session'))
    raise ValueError('未知批量操作')
