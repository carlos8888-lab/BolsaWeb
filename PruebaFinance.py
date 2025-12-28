# bolsa_basico.py
# Instala primero: pip install yfinance pandas

import yfinance as yf
import pandas as pd
import requests

def descargar_datos2(ticker: str, periodo: str = "1mo", intervalo: str = "1d") -> pd.DataFrame:
    """
    Descarga datos históricos de bolsa para un ticker.
    periodo: "5d", "1mo", "3mo", "6mo", "1y", "5y", "max"
    intervalo: "1m", "5m", "15m", "1h", "1d", "1wk", "1mo"
    """
    df = yf.download(tickers=ticker, period=periodo, interval=intervalo, auto_adjust=False, progress=False)
    if df.empty:
        raise ValueError(f"No se han devuelto datos para '{ticker}'. ¿Ticker correcto?")
    df = df.reset_index()  # para que la fecha sea una columna normal
    return df
def descargar_datos3(ticker, periodo="1mo", intervalo="1d"):
    t = yf.Ticker(ticker)
    df = t.history(period=periodo, interval=intervalo)

    if df.empty:
        print(f"No hay datos para {ticker}")

    return df.reset_index()

def descargar_datos(ticker, periodo="1mo", intervalo="1d"):
    # url = "https://www.bolsasymercados.es/bme-exchange/es/Mercados-y-Cotizaciones/Acciones/Mercado-Continuo/Ficha/Banco-Santander-ES0113900J37"
    # html = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).text
    # print("Longitud HTML:", len(html))
    # print(html[:2200])

    

    # session = requests.Session()
    # session.headers.update({
    #     "User-Agent": "Mozilla/5.0",
    #     "Accept": "application/json",
    #     "Referer": "https://www.bolsasymercados.es/"
    # })

    # # 1️⃣ Petición inicial para obtener cookies
    # session.get("https://www.bolsasymercados.es/bme-exchange/es/", timeout=20)

    # # 2️⃣ Endpoint REAL con parámetros
    # isin = "ES0113900J37"

    # url = "https://www.bolsasymercados.es/bme-exchange/api/v1/trading/securities/price-history"

    # params = {
    #     "isin": isin,
    #     "market": "MC",
    #     "from": "2023-01-01",
    #     "to": "2025-12-31"
    # }

    # r = session.get(url, params=params, timeout=20)

    # # 🔎 DEBUG
    # print("Status:", r.status_code)
    # print("Content-Type:", r.headers.get("Content-Type"))

    # data = r.json()   # ← ahora SÍ
    # CSV diario desde Stooq (sin API key)
    url = "https://stooq.com/q/d/l/?s=san&i=d"   # i=d diario, i=w semanal, i=m mensual
    df = pd.read_csv(url)

    print(df.head())
    df.to_csv("SAN_stooq.csv", index=False)

def main():
    ticker = "SAN.MC" #input("Ticker (ej: AAPL, MSFT, SAN.MC): ").strip().upper()
    periodo = "1d" #input("Periodo [1mo]: ").strip() or "1mo"
    intervalo = "1m" #input("Intervalo [1d]: ").strip() or "1d"

    try:
        df = descargar_datos3(ticker, periodo, intervalo)
        print("\n--- Primeras filas ---")
        print(df.head(10).to_string(index=False))

        # Guardar a CSV
        nombre_csv = f"{ticker}_{periodo}_{intervalo}.csv".replace("/", "-")
        df.to_csv(nombre_csv, index=False)
        print(f"\n✅ Guardado en: {nombre_csv}")

    except Exception as e:
        print(f"\n❌ Error: {e}")

if __name__ == "__main__":
    main()
