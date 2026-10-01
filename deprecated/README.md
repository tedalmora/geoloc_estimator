# codigo/deprecated/

Material com valor apenas histórico. **Nenhum código ativo pode importar ou referenciar esta
pasta**; `tests/test_sem_deprecated.py` verifica isso. Não há `__init__.py`, então os arquivos não
formam pacote importável. Eles também **não rodam mais**: importam funções do Estimador legado
(leitura do banco v1 e cadastro por PCI) que a reconfiguração do código removeu. O histórico completo continua no Git.

| Arquivo (origem: `codigo/estimador/`) | O que fazia | Por que foi substituído | Substituto |
|---|---|---|---|
| `estimador/compare.py` | Comparava `cell_id`/`ta` por PCI (versões anteriores também o fingerprint) num sorteio de 20% das linhas de um banco v1 | Divisão por linha mistura leituras do mesmo ponto e da mesma sessão entre treino e teste; identidade por PCI; banco v1 | `python -m estimador.evaluate compare`: usa o teste agrupado registrado no bundle e avalia WKNN e Cell-ID nas mesmas tentativas |
| `estimador/infer.py` | Estimava a posição a partir da última leitura gravada num banco v1 (`cell_id`/`ta` por PCI, AoA por âncoras digitadas) | Tratava o banco de coleta como fonte ao vivo; cadastro por PCI | `python -m estimador.live wknn|cell-id` (1 leitura atual do modem); `python -m estimador.evaluate cell-id` para validar o cadastro; TA e geometria de bearings em `experimental/` |
| `estimador/relatorio.py` | Resumo, com pandas, do que `estimador.live` gravava (média, p90 e máximo por método) | Dependia de pandas e não informava cobertura, mediana, RMSE nem p95 | nenhum: o `live.py` atual não grava estimativas, e o `evaluate live`, que lia o banco antigo, foi removido. Para métricas, use `python -m estimador.evaluate compare` |

Random Forest: removido por estar fora do escopo; não preservado.
