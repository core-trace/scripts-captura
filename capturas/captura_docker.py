
import json
import boto3

from pathlib import Path
from .containers import metricas, logs, eventos


BUCKET = "bucket-coretrace"
PASTA_S3 = "raw/docker"

# Mantemos os arquivos dentro da pasta de containers.
DIRETORIO_BASE = Path(metricas.__file__).resolve().parent
DIRETORIO_CSV = DIRETORIO_BASE / "csv"
DIRETORIO_PENDENTES = DIRETORIO_BASE / "estado" / "pendentes"


def criar_pendencia(tipo, caminho_csv, checkpoint=None):
    """
    Registra um CSV que ainda precisa ser enviado ao S3.

    O checkpoint fica associado ao CSV para que ele
    só seja confirmado após o upload.
    """

    DIRETORIO_PENDENTES.mkdir(
        parents=True,
        exist_ok=True
    )

    caminho_csv = Path(caminho_csv).resolve()

    pendencia = {
        "arquivo": str(caminho_csv),
        "chave_s3": f"{PASTA_S3}/{tipo}/{caminho_csv.name}",
        "checkpoint": checkpoint
    }

    caminho_pendencia = DIRETORIO_PENDENTES / f"{tipo}.json"
    temporario = caminho_pendencia.with_suffix(".tmp")

    # Escreve primeiro em um arquivo temporário.
    # Isso reduz o risco de deixar um JSON incompleto.
    with open(temporario, "w", encoding="utf-8") as arquivo:
        json.dump(
            pendencia,
            arquivo,
            indent=4,
            ensure_ascii=False
        )

    temporario.replace(caminho_pendencia)


def enviar_pendente(tipo, s3, confirmar_checkpoint=None):
    """
    Envia o CSV pendente e confirma o checkpoint
    somente depois do sucesso no S3.

    Retorna True se não houver pendências ou se o
    envio for concluído. False se o envio falhar.
    """

    caminho_pendencia = DIRETORIO_PENDENTES / f"{tipo}.json"

    if not caminho_pendencia.exists():
        return True

    with open(caminho_pendencia, "r", encoding="utf-8") as arquivo:
        pendencia = json.load(arquivo)

    caminho_csv = Path(pendencia["arquivo"])
    chave_s3 = pendencia["chave_s3"]

    # Se o arquivo desapareceu, interrompemos esse tipo
    # de captura. Não podemos avançar o checkpoint.
    if not caminho_csv.is_file():
        print(f"[ERRO] Arquivo pendente não encontrado: {caminho_csv}")
        return False

    try:
        # Envia o arquivo CSV diretamente do disco.
        s3.upload_file(
            Filename=str(caminho_csv),
            Bucket=BUCKET,
            Key=chave_s3,
            ExtraArgs={
                "ContentType": "text/csv; charset=utf-8"
            }
        )

        print(f"[S3] Enviado: s3://{BUCKET}/{chave_s3}")

    except Exception as erro:
        # Preservamos o arquivo e a pendência local.
        # Na próxima execução tentaremos novamente.
        print(f"[S3] Falha no envio de {tipo}: {erro}")
        return False

    # O upload terminou com sucesso.
    # Agora podemos confirmar a posição processada.
    checkpoint = pendencia.get("checkpoint")

    if confirmar_checkpoint is not None and checkpoint is not None:
        confirmar_checkpoint(checkpoint)

    # Só removemos a pendência depois de confirmar
    # o checkpoint. O CSV local é preservado.
    caminho_pendencia.unlink()

    return True


def processar_coleta(
    tipo,
    coletor,
    gerador_csv,
    s3,
    confirmar_checkpoint=None
):
    """
    Fluxo comum para métricas, logs e eventos:

    1. Reenvia possíveis arquivos pendentes.
    2. Executa a captura.
    3. Gera um CSV.
    4. Registra a pendência.
    5. Envia ao S3.
    6. Confirma o checkpoint após o envio.
    """

    # Primeiro resolvemos a pendência anterior.
    # Isso evita abandonar um CSV quando o S3 falhar.
    if not enviar_pendente(
        tipo,
        s3,
        confirmar_checkpoint
    ):
        print(f"[{tipo.upper()}] Upload pendente. Nova coleta adiada.")
        return

    # Todas as funções de coleta devem retornar:
    # (lista_de_registros, checkpoint)
    dados, checkpoint = coletor()

    if not dados:
        print(f"[{tipo.upper()}] Nenhum registro novo.")

        # Se não houve registros, não há CSV para enviar.
        # Podemos salvar a posição da consulta concluída.
        if confirmar_checkpoint is not None and checkpoint is not None:
            confirmar_checkpoint(checkpoint)

        return

    # O CSV é gerado antes de qualquer tentativa de envio.
    caminho_csv = gerador_csv(dados)

    if caminho_csv is None:
        raise RuntimeError(
            f"A coleta {tipo} retornou dados, mas não gerou CSV."
        )

    print(
        f"[{tipo.upper()}] "
        f"{len(dados)} registros capturados."
    )

    # Registra o arquivo e o checkpoint associado.
    # Se o envio falhar, ambos poderão ser recuperados.
    criar_pendencia(
        tipo,
        caminho_csv,
        checkpoint
    )

    # Tenta entregar o arquivo recém-gerado.
    enviar_pendente(
        tipo,
        s3,
        confirmar_checkpoint
    )


def main():
    """
    Executa uma rodada completa de captura Docker.

    Não possui while infinito: o main principal do
    CoreTrace controla o agendamento das coletas.
    """

    print("\n[DOCKER] Iniciando rodada de captura...")

    # Uma única instância do cliente S3 por rodada.
    s3 = boto3.client("s3")

    # Utilizamos o mesmo MAC nos três arquivos.
    endereco_mac = metricas.obter_endereco_mac()

    tarefas = [
        (
            "metricas",

            # Captura de todos os containers.
            # Armazenamento desativado por padrão para
            # reduzir o custo das coletas frequentes.
            lambda: (
                metricas.capturar_metricas(
                    incluir_armazenamento=False,
                    endereco_mac=endereco_mac
                ),
                None
            ),

            lambda dados: metricas.gerar_csv(
                dados,
                diretorio=DIRETORIO_CSV
            ),

            None
        ),
        (
            "logs",

            # Captura apenas mensagens ainda não processadas.
            lambda: logs.capturar_logs(
                endereco_mac=endereco_mac
            ),

            logs.gerar_csv,
            logs.salvar_estado
        ),
        (
            "eventos",

            # Captura acontecimentos desde o checkpoint.
            lambda: eventos.capturar_eventos(
                endereco_mac=endereco_mac
            ),

            eventos.gerar_csv,
            eventos.salvar_estado
        )
    ]

    # Tratamos erros individualmente.
    # Uma falha em métricas não deve impedir a
    # execução das capturas de logs e eventos.
    for tipo, coletor, gerador, confirmar in tarefas:
        try:
            processar_coleta(
                tipo=tipo,
                coletor=coletor,
                gerador_csv=gerador,
                s3=s3,
                confirmar_checkpoint=confirmar
            )

        except Exception as erro:
            print(f"[ERRO] Captura de {tipo}: {erro}")

    print("[DOCKER] Rodada finalizada.\n")


if __name__ == "__main__":
    main()
