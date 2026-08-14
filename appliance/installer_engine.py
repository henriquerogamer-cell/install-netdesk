#!/usr/bin/env python3
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

STATE = Path('/var/lib/netdesk-appliance')
INSTALL_ROOT = STATE / 'install'
REQUEST_FILE = INSTALL_ROOT / 'request.json'
STATUS_FILE = INSTALL_ROOT / 'status.json'
LOG_FILE = INSTALL_ROOT / 'install.log'
NETDESK_ROOT = Path('/opt/netdesk')
CHAT_ROOT = Path('/opt/chat')
NETDESK_REPO = 'https://github.com/henriquerogamer-cell/netdesk.git'
CHAT_REPO = 'https://github.com/henriquerogamer-cell/chat.git'
NETDESK_BRANCH = os.environ.get('NETDESK_INSTALL_BRANCH', 'agent/campaign-execution-history')
CHAT_BRANCH = os.environ.get('NETDESK_CHAT_BRANCH', 'main')


def _ensure_dirs():
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(INSTALL_ROOT, 0o700)


def _atomic_json(path, value):
    _ensure_dirs()
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _service_exists(name):
    return subprocess.run(['systemctl', 'list-unit-files', f'{name}.service'], capture_output=True, text=True).returncode == 0 and name in subprocess.run(['systemctl', 'list-unit-files', f'{name}.service'], capture_output=True, text=True).stdout


def install_preflight():
    disk = shutil.disk_usage('/')
    netdesk_exists = NETDESK_ROOT.exists() or _service_exists('netdesk-backend')
    chat_exists = CHAT_ROOT.exists() or _service_exists('chat-notifications')
    checks = [
        {'id': 'clean_netdesk', 'label': 'Nenhuma instalação NETDESK existente', 'ok': not netdesk_exists},
        {'id': 'clean_chat', 'label': 'Nenhuma instalação CHAT existente', 'ok': not chat_exists},
        {'id': 'disk', 'label': 'Espaço livre mínimo de 20 GB', 'ok': disk.free >= 20 * 1024**3, 'detail': f'{disk.free} bytes livres'},
        {'id': 'root', 'label': 'Agente privilegiado disponível', 'ok': os.geteuid() == 0},
    ]
    return {
        'apt': all(item['ok'] for item in checks),
        'checks': checks,
        'existing_installation': bool(netdesk_exists or chat_exists),
        'netdesk_branch': NETDESK_BRANCH,
        'chat_branch': CHAT_BRANCH,
    }


def install_status():
    _ensure_dirs()
    try:
        data = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
    except Exception:
        data = {'stage': 'idle', 'running': False, 'success': False}
    try:
        lines = LOG_FILE.read_text(encoding='utf-8', errors='replace').splitlines()
        data['log_tail'] = lines[-120:]
    except Exception:
        data['log_tail'] = []
    data['preflight'] = install_preflight()
    return data


def queue_install(payload):
    preflight = install_preflight()
    if not preflight['apt']:
        raise ValueError('Esta máquina não está apta para Nova instalação. O instalador nunca sobrescreve uma instalação existente.')

    token = str(payload.get('github_token') or '').strip()
    owner_username = str(payload.get('owner_username') or '').strip()
    owner_password = str(payload.get('owner_password') or '')
    owner_email = str(payload.get('owner_email') or '').strip()
    company = str(payload.get('company') or '').strip()

    if len(token) < 20:
        raise ValueError('Informe um token GitHub temporário com acesso aos repositórios privados NETDESK e CHAT.')
    if not re.fullmatch(r'[A-Za-z0-9._-]{3,80}', owner_username):
        raise ValueError('Usuário proprietário inválido. Use 3 a 80 caracteres: letras, números, ponto, hífen ou sublinhado.')
    if len(owner_password) < 10 or len(owner_password) > 128:
        raise ValueError('A senha do proprietário deve ter entre 10 e 128 caracteres.')
    if not company:
        raise ValueError('Informe o nome da empresa.')

    current = install_status()
    if current.get('running'):
        raise ValueError('Já existe uma instalação em andamento.')

    request = {
        'github_token': token,
        'owner_username': owner_username,
        'owner_password': owner_password,
        'owner_email': owner_email,
        'company': company,
        'created_at': int(time.time()),
    }
    _atomic_json(REQUEST_FILE, request)
    _atomic_json(STATUS_FILE, {
        'stage': 'queued',
        'running': True,
        'success': False,
        'started_at': int(time.time()),
        'company': company,
    })
    LOG_FILE.write_text('[INSTALL] Job recebido pela appliance.\n', encoding='utf-8')
    os.chmod(LOG_FILE, 0o600)

    result = subprocess.run(['systemctl', 'start', 'netdesk-install-agent.service'], capture_output=True, text=True)
    if result.returncode != 0:
        try:
            REQUEST_FILE.unlink()
        except FileNotFoundError:
            pass
        _atomic_json(STATUS_FILE, {'stage': 'failed', 'running': False, 'success': False, 'error': 'Falha ao iniciar agente privilegiado.'})
        raise ValueError('Não foi possível iniciar o agente de instalação.')
    return install_status()


def _log(message):
    _ensure_dirs()
    line = f'[{time.strftime("%H:%M:%S")}] {message}'
    with LOG_FILE.open('a', encoding='utf-8') as handle:
        handle.write(line + '\n')
    print(line, flush=True)


def _set_status(stage, **extra):
    try:
        current = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
    except Exception:
        current = {}
    current.update({'stage': stage, **extra, 'updated_at': int(time.time())})
    _atomic_json(STATUS_FILE, current)


def _run(args, cwd=None, env=None, secret_values=None):
    secrets_list = [str(x) for x in (secret_values or []) if x]
    display = ' '.join(str(x) for x in args)
    for secret in secrets_list:
        display = display.replace(secret, '***')
    _log(f'$ {display}')
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for raw in proc.stdout:
        line = raw.rstrip()
        for secret in secrets_list:
            line = line.replace(secret, '***')
        if line:
            _log(line)
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f'Comando falhou ({code}): {display}')


def _write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    os.chmod(path, mode)


def _git_clone(repo, branch, dest, token):
    askpass = INSTALL_ROOT / f'askpass-{secrets.token_hex(6)}.sh'
    _write(askpass, '#!/bin/sh\ncase "$1" in\n  *Username*) printf "%s\\n" "x-access-token" ;;\n  *) printf "%s\\n" "$NETDESK_GITHUB_TOKEN" ;;\nesac\n', 0o700)
    env = os.environ.copy()
    env.update({'GIT_ASKPASS': str(askpass), 'GIT_TERMINAL_PROMPT': '0', 'NETDESK_GITHUB_TOKEN': token})
    try:
        _run(['git', 'clone', '--branch', branch, '--single-branch', repo, str(dest)], env=env, secret_values=[token])
    finally:
        try:
            askpass.unlink()
        except FileNotFoundError:
            pass


def _sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _wait_postgres():
    for _ in range(60):
        result = subprocess.run(['docker', 'exec', 'netdesk-postgres', 'pg_isready', '-U', 'netdesk', '-d', 'netdesk'], capture_output=True)
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError('PostgreSQL não ficou disponível dentro do tempo esperado.')


def run_agent():
    if os.geteuid() != 0:
        raise SystemExit('O agente de instalação precisa executar como root.')
    _ensure_dirs()
    try:
        request = json.loads(REQUEST_FILE.read_text(encoding='utf-8'))
    except Exception:
        _set_status('failed', running=False, success=False, error='Solicitação de instalação ausente ou inválida.')
        return 1

    # Segredos deixam de ficar persistidos assim que o agente assume o job.
    try:
        REQUEST_FILE.unlink()
    except FileNotFoundError:
        pass

    token = str(request.get('github_token') or '')
    owner_username = str(request.get('owner_username') or '')
    owner_password = str(request.get('owner_password') or '')
    owner_email = str(request.get('owner_email') or '')
    company = str(request.get('company') or '')

    try:
        if not install_preflight()['apt']:
            raise RuntimeError('Instalação existente detectada. Nova instalação foi abortada antes de qualquer alteração.')

        _set_status('dependencies', running=True)
        _log('Instalando dependências base do Ubuntu...')
        _run(['apt-get', 'update', '-y'])
        _run(['apt-get', 'install', '-y', '--no-install-recommends', 'git', 'nginx', 'docker.io', 'docker-compose-v2', 'curl', 'ca-certificates', 'openssl'])
        _run(['systemctl', 'enable', '--now', 'docker'])

        node_ok = False
        try:
            version = subprocess.check_output(['node', '-p', 'process.versions.node'], text=True).strip()
            node_ok = int(version.split('.')[0]) >= 20
        except Exception:
            node_ok = False
        if not node_ok:
            _log('Node.js 20+ não encontrado. Instalando Node.js 22 LTS via NodeSource...')
            _run(['bash', '-lc', 'curl -fsSL https://deb.nodesource.com/setup_22.x | bash -'])
            _run(['apt-get', 'install', '-y', 'nodejs'])

        if subprocess.run(['id', 'netdesk'], capture_output=True).returncode != 0:
            _run(['useradd', '--system', '--create-home', '--home-dir', '/home/netdesk', '--shell', '/bin/bash', 'netdesk'])
        subprocess.run(['usermod', '-aG', 'docker', 'netdesk'], check=False)

        _set_status('source', running=True)
        _log('Baixando código privado NETDESK e CHAT...')
        _git_clone(NETDESK_REPO, NETDESK_BRANCH, NETDESK_ROOT, token)
        _git_clone(CHAT_REPO, CHAT_BRANCH, CHAT_ROOT, token)
        _run(['chown', '-R', 'netdesk:netdesk', str(NETDESK_ROOT), str(CHAT_ROOT)])

        pg_password = secrets.token_urlsafe(32)
        jwt_secret = secrets.token_hex(48)
        chat_password = secrets.token_urlsafe(32)
        chat_internal_key = secrets.token_hex(40)

        compose = NETDESK_ROOT / 'postgres/docker-compose.yml'
        compose_text = compose.read_text(encoding='utf-8').replace('POSTGRES_PASSWORD: netdesk123', f'POSTGRES_PASSWORD: {pg_password}')
        _write(compose, compose_text, 0o600)
        _run(['chown', 'netdesk:netdesk', str(compose)])

        backend_env = f'''NODE_ENV=production\nTZ=America/Sao_Paulo\nPORT=3333\nDB_HOST=127.0.0.1\nDB_PORT=5432\nDB_NAME=netdesk\nDB_USER=netdesk\nDB_PASSWORD={pg_password}\nJWT_SECRET={jwt_secret}\nNETDESK_LICENSE_STATE_PATH=/var/lib/netdesk-license/license-state.json\n'''
        _write(NETDESK_ROOT / 'backend/.env', backend_env, 0o600)
        _run(['chown', 'netdesk:netdesk', str(NETDESK_ROOT / 'backend/.env')])

        _set_status('database', running=True)
        _log('Subindo PostgreSQL NETDESK...')
        _run(['docker', 'compose', '-f', str(compose), 'up', '-d'])
        _wait_postgres()

        _set_status('netdesk', running=True)
        _log('Instalando backend e frontend NETDESK...')
        _run(['runuser', '-u', 'netdesk', '--', 'bash', '-lc', 'cd /opt/netdesk/backend && npm ci --omit=dev'])
        _run(['runuser', '-u', 'netdesk', '--', 'bash', '-lc', 'cd /opt/netdesk/frontend && npm install --no-audit --no-fund && npm run build'])

        service = '''[Unit]\nDescription=NETDESK Backend\nAfter=network-online.target docker.service\nWants=network-online.target docker.service\n\n[Service]\nType=simple\nUser=netdesk\nGroup=netdesk\nWorkingDirectory=/opt/netdesk/backend\nEnvironmentFile=/opt/netdesk/backend/.env\nExecStart=/usr/bin/node /opt/netdesk/backend/src/server.js\nRestart=on-failure\nRestartSec=3\n\n[Install]\nWantedBy=multi-user.target\n'''
        _write(Path('/etc/systemd/system/netdesk-backend.service'), service, 0o644)
        _run(['systemctl', 'daemon-reload'])
        _run(['systemctl', 'enable', '--now', 'netdesk-backend'])

        _log('Criando proprietário inicial system_owner...')
        hash_cmd = "const b=require('/opt/netdesk/backend/node_modules/bcryptjs'); b.hash(process.argv[1],12).then(x=>process.stdout.write(x))"
        password_hash = subprocess.check_output(['node', '-e', hash_cmd, owner_password], text=True).strip()
        sql = f"""
INSERT INTO roles (name, description) VALUES ('system_owner','Proprietário máximo do sistema') ON CONFLICT (name) DO NOTHING;
INSERT INTO users (role_id,name,email,username,password_hash,is_active)
SELECT id,{_sql_literal(owner_username)},{_sql_literal(owner_email)},{_sql_literal(owner_username)},{_sql_literal(password_hash)},true
FROM roles WHERE name='system_owner'
ON CONFLICT (username) DO NOTHING;
"""
        _run(['docker', 'exec', 'netdesk-postgres', 'psql', '-U', 'netdesk', '-d', 'netdesk', '-v', 'ON_ERROR_STOP=1', '-c', sql], secret_values=[password_hash])

        _set_status('chat', running=True)
        _log('Preparando banco e serviço do CHAT...')
        create_role = f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='chat_notifications') THEN CREATE ROLE chat_notifications LOGIN PASSWORD {_sql_literal(chat_password)}; END IF; END $$;"
        _run(['docker', 'exec', 'netdesk-postgres', 'psql', '-U', 'netdesk', '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c', create_role], secret_values=[chat_password])
        db_exists = subprocess.run(['docker', 'exec', 'netdesk-postgres', 'psql', '-U', 'netdesk', '-d', 'postgres', '-tAc', "SELECT 1 FROM pg_database WHERE datname='chat_notifications'"], capture_output=True, text=True).stdout.strip()
        if db_exists != '1':
            _run(['docker', 'exec', 'netdesk-postgres', 'createdb', '-U', 'netdesk', '-O', 'chat_notifications', 'chat_notifications'])

        chat_env = f'''PORT=3340\nDATABASE_URL=postgresql://chat_notifications:{chat_password}@127.0.0.1:5432/chat_notifications\nNETDESK_API_URL=http://127.0.0.1:3333/api\nCHAT_ORIGIN=http://127.0.0.1:8081\nINTERNAL_API_KEY={chat_internal_key}\nVAPID_SUBJECT=mailto:admin@localhost\nVAPID_PUBLIC_KEY=\nVAPID_PRIVATE_KEY=\n'''
        _write(CHAT_ROOT / 'backend/.env', chat_env, 0o600)
        _run(['chown', 'netdesk:netdesk', str(CHAT_ROOT / 'backend/.env')])
        _run(['bash', str(CHAT_ROOT / 'backend/scripts/install-production.sh')])
        _run(['runuser', '-u', 'netdesk', '--', 'bash', '-lc', 'cd /opt/chat && npm install --no-audit --no-fund && npm run build'])

        _set_status('nginx', running=True)
        _log('Configurando acesso inicial por IP...')
        nginx = '''server {\n    listen 80 default_server;\n    server_name _;\n    root /opt/netdesk/www;\n    index index.html;\n\n    location /api/ { proxy_pass http://127.0.0.1:3333/api/; proxy_http_version 1.1; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; }\n    location / { try_files $uri $uri/ /index.html; }\n}\nserver {\n    listen 8081;\n    server_name _;\n    root /opt/chat/dist;\n    index index.html;\n    location /push-api/ { proxy_pass http://127.0.0.1:3340/; proxy_http_version 1.1; proxy_set_header Host $host; }\n    location / { try_files $uri $uri/ /index.html; }\n}\n'''
        _write(Path('/etc/nginx/sites-available/netdesk-appliance-installed'), nginx, 0o644)
        default = Path('/etc/nginx/sites-enabled/default')
        try:
            default.unlink()
        except FileNotFoundError:
            pass
        link = Path('/etc/nginx/sites-enabled/netdesk-appliance-installed')
        try:
            link.unlink()
        except FileNotFoundError:
            pass
        link.symlink_to('/etc/nginx/sites-available/netdesk-appliance-installed')
        _run(['nginx', '-t'])
        _run(['systemctl', 'enable', '--now', 'nginx'])
        _run(['systemctl', 'reload', 'nginx'])

        if (NETDESK_ROOT / 'infra/scripts/install-restore-agent.sh').exists():
            _run(['bash', str(NETDESK_ROOT / 'infra/scripts/install-restore-agent.sh')])

        _set_status('health', running=True)
        _log('Executando health-check final...')
        netdesk_health = subprocess.run(['curl', '-fsS', 'http://127.0.0.1:3333/health'], capture_output=True, text=True)
        chat_health = subprocess.run(['curl', '-fsS', 'http://127.0.0.1:3340/health'], capture_output=True, text=True)
        if netdesk_health.returncode != 0:
            raise RuntimeError('NETDESK backend não respondeu ao health-check.')
        if chat_health.returncode != 0:
            raise RuntimeError('CHAT notifications não respondeu ao health-check.')

        _set_status('completed', running=False, success=True, finished_at=int(time.time()), company=company, owner_username=owner_username)
        _log('Nova instalação concluída com sucesso. NETDESK em :80 e CHAT em :8081 até configurar domínios/SSL.')
        return 0
    except Exception as exc:
        _log(f'ERRO: {exc}')
        _set_status('failed', running=False, success=False, error=str(exc), finished_at=int(time.time()))
        return 1
    finally:
        token = ''
        owner_password = ''


if __name__ == '__main__':
    if '--agent' in sys.argv:
        raise SystemExit(run_agent())
    print(json.dumps(install_status(), ensure_ascii=False, indent=2))
