#!/usr/bin/env python3
"""Post-cleanup verifier. It never emits credential values or model responses."""
from __future__ import annotations

import json, os, re, subprocess, sys
from pathlib import Path

ROOT = Path('/home/n8nadmin/.hermes')
PROFILES = ('root', 'apolo', 'atena', 'default', 'dev', 'hefesto', 'kratos', 'marketing', 'prometeu')
ENV_REF = re.compile(r'^\$\{(OPENCODEX_[A-Z0-9_]+_API_KEY)\}$')


def home(name): return ROOT if name == 'root' else ROOT / 'profiles' / name


def check_profile(name):
    target = home(name)
    code = '''
import json, os
from hermes_cli.env_loader import load_hermes_dotenv
load_hermes_dotenv(hermes_home=os.environ["HERMES_HOME"], load_external_secrets=False)
from hermes_cli.config import load_config
cfg=load_config()
print(json.dumps({"providers":sorted(cfg.get("providers",{})), "default":cfg.get("model",{}).get("default"), "delegation":cfg.get("delegation",{}).get("api_key"), "aux":cfg.get("auxiliary",{}).get("compression",{}).get("api_key")}))
'''
    env = dict(os.environ, HERMES_HOME=str(target))
    result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, check=True)
    details = json.loads(result.stdout)
    if details['providers'] != ['nous-api', 'opencodex']: raise RuntimeError(f'{name}: unexpected providers')
    raw = (target / 'config.yaml').read_text(encoding='utf-8')
    if re.search(r'^\s*api_key:\s*(?!\$\{OPENCODEX_[A-Z0-9_]+_API_KEY\})\S+', raw, re.M): raise RuntimeError(f'{name}: literal inference api_key remains')
    return {'providers': details['providers'], 'default': details['default']}


def smoke(base_url, key, model):
    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=key, timeout=30)
    response = client.chat.completions.create(model=model, messages=[{'role': 'user', 'content': 'Reply exactly: ok'}], max_tokens=4)
    if not response.choices: raise RuntimeError(f'{model}: empty response')


def main():
    import ruamel.yaml
    yaml = ruamel.yaml.YAML(typ='safe')
    results = {name: check_profile(name) for name in PROFILES}
    root_env = {}
    for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1); root_env[k] = v
    root_cfg = yaml.load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
    nous_model = root_cfg['providers']['nous-api']['default_model']
    smoke('https://inference-api.nousresearch.com/v1', root_env['NOUS_API_KEY'], nous_model)
    smoke('http://127.0.0.1:10100/v1', root_env['OPENCODEX_API_KEY'], 'fast')
    auth = json.loads((ROOT / 'auth.json').read_text(encoding='utf-8'))
    allowed = {'custom:opencodex', 'custom:nous-api'}
    providers = set((auth.get('providers') or {}).keys())
    pools = set((auth.get('credential_pool') or {}).keys())
    if not providers.issubset(allowed) or not pools.issubset(allowed): raise RuntimeError('auth cleanup incomplete')
    print(json.dumps({'profiles': len(results), 'smoke': {'nous': 'ok', 'opencodex_fast': 'ok'}, 'auth_provider_ids': sorted(providers), 'auth_pool_ids': sorted(pools)}))

if __name__ == '__main__': main()
