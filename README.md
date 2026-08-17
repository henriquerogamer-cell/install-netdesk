# NETDESK Appliance Installer

Bootstrap público e neutro para instalação da NETDESK Appliance em Ubuntu Server 24.04.

## Objetivo

Transformar uma VM Ubuntu Server 24.04 limpa em um ponto de entrada seguro para instalação, recuperação, licença e diagnóstico do NETDESK.

O bootstrap instala apenas a appliance. O NETDESK e o Chat são instalados posteriormente pelo assistente visual.

## Requisitos

- Ubuntu Server 24.04 LTS x86_64
- acesso root/sudo
- acesso à internet
- IP público fixo
- porta TCP 8443 liberada para o primeiro acesso

Para concluir uma instalação NETDESK completa também serão necessários dois domínios/subdomínios apontando para o IP público da VM, um para o NETDESK e outro para o Chat, além das portas TCP 80 e 443 liberadas.

## Instalação

Em uma VM Ubuntu Server 24.04 limpa:

```bash
curl -fsSL "https://raw.githubusercontent.com/henriquerogamer-cell/install-netdesk/main/bootstrap.sh?v=$(date +%s%N)" | sudo bash
```

Ao terminar, o instalador mostra o endereço HTTPS da appliance e o código inicial de acesso.

> O primeiro acesso usa certificado TLS local da appliance. O navegador pode exibir um aviso de certificado até que os domínios definitivos e certificados públicos sejam configurados.

## Segurança

Este repositório é público por projeto e não deve conter segredos. Nunca adicionar aqui:

- PAT do GitHub
- deploy keys privadas
- senhas
- arquivos `.env`
- chaves privadas de licença
- credenciais de clientes

A appliance deve receber credenciais sensíveis somente durante o fluxo autenticado de instalação.
