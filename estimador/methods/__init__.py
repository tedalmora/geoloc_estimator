"""Um módulo por método de geolocalização (cell_id, timing_advance, fingerprint, aoa).

Cada um expõe funções `train_*`/`predict_*` (ou, no caso do aoa, uma função geométrica
pura, sem treino) -- o dispatcher em estimador/train.py e estimador/infer.py chama a
função certa conforme o `--metodo`/subcomando escolhido na linha de comando.

Ver o README desta pasta (estimador/README.md) para a tabela completa de qual método
serve para qual tipo de geolocalização e o que cada um precisa como entrada.
"""
