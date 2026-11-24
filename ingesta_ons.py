#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FIBRX | Ingesta y validación de Obligaciones Negociables.

Objetivo:
- Consultar fuentes externas al ejecutar el script.
- Actualizar precio/indicadores desde CompararFondos.
- Registrar Data912 como fuente relacionada dentro de la misma cadena de datos;
  no se considera corroboración independiente de CompararFondos.
- Validar datos públicos contra el maestro contractual local.
- No inventar datos ni mapear especies O/C/D automáticamente.
- Generar JSON con trazabilidad, alertas y fecha de consulta.

Fuentes:
  CompararFondos: https://compararfondos.com.ar/api/bonos
  Data912 (fuente relacionada / upstream según documentación consultada):
    https://data912.com/live/arg_corp

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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


URL_COMPARARFONDOS = "https://compararfondos.com.ar/api/bonos"
URL_DATA912 = "https://data912.com/live/arg_corp"
VERSION_MOTOR = "2.0.0"
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
    except (TypeError, ValueError):
        return None


def indice_por_ticker(payload: Any, clave_lista: str) -> Dict[str, Dict[str, Any]]:
    if isinstance(payload, dict):
        if clave_lista not in payload or not isinstance(payload[clave_lista], list):
            raise ValueError(f"Respuesta API sin lista válida '{clave_lista}'")
        items = payload[clave_lista]
    elif isinstance(payload, list):
        items = payload
    else:
        raise ValueError("Formato de respuesta API no reconocido")

    resultado: Dict[str, Dict[str, Any]] = {}
    duplicados = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or item.get("symbol") or "").strip().upper()
        if ticker:
            if ticker in resultado:
                duplicados.add(ticker)
            resultado[ticker] = item
    if duplicados:
        raise ValueError("Tickers duplicados en respuesta API: " + ", ".join(sorted(duplicados)))
    return resultado



def analizar_fecha_rueda(rueda: Any, fecha_consulta: Optional[str] = None) -> Dict[str, Any]:
    """Describe antigüedad de rueda sin imponer un umbral arbitrario de bloqueo."""
    if not isinstance(rueda, dict):
        return {"estado": "RUEDA_NO_INFORMADA", "fecha_rueda": None,
                "dias_desde_rueda": None, "abierta": None,
                "liquidacion": None}
    fecha_rueda = normalizar_fecha(rueda.get("fechaRueda"))
    abierta = rueda.get("abierta") if isinstance(rueda.get("abierta"), bool) else None
    liquidacion = normalizar_fecha(rueda.get("liquidacion"))
    if not fecha_rueda:
        estado = "FECHA_RUEDA_NO_INFORMADA"
        dias = None
    else:
        try:
            fecha_base = datetime.fromisoformat((fecha_consulta or ahora_utc()).replace("Z", "+00:00")).date()
            fecha_dato = datetime.fromisoformat(fecha_rueda).date()
            dias = (fecha_base - fecha_dato).days
            estado = "FECHA_RUEDA_FUTURA" if dias < 0 else "FECHA_RUEDA_INFORMADA"
        except ValueError:
            dias = None
            estado = "FECHA_RUEDA_INVALIDA"
    return {"estado": estado, "fecha_rueda": fecha_rueda,
            "dias_desde_rueda": dias, "abierta": abierta,
            "liquidacion": liquidacion}


def validar_flujos_api(flujos: Any, vencimiento: Optional[str]) -> Dict[str, Any]:
    """Valida estructura de flujos; ausencia no equivale a conflicto contractual."""
    if flujos is None:
        return {"estado": "NO_INFORMADO", "valido": None,
                "motivo": "La API no informó flujos"}
    if not isinstance(flujos, list):
        return {"estado": "FORMATO_INVALIDO", "valido": False,
                "motivo": "El campo flujos no es una lista"}
    if not flujos:
        return {"estado": "VACIO", "valido": None,
                "motivo": "La API informó una lista de flujos vacía"}

    fechas, montos = [], []
    for flujo in flujos:
        if not isinstance(flujo, dict):
            return {"estado": "FORMATO_INVALIDO", "valido": False,
                    "motivo": "Elemento de flujo no es un objeto"}
        fecha = normalizar_fecha(flujo.get("fecha"))
        monto = normalizar_numero(flujo.get("monto"))
        if fecha is None or monto is None or monto < 0:
            return {"estado": "FORMATO_INVALIDO", "valido": False,
                    "motivo": "Flujo con fecha o monto inválido"}
        fechas.append(fecha)
        montos.append(monto)

    if fechas != sorted(fechas) or len(fechas) != len(set(fechas)):
        return {"estado": "INCONSISTENTE", "valido": False,
                "motivo": "Flujos desordenados o fechas duplicadas"}
    if vencimiento and fechas[-1] != vencimiento:
        return {
            "estado": "DIFIERE_VENCIMIENTO", "valido": False,
            "motivo": f"Último flujo API ({fechas[-1]}) no coincide con vencimiento ({vencimiento})",
        }
    return {
        "estado": "ESTRUCTURA_VALIDA_NO_CONTRACTUAL",
        "valido": True,
        "cantidad": len(fechas),
        "fecha_primer_flujo": fechas[0],
        "fecha_ultimo_flujo": fechas[-1],
        "suma_montos_futuros": round(sum(montos), 8),
        "nota": "La estructura es consistente; no acredita cupón ni amortización contra prospecto.",
    }


def es_booleano_explicito(registro: Dict[str, Any], campo: str) -> Optional[bool]:
    """No convierte ausencias ni textos ambiguos en False."""
    valor = registro.get(campo)
    return valor if isinstance(valor, bool) else None


def comparar_contrato(ticker: str, api: Dict[str, Any],
                      maestro: Dict[str, Any]) -> Dict[str, Any]:
    diferencias, pendientes = [], []

    venc_api = normalizar_fecha(api.get("vencimiento"))
    venc_maestro = normalizar_fecha(maestro.get("fecha_vencimiento"))
    if venc_api and venc_maestro and venc_api != venc_maestro:
        diferencias.append(f"Vencimiento distinto: API={venc_api}; maestro={venc_maestro}")
    elif not venc_api:
        pendientes.append("La API no informó vencimiento")

    emisor_api = str(api.get("emisor") or "").strip().lower()
    emisor_maestro = str(maestro.get("emisor") or "").strip().lower()
    if emisor_api and emisor_maestro:
        tokens = [t for t in emisor_maestro.replace(".", "").split() if len(t) > 2]
        if tokens and not any(token in emisor_api for token in tokens):
            diferencias.append(
                f"Emisor no coincide: API={api.get('emisor')}; maestro={maestro.get('emisor')}"
            )
    else:
        pendientes.append("Emisor no informado por la API; no se puede contrastar")

    validacion_flujos = validar_flujos_api(api.get("flujos"), venc_api)
    if validacion_flujos["estado"] == "DIFIERE_VENCIMIENTO":
        diferencias.append(validacion_flujos["motivo"])
    elif validacion_flujos["valido"] is False:
        pendientes.append(validacion_flujos["motivo"])
    elif validacion_flujos["valido"] is None:
        pendientes.append(validacion_flujos["motivo"])

    # CompararFondos no expone en todos los registros cupón/amortización
    # estructurados. Su ausencia nunca se interpreta como coincidencia.
    pendientes.append("Cupón y cronograma de amortización requieren prospecto/resolución oficial")

    if diferencias:
        estado = "CONFLICTO_CONFIRMADO"
    elif pendientes:
        estado = "INCOMPLETO_PENDIENTE_DOCUMENTACION"
    else:
        estado = "COINCIDENCIA_EN_CAMPOS_DISPONIBLES"

    return {
        "ticker_exacto": ticker,
        "vencimiento_api": venc_api,
        "vencimiento_maestro": venc_maestro,
        "emisor_api": api.get("emisor"),
        "emisor_maestro": maestro.get("emisor"),
        "flujos_api": validacion_flujos,
        "diferencias_confirmadas": diferencias,
        "pendientes": pendientes,
        "diferencias": diferencias + pendientes,
        "estado": estado,
        "estado_documental_maestro": maestro.get("estado_documental"),
    }


def evaluar_compararfondos(ticker: str, registro: Optional[Dict[str, Any]],
                           maestro: Dict[str, Any]) -> Dict[str, Any]:
    if not registro:
        return {
            "ticker": ticker,
            "estado": "SIN_COINCIDENCIA_EXACTA_EN_API",
            "fuente": "CompararFondos",
            "precio_utilizable": False,
            "indicadores_utilizables": False,
        }

    moneda = str(registro.get("moneda") or "").upper() or None
    tc_type = str(registro.get("tcType") or "").upper() or None
    ticker_devuelto = str(registro.get("ticker") or "").strip().upper()
    tipo = str(registro.get("tipo") or "").upper()

    liquidacion_esperada = (
        "MEP" if ticker.endswith("D")
        else "CCL" if ticker.endswith("C")
        else "ARS" if ticker.endswith("O")
        else None
    )
    if ticker_devuelto != ticker:
        estado_especie = "RECHAZADO_TICKER_NO_EXACTO"
    elif tipo != "ON":
        estado_especie = "TIPO_NO_ON_O_NO_INFORMADO"
    elif liquidacion_esperada is None:
        estado_especie = "ESPECIE_NO_RECONOCIDA"
    elif tc_type != liquidacion_esperada:
        estado_especie = "RECHAZADO_TIPO_LIQUIDACION_INCOMPATIBLE"
    else:
        estado_especie = "ESPECIE_Y_LIQUIDACION_COINCIDENTES"

    validacion = comparar_contrato(ticker, registro, maestro)
    stale = es_booleano_explicito(registro, "precioStale")
    operaciones = normalizar_numero(registro.get("operaciones"))
    precio_api = normalizar_numero(registro.get("precio"))
    precio_ars = normalizar_numero(registro.get("precioARS"))

    # En especie O, CompararFondos documenta que "precio" es el equivalente
    # USD MEP; el precio expresado en pesos es "precioARS".
    if tc_type == "ARS":
        precio_especie = precio_ars
        moneda_precio_especie = "ARS" if precio_ars is not None else None
    else:
        precio_especie = precio_api
        moneda_precio_especie = (
            "USD MEP" if tc_type == "MEP"
            else "USD CCL" if tc_type == "CCL"
            else None
        )

    if estado_especie != "ESPECIE_Y_LIQUIDACION_COINCIDENTES":
        estado_precio = estado_especie
    elif validacion["estado"] == "CONFLICTO_CONFIRMADO":
        estado_precio = "BLOQUEADO_CONFLICTO_CONTRACTUAL"
    elif stale is None:
        estado_precio = "ESTADO_PRECIO_DESCONOCIDO"
    elif stale:
        estado_precio = "PRECIO_STALE_SIN_OPERACIONES_EN_ULTIMA_RUEDA"
    elif operaciones is None:
        estado_precio = "OPERACIONES_NO_INFORMADAS"
    elif operaciones <= 0:
        estado_precio = "SIN_OPERACIONES_EN_ULTIMA_RUEDA"
    elif precio_especie is None or precio_especie <= 0:
        estado_precio = "PRECIO_ESPECIE_NO_DISPONIBLE"
    else:
        estado_precio = "PRECIO_API_UTILIZABLE_CON_RESERVAS"

    precio_utilizable = estado_precio == "PRECIO_API_UTILIZABLE_CON_RESERVAS"

    tir = normalizar_numero(registro.get("tir"))
    duration = normalizar_numero(registro.get("duration"))
    tir_stale = es_booleano_explicito(registro, "tirStale")
    tir_pocas_ops = es_booleano_explicito(registro, "tirPocasOps")
    tir_moneda_cruzada = es_booleano_explicito(registro, "tirMonedaCruzada")
    tir_es_real = es_booleano_explicito(registro, "tirEsReal")
    pago_en = str(registro.get("pagoEn") or "").lower()

    razones_tir = []
    if not precio_utilizable:
        razones_tir.append("Precio de especie no habilitado")
    if tir is None:
        razones_tir.append("TIR no informada por la API")
    if duration is None or duration < 0:
        razones_tir.append("Duration no informada o inválida")
    if tir_stale is not False:
        razones_tir.append("Estado de antigüedad de TIR no confirmado como vigente")
    if tir_pocas_ops is not False:
        razones_tir.append("La API no confirma que la TIR tenga operaciones suficientes")
    if tir_moneda_cruzada is not False:
        razones_tir.append("La API no descarta TIR con moneda cruzada")
    if tir_es_real is True:
        razones_tir.append("La API marca la TIR como real; no debe presentarse como TIR nominal")
    if pago_en == "cable" and tc_type == "MEP":
        razones_tir.append("El bono paga en cable y cotiza en MEP")

    indicadores_utilizables = not razones_tir

    return {
        "ticker": ticker,
        "estado": estado_precio,
        "fuente": "CompararFondos API",
        "url_fuente": URL_COMPARARFONDOS,
        "tipo_api": tipo or None,
        "nombre": registro.get("nombre"),
        "emisor": registro.get("emisor"),
        "moneda_denominacion_api": moneda,
        "tcType": tc_type,
        "moneda_precio_especie": moneda_precio_especie,
        "precio_especie": precio_especie,
        # En especie O, precioARS_api es la cotización nominal de la especie
        # en pesos; precio_api_equivalente es el equivalente USD MEP usado
        # por CompararFondos para sus cálculos de TIR/paridad.
        "precio_api_equivalente": precio_api,
        "precio_base_calculo_tir_api": precio_api,
        "precioARS_api": precio_ars,
        "precio_especie_moneda": moneda_precio_especie,
        "ley": registro.get("ley"),
        "precio": precio_especie,  # Alias de compatibilidad: precio de la especie.
        "precioStale": stale,
        "operaciones": operaciones,
        "volume": normalizar_numero(registro.get("volume")),
        "tir_api_pct": tir,
        "tirEsReal": tir_es_real,
        "tirStale": tir_stale,
        "tirPocasOps": tir_pocas_ops,
        "tirMonedaCruzada": tir_moneda_cruzada,
        "duration_api": duration,
        "paridad": normalizar_numero(registro.get("paridad")),
        "paridadBase": registro.get("paridadBase"),
        "valorTecnico": normalizar_numero(registro.get("valorTecnico")),
        "capitalResidual": normalizar_numero(registro.get("capitalResidual")),
        "vencimiento_api": normalizar_fecha(registro.get("vencimiento")),
        "pagoEn": registro.get("pagoEn"),
        "nextCoupon": registro.get("nextCoupon"),
        "flujos": registro.get("flujos"),
        "diagnostico_campos_api": {
            "campos_criticos_presentes": sorted(
                k for k in ("ticker", "tipo", "moneda", "tcType", "precio",
                            "precioARS", "tir", "duration", "operaciones",
                            "precioStale", "tirStale", "tirPocasOps",
                            "tirMonedaCruzada", "tirEsReal", "pagoEn",
                            "vencimiento", "flujos") if k in registro
            ),
            "campos_criticos_ausentes": [
                k for k in ("ticker", "tipo", "moneda", "tcType", "precio",
                            "tir", "duration", "operaciones", "precioStale",
                            "tirStale", "tirPocasOps", "tirMonedaCruzada",
                            "tirEsReal", "vencimiento", "flujos") if k not in registro
            ],
            "valores_raw_flags": {
                k: registro.get(k) for k in ("precioStale", "tirStale",
                    "tirPocasOps", "tirMonedaCruzada", "tirEsReal", "pagoEn")
                if k in registro
            },
        },
        "validacion_contractual": validacion,
        "precio_utilizable": precio_utilizable,
        "indicadores_utilizables": indicadores_utilizables,
        "motivos_indicadores_no_utilizables": razones_tir,
        "nota": (
            "Precio e indicadores son datos del agregador. La coincidencia de "
            "mercado no certifica cupón, amortización ni prospecto."
        ),
    }


def evaluar_data912(ticker: str, registro: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not registro:
        return {"ticker": ticker, "estado": "SIN_COINCIDENCIA", "fuente": "Data912"}

    return {
        "ticker": ticker,
        "estado": "FUENTE_RELACIONADA_NO_INDEPENDIENTE",
        "fuente": "Data912",
        "url_fuente": URL_DATA912,
        "precio": normalizar_numero(registro.get("c")),
        "operaciones": normalizar_numero(registro.get("q_op")),
        "volumen": normalizar_numero(registro.get("v")),
        "bid": normalizar_numero(registro.get("px_bid")),
        "ask": normalizar_numero(registro.get("px_ask")),
        "nota": (
            "Data912 forma parte de la cadena de suministro de cotizaciones "
            "documentada por CompararFondos. Se conserva para diagnóstico de "
            "consistencia, pero no cuenta como fuente independiente ni valida "
            "por sí sola precio, TIR o duration."
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
        "schema_version": 2,
        "producto": "FIBRX | Motor de ON",
        "version_motor": VERSION_MOTOR,
        "fecha_consulta_utc": ahora_utc(),
        "estado_general": "INICIALIZANDO",
        "fuentes": {},
        "linaje_datos": {
            "CompararFondos": "Agregador de mercado; atribución requerida.",
            "Data912": "Fuente relacionada/upstream según documentación pública de CompararFondos; no independiente.",
            "independencia_entre_fuentes": False,
            "nota": "La coincidencia entre ambas respuestas no equivale a corroboración independiente.",
        },
        "instrumentos": [],
        "advertencias": [
            "La coincidencia con un agregador no certifica el prospecto.",
            "No se convierten ni mezclan especies O, C y D.",
            "No se utilizan datos de cartera privada.",
            "Data912 no se considera una fuente independiente de CompararFondos.",
            "En especie O se distinguen precioARS (precio de especie) y precio (equivalente USD MEP usado por el agregador para TIR/paridad).",
            "La antigüedad de rueda se informa; no se aplica un umbral temporal arbitrario de bloqueo.",
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
            "rueda": payload_cf.get("rueda") if isinstance(payload_cf, dict) else None,
            "analisis_rueda": analizar_fecha_rueda(
                payload_cf.get("rueda") if isinstance(payload_cf, dict) else None,
                reporte["fecha_consulta_utc"]
            ),
            "age_min": payload_cf.get("age_min") if isinstance(payload_cf, dict) else None,
            "rol": "Fuente primaria de mercado agregada (no fuente contractual)",
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
            "rol": "Fuente relacionada dentro de la misma cadena; no independiente",
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

        # Data912 publica "c" como precio de la especie. Para especie O,
        # CompararFondos exige comparar contra precioARS, no contra su
        # equivalente USD MEP contenido en "precio".
        precio_cf_comparable = (
            cf.get("precioARS_api") if ticker.endswith("O")
            else cf.get("precio_api_equivalente")
        )
        if precio_cf_comparable is not None and d9.get("precio") is not None:
            diferencias_precio = round(
                abs(precio_cf_comparable - d9["precio"]), 6
            )

        instrumento = {
            "ticker": ticker,
            "isin_maestro": maestro.get("isin"),
            "maestro_contractual": maestro,
            "mercado": cf,
            "corroboracion": d9,
            "diferencia_absoluta_precio_vs_data912": diferencias_precio,
            "control_consistencia_misma_cadena": {
                "diferencia_absoluta": diferencias_precio,
                "independiente": False,
                "nota": "Comparación diagnóstica entre endpoints relacionados; no es validación independiente.",
            },
        }
        reporte["instrumentos"].append(instrumento)

    tickers_encontrados = [
        item for item in reporte["instrumentos"]
        if item["mercado"].get("estado") != "SIN_COINCIDENCIA_EXACTA_EN_API"
    ]
    if reporte["fuentes"].get("CompararFondos", {}).get("estado") != "OK":
        reporte["estado_general"] = "SIN_FUENTE_PRIMARIA"
    elif not tickers_encontrados:
        reporte["estado_general"] = "SIN_COINCIDENCIAS_EXACTAS_EN_FUENTE_PRIMARIA"
    elif any(i["mercado"].get("estado") == "BLOQUEADO_CONFLICTO_CONTRACTUAL"
             for i in reporte["instrumentos"]):
        reporte["estado_general"] = "REQUIERE_AUDITORIA_CONTRACTUAL"
    elif any(i["mercado"].get("precio_utilizable")
             for i in reporte["instrumentos"]):
        reporte["estado_general"] = "CONSULTA_COMPLETADA_CON_PRECIOS_DE_MERCADO"
    else:
        reporte["estado_general"] = "CONSULTA_COMPLETADA_SIN_PRECIOS_VALIDOS"

    reporte["errores"] = errores
    reporte["control_publicacion"] = {
        "publicar_tir_y_duration": False,
        "motivo": "Este motor aún no recalcula independientemente la TIR ni certifica los flujos contra documentación contractual oficial.",
        "publicar_precio": all(
            i["mercado"].get("precio_utilizable") is True
            for i in reporte["instrumentos"]
        ) and bool(reporte["instrumentos"]),
        "nota": "precio_utilizable indica disponibilidad de mercado con reservas; no equivale a recomendación ni certificación contractual.",
    }
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
            f'Precio especie={m.get("precio_especie")} {m.get("moneda_precio_especie")} | '
            f'Precio eq. API={m.get("precio_api_equivalente")} | '
            f'TIR API={m.get("tir_api_pct")} | Ops={m.get("operaciones")}'
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
        if reporte["estado_general"] in {
            "SIN_FUENTE_PRIMARIA",
            "SIN_COINCIDENCIAS_EXACTAS_EN_FUENTE_PRIMARIA",
        }:
            print(
                "ERROR: no se actualiza el tablero porque la fuente primaria "
                "no entregó coincidencias exactas suficientes.",
                file=sys.stderr,
            )
            return 2
        return 0
    except Exception as exc:
        print(f"ERROR FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
