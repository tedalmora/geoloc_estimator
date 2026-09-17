# Estimador — o "cérebro" central de geolocalização

O Estimador reúne os **5 métodos de geolocalização** do projeto. **Só o Fingerprint usa
aprendizado de máquina** — os outros 4 (`cell_id`, `ta`, `ecid_rede`, `aoa`) são cálculo
direto, possível porque você tem controle total do core e vai saber a posição real de cada
célula (`network_registry.json`). Ele tem 2 modos de operação bem separados:

- **Offline (treino/validação)** -- `train.py` e `compare.py` leem o radiomapa que o
  [Coletor](../coletor/README.md) gravou. Só existe porque o Fingerprint precisa de dados
  históricos para aprender.
- **Online (medição ao vivo)** -- `live.py` fala com o HAL **diretamente**, sem depender do
  Coletor rodando, e responde "onde estou agora" com o(s) método(s) que você escolher.

```
estimador/
    features.py           # vocabulário fixo de PCIs (usado só pelo fingerprint.py)
    evaluate.py            # métricas (haversine, CDF, percentil 90) -- compartilhadas por todo mundo
    db.py                  # SQLite próprio p/ log das estimativas do modo `live.py continuous`
    train.py               # OFFLINE -- treina o Fingerprint com o radiomapa do Coletor
    compare.py             # OFFLINE -- compara os métodos no mesmo conjunto de teste
    live.py                # ONLINE -- mede a posição agora, fala com o HAL direto
    network_registry.example.json   # cadastro de posição/azimute das células -- copie e preencha
    methods/
        network_position.py  # Métodos: Cell-ID/E-CID, E-CID de rede, Cell-ID+TA (sem ML)
        fingerprint.py         # Método: Fingerprinting completo (KNN/WKNN + Random Forest) -- ÚNICO com ML
        aoa.py                 # Método: DL-AoD/UL-AoA (geometria pura, sem ML)
```

## Tabela: os 5 métodos, o que cada um usa, e se precisa de ML

| Método | Onde mora | Usa ML? | Entrada | Precisa do radiomapa do Coletor? |
|---|---|---|---|---|
| `cell_id` (Cell-ID/E-CID, lado do UE) | `methods/network_position.py` | **Não** -- lookup no cadastro | `serving_pci` (do HAL) | Não |
| `ecid_rede` (E-CID de rede) | `methods/network_position.py` (mesma função `estimate_cell_position`) | **Não** | `serving_pci`/cell id (do **core** -- integração ainda não implementada, ver nota abaixo) | Não |
| `ta` (Cell-ID + Timing Advance) | `methods/network_position.py` | **Não** -- geometria/regra de 3 sobre o cadastro | `serving_pci` + `timing_advance` (do HAL) | Não |
| `fingerprint` | `methods/fingerprint.py` | **Sim** -- KNN/WKNN + Random Forest | RSRP da servidora + de todas as vizinhas (do HAL) | **Sim, é o único** |
| `aoa` (DL-AoD/UL-AoA) | `methods/aoa.py` | **Não** -- interseção geométrica de 2 retas | 2 ângulos (bearings) de 2 âncoras conhecidas | Não |

Isso cobre os métodos realmente viáveis com a topologia de até 2 antenas do projeto (ver
`planejamento/visao_geral_projeto.md`, Seção 5.1) — os métodos que exigem 3+ pontos de rede
(OTDOA, UL-TDOA, Multi-RTT) ficam fora de escopo por limitação física de infraestrutura, não
por limitação deste código.

**Por que `cell_id` e `ecid_rede` não têm cada um seu próprio arquivo:** são o mesmo cálculo
(`estimate_cell_position()`) -- a única diferença é de onde vem o `serving_pci` (HAL vs. core).
**`ecid_rede` ainda não tem um subcomando de CLI próprio** porque não existe, hoje, nenhuma
integração com o core que forneça esse dado -- quando existir, é só alimentar a mesma função
com o dado vindo de lá, nenhuma conta nova precisa ser escrita.

**Por que `ta` não usa mais KNN:** antes, sem saber a posição da torre, "célula + TA" só podia
virar posição aprendendo empiricamente do radiomapa (daí o KNN). Agora que você vai cadastrar a
posição real de cada célula, isso virou regra determinística: distância = TA × constante,
projetada a partir da posição cadastrada (na direção do azimute, se você souber; senão, a
posição fica igual à do Cell-ID puro e o TA só define um raio de incerteza). Ver
`methods/network_position.py` para a implementação e os comentários com a fórmula.

**Situação atual do `ta` (importante):** o modem em uso hoje (`FN990A28`, firmware
`M0R.000003`) não expõe Timing Advance em nenhum comando AT simples -- `get_signal_metrics()`
sempre devolve `timing_advance=None`, então **`ta` se comporta exatamente igual ao `cell_id`
puro** por enquanto (distância = 0). Existe um comando dedicado (`AT#TAINFO`) só numa versão
de firmware mais nova -- ver `planejamento/procedimento_ta_sinr_fn990.md` para a investigação
completa e a avaliação de risco do upgrade. Não é bloqueante: o projeto já é válido com os
outros 4 métodos, e `ta` volta a fazer sentido assim que o TA estiver disponível.

## O cadastro de posições (`network_registry.json`)

Copie `network_registry.example.json` para `network_registry.json` e preencha com a posição
real de cada célula (1 entrada por PCI). `azimute_graus` é opcional -- deixe `null` se a
célula for omnidirecional (ex.: o QCell indoor) ou se ainda não souber o azimute do setor.

## OFFLINE -- treino e comparação (dependem do Coletor)

```bash
# Treinar o Fingerprint (único método que treina)
python -m estimador.train --db radiomapa.db --technology 5G --out modelos/fingerprint_5g.joblib

# Comparar cell_id, ta, fingerprint (wknn e rf) no MESMO conjunto de teste -- tabela pronta p/ o Cap. 5
python -m estimador.compare --db radiomapa.db --technology 5G --registry network_registry.json --out resultados_5g.csv
```

`compare.py` também serve para **validar o cadastro de posições**: se `cell_id`/`ta` derem erro
muito maior que o esperado contra os pontos GNSS reais do radiomapa, é sinal de que a posição
cadastrada daquela célula está errada.

## ONLINE -- medir a posição agora (`live.py`, não depende do Coletor)

```bash
# once: tira 10 leituras (padrão), reporta média + desvio-padrão por método (métrica de Estabilidade)
# e SALVA 1 linha por método em --db (padrão: estimador_estimativas.db)
python -m estimador.live once --metodos cell_id,ta,fingerprint \
    --fn990-port /dev/ttyUSB2 --technology 5G --registry network_registry.json \
    --fingerprint-modelo modelos/fingerprint_5g.joblib

# se você sabe a posição real na hora (ex.: parado num ponto já surveyado), informe com
# --gnss-real -- o erro (haversine) já é calculado e salvo junto, sem precisar do GNSS do modem
python -m estimador.live once --metodos cell_id,ta,fingerprint --gnss-real -26.9005,-49.0703 \
    --fn990-port /dev/ttyUSB2 --technology 5G --registry network_registry.json \
    --fingerprint-modelo modelos/fingerprint_5g.joblib

# continuous: mede para sempre a cada --interval segundos, logando cada estimativa, até Ctrl+C
python -m estimador.live continuous --metodos cell_id,fingerprint --interval 5 \
    --fn990-port /dev/ttyUSB2 --technology 5G --registry network_registry.json \
    --fingerprint-modelo modelos/fingerprint_5g.joblib --db estimador_estimativas.db

# aoa precisa dos 2 ângulos (ainda extraídos manualmente do LPP) -- ficam fixos durante a execução
python -m estimador.live once --metodos aoa \
    --aoa-anchor1 -26.900,-49.070 --aoa-bearing1 45 --aoa-anchor2 -26.901,-49.071 --aoa-bearing2 300 \
    --fn990-port /dev/ttyUSB2
```

`live.py` usa o mesmo `HealthMonitor` que o Coletor (`hal/health.py`) no modo `continuous`, e
importa a função `poll_once` do Coletor só para não duplicar a lógica de leitura do HAL -- mas
gerencia sua **própria** conexão com o modem, e não precisa do daemon do Coletor rodando.

## Comparar depois do que foi medido ao vivo (`estimador/relatorio.py`)

Toda vez que você roda `live.py` (`once` ou `continuous`), cada estimativa é salva em
`position_estimates` -- com `lat_real`/`lon_real`/`erro_m` preenchidos sempre que havia uma
posição real conhecida na hora (GNSS do modem ou `--gnss-real`). Depois de medir em vários
pontos/sessões diferentes, compare todos os métodos de uma vez:

```bash
python -m estimador.relatorio --db estimador_estimativas.db --out resultados_live.csv
```

Isso é diferente do `compare.py` (que usa o radiomapa histórico do Coletor com split
treino/teste) -- aqui a comparação é feita com o que você **mediu de verdade em campo**, sem
precisar do Coletor rodando.

## Avaliação e comparação entre métodos

`evaluate.py` calcula erro haversine, CDF e percentil 90 (mesma definição de
`planejamento/visao_geral_projeto.md`, Seção 7). `compare.py` usa isso para `cell_id`, `ta` e
`fingerprint` (wknn e rf) lado a lado, no mesmo conjunto de teste. `aoa`, por não usar
radiomapa, deve ser avaliado à parte: colete alguns pontos com GNSS conhecido, rode
`estimador.live once --metodos aoa`, e calcule o erro manualmente com
`estimador.evaluate.haversine_m(lat_real, lon_real, lat_estimado, lon_estimado)`.

## Coleta vs. medição ao vivo -- por que não é a mesma coisa

- **Coletor** = pipeline de **treino**: junta histórico com GNSS conhecido, só para o
  Fingerprint aprender. Roda em campanhas de campo, ocasionalmente.
- **`estimador/live.py`** = pipeline de **inferência**: responde "onde estou agora", com
  qualquer um dos 5 métodos. Roda quando você quer usar o sistema de verdade.

Os dois usam o HAL (é uma biblioteca, feita para isso) e compartilham 2 utilitários pequenos
(`poll_once`, `HealthMonitor`) só para não duplicar código -- mas são processos independentes,
para propósitos diferentes, e você normalmente não roda os dois ao mesmo tempo sobre o mesmo
modem.

## Documento de referência

Ver `planejamento/arquitetura_estimadores_posicao.md` e `planejamento/knn_explicado.md` (esse
último é o tutorial conceitual de KNN/WKNN — este README é o mapa de "o que faz o quê").

