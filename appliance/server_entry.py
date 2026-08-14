#!/usr/bin/env python3
import ssl
from urllib.parse import urlparse

import server as base
from installer_engine_v2 import install_preflight, install_status, queue_install

INSTALL_UI = r'''
<style>
.install-panel{margin-top:18px}.install-form{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:15px}.install-form .field{margin:0}.install-form .wide{grid-column:1/-1}.install-note{padding:12px;border:1px solid #2c4260;border-radius:11px;background:#0a1625;color:#9fb4ce;font-size:13px;line-height:1.5}.install-log{margin-top:12px;padding:12px;min-height:100px;max-height:260px;overflow:auto;background:#050b12;border:1px solid #22354d;border-radius:11px;color:#b9d3ef;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}.install-status{margin-top:12px;padding:11px 12px;border-radius:11px;border:1px solid #29405e;background:#0b1523}.install-status.good{border-color:#2f6c4b;background:#0d281c}.install-status.bad{border-color:#6c3141;background:#2b1119}.install-buttons{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}@media(max-width:850px){.install-form{grid-template-columns:1fr}.install-form .wide{grid-column:auto}}
</style>
<script>
window.addEventListener('DOMContentLoaded', async () => {
  const actions = document.querySelectorAll('.action');
  const installAction = actions && actions[0];
  if (!installAction || document.getElementById('installPanel')) return;
  const oldButton = installAction.querySelector('button');
  if (oldButton) {
    oldButton.disabled = false;
    oldButton.textContent = 'Abrir instalador';
    oldButton.onclick = () => document.getElementById('installPanel')?.scrollIntoView({behavior:'smooth'});
  }

  const panel = document.createElement('section');
  panel.id = 'installPanel';
  panel.className = 'panel install-panel';
  panel.innerHTML = `
    <h3>Nova instalação</h3>
    <div class="muted">Constrói uma instalação limpa de NETDESK + CHAT. Este motor se recusa a sobrescrever uma instalação existente.</div>
    <div id="installEligibility" class="install-status">Verificando esta máquina...</div>
    <div class="install-form">
      <div class="field"><label>Empresa</label><input id="installCompany" placeholder="Nome da empresa" /></div>
      <div class="field"><label>Usuário proprietário do NETDESK</label><input id="installOwner" placeholder="Ex.: administrador" autocomplete="username" /></div>
      <div class="field"><label>E-mail do proprietário</label><input id="installOwnerEmail" type="email" placeholder="Opcional" /></div>
      <div class="field"><label>Senha do proprietário do NETDESK</label><input id="installOwnerPassword" type="password" autocomplete="new-password" placeholder="Mínimo 10 caracteres" /></div>
      <div class="field wide"><label>Senha do usuário Linux netdesk</label><input id="installLinuxPassword" type="password" autocomplete="new-password" placeholder="Usuário da máquina com sudo/root, mínimo 10 caracteres" /></div>
      <div class="field"><label>Domínio do NETDESK</label><input id="installNetdeskDomain" placeholder="netdesk.empresa.com.br" /></div>
      <div class="field"><label>Domínio do CHAT</label><input id="installChatDomain" placeholder="chat.empresa.com.br" /></div>
      <div class="field wide"><label>Token GitHub temporário</label><input id="installGithubToken" type="password" autocomplete="off" placeholder="Acesso somente aos repos privados NETDESK e CHAT" /></div>
      <div class="wide install-note">O instalador cria o usuário Linux <strong>netdesk</strong> com senha e acesso ao grupo <strong>sudo</strong>, além do grupo Docker. Ele será o usuário administrativo da máquina e também o dono de /opt/netdesk e /opt/chat. Os dois domínios são configurados no Nginx; se o DNS já apontar para este servidor, o HTTPS é emitido automaticamente.</div>
      <div class="wide install-note">O token GitHub é usado apenas pelo job para baixar os dois repositórios privados. Ele não é gravado em logs e o arquivo transitório é apagado assim que o agente privilegiado assume a instalação.</div>
    </div>
    <div class="install-buttons"><button id="installStartBtn" class="primary">Iniciar instalação</button><button id="installRefreshBtn" class="ghost">Atualizar estado</button></div>
    <div id="installStage" class="install-status">Instalador ocioso.</div>
    <pre id="installLog" class="install-log">[instalador] aguardando job...</pre>`;
  const licensePanel = document.querySelector('.license-panel');
  if (licensePanel) licensePanel.parentNode.insertBefore(panel, licensePanel); else document.getElementById('app').appendChild(panel);

  const stageLabels = {idle:'Ocioso',queued:'Na fila',dependencies:'Dependências',source:'Código fonte',database:'PostgreSQL',netdesk:'NETDESK',chat:'CHAT',nginx:'Nginx',health:'Health-check',domains:'Domínios e HTTPS',completed:'Concluído',failed:'Falhou'};
  let pollTimer = null;

  async function loadInstallStatus(){
    try{
      const d=await request('/api/install/status');
      const p=d.preflight||{};
      const eligible=Boolean(p.apt);
      const eligibility=document.getElementById('installEligibility');
      eligibility.className='install-status '+(eligible?'good':'bad');
      eligibility.textContent=eligible?'Máquina apta para Nova instalação.':'Nova instalação bloqueada: NETDESK/CHAT existente ou requisito de segurança pendente.';
      document.getElementById('installStartBtn').disabled=!eligible||Boolean(d.running);
      const s=document.getElementById('installStage');
      s.className='install-status '+(d.success?'good':(d.stage==='failed'?'bad':''));
      s.textContent=`Etapa: ${stageLabels[d.stage]||d.stage||'desconhecida'}${d.error?' • '+d.error:''}`;
      document.getElementById('installLog').textContent=(d.log_tail||[]).join('\n')||'[instalador] sem logs ainda.';
      if(d.running&&!pollTimer) pollTimer=setInterval(loadInstallStatus,2000);
      if(!d.running&&pollTimer){clearInterval(pollTimer);pollTimer=null;}
      return d;
    }catch(e){log(`Instalador: ${e.message}`);}
  }

  document.getElementById('installRefreshBtn').onclick=loadInstallStatus;
  document.getElementById('installStartBtn').onclick=async()=>{
    const payload={
      company:document.getElementById('installCompany').value.trim(),
      owner_username:document.getElementById('installOwner').value.trim(),
      owner_email:document.getElementById('installOwnerEmail').value.trim(),
      owner_password:document.getElementById('installOwnerPassword').value,
      linux_password:document.getElementById('installLinuxPassword').value,
      netdesk_domain:document.getElementById('installNetdeskDomain').value.trim(),
      chat_domain:document.getElementById('installChatDomain').value.trim(),
      github_token:document.getElementById('installGithubToken').value.trim(),
    };
    if(!confirm('Iniciar Nova instalação nesta máquina? O instalador só prossegue se não houver NETDESK/CHAT existente.')) return;
    try{
      await request('/api/install/start',{method:'POST',body:JSON.stringify(payload)});
      document.getElementById('installGithubToken').value='';
      document.getElementById('installOwnerPassword').value='';
      document.getElementById('installLinuxPassword').value='';
      log('Job de Nova instalação iniciado.');
      await loadInstallStatus();
    }catch(e){log(`Instalação não iniciada: ${e.message}`);await loadInstallStatus();}
  };
  await loadInstallStatus();
});
</script>
'''


class Handler(base.Handler):
    server_version = 'NETDESK-Appliance/0.8'

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/':
            try:
                html = base.INDEX.read_text(encoding='utf-8')
                body = html.replace('</body>', base.LICENSE_IMPORT_UI + INSTALL_UI + '\n</body>').encode('utf-8')
            except Exception:
                return self.send_json(500, {'error': 'interface_missing'})
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/api/install/preflight':
            if not self.authenticated():
                return self.send_json(401, {'error': 'authentication_required'})
            return self.send_json(200, install_preflight())

        if path == '/api/install/status':
            if not self.authenticated():
                return self.send_json(401, {'error': 'authentication_required'})
            return self.send_json(200, install_status())

        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/install/start':
            if not self.authenticated():
                return self.send_json(401, {'error': 'authentication_required'})
            if not base.operational_license():
                return self.send_json(423, {'error': 'license_inactive', 'message': 'Ative ou renove a licença da appliance antes de iniciar uma nova instalação.'})
            try:
                return self.send_json(202, {'ok': True, 'install': queue_install(self.body_json())})
            except ValueError as exc:
                return self.send_json(400, {'error': 'install_not_started', 'message': str(exc)})
            except Exception as exc:
                print(f'[install] queue failed: {exc}')
                return self.send_json(500, {'error': 'install_start_failed', 'message': 'Não foi possível iniciar a Nova instalação.'})
        return super().do_POST()


def main():
    for required in (base.INDEX, base.SESSION_SECRET, base.TLS_CERT, base.TLS_KEY):
        if not required.exists():
            raise SystemExit(f'Arquivo obrigatório ausente: {required}')
    if not base.password_configured() and not base.INITIAL_CODE.exists():
        raise SystemExit(f'Arquivo obrigatório ausente: {base.INITIAL_CODE}')
    base.STATE.mkdir(parents=True, exist_ok=True)
    base.installation_id()
    base.license_state()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(base.TLS_CERT), str(base.TLS_KEY))
    server = base.SecureThreadingHTTPServer((base.HOST, base.PORT), Handler, context)
    print(f'[NETDESK Appliance] HTTPS ativo em 0.0.0.0:{base.PORT}')
    server.serve_forever()


if __name__ == '__main__':
    main()
