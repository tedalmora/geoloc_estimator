# HAL — Camada de Abstração de Hardware

Biblioteca **on-demand** para falar com o modem: você chama um método, ele manda um comando
AT, espera a resposta, e devolve. Sem *loop*, sem estado de "sessão", sem banco de dados —
**não é um daemon**. Quem fica rodando continuamente são o [Coletor](../coletor/README.md)
(campanha de campo) e o `estimador/live.py` (posição ao vivo, ver
[`estimador/README.md`](../estimador/README.md)) -- os dois importam e chamam esta
biblioteca, cada um com sua própria conexão. Ver `planejamento/arquitetura_hal.md` para a
justificativa completa dessa separação.

```
hal/
    models.py         # estruturas de dados que os métodos abaixo devolvem
    modem_driver.py   # o contrato (interface) que todo driver de modem segue
    fn990_driver.py   # implementação real para o Telit Cinterion FN990
    parsing.py        # funções auxiliares de regex/conversão, compartilhadas
    health.py         # monitor de saúde da conexão (usado pelo Coletor e por estimador/live.py)
    cli.py            # utilitário de linha de comando para testar sozinho
```

## O que ele faz / não faz

**Faz:** abre/fecha a porta serial, envia comandos AT, faz o parsing da resposta em
estruturas de dados Python, expõe métodos padronizados independente do modem por baixo.

**Não faz:** não roda continuamente, não grava em banco de dados, não calcula posição —
isso é responsabilidade do Coletor e do Estimador, respectivamente.

## Tabela: cada método e para qual geolocalização ele serve

| Método | Comando AT usado | Serve para | Precisa de rede/SIM? |
|---|---|---|---|
| `connect()` / `close()` | — | gerenciar a conexão serial | Não |
| `send_at()` | qualquer, cru | depuração manual | Não |
| `is_alive()` / `reconnect()` | `AT` | health check (usado pelo Coletor) | Não |
| `get_status()` | `ATI`, `+CGMI/CGMM/CGMR/CGSN`, `+CPIN?`, `+CSQ`, `+COPS?`, `#UPTIME`, `#TEMPSENS` | diagnóstico/manutenção — não é geolocalização em si | Parcial |
| `reboot()` | `AT#REBOOT` | manutenção | Não |
| `get_serving_cell()` | `AT#MONI` (+ `AT#CSURV` para achar o PCI) | **Cell-ID / E-CID** | Sim, p/ valor útil |
| `get_signal_metrics()` | `AT#MONI` | RSRP/RSRQ — alimenta o **Fingerprint**. SINR e Timing Advance ficam sempre `None` neste firmware (ver nota abaixo) | Sim, p/ valor útil |
| `get_neighbor_cells()` | `AT#CSURV` | **Fingerprinting** (células vizinhas) | Sim, p/ valor útil |
| `get_gnss_fix()` | `AT$GPSACP` | *ground truth* (posição real, usada para treinar/avaliar TODOS os métodos) | Não (independente da operadora) |
| `enable_lpp_positioning()` | `AT$LCSLPP=1` | habilita a sessão para **DL-AoD/UL-AoA** | Sim |
| `get_lpp_report()` | `AT$LCSLPP?` | tenta ler o relatório da sessão LPP acima — insumo do método `aoa` do Estimador | Sim |

## Resposta direta: "dá pra fazer todos os tipos de medida, até AoA?"

**Cell-ID, Timing Advance, Fingerprinting e GNSS: sim**, os métodos já existem e já fazem o
parsing (o formato exato de cada resposta ainda precisa ser calibrado com SIM + rede real,
mas a estrutura toda já está pronta e funcional).

**AoA/DL-AoD: parcialmente.** `enable_lpp_positioning()`/`get_lpp_report()` já existem e já
tentam habilitar a sessão e ler uma resposta — mas **quem mede o ângulo é a rede** (via LPP),
não o HAL. Sem SIM + acesso ao core, esse método não tem como ser validado: o HAL pode pedir o
relatório, mas o conteúdo real dele (onde exatamente está o ângulo dentro do texto) só se
descobre testando em campo — exatamente como já aconteceu com os outros comandos confirmados
via `AT+CLAC` (ver `planejamento/guia_comandos_modem.md`). Quando isso acontecer, ajuste as
regex em `get_lpp_report()` (dentro de `fn990_driver.py`) com o texto real recebido.

## Como testar sozinho (sem o Coletor)

```bash
python -m hal.cli --port /dev/ttyUSB2 alive
python -m hal.cli --port /dev/ttyUSB2 status
python -m hal.cli --port /dev/ttyUSB2 signal
python -m hal.cli --port /dev/ttyUSB2 cell
python -m hal.cli --port /dev/ttyUSB2 neighbors
python -m hal.cli --port /dev/ttyUSB2 gnss
python -m hal.cli --port /dev/ttyUSB2 lpp
python -m hal.cli --port /dev/ttyUSB2 reboot
```

## Como ele conversa com o Coletor e com o Estimador

O Coletor faz `from hal.fn990_driver import FN990Driver`, instancia um driver por modem, e
chama os métodos acima em um laço com intervalo configurável — o HAL nunca sabe que isso está
acontecendo, ele só responde uma chamada de cada vez. `estimador/live.py` faz exatamente a
mesma coisa, de forma independente (não pelo Coletor) — ver
[`coletor/README.md`](../coletor/README.md) e [`estimador/README.md`](../estimador/README.md)
para a diferença entre os dois usos (campanha de campo vs. medição ao vivo).

## Quando os outros modems chegarem (EM7565, EM120R-GL)

Crie `EM7565Driver(ModemDriver)`/`EM120RGLDriver(ModemDriver)` em novos arquivos, implementando
o mesmo contrato de `modem_driver.py` (descobrindo o dialeto AT de cada um via `AT+CLAC`,
mesmo processo já usado para o FN990). O Coletor e o Estimador **não precisam mudar nada**.
