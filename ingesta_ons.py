#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FIBRX | Diagnóstico independiente de Obligaciones Negociables (ON)

Etapa de descubrimiento y cobertura:
- Consulta CompararFondos y Data912.
- Busca por ticker exacto, ISIN, emisor y año de vencimiento.
- Distingue coincidencias exactas de candidatos por texto.
- No convierte automáticamente especies O/C a D.
- No calcula TIR, duration ni flujos propios.
- No modifica ingesta_tasas.py ni escribe tasas_mercado.json.

Uso:
    python ingesta_ons.py
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TIMEOUT = 35
URL_COMPARARFONDOS = "https://compararfondos.com.ar/api/bonos"
URL_DATA912 = "https://data912.com/live/arg_corp"

# Los tickers D de las seis emisiones adicionales son hipótesis de búsqueda.
# Su presencia en una API no confirma por sí sola ISIN, moneda ni equivalencia.
INSTRUMENTOS = [
    {
        "ticker_referencia": "CP36D",
        "tickers_busqueda": ["CP36D"],
        "isin": "AR0015464982",
        "emisor": "Compania General de Combustibles",
        "alias_emisor": ["CGC", "Compania General de Combustibles", "Compania General de Combustible"],
        "anio_vencimiento": 2027,
        "descripcion": "CGC Clase 36",
        "especie_d_confirmada_por_usuario": True,
    },
    {
        "ticker_referencia": "YM34D",
        "tickers_busqueda": ["YM34D"],
        "isin": "USP989MJBY67",
        "emisor": "YPF",
        "alias_emisor": ["YPF", "YPF Sociedad Anonima"],
        "anio_vencimiento": 2034,
        "descripcion": "YPF Clase XXXIV",
        "especie_d_confirmada_por_usuario": True,
    },
    {
        "ticker_referencia": "VSCVD",
        "tickers_busqueda": ["VSCVD"],
        "isin": "USP9659RAB44",
        "emisor": "Vista Energy Argentina",
        "alias_emisor": ["Vista Energy", "Vista Energy Argentina", "Vista Argentina"],
        "anio_vencimiento": 2033,
        "descripcion": "Vista Clase XXIX",
        "especie_d_confirmada_por_usuario": True,
    },
    {
        "ticker_referencia": "AEC3O",
        "tickers_busqueda": ["AEC3O", "AEC3D"],
        "isin": None,
        "emisor": "AES Argentina Generacion",
        "alias_emisor": ["AES Argentina", "AES Argentina Generacion", "AES Generacion"],
        "anio_vencimiento": 2028,
        "descripcion": "AES Argentina Generacion Clase 3",
        "especie_d_confirmada_por_usuario": False,
    },
    {
        "ticker_referencia": "IRCPO",
        "tickers_busqueda": ["IRCPO", "IRCPD"],
        "isin": None,
        "emisor": "IRSA Inversiones",
        "alias_emisor": ["IRSA", "IRSA Inversiones", "IRSA Propiedades Comerciales"],
        "anio_vencimiento": 2035,
        "descripcion": "IRSA 8% 2035",
        "especie_d_confirmada_por_usuario": False,
    },
    {
        "ticker_referencia": "MGCRO",
        "tickers_busqueda": ["MGCRO", "MGCRD"],
        "isin": None,
        "emisor": "Pampa Energia",
        "alias_emisor": ["Pampa", "Pampa Energia", "Pampa Energia S.A."],
        "anio_vencimiento": 2037,
        "descripcion": "Pampa Energia 7,75% 2037",
        "especie_d_confirmada_por_usuario": False,
    },
    {
        "ticker_referencia": "PQCSO",
        "tickers_busqueda": ["PQCSO", "PQCSD"],
        "isin": None,
        "emisor": "Petroquimica Comodoro Rivadavia",
        "alias_emisor": ["PCR", "Petroquimica", "Petroquimica Comodoro Rivadavia"],
        "anio_vencimiento": 2031,
        "descripcion": "Petroquimica Clase S",
        "especie_d_confirmada_por_usuario": False,
    },
    {
        "ticker_referencia": "TLCPO",
        "tickers_busqueda": ["TLCPO", "TLCPD"],
        "isin": None,
        "emisor": "Telecom Argentina",
        "alias_emisor": ["Telecom", "Telecom Argentina"],
        "anio_vencimiento": 2033,
        "descripcion": "Telecom Argentina 9,25% 2033",
        "especie_d_confirmada_por_usuario": False,
    },
    {
        "ticker_referencia": "VSCXO",
        "tickers_busqueda": ["VSCXO", "VSCXD"],
        "isin": None,
        "emisor": "Vista Energy Argentina",
        "alias_emisor": ["Vista Energy", "Vista Energy Argentina", "Vista Argentina"],
        "anio_vencimiento": 2038,
        "descripcion": "Vista Energy 7,875% 2038",
        "especie_d_confirmada_por_usuario": False,
    },
]

CAMPOS_TEXTO = (
    "ticker", "symbol", "nombre", "name", "emisor", "issuer",
    "descripcion", "description", "isin", "especie", "moneda",
    "currency", "vencimiento", "maturity",
)

CAMPOS_SALIDA_COMPARAR = (
    "ticker", "nombre", "tipo", "emisor", "isin", "moneda", "tcType",
    "pagoEn", "precio", "precioARS", "precioStale", "operaciones",
    "tir", "tirEsReal", "tirPocasOps", "tirMonedaCruzada", "paridad",
    "duration", "vencimiento", "valorTecnico", "capitalResidual",
)

CAMPOS_SALIDA_DATA912 = (
    "symbol", "c", "v", "q_bid", "px_bid", "px_ask", "q_ask",
    "q_op", "pct_change",
)


def normalizar(valor) -> str:
    """Normaliza texto para comparar nombres sin depender de tildes o mayúsculas."""
    if valor is None:
        return ""
    texto = unicodedata.normalize("NFKD", str(valor))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.upper()
    return re.sub(r"[^A-Z0-9]", "", texto)


def obtener_anio_vencimiento(registro: dict):
    for campo in ("vencimiento", "maturity", "fechaVencimiento", "fecha_vencimiento"):
        valor = registro.get(campo)
        if valor is None:
            continue
        coincidencia = re.search(r"(?:19|20)\d{2}", str(valor))
        if coincidencia:
            return int(coincidencia.group(0))
    return None


def fetch_json(url: str):
    request = Request(
        url,
        headers={
            "User-Agent": "FIBRX-ON-Diagnostic/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            contenido = response.read().decode("utf-8")
            return json.loads(contenido), None
    except HTTPError as exc:
        return None, f"HTTP {exc.code}: {exc.reason}"
    except URLError as exc:
        return None, f"Error de conexión: {exc.reason}"
    except TimeoutError:
        return None, f"Tiempo de espera agotado ({TIMEOUT}s)"
    except json.JSONDecodeError as exc:
        return None, f"Respuesta no JSON: {exc}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def extraer_bonos_compararfondos(payload):
    if isinstance(payload, dict):
        bonos = payload.get("bonds")
        rueda = payload.get("rueda")
        if isinstance(bonos, list):
            return bonos, rueda
        # Compatibilidad defensiva ante cambios de nombre en el endpoint.
        for clave in ("bonos", "data", "resultados", "results"):
            if isinstance(payload.get(clave), list):
                return payload[clave], rueda
    if isinstance(payload, list):
        return payload, None
    return [], None


def extraer_registros_data912(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for clave in ("data", "results", "bonos", "bonds"):
            if isinstance(payload.get(clave), list):
                return payload[clave]
    return []


def compactar(registro: dict, campos) -> dict:
    return {campo: registro.get(campo) for campo in campos if campo in registro}


def texto_registro(registro: dict) -> str:
    partes = []
    for campo in CAMPOS_TEXTO:
        valor = registro.get(campo)
        if valor is not None:
            partes.append(str(valor))
    return normalizar(" ".join(partes))


def buscar_coincidencias(instrumento: dict, catalogo: list[dict]):
    """
    Devuelve coincidencias ordenadas por fuerza:
    1) ticker exacto;
    2) ISIN exacto;
    3) emisor + año de vencimiento;
    4) emisor sin año (candidato débil).
    """
    tickers = {normalizar(x) for x in instrumento["tickers_busqueda"]}
    isin_objetivo = normalizar(instrumento.get("isin"))
    alias = [normalizar(x) for x in instrumento["alias_emisor"] if normalizar(x)]
    anio_objetivo = instrumento.get("anio_vencimiento")

    resultados = []
    vistos = set()

    for registro in catalogo:
        if not isinstance(registro, dict):
            continue

        ticker = normalizar(registro.get("ticker") or registro.get("symbol"))
        isin_registro = normalizar(registro.get("isin") or registro.get("ISIN"))
        anio_registro = obtener_anio_vencimiento(registro)
        texto = texto_registro(registro)

        razones = []
        nivel = 0

        if ticker and ticker in tickers:
            razones.append("TICKER_EXACTO")
            nivel = max(nivel, 100)

        if isin_objetivo and isin_registro and isin_objetivo == isin_registro:
            razones.append("ISIN_EXACTO")
            nivel = max(nivel, 95)

        emisor_coincide = any(a in texto for a in alias)
        if emisor_coincide and anio_objetivo and anio_registro == anio_objetivo:
            razones.append("EMISOR_Y_ANIO_DE_VENCIMIENTO")
            nivel = max(nivel, 80)
        elif emisor_coincide:
            razones.append("EMISOR_COINCIDENTE_SIN_VALIDAR_VENCIMIENTO")
            nivel = max(nivel, 50)

        if not razones:
            continue

        clave = (
            str(registro.get("ticker") or registro.get("symbol") or ""),
            str(registro.get("isin") or registro.get("ISIN") or ""),
            str(registro.get("nombre") or registro.get("name") or ""),
        )
        if clave in vistos:
            continue
        vistos.add(clave)

        resultados.append({
            "nivel": nivel,
            "razones": razones,
            "registro": registro,
        })

    resultados.sort(key=lambda x: x["nivel"], reverse=True)
    return resultados


def indice_por_ticker(registros, campo):
    indice = {}
    for registro in registros:
        if isinstance(registro, dict) and registro.get(campo):
            indice[normalizar(registro[campo])] = registro
    return indice


def ejecutar_compararfondos():
    print("\n" + "=" * 78)
    print("FUENTE 1 | COMPARARFONDOS")
    print("=" * 78)

    payload, error = fetch_json(URL_COMPARARFONDOS)
    if error:
        print(f"No se pudo consultar CompararFondos: {error}")
        return None

    catalogo, rueda = extraer_bonos_compararfondos(payload)
    if not catalogo:
        print("La API respondió, pero no se identificó una lista de bonos.")
        if isinstance(payload, dict):
            print("Claves recibidas:", ", ".join(map(str, payload.keys())))
        return None

    print(f"Registros recibidos: {len(catalogo)}")
    if rueda is not None:
        print("Metadatos de rueda:", json.dumps(rueda, ensure_ascii=False, default=str))

    for instrumento in INSTRUMENTOS:
        coincidencias = buscar_coincidencias(instrumento, catalogo)
        print(f"\n{instrumento['ticker_referencia']} | {instrumento['descripcion']}")
        if not coincidencias:
            print("  Sin coincidencias por ticker, ISIN, emisor o año de vencimiento.")
            continue

        for item in coincidencias[:8]:
            registro = item["registro"]
            if item["nivel"] >= 95:
                clasificacion = "COINCIDENCIA FUERTE (ticker/ISIN)"
            elif item["nivel"] >= 80:
                clasificacion = "CANDIDATO (emisor + año)"
            else:
                clasificacion = "CANDIDATO DÉBIL (solo emisor)"

            print(f"  [{clasificacion}]")
            print("  Motivo:", ", ".join(item["razones"]))
            print("  Datos:", json.dumps(
                compactar(registro, CAMPOS_SALIDA_COMPARAR),
                ensure_ascii=False,
                default=str,
            ))

    return catalogo


def ejecutar_data912():
    print("\n" + "=" * 78)
    print("FUENTE 2 | DATA912 (REFERENCIA SECUNDARIA)")
    print("=" * 78)

    payload, error = fetch_json(URL_DATA912)
    if error:
        print(f"No se pudo consultar Data912: {error}")
        return None

    registros = extraer_registros_data912(payload)
    if not registros:
        print("La API respondió, pero no se identificó una lista de instrumentos.")
        return None

    print(f"Registros recibidos: {len(registros)}")
    indice = indice_por_ticker(registros, "symbol")

    for instrumento in INSTRUMENTOS:
        print(f"\n{instrumento['ticker_referencia']} | {instrumento['descripcion']}")
        encontrados = []
        for ticker in instrumento["tickers_busqueda"]:
            registro = indice.get(normalizar(ticker))
            if registro and registro not in encontrados:
                encontrados.append(registro)

        if not encontrados:
            print("  Sin coincidencia por símbolo exacto.")
            continue

        for registro in encontrados:
            print("  Símbolo encontrado:", json.dumps(
                compactar(registro, CAMPOS_SALIDA_DATA912),
                ensure_ascii=False,
                default=str,
            ))

    print(
        "\nAdvertencia: Data912 se utiliza como referencia secundaria de cotización/"
        "puntas/volumen. No se usa para calcular TIR ni para confirmar ISIN, moneda "
        "o equivalencia económica."
    )
    return registros


def imprimir_resumen():
    print("\n" + "=" * 78)
    print("CRITERIOS DE INTERPRETACIÓN")
    print("=" * 78)
    print("- Ticker o ISIN exacto: coincidencia fuerte, pendiente de validar especie y moneda.")
    print("- Emisor + año: candidato de identificación; no confirma que sea la misma emisión.")
    print("- Emisor sin año: candidato débil; requiere revisión manual.")
    print("- Los tickers terminados en O no se transforman automáticamente en D.")
    print("- precioStale indica que el precio puede ser de una rueda anterior; no se presenta")
    print("  como cotización actual.")
    print("- TIR, duration y paridad se muestran solo si la fuente las informa.")
    print("- Este diagnóstico no modifica archivos ni genera tasas_mercado.json.")


def main():
    print("FIBRX | MOTOR INDEPENDIENTE DE DIAGNÓSTICO DE ON")
    print("Objetivo: ampliar cobertura de identificación sin alterar el motor de FCI.")

    resultado_cf = ejecutar_compararfondos()
    resultado_d912 = ejecutar_data912()
    imprimir_resumen()

    if resultado_cf is None and resultado_d912 is None:
        print("\nRESULTADO: no fue posible obtener datos de ninguna de las dos fuentes.")
        return 1

    print("\nRESULTADO: diagnóstico ejecutado. Revisar las coincidencias y candidatos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
