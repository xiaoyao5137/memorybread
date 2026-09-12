# -*- coding: utf-8 -*-
import argparse, ctypes, fcntl, json, os, socket, statistics, subprocess, sys, threading, time, urllib.request
from pathlib import Path
import psutil
parser = argparse.ArgumentParser()
parser.add_argument('--artifact-root', type=Path, default=Path.home()/'.memory-bread/experiments/inference-20260905')
parser.add_argument('--output-dir', type=Path, required=True)
args = parser.parse_args()
ROOT = args.artifact_root.resolve()
OUT = args.output_dir.resolve()
OUT.mkdir(parents=True, exist_ok=True)
if any(OUT.iterdir()):
    raise RuntimeError('Choose an empty output directory')
source=Path(__file__).with_name('benchmark.py').read_text(encoding='utf-8')
source=source.replace("p.add_argument('--repeats', type=int, default=2)","p.add_argument('--repeats', type=int, default=2)\np.add_argument('--num-ctx', type=int, default=16384)").replace("'num_ctx': 16384", "'num_ctx': args.num_ctx")
(OUT/'benchmark.py').write_text(source,encoding='utf-8')
class Usage(ctypes.Structure):
    _fields_=[('uuid',ctypes.c_uint8*16)]+[(n,ctypes.c_uint64) for n in ['user','system','idle','interrupt','pageins','wired','resident','footprint','start','exit','child_user','child_system','child_idle','child_interrupt','child_pageins','child_elapsed','disk_read','disk_written']]
lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
lib.proc_pid_rusage.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_void_p]
lib.proc_pid_rusage.restype=ctypes.c_int
assert ctypes.sizeof(Usage)==160
URL='http://127.0.0.1:11436'
def api(path,body=None):
    req=urllib.request.Request(URL+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(req,timeout=15))
def sample(server):
    procs=[server]+server.children(recursive=True); values=[]
    for proc in procs:
        u=Usage()
        if lib.proc_pid_rusage(proc.pid,2,ctypes.byref(u))==0:
            values.append({'pid':proc.pid,'rss':int(u.resident),'footprint':int(u.footprint)})
    return {'t':time.monotonic(),'rss':sum(p['rss'] for p in values),'footprint':sum(p['footprint'] for p in values),'processes':values}
def summary(samples):
    return {k:{'peak_gib':max(x[k] for x in samples)/2**30,'last_gib':samples[-1][k]/2**30,'median_gib':statistics.median(x[k] for x in samples)/2**30} for k in ['rss','footprint']}
def run_bench(server,label,model,ctx,smoke=False):
    args=[sys.executable,str(OUT/'benchmark.py'),'--label',label,'--model',model,'--output-dir',str(OUT),'--num-ctx',str(ctx),'--repeats','1', '--smoke' if smoke else '--throughput']
    samples=[]
    with (OUT/(label+'.console')).open('w') as log:
        proc=subprocess.Popen(args,stdout=log,stderr=log)
        try:
            while proc.poll() is None:
                samples.append(sample(server));time.sleep(.2)
            assert proc.returncode==0,(label,proc.returncode)
        finally:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=10)
    return samples
profiles=[('gguf-16k','old-runtime','qwen3.5:4b',16384),('gguf-8k','old-runtime','qwen3.5:4b',8192),('mlx-16k','new-runtime','qwen3.5:4b-mlx',16384),('mtp-16k','new-runtime','mbem-qwen35:4b-mlx-mtp-native',16384)]
with open('/tmp/memory-bread-interactive-demand.lock','a+') as lock:
    fcntl.flock(lock,fcntl.LOCK_SH)
    for label,runtime,model,ctx in profiles:
        with socket.socket() as s:assert s.connect_ex(('127.0.0.1',11436))!=0,'test port busy'
        env=dict(os.environ,OLLAMA_HOST='127.0.0.1:11436',OLLAMA_MODELS=str(ROOT/'models'),OLLAMA_NO_CLOUD='1',OLLAMA_NOHISTORY='1',OLLAMA_MAX_LOADED_MODELS='1',OLLAMA_NUM_PARALLEL='1')
        for k in ['OLLAMA_KV_CACHE_TYPE','OLLAMA_FLASH_ATTENTION']:env.pop(k,None)
        with (OUT/(label+'-server.log')).open('w') as log:
            process=subprocess.Popen([str(ROOT/runtime/'ollama'),'serve'],env=env,stdout=log,stderr=log,start_new_session=True)
            server=psutil.Process(process.pid)
            try:
                for attempt in range(100):
                    try:api('/api/version');break
                    except Exception:time.sleep(.2)
                else:raise RuntimeError('server startup failed')
                baseline=sample(server)
                loading=run_bench(server,label+'-load',model,ctx,True)
                loaded=sample(server)
                generated=run_bench(server,label,model,ctx)
                idle=[]
                for _ in range(25):idle.append(sample(server));time.sleep(.2)
                model_ps=api('/api/ps')
                api('/api/generate',{'model':model,'keep_alive':0})
                time.sleep(1)
                unloaded=sample(server)
                result={'label':label,'runtime':runtime,'model':model,'ctx':ctx,'server_idle':baseline,'loaded_short':loaded,'load_peak':summary(loading),'generation':summary(generated),'post_generation_idle_5s':summary(idle),'after_unload':unloaded,'ollama_ps':model_ps}
                (OUT/(label+'-memory.json')).write_text(json.dumps(result,indent=2))
                (OUT/(label+'-samples.json')).write_text(json.dumps({'load':loading,'generation':generated,'idle':idle}))
                print(json.dumps({'label':label,'generation':result['generation'],'idle':result['post_generation_idle_5s']},ensure_ascii=False),flush=True)
            finally:
                children=server.children(recursive=True) if server.is_running() else []
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                for child in children:
                    try:child.terminate()
                    except psutil.NoSuchProcess:pass
print('memory comparison complete',flush=True)
