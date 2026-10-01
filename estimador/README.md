# Estimador

Estima a posição do UE a partir das leituras do modem. Desde a fase 5 da reconfiguração, o
Estimador principal tem dois métodos, ambos em Python puro (só a biblioteca padrão; o runtime
continua precisando apenas do `pyserial`):

- **Cell-ID** (`methods/cell_id.py`): baseline. Devolve a posição **cadastrada da célula
  servidora**, identificada pela `global_cell_key`. Não é E-CID e não é a posição do UE.
- **Fingerprinting WKNN** (`methods/fingerprint.py`): RSRP das células do `#CSURV` por
  `radio_key`, treinado offline com o radiomapa v2 do [Coletor](../coletor/README.md).

TA, AoA/LPP e geometria angular (`methods/network_position.py`, `methods/aoa.py`) continuam
legados e são revistos na fase 7. Os scripts antigos `compare.py`, `infer.py` e `relatorio.py`
foram para [`../deprecated/`](../deprecated/README.md).

```
estimador/
    dataset.py     # radiomapa v2 (só leitura) -> amostras rotuladas; exclusões; divisão por grupos
    geometry.py    # haversine, WGS-84 <-> ECEF <-> ENU
    features.py    # política de células, vocabulário por radio_key, ausência, escala
    estimate.py    # PositionEstimate (método, estado, motivo, lat/lon)
    train.py       # OFFLINE: seleção e treino do WKNN; grava o bundle JSON
    evaluate.py    # métricas e os subcomandos compare, cell-id e live
    live.py        # ONLINE: 1 leitura do modem -> 1 estimativa (WKNN ou Cell-ID); não precisa do Coletor
    db.py          # banco position_estimates do live.py antigo (até a fase 5); só leitura via evaluate live
    network_registry.example.json   # modelo do cadastro de células
    methods/
        cell_id.py           # Cell-ID + validação do cadastro
        fingerprint.py       # runtime WKNN; validação, leitura e gravação do bundle
        network_position.py  # TA legado (fase 7)
        aoa.py               # AoA legado (fase 7)
```

## Dados de entrada (`dataset.py`)

- Lê só o schema v2, em modo somente leitura; o v1 é recusado (`schema_v1`) com a instrução de
  migração (`python -m coletor.migrate`).
- Ficam fora: linhas migradas ambíguas (`legacy_ambiguous`), snapshot inválido, `#CSURV`
  indisponível ou vazio e amostras sem referência posicional. MONI e SERVINFO não entram nas
  features.
- Referência posicional, nesta ordem: coordenada cadastrada do ponto; mediana GNSS do ponto
  (mínimo de 3 leituras, com a dispersão registrada); GNSS isolado só com `--allow-single-gnss`.

## Protocolos de validação

| Protocolo | Grupo | Mede | Grupos de teste |
|---|---|---|---|
| `spatial` | `reference_point_id` | generalização para pontos não vistos | fração dos pontos, sorteada com semente |
| `temporal` | `session_id` | estabilidade no tempo nos mesmos pontos | sessões mais recentes |

Nenhuma divisão é por linha. O teste é reservado antes da seleção; k, distância (L1/L2),
escala (RSRP bruto ou padronizado) e política de ausência são escolhidos só por validação
cruzada agrupada nos demais grupos, e o teste é avaliado uma única vez. Sem grupos
suficientes, o treino encerra com `<protocolo>_validation_unavailable: insufficient_groups`.
Os protocolos não são equivalentes: compare resultados só dentro do mesmo protocolo.

## Bundle WKNN

JSON com `model_type = "wknn"` e `model_version = 2`. Campos obrigatórios em `REQUIRED_FIELDS`
e regras em `validate_bundle()` (`methods/fingerprint.py`): vocabulário de `radio_key`, status
de PLMN por chave, valor de ausência (negativo e abaixo do menor RSRP do treino; nunca 0 dBm),
escala, origem ENU, k, distância, política de distância zero, políticas de células e de
referência, protocolo, grupos de treino, identificação do teste (`dataset.test_measurement_ids`
e `dataset.test_samples_sha256`) e métricas de validação e de teste. Bundles `.joblib`/pickle
são recusados sem serem abertos, e um bundle existente nunca é sobrescrito.

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

# WKNN e Cell-ID nas mesmas amostras de teste registradas no bundle
python -m estimador.evaluate compare --db radiomapa_v2.db --model modelos/wknn_lte_spatial.json \
    --registry estimador/network_registry.json --out resumo_spatial.csv --predictions-out estimativas_spatial.csv

# validar o cadastro contra todas as amostras rotuladas (erro por célula servidora)
python -m estimador.evaluate cell-id --db radiomapa_v2.db --registry estimador/network_registry.json

# estimar a posição agora com 1 leitura do FN990 (--json para o resultado completo)
python -m estimador.live wknn --port /dev/ttyUSB2 --model modelos/wknn_lte_spatial.json
python -m estimador.live cell-id --port /dev/ttyUSB2 --registry estimador/network_registry.json
```

Opções do treino: `python -m estimador.train --help` (grade, PLMN da campanha, inclusão de
outras operadoras, de PLMN não confirmado e de `CELL_FORBIDDEN`, semente e folds). O
`compare` recusa o banco se as medições de teste do bundle sumiram ou mudaram
(`test_samples_missing`, `test_samples_changed`). Métricas: cobertura, média, mediana (CE50),
RMSE, p90, p95, máximo e CDF.

## `live.py`: inferência atual (modo once)

Um comando por método (`wknn` ou `cell-id`). Valida o bundle ou o cadastro antes de abrir a
porta, conecta ao FN990, tira 1 snapshot pela aquisição compartilhada
(`acquisition/snapshot.py`), estima, mostra o resultado e fecha a conexão. Não depende do
Coletor (não rode os dois sobre o mesmo modem ao mesmo tempo), não grava banco, não liga o
GNSS e não usa TA, LPP nem geometria angular.

- Snapshot `invalid`: nenhuma estimativa (`unavailable`). Snapshot `partial`: usado só se tiver o
  que o método exige (WKNN: `#CSURV` lido; Cell-ID: `global_cell_key` da servidora); o resultado
  registra `snapshot_status`. Falta só de fix GNSS já deixa o snapshot `partial`.
- WKNN: só células do `#CSURV`, vetor na ordem das `radio_key` do bundle, com as políticas de
  PLMN, `CELL_FORBIDDEN`, ausência e escala do treino; células fora do vocabulário são
  ignoradas e contadas (`n_ignored_keys`). Sem nenhuma `radio_key` do bundle: `unavailable`.
  Leitura sem células da tecnologia do bundle: `technology_mismatch`.
- Cell-ID: `valid`, `unknown_cell` ou `missing_global_cell_key`, como na avaliação.
- Resultado (`PositionEstimate` em `estimate.py`): `method`, `valid`, `status`, `reason`,
  `x_m`/`y_m` (ENU do bundle), `lat`/`lon`, `technology`, `snapshot_status` e `metadata`
  (resumo do snapshot e tempos de carga e de estimativa).
- Código de saída: 0 com estimativa válida; 1 sem estimativa ou com o FN990 indisponível
  (porta ou modem sem resposta); 2 para bundle, cadastro ou argumento inválido.

`evaluate live` continua lendo, em modo somente leitura, bancos `position_estimates` gravados
pelo `live.py` até a fase 5; o `live.py` atual não grava nesse banco.

Referências conceituais: `planejamento/arquitetura_estimadores_posicao.md` e
`planejamento/knn_explicado.md`.

