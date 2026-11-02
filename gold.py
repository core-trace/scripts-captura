
import io
import time
import boto3
import pandas as pd
from botocore.exceptions import ClientError
import json

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


# Função pra converter csv para json

def converter_registros(df):

    # O drop pra tirar a coluna de data pq hoje em dia usamos a data como parêmetro só pra agrupar os dados
    # Ignorar erros pq se algum arquivo vier sem essa coluna ele só ignora isso sem dar erro que trave tudo
    df = df.drop(columns=["DATA"], errors="ignore")

    # Lê o texto json e transforma numa estrutura de lista com dicionários que o python entende
    return json.loads(
        # Transforma em json
        df.to_json(
            # Cada linha/registro vira um objeto. A coluna vira a chave e o conteúdo vira o valor
            orient="records",
            date_format="iso",
            # Pros acentos e ç e ã ficarem sem bugar
            force_ascii=False
        )
    )

def processar_gold():

    print("Buscando arquivos na Silver...")

    paginator = s3.get_paginator(
        "list_objects_v2"
    )

    paginas = paginator.paginate(
        Bucket=BUCKET,
        Prefix=SILVER
    )

    documentos = {}

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

                    chave_documento = (mac, data)

                    if chave_documento not in documentos:
                        documentos[chave_documento] = {
                            "endereco_mac": mac,
                            "data": data,
                            "metricas": [],
                            "processos": [],
                            "containers": []
                        }

                    campo = {
                        "metricas": "metricas",
                        "processos": "processos",
                        "docker": "containers"
                    }[tipo_arquivo]

                    documentos[chave_documento][campo].extend(
                        converter_registros(grupo)
                    )

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
                # Interrompe antes de enviar
                raise

    for (mac, data), documento in documentos.items():
        
        for campo in ["metricas", "processos", "containers"]:
            tabela = pd.DataFrame(documento[campo])

            if not tabela.empty:
                tabela = tabela.drop_duplicates()
                tabela = tabela.sort_values("TIMESTAMP", kind="stable")

                documento[campo] = converter_registros(tabela)
        
        destino = (
            f"{GOLD}maquina={mac}/"
            f"data={data}/capturas.json"
        )

        conteudo = json.dumps(
            documento,
            ensure_ascii=False,
            allow_nan=False
        )

        s3.put_object(
            Bucket=BUCKET,
            Key=destino,
            Body=conteudo.encode("utf-8"),
            ContentType="application/json"
        )

        print("Arquivo JSON enviado para:", destino)

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
