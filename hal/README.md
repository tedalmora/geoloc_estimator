# HAL — Camada de Abstração de Hardware

Biblioteca sob demanda para falar com o modem. Cada método de leitura envia **um** comando AT,
espera o código final dentro de um prazo e devolve uma leitura tipada com `status` (`ok`,
`empty`, `error`, `timeout`, `malformed`, `unsupported`; no GNSS também `no_fix` e `off`).
Não há laço, sessão nem banco de dados. Quem chama o HAL é a aquisição compartilhada
(Coletor e medição ao vivo).

> Documento de trabalho da Etapa 2 (fase 2). A documentação final será revisada em fase posterior.

```
hal/
    models.py         # observações de célula, chaves de identidade, leituras, AtResponse
    parsing.py        # parsers puros (#MONI, #SERVINFO, #CSURV, $GPSACP, $GPSP?, $LCSLPP?) e cruzamento da servidora
    modem_driver.py   # contrato comum dos drivers
    fn990_driver.py   # transporte AT e comandos do Telit Cinterion FN990
    health.py         # contagem de falhas seguidas para decidir reconexão
    cli.py            # teste manual, sem o Coletor
```

## Identidade de célula

- `radio_key = RAT:ARFCN:PCI` (ex.: `LTE:3375:254`): identidade de rádio, usada nas observações,
  no snapshot, no banco multicélula e nas features do fingerprint.
- `global_cell_key = RAT:PLMN:CELL_ID` (ex.: `LTE:72406:F560E67`): identidade lógica (ECGI/NCGI),
  usada no cadastro de células e no Cell-ID.

Cada chave só existe quando todos os componentes vieram no mesmo registro do modem; não há
chave parcial nem substituição de campos. A RAT é a da célula (`LTE`/`NR`). O modo de acesso
da leitura (`LTE`, `NR_NSA`, `NR_SA`, `UNKNOWN`) é guardado à parte. Cell-ID e TAC ficam em
hexadecimal maiúsculo; o `#CSURV` os informa em decimal e o parser converte.

## Métodos

| Método | Comando AT | Devolve | Observação |
|---|---|---|---|
| `connect()` | `AT` | — | descarta sobras da porta e exige `OK`; senão `ConnectionError` |
| `send_at(cmd, timeout, drain_after)` | qualquer | `AtResponse` | prazo total; terminador `<CR>` |
| `is_alive()` / `reconnect()` | `AT` | `bool` / — | health check |
| `get_status()` | `+CGMI`, `+CGMM`, `+CGMR`, `+CGSN`, `+CPIN?`, `+CSQ`, `+COPS?`, `#UPTIME`, `#TEMPSENS` | `ModemStatus` | texto cru de cada comando |
| `prepare_for_collection()` | `$GPSP?` e, só se desligado, `$GPSP=1` | `PrepareResult` | `$GPSP=1` apaga a memória do GNSS (manual p. 763) |
| `read_serving_cell()` | `#MONI` | `MoniReading` | servidora LTE e/ou NR; NR_NSA só com `NR_STATE` = 2 |
| `read_serving_info()` | `#SERVINFO` | `ServInfoReading` | traz o PLMN da servidora |
| `scan_cells()` | `#CSURV` | `CsurvReading` | até 3 min; todas as células LTE/NR, inclusive de outras operadoras e `CELL_FORBIDDEN` |
| `read_gnss()` | `$GPSACP` | `GnssReading` | fix 2D/3D com HDOP, satélites e UTC; fix 0/1 = sem posição |
| `query_lpp_config()` | `$LCSLPP?` | `LppConfigProbe` | sonda experimental, só leitura: texto cru e status |
| `reboot()` | `#REBOOT` | — | fecha a porta local |

O `#MONI` e o `#SERVINFO` não informam o PCI da servidora LTE. `parsing.resolve_serving_cell()`
(função pura) cruza o Cell-ID da servidora com os registros completos do `#CSURV`. Se houver
exatamente um registro compatível, as duas chaves vêm desse registro. Caso contrário,
`radio_key` fica vazia; o PCI não é adivinhado.

## O que o HAL não faz

- Não grava configuração persistente. Não há escrita de `AT$LCSLPP` (o manual indica
  "Setting saved: Auto"), e o `#MONI` não é reconfigurado: o formato de célula adjacente vira
  `malformed`.
- Não mede ângulo nem RSTD: o manual não documenta comando AT que devolva DL-AoD, UL-AoA ou RSTD.
- Não lê nem converte Timing Advance. O `#TAINFO` não existe no firmware de campo e o manual
  não documenta a unidade.
- Não calcula posição e não grava banco.

## Teste manual

```bash
python -m hal.cli --port /dev/ttyUSB2 alive
python -m hal.cli --port /dev/ttyUSB2 status
python -m hal.cli --port /dev/ttyUSB2 prepare
python -m hal.cli --port /dev/ttyUSB2 moni
python -m hal.cli --port /dev/ttyUSB2 servinfo
python -m hal.cli --port /dev/ttyUSB2 csurv
python -m hal.cli --port /dev/ttyUSB2 gnss
python -m hal.cli --port /dev/ttyUSB2 lpp-config
python -m hal.cli --port /dev/ttyUSB2 reboot
```

## Testes automatizados

A partir de `codigo/`, com `requirements-dev.txt` instalado: `python -m pytest tests/test_hal.py`.
As respostas usadas estão em `tests/fixtures/at/`, cada uma com origem declarada no cabeçalho.

## Outros modems (EM7565, EM120R-GL)

Um driver novo implementa o contrato de `modem_driver.py`, com o dialeto AT de cada modem
levantado via `AT+CLAC`. Para isso, ele deve devolver as mesmas leituras de `models.py`.
