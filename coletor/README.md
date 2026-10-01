# Coletor — grava snapshots da aquisição no banco v2

> Documento de trabalho da Etapa 2 (fase 4). A documentação final será revisada em fase posterior.

O Coletor executa sessões de coleta. Cada ciclo é um snapshot da aquisição compartilhada
([`acquisition/snapshot.py`](../acquisition/snapshot.py)): `#MONI`, `#SERVINFO`, 1 `#CSURV`
e `$GPSACP`. O snapshot é gravado no SQLite com status, motivos, latências, todas as células
observadas e as respostas cruas. É síncrono: 1 processo, 1 modem, 1 conexão SQLite.

```
coletor/
    db.py           # schema v2 + gravação (foreign keys ligadas, user_version = 2, horários em UTC)
    daemon.py       # CLI: status | once | continuous | point-add | point-list
    export_csv.py   # export completo em CSV + metadata.json, e restauração num banco novo
    migrate.py      # inspeção e migração NÃO destrutiva de bancos v1 para v2
```

## Sessões, pontos de referência e rotas

Toda sessão de coleta exige `--reference-point NOME` (medição parada num ponto) **ou**
`--route NOME` (medição em movimento). Ponto inexistente é criado sem coordenada, com aviso.
Para registrar a coordenada do ponto, use `point-add`. A referência de posição segue esta
prioridade:

1. coordenada cadastrada do ponto (`point-add`, com a origem da coordenada);
2. mediana das leituras GNSS feitas no ponto, com a dispersão (calculada na análise);
3. leitura GNSS isolada, só como dado auxiliar.

Cada medição guarda o GNSS do modem com tipo de fix, HDOP, número de satélites e hora UTC.

A sessão termina sempre com `status` `completed`, `interrupted` (SIGINT/SIGTERM ou Ctrl+C) ou
`error` (a exceção fica em `end_reason`), além de `ended_at`, mesmo quando há exceção. Toda
tentativa vira uma linha em `measurements`, inclusive as inválidas (`snapshot_status` e
`reasons_json`). Os `health_checks` ficam ligados à sessão e registram início, fim, falhas,
recuperações, reconexões e uma checagem periódica (`--health-interval`).

## Comandos

```bash
# status do modem (não grava nada)
python -m coletor.daemon status --port /dev/ttyUSB2

# cadastrar a coordenada de um ponto (origem: levantamento, rtk, planta...)
python -m coletor.daemon point-add --db radiomapa_v2.db P01 --lat -26.90012 --lon -49.07034 \
    --coord-source levantamento --uncertainty-m 0.5
python -m coletor.daemon point-list --db radiomapa_v2.db

# 1 snapshot numa sessão nova
python -m coletor.daemon once --port /dev/ttyUSB2 --db radiomapa_v2.db \
    --scenario estatico --environment outdoor --reference-point P01 --campaign-plmn 72406

# snapshots a cada 60 s (grade monotônica), até --max-measurements/--duration-s ou Ctrl+C
python -m coletor.daemon continuous --port /dev/ttyUSB2 --db radiomapa_v2.db --interval 60 \
    --scenario pedestre --environment outdoor --route trilha_1 --campaign-plmn 72406
```

`--campaign-plmn` registra a operadora da campanha (MCC+MNC). As células de outras operadoras
e as `CELL_FORBIDDEN` são gravadas do mesmo jeito, e a escolha do que entra nas features é do
Estimador. Antes da sessão, o Coletor consulta `AT$GPSP?` e só liga o GNSS se estiver desligado.
Nenhuma configuração persistente do modem é alterada.

## Banco v2 (resumo)

| Tabela | Conteúdo |
|---|---|
| `sessions` | status, horários UTC, modo, cenário/ambiente, ponto ou rota, `campaign_plmn`, `metadata_json` (porta, firmware, modelo da placa lido de `/proc/device-tree/model`, resultado do prepare; o IMEI não é guardado) |
| `reference_points` | nome, coordenada cadastrada e sua origem/incerteza (`pendente` = sem coordenada) |
| `measurements` | 1 linha por tentativa: `snapshot_status`, `reasons_json`, `access_mode` medido (`LTE`/`NR_NSA`/`NR_SA`/`UNKNOWN`), servidora (`serving_radio_key`, `serving_global_cell_key` e as fontes), status de cada comando, GNSS, latências, anomalias, erro de porta e respostas cruas (`raw_json`) |
| `observed_cells` | 1 linha por célula observada: fonte (`MONI`, `SERVINFO`, `CSURV`, `LEGACY_V1`), RAT, ARFCN, PCI, PLMN, Cell-ID, TAC, medidas, `cell_status`, `radio_key`, `global_cell_key`, `identity_status` |
| `health_checks` | eventos de saúde ligados à sessão |

Um banco v1 (antigo) é recusado com instruções, sem nenhuma alteração. A coluna
`measurements.lpp_probe_status` continua no schema v2, sempre vazia: a sonda LPP saiu do Coletor
na fase 7 e fica só em `experimental/lpp_probe.py`.

## Export completo e restauração

```bash
python -m coletor.export_csv export --db radiomapa_v2.db --out backups/2026-09-30
python -m coletor.export_csv restore --from backups/2026-09-30 --db restaurado_v2.db
```

O export gera `sessions.csv`, `reference_points.csv`, `measurements.csv`, `observed_cells.csv`,
`health_checks.csv` e `metadata.json` (SHA-256 e contagem por arquivo; NULL = `\N`). A
restauração cria um banco novo e confere tudo.

## Bancos v1: inspeção e migração não destrutiva

```bash
python -m coletor.migrate inspect --db radiomapa.db
python -m coletor.migrate migrate --db radiomapa.db --out radiomapa_v2.db
```

A origem é aberta somente leitura e o SHA-256 dela é conferido antes e depois. A saída precisa
ser um arquivo novo. O v1 não tinha ARFCN, PLMN e modo de acesso medido, então nenhuma
identidade é inventada:

- linhas migradas: `legacy_ambiguous = 1`, `access_mode = UNKNOWN` e sem `radio_key`/`global_cell_key`;
- vizinhas: fonte `LEGACY_V1`; RSRP 0 vira NULL;
- coordenadas de fix diferente de 2D/3D: só em `legacy_json`.

## Relação com o Estimador

O Estimador (`estimador.train` e `estimador.evaluate`) lê o schema v2 em modo somente leitura e
recusa o v1 (`schema_v1`). Entram só leituras com `#CSURV` válido e referência posicional; as
linhas migradas ambíguas ficam fora (ver `estimador/README.md`). O `estimador/live.py` não
depende do Coletor: lê o modem pela mesma aquisição compartilhada.

