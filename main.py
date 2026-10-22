"""
API del monitor de salud para astronautas.
Recibe lecturas del ESP32, las guarda en Supabase y evalúa el estado de salud.
"""
import hmac
import os
import statistics

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
API_KEY = os.environ["API_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Astro Health Monitor API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Lectura(BaseModel):
    device_id: str = Field(min_length=1, max_length=50)
    temp_c: float = Field(ge=0, le=60)
    bpm: int = Field(ge=20, le=250)
    spo2: float = Field(ge=50, le=100)


def verificar_clave(x_api_key: str | None):
    if not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="API key inválida")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/health")
def salud():
    return {"status": "ok", "servicio": "astro-health-monitor"}


@app.post("/api/lecturas", status_code=201)
def guardar_lectura(lectura: Lectura, x_api_key: str | None = Header(default=None)):
    verificar_clave(x_api_key)
    res = supabase.table("lecturas").insert(lectura.model_dump()).execute()
    return {"guardado": True, "data": res.data}


@app.get("/api/lecturas")
def listar_lecturas(device_id: str | None = None, limit: int = Query(50, ge=1, le=500)):
    q = supabase.table("lecturas").select("*").order("created_at", desc=True).limit(limit)
    if device_id:
        q = q.eq("device_id", device_id)
    return q.execute().data


def alertas_por_reglas(temp_c: float, bpm: int, spo2: float) -> list[dict]:
    alertas = []
    if temp_c >= 38:
        alertas.append({"nivel": "alto", "msg": f"Fiebre ({temp_c:.1f} °C)"})
    elif temp_c < 35:
        alertas.append({"nivel": "alto", "msg": f"Hipotermia ({temp_c:.1f} °C)"})
    if bpm > 100:
        alertas.append({"nivel": "medio", "msg": f"Taquicardia ({bpm} BPM)"})
    elif bpm < 60:
        alertas.append({"nivel": "medio", "msg": f"Bradicardia ({bpm} BPM)"})
    if spo2 < 90:
        alertas.append({"nivel": "alto", "msg": f"Hipoxia severa (SpO2 {spo2:.1f} %)"})
    elif spo2 < 95:
        alertas.append({"nivel": "medio", "msg": f"SpO2 baja ({spo2:.1f} %)"})
    return alertas


def anomalias_estadisticas(historial: list[dict], actual: dict) -> list[dict]:
    anomalias = []
    if len(historial) < 10:
        return anomalias
    for campo, etiqueta in (("temp_c", "Temperatura"), ("bpm", "Pulso"), ("spo2", "SpO2")):
        valores = [h[campo] for h in historial]
        media = statistics.mean(valores)
        desv = statistics.pstdev(valores)
        if desv == 0:
            continue
        z = (actual[campo] - media) / desv
        if abs(z) >= 2.5:
            anomalias.append({
                "campo": campo,
                "msg": f"{etiqueta} atípica respecto a su línea base (z={z:.1f})",
            })
    return anomalias


@app.get("/api/estado/{device_id}")
def estado(device_id: str):
    filas = (
        supabase.table("lecturas")
        .select("*")
        .eq("device_id", device_id)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
        .data
    )
    if not filas:
        raise HTTPException(status_code=404, detail="Sin lecturas para este dispositivo")

    actual, historial = filas[0], filas[1:]
    alertas = alertas_por_reglas(actual["temp_c"], actual["bpm"], actual["spo2"])
    anomalias = anomalias_estadisticas(historial, actual)

    if any(a["nivel"] == "alto" for a in alertas):
        nivel = "critico"
    elif alertas or anomalias:
        nivel = "atencion"
    else:
        nivel = "normal"

    return {"device_id": device_id, "nivel": nivel, "actual": actual,
            "alertas": alertas, "anomalias": anomalias}
