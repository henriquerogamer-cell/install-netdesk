#!/usr/bin/env python3
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import installer_engine as legacy

STATE = Path('/var/lib/netdesk-appliance')
INSTALL_ROOT = STATE / 'install'
REQUEST_FILE = INSTALL_ROOT / 'request.json'
STATUS_FILE = INSTALL_ROOT / 'status.json'

DOMAIN_RE = re.compile(r'^(?=.{1,253}$)(?!-)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$')


def _atomic_json(path, value):
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _normalize_domain(value):
    value = str(value or '').strip().lower()
    if value.startswith('http://') or value.startswith('https://'):
        raise ValueError('Informe somente o domínio, sem http:// ou https://.')
    value = value.rstrip('.')
    if not DOMAIN_RE.fullmatch(value):
        raise ValueError(f'Domínio inválido: {value or "vazio"}.')
    return value


def install_preflight():
    return legacy.install_preflight()


def install_status():
    return legacy.install_status()


def queue_install(payload):
    preflight = install_preflight()
    if not preflight['apt']:
        raise ValueError('Esta máquina não está apta para Nova instalação. O instalador nunca sobrescreve uma instalação existente.')

    token = str(payload.get('github_token') or '').strip()
    owner_username = str(payload.get('owner_username') or '').strip()
    owner_password = str(payload.get('owner_password') or '')
    owner_email = str(payload.get('owner_email') or '').strip()
    company = str(payload.get('company') or '').strip()
    linux_password = str(payload.get('linux_password') or '')
    netdesk_domain = _normalize_domain(payload.get('netdesk_domain'))
    chat_domain = _normalize_domain(payload.get('chat_domain'))

    if netdesk_domain == chat_domain:
        raise ValueError('Os domínios do NETDESK e do CHAT precisam ser diferentes.')
    if len(linux_password) < 10 or len(linux_password) > 128:
        raise ValueError('A senha do usuário Linux netdesk deve ter entre 10 e 128 caracteres.')
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
        'linux_password': linux_password,
        'netdesk_domain': netdesk_domain,
        'chat_domain': chat_domain,
        'created_at': int(time.time()),
    }
    _atomic_json(REQUEST_FILE, request)
    _atomic_json(STATUS_FILE, {
        'stage': 'queued',
        'running': True,
        'success': False,
        'started_at': int(time.time()),
        'company': company,
        'netdesk_domain': netdesk_domain,
        'chat_domain': chat_domain,
    })
    legacy.LOG_FILE.write_text('[INSTALL] Job recebido pela appliance.\n', encoding='utf-8')
    os.chmod(legacy.LOG_FILE, 0o600)

    result = subprocess.run(['systemctl', 'start', 'netdesk-install-agent.service'], capture_output=True, text=True)
    if result.returncode != 0:
        try:
            REQUEST_FILE.unlink()
        except FileNotFoundError:
            pass
        _atomic_json(STATUS_FILE, {'stage': 'failed', 'running': False, 'success': False, 'error': 'Falha ao iniciar agente privilegiado.'})
        raise ValueError('Não foi possível iniciar o agente de instalação.')
    return install_status()


def _prepare_linux_admin(password):
    legacy._log('Preparando usuário Linux administrativo netdesk...')
    subprocess.run(['apt-get', 'update', '-y'], check=True)
    subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends', 'sudo'], check=True)

    if subprocess.run(['id', 'netdesk'], capture_output=True).returncode != 0:
        subprocess.run([
            'useradd', '--create-home', '--home-dir', '/home/netdesk',
            '--shell', '/bin/bash', 'netdesk'
        ], check=True)

    # Garante que não fique marcado como usuário de sistema/sem login utilizável.
    subprocess.run(['usermod', '-s', '/bin/bash', 'netdesk'], check=True)
    subprocess.run(['usermod', '-aG', 'sudo,docker', 'netdesk'], check=False)

    proc = subprocess.run(['chpasswd'], input=f'netdesk:{password}\n', text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError('Não foi possível definir a senha do usuário Linux netdesk.')

    # Ubuntu usa o grupo sudo para delegar privilégio administrativo com senha.
    test = subprocess.run(['getent', 'group', 'sudo'], capture_output=True, text=True)
    if test.returncode != 0:
        raise RuntimeError('Grupo sudo não está disponível no sistema.')


def _resolve_ips(domain):
    try:
        return sorted({item[4][0] for item in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM) if item[0] == socket.AF_INET})
    except Exception:
        return []


def _public_ip():
    try:
        return subprocess.check_output(['curl', '-4fsS', '--max-time', '8', 'https://api.ipify.org'], text=True).strip()
    except Exception:
        return ''


def _configure_domains(netdesk_domain, chat_domain, owner_email):
    legacy._set_status('domains', running=True)
    legacy._log(f'Configurando domínios {netdesk_domain} e {chat_domain}...')

    nginx = f'''server {{\n    listen 80;\n    server_name {netdesk_domain};\n    root /opt/netdesk/www;\n    index index.html;\n    location /api/ {{ proxy_pass http://127.0.0.1:3333/api/; proxy_http_version 1.1; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme; }}\n    location / {{ try_files $uri $uri/ /index.html; }}\n}}\n\nserver {{\n    listen 80;\n    server_name {chat_domain};\n    root /opt/chat/dist;\n    index index.html;\n    location /push-api/ {{ proxy_pass http://127.0.0.1:3340/; proxy_http_version 1.1; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme; }}\n    location /api/ {{ proxy_pass http://127.0.0.1:3333/api/; proxy_http_version 1.1; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme; }}\n    location / {{ try_files $uri $uri/ /index.html; }}\n}}\n'''
    site = Path('/etc/nginx/sites-available/netdesk-domains')
    legacy._write(site, nginx, 0o644)
    link = Path('/etc/nginx/sites-enabled/netdesk-domains')
    try:
        link.unlink()
    except FileNotFoundError:
        pass
    link.symlink_to(site)

    old = Path('/etc/nginx/sites-enabled/netdesk-appliance-installed')
    try:
        old.unlink()
    except FileNotFoundError:
        pass

    legacy._run(['nginx', '-t'])
    legacy._run(['systemctl', 'reload', 'nginx'])

    public_ip = _public_ip()
    nd_ips = _resolve_ips(netdesk_domain)
    chat_ips = _resolve_ips(chat_domain)
    dns_ok = bool(public_ip and public_ip in nd_ips and public_ip in chat_ips)

    if not dns_ok:
        legacy._log(f'DNS ainda não aponta os dois domínios para {public_ip or "o IP público"}. HTTP foi configurado; SSL ficou pendente.')
        return {'ssl': False, 'dns_ok': False, 'public_ip': public_ip, 'netdesk_ips': nd_ips, 'chat_ips': chat_ips}

    legacy._log('DNS validado. Emitindo HTTPS com Certbot...')
    legacy._run(['apt-get', 'install', '-y', '--no-install-recommends', 'certbot', 'python3-certbot-nginx'])
    email = owner_email if owner_email and '@' in owner_email else 'admin@localhost.invalid'
    args = ['certbot', '--nginx', '--non-interactive', '--agree-tos', '--redirect', '-d', netdesk_domain, '-d', chat_domain]
    if email.endswith('.invalid'):
        args.append('--register-unsafely-without-email')
    else:
        args.extend(['--email', email])
    legacy._run(args)
    return {'ssl': True, 'dns_ok': True, 'public_ip': public_ip, 'netdesk_ips': nd_ips, 'chat_ips': chat_ips}


def run_agent():
    if os.geteuid() != 0:
        raise SystemExit('O agente de instalação precisa executar como root.')

    try:
        request = json.loads(REQUEST_FILE.read_text(encoding='utf-8'))
    except Exception:
        legacy._set_status('failed', running=False, success=False, error='Solicitação de instalação ausente ou inválida.')
        return 1

    linux_password = str(request.get('linux_password') or '')
    netdesk_domain = str(request.get('netdesk_domain') or '')
    chat_domain = str(request.get('chat_domain') or '')
    owner_email = str(request.get('owner_email') or '')

    # O motor legado precisa ler o request depois, então primeiro preparamos apenas a conta Linux.
    try:
        _prepare_linux_admin(linux_password)
    except Exception as exc:
        legacy._log(f'ERRO ao preparar usuário Linux: {exc}')
        legacy._set_status('failed', running=False, success=False, error=str(exc), finished_at=int(time.time()))
        try:
            REQUEST_FILE.unlink()
        except FileNotFoundError:
            pass
        return 1

    result = legacy.run_agent()
    if result != 0:
        return result

    try:
        domain_result = _configure_domains(netdesk_domain, chat_domain, owner_email)
        legacy._set_status(
            'completed', running=False, success=True, finished_at=int(time.time()),
            netdesk_domain=netdesk_domain, chat_domain=chat_domain,
            ssl_enabled=bool(domain_result.get('ssl')),
            dns_ready=bool(domain_result.get('dns_ok')),
        )
        if domain_result.get('ssl'):
            legacy._log(f'Instalação concluída. NETDESK: https://{netdesk_domain} | CHAT: https://{chat_domain}')
        else:
            legacy._log(f'Instalação concluída com SSL pendente. Domínios configurados: {netdesk_domain} e {chat_domain}.')
        return 0
    except Exception as exc:
        legacy._log(f'ERRO ao configurar domínios/SSL: {exc}')
        legacy._set_status('failed', running=False, success=False, error=str(exc), finished_at=int(time.time()))
        return 1
    finally:
        linux_password = ''


if __name__ == '__main__':
    if '--agent' in sys.argv:
        raise SystemExit(run_agent())
    print(json.dumps(install_status(), ensure_ascii=False, indent=2))
