from __future__ import annotations

import io
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

import matplotlib
matplotlib.use("Agg")  # servidor sin UI
import matplotlib.pyplot as plt
import pandas as pd
import yfinance as yf

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

# =========================
# Configuración general
# =========================

RUTA_DB = "database.db"
RUTA_RESULTADOS = "resultados.json"
RUTA_LAST_CHECK = "last_check.json"
RUTA_MARKET_REFRESH = "market_refresh.json"  # NUEVO: controla TTL de refresh

CLAVE_SESION = os.environ.get("SECRET_KEY", "cambia-esto-por-una-clave-segura")
COMISION_POR_OPERACION = 10.0

# TTL para no bajar datos todo el rato (en Railway te salva los tests)
MARKET_REFRESH_TTL_SECONDS = int(os.environ.get("MARKET_TTL_SECONDS", "900"))  # 15 min por defecto

# Mapeo de periodos (siguiendo tu estructura)
INTERVALO_PERIODO = [
    ["D",   "1d",  "1m"],   # 1 día, 1m
    ["2M",  "2mo", "5m"],   # 2 meses, 5m
    ["2A",  "2y",  "1h"],   # 2 años, 1h
    ["MAX", "max", "1d"],   # toda la vida, 1d
]

TODOS_LOS_DATOS = []


# =========================
# Tipos / utilidades
# =========================

TickerInput = Union[
    str,                       # "SAN"
    Tuple[str, str],           # ("Banco Santander", "SAN")
    Dict[str, str],            # {"empresa": "...", "ticker": "SAN"}
]


@dataclass(frozen=True)
class RegistroTickerUI:
    empresa: str
    ticker: str
    precio_actual: Optional[float]
    acciones: int
    beneficio: Optional[float]
    tp: Optional[float]
    sl: Optional[float]


def ahora_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def existe_archivo(ruta: str) -> bool:
    return Path(ruta).is_file()


def leer_json(ruta: str) -> dict:
    if not existe_archivo(ruta):
        return {}
    with open(ruta, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}


def guardar_json(ruta: str, data: dict) -> None:
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def normalizar_float_texto(s: str) -> Optional[float]:
    s = (s or "").strip().replace(",", ".")
    if s == "":
        return None
    try:
        return float(s)
    except Exception:
        return None


# =========================
# Capa de Base de Datos (sin cambiar tu DB)
# =========================

class RepositorioDB:
    def __init__(self, ruta_db: str):
        self.ruta_db = ruta_db

    def conectar(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.ruta_db)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- user ----
    def db_get_usuario(self, conn: sqlite3.Connection, usuario_id: int) -> Tuple[str, float]:
        cur = conn.cursor()
        cur.execute('SELECT username, saldo FROM user WHERE id = ?', (usuario_id,))
        row = cur.fetchone()
        if not row:
            return ("Desconocido", 0.0)
        nombre = str(row["username"]) if row["username"] is not None else "Desconocido"
        saldo = float(row["saldo"] or 0.0)
        return nombre, saldo

    def db_get_saldo(self, conn: sqlite3.Connection, usuario_id: int) -> float:
        cur = conn.cursor()
        cur.execute('SELECT saldo FROM user WHERE id = ?', (usuario_id,))
        row = cur.fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def db_set_saldo(self, conn: sqlite3.Connection, usuario_id: int, nuevo_saldo: float) -> None:
        cur = conn.cursor()
        cur.execute('UPDATE user SET saldo = ? WHERE id = ?', (float(nuevo_saldo), usuario_id))

    def db_existe_usuario(self, conn: sqlite3.Connection, usuario_id: int) -> bool:
        cur = conn.cursor()
        cur.execute('SELECT id FROM user WHERE id = ?', (usuario_id,))
        return cur.fetchone() is not None

    # ---- tickers ----
    def leer_tickers(self, conn: sqlite3.Connection) -> List[Tuple[str, str]]:
        cur = conn.cursor()
        # cur.execute("SELECT empresa, ticker FROM tickers")
        cur.execute("SELECT empresa, ticker FROM tickers where ticker='san'")
        return [(str(r[0]), str(r[1])) for r in cur.fetchall()]

    # ---- posiciones ----
    def db_get_posiciones(self, conn: sqlite3.Connection) -> Dict[str, int]:
        cur = conn.cursor()
        cur.execute("SELECT symbol, cantidad FROM posiciones")
        return {str(sym): int(cant) for sym, cant in cur.fetchall()}

    def db_set_posicion(self, conn: sqlite3.Connection, ticker: str, cantidad: int) -> None:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO posiciones(symbol, cantidad) VALUES(?, ?)
            ON CONFLICT(symbol) DO UPDATE SET cantidad=excluded.cantidad
            """,
            (ticker, int(cantidad)),
        )

    # ---- compras ----
    def db_insert_compra(self, conn: sqlite3.Connection, usuario_id: int, ticker: str, cantidad: int, precio: float) -> None:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO compras(usuario, ticker, cantidad, "precio compra", "fecha compra")
            VALUES(?, ?, ?, ?, ?)
            """,
            (usuario_id, ticker, int(cantidad), float(precio), ahora_iso()),
        )

    def db_cerrar_compras(
        self,
        conn: sqlite3.Connection,
        usuario_id: int,
        ticker: str,
        cantidad_vender: int,
        precio_venta: float,
        metodo: str = "LIFO",
    ) -> None:
        cantidad_vender = int(cantidad_vender)
        if cantidad_vender <= 0:
            return

        fecha_venta = ahora_iso()
        order = "DESC" if metodo.upper() == "LIFO" else "ASC"
        cur = conn.cursor()

        cur.execute(
            f"""
            SELECT "ID", cantidad, "precio compra", "fecha compra",
                   "precio venta automatico sup", "precio venta automatico inf"
            FROM compras
            WHERE usuario = ? AND ticker = ? AND "fecha venta" IS NULL
            ORDER BY "fecha compra" {order}, "ID" {order}
            """,
            (usuario_id, ticker),
        )

        abiertas = cur.fetchall()
        restante = cantidad_vender

        for row in abiertas:
            if restante <= 0:
                break

            id_ = int(row["ID"])
            qty = int(row["cantidad"])
            p_compra = row["precio compra"]
            f_compra = row["fecha compra"]

            if qty <= restante:
                cur.execute(
                    """
                    UPDATE compras
                    SET "precio venta" = ?, "fecha venta" = ?,
                        "precio venta automatico sup" = NULL,
                        "precio venta automatico inf" = NULL
                    WHERE "ID" = ?
                    """,
                    (float(precio_venta), fecha_venta, id_),
                )
                restante -= qty
            else:
                vendidas = restante
                quedan = qty - vendidas

                cur.execute('UPDATE compras SET cantidad = ? WHERE "ID" = ?', (quedan, id_))

                cur.execute(
                    """
                    INSERT INTO compras(usuario, ticker, cantidad, "precio compra", "fecha compra", "fecha venta", "precio venta")
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        usuario_id,
                        ticker,
                        int(vendidas),
                        float(p_compra) if p_compra is not None else None,
                        f_compra,
                        fecha_venta,
                        float(precio_venta),
                    ),
                )
                restante = 0

        if restante > 0:
            raise ValueError(f"No hay suficientes compras abiertas para vender {cantidad_vender}. Faltan {restante}.")

    def db_coste_medio_posicion(self, conn: sqlite3.Connection, usuario_id: int, ticker: str) -> float:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT cantidad, "precio compra", "fecha compra", "precio venta", "fecha venta"
            FROM compras
            WHERE usuario = ? AND ticker = ?
            """,
            (usuario_id, ticker),
        )
        rows = cur.fetchall()

        movimientos = []
        for r in rows:
            cantidad = int(r["cantidad"])
            p_compra = r["precio compra"]
            f_compra = r["fecha compra"]
            p_venta = r["precio venta"]
            f_venta = r["fecha venta"]

            if p_compra is not None and f_compra:
                movimientos.append(("BUY", str(f_compra), cantidad, float(p_compra)))
            elif p_venta is not None and f_venta:
                movimientos.append(("SELL", str(f_venta), cantidad, float(p_venta)))

        movimientos.sort(key=lambda x: x[1])

        shares = 0
        avg_cost = 0.0

        for tipo, _, qty, price in movimientos:
            if tipo == "BUY":
                total_cost = avg_cost * shares + price * qty + COMISION_POR_OPERACION
                shares += qty
                avg_cost = (total_cost / shares) if shares > 0 else 0.0
            else:
                shares -= qty
                if shares <= 0:
                    shares = 0
                    avg_cost = 0.0

        return float(avg_cost)

    def db_get_auto_venta_compra_activa(self, conn: sqlite3.Connection, usuario_id: int, ticker: str) -> Tuple[Optional[float], Optional[float]]:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT "precio venta automatico sup", "precio venta automatico inf"
            FROM compras
            WHERE usuario = ? AND ticker = ?
              AND "fecha venta" IS NULL
            ORDER BY "fecha compra" DESC
            LIMIT 1
            """,
            (usuario_id, ticker),
        )
        row = cur.fetchone()
        if not row:
            return None, None
        sup = float(row[0]) if row[0] is not None else None
        inf = float(row[1]) if row[1] is not None else None
        return sup, inf

    def db_set_auto_venta_compra_activa(
        self,
        conn: sqlite3.Connection,
        usuario_id: int,
        ticker: str,
        sup: Optional[float],
        inf: Optional[float],
    ) -> None:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE compras
            SET "precio venta automatico sup" = ?,
                "precio venta automatico inf" = ?
            WHERE rowid = (
                SELECT rowid
                FROM compras
                WHERE usuario = ? AND ticker = ?
                  AND "fecha venta" IS NULL
                ORDER BY "fecha compra" DESC
                LIMIT 1
            )
            """,
            (sup, inf, usuario_id, ticker),
        )

    def db_clear_auto_venta_compra_activa(self, conn: sqlite3.Connection, usuario_id: int, ticker: str) -> None:
        self.db_set_auto_venta_compra_activa(conn, usuario_id, ticker, None, None)


# =========================
# Mercado / datos (yfinance + cache + TTL)
# =========================

class ServicioMercado:
    def __init__(self, ruta_resultados: str):
        self.ruta_resultados = ruta_resultados

    def _extraer_tickers(self, cargar_tickers_output: Iterable[TickerInput]) -> List[str]:
        tickers: List[str] = []
        for item in cargar_tickers_output:
            if isinstance(item, str):
                tickers.append(item.strip())
            elif isinstance(item, tuple) and len(item) >= 2:
                tickers.append(str(item[1]).strip())
            elif isinstance(item, dict) and "ticker" in item:
                tickers.append(str(item["ticker"]).strip())
            else:
                raise TypeError(f"Formato no soportado en tickers: {item!r}")

        seen = set()
        out = []
        for t in tickers:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def descargar_datos_tickers(self, tickers_db: List[Tuple[str, str]]) -> Dict[str, Dict[str, pd.DataFrame]]:
        """
        IMPORTANTE (Railway): esto puede fallar por rate limit / red.
        Esta función NO debe romper la app: devolvemos lo que podamos.
        """
        tickers = self._extraer_tickers(tickers_db)
        resultados: Dict[str, Dict[str, pd.DataFrame]] = {}

        for tick in tickers:
            resultados[tick] = {}
            for periodo in INTERVALO_PERIODO:
                clave_periodo = periodo[0]
                yf_period = periodo[1]
                intervalo = periodo[2]

                try:
                    t = yf.Ticker(tick + ".MC")
                    df = t.history(period=yf_period, interval=intervalo)

                    # ✅ FIX: antes estabas haciendo df = None siempre
                    if df is None or df.empty:
                        continue

                    resultados[tick][clave_periodo] = df.sort_index().copy()

                except Exception:
                    # fail-open: si un ticker/periodo falla, seguimos con el resto
                    continue

        return resultados

    def guardar_datos_en_disco(self, resultados: Dict[str, Dict[str, pd.DataFrame]]) -> None:
        salida = {}
        for ticker, data_por_periodo in resultados.items():
            if not isinstance(data_por_periodo, dict) or not data_por_periodo:
                continue
            salida[ticker] = {}
            for periodo, df in data_por_periodo.items():
                if df is None or df.empty:
                    continue
                df_out = df.copy()
                df_out.index.name = "Datetime"
                salida[ticker][periodo] = df_out.reset_index().to_dict(orient="records")

        guardar_json(self.ruta_resultados, salida)

    def cargar_datos_desde_disco(self) -> Dict[str, Dict[str, pd.DataFrame]]:
        global TODOS_LOS_DATOS
        if TODOS_LOS_DATOS == []:
            data = leer_json(self.ruta_resultados)
            resultados: Dict[str, Dict[str, pd.DataFrame]] = {}

            for ticker, data_por_periodo in data.items():
                resultados[ticker] = {}
                for periodo, registros in data_por_periodo.items():
                    df = pd.DataFrame(registros)

                    if "Datetime" not in df.columns:
                        for col in ("Date", "index"):
                            if col in df.columns:
                                df = df.rename(columns={col: "Datetime"})
                                break

                    df["Datetime"] = pd.to_datetime(df["Datetime"], errors="coerce")
                    df = df.set_index("Datetime")
                    resultados[ticker][periodo] = df

            TODOS_LOS_DATOS = resultados

        return TODOS_LOS_DATOS

    def obtener_precio_tiempo_real(self, ticker: str, intervalo: str = "1m") -> Optional[float]:
        """
        Para operar: no debe tirar la app si Yahoo falla.
        """
        try:
            t = yf.Ticker(ticker + ".MC")
            df = t.history(period="1d", interval=intervalo)
            if df is None or df.empty:
                return None
            df = df.sort_index()
            if "Close" in df.columns:
                return float(df["Close"].iloc[-1])
            for col in ("High", "Open", "Low"):
                if col in df.columns:
                    return float(df[col].iloc[-1])
            return None
        except Exception:
            return None

    def obtener_df(self, datos: Dict[str, Dict[str, pd.DataFrame]], ticker: str, periodo: str) -> Optional[pd.DataFrame]:
        data_por_periodo = datos.get(ticker, {})
        if not isinstance(data_por_periodo, dict):
            return None
        return data_por_periodo.get(periodo)


# =========================
# Render de gráficas mini (sin JS)
# =========================

class ServicioGraficas:
    def crear_png_mini_grafica(self, serie: pd.Series, alto_px: int = 70, ancho_px: int = 260) -> bytes:
        dpi = 100
        fig_w = max(1.0, float(ancho_px) / dpi)
        fig_h = max(0.7, float(alto_px) / dpi)

        fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi)
        ax = fig.add_subplot(111)
        ax.set_axis_off()
        fig.subplots_adjust(0, 0, 1, 1)

        try:
            ax.plot(serie.index, serie.values)
        except Exception:
            pass

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, transparent=True)
        plt.close(fig)
        return buf.getvalue()


# =========================
# Helpers TTL refresh (NUEVO)
# =========================

def _leer_market_refresh_ts() -> Optional[float]:
    data = leer_json(RUTA_MARKET_REFRESH)
    try:
        return float(data.get("ts", 0.0)) or None
    except Exception:
        return None


def _guardar_market_refresh_ts(ts: float) -> None:
    guardar_json(RUTA_MARKET_REFRESH, {"ts": ts})


def _ha_expirado_market_cache(ttl_seconds: int) -> bool:
    ts = _leer_market_refresh_ts()
    if not ts:
        return True
    return (time.time() - ts) > ttl_seconds


# =========================
# App Flask
# =========================

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = CLAVE_SESION

    repo = RepositorioDB(RUTA_DB)
    mercado = ServicioMercado(RUTA_RESULTADOS)
    graficas = ServicioGraficas()

    # ✅ Healthcheck rápido (Railway tests)
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    def usuario_id_actual() -> Optional[int]:
        uid = session.get("usuario_id")
        try:
            return int(uid) if uid is not None else None
        except Exception:
            return None

    def requiere_login() -> Optional[Response]:
        if usuario_id_actual() is None:
            return redirect(url_for("login"))
        return None

    def cargar_datos_mercado(conn: sqlite3.Connection) -> Dict[str, Dict[str, pd.DataFrame]]:
        """
        ✅ Estrategia Railway-friendly:
        - Si hay cache en disco y NO ha expirado -> úsala
        - Si expira -> intenta refrescar
        - Si refresco falla -> vuelve a cache y NO rompas la página
        """
        tickers_db = repo.leer_tickers(conn)

        # 1) Si hay cache y está fresca, la devolvemos
        if existe_archivo(RUTA_RESULTADOS) and not _ha_expirado_market_cache(MARKET_REFRESH_TTL_SECONDS):
            datos = mercado.cargar_datos_desde_disco()
            if datos:
                return datos

        # 2) Si hay cache aunque esté vieja, la cargamos como fallback
        fallback = {}
        if existe_archivo(RUTA_RESULTADOS):
            try:
                fallback = mercado.cargar_datos_desde_disco() or {}
            except Exception:
                fallback = {}

        # 3) Intentar refrescar (puede fallar por yfinance)
        try:
            datos_nuevos = mercado.descargar_datos_tickers(tickers_db)
            if datos_nuevos:
                mercado.guardar_datos_en_disco(datos_nuevos)
                _guardar_market_refresh_ts(time.time())
                return datos_nuevos
        except Exception:
            pass

        # 4) Fail-open: si no se pudo refrescar, devolvemos lo que haya
        if fallback:
            return fallback

        # 5) Último recurso: vacío
        return {}

    # =========================
    # Rutas: páginas
    # =========================

    @app.get("/")
    def index():
        uid = usuario_id_actual()
        if uid is None:
            return redirect(url_for("login"))
        return redirect(url_for("valores"))

    @app.get("/app")
    def app_home():
        uid = usuario_id_actual()
        if uid is None:
            return redirect(url_for("login"))
        return redirect(url_for("valores"))

    @app.get("/login")
    def login():
        return render_template("paginas/login.html")

    @app.post("/login")
    def login_post():
        usuario_id_txt = (request.form.get("usuario_id") or "").strip()
        try:
            usuario_id = int(usuario_id_txt)
        except Exception:
            flash("Usuario_id inválido.", "error")
            return redirect(url_for("login"))

        conn = repo.conectar()
        try:
            if not repo.db_existe_usuario(conn, usuario_id):
                flash("No existe ese usuario_id en la BBDD.", "error")
                return redirect(url_for("login"))
        finally:
            conn.close()

        session["usuario_id"] = usuario_id
        return redirect(url_for("valores"))

    @app.get("/logout")
    def logout():
        session.pop("usuario_id", None)
        return redirect(url_for("login"))

    @app.get("/valores")
    def valores():
        resp = requiere_login()
        if resp:
            return resp

        periodo = (request.args.get("periodo") or "MAX").upper().strip()
        periodos_validos = [p[0] for p in INTERVALO_PERIODO]
        if periodo not in periodos_validos:
            periodo = "MAX"

        refresco_seg = request.args.get("refresco", "900").strip()
        try:
            refresco_seg_i = max(30, int(refresco_seg))
        except Exception:
            refresco_seg_i = 900

        uid = usuario_id_actual()
        assert uid is not None

        conn = repo.conectar()
        try:
            tickers_db = repo.leer_tickers(conn)
            empresas = {t: e for e, t in tickers_db}

            datos = cargar_datos_mercado(conn)

            portfolio = repo.db_get_posiciones(conn)
            usuario_nombre, cash = repo.db_get_usuario(conn, uid)

            registros: List[RegistroTickerUI] = []
            for ticker in sorted(empresas.keys()):
                empresa = empresas.get(ticker, "Desconocida")
                acciones = int(portfolio.get(ticker, 0))

                df = mercado.obtener_df(datos, ticker, periodo)
                precio_actual = None
                if df is not None and not df.empty and "Close" in df.columns:
                    df = df.sort_index()
                    precio_actual = float(df["Close"].iloc[-1])

                beneficio = None
                if precio_actual is not None and acciones > 0:
                    coste_medio = repo.db_coste_medio_posicion(conn, uid, ticker)
                    beneficio = (precio_actual - coste_medio) * acciones

                # (TP/SL siguen igual)
                tp, sl = repo.db_get_auto_venta_compra_activa(conn, uid, ticker)

                registros.append(
                    RegistroTickerUI(
                        empresa=empresa,
                        ticker=ticker,
                        precio_actual=precio_actual,
                        acciones=acciones,
                        beneficio=beneficio,
                        tp=tp,
                        sl=sl,
                    )
                )

            return render_template(
                "paginas/valores.html",
                usuario_nombre=usuario_nombre,
                saldo=cash,
                periodo=periodo,
                periodos=periodos_validos,
                registros=registros,
                refresco_seg=refresco_seg_i,
            )
        finally:
            conn.close()

    @app.get("/clasificacion")
    def clasificacion():
        resp = requiere_login()
        if resp:
            return resp
        return render_template("paginas/clasificacion.html")

    @app.get("/instrucciones")
    def instrucciones():
        resp = requiere_login()
        if resp:
            return resp
        return render_template("paginas/instrucciones.html")

    @app.get("/creditos")
    def creditos():
        resp = requiere_login()
        if resp:
            return resp
        return render_template("paginas/creditos.html")

    # =========================
    # Rutas: operaciones (POST)
    # =========================

    def _obtener_precio_operacion() -> Optional[float]:
        ticker = (request.form.get("ticker") or "").strip().upper()
        if not ticker:
            return None
        return mercado.obtener_precio_tiempo_real(ticker, intervalo="1m")

    @app.post("/operar/comprar")
    def operar_comprar():
        resp = requiere_login()
        if resp:
            return resp

        ticker = (request.form.get("ticker") or "").strip().upper()
        periodo = (request.form.get("periodo") or "MAX").strip().upper()
        cantidad_txt = (request.form.get("cantidad") or "1").strip()

        try:
            cantidad = max(1, int(cantidad_txt))
        except Exception:
            flash("Cantidad inválida.", "error")
            return redirect(url_for("valores", periodo=periodo))

        uid = usuario_id_actual()
        assert uid is not None

        precio = _obtener_precio_operacion()
        if precio is None:
            flash("No se pudo obtener precio en tiempo real (Yahoo). Prueba más tarde.", "error")
            return redirect(url_for("valores", periodo=periodo))

        conn = repo.conectar()
        try:
            cash = repo.db_get_saldo(conn, uid)
            coste = precio * cantidad + COMISION_POR_OPERACION
            if cash < coste:
                flash("Saldo insuficiente para comprar (incluye comisión).", "error")
                return redirect(url_for("valores", periodo=periodo))

            portfolio = repo.db_get_posiciones(conn)
            acciones_actuales = int(portfolio.get(ticker, 0))
            acciones_nuevas = acciones_actuales + cantidad

            repo.db_set_saldo(conn, uid, cash - coste)
            repo.db_set_posicion(conn, ticker, acciones_nuevas)
            repo.db_insert_compra(conn, uid, ticker, cantidad, precio)
            conn.commit()

            flash(f"Compra OK: {ticker} x{cantidad} a {precio:.3f} (comisión {COMISION_POR_OPERACION:.2f} €).", "ok")
            return redirect(url_for("valores", periodo=periodo))
        finally:
            conn.close()

    @app.post("/operar/vender")
    def operar_vender():
        resp = requiere_login()
        if resp:
            return resp

        ticker = (request.form.get("ticker") or "").strip().upper()
        periodo = (request.form.get("periodo") or "MAX").strip().upper()
        cantidad_txt = (request.form.get("cantidad") or "1").strip()

        try:
            cantidad = max(1, int(cantidad_txt))
        except Exception:
            flash("Cantidad inválida.", "error")
            return redirect(url_for("valores", periodo=periodo))

        uid = usuario_id_actual()
        assert uid is not None

        precio = _obtener_precio_operacion()
        if precio is None:
            flash("No se pudo obtener precio en tiempo real (Yahoo). Prueba más tarde.", "error")
            return redirect(url_for("valores", periodo=periodo))

        conn = repo.conectar()
        try:
            portfolio = repo.db_get_posiciones(conn)
            acciones_actuales = int(portfolio.get(ticker, 0))
            if acciones_actuales < cantidad:
                flash("No tienes suficientes acciones para vender.", "error")
                return redirect(url_for("valores", periodo=periodo))

            cash = repo.db_get_saldo(conn, uid)
            ingreso = precio * cantidad - COMISION_POR_OPERACION
            if ingreso < 0:
                flash("Ingreso negativo tras comisión (revisa cantidad).", "error")
                return redirect(url_for("valores", periodo=periodo))

            acciones_nuevas = acciones_actuales - cantidad
            repo.db_set_saldo(conn, uid, cash + ingreso)
            repo.db_set_posicion(conn, ticker, acciones_nuevas)

            repo.db_cerrar_compras(conn, uid, ticker, cantidad, precio)
            conn.commit()

            flash(f"Venta OK: {ticker} x{cantidad} a {precio:.3f} (comisión {COMISION_POR_OPERACION:.2f} €).", "ok")
            return redirect(url_for("valores", periodo=periodo))
        finally:
            conn.close()

    @app.post("/operar/guardar_auto")
    def operar_guardar_auto():
        resp = requiere_login()
        if resp:
            return resp

        ticker = (request.form.get("ticker") or "").strip().upper()
        periodo = (request.form.get("periodo") or "MAX").strip().upper()
        tp = normalizar_float_texto(request.form.get("tp") or "")
        sl = normalizar_float_texto(request.form.get("sl") or "")

        uid = usuario_id_actual()
        assert uid is not None

        conn = repo.conectar()
        try:
            repo.db_set_auto_venta_compra_activa(conn, uid, ticker, tp, sl)
            conn.commit()
            flash(
                f"Auto-venta actualizada para {ticker} (TP={tp if tp is not None else '--'} / SL={sl if sl is not None else '--'}).",
                "ok",
            )
            return redirect(url_for("valores", periodo=periodo))
        finally:
            conn.close()

    @app.post("/operar/eliminar_auto")
    def operar_eliminar_auto():
        resp = requiere_login()
        if resp:
            return resp

        ticker = (request.form.get("ticker") or "").strip().upper()
        periodo = (request.form.get("periodo") or "MAX").strip().upper()

        uid = usuario_id_actual()
        assert uid is not None

        conn = repo.conectar()
        try:
            repo.db_clear_auto_venta_compra_activa(conn, uid, ticker)
            conn.commit()
            flash(f"Auto-venta eliminada para {ticker}.", "ok")
            return redirect(url_for("valores", periodo=periodo))
        finally:
            conn.close()

    # =========================
    # Rutas: imágenes (mini-gráficas)
    # =========================

    @app.get("/mini_grafica")
    def mini_grafica():
        resp = requiere_login()
        if resp:
            return resp

        ticker = (request.args.get("ticker") or "").strip().upper()
        periodo = (request.args.get("periodo") or "MAX").strip().upper()
        puntos_txt = (request.args.get("puntos") or "60").strip()

        try:
            puntos = max(20, int(puntos_txt))
        except Exception:
            puntos = 60

        if not ticker:
            abort(400)

        conn = repo.conectar()
        try:
            datos = cargar_datos_mercado(conn)
            df = mercado.obtener_df(datos, ticker, periodo)
            if df is None or df.empty or "Close" not in df.columns:
                serie = pd.Series([0, 0, 0], index=pd.date_range(end=datetime.now(), periods=3))
                png = graficas.crear_png_mini_grafica(serie)
                return Response(png, mimetype="image/png")

            df = df.sort_index()
            serie = df["Close"].tail(puntos)
            png = graficas.crear_png_mini_grafica(serie)
            return Response(png, mimetype="image/png")
        finally:
            conn.close()

    return app


app = create_app()

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=puerto, debug=False)
