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

No `temporal`, se as sessões têm o rótulo `campaign_tools:<sistema>:<ponto>:S1|S2` nas `notes`, todas as
S1 formam a seleção (hiperparâmetros por validação só em S1) e todas as S2 o teste independente
(`temporal_selection=S1`, `temporal_test=S2` no bundle); `--test-fraction`, o horário e a ordem dos
`session_id` não participam, e sessão sem rótulo é erro. Sessões `S3` (opcionais) são ignoradas no
temporal e entram no spatial e no complete. Bancos antigos sem nenhum rótulo mantêm a
divisão cronológica. Um ponto do teste que não aparece no treino é extrapolação espacial, não
generalização temporal: as métricas saem separadas em `known_points` e `new_points`
(`test_metrics_by_point_type`), com aviso. Se a referência de um ponto do teste for a mediana
GNSS com fixes de várias sessões (inclusive de treino), o resultado não tem independência
temporal completa, e o relatório avisa.
Os protocolos spatial e temporal não são equivalentes: compare resultados só dentro do mesmo protocolo.

Protocolo `complete` (modelo operacional): usa todas as amostras elegíveis (grupos de seleção por
sessão), sem grupo de teste. Os hiperparâmetros vêm da mesma validação cruzada agrupada, sobre todos
os grupos (`hyperparameter_source = grouped_cv_all_eligible`), e as métricas dessa validação não são
gravadas. O bundle tem o formato v2 de sempre, com `protocol = complete`, `evaluation_available =
false` e `dataset.training_samples_sha256`; não há `test_*`. `estimador.evaluate compare` o recusa
("modelo complete não possui conjunto de teste independente"); `estimador.live wknn` o carrega
normalmente. Mínimo técnico: 3 sessões com amostras elegíveis.

Por campanha, `python -m campaign_tools.wknn train|deploy --campaign NOME` executa os três treinos
(e a avaliação de spatial e temporal) e seleciona o bundle para teste posterior (padrão: `complete`);
o `deploy` também prepara o `runtime.conf` em modo manual (`TCC_MODE=manual`, `TCC_METHOD=wknn`,
porta e intervalo de `tcc-location.conf`, `TCC_MODEL` = bundle implantado), sem iniciar o daemon.
`python -m campaign_tools.position` faz uma inferência pontual com `estimador.live.run_estimate`
(a mesma lógica de `estimador.live wknn`, que continua válido): sem argumentos usa o `runtime.conf`;
com `--campaign NOME --model spatial|temporal|complete|deployed` testa cada bundle da campanha.
Ver o roteiro 04, seção 12.

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
# treino: um bundle por tecnologia e protocolo (complete = todas as amostras elegíveis, sem teste)
python -m estimador.train --db radiomapa_v2.db --technology LTE --protocol spatial \
    --out modelos/wknn_lte_spatial.json --report modelos/wknn_lte_spatial_relatorio.json
python -m estimador.train --db radiomapa_v2.db --technology LTE --protocol temporal \
    --out modelos/wknn_lte_temporal.json
python -m estimador.train --db radiomapa_v2.db --technology LTE --protocol complete \
    --out modelos/wknn_lte_complete.json

# WKNN e Cell-ID em todas as tentativas dos grupos de teste do bundle
python -m estimador.evaluate compare --db radiomapa_v2.db --model modelos/wknn_lte_spatial.json \
    --registry estimador/network_registry.json --out resumo_spatial.csv --predictions-out estimativas_spatial.csv

# validar o cadastro contra todas as tentativas (erro por célula servidora)
python -m estimador.evaluate cell-id --db radiomapa_v2.db --registry estimador/network_registry.json

# estimar a posição agora com 1 leitura do FN990 (--json para o resultado completo)
python -m estimador.live wknn --port /dev/ttyUSB2 --model modelos/wknn_lte_spatial.json
python -m estimador.live cell-id --port /dev/ttyUSB2 --registry estimador/network_registry.json
```

Opções do treino: `python -m estimador.train --help` (grade, PLMN da campanha, `--exclude-other-plmn`,
`--exclude-unknown-plmn`, `--include-forbidden`, semente e folds). Por padrão o WKNN usa células de
qualquer operadora: o fingerprint depende só do RSRP de um identificador estável (`radio_key`), e o
filtro por PLMN reduzia a cobertura (numa campanha real, 38% das leituras ficaram sem nenhuma célula
conhecida; sem o filtro, 96%). O
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

## Validação prática comparável (`campaign_tools.validate`)

Ferramenta de engenharia (não do usuário final, que usa `campaign_tools.position`). Para cada ponto
da campanha faz N repetições (padrão 3); em cada uma tira **um** snapshot (um `#CSURV`) e aplica esse
mesmo objeto aos bundles spatial, temporal e complete com `estimate_from_snapshot` (nunca
`run_estimate`, que leria o modem de novo). Os bundles são carregados e validados uma vez, antes de
abrir a porta; o deployed não entra na comparação (só é conferido e registrado em `platform.json`).

```bash
python -m campaign_tools.validate point --campaign casa-8-pontos-rpios --point P01   # um por ponto
python -m campaign_tools.validate summarize --campaign casa-8-pontos-rpios            # sem hardware
```

- Opções de `point`: `--repetitions` (3), `--models` (spatial,temporal,complete), `--stabilization-s`
  (60), `--between-repetitions-s` (10), `--metrics-interval-s` (1), `--output`, `--force` (refaz só o
  ponto). O ponto precisa estar no `collection.conf`; confirmação `OK` antes de começar.
- Verdade: `referencias_canonicas.csv`; erro = `reference.distance_m` (haversine do `geometry`).
- Ordem das predições alternada por repetição (T1 complete,temporal,spatial; T2 temporal,spatial,
  complete; T3 spatial,complete,temporal) e registrada em `protocol.json`.
- Artefatos (`<results>/avaliacoes/validation/`): `platform.json`, `protocol.json`,
  `fingerprints/<ponto>_T<n>.json` (snapshot completo, suficiente para recalcular sem o modem),
  `predictions.csv` (uma linha por modelo e snapshot, todos com o mesmo `snapshot_id`),
  `acquisitions.csv`, `system_metrics.csv` (mesmos campos do `monitor`, `mode=validation`,
  `session=T<n>`, `state=stabilizing|acquiring|predicting|completed|failed`), `failures.csv` e os
  resumos `summary_by_model.csv`, `summary_by_point.csv`, `validation_summary.json`.
- Métricas de sistema pelo `monitor.ProcessSampler` (thread no próprio processo; só `/proc` e `/sys`,
  sem psutil, GNU time, bash ou `vcgencmd`); campos ausentes no Buildroot ficam vazios.
- Snapshot `invalid`: fingerprint e aquisição são gravados e os três modelos dão `unavailable`;
  `partial` segue a regra do `estimate_from_snapshot`. Falha de porta registra a repetição e tenta de
  novo na seguinte. O `summarize` aponta pontos, repetições, modelos ou `snapshot_id` ausentes ou
  duplicados e arquivos ilegíveis (saída 1) e informa se está pronto para comparar com a outra
  plataforma.

## Runtime: perfil normal e perfil debug (`TCC_DEBUG`)

O `runtime/daemon.py` reutiliza `acquisition.take_snapshot` e `estimate_from_snapshot` (a mesma
inferência do `live.py`). `TCC_DEBUG=0` (padrão) é o produto: `latest_position.json`, `history.jsonl` e
`runtime.log`, sem arquivos de engenharia. `TCC_DEBUG=1` executa exatamente a mesma aquisição e
inferência (um snapshot e uma estimativa por ciclo, nenhum AT extra) e só observa: campos extras por ciclo
no histórico, `runtime_metrics.csv` (`ProcessSampler`, `mode=runtime`, `session=<cycle_id>`), um
`snapshots/<cycle_id>.json` por ciclo e `runtime_manifest.json`. Os campos e arquivos são idênticos no
Raspberry Pi OS e no Buildroot e permitem comparar cobertura, falhas, periodicidade, atraso de
agendamento, latências MONI/SERVINFO/CSURV/GNSS, tempo de predição, CPU, memória, temperatura, observações
e `radio_key` LTE/NR, estabilidade da posição e disponibilidade/qualidade do GNSS.

O GNSS do ciclo é observação auxiliar: não substitui o WKNN e não é ground truth.
`wknn_gnss_distance_m` é uma distância auxiliar WKNN × GNSS (só com os dois válidos), não o `error_m`
das avaliações; o erro formal em P01–P08 vem da referência canônica, em `campaign_tools.validate point`.
Detalhes, exemplos de configuração e lista de campos: `README.md` (seção Runtime autônomo).

