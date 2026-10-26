"""Run once using the ComfyUI Python, on the machine that performs processing."""
from pathlib import Path
from huggingface_hub import snapshot_download,HfApi
try:from .semantic import MODEL_IDS,MODEL_ROOT
except ImportError:from semantic import MODEL_IDS,MODEL_ROOT
for name,repo in MODEL_IDS.items():
    print('Downloading',repo,'to',MODEL_ROOT/name,flush=True)
    files=HfApi().list_repo_files(repo);weights=['*.safetensors'] if any(f.endswith('.safetensors') for f in files) else ['pytorch_model.bin']
    snapshot_download(repo,local_dir=str(MODEL_ROOT/name),allow_patterns=['*.json','*.txt']+weights,ignore_patterns=['*onnx*','*flax*','*tf_model*'])
print('Models ready. Restart ComfyUI. First job loads models into CPU memory; later jobs reuse them.')
