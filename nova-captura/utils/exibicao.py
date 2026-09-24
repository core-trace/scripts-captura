import cpu.cpu as cpu
import ram.ram as ram
import disco.disco as disco
import swap.swap as swap

def titulo(texto: str):
    print("\n" + "=" * 65)
    print(f"{texto:^65}")
    print("=" * 65)

def resultado(nome: str, valor: float, unidade: str = "", casas: int = 2):
    print(f"{nome:<40} {valor:>15.{casas}f} {unidade}")

def exibir_relatorio(dados: list):
    titulo("RELATÓRIO DE MONITORAMENTO")

    print(f"Registros: {len(dados)}")
    print(f"Início: {dados[0]['TIMESTAMP']}")
    print(f"Fim:    {dados[-1]['TIMESTAMP']}")

    cpu.exibir_cpu(dados)
    ram.exibir_ram(dados)
    disco.exibir_disco(dados)
    swap.exibir_swap(dados)

    titulo("FIM")
    