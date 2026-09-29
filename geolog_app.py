"""
GeoLog - Plataforma de Telemetria Logística e Persistência Poliglota
UNIPÊ | Tópicos Avançados em Banco de Dados / Arquitetura de Software

Persistência poliglota:
  - SQLite  (logitech.db)              -> dados cadastrais/transacionais (motoristas, veículos)
  - MongoDB (geolog_db.telemetria)     -> telemetria IoT com GeoJSON + índice 2dsphere

Como executar:
  pip install streamlit pymongo folium streamlit-folium plotly pandas
  # MongoDB rodando (ex.: docker run -d -p 27017:27017 mongo:7)
  streamlit run geolog_app.py

Variável opcional: MONGO_URI (padrão: mongodb://localhost:27017)
"""

import math
import os
import random
import sqlite3
from datetime import datetime, timezone

import folium
import pandas as pd
import plotly.express as px
import streamlit as st
from pymongo import GEOSPHERE, MongoClient
from pymongo.errors import PyMongoError
from streamlit_folium import st_folium

# ----------------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = "geolog_db"
MONGO_COL = "telemetria"
SQLITE_PATH = "logitech.db"
LIMITE_VELOCIDADE = 80  # km/h

# Pontos de apoio (referência para a busca por raio) - região de João Pessoa/PB
PONTOS_APOIO = {
    "João Pessoa (Centro)": (-7.115, -34.873),
    "Cabo Branco": (-7.121, -34.832),
    "Tibiri / BR-230": (-7.150, -34.950),
    "Personalizado": None,
}

# ----------------------------------------------------------------------------
# Seed (dados de teste)
# ----------------------------------------------------------------------------
MOTORISTAS = [
    (1, "Carlos Andrade", "123456789", "Ativo"),
    (2, "Mariana Silva", "987654321", "Ativo"),
    (3, "Roberto Souza", "456789123", "Em Descanso"),
]

VEICULOS = [
    (101, "ABC-1A23", "Volvo FH 540", 1),
    (102, "XYZ-9876", "Scania R450", 2),
    (103, "KGB-4567", "Mercedes Actros", 3),
]

TELEMETRIA_SEED = [
    {
        "veiculo_id": 101,
        "location": {"type": "Point", "coordinates": [-34.873, -7.115]},  # Centro
        "temperatura": 4.2,
        "velocidade": 65,
        "timestamp": "2026-09-11T10:00:00Z",
    },
    {
        "veiculo_id": 102,
        "location": {"type": "Point", "coordinates": [-34.832, -7.121]},  # Cabo Branco
        "temperatura": -18.5,
        "velocidade": 85,
        "timestamp": "2026-09-11T10:05:00Z",
    },
    {
        "veiculo_id": 103,
        "location": {"type": "Point", "coordinates": [-34.950, -7.150]},  # Tibiri / BR-230
        "temperatura": 22.0,
        "velocidade": 0,
        "timestamp": "2026-09-11T09:45:00Z",
    },
]


# ----------------------------------------------------------------------------
# MÓDULO 1 - Camada de persistência poliglota e carga inicial
# ----------------------------------------------------------------------------
def get_sqlite() -> sqlite3.Connection:
    """Nova conexão SQLite por chamada (sqlite3 não é thread-safe entre reruns)."""
    conn = sqlite3.connect(SQLITE_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_sqlite() -> None:
    """Cria o esquema relacional e faz o seed (idempotente)."""
    with get_sqlite() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS motoristas (
                id     INTEGER PRIMARY KEY,
                nome   TEXT NOT NULL,
                cnh    TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS veiculos (
                id           INTEGER PRIMARY KEY,
                placa        TEXT NOT NULL UNIQUE,
                modelo       TEXT NOT NULL,
                motorista_id INTEGER NOT NULL,
                FOREIGN KEY (motorista_id) REFERENCES motoristas(id)
            );
            """
        )
        conn.executemany("INSERT OR IGNORE INTO motoristas VALUES (?,?,?,?)", MOTORISTAS)
        conn.executemany("INSERT OR IGNORE INTO veiculos VALUES (?,?,?,?)", VEICULOS)


@st.cache_resource(show_spinner="Conectando ao MongoDB...")
def get_mongo_client() -> MongoClient:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")  # falha cedo se o servidor estiver indisponível
    return client


def _parse_ts(valor: str) -> datetime:
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


def init_mongo(client: MongoClient):
    """Cria o índice 2dsphere (obrigatório) e popula a coleção se estiver vazia."""
    col = client[MONGO_DB][MONGO_COL]
    col.create_index([("location", GEOSPHERE)], name="location_2dsphere")
    col.create_index([("veiculo_id", 1), ("timestamp", -1)], name="veiculo_ts")
    if col.count_documents({}) == 0:
        docs = [{**d, "timestamp": _parse_ts(d["timestamp"])} for d in TELEMETRIA_SEED]
        col.insert_many(docs)
    return col


def resetar_telemetria(col) -> None:
    col.delete_many({})
    col.insert_many([{**d, "timestamp": _parse_ts(d["timestamp"])} for d in TELEMETRIA_SEED])


# ----------------------------------------------------------------------------
# Consultas auxiliares
# ----------------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ultimas_telemetrias(col) -> list[dict]:
    """Último registro de cada veículo (pipeline de agregação)."""
    pipeline = [
        {"$sort": {"timestamp": -1}},
        {"$group": {"_id": "$veiculo_id", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
    ]
    return list(col.aggregate(pipeline))


def buscar_por_raio(col, lat: float, lon: float, raio_km: float) -> list[dict]:
    """
    Veículos (última posição) a até `raio_km` do ponto de referência.
    Usa $near sobre o índice 2dsphere; o resultado já vem ordenado por distância.
    """
    ids_ultimos = [d["_id"] for d in ultimas_telemetrias(col)]
    cursor = col.find(
        {
            "_id": {"$in": ids_ultimos},
            "location": {
                "$near": {
                    "$geometry": {"type": "Point", "coordinates": [lon, lat]},  # GeoJSON: [lon, lat]
                    "$maxDistance": raio_km * 1000,  # metros
                }
            },
        }
    )
    resultado = []
    for d in cursor:
        lon_v, lat_v = d["location"]["coordinates"]
        d["distancia_km"] = haversine_km(lat, lon, lat_v, lon_v)
        resultado.append(d)
    return resultado


def carregar_cadastro() -> pd.DataFrame:
    """Join relacional no SQLite: veículos x motoristas."""
    with get_sqlite() as conn:
        return pd.read_sql_query(
            """
            SELECT v.id AS veiculo_id, v.placa, v.modelo,
                   m.id AS motorista_id, m.nome AS motorista, m.status
            FROM veiculos v
            JOIN motoristas m ON m.id = v.motorista_id
            ORDER BY v.id
            """,
            conn,
        )


def visao_unificada(col) -> pd.DataFrame:
    """Join poliglota em memória: SQLite (cadastro) + MongoDB (última telemetria)."""
    cadastro = carregar_cadastro()
    ult = ultimas_telemetrias(col)
    linhas = [
        {
            "veiculo_id": d["veiculo_id"],
            "temperatura": d["temperatura"],
            "velocidade": d["velocidade"],
            "latitude": d["location"]["coordinates"][1],
            "longitude": d["location"]["coordinates"][0],
            "timestamp": d["timestamp"],
        }
        for d in ult
    ]
    telemetria = pd.DataFrame(
        linhas,
        columns=["veiculo_id", "temperatura", "velocidade", "latitude", "longitude", "timestamp"],
    )
    return cadastro.merge(telemetria, on="veiculo_id", how="left")


def historico_temperatura(col) -> pd.DataFrame:
    docs = col.find({}, {"veiculo_id": 1, "temperatura": 1, "velocidade": 1, "timestamp": 1}).sort(
        "timestamp", 1
    )
    df = pd.DataFrame(list(docs))
    if df.empty:
        return df
    cadastro = carregar_cadastro()[["veiculo_id", "placa"]]
    return df.merge(cadastro, on="veiculo_id", how="left")


def trilha_veiculo(col, veiculo_id: int) -> list[list[float]]:
    docs = col.find({"veiculo_id": veiculo_id}, {"location": 1}).sort("timestamp", 1)
    return [[d["location"]["coordinates"][1], d["location"]["coordinates"][0]] for d in docs]


# ----------------------------------------------------------------------------
# Bônus - Simulador de movimentação
# ----------------------------------------------------------------------------
def simular_movimentacao(col) -> int:
    """Insere um novo ponto GPS (pequena variação aleatória) para cada veículo."""
    novos = []
    for d in ultimas_telemetrias(col):
        lon, lat = d["location"]["coordinates"]
        novos.append(
            {
                "veiculo_id": d["veiculo_id"],
                "location": {
                    "type": "Point",
                    "coordinates": [
                        round(lon + random.uniform(-0.004, 0.004), 6),
                        round(lat + random.uniform(-0.004, 0.004), 6),
                    ],
                },
                "temperatura": round(d["temperatura"] + random.uniform(-1.0, 1.0), 1),
                "velocidade": max(0, min(120, int(d["velocidade"] + random.randint(-20, 25)))),
                "timestamp": datetime.now(timezone.utc),
            }
        )
    if novos:
        col.insert_many(novos)
    return len(novos)


# ----------------------------------------------------------------------------
# Componentes de UI
# ----------------------------------------------------------------------------
def zoom_para_raio(raio_km: float) -> int:
    if raio_km <= 3:
        return 13
    if raio_km <= 8:
        return 12
    if raio_km <= 20:
        return 11
    if raio_km <= 50:
        return 10
    return 9


def render_mapa(col, lat, lon, raio_km, mostrar_trilha: bool) -> list[dict]:
    dentro = buscar_por_raio(col, lat, lon, raio_km)
    ids_dentro = {d["veiculo_id"] for d in dentro}
    cadastro = carregar_cadastro().set_index("veiculo_id")

    m = folium.Map(location=[lat, lon], zoom_start=zoom_para_raio(raio_km), tiles="OpenStreetMap")

    folium.Circle(
        location=[lat, lon],
        radius=raio_km * 1000,
        color="#1f77b4",
        fill=True,
        fill_opacity=0.10,
        tooltip=f"Raio de busca: {raio_km:.1f} km",
    ).add_to(m)
    folium.Marker(
        [lat, lon],
        tooltip="Ponto de referência",
        icon=folium.Icon(color="blue", icon="home", prefix="fa"),
    ).add_to(m)

    for d in ultimas_telemetrias(col):
        vid = d["veiculo_id"]
        lon_v, lat_v = d["location"]["coordinates"]
        info = cadastro.loc[vid] if vid in cadastro.index else None
        placa = info["placa"] if info is not None else str(vid)
        motorista = info["motorista"] if info is not None else "-"
        alerta = d["velocidade"] > LIMITE_VELOCIDADE
        aviso = "<br><b style='color:red'>⚠ Excesso de velocidade</b>" if alerta else ""
        popup = (
            f"<b>{placa}</b><br>Motorista: {motorista}<br>"
            f"Temp.: {d['temperatura']} °C<br>Velocidade: {d['velocidade']} km/h{aviso}"
        )
        if vid in ids_dentro:
            folium.Marker(
                [lat_v, lon_v],
                popup=folium.Popup(popup, max_width=250),
                tooltip=f"{placa} - {d['velocidade']} km/h",
                icon=folium.Icon(color="red" if alerta else "green", icon="truck", prefix="fa"),
            ).add_to(m)
            if mostrar_trilha:
                pontos = trilha_veiculo(col, vid)
                if len(pontos) > 1:
                    folium.PolyLine(pontos, color="#ff7f0e", weight=3, opacity=0.8).add_to(m)
        else:
            folium.CircleMarker(
                [lat_v, lon_v],
                radius=6,
                color="gray",
                fill=True,
                tooltip=f"{placa} (fora do raio)",
            ).add_to(m)

    st_folium(m, height=480, use_container_width=True, returned_objects=[], key="mapa_raio")
    return dentro


def modulo_geo(col, lat, lon, raio_km, mostrar_trilha):
    st.subheader("Geoprocessamento — busca por raio (MongoDB 2dsphere)")
    st.caption("Consulta `$near` com `$maxDistance` sobre a última posição de cada veículo.")
    dentro = render_mapa(col, lat, lon, raio_km, mostrar_trilha)

    if not dentro:
        st.info("Nenhum veículo dentro do raio selecionado. Aumente o raio na barra lateral.")
        return

    cadastro = carregar_cadastro().set_index("veiculo_id")
    linhas = []
    for d in dentro:
        vid = d["veiculo_id"]
        linhas.append(
            {
                "Placa": cadastro.loc[vid, "placa"] if vid in cadastro.index else vid,
                "Motorista": cadastro.loc[vid, "motorista"] if vid in cadastro.index else "-",
                "Distância (km)": round(d["distancia_km"], 2),
                "Velocidade (km/h)": d["velocidade"],
                "Temperatura (°C)": d["temperatura"],
            }
        )
    st.markdown(f"**{len(dentro)} veículo(s) a até {raio_km:.1f} km** (ordenados por proximidade):")
    st.dataframe(pd.DataFrame(linhas), use_container_width=True, hide_index=True)


def modulo_unificado(col):
    st.subheader("Visão unificada — join poliglota em memória")
    st.caption("SQLite (motorista, placa) ⨝ MongoDB (última telemetria) via `veiculo_id`.")
    df = visao_unificada(col)
    exibir = pd.DataFrame(
        {
            "Nome do Motorista": df["motorista"],
            "Placa": df["placa"],
            "Última Temperatura (°C)": df["temperatura"],
            "Velocidade (km/h)": df["velocidade"],
            "Coordenadas Atualizadas": df.apply(
                lambda r: f"{r['latitude']:.5f}, {r['longitude']:.5f}"
                if pd.notna(r["latitude"])
                else "sem telemetria",
                axis=1,
            ),
            "Última Leitura": df["timestamp"],
        }
    )

    def destaca_alerta(linha):
        vel = linha["Velocidade (km/h)"]
        if pd.notna(vel) and vel > LIMITE_VELOCIDADE:
            return ["background-color: #ffd6d6"] * len(linha)
        return [""] * len(linha)

    st.dataframe(exibir.style.apply(destaca_alerta, axis=1), use_container_width=True, hide_index=True)
    st.caption(f"Linhas em vermelho: velocidade acima de {LIMITE_VELOCIDADE} km/h.")


def modulo_dashboard(col):
    st.subheader("Dashboard analítico")
    df = visao_unificada(col)

    ativos = int((df["status"] == "Ativo").sum())
    media_temp = df["temperatura"].mean()
    alertas = int((df["velocidade"] > LIMITE_VELOCIDADE).sum())

    k1, k2, k3 = st.columns(3)
    k1.metric("Frotas ativas", f"{ativos} / {len(df)}")
    k2.metric("Temperatura média da carga", f"{media_temp:.1f} °C" if pd.notna(media_temp) else "-")
    k3.metric(f"Alertas de velocidade (> {LIMITE_VELOCIDADE} km/h)", alertas)

    g1, g2 = st.columns([3, 2])
    with g1:
        hist = historico_temperatura(col)
        if hist.empty:
            st.info("Sem histórico de telemetria.")
        else:
            fig = px.line(
                hist,
                x="timestamp",
                y="temperatura",
                color="placa",
                markers=True,
                title="Histórico de temperatura por veículo",
                labels={"timestamp": "Horário", "temperatura": "Temperatura (°C)", "placa": "Veículo"},
            )
            st.plotly_chart(fig, use_container_width=True)
            if hist.groupby("veiculo_id").size().max() < 2:
                st.caption("Dica: use **Simular Movimentação** na barra lateral para gerar histórico.")
    with g2:
        status = carregar_cadastro().groupby("status").size().reset_index(name="quantidade")
        fig2 = px.pie(
            status,
            names="status",
            values="quantidade",
            hole=0.4,
            title="Status dos motoristas",
        )
        st.plotly_chart(fig2, use_container_width=True)


# ----------------------------------------------------------------------------
# Aplicação
# ----------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="GeoLog", page_icon="🚚", layout="wide")
    st.title("🚚 GeoLog — Telemetria Logística e Persistência Poliglota")
    st.caption("LogiTech Express · SQLite (cadastros) + MongoDB (telemetria geoespacial)")

    # --- Inicialização das duas camadas de persistência ---
    try:
        init_sqlite()
        client = get_mongo_client()
        col = init_mongo(client)
    except PyMongoError as e:
        st.error(
            f"Não foi possível conectar ao MongoDB em `{MONGO_URI}`. "
            "Inicie o servidor (ex.: `docker run -d -p 27017:27017 mongo:7`) "
            "ou defina a variável de ambiente MONGO_URI."
        )
        st.exception(e)
        st.stop()

    # --- Sidebar: filtros e ações ---
    with st.sidebar:
        st.header("Filtro geoespacial")
        nome_ponto = st.selectbox("Ponto de apoio", list(PONTOS_APOIO.keys()))
        if PONTOS_APOIO[nome_ponto] is None:
            lat = st.number_input("Latitude", value=-7.115, format="%.5f", min_value=-90.0, max_value=90.0)
            lon = st.number_input("Longitude", value=-34.873, format="%.5f", min_value=-180.0, max_value=180.0)
        else:
            lat, lon = PONTOS_APOIO[nome_ponto]
            st.caption(f"Lat {lat:.3f} · Lon {lon:.3f}")
        raio_km = st.slider("Raio de busca (km)", 1.0, 50.0, 10.0, 0.5)
        mostrar_trilha = st.checkbox("Mostrar trilha (rota percorrida)", value=True)

        st.divider()
        st.header("Simulador de telemetria")
        if st.button("▶ Simular Movimentação", use_container_width=True, type="primary"):
            n = simular_movimentacao(col)
            st.success(f"{n} novos pontos GPS inseridos no MongoDB.")
        if st.button("↺ Resetar telemetria", use_container_width=True):
            resetar_telemetria(col)
            st.info("Telemetria restaurada para o seed inicial.")

        st.divider()
        total = col.count_documents({})
        st.caption(f"MongoDB: `{MONGO_DB}.{MONGO_COL}` — {total} documentos")
        st.caption(f"Índices: {', '.join(col.index_information().keys())}")

    # --- Módulos ---
    tab_mapa, tab_join, tab_dash = st.tabs(["🗺️ Mapa e Raio", "🔗 Visão Unificada", "📊 Dashboard"])
    with tab_mapa:
        modulo_geo(col, lat, lon, raio_km, mostrar_trilha)
    with tab_join:
        modulo_unificado(col)
    with tab_dash:
        modulo_dashboard(col)


if __name__ == "__main__":
    main()
