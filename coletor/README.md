# Coletor — o daemon que constrói o radiomapa (só para treinar o Fingerprint)

**Sim, o Coletor continua sendo um daemon, exatamente como antes.** Nada muda na forma como
ele roda. O que muda é o **motivo** dele existir: agora que o [Estimador](../estimador/README.md)
sabe a posição das células de antemão (`estimador/network_registry.json`), 4 dos 5 métodos de
geolocalização (`cell_id`, `ta`, `ecid_rede`, `aoa`) viraram cálculo direto e **não precisam
mais de histórico nenhum**. Sobrou só o **Fingerprint**, que é o único método deste projeto
que ainda aprende de um radiomapa — e é exclusivamente para isso que o Coletor existe.

Se você só vai usar Cell-ID, TA, E-CID de rede ou AoA, **não precisa nem rodar o Coletor**.
Ele só entra em cena quando você quer treinar/melhorar o Fingerprint.

```
coletor/
    db.py           # schema SQLite do radiomapa + funções de escrita
    daemon.py       # o daemon em si -- 3 modos: status / ondemand / continuous
    export_csv.py   # exportação de backup em CSV
```

(`health.py` saiu daqui — virou `hal/health.py`, ver "Quem faz o quê" abaixo.)

## O que ele faz / não faz

**Faz:** decide a cadência de leitura, roda 1 *thread* por modem + 1 *thread* única de
escrita (evita concorrência no SQLite), faz health check a cada ciclo (e tenta reconectar
sozinho se um modem parar de responder), grava sessão/medições/vizinhas/health checks.

**Não faz:** não sabe interpretar AT (isso é o HAL), não estima posição (isso é o
[Estimador](../estimador/README.md)) — só grava a matéria-prima que o HAL entrega, para o
Fingerprint treinar depois.

## Quem faz o quê: Coletor continua sendo o daemon de campanha; quem cuida da conexão é o HAL

- **Coletor** decide **quando** ler (a cadência, os 3 modos) e **o que fazer com o resultado**
  (gravar no radiomapa) -- isso não mudou nada.
- **Configurar/conectar o modem** (`driver.connect(port, baudrate)`) continua sendo feito pelo
  próprio Coletor, como sempre foi -- ele é quem decide qual porta usar.
- **Health check** (a pergunta "o modem ainda responde?") virou um utilitário compartilhado em
  `hal/health.py` (`HealthMonitor`), porque essa pergunta é sobre a *conexão*, não sobre
  *coleta de dados* -- e agora tem 2 daemons que precisam da mesma resposta: o Coletor (campanha
  de campo) e o `estimador/live.py` no modo `continuous` (posição ao vivo). Os dois usam a
  mesma classe, mas cada um gerencia sua **própria** conexão -- não é a mesma conexão
  compartilhada entre os dois (nem faria sentido, já que servem momentos diferentes: coletar
  radiomapa vs. usar o sistema no dia a dia).

## Tabela: cada coluna gravada e para qual geolocalização ela serve hoje

| Tabela / coluna | De onde vem (método do HAL) | Serve para |
|---|---|---|
| `measurements.rsrp_dbm/rsrq_db/sinr_db` + `neighbor_cells.pci/rsrp_dbm` | `read_serving_cell()` + `scan_cells()` | **Fingerprinting** -- o único método que ainda precisa deste histórico para treinar |
| `measurements.gnss_lat/gnss_lon/...` | `read_gnss()` | posição de referência auxiliar (GNSS do modem) |
| `measurements.serving_cell_id/serving_pci/serving_tac` | `read_serving_cell()` + `read_serving_info()` + `scan_cells()` (cruzamento em `hal.parsing.resolve_serving_cell`) | comparar/validar o cadastro (`estimador.compare`) contra o histórico |
| `measurements.timing_advance` | — (sempre vazio: TA não é lido) | idem |
| `measurements.lpp_raw_log` | `query_lpp_config()`, só com `--lpp-probe` | texto cru da sonda experimental somente leitura `AT$LCSLPP?` |
| `health_checks.alive` | `is_alive()` (via `hal.health.HealthMonitor`) | confiabilidade/robustez -- não é geolocalização, é operação |

## Os 3 modos (sem mudanças)

```bash
# status: só imprime o status de cada modem, não cria sessão de coleta
python -m coletor.daemon --mode status --fn990-port /dev/ttyUSB2

# ondemand (padrão): 1 leitura de cada modem configurado, grava, e termina
python -m coletor.daemon --scenario pedestre --environment outdoor \
    --fn990-port /dev/ttyUSB2 --db radiomapa.db

# continuous: fica rodando, lendo a cada --interval segundos (padrão 60s), até Ctrl+C
python -m coletor.daemon --mode continuous --interval 60 \
    --scenario pedestre --environment outdoor \
    --fn990-port /dev/ttyUSB2 --db radiomapa.db

# qualquer modo acima aceita --lpp-probe (sonda experimental somente leitura de AT$LCSLPP?)
python -m coletor.daemon --mode continuous --lpp-probe \
    --scenario pedestre --environment outdoor \
    --fn990-port /dev/ttyUSB2 --db radiomapa.db
```

No modo `continuous`, cada modem tem sua própria *thread* com health check embutido: antes de
cada leitura, testa se o modem ainda responde; se falhar 3 vezes seguidas, tenta reconectar a
porta serial sozinho. `Ctrl+C` encerra graciosamente (fecha as *threads*, marca o fim da sessão).

## Backup em CSV

```bash
python -m coletor.export_csv --db radiomapa.db --session 1 --out sessao_1.csv
python -m coletor.export_csv --db radiomapa.db --out tudo.csv   # sem --session = exporta tudo
```

## Como ele conversa com o HAL e com o Estimador

Importa `hal.fn990_driver.FN990Driver` e `hal.health.HealthMonitor` -- o HAL não sabe que
existe um daemon por trás. Duas formas de o Estimador usar o que o Coletor produziu:

1. **`estimador/train.py` e `estimador/compare.py`** (offline): leem o arquivo SQLite que o
   Coletor gravou, depois que a campanha de campo terminou.
2. **`estimador/live.py`** (online): **não depende do Coletor rodando** -- fala com o HAL
   diretamente. Ele só importa 1 função do Coletor (`poll_once`, que já não grava nada em
   banco por conta própria) para não duplicar a lógica de leitura do modem.

