import boto3
import pandas as pd
import io
import time
from botocore.exceptions import ClientError
#ve os possiveis erros da aws.



BUCKET = "bucket-coretrace"
BRONZE = "raw/"
SILVER = "trusted/"

s3 = boto3.client("s3")



def transformar_dados(df, tipo_arquivo):

    print("Iniciando transformacao...")

    df = df.copy()

    print("Registros originais:", len(df))


    df.columns = df.columns.str.strip()

    df = df.dropna(how="all")

    # remove espacos e ve se tem textos vazios
    for coluna in df.select_dtypes(include="object").columns:
        df[coluna] = df[coluna].str.strip()
        df[coluna] = df[coluna].replace("", pd.NA)


    if "TIMESTAMP" in df.columns:
        df["TIMESTAMP"] = pd.to_datetime(
            df["TIMESTAMP"],
            errors="coerce"
        )

    # deixa os enderecos MAC iguais
    if "ENDERECO_MAC" in df.columns:
        df["ENDERECO_MAC"] = (
            df["ENDERECO_MAC"].astype("string").str.upper()
        )

       # Ve se as colunas obrigatorias existem
    obrigatorias = ["TIMESTAMP", "ENDERECO_MAC"]

    if tipo_arquivo == "processos":
        obrigatorias.extend(["PID", "NOME"])

    colunas_faltantes = [
        coluna for coluna in obrigatorias
        if coluna not in df.columns
    ]

    if colunas_faltantes:
        raise ValueError(
            f"Arquivo sem colunas obrigatorias: {colunas_faltantes}"
        )


    df = df.dropna(subset=obrigatorias)


    colunas_percentuais = [
        coluna for coluna in df.columns
        if coluna.upper().endswith("_PERCENT")
    ]

    for coluna in colunas_percentuais:
        df[coluna] = pd.to_numeric(
            df[coluna],
            errors="coerce"
        )

        if not (tipo_arquivo == "processos" and coluna.upper() == "CPU_PERCENT"):
            df.loc[
                (df[coluna] < 0) | (df[coluna] > 100),
                coluna
            ] = pd.NA
        else:
            df.loc[df[coluna] < 0, coluna] = pd.NA


    colunas_numericas = [
        "RAM_USED",
        "RAM_AVAILABLE",
        "DISCO_USED",
        "DISCO_FREE",
        "SWAP_IN",
        "SWAP_OUT",
        "CPU_FREQ_ATUAL",
        "REDE_BYTES_ENVIADOS_S",
        "REDE_BYTES_RECEBIDOS_S",
        "REDE_PACOTES_ENVIADOS_S",
        "REDE_PACOTES_RECEBIDOS_S",
        "REDE_ERROS",
        "REDE_CONEXOES_ESTABELECIDAS"
    ]

    for coluna in colunas_numericas:

        if coluna in df.columns:

            df[coluna] = pd.to_numeric(
                df[coluna],
                errors="coerce"
            )


            df.loc[df[coluna] < 0, coluna] = pd.NA



    
    if tipo_arquivo == "processos":

        df["PID"] = pd.to_numeric(
            df["PID"],
            errors="coerce"
        )

        df = df.dropna(subset=["PID", "NOME"])

        df = df[df["PID"] >= 0]


        df = df.drop_duplicates(
            subset=["TIMESTAMP", "ENDERECO_MAC", "PID"]
        )

    else:
 
        df = df.drop_duplicates()

# por ordem de data
    df = df.sort_values("TIMESTAMP")

    print("Registros tratados:", len(df))
    print("Transformacao concluida!")

    return df



def processar_arquivos():

    print("Buscando arquivos na camada Bronze...")

    paginator = s3.get_paginator("list_objects_v2")

    paginas = paginator.paginate(
        Bucket=BUCKET,
        Prefix=BRONZE
    )

    for pagina in paginas:

        for arquivo in pagina.get("Contents", []):

            chave = arquivo["Key"]

#funciona so se estiver lendo um csv
            if not chave.lower().endswith(".csv"):
                continue

            nome_arquivo = chave.split("/")[-1]

          
            if nome_arquivo.startswith("processos_"):
                tipo_arquivo = "processos"

            elif nome_arquivo.startswith("coretrace_coleta_"):
                tipo_arquivo = "metricas"

            else:
                print("Arquivo ignorado:", nome_arquivo)
                continue

            destino = SILVER + nome_arquivo

            print("\nProcessando:", nome_arquivo)


            try:
                s3.head_object(
                    Bucket=BUCKET,
                    Key=destino
                )

                print("Arquivo ja existe na Silver. Pulando...")
                continue

            except ClientError as erro:
                codigo = erro.response["Error"]["Code"]

                if codigo not in ("404", "NoSuchKey", "NotFound"):
                    raise

            # le o csv da Bronze
            resposta = s3.get_object(
                Bucket=BUCKET,
                Key=chave
            )

            conteudo = resposta["Body"].read()

            df = pd.read_csv(
                io.BytesIO(conteudo)
            )


            
            try:
                df_limpo = transformar_dados(
                    df,
                    tipo_arquivo
                )

            except (ValueError, KeyError, TypeError) as erro:
                print(f"Erro ao tratar {nome_arquivo}: {erro}")
                continue


            csv_tratado = df_limpo.to_csv(
                index=False,
                date_format="%Y-%m-%d %H:%M:%S"
            )


            s3.put_object(
                Bucket=BUCKET,
                Key=destino,
                Body=csv_tratado.encode("utf-8"),
                ContentType="text/csv; charset=utf-8",
            
            )

            print("Arquivo enviado para:", destino)

    print("\n tratamento finalizado!")


if __name__ == "__main__":
  while True:

        try:
            print("\nIniciando tratamento dos arquivos...")

            processar_arquivos()

            print("Aguardando 5 minutos...")

        except Exception as erro:
            print("Erro durante o processamento:", erro)

        time.sleep(300)