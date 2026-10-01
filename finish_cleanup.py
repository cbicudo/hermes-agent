#!/usr/bin/env python3
"""Authorized final repair for Nous/OpenCodex cleanup; never prints secrets."""
from __future__ import annotations
import copy, hashlib, json, os, shutil, stat
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

ROOT=Path('/home/n8nadmin/.hermes')
ORIG=ROOT/'backups/provider-cleanup-20261001T111708Z'
NAMES=('root','apolo','atena','default','dev','hefesto','kratos','marketing','prometeu')
ALLOWED={'custom:opencodex','custom:nous-api'}
REPL={'fallback-coding':'coding','fallback-reasoning':'reasoning','openai/gpt-5.6-luna':'gpt-5.6-luna','openai/gpt-5.6-terra':'gpt-5.6-terra'}
def home(n): return ROOT if n=='root' else ROOT/'profiles'/n
def original(n): return ORIG/'config.yaml' if n=='root' else ORIG/'profiles'/n/'config.yaml'
def aw(p,s):
 t=p.with_name('.'+p.name+'.finish.tmp'); t.write_text(s,encoding='utf8'); os.chmod(t,0o600); os.replace(t,p)
def norm(x):
 if isinstance(x,dict):
  for k,v in x.items(): x[k]=REPL.get(v,v) if k=='model' and isinstance(v,str) else norm(v)
 elif isinstance(x,list):
  for v in x:norm(v)
 return x
def fallback(raw,yaml):
 x=raw.get('fallback_providers',[])
 if isinstance(x,str): x=yaml.load(x) or []
 if not isinstance(x,list): return []
 out=[]
 for item in x:
  if not isinstance(item,dict) or item.get('provider') not in {'opencodex','nous-api'}: continue
  item=norm(copy.deepcopy(item))
  if item not in out: out.append(item)
 return out
def auth_clean(p):
 d=json.loads(p.read_text(encoding='utf8')); out=copy.deepcopy(d)
 for key in ('providers','credential_pool'):
  if isinstance(out.get(key),dict): out[key]={k:v for k,v in out[key].items() if k in ALLOWED}
 for key in ('active_provider','default_provider'):
  if out.get(key) not in ALLOWED: out.pop(key,None)
 return json.dumps(out,indent=2,sort_keys=True)+'\n'
def main():
 from ruamel.yaml import YAML
 from hermes_cli.config_defaults import DEFAULT_CONFIG
 from cleanup_tmp import prune_defaults
 y=YAML(); y.default_flow_style=False
 if not ORIG.is_dir(): raise RuntimeError('original backup unavailable')
 staged={}; auths={}
 for n in NAMES:
  cur=y.load((home(n)/'config.yaml').read_text())
  old=y.load(original(n).read_text())
  memold=old.get('memory','__missing__') if isinstance(old,dict) else '__missing__'
  if memold=='__missing__': cur.pop('memory',None)
  else:
   if not isinstance(cur.get('memory'),dict): cur['memory']={}
   # external memory selector only; retention excludes delegation/quick routing.
   if 'provider' in memold: cur['memory']['provider']=memold['provider']
   else: cur['memory'].pop('provider',None)
  fb=fallback(old,y)
  if fb: cur['fallback_providers']=fb
  else: cur.pop('fallback_providers',None)
  norm(cur)
  default=cur.get('model',{}).get('default') if isinstance(cur.get('model'),dict) else None
  refs=set()
  for f in fb:
   if isinstance(f.get('model'),str): refs.add(f['model'])
  if isinstance(default,str): refs.add(default)
  providers=cur.get('providers',{})
  if isinstance(providers.get('opencodex'),dict):
   models=providers['opencodex'].get('models',[])
   providers['opencodex']['models']=sorted(set(models if isinstance(models,list) else [])|refs)
  cur=prune_defaults(cur,DEFAULT_CONFIG)
  staged[n]=cur
  ap=home(n)/'auth.json'
  if ap.exists(): auths[n]=auth_clean(ap)
 # Validate all staged config and auth copies before writes.
 for n,cfg in staged.items():
  s=StringIO(); y.dump(cfg,s)
  if not isinstance(y.load(s.getvalue()),dict): raise RuntimeError(f'{n}: yaml roundtrip')
  if not set(json.loads(auths[n]).get('credential_pool',{})).issubset(ALLOWED) if n in auths else False: raise RuntimeError(f'{n}: auth')
 stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'); backup=ROOT/'backups'/f'nous-opencodex-final-{stamp}'
 backup.mkdir(mode=0o700,parents=True); os.chmod(backup,0o700); manifest=[]
 for n in NAMES:
  for fn in ('config.yaml','auth.json'):
   src=home(n)/fn
   if not src.exists(): continue
   dst=backup/n/fn; dst.parent.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(dst.parent,0o700); shutil.copy2(src,dst); os.chmod(dst,0o600)
   manifest.append({'path':str(src.relative_to(ROOT)),'sha256':hashlib.sha256(src.read_bytes()).hexdigest()})
 aw(backup/'MANIFEST.json',json.dumps(manifest,indent=2)+'\n')
 for n,cfg in staged.items():
  s=StringIO(); y.dump(cfg,s); aw(home(n)/'config.yaml',s.getvalue())
 for n,text in auths.items(): aw(home(n)/'auth.json',text)
 print(json.dumps({'backup':str(backup),'configs':len(staged),'auths':len(auths),'fallback_profiles':sum('fallback_providers' in x for x in staged.values())}))
if __name__=='__main__': main()
