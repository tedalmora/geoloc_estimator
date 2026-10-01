# Estimador

Estima a posição do UE a partir das leituras do modem. O
Estimador principal tem dois métodos, ambos em Python puro (só a biblioteca padrão; o runtime
continua precisando apenas do `pyserial`):

- **Cell-ID** (`methods/cell_id.py`): baseline. Devolve a posição **cadastrada da célula
  servidora**, identificada pela `global_cell_key`. Não é E-CID e não é a posição do UE.
- **Fingerprinting WKNN** (`methods/fingerprint.py`): RSRP das células do `#CSURV` por
  `radio_key`, treinado offline com o radiomapa v2 do [Coletor](../coletor/README.md).

TA, LPP e geometria de bearings ficam isolados em [`../experimental/`](../experimental/), fora do
caminho principal. Os scripts antigos `compare.py`, `infer.py` e `relatorio.py`
foram para [`../deprecated/`](../deprecated/README.md).

```
estimador/
    dataset.py     # radiomapa v2 (só leitura) -> amostras rotuladas e tentativas; exclusões; divisão por grupos
    geometry.py    # haversine, WGS-84 <-> ECEF <-> ENU
    features.py    # política de células, vocabulário por radio_key, ausência, escala
    estimate.py    # PositionEstimate (método, estado, motivo, lat/lon) e a regra de leitura indisponível
    train.py       # OFFLINE: seleção e treino do WKNN; grava o bundle JSON
    evaluate.py    # métricas e os subcomandos compare e cell-id
    live.py        # ONLINE: 1 leitura do modem -> 1 estimativa (WKNN ou Cell-ID); não precisa do Coletor
    network_registry.example.json   # modelo do cadastro de células
    methods/
        cell_id.py           # Cell-ID + validação do cadastro
        fingerprint.py       # runtime WKNN; validação, leitura e gravação do bundle
```

## Dados de entrada (`dataset.py`)

- Lê só o schema v2, em modo somente leitura; o v1 é recusado (`schema_v1`) com a instrução de
  migração (`python -m coletor.migrate`).
- Ficam fora das amostras: linhas migradas ambíguas (`legacy_ambiguous`), snapshot inválido,
  `#CSURV` indisponível ou vazio e amostras sem referência posicional. MONI e SERVINFO não entram
  nas features. Todas as linhas não migradas continuam como **tentativas** (`attempts`), com o
  motivo de exclusão e o ambiente da sessão: são a base da cobertura operacional.
- Referência posicional, nesta ordem: coordenada cadastrada do ponto (a única tratada como
  referência **final**); mediana GNSS do ponto (mínimo de 3 leituras, com a dispersão e o número
  de sessões que contribuíram); GNSS isolado só com `--allow-single-gnss`. As duas últimas são
  referências **exploratórias**, e os relatórios avisam quando aparecem. Para resultados finais,
  cadastre coordenadas levantadas (`coletor.daemon point-add`).

## Protocolos de validação

| Protocolo | Grupo | Mede | Grupos de teste |
|---|---|---|---|
| `spatial` | `reference_point_id` | generalização para pontos não vistos | fração dos pontos, sorteada com semente |
| `temporal` | `session_id` | estabilidade no tempo nos mesmos pontos | sessões mais recentes |

Nenhuma divisão é por linha. O teste é reservado antes da seleção; k, distância (L1/L2),
escala (RSRP bruto ou padronizado) e política de ausência são escolhidos só por validação
cruzada agrupada nos demais grupos (vocabulário, ausência e escala ajustados em cada fold só
com o treino do fold), e o teste é avaliado uma única vez. Mínimo técnico: 4 grupos (1 de
teste e 3 de seleção), com pelo menos 2 grupos de treino e 1 de validação em cada fold; abaixo
disso, o treino encerra com `<protocolo>_validation_unavailable: insufficient_groups`. O mínimo
não garante um resultado estável: com 1 grupo de teste ou folds com 2 grupos de treino, o
relatório emite aviso de variância alta. O bundle registra grupos, pontos, sessões e amostras
do teste, da seleção e de cada fold (`validation.subsets`).

No `temporal`, um ponto do teste que não aparece no treino é extrapolação espacial, não
generalização temporal: as métricas saem separadas em `known_points` e `new_points`
(`test_metrics_by_point_type`), com aviso. Se a referência de um ponto do teste for a mediana
GNSS com fixes de várias sessões (inclusive de treino), o resultado não tem independência
temporal completa, e o relatório avisa.
Os protocolos não são equivalentes: compare resultados só dentro do mesmo protocolo.

## Bundle WKNN

JSON com `model_type = "wknn"` e `model_version = 2`. Campos obrigatórios em `REQUIRED_FIELDS`
e regras em `validate_bundle()` (`methods/fingerprint.py`): vocabulário de `radio_key`, status
de PLMN por chave, valor de ausência (negativo e abaixo do menor RSRP do treino; nunca 0 dBm),
escala, origem ENU, k, distância, política de distância zero, políticas de células e de
referência, protocolo, grupos e pontos de treino, identificação do teste (`dataset.test_measurement_ids`
e `dataset.test_samples_sha256`), versão do software (`software_version`) e métricas de validação
e de teste. Bundles `.joblib`/pickle são recusados sem serem abertos, e um bundle existente
nunca é sobrescrito.

Distância zero: se uma ou mais amostras do bundle forem idênticas à leitura (distância
<= 1e-9), a posição é a média simples de **todas** elas, mesmo que sejam mais que k, sem peso
inverso. Senão, valem os k vizinhos com peso 1/d. Empate na k-ésima distância: entra a amostra
que vem antes no bundle (ordem crescente de `measurement_id`), regra determinística.

## Cadastro de células (`network_registry.json`)

Copie `network_registry.example.json` (entradas fictícias, PLMN 00101) e preencha 1 entrada
por célula, identificada pela `global_cell_key` (`RAT:PLMN:CELL_ID`, a mesma que o Coletor
grava em `measurements.serving_global_cell_key`), nunca pelo PCI. Formato e regras: docstring
de `methods/cell_id.py`. Estados do Cell-ID: `valid`, `unknown_cell`,
`missing_global_cell_key`, `invalid_registry` e `unavailable`.

## Comandos (a partir de `codigo/`)

```bash
# treino: um bundle por tecnologia e protocolo
python -m estimador.train --db radiomapa_v2.db --technology LTE --protocol spatial \
    --out modelos/wknn_lte_spatial.json --report modelos/wknn_lte_spatial_relatorio.json
python -m estimador.train --db radiomapa_v2.db --technology LTE --protocol temporal \
    --out modelos/wknn_lte_temporal.json

# WKNN e Cell-ID em todas as tentativas dos grupos de teste do bundle
python -m estimador.evaluate compare --db radiomapa_v2.db --model modelos/wknn_lte_spatial.json \
    --registry estimador/network_registry.json --out resumo_spatial.csv --predictions-out estimativas_spatial.csv

# validar o cadastro contra todas as tentativas (erro por célula servidora)
python -m estimador.evaluate cell-id --db radiomapa_v2.db --registry estimador/network_registry.json

# estimar a posição agora com 1 leitura do FN990 (--json para o resultado completo)
python -m estimador.live wknn --port /dev/ttyUSB2 --model modelos/wknn_lte_spatial.json
python -m estimador.live cell-id --port /dev/ttyUSB2 --registry estimador/network_registry.json
```

Opções do treino: `python -m estimador.train --help` (grade, PLMN da campanha, inclusão de
outras operadoras, de PLMN não confirmado e de `CELL_FORBIDDEN`, semente e folds). O
`compare` recusa o banco se as medições de teste do bundle sumiram ou mudaram
(`test_samples_missing`, `test_samples_changed`). Métricas: média, mediana (CE50), RMSE, p90,
p95, máximo e CDF, sempre com a cobertura.

### Cobertura e subconjunto comum (`compare`)

O `compare` avalia os dois métodos em **todas as tentativas** dos grupos de teste, inclusive as
que falharam antes de chegar ao método (ex.: `#CSURV` sem resposta). Para cada método informa
tentativas, elegíveis, válidas e falhas (por estado):

- elegível: a leitura tem a entrada que o método exige (WKNN: snapshot não inválido e `#CSURV`
  lido com célula da tecnologia do bundle; Cell-ID: snapshot não inválido e `global_cell_key`
  da servidora, sem depender do `#CSURV`);
- **cobertura operacional** = válidas / tentativas; **cobertura específica** = válidas / elegíveis;
- **subconjunto comum** = tentativas em que os dois métodos estimaram (com referência): é o único
  recorte em que os erros do WKNN e do Cell-ID são diretamente comparáveis. No recorte
  `tentativas`, o erro de cada método usa as suas próprias estimativas válidas.

A saída também registra as versões do software (avaliação e treino), os ambientes, a origem e
a dispersão da referência (`final`/`exploratoria`), os pontos com referência compartilhada
entre sessões, a separação `known_points`/`new_points` e os avisos. O CSV `--out` tem uma linha
por método e recorte (`tentativas`, `comum` e, no temporal, os tipos de ponto).

## `live.py`: inferência atual (modo once)

Um comando por método (`wknn` ou `cell-id`). Valida o bundle ou o cadastro antes de abrir a
porta, conecta ao FN990, tira 1 snapshot pela aquisição compartilhada
(`acquisition/snapshot.py`), estima, mostra o resultado e fecha a conexão. Não depende do
Coletor (não rode os dois sobre o mesmo modem ao mesmo tempo), não grava banco, não liga o
GNSS e não usa TA, LPP nem geometria angular.

- Snapshot `invalid`: nenhuma estimativa (`unavailable`). Snapshot `partial`: usado só se tiver o
  que o método exige (WKNN: `#CSURV` lido; Cell-ID: `global_cell_key` da servidora); o resultado
  registra `snapshot_status`. Falta só de fix GNSS já deixa o snapshot `partial`. A regra é a
  mesma do `compare` (`estimate.unavailable_reading`).
- WKNN: só células do `#CSURV`, vetor na ordem das `radio_key` do bundle, com as políticas de
  PLMN, `CELL_FORBIDDEN`, ausência e escala do treino; células fora do vocabulário são
  ignoradas e contadas (`n_ignored_keys`). Sem nenhuma `radio_key` do bundle:
  `no_matching_radio_keys`. Leitura sem células da tecnologia do bundle: `technology_mismatch`.
- Cell-ID: `valid`, `unknown_cell` ou `missing_global_cell_key`, como na avaliação.
- Resultado (`PositionEstimate` em `estimate.py`): `method`, `valid`, `status`, `reason`,
  `x_m`/`y_m` (ENU do bundle), `lat`/`lon`, `technology`, `snapshot_status` e `metadata`
  (resumo do snapshot e tempos: carga, estimativa e ponta a ponta, da carga do bundle ao fim da
  estimativa, incluindo conexão e leitura).
- Código de saída: 0 com estimativa válida; 1 sem estimativa ou com o FN990 indisponível
  (porta ou modem sem resposta); 2 para bundle, cadastro ou argumento inválido.

Um bundle treinado com radiomapa sintético só serve para conferir o caminho (carga, leitura,
estimativa, tempos e código de saída) no hardware: a posição que ele devolve não tem sentido físico
e não é medida de erro nem de precisão. O `live.py` não grava estimativas; o antigo
`evaluate live` (leitura do banco `position_estimates` do live.py anterior) foi removido.

Fontes de cada decisão: `planejamento/RASTREABILIDADE_CODIGO.md`. O `planejamento/knn_explicado.md`
explica KNN/WKNN do zero, mas é documento histórico (arquivos e comandos do desenho anterior).

