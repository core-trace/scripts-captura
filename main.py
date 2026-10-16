import threading
import time

from capturas import captura
from capturas import captura_container

parada = threading.Event()

try:
    while not parada.is_set():
        inicio = time.monotonic()

        threads = [
            threading.Thread(target=captura.main),
            threading.Thread(target=captura_container.main),
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        tempo_decorrido = time.monotonic() - inicio

        parada.wait(max(0, 60 - tempo_decorrido))

except KeyboardInterrupt:
    print("\nEncerrando capturas...")
    parada.set()

    for thread in threads:
        thread.join()

    print("Todas as capturas foram finalizadas.")
