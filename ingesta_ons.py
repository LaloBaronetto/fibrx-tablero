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
import gzip
import hashlib
import zipfile
import re
import unicodedata
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


URL_COMPARARFONDOS = "https://compararfondos.com.ar/api/bonos"
URL_DATA912 = "https://data912.com/live/arg_corp"
VERSION_MOTOR = "2.2.0"
TIMEOUT_SEGUNDOS = 20
ARCHIVO_SALIDA = os.environ.get("FIBRX_ON_OUTPUT", "ons_mercado.json")
DIRECTORIO_EVIDENCIA = os.environ.get("FIBRX_ON_EVIDENCE_DIR", "ons_evidencia")
ARCHIVO_PAQUETE_EVIDENCIA = os.environ.get("FIBRX_ON_EVIDENCE_ZIP", "ons_evidencia.zip")
NIVEL_1 = "1_DATO_RECIBIDO_TRAZABLE"
NIVEL_2 = "2_DATO_CONTRASTADO_FUENTE_INDEPENDIENTE"
NIVEL_3 = "3_DATO_VALIDADO_DOCUMENTALMENTE"

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


def obtener_json_con_evidencia(url: str, nombre: str, directorio: str) -> Tuple[Any, Dict[str, Any]]:
    """Descarga JSON, conserva bytes originales comprimidos y calcula su SHA-256."""
    capturado_utc = ahora_utc()
    req = Request(
        url,
        headers={
            "User-Agent": "FIBRX-ON-Research/1.0 (educational financial data)",
            "Accept": "application/json",
        },
    )
    with urlopen(req, timeout=TIMEOUT_SEGUNDOS) as response:
        status_http = getattr(response, "status", 200)
        headers = response.headers
        raw = response.read()
    if status_http != 200:
        raise RuntimeError(f"HTTP {status_http} consultando {url}")

    # El hash se calcula sobre los bytes HTTP originales, antes de descomprimir/parsear.
    sha256 = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8-sig"))
    carpeta = Path(directorio)
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre_archivo = f"{nombre}_respuesta_original.json.gz"
    ruta = carpeta / nombre_archivo
    with ruta.open("wb") as destino:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destino, mtime=0) as comprimido:
            comprimido.write(raw)

    metadata = {
        "fuente": nombre,
        "url": url,
        "capturado_utc": capturado_utc,
        "http_status": status_http,
        "content_type": headers.get("Content-Type"),
        "http_date": headers.get("Date"),
        "bytes_respuesta_original": len(raw),
        "sha256_respuesta_original": sha256,
        "archivo_snapshot_comprimido": str(ruta.as_posix()),
        "formato_snapshot": "gzip del cuerpo HTTP original; al descomprimir se recuperan los bytes exactos",
    }
    return payload, metadata


def crear_paquete_evidencia(metadata_fuentes: Dict[str, Any], errores: List[str],
                             directorio: str = DIRECTORIO_EVIDENCIA,
                             destino_zip: str = ARCHIVO_PAQUETE_EVIDENCIA) -> Dict[str, Any]:
    """Empaqueta snapshots y manifiesto verificable de cada consulta."""
    carpeta = Path(directorio)
    carpeta.mkdir(parents=True, exist_ok=True)
    manifiesto = {
        "producto": "FIBRX | Evidencia de ingesta de ON",
        "version_motor": VERSION_MOTOR,
        "generado_utc": ahora_utc(),
        "algoritmo_hash": "SHA-256 sobre bytes HTTP originales, antes del parseo JSON",
        "fuentes": metadata_fuentes,
        "errores": errores,
        "advertencia": "El hash acredita integridad del snapshot capturado, no la veracidad económica del dato publicado por la fuente.",
    }
    ruta_manifiesto = carpeta / "manifiesto_evidencia.json"
    ruta_manifiesto.write_text(json.dumps(manifiesto, ensure_ascii=False, indent=2), encoding="utf-8")
    ruta_readme = carpeta / "LEEME.txt"
    ruta_readme.write_text(
        "FIBRX - Evidencia de ingesta de ON\n\n"
        "Los archivos *_respuesta_original.json.gz contienen el cuerpo HTTP original comprimido.\n"
        "Para verificar: descomprimir el archivo y calcular SHA-256 sobre los bytes resultantes;\n"
        "comparar con sha256_respuesta_original del manifiesto.\n\n"
        "El hash prueba integridad del archivo capturado, no exactitud del dato de origen.\n"
        "Data912 y CompararFondos se consideran fuentes relacionadas, no independientes.\n",
        encoding="utf-8",
    )
    zip_path = Path(destino_zip)
    if zip_path.parent != Path('.'):
        zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for archivo in sorted(carpeta.iterdir()):
            if archivo.is_file():
                zf.write(archivo, arcname=archivo.name)
    return {
        "archivo": str(zip_path.as_posix()),
        "sha256_paquete": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
        "archivos_incluidos": sorted(a.name for a in carpeta.iterdir() if a.is_file()),
        "fuentes_capturadas": sorted(metadata_fuentes),
    }


def evidencia_campo(valor: Any, fuente: str, url: str, campo_api: str,
                    captura: Optional[Dict[str, Any]], nota: str = "") -> Dict[str, Any]:
    """Etiqueta la procedencia y el respaldo sin confundir recepción con validación."""
    existe = valor is not None
    return {
        "valor": valor,
        "estado": ("RECIBIDO_CON_SNAPSHOT" if (captura or {}).get("sha256_respuesta_original") else "RECIBIDO_SIN_SNAPSHOT") if existe else "NO_INFORMADO",
        "nivel": NIVEL_1 if existe and (captura or {}).get("sha256_respuesta_original") else None,
        "fuente": fuente if existe else None,
        "url": url if existe else None,
        "campo_origen": campo_api if existe else None,
        "captura_utc": (captura or {}).get("capturado_utc") if existe else None,
        "sha256_respuesta_origen": (captura or {}).get("sha256_respuesta_original") if existe else None,
        "contraste_independiente": False,
        "validacion_documental": False,
        "habilitado_publicacion": False,
        "nota": nota or ("Dato recibido y trazable; no implica verificación independiente." if existe else "La fuente no informó este campo."),
    }


def construir_evidencias_instrumento(ticker: str, mercado: Dict[str, Any],
                                     maestro: Dict[str, Any], raw_cf: Optional[Dict[str, Any]],
                                     raw_d9: Optional[Dict[str, Any]],
                                     captura_cf: Optional[Dict[str, Any]],
                                     captura_d9: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    tc = str((raw_cf or {}).get("tcType") or "").upper()
    campo_precio = "precioARS" if tc == "ARS" else "precio"
    precio = (raw_cf or {}).get(campo_precio)
    precio_equiv = (raw_cf or {}).get("precio")
    raw_cf = raw_cf or {}
    raw_d9 = raw_d9 or {}

    evidencias = {
        "precio_especie": evidencia_campo(
            precio, "CompararFondos", URL_COMPARARFONDOS, campo_precio, captura_cf,
            "Precio de la especie de negociación. En especie O se toma precioARS; en D/C se toma precio según tcType."
        ),
        "precio_equivalente_api": evidencia_campo(
            precio_equiv, "CompararFondos", URL_COMPARARFONDOS, "precio", captura_cf,
            "Equivalente informado por el agregador; no reemplaza el precio nominal de especie O."
        ),
        "tir_api_pct": evidencia_campo(
            raw_cf.get("tir"), "CompararFondos", URL_COMPARARFONDOS, "tir", captura_cf,
            "TIR calculada/informada por el agregador; no es cálculo independiente de FIBRX y permanece no publicable."
        ),
        "duration_api": evidencia_campo(
            raw_cf.get("duration"), "CompararFondos", URL_COMPARARFONDOS, "duration", captura_cf,
            "Duration informada por el agregador; no está recalculada independientemente."
        ),
        "vencimiento_api": evidencia_campo(
            raw_cf.get("vencimiento"), "CompararFondos", URL_COMPARARFONDOS, "vencimiento", captura_cf,
            "Dato de API comparado con el maestro heredado; revisar estado de conflicto/pendiente."
        ),
        "emisor_api": evidencia_campo(
            raw_cf.get("emisor"), "CompararFondos", URL_COMPARARFONDOS, "emisor", captura_cf
        ),
        "flujos_api": evidencia_campo(
            raw_cf.get("flujos"), "CompararFondos", URL_COMPARARFONDOS, "flujos", captura_cf,
            "Flujos originales de API; su estructura no acredita el contrato."
        ),
        "isin_maestro": {
            "valor": maestro.get("isin"), "estado": "REGISTRO_LOCAL_PENDIENTE_DOCUMENTACION",
            "nivel": None,
            "fuente": maestro.get("fuente_contractual"), "campo_origen": "MAESTRO_CONTRACTUAL",
            "contraste_independiente": False, "validacion_documental": False,
            "nota": "ISIN conservado del maestro local; requiere contraste con documentación oficial."
        },
        "tna_maestro": {
            "valor": maestro.get("tna"), "estado": "REGISTRO_LOCAL_PENDIENTE_DOCUMENTACION",
            "nivel": None,
            "fuente": maestro.get("fuente_contractual"), "campo_origen": "MAESTRO_CONTRACTUAL",
            "contraste_independiente": False, "validacion_documental": False,
            "nota": "Tasa del maestro heredado; no certificada por prospecto."
        },
        "amortizaciones_maestro": {
            "valor": maestro.get("cronograma_amortizacion"), "estado": "REGISTRO_LOCAL_PENDIENTE_DOCUMENTACION",
            "nivel": None,
            "fuente": maestro.get("fuente_contractual"), "campo_origen": "MAESTRO_CONTRACTUAL",
            "contraste_independiente": False, "validacion_documental": False,
            "nota": "Cronograma heredado; no certificado contra suplemento/prospecto oficial."
        },
        "comparacion_vencimiento": {
            "valor_api": mercado.get("vencimiento_api"),
            "valor_maestro": maestro.get("fecha_vencimiento"),
            "estado": (mercado.get("validacion_contractual") or {}).get("estado"),
            "nivel": NIVEL_1 if captura_cf and captura_cf.get("sha256_respuesta_original") else None,
            "fuente_api": "CompararFondos",
            "fuente_maestro": maestro.get("fuente_contractual"),
            "validacion_documental": False,
            "nota": "La comparación detecta coincidencias o conflictos entre API y maestro; no sustituye el prospecto oficial."
        },
    }
    # Data912 se documenta como consistencia interna de una cadena relacionada.
    precio_d9 = raw_d9.get("c")
    evidencias["precio_data912_relacionado"] = evidencia_campo(
        precio_d9, "Data912 (fuente relacionada)", URL_DATA912, "c", captura_d9,
        "No eleva el nivel de evidencia: no se considera fuente independiente de CompararFondos."
    )

    estado_contractual = (mercado.get("validacion_contractual") or {}).get("estado")
    conflicto = estado_contractual == "CONFLICTO_CONFIRMADO"
    pendientes = estado_contractual != "COINCIDENCIA_EN_CAMPOS_DISPONIBLES"
    valores_con_nivel = [e for e in evidencias.values() if e.get("nivel")]
    captura_primaria_ok = bool(captura_cf and captura_cf.get("sha256_respuesta_original"))
    if conflicto:
        estado_global = "CONFLICTO_PENDIENTE_DE_RESOLUCION"
    elif not captura_primaria_ok:
        estado_global = "SIN_SNAPSHOT_DE_FUENTE_PRIMARIA"
    else:
        estado_global = "SOLO_DATOS_RECIBIDOS_TRAZABLES"
    return {
        "estado_global": estado_global,
        "nivel_actual": NIVEL_1 if captura_primaria_ok and valores_con_nivel else None,
        "nivel_2_alcanzado": False,
        "nivel_3_alcanzado": False,
        "requiere_documentacion_contractual": pendientes,
        "requiere_fuente_mercado_independiente": True,
        "cantidad_campos_nivel_1": len(valores_con_nivel),
        "cantidad_campos_nivel_2": 0,
        "cantidad_campos_nivel_3": 0,
        "campos": evidencias,
        "registros_originales_api": {
            "CompararFondos": raw_cf if raw_cf else None,
            "Data912": raw_d9 if raw_d9 else None,
        },
        "regla": "La recepción con hash acredita trazabilidad/integridad del snapshot, no veracidad económica. Nivel 2 exige fuente realmente independiente; nivel 3 exige documento oficial comprobado.",
    }


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


def normalizar_emisor(valor: Any) -> List[str]:
    """Normaliza emisores y elimina sufijos societarios al final."""
    if not valor:
        return []

    texto = unicodedata.normalize("NFKD", str(valor))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.upper()
    texto = re.sub(r"[^A-Z0-9]+", " ", texto)

    tokens = texto.split()

    sufijos = (
        ("SOCIEDAD", "ANONIMA"),
        ("SOCIEDAD", "COMERCIAL"),
        ("S", "A", "C"),
        ("S", "C", "A"),
        ("S", "A", "S"),
        ("S", "R", "L"),
        ("S", "A"),
        ("SA",),
        ("SAC",),
        ("SCA",),
        ("SAS",),
        ("SRL",),
    )

    cambio = True
    while cambio and tokens:
        cambio = False
        for sufijo in sufijos:
            if len(tokens) >= len(sufijo) and tuple(tokens[-len(sufijo):]) == sufijo:
                tokens = tokens[:-len(sufijo)]
                cambio = True
                break

    return tokens

def comparar_contrato(ticker: str, api: Dict[str, Any],
                      maestro: Dict[str, Any]) -> Dict[str, Any]:
    diferencias, pendientes = [], []

    venc_api = normalizar_fecha(api.get("vencimiento"))
    venc_maestro = normalizar_fecha(maestro.get("fecha_vencimiento"))

    if venc_api and venc_maestro and venc_api != venc_maestro:
        diferencias.append(
            f"Vencimiento distinto: API={venc_api}; maestro={venc_maestro}"
        )
    elif not venc_api:
        pendientes.append("La API no informó vencimiento")
    elif not venc_maestro:
        pendientes.append("El maestro no tiene vencimiento válido")

    tokens_api = set(normalizar_emisor(api.get("emisor")))
    tokens_maestro = set(normalizar_emisor(maestro.get("emisor")))

    if not tokens_api:
        pendientes.append("Emisor no informado o no normalizable por la API")
    elif not tokens_maestro:
        pendientes.append("Emisor del maestro no disponible para contrastar")
    elif not tokens_maestro.issubset(tokens_api):
        diferencias.append(
            f"Emisor no coincide: API={api.get('emisor')}; "
            f"maestro={maestro.get('emisor')}"
        )

    validacion_flujos = validar_flujos_api(api.get("flujos"), venc_api)
    if validacion_flujos["estado"] == "DIFIERE_VENCIMIENTO":
        diferencias.append(validacion_flujos["motivo"])
    elif validacion_flujos["valido"] is False:
        pendientes.append(validacion_flujos["motivo"])
    elif validacion_flujos["valido"] is None:
        pendientes.append(validacion_flujos["motivo"])

    # La API no sustituye la documentación contractual oficial.
    pendientes.append(
        "Cupón y cronograma de amortización requieren prospecto/resolución oficial"
    )

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

    # Política 2.2: publicar los indicadores del proveedor como tales,
    # con reservas visibles. Bloquear únicamente si hay flags adversos explícitos
    # o incompatibilidad de pago/negociación. Ausencias generan advertencias,
    # no se convierten silenciosamente en False ni en validación.
    bloqueos_tir = []
    advertencias_indicadores = []
    if tir is None:
        bloqueos_tir.append("TIR no informada por la API")
    if tir_stale is True:
        bloqueos_tir.append("TIR marcada como desactualizada por la API")
    elif tir_stale is None:
        advertencias_indicadores.append("Flag tirStale no informado")
    if tir_moneda_cruzada is True:
        bloqueos_tir.append("TIR marcada como moneda cruzada por la API")
    elif tir_moneda_cruzada is None:
        advertencias_indicadores.append("Flag tirMonedaCruzada no informado")
    if tir_pocas_ops is True:
        advertencias_indicadores.append("API marca pocas operaciones; indicador de liquidez débil")
    elif tir_pocas_ops is None:
        advertencias_indicadores.append("Flag tirPocasOps no informado")
    if tir_es_real is True:
        advertencias_indicadores.append("TIR informada como REAL; mostrar claramente y no confundir con nominal")
    elif tir_es_real is None:
        advertencias_indicadores.append("Flag tirEsReal no informado; naturaleza de la tasa no confirmada")
    if pago_en == "cable" and tc_type == "MEP":
        bloqueos_tir.append("Instrumento paga en cable y cotiza en MEP")
    if not precio_utilizable:
        bloqueos_tir.append("Precio de especie no habilitado; no se publica TIR como indicador vigente")
    if estado_especie != "ESPECIE_Y_LIQUIDACION_COINCIDENTES":
        bloqueos_tir.append("Identidad/especie de mercado no validada")

    tir_publicable_proveedor = tir is not None and not bloqueos_tir

    # Duration solo se publica si la especie y su precio están habilitados.
    # Esto no equivale a una validación contractual ni a un recálculo FIBRX.
    bloqueos_duration = []
    if duration is None:
        bloqueos_duration.append("Duration no informada o inválida")
    elif duration < 0:
        bloqueos_duration.append("Duration negativa")

    if estado_especie != "ESPECIE_Y_LIQUIDACION_COINCIDENTES":
        bloqueos_duration.append("Identidad/especie de mercado no validada")

    if not precio_utilizable:
        bloqueos_duration.append(
            "Precio de especie no habilitado; no se publica duration como vigente"
        )

    duration_publicable_proveedor = (
        duration is not None
        and duration >= 0
        and not bloqueos_duration
    )
    indicadores_utilizables = tir_publicable_proveedor
    razones_tir = bloqueos_tir + advertencias_indicadores

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
        "tir_publicable_proveedor": tir_publicable_proveedor,
        "duration_publicable_proveedor": duration_publicable_proveedor,
        "estado_publicacion_tir": "PUBLICABLE_PROVEEDOR_CON_RESERVAS" if tir_publicable_proveedor else "BLOQUEADA",
        "estado_publicacion_duration": "PUBLICABLE_PROVEEDOR_CON_RESERVAS" if duration_publicable_proveedor else "BLOQUEADA",
        "tipo_tir": "REAL" if tir_es_real is True else "NOMINAL_NO_CONFIRMADA" if tir_es_real is None else "NOMINAL",
        "bloqueos_indicadores": bloqueos_tir,
        "bloqueos_duration": bloqueos_duration,
        "advertencias_indicadores": advertencias_indicadores,
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

    directorio_evidencia_run = str(
        Path(DIRECTORIO_EVIDENCIA) / (ahora_utc().replace(":", "").replace("+", "_").replace("-", ""))
    )
    reporte = {
        "schema_version": 3,
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
        "paquete_evidencia": None,
        "advertencias": [
            "La coincidencia con un agregador no certifica el prospecto.",
            "No se convierten ni mezclan especies O, C y D.",
            "No se utilizan datos de cartera privada.",
            "Data912 no se considera una fuente independiente de CompararFondos.",
            "En especie O se distinguen precioARS (precio de especie) y precio (equivalente USD MEP usado por el agregador para TIR/paridad).",
            "La antigüedad de rueda se informa; no se aplica un umbral temporal arbitrario de bloqueo.",
            "Nivel 1 significa dato recibido con trazabilidad; nivel 2 exige fuente independiente; nivel 3 exige respaldo documental oficial.",
            "El hash SHA-256 acredita integridad del snapshot capturado, no veracidad económica del contenido.",
        ],
    }

    errores = []
    metadata_capturas: Dict[str, Any] = {}
    try:
        payload_cf, metadata_cf = obtener_json_con_evidencia(
            URL_COMPARARFONDOS, "CompararFondos", directorio_evidencia_run
        )
        metadata_capturas["CompararFondos"] = metadata_cf
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
            "captura_evidencia": metadata_cf,
        }
    except Exception as exc:
        indice_cf = {}
        errores.append(f"CompararFondos: {type(exc).__name__}: {exc}")
        reporte["fuentes"]["CompararFondos"] = {
            "estado": "ERROR", "detalle": str(exc), "url": URL_COMPARARFONDOS
        }

    try:
        payload_d9, metadata_d9 = obtener_json_con_evidencia(
            URL_DATA912, "Data912", directorio_evidencia_run
        )
        metadata_capturas["Data912"] = metadata_d9
        indice_d9 = indice_por_ticker(payload_d9, "")
        reporte["fuentes"]["Data912"] = {
            "estado": "OK",
            "url": URL_DATA912,
            "registros_recibidos": len(indice_d9),
            "rol": "Fuente relacionada dentro de la misma cadena; no independiente",
            "captura_evidencia": metadata_d9,
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

        evidencias = construir_evidencias_instrumento(
            ticker, cf, maestro, indice_cf.get(ticker), indice_d9.get(ticker),
            metadata_capturas.get("CompararFondos"), metadata_capturas.get("Data912")
        )
        instrumento = {
            "ticker": ticker,
            "controles_publicacion_individuales": {
                "precio": {"publicable": cf.get("precio_utilizable") is True, "estado": cf.get("estado"), "advertencia_contractual": cf.get("validacion_contractual", {}).get("estado")},
                "tir_proveedor": {"publicable": cf.get("tir_publicable_proveedor") is True, "estado": cf.get("estado_publicacion_tir"), "tipo": cf.get("tipo_tir"), "advertencias": cf.get("advertencias_indicadores", []), "bloqueos": cf.get("bloqueos_indicadores", [])},
                "duration_proveedor": {"publicable": cf.get("duration_publicable_proveedor") is True, "estado": cf.get("estado_publicacion_duration")},
                "validacion_contractual": {"estado": cf.get("validacion_contractual", {}).get("estado"), "documentalmente_validada": False},
            },
            "isin_maestro": maestro.get("isin"),
            "maestro_contractual": maestro,
            "mercado": cf,
            "corroboracion": d9,
            "evidencia": evidencias,
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
    elif any(i["mercado"].get("precio_utilizable")
             for i in reporte["instrumentos"]):
        precios_ok = sum(1 for i in reporte["instrumentos"] if i["mercado"].get("precio_utilizable"))
        total = len(reporte["instrumentos"])
        reporte["estado_general"] = (
            "CONSULTA_COMPLETADA_PUBLICACION_PARCIAL" if precios_ok < total
            else "CONSULTA_COMPLETADA_CON_PRECIOS_DE_MERCADO"
        )
    else:
        reporte["estado_general"] = "CONSULTA_COMPLETADA_SIN_PRECIOS_VALIDOS"

    reporte["errores"] = errores
    reporte["paquete_evidencia"] = crear_paquete_evidencia(
        metadata_capturas, errores, directorio_evidencia_run, ARCHIVO_PAQUETE_EVIDENCIA
    )
    precios_publicables = [i for i in reporte["instrumentos"] if i["mercado"].get("precio_utilizable") is True]
    tir_publicables = [i for i in reporte["instrumentos"] if i["mercado"].get("tir_publicable_proveedor") is True]
    duration_publicables = [i for i in reporte["instrumentos"] if i["mercado"].get("duration_publicable_proveedor") is True]
    reporte["control_publicacion"] = {
        "politica": "PUBLICACION_INDEPENDIENTE_POR_INSTRUMENTO_Y_CAMPO",
        "publicar_precio": bool(precios_publicables),
        "estado_publicacion_precio": "PUBLICACION_PARCIAL" if precios_publicables and len(precios_publicables) < len(reporte["instrumentos"]) else "PUBLICABLE" if precios_publicables else "SIN_PRECIOS_PUBLICABLES",
        "tickers_precio_publicable": [i["ticker"] for i in precios_publicables],
        "tickers_precio_bloqueado": [i["ticker"] for i in reporte["instrumentos"] if not i["mercado"].get("precio_utilizable")],
        "publicar_tir_y_duration": bool(tir_publicables or duration_publicables),
        "estado_publicacion_indicadores": "PUBLICACION_PARCIAL_CON_RESERVAS" if (tir_publicables or duration_publicables) else "SIN_INDICADORES_PUBLICABLES",
        "tickers_tir_publicable_proveedor": [i["ticker"] for i in tir_publicables],
        "tickers_duration_publicable_proveedor": [i["ticker"] for i in duration_publicables],
        "nota": "TIR y duration se publican identificadas como datos de CompararFondos, con fecha y advertencias. No son cálculos independientes de FIBRX ni certificación contractual. Precio se evalúa individualmente; conflictos contractuales no bloquean por sí solos la cotización de mercado.",
        "niveles_evidencia": {
            "nivel_1": "Dato recibido, con origen, fecha de captura y hash del snapshot.",
            "nivel_2": "Dato contrastado con fuente verdaderamente independiente.",
            "nivel_3": "Dato validado contra documentación oficial identificada y revisada.",
            "nivel_2_y_3_habilitados": False,
        },
    }
    return reporte


def guardar_reporte(reporte: Dict[str, Any], ruta: str = ARCHIVO_SALIDA) -> None:
    """
    Escribe en un temporal del mismo directorio y reemplaza el destino
    solo después de validar que el JSON generado sea íntegro y compatible.
    """
    destino = Path(ruta).resolve()
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(destino.parent),
            prefix=f".{destino.name}.",
            suffix=".tmp",
            delete=False,
        ) as archivo:
            temporal = Path(archivo.name)
            json.dump(
                reporte,
                archivo,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            archivo.write("\n")
            archivo.flush()
            os.fsync(archivo.fileno())

        with temporal.open("r", encoding="utf-8") as archivo:
            validado = json.load(archivo)

        if not isinstance(validado, dict) or validado.get("schema_version") != 3:
            raise ValueError("El JSON temporal no conserva schema_version 3")
        if not isinstance(validado.get("instrumentos"), list):
            raise ValueError(
                "El JSON temporal no contiene una lista válida de instrumentos"
            )

        os.replace(temporal, destino)
        temporal = None

    finally:
        if temporal is not None:
            try:
                temporal.unlink(missing_ok=True)
            except OSError:
                pass


def imprimir_resumen(reporte: Dict[str, Any]) -> None:
    print("=" * 76)
    print("FIBRX | ACTUALIZACIÓN DE OBLIGACIONES NEGOCIABLES")
    print("Consulta UTC:", reporte["fecha_consulta_utc"])
    print("Estado:", reporte["estado_general"])
    print("Versión motor:", reporte.get("version_motor"))
    paquete = reporte.get("paquete_evidencia") or {}
    if paquete:
        print("Paquete evidencia:", paquete.get("archivo"))
        print("SHA-256 paquete:", paquete.get("sha256_paquete"))
    print("=" * 76)
    for item in reporte["instrumentos"]:
        m = item["mercado"]
        print(
            f'{item["ticker"]:8} | {m.get("estado","N/D"):42} | '
            f'Precio especie={m.get("precio_especie")} {m.get("moneda_precio_especie")} | '
            f'Precio eq. API={m.get("precio_api_equivalente")} | '
            f'TIR API={m.get("tir_api_pct")} | Ops={m.get("operaciones")}'
        )
        evidencia = item.get("evidencia", {})
        print("  Evidencia:", evidencia.get("estado_global"), "| nivel:", evidencia.get("nivel_actual"))
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
        print(f"Paquete de evidencia: {os.path.abspath(ARCHIVO_PAQUETE_EVIDENCIA)}")
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
