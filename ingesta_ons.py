#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FIBRX | Ingesta y validación de Obligaciones Negociables.

Objetivo:
- Consultar fuentes externas al ejecutar el script.
- Actualizar precio/indicadores desde CompararFondos.
- Corroborar cotización con Data912, que su propia documentación califica
  como educativa/no necesariamente real; nunca se usa como fuente primaria.
- Validar datos públicos contra el maestro contractual local.
- No inventar datos ni mapear especies O/C/D automáticamente.
- Generar JSON con trazabilidad, alertas y fecha de consulta.

Fuentes:
  CompararFondos: https://compararfondos.com.ar/api/bonos
  Data912 (corroboración no autoritativa): https://data912.com/live/arg_corp

IMPORTANTE:
CompararFondos es un agregador público, no el prospecto ni BYMA directamente.
La validación contractual final requiere prospecto/resolución oficial del emisor.
El script conserva el maestro contractual recibido; sus datos no quedan
"certificados" por el mero hecho de que una API coincida.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


URL_COMPARARFONDOS = "https://compararfondos.com.ar/api/bonos"
URL_DATA912 = "https://data912.com/live/arg_corp"
TIMEOUT_SEGUNDOS = 20
ARCHIVO_SALIDA = os.environ.get("FIBRX_ON_OUTPUT", "ons_mercado.json")

# No se agregan equivalencias O/C/D. Cada símbolo debe existir en forma exacta.
# Estos cuatro contratos proceden del archivo entregado por el usuario y
# requieren contraste documental oficial antes de ser considerados definitivos.
MAESTRO_CONTRACTUAL: Dict[str, Dict[str, Any]] = {
    "YM34D": {
        "isin": "USP989MJBY67",
        "emisor": "YPF S.A.",
        "tna": 8.25,
        "frecuencia_pago": "Semestral",
        "meses_pago": [1, 7],
        "fecha_vencimiento": "2034-01-17",
        "cronograma_amortizacion": [
            ("2032-01-17", 0.30), ("2033-01-17", 0.30), ("2034-01-17", 0.40)
        ],
        "fuente_contractual": "Maestro heredado del archivo del usuario; pendiente de prospecto oficial",
        "estado_documental": "PENDIENTE_VERIFICACION_OFICIAL",
    },
    "AEC3O": {
        "isin": "AR0382592001",
        "emisor": "AES Argentina",
        "tna": 6.47,
        "frecuencia_pago": "Semestral",
        "meses_pago": [2, 8],
        "fecha_vencimiento": "2028-08-06",
        "cronograma_amortizacion": [("2028-08-06", 1.00)],
        "fuente_contractual": "Maestro heredado del archivo del usuario; pendiente de prospecto oficial",
        "estado_documental": "PENDIENTE_VERIFICACION_OFICIAL",
    },
    "IRCPO": {
        "isin": "USP58809BU07",
        "emisor": "IRSA",
        "tna": 8.00,
        "frecuencia_pago": "Semestral",
        "meses_pago": [3, 9],
        "fecha_vencimiento": "2035-03-25",
        "cronograma_amortizacion": [
            ("2033-03-25", 0.33), ("2034-03-25", 0.33), ("2035-03-25", 0.34)
        ],
        "fuente_contractual": "Maestro heredado del archivo del usuario; pendiente de prospecto oficial",
        "estado_documental": "PENDIENTE_VERIFICACION_OFICIAL",
    },
    "TLCPO": {
        "isin": "USP9028NCA74",
        "emisor": "Telecom Argentina",
        "tna": 9.25,
        "frecuencia_pago": "Semestral",
        "meses_pago": [5, 11],
        "fecha_vencimiento": "2033-05-28",
        "cronograma_amortizacion": [
            ("2032-05-28", 0.50), ("2033-05-28", 0.50)
        ],
        "fuente_contractual": "Maestro heredado del archivo del usuario; pendiente de prospecto oficial",
        "estado_documental": "PENDIENTE_VERIFICACION_OFICIAL",
    },
}


def ahora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def obtener_json(url: str) -> Any:
    req = Request(
        url,
        headers={
            "User-Agent": "FIBRX-ON-Research/1.0 (educational financial data)",
            "Accept": "application/json",
        },
    )
    with urlopen(req, timeout=TIMEOUT_SEGUNDOS) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} consultando {url}")
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def normalizar_numero(valor: Any) -> Optional[float]:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def normalizar_fecha(valor: Any) -> Optional[str]:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor)[:10]).date().isoformat()
    except ValueError:
        return None


def indice_por_ticker(payload: Any, clave_lista: str) -> Dict[str, Dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get(clave_lista, [])
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    resultado = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or item.get("symbol") or "").strip().upper()
        if ticker:
            resultado[ticker] = item
    return resultado


def validar_flujos_api(flujos: Any, vencimiento: Optional[str]) -> Dict[str, Any]:
    """Valida estructura y orden; no presume que un flujo API sea un prospecto."""
    if not isinstance(flujos, list) or not flujos:
        return {"valido": False, "motivo": "API sin flujos futuros utilizables"}

    fechas = []
    montos = []
    for flujo in flujos:
        if not isinstance(flujo, dict):
            return {"valido": False, "motivo": "Formato de flujo inválido"}
        fecha = normalizar_fecha(flujo.get("fecha"))
        monto = normalizar_numero(flujo.get("monto"))
        if fecha is None or monto is None or monto < 0:
            return {"valido": False, "motivo": "Flujo con fecha/monto inválido"}
        fechas.append(fecha)
        montos.append(monto)

    if fechas != sorted(fechas) or len(fechas) != len(set(fechas)):
        return {"valido": False, "motivo": "Flujos desordenados o fechas duplicadas"}
    if vencimiento and fechas[-1] != vencimiento:
        return {
            "valido": False,
            "motivo": f"Último flujo API ({fechas[-1]}) no coincide con vencimiento ({vencimiento})",
        }
    return {
        "valido": True,
        "cantidad": len(fechas),
        "fecha_primer_flujo": fechas[0],
        "fecha_ultimo_flujo": fechas[-1],
        "suma_montos_futuros": round(sum(montos), 8),
    }


def comparar_contrato(ticker: str, api: Dict[str, Any],
                      maestro: Dict[str, Any]) -> Dict[str, Any]:
    diferencias = []

    venc_api = normalizar_fecha(api.get("vencimiento"))
    venc_maestro = normalizar_fecha(maestro.get("fecha_vencimiento"))
    if venc_api and venc_api != venc_maestro:
        diferencias.append(
            f"Vencimiento distinto: API={venc_api}; maestro={venc_maestro}"
        )

    emisor_api = str(api.get("emisor") or "").strip().lower()
    emisor_maestro = str(maestro.get("emisor") or "").strip().lower()
    if emisor_api and emisor_maestro:
        # Comparación tolerante a razón social ampliada, sin aceptar emisor ajeno.
        tokens = [t for t in emisor_maestro.replace(".", "").split() if len(t) > 2]
        if not any(token in emisor_api for token in tokens):
            diferencias.append(
                f"Emisor no coincide: API={api.get('emisor')}; maestro={maestro.get('emisor')}"
            )

    validacion_flujos = validar_flujos_api(api.get("flujos"), venc_api)
    if not validacion_flujos["valido"]:
        diferencias.append(validacion_flujos["motivo"])

    # Comparación básica de cupón/amortización sólo si el proveedor entrega
    # datos estructurados para ello. La API actual no siempre lo hace.
    return {
        "ticker_exacto": ticker,
        "vencimiento_api": venc_api,
        "vencimiento_maestro": venc_maestro,
        "emisor_api": api.get("emisor"),
        "emisor_maestro": maestro.get("emisor"),
        "flujos_api": validacion_flujos,
        "diferencias": diferencias,
        "estado": "CONFLICTO" if diferencias else "COINCIDE_EN_CAMPOS_DISPONIBLES",
        "estado_documental_maestro": maestro.get("estado_documental"),
    }


def evaluar_compararfondos(ticker: str, registro: Optional[Dict[str, Any]],
                           maestro: Dict[str, Any]) -> Dict[str, Any]:
    if not registro:
        return {
            "ticker": ticker,
            "estado": "SIN_COINCIDENCIA_EXACTA_EN_API",
            "fuente": "CompararFondos",
        }

    moneda = str(registro.get("moneda") or "").upper()
    tc_type = str(registro.get("tcType") or "").upper()
    ticker_devuelto = str(registro.get("ticker") or "").upper()

    # Se rechaza cualquier sustitución de especie. También se comprueba
    # que una especie D sea liquidable MEP según el campo publicado.
    liquidacion_esperada = (
        "MEP" if ticker.endswith("D")
        else "CCL" if ticker.endswith("C")
        else "ARS" if ticker.endswith("O")
        else None
    )
    if ticker_devuelto != ticker:
        estado_especie = "RECHAZADO_TICKER_NO_EXACTO"
    elif liquidacion_esperada is None:
        estado_especie = "ESPECIE_NO_RECONOCIDA"
    elif tc_type != liquidacion_esperada:
        estado_especie = "RECHAZADO_TIPO_LIQUIDACION_INCOMPATIBLE"
    else:
        estado_especie = "ESPECIE_Y_LIQUIDACION_COINCIDENTES"

    validacion = comparar_contrato(ticker, registro, maestro)
    precio = normalizar_numero(registro.get("precio"))
    operaciones = normalizar_numero(registro.get("operaciones"))
    stale = bool(registro.get("precioStale", False))
    tir = normalizar_numero(registro.get("tir"))
    duration = normalizar_numero(registro.get("duration"))

    precio_utilizable = (
        estado_especie == "ESPECIE_Y_LIQUIDACION_COINCIDENTES"
        and validacion["estado"] != "CONFLICTO"
        and precio is not None
        and precio > 0
        and not stale
        and operaciones is not None
        and operaciones > 0
    )

    if validacion["estado"] == "CONFLICTO":
        estado = "BLOQUEADO_CONFLICTO_CONTRACTUAL"
    elif estado_especie != "ESPECIE_Y_LIQUIDACION_COINCIDENTES":
        estado = estado_especie
    elif stale or not operaciones:
        estado = "PRECIO_SIN_OPERACIONES_RECIENTES"
    elif precio_utilizable:
        estado = "PRECIO_API_UTILIZABLE_CON_RESERVAS"
    else:
        estado = "DATOS_INSUFICIENTES"

    return {
        "ticker": ticker,
        "estado": estado,
        "fuente": "CompararFondos API",
        "url_fuente": URL_COMPARARFONDOS,
        "nombre": registro.get("nombre"),
        "emisor": registro.get("emisor"),
        "moneda_denominacion_api": moneda,
        "tcType": tc_type,
        "moneda_precio": (
            "USD MEP" if tc_type == "MEP"
            else "USD CCL" if tc_type == "CCL"
            else "ARS" if tc_type == "ARS"
            else "NO DETERMINADA"
        ),
        "ley": registro.get("ley"),
        "precio": precio,
        "precioStale": stale,
        "operaciones": operaciones,
        "volume": normalizar_numero(registro.get("volume")),
        "tir_api_pct": tir,
        "tirEsReal": bool(registro.get("tirEsReal", False)),
        "tirStale": bool(registro.get("tirStale", False)),
        "tirPocasOps": bool(registro.get("tirPocasOps", False)),
        "tirMonedaCruzada": bool(registro.get("tirMonedaCruzada", False)),
        "duration_api": duration,
        "paridad": normalizar_numero(registro.get("paridad")),
        "paridadBase": registro.get("paridadBase"),
        "valorTecnico": normalizar_numero(registro.get("valorTecnico")),
        "capitalResidual": normalizar_numero(registro.get("capitalResidual")),
        "vencimiento_api": normalizar_fecha(registro.get("vencimiento")),
        "pagoEn": registro.get("pagoEn"),
        "nextCoupon": registro.get("nextCoupon"),
        "flujos": registro.get("flujos"),
        "validacion_contractual": validacion,
        "precio_utilizable": precio_utilizable,
        "indicadores_utilizables": (
            precio_utilizable
            and tir is not None
            and not bool(registro.get("tirStale", False))
            and not bool(registro.get("tirPocasOps", False))
            and not bool(registro.get("tirMonedaCruzada", False))
        ),
        "nota": (
            "Indicadores provistos por agregador externo; no equivalen a una "
            "cotización oficial directa ni sustituyen el prospecto."
        ),
    }


def evaluar_data912(ticker: str, registro: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not registro:
        return {"ticker": ticker, "estado": "SIN_COINCIDENCIA", "fuente": "Data912"}

    return {
        "ticker": ticker,
        "estado": "SOLO_CORROBORACION_NO_AUTORITATIVA",
        "fuente": "Data912",
        "url_fuente": URL_DATA912,
        "precio": normalizar_numero(registro.get("c")),
        "operaciones": normalizar_numero(registro.get("q_op")),
        "volumen": normalizar_numero(registro.get("v")),
        "bid": normalizar_numero(registro.get("px_bid")),
        "ask": normalizar_numero(registro.get("px_ask")),
        "nota": (
            "La documentación de Data912 indica que sus datos son educativos "
            "y no necesariamente reales/en tiempo real. No se usa para publicar "
            "precio, TIR ni duration como dato validado."
        ),
    }


def construir_reporte(tickers: Optional[List[str]] = None) -> Dict[str, Any]:
    tickers = [t.upper() for t in (tickers or list(MAESTRO_CONTRACTUAL))]
    desconocidos = [t for t in tickers if t not in MAESTRO_CONTRACTUAL]
    if desconocidos:
        raise KeyError(
            "No hay maestro contractual explícito para: "
            + ", ".join(desconocidos)
            + ". No se aplican alias automáticos."
        )

    reporte = {
        "producto": "FIBRX | Motor de ON",
        "fecha_consulta_utc": ahora_utc(),
        "estado_general": "INICIALIZANDO",
        "fuentes": {},
        "instrumentos": [],
        "advertencias": [
            "La coincidencia con un agregador no certifica el prospecto.",
            "No se convierten ni mezclan especies O, C y D.",
            "No se utilizan datos de cartera privada.",
            "Data912 se conserva sólo como corroboración no autoritativa.",
        ],
    }

    errores = []
    try:
        payload_cf = obtener_json(URL_COMPARARFONDOS)
        indice_cf = indice_por_ticker(payload_cf, "bonds")
        reporte["fuentes"]["CompararFondos"] = {
            "estado": "OK",
            "url": URL_COMPARARFONDOS,
            "registros_recibidos": len(indice_cf),
            "rol": "Fuente primaria de mercado agregada",
            "atribucion_requerida": True,
        }
    except Exception as exc:
        indice_cf = {}
        errores.append(f"CompararFondos: {type(exc).__name__}: {exc}")
        reporte["fuentes"]["CompararFondos"] = {
            "estado": "ERROR", "detalle": str(exc), "url": URL_COMPARARFONDOS
        }

    try:
        payload_d9 = obtener_json(URL_DATA912)
        indice_d9 = indice_por_ticker(payload_d9, "")
        reporte["fuentes"]["Data912"] = {
            "estado": "OK",
            "url": URL_DATA912,
            "registros_recibidos": len(indice_d9),
            "rol": "Corroboración no autoritativa; no habilita publicación",
        }
    except Exception as exc:
        indice_d9 = {}
        errores.append(f"Data912: {type(exc).__name__}: {exc}")
        reporte["fuentes"]["Data912"] = {
            "estado": "ERROR", "detalle": str(exc), "url": URL_DATA912
        }

    for ticker in tickers:
        maestro = MAESTRO_CONTRACTUAL[ticker]
        cf = evaluar_compararfondos(ticker, indice_cf.get(ticker), maestro)
        d9 = evaluar_data912(ticker, indice_d9.get(ticker))
        diferencias_precio = None

        if cf.get("precio") is not None and d9.get("precio") is not None:
            diferencias_precio = round(
                abs(cf["precio"] - d9["precio"]), 6
            )

        instrumento = {
            "ticker": ticker,
            "isin_maestro": maestro.get("isin"),
            "maestro_contractual": maestro,
            "mercado": cf,
            "corroboracion": d9,
            "diferencia_absoluta_precio_vs_data912": diferencias_precio,
        }
        reporte["instrumentos"].append(instrumento)

    if errores and not indice_cf:
        reporte["estado_general"] = "SIN_FUENTE_PRIMARIA"
    elif any(i["mercado"].get("estado", "").startswith("BLOQUEADO")
             for i in reporte["instrumentos"]):
        reporte["estado_general"] = "REQUIERE_AUDITORIA_CONTRACTUAL"
    elif any(i["mercado"].get("precio_utilizable")
             for i in reporte["instrumentos"]):
        reporte["estado_general"] = "CONSULTA_COMPLETADA_CON_VALIDACIONES"
    else:
        reporte["estado_general"] = "CONSULTA_COMPLETADA_SIN_PRECIOS_VALIDOS"

    reporte["errores"] = errores
    return reporte


def guardar_reporte(reporte: Dict[str, Any], ruta: str = ARCHIVO_SALIDA) -> None:
    with open(ruta, "w", encoding="utf-8") as archivo:
        json.dump(reporte, archivo, ensure_ascii=False, indent=2)


def imprimir_resumen(reporte: Dict[str, Any]) -> None:
    print("=" * 76)
    print("FIBRX | ACTUALIZACIÓN DE OBLIGACIONES NEGOCIABLES")
    print("Consulta UTC:", reporte["fecha_consulta_utc"])
    print("Estado:", reporte["estado_general"])
    print("=" * 76)
    for item in reporte["instrumentos"]:
        m = item["mercado"]
        print(
            f'{item["ticker"]:8} | {m.get("estado","N/D"):42} | '
            f'Precio={m.get("precio")} | TIR={m.get("tir_api_pct")} | '
            f'Ops={m.get("operaciones")}'
        )
        validacion = m.get("validacion_contractual", {})
        for diferencia in validacion.get("diferencias", []):
            print("  ALERTA:", diferencia)
        if m.get("precioStale"):
            print("  ALERTA: precio marcado como desactualizado por la API.")
    print("=" * 76)


def main() -> int:
    try:
        reporte = construir_reporte()
        guardar_reporte(reporte)
        imprimir_resumen(reporte)
        print(f"Archivo generado: {os.path.abspath(ARCHIVO_SALIDA)}")
        return 0
    except Exception as exc:
        print(f"ERROR FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
