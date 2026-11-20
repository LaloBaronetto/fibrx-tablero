#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FIBRX — Ingesta FCI desde ArgentinaDatos (una sola fuente).

- No consulta Fonditos ni requiere API key.
- No selecciona clases de FCI por aproximación.
- Permite fijar la clase exacta mediante fondoId/claseId una vez auditada.
- Genera auditoria_fci.json con candidatos para los mapeos pendientes.
- Nunca convierte una tasa ambigua ni publica una clase no identificada.
- Si no hay datos publicables, actualiza el JSON con estado explícito y cero
  vehículos; el frontend debe mostrar ausencia de datos, no tasas fallback.
"""
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.argentinadatos.com/v1"
FUNDS_ENDPOINT = f"{API}/finanzas/fci/fondos"
OUTPUT = Path("tasas_mercado.json")
AUDIT_OUTPUT = Path("auditoria_fci.json")
TIMEOUT = 40

# IMPORTANTE:
# Completar fondo_id y clase_id solo después de verificar la clase correcta.
# No usar solamente el nombre comercial del broker como identificador.
TARGETS = {
    "BALZ_MM_PESOS": {
        "aliases": ["balanz money market", "balanz ahorro", "balanz liquidez"],
        "display": "FCI Balanz Money Market",
        "fondo_id": None,
        "clase_id": None,
    },
    "BALZ_LECAPS": {
        "aliases": ["balanz lecaps"],
        "display": "FCI Balanz Lecaps",
        "fondo_id": None,
        "clase_id": None,
    },
    "IOL_CASH_MGMT": {
        "aliases": ["iol cash management", "iol money market"],
        "display": "FCI IOL Cash Management",
        "fondo_id": None,
        "clase_id": None,
    },
    "MP_MONEY_MARKET": {
        "aliases": ["mercado fondo"],
        "display": "Mercado Fondo",
        "fondo_id": None,
        "clase_id": None,
    },
    "UALINTEC_AHORRO": {
        "aliases": ["ualintec ahorro pesos"],
        "display": "Ualintec Ahorro Pesos",
        "fondo_id": None,
        "clase_id": None,
    },
    "UALINTEC_PESOS_PLUS": {
        "aliases": ["ualintec pesos plus"],
        "display": "Ualintec Pesos Plus",
        "fondo_id": None,
        "clase_id": None,
    },
}


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "FIBRX/1.2", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status} consultando ArgentinaDatos")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"ArgentinaDatos respondió HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"No se pudo conectar con ArgentinaDatos: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("ArgentinaDatos devolvió JSON inválido") from exc


def fetch_funds():
    payload = get_json(FUNDS_ENDPOINT)
    funds = payload.get("fondos") if isinstance(payload, dict) else None
    if not isinstance(funds, list) or not funds:
        raise RuntimeError("La respuesta no contiene la lista 'fondos' esperada.")
    return payload, funds


def candidate_matches(cfg, funds):
    """Devuelve candidatos por alias y por IDs; no decide por similitud."""
    found = {}
    wanted_fondo = cfg.get("fondo_id")
    wanted_clase = cfg.get("clase_id")

    for fund in funds:
        if not isinstance(fund, dict):
            continue

        # Cuando se configuraron IDs, estos son la única regla de selección.
        if wanted_fondo or wanted_clase:
            if wanted_fondo and str(fund.get("fondoId")) != str(wanted_fondo):
                continue
            if wanted_clase and str(fund.get("claseId")) != str(wanted_clase):
                continue
            key = (str(fund.get("fondoId") or ""), str(fund.get("claseId") or ""))
            found[key] = (fund, ["ID explícito"])
            continue

        name = normalize(fund.get("nombre"))
        matched_aliases = []
        for alias in cfg["aliases"]:
            alias_norm = normalize(alias)
            # Coincidencia de frase completa; no se usan fragmentos sueltos.
            if alias_norm and re.search(
                rf"(?<![a-z0-9]){re.escape(alias_norm)}(?![a-z0-9])", name
            ):
                matched_aliases.append(alias)

        if matched_aliases:
            key = (str(fund.get("fondoId") or ""), str(fund.get("claseId") or ""))
            found[key] = (fund, matched_aliases)

    return list(found.values())


def normalize_tna(value):
    """
    ArgentinaDatos documenta el campo tnaEstimada como numérico, pero el
    esquema público no explicita inequívocamente la unidad. Para no adivinar,
    solo se acepta el formato porcentaje convencional >1 y <=500.
    Los valores 0<x<=1 quedan pendientes de confirmación de unidad.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (1 < number <= 500):
        return None
    return number / 100.0


def fund_audit_record(fund, aliases):
    rend = fund.get("rendimientos") or {}
    return {
        "nombre": fund.get("nombre"),
        "alias_coincidentes": aliases,
        "fondoId": fund.get("fondoId"),
        "claseId": fund.get("claseId"),
        "codigoCNV": fund.get("codigoCNV"),
        "administradora": fund.get("administradora"),
        "depositaria": fund.get("depositaria"),
        "moneda": fund.get("moneda"),
        "monedaInversion": fund.get("monedaInversion"),
        "tipoRenta": fund.get("tipoRenta"),
        "horizonte": fund.get("horizonte"),
        "fecha": fund.get("fecha"),
        "plazoLiquidacionDias": fund.get("plazoLiquidacionDias"),
        "inversionMinima": fund.get("inversionMinima"),
        "tnaEstimada_raw": rend.get("tnaEstimada"),
        "tnaEstimadaPeriodo": rend.get("tnaEstimadaPeriodo"),
        "tnaEstimadaDias": rend.get("tnaEstimadaDias"),
        "ultimos7Dias": rend.get("ultimos7Dias"),
        "diasUltimos7Dias": rend.get("diasUltimos7Dias"),
        "unMes": rend.get("unMes"),
        "diasUnMes": rend.get("diasUnMes"),
        "valorCuotaparte": rend.get("valorCuotaparte"),
    }


def build_rows(funds):
    rows = []
    audit = {
        "generado_el": datetime.now(timezone.utc).astimezone().isoformat(),
        "fuente": FUNDS_ENDPOINT,
        "total_clases_recibidas": len(funds),
        "nota": (
            "Informe diagnóstico. Los candidatos no están validados como el "
            "instrumento comercial indicado por FIBRX."
        ),
        "vehiculos": {},
    }

    for vehicle_id, cfg in TARGETS.items():
        candidates = candidate_matches(cfg, funds)
        audit_rows = [fund_audit_record(f, aliases) for f, aliases in candidates]
        audit["vehiculos"][vehicle_id] = {
            "cantidad_candidatos": len(audit_rows),
            "ids_configurados": {
                "fondo_id": cfg.get("fondo_id"),
                "clase_id": cfg.get("clase_id"),
            },
            "candidatos": audit_rows,
        }

        if len(candidates) != 1:
            print(
                f"[PENDIENTE] {vehicle_id}: {len(candidates)} candidatos. "
                "Se requiere mapear fondoId/claseId exactos."
            )
            for record in audit_rows[:30]:
                print(
                    "  - {nombre} | fondoId={fondoId} | claseId={claseId} | "
                    "CNV={codigoCNV} | admin={administradora} | moneda={moneda}".format(**record)
                )
            continue

        fund, aliases = candidates[0]
        # Si aún no hay IDs explícitos, una coincidencia única por nombre no
        # basta para afirmar que el fondo del broker es el mismo instrumento.
        if not (cfg.get("fondo_id") and cfg.get("clase_id")):
            print(
                f"[PENDIENTE] {vehicle_id}: candidato único por nombre, "
                "pero falta confirmar fondoId y claseId."
            )
            continue

        rend = fund.get("rendimientos") or {}
        tna = normalize_tna(rend.get("tnaEstimada"))
        if tna is None:
            print(
                f"[OMITIDO] {vehicle_id}: tnaEstimada ausente o con unidad "
                "no confirmada (valor={rend.get('tnaEstimada')!r})."
            )
            continue

        fecha = fund.get("fecha") or fund.get("fechaActualizacion")
        if not fecha:
            print(f"[OMITIDO] {vehicle_id}: sin fecha de observación.")
            continue

        rows.append({
            "id": vehicle_id,
            "nombre": fund.get("nombre") or cfg["display"],
            "tna": tna,
            "risky": False,
            "cur": "ARS",
            "fuente_tasa": "ArgentinaDatos — CNV Cuotapartes, campo tnaEstimada",
            "fuente_cuotaparte": "ArgentinaDatos — CNV Cuotapartes",
            "fondoId": fund.get("fondoId"),
            "claseId": fund.get("claseId"),
            "codigoCNV": fund.get("codigoCNV"),
            "fecha_observacion": fecha,
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
            "moneda_fondo": fund.get("moneda"),
            "plazo_liquidacion_dias": fund.get("plazoLiquidacionDias"),
            "inversion_minima_informada": fund.get("inversionMinima"),
            "honorarios_informados": fund.get("honorarios"),
            "estado_dato": "OBSERVADO_UNA_FUENTE",
            "nivel_validacion": "UNA_FUENTE_PUBLICA",
        })
        print(
            f"[OK] {vehicle_id}: {tna * 100:.2f}% TNA — "
            f"{fund.get('nombre')} — claseId={fund.get('claseId')}"
        )

    return rows, audit


def atomic_write(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def main():
    print("[FIBRX] Consultando ArgentinaDatos — catálogo detallado de FCI...")
    payload, funds = fetch_funds()
    print(f"[OK] ArgentinaDatos: {len(funds)} clases recibidas.")
    rows, audit = build_rows(funds)
    atomic_write(AUDIT_OUTPUT, audit)
    print(f"[OK] Auditoría generada: {AUDIT_OUTPUT}")

    if not rows:
        status = "SIN_FONDOS_IDENTIFICADOS"
        warning = (
            "No hay fondos con identidad de clase confirmada. No se publican tasas. "
            "Revisar auditoria_fci.json y completar fondoId/claseId en ingesta_tasas.py. "
            "No se usan tasas fallback ni se inventan asociaciones."
        )
    else:
        status = "DATOS_ARGENTINADATOS_UNA_FUENTE"
        warning = (
            "Datos de una sola fuente pública (ArgentinaDatos/CNV). La TNA es estimada "
            "por la fuente para el período informado; no es contractual ni garantiza "
            "rendimiento futuro. Solo se publican clases con fondoId y claseId explícitos."
        )

    data = {
        "schema_version": 4,
        "actualizado_el": datetime.now(timezone.utc).astimezone().isoformat(),
        "fecha_datos_fuente": payload.get("fechaActualizacion"),
        "estado_global": status,
        "fuentes": [{
            "nombre": "ArgentinaDatos",
            "endpoint": "/v1/finanzas/fci/fondos",
            "base": API,
            "origen_de_datos": "CNV - Cuotapartes",
            "rol": "Identificación de clases, cuotaparte y rendimientos publicados por la API.",
        }],
        "advertencia": warning,
        "cantidad_fondos_fuente": len(funds),
        "vehiculos": rows,
        "vehiculos_omitidos_o_no_cubiertos": sorted(set(TARGETS) - {r["id"] for r in rows}),
    }
    atomic_write(OUTPUT, data)
    print(f"[OK] JSON actualizado: {len(rows)} vehículos con TNA publicable.")
    if not rows:
        print("[ATENCIÓN] No hay tasas publicables. El archivo declara explícitamente la ausencia de datos.")
    print(f"[OK] Estado global: {status}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print("[SAFEGUARD] No se reemplaza tasas_mercado.json si falla la consulta.", file=sys.stderr)
        sys.exit(1)
