#!/usr/bin/env python3
"""Final verifier: no secrets, model outputs, or network calls."""
from __future__ import annotations
import json, os, re, subprocess, sys
from pathlib import Path
ROOT=Path('/home/n8nadmin/.hermes'); NAMES=('root','apolo','atena','default','dev','hefesto','kratos','marketing','prometeu'); ALLOWED={'custom:opencodex','custom:nous-api'}
def home(n): return ROOT if n=='root' else ROOT/'profiles'/n
def runtime(n):
 code='''import json,os
from hermes_cli.env_loader import load_hermes_dotenv
load_hermes_dotenv(hermes_home=os.environ["HERMES_HOME"],load_external_secrets=False)
from hermes_cli.config import load_config
c=load_config(); print(json.dumps({"providers":sorted(c.get("providers",{})),"default":c.get("model",{}).get("default"),"fallback":c.get("fallback_providers",[]),"memory_provider":c.get("memory",{}).get("provider")}))'''
 r=subprocess.run([sys.executable,'-c',code],env=dict(os.environ,HERMES_HOME=str(home(n))),capture_output=True,text=True,check=True)
 d=json.loads(r.stdout)
 if d['providers']!=['nous-api','opencodex']: raise RuntimeError(f'{n}: providers')
 return d
def main():
 results={n:runtime(n) for n in NAMES}; auth={}
 for n in NAMES:
  p=home(n)/'auth.json'
  if p.exists():
   d=json.loads(p.read_text()); ids=set((d.get('providers')or{}))|set((d.get('credential_pool')or{}))
   if not ids.issubset(ALLOWED): raise RuntimeError(f'{n}: auth IDs')
   auth[n]=sorted(ids)
 print(json.dumps({'profiles':len(results),'auth_paths':len(auth),'fallback_profiles':sum(bool(v['fallback']) for v in results.values()),'auth_ids':auth}))
if __name__=='__main__': main()
