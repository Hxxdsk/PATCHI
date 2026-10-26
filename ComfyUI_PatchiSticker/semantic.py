"""Lazy CPU-only depth + text segmentation. Models stay on the external ComfyUI host."""
import os
from pathlib import Path
import threading
import numpy as np
from PIL import Image

MODEL_IDS={'depth':'depth-anything/Depth-Anything-V2-Small-hf','segment':'CIDAS/clipseg-rd64-refined'}
MODEL_ROOT=Path(os.environ.get('PATCHI_MODEL_DIR',str(Path(__file__).resolve().parent/'models')))
_lock=threading.Lock();_models=None

def readiness():
    return {name:(MODEL_ROOT/name/'config.json').is_file() and any((MODEL_ROOT/name).glob('*.safetensors')) or (MODEL_ROOT/name/'config.json').is_file() and (MODEL_ROOT/name/'pytorch_model.bin').is_file() for name in MODEL_IDS}

def predict(image,occluders,target,check):
    global _models
    with _lock:
        check()
        import torch
        from transformers import AutoImageProcessor,AutoModelForDepthEstimation,CLIPSegProcessor,CLIPSegForImageSegmentation
        if not all(readiness().values()):raise RuntimeError('PATCHI 模型未准备好，请先在 ComfyUI 环境运行 prepare_models.py')
        if _models is None:
            torch.set_num_threads(max(1,min(4,os.cpu_count() or 1)))
            dp=AutoImageProcessor.from_pretrained(MODEL_ROOT/'depth',local_files_only=True)
            dm=AutoModelForDepthEstimation.from_pretrained(MODEL_ROOT/'depth',local_files_only=True).to('cpu').eval()
            sp=CLIPSegProcessor.from_pretrained(MODEL_ROOT/'segment',local_files_only=True)
            sm=CLIPSegForImageSegmentation.from_pretrained(MODEL_ROOT/'segment',local_files_only=True).to('cpu').eval()
            _models=(dp,dm,sp,sm)
        dp,dm,sp,sm=_models;check();small=image.convert('RGB');small.thumbnail((448,448),Image.Resampling.LANCZOS)
        with torch.inference_mode():
            depth=dm(**dp(images=small,return_tensors='pt',size={'height':392,'width':392})).predicted_depth[0].float().cpu().numpy()
            check()
            # Separate object prompts improve thin-object recall over a sentence containing commas.
            texts=['a photo of '+p.strip() for p in occluders.split(',')]+['a photo of '+target]
            out=sm(**sp(text=texts,images=[small]*len(texts),padding=True,return_tensors='pt')).logits.sigmoid().float().cpu().numpy()
        check();return depth,out[:-1].max(axis=0),out[-1]
