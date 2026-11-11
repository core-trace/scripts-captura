
import csv
import io
import uuid
import boto3

from datetime import datetime, timezone
from capturas.containers import metricas, logs, eventos


BUCKET = "bucket-coretrace"
PASTA_S3 = "raw/docker"


def enviar_csv_s3(s3, tipo, dados, colunas):
    # Não criamos arquivos quando não existem registros.
    if not dados:
        return None

    # StringIO cria um arquivo de texto em memória.
    # Assim, não precisamos salvar um CSV no disco.
    buffer = io.StringIO(newline="")

    try:
        # Monta o CSV diretamente na memória,
        # mantendo as colunas definidas em cada módulo.
        escritor = csv.DictWriter(
            buffer,
            fieldnames=colunas,
            delimiter=";"
        )

        escritor.writeheader()
        escritor.writerows(dados)

        # Converte o conteúdo em bytes para enviar ao S3.
        conteudo = buffer.getvalue().encode("utf-8")

    finally:
        # Libera o buffer após obter seu conteúdo.
        buffer.close()

    # Nome único para cada lote de captura.
    horario = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )

    identificador = uuid.uuid4().hex[:8]

    nome_arquivo = (
        f"{tipo}_{horario}_{identificador}.csv"
    )

    # Separamos os CSVs por tipo para facilitar a ETL.
    chave = f"{PASTA_S3}/{tipo}/{nome_arquivo}"

    # Envia diretamente para o S3, sem arquivo local.
    # Se ocorrer uma exceção, ela será tratada pela
    # função principal e o checkpoint não avançará.
    s3.put_object(
        Bucket=BUCKET,
        Key=chave,
        Body=conteudo,
        ContentType="text/csv; charset=utf-8"
    )

    print(f"[S3] Enviado: s3://{BUCKET}/{chave}")

    return chave


def processar_coleta(
    s3,
    tipo,
    coletor,
    colunas,
    confirmar_checkpoint=None
):
    # Cada coletor retorna os registros e, quando
    # aplicável, o estado da leitura realizada.
    dados, checkpoint = coletor()

    if dados:
        # O upload precisa terminar com sucesso antes
        # de confirmarmos que os registros foram processados.
        enviar_csv_s3(
            s3=s3,
            tipo=tipo,
            dados=dados,
            colunas=colunas
        )

        print(
            f"[{tipo.upper()}] "
            f"{len(dados)} registros enviados."
        )

    else:
        print(f"[{tipo.upper()}] Nenhum registro novo.")

    # Logs e eventos possuem checkpoints.
    #
    # Se havia dados, só chegamos aqui após o upload.
    # Se não havia dados, a consulta foi concluída
    # e podemos atualizar o estado normalmente.
    if (
        confirmar_checkpoint is not None
        and checkpoint is not None
    ):
        confirmar_checkpoint(checkpoint)


def main():
    print("\n[DOCKER] Iniciando rodada de captura...")

    # Uma conexão S3 compartilhada pelos três módulos.
    s3 = boto3.client("s3")

    # Usa o mesmo MAC em métricas, logs e eventos.
    endereco_mac = metricas.obter_endereco_mac()

    # Cada módulo mantém sua responsabilidade.
    # O orquestrador apenas executa e envia os dados.
    tarefas = [
        (
            "metricas",
            lambda: (
                metricas.capturar_metricas(
                    incluir_armazenamento=False,
                    endereco_mac=endereco_mac
                ),
                None
            ),
            metricas.COLUNAS_METRICAS,
            None
        ),
        (
            "logs",
            lambda: logs.capturar_logs(
                endereco_mac=endereco_mac
            ),
            logs.COLUNAS_LOGS,
            logs.salvar_estado
        ),
        (
            "eventos",
            lambda: eventos.capturar_eventos(
                endereco_mac=endereco_mac
            ),
            eventos.COLUNAS_EVENTOS,
            eventos.salvar_estado
        )
    ]

    # Executamos as capturas separadamente.
    # Uma falha em um módulo não deve impedir
    # que os outros dois sejam executados.
    for tipo, coletor, colunas, confirmar in tarefas:
        try:
            processar_coleta(
                s3=s3,
                tipo=tipo,
                coletor=coletor,
                colunas=colunas,
                confirmar_checkpoint=confirmar
            )

        except Exception as erro:
            # Em caso de falha no envio, o checkpoint
            # não é atualizado.
            #
            # Na próxima execução, logs e eventos
            # poderão tentar recuperar os registros.
            print(
                f"[ERRO] Captura de {tipo}: {erro}"
            )

    print("[DOCKER] Rodada finalizada.\n")


if __name__ == "__main__":
    main()
