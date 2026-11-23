# CoreTrace-codigosCaptura

O `client/resumos/maquina=.../data=.../resumo.json` inclui os logs completos
no campo `logs`, na raiz, com o mesmo formato dos lotes: `timestamp`,
`container_id`, `nome`, `nivel`, `mensagem` e `id_captura`.
As contagens por container e nível continuam em `resumos.logs`.

Os logs são separados por máquina e pela data do registro no fuso
`America/Sao_Paulo`, ordenados cronologicamente e deduplicados entre lotes
pela mesma regra usada nas contagens. Dias sem logs recebem `logs: []`.
Executar a Gold atualizada reconstrói os resumos a partir dos CSVs da Silver,
mesmo quando os lotes já foram publicados.

Validação local, sem acesso à AWS: `python -m unittest discover -s tests -v`.
