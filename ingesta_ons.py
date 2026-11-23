#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FIBRX | Motor independiente de descubrimiento y consulta de ON.

ETAPA 1: diagnóstico de identificación y cobertura de fuentes.
- Consulta el catálogo público de CompararFondos.
- Contrasta símbolos en el panel de deuda corporativa de Data912.
- No modifica ingesta_tasas.py.
- No escribe ni modifica tasas_mercado.json.
- No calcula TIR ni flujos por cuenta propia.
- No convierte una especie O en D: solo informa candidatos observados.

Ejecutar: python ingesta_ons.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

TIMEOUT = 35
COMPARARFONDOS_URL = "https://compararfondos.com.ar/api/bonos"
DATA912_URL = "https://data912.com/live/arg_corp"

# Cada emisión mantiene el ticker informado originalmente y, por separado,
# el posible ticker D que debe verificarse contra los catálogos públicos.
# Los candidatos D no se consideran confirmados hasta aparecer en la fuente.
INSTRUMENTOS = [
    {
        "emision": "CGC Clase 36",
        "emisor_esperado": "Compañía General de Combustibles S.A.",
        "tickers_informados": ["CP36D"],
        "candidatos_mep": ["CP36D"],
        "isin": "AR0015464982",
    },
    {
        "emision": "YPF Clase XXXIV",
        "emisor_esperado": "YPF S.A.",
        "tickers_informados": ["YM34D"],
        "candidatos_mep": ["YM34D"],
        "isin": "USP989MJBY67",
    },
    {
        "emision": "Vista Energy Clase XXIX",
        "emisor_esperado": "Vista Energy Argentina S.A.U.",
        "tickers_informados": ["VSCVD"],
        "candidatos_mep": ["VSCVD"],
        "isin": "USP9659RAB44",
    },
    {
        "emision": "AES Argentina Generación Clase 3",
        "emisor_esperado": "AES Argentina Generación",
        "tickers_informados": ["AEC3O"],
        "candidatos_mep": ["AEC3D"],
        "isin": None,
    },
    {
        "emision": "IRSA 8% 2035",
        "emisor_esperado": "IRSA Inversiones y Representaciones S.A.",
        "tickers_informados": ["IRCPO"],
        "candidatos_mep": ["IRCPD"],
        "isin": None,
    },
    {
        "emision": "Pampa Energía 7,75% 2037",
        "emisor_esperado": "Pampa Energía S.A.",
        "tickers_informados": ["MGCRO"],
        "candidatos_mep": ["MGCRD"],
        "isin": None,
    },
    {
        "emision": "PCR Clase S 2031",
        "emisor_esperado": "Petroquímica Comodoro Rivadavia S.A.",
        "tickers_informados": ["PQCSO"],
        "candidatos_mep": ["PQCSD"],
        "isin": None,
    },
    {
        "emision": "Telecom Argentina 9,25% 2033",
        "emisor_esperado": "Telecom Argentina S.A.",
        "tickers_informados": ["TLCPO"],
        "candidatos_mep": ["TLCPD"],
        "isin": None,
    },
    {
        "emision": "Vista Energy 7,875% 2038",
        "emisor_esperado": "Vista Energy Argentina S.A.U.",
        "tickers_informados": ["VSCXO"],
        "candidatos_mep": ["VSCXD"],
        "isin": None,
    },
]


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "FIBRX-ON-Discovery/1.0 (+https://github.com/lalobaronetto/fibrx-tablero)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Error de conexión: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("La respuesta no contiene JSON válido") from exc


def extract_compararfondos(payload: Any) -> tuple[list[dict], dict]:
    if not isinstance(payload, dict):
        raise RuntimeError("CompararFondos no devolvió un objeto JSON")
    bonds = payload.get("bonds")
    if not isinstance(bonds, list):
        raise RuntimeError("No se encontró el arreglo documentado 'bonds'")
    rueda = payload.get("rueda") if isinstance(payload.get("rueda"), dict) else {}
    return [x for x in bonds if isinstance(x, dict)], rueda


def extract_data912(payload: Any) -> list[dict]:
    if not isinstance(payload, list):
        raise RuntimeError("Data912 no devolvió el arreglo JSON esperado")
    return [x for x in payload if isinstance(x, dict)]


def index_by_symbol(records: list[dict], field: str) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for item in records:
        symbol = item.get(field)
        if symbol:
            result.setdefault(str(symbol).strip().upper(), []).append(item)
    return result


def compact_compararfondos(item: dict) -> dict:
    fields = (
        "ticker", "nombre", "tipo", "emisor", "moneda", "tcType", "pagoEn",
        "precio", "precioARS", "precioStale", "operaciones", "tir",
        "tirEsReal", "tirPocasOps", "tirMonedaCruzada", "paridad",
        "duration", "vencimiento", "valorTecnico", "capitalResidual",
    )
    return {key: item.get(key) for key in fields if key in item}


def compact_data912(item: dict) -> dict:
    fields = ("symbol", "c", "v", "q_bid", "px_bid", "px_ask", "q_ask", "q_op", "pct_change")
    return {key: item.get(key) for key in fields if key in item}


def run_source_compararfondos() -> tuple[dict[str, list[dict]], dict]:
    payload = fetch_json(COMPARARFONDOS_URL)
    records, rueda = extract_compararfondos(payload)
    index = index_by_symbol(records, "ticker")
    print(f"[OK] CompararFondos: {len(records)} instrumentos en catálogo")
    print(
        "[RUEDA] "
        f"abierta={rueda.get('abierta')} | habil={rueda.get('habil')} | "
        f"fecha={rueda.get('fechaRueda')} | feriado={rueda.get('feriado')}"
    )
    return index, rueda


def run_source_data912() -> dict[str, list[dict]]:
    payload = fetch_json(DATA912_URL)
    records = extract_data912(payload)
    index = index_by_symbol(records, "symbol")
    print(f"[OK] Data912: {len(records)} símbolos de deuda corporativa recibidos")
    return index


def report(index_cf: dict[str, list[dict]] | None,
           index_d912: dict[str, list[dict]] | None) -> int:
    found_any = 0
    print("\n========== DIAGNÓSTICO DE LAS NUEVE ON ==========")
    for cfg in INSTRUMENTOS:
        candidates = list(dict.fromkeys(cfg["tickers_informados"] + cfg["candidatos_mep"]))
        print(f"\n{cfg['emision']} | ISIN: {cfg['isin'] or 'NO INFORMADO'}")
        print(f"  Emisor esperado: {cfg['emisor_esperado']}")
        for ticker in candidates:
            cf_matches = (index_cf or {}).get(ticker.upper(), [])
            d912_matches = (index_d912 or {}).get(ticker.upper(), [])
            if cf_matches or d912_matches:
                found_any += 1
            label = "ticker informado" if ticker in cfg["tickers_informados"] else "candidato MEP por verificar"
            print(f"  - {ticker} ({label})")
            if len(cf_matches) == 1:
                print("      CompararFondos:", json.dumps(compact_compararfondos(cf_matches[0]), ensure_ascii=False))
            elif len(cf_matches) > 1:
                print(f"      CompararFondos: ALERTA, {len(cf_matches)} coincidencias para el mismo ticker")
            else:
                print("      CompararFondos: no encontrado por ticker exacto")
            if len(d912_matches) == 1:
                print("      Data912:", json.dumps(compact_data912(d912_matches[0]), ensure_ascii=False))
            elif len(d912_matches) > 1:
                print(f"      Data912: ALERTA, {len(d912_matches)} coincidencias para el mismo símbolo")
            else:
                print("      Data912: no encontrado por símbolo exacto")
    print("\n[IMPORTANTE] La presencia de un ticker en una API no confirma por sí sola su moneda,")
    print("            ISIN ni equivalencia económica. Revisar emisor, especie, tcType y pagoEn.")
    print("[IMPORTANTE] Data912 es una fuente secundaria educativa; no se usa para calcular TIR.")
    return found_any


def main() -> int:
    print("FIBRX | MOTOR INDEPENDIENTE DE ON — ETAPA 1 (SOLO CONSULTA)")
    print("Ejecución UTC:", datetime.now(timezone.utc).isoformat())
    cf_index = None
    d912_index = None
    errors = []

    try:
        print("\n[CONSULTA] CompararFondos /api/bonos")
        cf_index, _ = run_source_compararfondos()
    except Exception as exc:
        errors.append(f"CompararFondos: {exc}")
        print(f"[ERROR] CompararFondos: {exc}")

    try:
        print("\n[CONSULTA] Data912 /live/arg_corp")
        d912_index = run_source_data912()
    except Exception as exc:
        errors.append(f"Data912: {exc}")
        print(f"[ERROR] Data912: {exc}")

    if cf_index is None and d912_index is None:
        print("\n[ERROR] Ninguna fuente respondió. No se generaron ni modificaron archivos.", file=sys.stderr)
        return 1

    report(cf_index, d912_index)
    if errors:
        print("\n[AVISO] Una o más fuentes no respondieron:")
        for error in errors:
            print(" -", error)
    print("\n[OK] Diagnóstico finalizado. No se modificó ingesta_tasas.py ni tasas_mercado.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
