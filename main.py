
import threading
import time

from capturas import captura
from capturas import captura_docker

parada = threading.Event()


def executar_captura(nome, funcao):
    # Uma falha em uma captura não deve impedir
    # a execução da outra.
    try:
        print(f"[INICIANDO] {nome}")
        funcao()
        print(f"[FINALIZADO] {nome}")

    except Exception as erro:
        print(f"[ERRO] {nome}: {erro}")


threads = []

try:
    while not parada.is_set():
        inicio = time.monotonic()

        # As duas capturas executam em paralelo.
        threads = [
            threading.Thread(
                target=executar_captura,
                args=("Servidor", captura.main)
            ),
            threading.Thread(
                target=executar_captura,
                args=("Docker", captura_docker.main)
            )
        ]

        for thread in threads:
            thread.start()

        # Aguarda a conclusão das duas capturas.
        for thread in threads:
            thread.join()

        tempo_decorrido = time.monotonic() - inicio

        # Inicia a próxima rodada após 60 segundos,
        # descontando o tempo gasto na captura.
        parada.wait(max(0, 60 - tempo_decorrido))

except KeyboardInterrupt:
    print("\nEncerrando capturas...")
    parada.set()

    for thread in threads:
        if thread.is_alive():
            thread.join()

    print("Todas as capturas foram finalizadas.")

    parada.set()

    for thread in threads:
        thread.join()

    print("Todas as capturas foram finalizadas.")

