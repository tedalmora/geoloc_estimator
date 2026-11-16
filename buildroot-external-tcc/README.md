# buildroot-external-tcc

Árvore `br2-external` mínima para gerar uma imagem Buildroot do projeto (Raspberry Pi 3
Model B Rev 1.2 + FN990 por USB).

> **Estado:** a sincronização local do pacote tem teste com `make` e `rsync`.
> A compilação completa da imagem, o boot e a validação no Raspberry Pi/FN990
> continuam pendentes.

## Conteúdo

| Caminho | Função |
|---|---|
| `external.desc`, `external.mk`, `Config.in` | registro da árvore (`BR2_EXTERNAL_TCC_PATH`) |
| `configs/tcc_rpi3_fn990_defconfig` | `raspberrypi3_defconfig` do Buildroot 2026.08.1 + Python 3, `python3-sqlite`, `python-serial`, `usbutils`, hotspot, Dropbear e o pacote |
| `package/tcc-positioning/` | instala em `/opt/tcc` somente `hal/`, `acquisition/`, `coletor/`, `estimador/`, `experimental/`, `campaign_tools/`, `runtime/`, `software_version.py` e `VERSION` |
| `board/tcc-rpi3/linux-usb-serial.fragment` | só o núcleo USB serial (`CONFIG_USB_SERIAL`) |
| `board/tcc-rpi3/rootfs-overlay/` | exemplos `/etc/tcc/*.example`, DHCP `/etc/dnsmasq.conf`, `S02tcc-dirs`, `S40tcc-hotspot`, `S98tcc-runtime` e `/usr/bin/tcc-*` |
| `board/tcc-rpi3/post-build.sh` | valida as credenciais locais, injeta hostapd e hash root, e preserva `runtime.conf` existente |
| `local-secrets/` | instruções e exemplos versionáveis; credenciais reais ficam em dois arquivos ignorados individualmente |

Não são instalados: `.git`, `venv`, `__pycache__`, `tests`, fixtures, documentos (`*.md`),
`deprecated/`, bancos, modelos, cadastros reais (`network_registry.json`), logs, CSVs e
`pytest_*.txt`. Não há ambiente virtual nem `pip` no alvo.

## Uso previsto (Ubuntu nativo)

```bash
git clone https://github.com/buildroot/buildroot.git && cd buildroot
git checkout 2026.08.1
make BR2_EXTERNAL=/caminho/para/codigo/buildroot-external-tcc tcc_rpi3_fn990_defconfig
make
```

O kernel `21b410140c47ffab5668399f6f143c7d7b935c8b` (série 6.12) é mantido porque é exatamente
o kernel fixado no `configs/raspberrypi3_defconfig` oficial da versão 2026.08.1, e a configuração
exige hashes de download. Não foi atualizado apenas para acompanhar este guia.

O pacote usa o próprio repositório como fonte (`SITE_METHOD = local`, pasta acima de
`buildroot-external-tcc/`); portanto a árvore deve permanecer dentro do repositório.
O `bcm2709_defconfig` já habilita `CONFIG_BRCMFMAC=m`; o defconfig seleciona
`BR2_PACKAGE_LINUX_FIRMWARE_BRCM_BCM43XXX`, além dos símbolos confirmados de hostapd,
dnsmasq e Dropbear. A configuração DHCP de dnsmasq escuta somente em `wlan0`, desativa
DNS e oferece `192.168.50.10`–`192.168.50.30`; não há NAT, ponte ou encaminhamento.

## Credenciais locais e build headless

Antes de compilar no Ubuntu, consulte [`local-secrets/README.md`](local-secrets/README.md).
Crie `local-secrets/hostapd.conf` com a senha WPA2 e `local-secrets/root_password_hash` com o
resultado local de `openssl passwd -6`. Ambos devem ter modo 0600 e são os únicos arquivos
ignorados nesta pasta; os `.example` e o README permanecem versionáveis.

O post-build oficial é executado junto com o script upstream do Raspberry Pi. Ele falha se os
arquivos locais estiverem ausentes, vazios ou inválidos, copia a configuração de hostapd com
modo 0600 e troca somente o segundo campo da entrada `root` em `/etc/shadow`, preservando os
demais campos e aplicando modo 0600. O hash não é enviado à saída do build. O arquivo
`/etc/tcc/runtime.conf` é copiado do exemplo somente se ainda não existir; seu padrão é manual.

No boot, `S40tcc-hotspot` configura `wlan0` e inicia hostapd uma única vez. O pacote hostapd do
Buildroot 2026.08.1 não instala init script, portanto este é o único responsável por hostapd.
O init oficial `S80dnsmasq` inicia dnsmasq depois da configuração de `wlan0`, e `S50dropbear`
inicia Dropbear uma única vez. A opção oficial de login root está habilitada; o hash injetado
permite autenticação por senha, sem `authorized_keys` e sem login sem senha.

O primeiro boot não requer monitor, teclado ou configuração no equipamento: associe-se ao AP
`TCC-RPI3`, receba DHCP e conecte por `ssh root@192.168.50.1` usando as credenciais fornecidas
antes do build. O FN990 pode estar desconectado; hotspot e SSH não dependem dele.

As exclusões da sincronização são opções `--exclude` passadas ao `rsync`. O teste
`tests/test_buildroot_package.py` verifica que runtime, campaign_tools e os arquivos
de serviço/configuração fazem parte do pacote/overlay e, quando `make` e `rsync`
estão disponíveis, executa a sincronização com arquivos de exemplo.

## Diretórios na imagem

`/opt/tcc`, `/etc/tcc`, `/data/tcc`, `/data/tcc/db`, `/data/tcc/models`, `/data/tcc/registry`,
`/data/tcc/runtime` e `/var/log/tcc`. `/data` é um diretório da raiz ext4 gravável no cartão SD
(sem partição separada). `S02tcc-dirs` só cria diretórios e ajusta permissões; **não inicia coleta**.

## Comandos no alvo

`tcc-diagnose` (somente leitura: não acessa o modem nem cria banco), `tcc-hal`, `tcc-collect`,
`tcc-live`, `tcc-ta` e `tcc-angular`. Os wrappers carregam `/etc/tcc/tcc.env`, definem
`PYTHONPATH=/opt/tcc` e executam `python3 -m <módulo> "$@"`. Não injetam `--port`: use, por exemplo,
`tcc-hal --port "$TCC_AT_PORT" alive` depois de preencher `TCC_AT_PORT`. A coleta é sempre manual
(`tcc-collect ...`).

## Hotspot, SSH e runtime

`S40tcc-hotspot` configura o Wi-Fi integrado (`wlan0`) como AP administrativo `TCC-RPI3` em
`192.168.50.1/24`; o init oficial `S80dnsmasq` atende somente DHCP nessa interface, e o init
oficial `S50dropbear` inicia SSH por senha em todos os boots, sem depender do FN990. O FN990
permanece dedicado à aquisição celular. Não há DNS, NAT, bridge ou encaminhamento. As duas
credenciais são criadas localmente antes do build e nunca são versionadas; sem elas, o post-build
interrompe a geração da imagem.

O post-build copia `/etc/tcc/runtime.conf.example` para `/etc/tcc/runtime.conf` somente se o
arquivo ainda não existir. O padrão é `TCC_MODE=manual`; o serviço `S98tcc-runtime` registra que
não iniciou runtime e sai sem inferência; hotspot, SSH e `campaign_tools` continuam disponíveis.
Com `TCC_MODE=runtime`, o serviço valida a configuração em cada boot, inicia `runtime.daemon` e
aguarda o FN990. Antes da espera, o daemon substitui atomicamente qualquer posição anterior em
`/data/tcc/runtime/latest_position.json` por `unavailable` com coordenadas nulas. O histórico
registra início, mudanças de estado e tentativas, não cada espera curta. O daemon reutiliza
`estimador.live` e a aquisição existente.

Teste manual em Raspberry Pi OS ou Buildroot, com `runtime.conf` em modo runtime e FN990 pronto:

```bash
PYTHONPATH=/opt/tcc python3 -m runtime.daemon --config /etc/tcc/runtime.conf --once
```

Pare o serviço Buildroot com `/etc/init.d/S98tcc-runtime stop`. No Raspberry Pi OS, o mesmo
módulo pode ser executado diretamente; este repositório não instala uma unidade systemd.

## Pendente

Ubuntu: carregar o defconfig 2026.08.1; compilar; gerar `sdcard.img`; validar os scripts shell,
a instalação integrada do pacote e a presença do driver/firmware no alvo.

Raspberry Pi/FN990: primeiro boot; senha local, hotspot/SSH e persistência em `/data` após reboot;
verificar `wlan0`, hostapd, DHCP e cobertura; `dmesg`/sysfs para VID/PID e driver USB serial do
FN990 (nenhum foi definido aqui, só o núcleo `USB_SERIAL`); identificar a porta AT
(`ttyUSB*` ou `/dev/serial/by-id`), preencher `TCC_PORT` e validar os métodos com o modem.
