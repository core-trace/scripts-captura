
import io
import time
import boto3
import numpy as np
import pandas as pd
from botocore.exceptions import ClientError

BUCKET = "bucket-coretrace"
BRONZE = "raw/"
SILVER = "trusted/"

s3 = boto3.client("s3")

CAMPOS_TEXTO = {
    "timestamp",
    "endereco_mac",
    "nome",
    "usuario",
    "container",
    "cpu_schema_version",
    "cenario",
    "experimento",
    "ram_oom_boot_id",

    # Identificação do Docker
    "container_id",
    "imagem",
    "status",
    "oom_killed",

    # Datas do contêiner
    "started_at",
    "finished_at",

    # Eventos do Docker
    "event_id",
    "event_type",
    "event_action",
    "event_details",

    # Logs do Docker
    "log_level",
    "log_mensagem"
}


SUFIXOS_TEXTO = (
    "_status",
    "_montagem",
    "_dispositivo",
    "_tipo_sistema"
)

PERCENTUAIS_CPU = {
    "cpu_user",
    "cpu_system",
    "cpu_idle",
    "cpu_iowait"
}


def eh_texto(coluna):
    nome = coluna.lower()

    return (
        nome in CAMPOS_TEXTO
        or nome.endswith(SUFIXOS_TEXTO)
    )


def eh_percentual(coluna):
    nome = coluna.lower()

    return (
        nome.endswith("_percent")
        or nome in PERCENTUAIS_CPU
        or nome.startswith("cpu_percent_percpu_")
    )


def identificar_tipo(df):

    colunas = set(
        df.columns.astype(str).str.strip().str.upper()
    )

    obrigatorias = {"TIMESTAMP", "ENDERECO_MAC"}

    if not obrigatorias.issubset(colunas):
        raise ValueError(
            "CSV sem TIMESTAMP ou ENDERECO_MAC"
        )

    if {"PID", "NOME"}.issubset(colunas):
        return "processos"

    if "CONTAINER" in colunas:

        # Identifica eventos pelas colunas específicas
        if {"EVENT_TYPE", "EVENT_ACTION"}.issubset(colunas):
            return "docker_eventos"

        # Identifica logs pela coluna de mensagem
        if "LOG_MENSAGEM" in colunas:
            return "docker_logs"

        # Identifica métricas pelas colunas de utilização
        if {"CPU_PERCENT", "RAM_PERCENT"}.issubset(colunas):
            return "docker_metricas"

        raise ValueError("Tipo de CSV Docker desconhecido")

    return "metricas"


def transformar_dados(df, tipo_arquivo):

    if tipo_arquivo not in (
        "metricas",
        "processos",
        "docker_metricas",
        "docker_eventos",
        "docker_logs"
    ):
        raise ValueError(
            f"Tipo de arquivo desconhecido: {tipo_arquivo}"
        )

    print("Iniciando transformacao...")

    df = df.copy()

    print("Registros originais:", len(df))

    df.columns = df.columns.astype(str).str.strip().str.upper()

    if df.columns.duplicated().any():
        raise ValueError(
            "CSV com nomes de colunas duplicados"
        )

    obrigatorias = ["TIMESTAMP", "ENDERECO_MAC"]

    if tipo_arquivo == "processos":
        obrigatorias.extend(["PID", "NOME"])

    elif tipo_arquivo.startswith("docker_"):
        obrigatorias.extend(["CONTAINER_ID", "CONTAINER"])

    faltantes = [
        coluna
        for coluna in obrigatorias
        if coluna not in df.columns
    ]

    if faltantes:
        raise ValueError(
            f"Colunas obrigatorias ausentes: {faltantes}"
        )

    # Identifica as colunas que podem passar pela limpeza.
    # A mensagem de log deve manter sua formatação original.
    colunas_limpeza = [
        coluna
        for coluna in df.columns
        if not (
            tipo_arquivo == "docker_logs"
            and coluna == "LOG_MENSAGEM"
        )
    ]

    df[colunas_limpeza] = df[colunas_limpeza].replace(
        r"^\s*$",
        pd.NA,
        regex=True
    )

    df = df.dropna(how="all")

    for coluna in df.select_dtypes(
        include=["object", "string"]
    ).columns:

        # Converte para string mantendo um tipo consistente.
        df[coluna] = df[coluna].astype("string")

        # Não remove espaços das mensagens de logs,
        # pois a indentação pode ter significado.
        if tipo_arquivo == "docker_logs" and coluna == "LOG_MENSAGEM":
            continue

        # Mantém a limpeza original nas demais colunas.
        df[coluna] = df[coluna].str.strip()

        df[coluna] = df[coluna].replace("", pd.NA)

        if tipo_arquivo.startswith("docker_"):

            # Os timestamps do Docker utilizam formato ISO 8601,
            # com precisão de até nanossegundos e fuso UTC.
            df["TIMESTAMP"] = pd.to_datetime(
                df["TIMESTAMP"],
                errors="coerce",
                format="ISO8601",
                utc=True
            )

        else:

            # Mantém o comportamento original para os CSVs antigos.
            df["TIMESTAMP"] = pd.to_datetime(
                df["TIMESTAMP"],
                errors="coerce"
            )

    df["ENDERECO_MAC"] = (
        df["ENDERECO_MAC"]
        .astype("string")
        .str.upper()
    )

    df = df.dropna(
        subset=["TIMESTAMP", "ENDERECO_MAC"]
    )

    df = df[
        df["ENDERECO_MAC"].ne("").fillna(False)
    ]

    if tipo_arquivo == "docker_metricas":
        # Converte as datas de início e encerramento.
        # Datas inválidas ou não definidas tornam-se NaT.
        for coluna in ("STARTED_AT", "FINISHED_AT"):

            if coluna in df.columns:
                df[coluna] = pd.to_datetime(
                    df[coluna],
                    errors="coerce",
                    format="ISO8601",
                    utc=True
                )

    colunas_numericas = [
        coluna
        for coluna in df.columns
        if not eh_texto(coluna)
    ]

    for coluna in colunas_numericas:

        numeros = pd.to_numeric(
            df[coluna],
            errors="coerce"
        )

        numeros = numeros.replace(
            [np.inf, -np.inf],
            np.nan
        )

        numeros = numeros.mask(numeros < 0)

        if eh_percentual(coluna):

            if not (
                tipo_arquivo in ("processos", "docker_metricas")
                and coluna.lower() == "cpu_percent"    
            ):
                numeros = numeros.mask(numeros > 100)

        status_coluna = coluna + "_STATUS"

        if status_coluna in df.columns:

            status = (
                df[status_coluna]
                .astype("string")
                .str.strip()
                .str.lower()
            )

            numeros = numeros.mask(
                status.ne("ok").fillna(True)
            )

        df[coluna] = numeros

    if tipo_arquivo == "processos":

        df = df.dropna(
            subset=["PID", "NOME"]
        )

        df = df.drop_duplicates(
            subset=[
                "TIMESTAMP",
                "ENDERECO_MAC",
                "PID"
            ]
        )

    elif tipo_arquivo in ("metricas", "docker_metricas"):
        df = df.drop_duplicates()

    df = df.sort_values("TIMESTAMP")
    df = df.reset_index(drop=True)

    print("Registros tratados:", len(df))
    print("Transformacao concluida!")

    return df


def processar_arquivos():

    print("Buscando arquivos na Bronze...")

    paginator = s3.get_paginator("list_objects_v2")

    paginas = paginator.paginate(
        Bucket=BUCKET,
        Prefix=BRONZE
    )

    for pagina in paginas:

        for arquivo in pagina.get("Contents", []):

            chave = arquivo["Key"]
            nome_arquivo = chave.rsplit("/", 1)[-1]

            if not nome_arquivo.lower().endswith(".csv"):
                continue

            print("\nProcessando:", nome_arquivo)

            try:

                resposta = s3.get_object(
                    Bucket=BUCKET,
                    Key=chave
                )

                conteudo = resposta["Body"].read()

                df = pd.read_csv(
                    io.BytesIO(conteudo),

                    # Aceitar qualquer separador
                    sep=None,
                    engine="python",
                    dtype={
                        "ENDERECO_MAC": "string",
                        "NOME": "string",
                        "USUARIO": "string",
                        "CONTAINER": "string"
                    }
                )

                tipo_arquivo = identificar_tipo(df)

                # Relaciona cada tipo de arquivo à sua pasta na Silver.
                # Mantemos as pastas antigas e separamos os dados do Docker.
                pastas_destino = {
                    "metricas": "metricas",
                    "processos": "processos",
                    "docker_metricas": "docker/metricas",
                    "docker_eventos": "docker/eventos",
                    "docker_logs": "docker/logs"
                }

                # Obtém a pasta correspondente ao tipo identificado.
                pasta_destino = pastas_destino[tipo_arquivo]

                # Monta o caminho completo do arquivo na Silver.
                destino = f"{SILVER}{pasta_destino}/{nome_arquivo}"

                try:

                    s3.head_object(
                        Bucket=BUCKET,
                        Key=destino
                    )

                    print(
                        "Arquivo ja existe na Silver. Pulando..."
                    )
                    continue

                except ClientError as erro:

                    codigo = erro.response.get(
                        "Error", {}
                    ).get("Code")

                    if codigo not in (
                        "404",
                        "NoSuchKey",
                        "NotFound"
                    ):
                        raise

                df_limpo = transformar_dados(
                    df,
                    tipo_arquivo
                )

                if df_limpo.empty:
                    print("Arquivo sem registros validos.")
                    continue

                if tipo_arquivo.startswith("docker_"):

                    # Preserva a precisão dos timestamps do Docker.
                    csv_tratado = df_limpo.to_csv(
                        index=False
                    )

                else:

                    # Mantém o formato original dos arquivos antigos.
                    csv_tratado = df_limpo.to_csv(
                        index=False,
                        date_format="%Y-%m-%d %H:%M:%S"
                    )

                s3.put_object(
                    Bucket=BUCKET,
                    Key=destino,
                    Body=csv_tratado.encode("utf-8"),
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

    print("\nTratamento finalizado!")


if __name__ == "__main__":

    while True:

        try:
            print("\nIniciando tratamento dos arquivos...")
            processar_arquivos()

        except Exception as erro:
            print("Erro durante o processamento:", erro)

        print("Aguardando 5 minutos...")
        time.sleep(300)
