# codigo/deprecated/

Material com valor apenas histórico. **Nenhum código ativo pode importar ou referenciar esta
pasta**; `tests/test_sem_deprecated.py` verifica isso. Não há `__init__.py`, então os arquivos não
formam pacote importável. Eles também **não rodam mais**: importam funções do Estimador legado
(leitura do banco v1 e cadastro por PCI) que a fase 5 removeu. O histórico completo continua no Git.

| Arquivo (origem: `codigo/estimador/`) | O que fazia | Por que foi substituído | Substituto |
|---|---|---|---|
| `estimador/compare.py` | Comparava `cell_id`/`ta` por PCI (e, até a fase 5, o fingerprint) num sorteio de 20% das linhas de um banco v1 | Divisão por linha mistura leituras do mesmo ponto e da mesma sessão entre treino e teste; identidade por PCI; banco v1 | `python -m estimador.evaluate compare`: usa o teste agrupado registrado no bundle e avalia WKNN e Cell-ID nas mesmas amostras |
| `estimador/infer.py` | Estimava a posição a partir da última leitura gravada num banco v1 (`cell_id`/`ta` por PCI, AoA por âncoras digitadas) | Tratava o banco de coleta como fonte ao vivo; cadastro por PCI | `estimador.live` (operação revista na fase 6); `python -m estimador.evaluate cell-id` para validar o cadastro; TA e AoA na fase 7 |
| `estimador/relatorio.py` | Resumo, com pandas, do que `estimador.live` gravou (média, p90 e máximo por método) | Dependia de pandas e não informava cobertura, mediana, RMSE nem p95 | `python -m estimador.evaluate live` |

Random Forest: removido por estar fora do escopo; não preservado.
