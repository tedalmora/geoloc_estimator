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
| `configs/tcc_rpi3_fn990_defconfig` | `raspberrypi3_defconfig` (branch 2025.02.x) + Python 3, `python3-sqlite`, `python-serial`, `usbutils` e o pacote |
| `package/tcc-positioning/` | instala em `/opt/tcc` somente `hal/`, `acquisition/`, `coletor/`, `estimador/`, `experimental/`, `campaign_tools/`, `software_version.py` e `VERSION` |
| `board/tcc-rpi3/linux-usb-serial.fragment` | só o núcleo USB serial (`CONFIG_USB_SERIAL`) |
| `board/tcc-rpi3/rootfs-overlay/` | `/etc/tcc/tcc.env`, `/etc/init.d/S02tcc-dirs`, `/usr/bin/tcc-*` |

Não são instalados: `.git`, `venv`, `__pycache__`, `tests`, fixtures, documentos (`*.md`),
`deprecated/`, bancos, modelos, cadastros reais (`network_registry.json`), logs, CSVs e
`pytest_*.txt`. Não há ambiente virtual nem `pip` no alvo.

## Uso previsto (Ubuntu nativo)

```bash
git clone https://github.com/buildroot/buildroot.git && cd buildroot
git checkout 2025.02.x            # ou a tag exata escolhida
make BR2_EXTERNAL=/caminho/para/codigo/buildroot-external-tcc tcc_rpi3_fn990_defconfig
make menuconfig                   # conferir Python 3, SQLite e PySerial
make
```

O pacote usa o próprio repositório como fonte (`SITE_METHOD = local`, pasta acima de
`buildroot-external-tcc/`); portanto a árvore deve permanecer dentro do repositório.
As exclusões da sincronização são opções `--exclude` passadas ao `rsync`. O teste
`tests/test_buildroot_package.py` verifica essas opções e, quando `make` e `rsync`
estão disponíveis, executa a sincronização com arquivos de exemplo.

## Diretórios na imagem

`/opt/tcc`, `/etc/tcc`, `/data/tcc`, `/data/tcc/db`, `/data/tcc/models`, `/data/tcc/registry`,
`/var/log/tcc`. `/data` é um diretório da raiz ext4 gravável no cartão SD (sem partição
separada). `S02tcc-dirs` só cria os diretórios e ajusta permissões; **não inicia coleta**.

## Comandos no alvo

`tcc-diagnose` (somente leitura: não acessa o modem nem cria banco), `tcc-hal`, `tcc-collect`,
`tcc-live`, `tcc-ta` e `tcc-angular`. Os wrappers carregam `/etc/tcc/tcc.env`, definem
`PYTHONPATH=/opt/tcc` e executam `python3 -m <módulo> "$@"`. Não injetam `--port`: use, por exemplo,
`tcc-hal --port "$TCC_AT_PORT" alive` depois de preencher `TCC_AT_PORT`. A coleta é sempre manual
(`tcc-collect ...`).

## Pendente

Ubuntu: carregar o defconfig; `menuconfig`; compilar; gerar `sdcard.img`; validar os scripts
shell e a instalação integrada do pacote; conferir o conteúdo de `/opt/tcc` na imagem.

Raspberry Pi/FN990: primeiro boot; persistência em `/data` após reboot; `lsusb`/`dmesg` para
VID/PID e driver USB serial do FN990 (nenhum foi definido aqui, só o núcleo `USB_SERIAL`);
identificar a porta AT (`ttyUSB*` ou `/dev/serial/by-id`) e preencher `TCC_AT_PORT`; regras
udev/mdev, se necessárias; testes funcionais.
