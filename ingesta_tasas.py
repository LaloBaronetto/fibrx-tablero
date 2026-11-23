#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FIBRX — Ingesta estricta de seis clases FCI desde ArgentinaDatos.

Publica exclusivamente tasas_mercado.json (schema_version 6).
No genera auditoria_fci.json, no usa tasas fallback y no cambia clases.
Si falta una clase, su identidad no coincide o la TNA no tiene unidad validable,
la ejecución falla antes de reemplazar el JSON publicado. Se admiten TNA negativas
cuando la fuente las entrega explícitamente en porcentaje convencional.
"""
from __future__ import annotations
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.argentinadatos.com/v1"
FUNDS_URL = f"{API}/finanzas/fci/fondos"
MONEY_MARKET_URL = f"{API}/finanzas/fci/mercadoDinero/ultimo"
# API pública adicional para renta fija argentina (BYMA; rezago aproximado 5 min).
BONDS_URL = "https://compararfondos.com.ar/api/bonos"
OUTPUT = Path("tasas_mercado.json")
TIMEOUT = 45

TARGETS = [
    {"id":"BALZ_MM_PESOS_A", "name":"Balanz Capital Money Market - Clase A", "fondo_id":"1213", "clase_id":"3355", "cnv":"1152", "manager":"balanz", "cafci":{"fondo_id":"1213","clase_id":"3355"}},
    {"id":"BALZ_MM_PESOS_L", "name":"Balanz Capital Money Market - Clase L Ley N° 27.743", "fondo_id":"1213", "clase_id":"4779", "cnv":"1152", "manager":"balanz", "cafci":{"fondo_id":"1213","clase_id":"4779"}},
    {"id":"BALZ_PERFORMANCE_XIII_D", "name":"Balanz Performance XIII - Clase D", "fondo_id":"1568", "clase_id":"5074", "cnv":"1293", "manager":"balanz", "cafci":{"fondo_id":"1568","clase_id":"5074"}},
    {"id":"IOL_CASH_MGMT_B", "name":"IOL Cash Management - Clase B", "fondo_id":"1301", "clase_id":"3698", "cnv":"1246", "manager":"adcap", "cafci":{"fondo_id":"1301","clase_id":"3698"}},
    {"id":"UALINTEC_AHORRO_A", "name":"Ualintec Ahorro Pesos - Clase A", "fondo_id":"1307", "clase_id":"3713", "cnv":"1202", "manager":"uala", "cafci":{"fondo_id":"1307","clase_id":"3713"}},
    {"id":"UALINTEC_PESOS_PLUS_A", "name":"Ualintec Pesos Plus - Clase A", "fondo_id":"1564", "clase_id":"5021", "cnv":"1565", "manager":"uala", "cafci":{"fondo_id":"1564","clase_id":"5021"}},
]


# ON incorporadas como universo de comparación, sin datos de cartera personal.
# Los campos contractuales no confirmados se dejan en null; no se infieren cupones
# ni porcentajes de amortización a partir de importes de tenencias individuales.
ON_TARGETS = [
    {"ticker":"CP36D", "emisor":"Compañía General de Combustibles S.A.", "nombre":"CGC Clase 36", "isin":"AR0015464982", "moneda_objetivo":"USD MEP", "ley":"Argentina", "vencimiento":"2027-10-10", "tasa_cupon_anual":6.50, "tipo_tasa":"Fija", "estructura":"Bullet", "frecuencia":"Semestral", "fuente_contractual":"Datos de Balanz informados por el usuario; corte 2026-11-20"},
    {"ticker":"YM34D", "emisor":"YPF S.A.", "nombre":"YPF Clase XXXIV", "isin":"USP989MJBY67", "moneda_objetivo":"USD MEP", "ley":"Nueva York", "vencimiento":"2034-01-17", "tasa_cupon_anual":8.25, "tipo_tasa":"Fija", "estructura":"Amortizable", "frecuencia":"Semestral", "fuente_contractual":"Datos de Balanz informados por el usuario; corte 2026-11-20"},
    {"ticker":"VSCVD", "emisor":"Vista Energy Argentina S.A.U.", "nombre":"Vista Energy Clase XXIX", "isin":"USP9659RAB44", "moneda_objetivo":"USD MEP", "ley":"Nueva York", "vencimiento":"2033-06-10", "tasa_cupon_anual":8.50, "tipo_tasa":"Fija", "estructura":"Amortizable", "frecuencia":"Semestral", "fuente_contractual":"Datos de Balanz informados por el usuario; corte 2026-11-20"},
    {"ticker":"AEC3O", "emisor":"AES Argentina Generación", "nombre":"AES Argentina Generación Clase 3", "isin":None, "moneda_objetivo":"Especie O (validar moneda de liquidación)", "ley":None, "vencimiento":"2028-08-06", "tasa_cupon_anual":None, "tipo_tasa":None, "estructura":None, "frecuencia":"Semestral (fechas observadas; contrato a validar)", "fuente_contractual":"Descripción y fechas de pagos aportadas; sin importes personales"},
    {"ticker":"IRCPO", "emisor":"IRSA Inversiones y Representaciones S.A.", "nombre":"IRSA 8% 2035", "isin":None, "moneda_objetivo":"Especie O (validar moneda de liquidación)", "ley":None, "vencimiento":"2035-03-31", "tasa_cupon_anual":8.00, "tipo_tasa":"Fija (según descripción aportada)", "estructura":"Amortizable (por validar porcentajes)", "frecuencia":"Semestral", "fuente_contractual":"Descripción y fechas de pagos aportadas; sin importes personales"},
    {"ticker":"MGCRO", "emisor":"Pampa Energía S.A.", "nombre":"Pampa Energía 7,75% 2037", "isin":None, "moneda_objetivo":"Especie O (validar moneda de liquidación)", "ley":None, "vencimiento":"2037-11-14", "tasa_cupon_anual":7.75, "tipo_tasa":"Fija (según descripción aportada)", "estructura":"Bullet (pendiente de prospecto)", "frecuencia":"Semestral", "fuente_contractual":"Descripción y fechas de pagos aportadas; sin importes personales"},
    {"ticker":"PQCSO", "emisor":"Petroquímica Comodoro Rivadavia S.A.", "nombre":"PCR Clase S 2031", "isin":None, "moneda_objetivo":"Especie O (validar moneda de liquidación)", "ley":None, "vencimiento":"2031-02-17", "tasa_cupon_anual":None, "tipo_tasa":None, "estructura":"Bullet (pendiente de prospecto)", "frecuencia":"Semestral", "fuente_contractual":"Descripción y fechas de pagos aportadas; sin importes personales"},
    {"ticker":"TLCPO", "emisor":"Telecom Argentina S.A.", "nombre":"Telecom Argentina 9,25% 2033", "isin":None, "moneda_objetivo":"Especie O (validar moneda de liquidación)", "ley":None, "vencimiento":"2033-05-28", "tasa_cupon_anual":9.25, "tipo_tasa":"Fija (según descripción aportada)", "estructura":"Amortizable (por validar porcentajes)", "frecuencia":"Semestral", "fuente_contractual":"Descripción y fechas de pagos aportadas; sin importes personales"},
    {"ticker":"VSCXO", "emisor":"Vista Energy Argentina S.A.U.", "nombre":"Vista Energy 7,875% 2038", "isin":None, "moneda_objetivo":"Especie O (validar moneda de liquidación)", "ley":None, "vencimiento":"2038-04-08", "tasa_cupon_anual":7.875, "tipo_tasa":"Fija (según descripción aportada)", "estructura":"Amortizable (por validar porcentajes)", "frecuencia":"Semestral", "fuente_contractual":"Descripción y fechas de pagos aportadas; sin importes personales"},
]


def norm(value: object) -> str:
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent":"FIBRX/1.5 (+https://github.com/lalobaronetto/fibrx-tablero)", "Accept":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status} en {url}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} en {url}: {body}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"No se pudo consultar {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"La fuente {url} devolvió JSON inválido") from exc


def as_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("fondos", "data", "resultados", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def currency_is_ars(value):
    n = norm(value)
    return n in {"ars", "peso argentino", "pesos argentinos", "peso", "pesos", "moneda nacional"}


def normalize_tna(raw):
    """Convierte la TNA de porcentaje convencional a fracción.

    La API de ArgentinaDatos informa el campo tnaEstimada en porcentaje:
    18.1509 -> 0.181509 y -17.6921 -> -0.176921.
    Se admiten valores negativos porque una TNA estimada puede ser negativa.
    Se rechazan cero y valores entre -1 y 1 porque su unidad sería ambigua
    frente a una posible fracción ya normalizada.
    """
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return None
    if 1 < x <= 500 or -500 <= x < -1:
        return x / 100.0
    return None


def find_exact_fund(funds, cfg):
    matches = [f for f in funds if isinstance(f, dict)
               and str(f.get("fondoId")) == cfg["fondo_id"]
               and str(f.get("claseId")) == cfg["clase_id"]]
    if len(matches) != 1:
        raise RuntimeError(f"{cfg['id']}: se esperaba 1 coincidencia por fondoId/claseId; se encontraron {len(matches)}")
    f = matches[0]
    name = norm(f.get("nombre"))
    # No alcanza con el ID si el nombre comercial/clase cambió inesperadamente.
    if "clase" not in name or not all(token in name for token in norm(cfg["name"]).split()[:3]):
        raise RuntimeError(f"{cfg['id']}: el nombre recibido no coincide con la clase esperada: {f.get('nombre')!r}")
    manager = norm(f.get("administradora"))
    if cfg["manager"] not in manager:
        raise RuntimeError(f"{cfg['id']}: administradora inesperada: {f.get('administradora')!r}")
    if not currency_is_ars(f.get("moneda")):
        raise RuntimeError(f"{cfg['id']}: moneda no ARS: {f.get('moneda')!r}")
    if str(f.get("codigoCNV") or "") != cfg["cnv"]:
        raise RuntimeError(f"{cfg['id']}: código CNV inesperado: {f.get('codigoCNV')!r}")
    return f


def money_market_records(payload):
    return as_list(payload)


def money_market_contrast(records, fund, cfg):
    if cfg["id"] not in {"BALZ_MM_PESOS_A", "BALZ_MM_PESOS_L"}:
        return None
    target_name = norm(fund.get("nombre"))
    candidates = []
    for item in records:
        if not isinstance(item, dict):
            continue
        name = norm(item.get("fondo") or item.get("nombre") or item.get("nombreFondo"))
        if name and (name == target_name or ("balanz capital money market" in name and ("clase a" if cfg["id"].endswith("_A") else "clase l") in name)):
            candidates.append(item)
    if len(candidates) != 1:
        return None
    x = candidates[0]
    return {
        "fondo": x.get("fondo") or x.get("nombre") or x.get("nombreFondo"),
        "fecha": x.get("fecha") or x.get("fechaActualizacion"),
        "vcp": x.get("valorCuotaparte") or x.get("vcp"),
        "ccp": x.get("cuotapartes") or x.get("ccp"),
        "patrimonio": x.get("patrimonio") or x.get("valorPatrimonio"),
    }


def build_record(fund, cfg, contrast):
    rend = fund.get("rendimientos") or {}
    tna = normalize_tna(rend.get("tnaEstimada"))
    if tna is None:
        raise RuntimeError(f"{cfg['id']}: TNA ausente o unidad no confirmada ({rend.get('tnaEstimada')!r}); no se publica.")
    date = fund.get("fecha") or fund.get("fechaActualizacion")
    if not date:
        raise RuntimeError(f"{cfg['id']}: falta fecha de observación.")
    return {
        "id": cfg["id"],
        "nombre": fund.get("nombre") or cfg["name"],
        "nombre_api": fund.get("nombre"),
        "tna": tna,
        "estado_tasa": "NEGATIVA_OBSERVADA" if tna < 0 else "POSITIVA",
        "tipo_indicador": "TNA_ESTIMADA",
        "unidad_original": "porcentaje_convencional",
        "risky": False,
        "cur": "ARS",
        "fuente_tasa": "ArgentinaDatos — CNV Cuotapartes, campo tnaEstimada",
        "fuente_cuotaparte": "ArgentinaDatos — CNV Cuotapartes",
        "fondoId": str(fund.get("fondoId")),
        "claseId": str(fund.get("claseId")),
        "codigoCNV": str(fund.get("codigoCNV")),
        "referencia_cafci": cfg["cafci"],
        "fecha_observacion": str(date)[:10],
        "periodo_tna": rend.get("tnaEstimadaPeriodo"),
        "dias_base_tna": rend.get("tnaEstimadaDias"),
        "retorno_7d": rend.get("ultimos7Dias"),
        "dias_7d": rend.get("diasUltimos7Dias"),
        "retorno_30d": rend.get("unMes"),
        "dias_30d": rend.get("diasUnMes"),
        "retorno_90d": rend.get("noventaDias"),
        "dias_90d": rend.get("diasNoventaDias"),
        "valor_cuotaparte": rend.get("valorCuotaparte"),
        "administradora": fund.get("administradora"),
        "depositaria": fund.get("depositaria"),
        "moneda_fondo": fund.get("moneda"),
        "plazo_liquidacion_dias": fund.get("plazoLiquidacionDias"),
        "inversion_minima_informada": fund.get("inversionMinima"),
        "honorarios_informados": fund.get("honorarios"),
        "estado_dato": "OBSERVADO_UNA_FUENTE",
        "nivel_validacion": "NOMBRE_CLASE_ADMINISTRADORA_MONEDA_API",
        "contraste_mercado_dinero": contrast,
    }



def bond_records(payload):
    """Normaliza el contenedor documentado por CompararFondos sin inventar campos."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("bonds", "bonos", "data", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def build_on_records(payload, fetch_error=None):
    bonds = bond_records(payload) if payload is not None else []
    by_ticker = {}
    for item in bonds:
        if isinstance(item, dict) and item.get("ticker"):
            by_ticker.setdefault(str(item["ticker"]).upper(), []).append(item)

    rueda = payload.get("rueda") if isinstance(payload, dict) else None
    fecha_rueda = rueda.get("fechaRueda") if isinstance(rueda, dict) else None
    rows, diagnostics = [], {}
    for cfg in ON_TARGETS:
        matches = by_ticker.get(cfg["ticker"].upper(), [])
        quote = matches[0] if len(matches) == 1 else None
        if len(matches) > 1:
            status = "TICKER_DUPLICADO_EN_FUENTE"
        elif quote is None:
            status = "SIN_COTIZACION_EN_FUENTE" if not fetch_error else "FUENTE_NO_DISPONIBLE"
        elif quote.get("precioStale") is True:
            status = "PRECIO_SIN_OPERACIONES_EN_ULTIMA_RUEDA"
        else:
            status = "COTIZACION_RECIBIDA"

        # El proveedor publica precio/TIR/duración calculados desde su rueda.
        # Se conserva null cuando la fuente no puede calcular una métrica.
        market = {}
        if quote:
            for key in ("precio", "precioARS", "tir", "tirEsReal", "paridad", "paridadBase",
                        "valorTecnico", "capitalResidual", "duration", "vencimiento",
                        "moneda", "tcType", "pagoEn", "operaciones", "precioStale",
                        "tirPocasOps", "tirMonedaCruzada", "flujos"):
                if key in quote:
                    market[key] = quote.get(key)
        row = {**cfg, **market,
               "estado_dato_mercado":status,
               "fecha_rueda":fecha_rueda,
               "fuente_mercado":"CompararFondos API — precios BYMA y métricas de renta fija",
               "url_fuente":"https://compararfondos.com.ar/api/bonos",
               "fecha_cotizacion":fecha_rueda,
               "nivel_validacion":"TICKER_EXACTO" if quote else "IDENTIDAD_CONFIGURADA_SIN_COTIZACION",
               "advertencia":"La especie O puede representar cotización en pesos; revisar tcType/moneda antes de comparar con especies D en USD. Flujos solo se publican si la API los devuelve."}
        rows.append(row)
        diagnostics[cfg["ticker"]] = {"estado":status, "coincidencias":len(matches), "fecha_rueda":fecha_rueda}
    return rows, diagnostics, len(bonds)


def atomic_write(path, payload):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main():
    print("[FIBRX] Consultando catálogo FCI de ArgentinaDatos...")
    source_payload = fetch_json(FUNDS_URL)
    funds = as_list(source_payload)
    if not funds:
        raise RuntimeError("La fuente no devolvió una lista de fondos reconocible.")
    print(f"[OK] {len(funds)} registros recibidos.")

    market_records, market_error = [], None
    try:
        market_payload = fetch_json(MONEY_MARKET_URL)
        market_records = money_market_records(market_payload)
    except Exception as exc:  # Contraste auxiliar: no reemplaza ni invalida la fuente principal.
        market_error = str(exc)
        print(f"[AVISO] No se pudo obtener contraste de mercado dinero: {market_error}")

    # La fuente de ON es independiente: su caída no invalida ni bloquea los seis FCI.
    bonds_payload, bonds_error = None, None
    try:
        print("[FIBRX] Consultando cotizaciones y flujos de ON...")
        bonds_payload = fetch_json(BONDS_URL)
    except Exception as exc:
        bonds_error = str(exc)
        print(f"[AVISO] No se pudo obtener mercado de ON: {bonds_error}")
    on_rows, on_diagnostics, bonds_count = build_on_records(bonds_payload, bonds_error)
    for item in on_rows:
        print(f"[ON] {item['ticker']}: {item['estado_dato_mercado']}")

    rows, diagnostics = [], {}
    for cfg in TARGETS:
        fund = find_exact_fund(funds, cfg)
        contrast = money_market_contrast(market_records, fund, cfg)
        row = build_record(fund, cfg, contrast)
        rows.append(row)
        diagnostics[cfg["id"]] = {
            "estado": "IDENTIFICADO_Y_PUBLICADO",
            "detalle": "Coincidencia exacta por fondoId/claseId; nombre, clase, administradora, código CNV y moneda validados.",
            "candidatos": 1,
            "fondoId_argentinadatos": row["fondoId"],
            "claseId_argentinadatos": row["claseId"],
            "codigoCNV": row["codigoCNV"],
        }
        print(f"[OK] {cfg['id']}: TNA {row['tna']*100:.4f}% — {row['nombre']}")

    if len(rows) != 6 or len({r['id'] for r in rows}) != 6:
        raise RuntimeError("Control final fallido: no se reunieron exactamente seis clases únicas.")

    updated = datetime.now(timezone.utc).isoformat()
    source_date = source_payload.get("fechaActualizacion") if isinstance(source_payload, dict) else None
    if not source_date:
        source_date = max(r["fecha_observacion"] for r in rows)
    data = {
        "schema_version": 6,
        "actualizado_el": updated,
        "fecha_datos_fuente": source_date,
        "estado_global": "DATOS_ARGENTINADATOS_UNA_FUENTE",
        "fuentes": [
            {"nombre":"ArgentinaDatos", "endpoint":"/v1/finanzas/fci/fondos", "base":API,
             "origen_de_datos":"CNV - Cuotapartes", "rol":"Identificación de clases, cuotaparte y rendimientos publicados por la API."},
            {"nombre":"ArgentinaDatos", "endpoint":"/v1/finanzas/fci/mercadoDinero/ultimo", "base":API,
             "origen_de_datos":"CNV - Cuotapartes", "rol":"Contraste exacto de nombre, fecha y VCP para Balanz Money Market A/L."},
        ],
        "advertencia":"FCI: datos observados desde ArgentinaDatos/CNV; TNA estimada, no contractual. ON: cotizaciones y métricas de CompararFondos/BYMA, sujetas a disponibilidad, volumen y rezago; TIR nula significa que la fuente no la pudo calcular. Los datos personales de cartera no se incorporan. No se infieren cupones ni amortizaciones faltantes.",
        "cantidad_fondos_fuente": len(funds),
        "cantidad_registros_mercado_dinero": len(market_records),
        "error_consulta_mercado_dinero": market_error,
        "fuente_obligaciones_negociables": {
            "nombre":"CompararFondos", "endpoint":"/api/bonos",
            "url":BONDS_URL, "origen_de_datos":"BYMA (precios con rezago aproximado de 5 minutos) y flujos cargados/verificados por el proveedor",
            "atribucion":"Al publicar estos datos debe mencionarse compararfondos.com.ar.",
            "error_consulta":bonds_error, "cantidad_instrumentos_recibidos":bonds_count,
            "fecha_rueda":(bonds_payload.get("rueda", {}).get("fechaRueda") if isinstance(bonds_payload, dict) and isinstance(bonds_payload.get("rueda"), dict) else None)
        },
        "obligaciones_negociables": on_rows,
        "diagnostico_obligaciones_negociables": on_diagnostics,
        "vehiculos": rows,
        "diagnostico_vehiculos": diagnostics,
        "vehiculos_omitidos_o_no_cubiertos": [],
    }
    # Final structural checks before atomic replacement.
    required = {x["id"] for x in TARGETS}
    actual = {x.get("id") for x in data["vehiculos"]}
    # Una TNA negativa es válida si viene del campo tnaEstimada y su unidad
    # porcentual fue reconocida por normalize_tna(). Se conserva su signo.
    if actual != required or any(
        float(x["tna"]) == 0 or not (-5 <= float(x["tna"]) <= 5)
        for x in rows
    ):
        raise RuntimeError("Validación final del esquema 6 fallida; se conserva el JSON anterior.")
    atomic_write(OUTPUT, data)
    print(f"[OK] Publicado {OUTPUT}: schema 6, {len(rows)}/6 clases FCI y {len(on_rows)} ON configuradas ({sum(1 for x in on_rows if x['estado_dato_mercado']=='COTIZACION_RECIBIDA')} con cotización recibida). No se genera auditoria_fci.json.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print("[SAFEGUARD] No se reemplazó tasas_mercado.json.", file=sys.stderr)
        sys.exit(1)
