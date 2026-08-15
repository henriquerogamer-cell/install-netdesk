#!/usr/bin/env python3
import os
import subprocess
import sys

import installer_engine_v2 as v2


def prepare_linux_admin(password):
    v2.legacy._log('Preparando usuário Linux administrativo netdesk...')
    subprocess.run(['apt-get', '-o', 'DPkg::Lock::Timeout=300', 'update', '-y'], check=True)
    subprocess.run([
        'apt-get', '-o', 'DPkg::Lock::Timeout=300', 'install', '-y', '--no-install-recommends',
        'sudo', 'build-essential'
    ], check=True)

    if subprocess.run(['id', 'netdesk'], capture_output=True).returncode != 0:
        subprocess.run([
            'useradd', '--create-home', '--home-dir', '/home/netdesk',
            '--shell', '/bin/bash', 'netdesk'
        ], check=True)

    subprocess.run(['usermod', '-s', '/bin/bash', 'netdesk'], check=True)
    subprocess.run(['usermod', '-aG', 'sudo', 'netdesk'], check=True)

    proc = subprocess.run(['chpasswd'], input=f'netdesk:{password}\n', text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError('Não foi possível definir a senha do usuário Linux netdesk.')

    groups = subprocess.check_output(['id', '-nG', 'netdesk'], text=True).split()
    if 'sudo' not in groups:
        raise RuntimeError('O usuário Linux netdesk foi criado, mas não recebeu privilégio sudo.')


v2._prepare_linux_admin = prepare_linux_admin

if __name__ == '__main__':
    if '--agent' in sys.argv:
        raise SystemExit(v2.run_agent())
    print(v2.json.dumps(v2.install_status(), ensure_ascii=False, indent=2))
