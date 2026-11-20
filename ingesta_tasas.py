#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FIBRX — Ingesta estricta de seis clases FCI desde ArgentinaDatos.

Publica exclusivamente tasas_mercado.json (schema_version 6).
No genera auditoria_fci.json, no usa tasas fallback y no cambia clases.
Si falta una clase, su identidad no coincide o la TNA no tiene unidad validable,
la ejecución falla antes de reemplazar el JSON publicado.
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
        raise RuntimeError(f"ArgentinaDatos HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"No se pudo consultar ArgentinaDatos: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("ArgentinaDatos devolvió JSON inválido") from exc


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
    """La API observada entrega TNA en porcentaje convencional (p.ej. 18.1509).
    Se convierte a fracción (0.181509). No se adivinan valores <=1.
    """
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return None
    if 1 < x <= 500:
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
        "advertencia":"Datos observados desde una única fuente pública (ArgentinaDatos/CNV). La TNA estimada corresponde al período informado; no es contractual ni garantiza rendimientos futuros. Las referencias CAFCI se mantienen separadas de los IDs API.",
        "cantidad_fondos_fuente": len(funds),
        "cantidad_registros_mercado_dinero": len(market_records),
        "error_consulta_mercado_dinero": market_error,
        "vehiculos": rows,
        "diagnostico_vehiculos": diagnostics,
        "vehiculos_omitidos_o_no_cubiertos": [],
    }
    # Final structural checks before atomic replacement.
    required = {x["id"] for x in TARGETS}
    actual = {x.get("id") for x in data["vehiculos"]}
    if actual != required or any(not (0 < float(x["tna"]) <= 5) for x in rows):
        raise RuntimeError("Validación final del esquema 6 fallida; se conserva el JSON anterior.")
    atomic_write(OUTPUT, data)
    print(f"[OK] Publicado {OUTPUT}: schema 6, {len(rows)}/6 clases. No se genera auditoria_fci.json.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print("[SAFEGUARD] No se reemplazó tasas_mercado.json.", file=sys.stderr)
        sys.exit(1)
