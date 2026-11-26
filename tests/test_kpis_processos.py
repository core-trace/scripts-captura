"""Validacao offline dos KPIs de processo, sem S3 e sem chamadas HTTP."""
import json
import re
import unittest
from copy import deepcopy
from pathlib import Path

import pandas as pd

from formato_gold import organizar_processo
from resumos_gold import AcumuladorProcessos, instante_utc
from silver import transformar_dados

RAIZ = Path(__file__).resolve().parents[1]
CAMINHO_CSV = RAIZ / "ferramentas" / "saida-local" / "processos_20261119T220040.csv"
INSTANTE_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def converter_registros(df):
    # Mesma conversao do gold.py: DataFrame da Silver -> lista de dicionarios.
    return json.loads(
        df.to_json(
            orient="records",
            date_format="iso",
            date_unit="us",
            double_precision=15,
            force_ascii=False
        )
    )


class TestKpisProcessos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.csv = pd.read_csv(CAMINHO_CSV)
        limpo = transformar_dados(cls.csv, "processos")
        cls.registros = [
            organizar_processo(registro)
            for registro in converter_registros(limpo)
        ]
        cls.kpis = cls._calcular(cls.registros)

    @staticmethod
    def _calcular(registros):
        acumulador = AcumuladorProcessos()
        for registro in registros:
            acumulador.adicionar(registro)
        return acumulador.calcular()

    @classmethod
    def _maximo_csv(cls, coluna):
        # CSV antigo usa CPU_PERCENT/MEMORIA_PERCENT; o coletor novo usa o prefixo P_.
        if coluna not in cls.csv.columns:
            coluna = coluna.replace("P_", "", 1)
        coluna_status = coluna + "_STATUS"
        valores = cls.csv[coluna]
        if coluna_status in cls.csv.columns:
            valores = valores[
                cls.csv[coluna_status].astype("string").str.strip().str.lower().eq("ok")
            ]
        return float(pd.to_numeric(valores, errors="coerce").dropna().max())

    def test_json_resultante(self):
        # Mostra exatamente o conteudo que o gold.py grava em "kpis_processos".
        print("\nkpis_processos (CSV real):")
        print(json.dumps(self.kpis, ensure_ascii=False, indent=2))
        self.assertIn("pico_cpu", self.kpis)
        self.assertIn("pico_memoria", self.kpis)

    def test_pico_cpu_bate_com_maximo_real(self):
        destaque = self.kpis["pico_cpu"]["destaque"]
        self.assertIsNotNone(destaque)
        self.assertEqual(
            destaque["pico_cpu_percent"],
            self._maximo_csv("P_CPU_PERCENT")
        )

    def test_pico_memoria_bate_com_maximo_real(self):
        destaque = self.kpis["pico_memoria"]["destaque"]
        self.assertIsNotNone(destaque)
        self.assertEqual(
            destaque["pico_memoria_percent"],
            self._maximo_csv("P_MEMORIA_PERCENT")
        )

    def test_instante_pico_em_iso_utc(self):
        for chave in ("pico_cpu", "pico_memoria"):
            instante = self.kpis[chave]["destaque"]["instante_pico"]
            self.assertRegex(instante, INSTANTE_ISO_UTC)
            self.assertEqual(
                instante,
                instante_utc(instante).isoformat().replace("+00:00", "Z")
            )

    def test_leituras_aquecendo_sao_ignoradas(self):
        # Registros derivados de linhas reais: so o status e o valor mudam, pra provar
        # que "aquecendo" nao entra na conta mesmo com um valor acima do pico do dia.
        registro_cpu = deepcopy(self.registros[0])
        registro_cpu["cpu"]["uso"]["valor"] = self._maximo_csv("P_CPU_PERCENT") + 1000
        registro_cpu["cpu"]["uso"]["status"] = "aquecendo"
        registro_cpu["memoria"]["uso_percentual"]["status"] = "aquecendo"

        registro_memoria = deepcopy(self.registros[0])
        registro_memoria["memoria"]["uso_percentual"]["valor"] = (
            self._maximo_csv("P_MEMORIA_PERCENT") + 1000
        )
        registro_memoria["memoria"]["uso_percentual"]["status"] = "aquecendo"
        registro_memoria["cpu"]["uso"]["status"] = "aquecendo"

        obtido = self._calcular(self.registros + [registro_cpu, registro_memoria])

        antes_cpu = self.kpis["pico_cpu"]["destaque"]
        depois_cpu = obtido["pico_cpu"]["destaque"]
        self.assertEqual(depois_cpu["pico_cpu_percent"], antes_cpu["pico_cpu_percent"])
        self.assertEqual(depois_cpu["amostras_validas"], antes_cpu["amostras_validas"])

        antes_memoria = self.kpis["pico_memoria"]["destaque"]
        depois_memoria = obtido["pico_memoria"]["destaque"]
        self.assertEqual(
            depois_memoria["pico_memoria_percent"],
            antes_memoria["pico_memoria_percent"]
        )
        self.assertEqual(
            depois_memoria["amostras_validas"],
            antes_memoria["amostras_validas"]
        )


if __name__ == "__main__":
    unittest.main()
