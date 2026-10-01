#!/usr/bin/env python3
"""Authorized Nous/OpenCodex cleanup. Secrets stay in memory and are never printed."""
from __future__ import annotations

import copy, hashlib, json, os, re, shutil, stat, sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path('/home/n8nadmin/.hermes')
PROFILES = ('root', 'apolo', 'atena', 'default', 'dev', 'hefesto', 'kratos', 'marketing', 'prometeu')
OPENCODEX_URL = 'http://127.0.0.1:10100/v1'
NOUS_URL = 'https://inference-api.nousresearch.com/v1'
ALIASES = ('orchestrator', 'reasoning', 'coding', 'fast', 'auxiliar', 'vision')
MODEL_REPLACEMENTS = {'fallback-coding': 'coding', 'fallback-reasoning': 'reasoning', 'openai/gpt-5.6-luna': 'gpt-5.6-luna', 'claude-opus-4-6': 'Cheaper/claude-opus-4.6'}
LEGACY_INFERENCE_PREFIXES = ('NVIDIA_', 'OPENAI_', 'ANTHROPIC_', 'DEEPSEEK_', 'GEMINI_', 'BEDROCK_', 'AWS_', 'HF_', 'HUGGINGFACE_', 'OLLAMA_', 'MISTRAL_', 'ELEVENLABS_', 'MINIMAX_', 'GLM_', 'ZAI_', 'OPENROUTER_', 'OPENCODE_')
INFERENCE_ROOTS = {'model', 'auxiliary', 'memory', 'delegation', 'moa', 'fallback_providers'}


def pdir(name): return ROOT if name == 'root' else ROOT / 'profiles' / name

def env_name(path):
    token = re.sub(r'[^A-Za-z0-9]+', '_', '_'.join(map(str, path))).strip('_').upper()
    return f'OPENCODEX_{token}_API_KEY'

def parse_env(path):
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()): values[key.strip()] = value
    return values

def render_env(values):
    groups = [
        ('Runtime and browser', lambda k: k.startswith(('TERMINAL_', 'BROWSER_', 'WEB_TOOLS_', 'VISION_TOOLS_', 'MOA_TOOLS_', 'IMAGE_TOOLS_'))),
        ('Messaging and gateway', lambda k: k.startswith(('TELEGRAM_', 'WHATSAPP_', 'GATEWAY_', 'API_SERVER_'))),
        ('Operational integrations', lambda k: k in {'SUPERMEMORY_API_KEY', 'FIRECRAWL_API_KEY', 'FIRECRAWL_API_URL', 'NOTION_API_KEY'}),
        ('Inference', lambda k: k == 'OPENCODEX_API_KEY' or k == 'NOUS_API_KEY' or (k.startswith('OPENCODEX_') and k.endswith('_API_KEY'))),
    ]
    emitted, out = set(), []
    for title, predicate in groups:
        keys = sorted(k for k in values if predicate(k))
        if keys:
            out += [f'# {title}', *[f'{k}={values[k]}' for k in keys], '']
            emitted.update(keys)
    rest = sorted(k for k in values if k not in emitted)
    if rest: out += ['# Other preserved settings', *[f'{k}={values[k]}' for k in rest], '']
    return '\n'.join(out)

def atomic_write(path, content, mode=0o600):
    tmp = path.with_name('.' + path.name + '.nous-cleanup.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, mode); os.replace(tmp, path)
    finally:
        if tmp.exists(): tmp.unlink()

def walk(node, path=()):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items(): yield from walk(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node): yield from walk(value, path + (index,))

def normalize_models(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'model' and isinstance(value, str): node[key] = MODEL_REPLACEMENTS.get(value, value)
            else: normalize_models(value)
    elif isinstance(node, list):
        for value in node: normalize_models(value)


def normalize_inference_routes(node, path=()):
    if isinstance(node, dict):
        if path and path[0] in INFERENCE_ROOTS and 'provider' in node and node['provider'] not in {'opencodex', 'nous-api', 'auto', 'moa'}:
            node['provider'] = 'opencodex'
        for key, value in node.items(): normalize_inference_routes(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node): normalize_inference_routes(value, path + (index,))

def collect_models(node, found, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            here = path + (key,)
            if path and path[0] in INFERENCE_ROOTS and key == 'model' and isinstance(value, str): found.add(value)
            collect_models(value, found, here)
    elif isinstance(node, list):
        for i, value in enumerate(node): collect_models(value, found, path + (i,))

def is_inference_block(path, node):
    if not path or path[0] not in INFERENCE_ROOTS: return False
    return node.get('provider') == 'opencodex' or node.get('base_url') == OPENCODEX_URL or 'api_key' in node

def external_models(base_url, key):
    req = Request(base_url + '/models', headers={'Authorization': f'Bearer {key}'})
    with urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode('utf-8'))
    ids = {item['id'] for item in payload.get('data', []) if isinstance(item, dict) and isinstance(item.get('id'), str)}
    if not ids: raise RuntimeError(f'empty model catalogue: {base_url}')
    return ids

def plugin_ids_to_disable(home, cfg):
    plugins = cfg.get('plugins')
    if not isinstance(plugins, dict): return []
    enabled = plugins.get('enabled') if isinstance(plugins.get('enabled'), list) else []
    roots = (ROOT / 'plugins', home / 'plugins')
    retired = []
    for plugin_id in enabled:
        if not isinstance(plugin_id, str): continue
        text = ''
        for root in roots:
            for name in ('plugin.yaml', 'plugin.yml', 'manifest.yaml', 'manifest.json'):
                candidate = root / plugin_id / name
                if candidate.exists(): text += candidate.read_text(encoding='utf-8', errors='ignore').lower()
        if any(term in text for term in ('nvidia', 'openrouter', 'deepseek', 'gemini-provider', 'opencode-zen', 'huggingface-provider')): retired.append(plugin_id)
    plugins['enabled'] = [x for x in enabled if x not in retired]
    disabled = plugins.get('disabled') if isinstance(plugins.get('disabled'), list) else []
    plugins['disabled'] = sorted(set(disabled).union(retired))
    return retired

def prune_defaults(value, default):
    if isinstance(value, dict) and isinstance(default, dict):
        out = {}
        for key, child in value.items():
            if key not in default: out[key] = child; continue
            candidate = prune_defaults(child, default[key])
            if candidate != default[key]: out[key] = candidate
        return out
    return value

def clean_auth(path, backup):
    source = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(source, dict): raise RuntimeError('auth store is not an object')
    target = copy.deepcopy(source)
    providers = target.get('providers')
    if isinstance(providers, dict): target['providers'] = {k: v for k, v in providers.items() if k in {'custom:opencodex', 'custom:nous-api'}}
    pools = target.get('credential_pool')
    if isinstance(pools, dict): target['credential_pool'] = {k: v for k, v in pools.items() if k in {'custom:opencodex', 'custom:nous-api'}}
    for key in ('active_provider', 'default_provider'):
        if target.get(key) not in {'custom:opencodex', 'custom:nous-api'}: target.pop(key, None)
    return json.dumps(target, indent=2, sort_keys=True) + '\n'

def main():
    from ruamel.yaml import YAML
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    yaml = YAML(); yaml.default_flow_style = False
    homes = [(name, pdir(name)) for name in PROFILES]
    for _, home in homes:
        for filename in ('config.yaml', '.env'):
            if not (home / filename).is_file(): raise RuntimeError(f'missing target {home / filename}')
    parsed = {name: yaml.load((home / 'config.yaml').read_text(encoding='utf-8')) for name, home in homes}
    envs = {name: parse_env(home / '.env') for name, home in homes}
    if any(not isinstance(value, dict) for value in parsed.values()): raise RuntimeError('non-mapping config')
    # Load every literal into a distinct profile-local variable and leave the primary key unchanged.
    literal_records = {}
    for name, _ in homes:
        cfg, env = parsed[name], envs[name]
        records = []
        for path, block in walk(cfg):
            if isinstance(block, dict) and isinstance(block.get('api_key'), str) and block['api_key'].strip() and is_inference_block(path, block):
                variable = env_name(path)
                if variable in env and env[variable] != block['api_key']:
                    raise RuntimeError(f'{name}: conflicting target env name {variable}')
                env[variable] = block['api_key']
                block['api_key'] = '${' + variable + '}'
                records.append((path, variable))
        if not env.get('OPENCODEX_API_KEY', '').strip(): raise RuntimeError(f'{name}: missing primary OPENCODEX_API_KEY')
        if not env.get('NOUS_API_KEY', '').strip(): raise RuntimeError(f'{name}: missing NOUS_API_KEY')
        literal_records[name] = records
    # Live catalogues are checked before any file is changed. Nous default is a real advertised model.
    local_catalog = external_models(OPENCODEX_URL, envs['root']['OPENCODEX_API_KEY'])
    nous_catalog = external_models(NOUS_URL, envs['root']['NOUS_API_KEY'])
    nous_default = sorted(nous_catalog)[0]
    staged, evidence = {}, {}
    for name, home in homes:
        cfg = copy.deepcopy(parsed[name]); env = envs[name]
        normalize_models(cfg)
        normalize_inference_routes(cfg)
        cfg.pop('bedrock', None)
        cfg.pop('fallback_providers', None)
        cfg.pop('models', None)
        model = cfg.get('model')
        if not isinstance(model, dict): model = {'default': model} if isinstance(model, str) else {}
        default = model.get('default', 'orchestrator')
        if default == 'fallback-coding': default = 'coding'
        elif default == 'fallback-reasoning': default = 'reasoning'
        elif default == 'openai/gpt-5.6-luna': default = 'gpt-5.6-luna'
        model = {'default': default, 'provider': 'opencodex', 'base_url': OPENCODEX_URL}
        cfg['model'] = model
        refs = set(ALIASES); collect_models(cfg, refs)
        refs.discard(''); refs.discard(nous_default)
        missing = sorted(refs - local_catalog)
        if missing: raise RuntimeError(f'{name}: OpenCodex unknown model references: {missing}')
        cfg['providers'] = {
            'opencodex': {'base_url': OPENCODEX_URL, 'key_env': 'OPENCODEX_API_KEY', 'api_mode': 'chat_completions', 'discover_models': False, 'models': sorted(refs)},
            'nous-api': {'base_url': NOUS_URL, 'key_env': 'NOUS_API_KEY', 'api_mode': 'chat_completions', 'discover_models': True, 'default_model': nous_default},
        }
        # Retain selected local/edge engines only; remove inactive provider blocks.
        if isinstance(cfg.get('tts'), dict): cfg['tts'] = {'provider': cfg['tts'].get('provider', 'edge')}
        if isinstance(cfg.get('stt'), dict):
            old = cfg['stt']; cfg['stt'] = {'enabled': old.get('enabled', True), 'provider': old.get('provider', 'local')}
            if cfg['stt']['provider'] == 'local' and isinstance(old.get('local'), dict): cfg['stt']['local'] = {'model': old['local'].get('model', 'base')}
        retired = plugin_ids_to_disable(home, cfg)
        for key in list(env):
            if key == 'NVIDIA_API_KEY' or (key.startswith(LEGACY_INFERENCE_PREFIXES) and key not in {'OPENCODEX_API_KEY', 'NOUS_API_KEY'}): env.pop(key, None)
        cfg = prune_defaults(cfg, DEFAULT_CONFIG)
        staged[name] = cfg
        evidence[name] = {'inline_literals': len(literal_records[name]), 'retired_plugins': retired, 'models': len(refs)}
    # Round-trip and effective config resolution before swaps. Env values are compared in-process only.
    for name, home in homes:
        text_stream = StringIO(); yaml.dump(staged[name], text_stream)
        candidate = yaml.load(text_stream.getvalue())
        if not isinstance(candidate, dict): raise RuntimeError(f'{name}: YAML roundtrip failed')
        sandbox = home / '.nous-cleanup-stage'
        sandbox.mkdir(mode=0o700, exist_ok=False)
        try:
            atomic_write(sandbox / 'config.yaml', text_stream.getvalue())
            atomic_write(sandbox / '.env', render_env(envs[name]))
            old_home = os.environ.get('HERMES_HOME'); os.environ['HERMES_HOME'] = str(sandbox)
            from hermes_cli.env_loader import load_hermes_dotenv
            from hermes_cli.config import load_config
            load_hermes_dotenv(hermes_home=sandbox, load_external_secrets=False)
            effective = load_config()
            for path, variable in literal_records[name]:
                probe = effective
                for part in path: probe = probe[part]
                if probe.get('api_key') != envs[name][variable]: raise RuntimeError(f'{name}: env expansion mismatch at {path}')
            if old_home is None: os.environ.pop('HERMES_HOME', None)
            else: os.environ['HERMES_HOME'] = old_home
        finally: shutil.rmtree(sandbox, ignore_errors=True)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = ROOT / 'backups' / f'nous-opencodex-cleanup-{timestamp}'
    backup.mkdir(mode=0o700, parents=True); os.chmod(backup, 0o700)
    manifest = []
    targets = [(name, home, 'config.yaml') for name, home in homes] + [(name, home, '.env') for name, home in homes] + [('root', ROOT, 'auth.json')]
    for name, home, filename in targets:
        source = home / filename; dest = backup / name / filename; dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True); os.chmod(dest.parent, 0o700)
        shutil.copy2(source, dest); os.chmod(dest, 0o600)
        manifest.append({'path': str(source.relative_to(ROOT)), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'mode': oct(stat.S_IMODE(source.stat().st_mode))})
    atomic_write(backup / 'MANIFEST.json', json.dumps(manifest, indent=2) + '\n')
    for name, home in homes:
        stream = StringIO(); yaml.dump(staged[name], stream)
        atomic_write(home / 'config.yaml', stream.getvalue()); atomic_write(home / '.env', render_env(envs[name]))
    atomic_write(ROOT / 'auth.json', clean_auth(ROOT / 'auth.json', backup))
    result = {'backup': str(backup), 'profiles': {name: {**evidence[name], 'nvidia_removed': 'NVIDIA_API_KEY' not in envs[name]} for name, _ in homes}, 'local_models': len(local_catalog), 'nous_models': len(nous_catalog), 'auth_pruned': True}
    print(json.dumps(result, sort_keys=True))

if __name__ == '__main__': main()
