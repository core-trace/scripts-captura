from datetime import datetime

def media_interv_min(lista: list, minutos: int, key: str) -> float:
    soma = 0
    cont = 0

    ult_data = datetime.strptime(
        lista[-1]["TIMESTAMP"],
        '%Y-%m-%d %H:%M:%S'
    )

    for registro in reversed(lista):
        data = datetime.strptime(
            registro["TIMESTAMP"],
            '%Y-%m-%d %H:%M:%S'
        )

        diferenca = (
            ult_data - data
        ).total_seconds()

        if diferenca > minutos * 60:
            break

        soma += float(registro[key])
        cont += 1

    return soma / cont if cont != 0 else 0


def media_uso_componente(lista: list, key: str) -> float:
    soma = 0
    cont = 0

    for registro in lista:
        soma += float(registro[key])
        cont += 1

    return soma / cont if cont != 0 else 0
