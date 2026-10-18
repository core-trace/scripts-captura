
import io
import time
import boto3
import pandas as pd
from botocore.exceptions import ClientError

BUCKET = "bucket-coretrace"
SILVER = "trusted/"
GOLD = "client/"

s3 = boto3.client("s3")


def preparar_gold(df, tipo_arquivo):

    print("Preparando dados para Gold...")

    df = df.copy()

    obrigatorias = ["TIMESTAMP", "ENDERECO_MAC"]

    if tipo_arquivo == "processos":
        obrigatorias.extend(["PID", "NOME"])
    elif tipo_arquivo == "docker":
        obrigatorias.append("CONTAINER")

    faltantes = [
        coluna
        for coluna in obrigatorias
        if coluna not in df.columns
    ]

    if faltantes:
        raise ValueError(
            f"Colunas obrigatorias ausentes: {faltantes}"
        )

    df["TIMESTAMP"] = pd.to_datetime(
        df["TIMESTAMP"],
        errors="coerce"
    )

    df["ENDERECO_MAC"] = (
        df["ENDERECO_MAC"]
        .astype("string")
        .str.strip()
        .str.upper()
    )

    df = df.dropna(
        subset=["TIMESTAMP", "ENDERECO_MAC"]
    )

    df = df[
        df["ENDERECO_MAC"].ne("").fillna(False)
    ]

    df = df.sort_values(
        ["ENDERECO_MAC", "TIMESTAMP"]
    ).reset_index(drop=True)

    print("Registros preparados:", len(df))

    return df


def arquivo_existe(chave):

    try:

        s3.head_object(
            Bucket=BUCKET,
            Key=chave
        )

        return True

    except ClientError as erro:

        codigo = erro.response.get(
            "Error", {}
        ).get("Code")

        if codigo in (
            "404",
            "NoSuchKey",
            "NotFound"
        ):
            return False

        raise


def processar_gold():

    print("Buscando arquivos na Silver...")

    paginator = s3.get_paginator(
        "list_objects_v2"
    )

    paginas = paginator.paginate(
        Bucket=BUCKET,
        Prefix=SILVER
    )

    for pagina in paginas:

        for arquivo in pagina.get("Contents", []):

            chave = arquivo["Key"]

            if not chave.lower().endswith(".csv"):
                continue

            if chave.startswith(SILVER + "metricas/"):
                tipo_arquivo = "metricas"

            elif chave.startswith(SILVER + "processos/"):
                tipo_arquivo = "processos"

            else:
                print("Arquivo ignorado:", chave)
                continue

            nome_arquivo = chave.rsplit("/", 1)[-1]

            print("\nProcessando:", nome_arquivo)

            try:

                resposta = s3.get_object(
                    Bucket=BUCKET,
                    Key=chave
                )

                conteudo = resposta["Body"].read()

                df = pd.read_csv(
                    io.BytesIO(conteudo),
                    low_memory=False,
                    dtype={
                        "ENDERECO_MAC": "string",
                        "NOME": "string",
                        "USUARIO": "string",
                        "CONTAINER": "string"
                    }
                )

                # Docker compartilha a pasta de processos, mas tem outro esquema.
                if "CONTAINER" in df.columns and not {"PID", "NOME"}.issubset(df.columns):
                    tipo_arquivo = "docker"

                pasta_destino = (
                    "processos" if tipo_arquivo == "docker" else tipo_arquivo
                )

                df_gold = preparar_gold(
                    df,
                    tipo_arquivo
                )

                if df_gold.empty:
                    print("Arquivo sem registros validos.")
                    continue

                df_gold["DATA"] = (
                    df_gold["TIMESTAMP"]
                    .dt.strftime("%Y-%m-%d")
                )

                grupos = df_gold.groupby(
                    ["ENDERECO_MAC", "DATA"]
                )

                for (mac, data), grupo in grupos:

                    destino = (
                        f"{GOLD}"
                        f"{pasta_destino}/"
                        f"maquina={mac}/"
                        f"data={data}/"
                        f"{nome_arquivo}"
                    )

                    if arquivo_existe(destino):
                        print(
                            "Arquivo ja existe na Gold. Pulando..."
                        )
                        continue

                    grupo = grupo.drop(
                        columns=["DATA"]
                    )

                    csv_gold = grupo.to_csv(
                        index=False,
                        date_format="%Y-%m-%d %H:%M:%S"
                    )

                    s3.put_object(
                        Bucket=BUCKET,
                        Key=destino,
                        Body=csv_gold.encode("utf-8"),
                        ContentType="text/csv; charset=utf-8"
                    )

                    print("Arquivo enviado para:", destino)

            except (
                ValueError,
                KeyError,
                TypeError,
                pd.errors.ParserError,
                pd.errors.EmptyDataError,
                UnicodeError
            ) as erro:

                print(
                    f"Erro ao processar {nome_arquivo}: {erro}"
                )
                continue

    print("\nProcessamento Gold finalizado!")


if __name__ == "__main__":

    while True:

        try:
            print("\nIniciando processamento Gold...")
            processar_gold()

        except Exception as erro:
            print(
                "Erro durante o processamento:",
                erro
            )

        print("Aguardando 5 minutos...")
        time.sleep(300)
