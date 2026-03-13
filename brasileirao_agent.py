"""
=============================================================
  AGENTE BRASILEIRAO — KICK MONITOR
  Dashboard visual (browser) + CSV histórico
=============================================================
INSTALACIÓN:
    pip install requests flask flask-cors

EJECUCIÓN:
    python brasileirao_agent.py
    → Abrí http://localhost:5000 en tu browser

El agente:
  - Monitorea cada 60s los 8 canales del Brasileirao en Kick
  - SOLO registra datos cuando detecta un partido en vivo
  - Muestra dashboard en tiempo real en el browser
  - Acumula todo en kick_brasileirao_data.csv
=============================================================
"""

import requests
import threading
import csv
import os
import time
import json
from datetime import datetime
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS

# ─── CONFIGURACIÓN ───────────────────────────────────────────────────────────

CANALES = [
    {"canal": "lacobraaa",         "nombre": "La Cobra",          "pais": "ARG"},
    {"canal": "agusneta",          "nombre": "Agusneta",          "pais": "ARG"},
    {"canal": "benitosdr",         "nombre": "BenitoSDR",         "pais": "ARG"},
    {"canal": "teodeliaa",         "nombre": "Teo D'Elia",        "pais": "ARG"},
    {"canal": "mikemaquinadelmal", "nombre": "Mike Máq. del Mal", "pais": "MEX"},
    {"canal": "werevertumorro",    "nombre": "Werevertumorro",    "pais": "MEX"},
    {"canal": "goat",              "nombre": "Canal GOAT",        "pais": "BRA"},
    {"canal": "pipesierra",        "nombre": "Pipe Sierra",       "pais": "COL"},
]

KEYWORDS_BRA = [
    "brasileir", "brasileirao", "brasileirão",
    "brazilian serie", "serie a brasil",
    "flamengo", "palmeiras", "corinthians", "santos", "sao paulo",
    "vasco", "botafogo", "cruzeiro", "atletico", "gremio", "inter",
    "chapecoense", "sport", "bragantino", "fortaleza", "bahia",
    "ceara", "goias", "cuiaba", "juventude", "athletico", "america",
]

INTERVALO = 60
CSV_FILE = "kick_brasileirao_data.csv"
CSV_COLUMNS = [
    "timestamp", "canal", "nombre", "pais", "estado",
    "viewers_actuales", "peak_sesion", "followers",
    "titulo_stream", "categoria", "duracion_min",
]

KICK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://kick.com/",
}

# ─── ESTADO GLOBAL ───────────────────────────────────────────────────────────

state = {
    "canales": [],            # último snapshot de todos los canales
    "brasileirao_activo": False,
    "ultimo_update": None,
    "historial_viewers": {},  # canal -> lista últimos N valores para sparkline
    "peaks_sesion": {},       # canal -> peak de la sesión actual
    "partidos_detectados": set(),  # títulos únicos detectados como brasileirao
    "total_checks": 0,
    "log": [],                # últimas 20 líneas de log
}

# ─── CSV ─────────────────────────────────────────────────────────────────────

def init_csv():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_COLUMNS).writeheader()
        log_msg(f"CSV creado: {CSV_FILE}")

def save_to_csv(row: dict):
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writerow({k: row.get(k, "") for k in CSV_COLUMNS})

# ─── API KICK ────────────────────────────────────────────────────────────────

def fetch_channel(canal: str) -> dict | None:
    url = f"https://kick.com/api/v2/channels/{canal}"
    try:
        r = requests.get(url, headers=KICK_HEADERS, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def parse_channel(data: dict | None, info: dict) -> dict:
    canal = info["canal"]
    base = {
        "canal": canal,
        "nombre": info["nombre"],
        "pais": info["pais"],
        "estado": "offline",
        "viewers_actuales": 0,
        "peak_sesion": state["peaks_sesion"].get(canal, 0),
        "followers": 0,
        "titulo_stream": "",
        "categoria": "",
        "duracion_min": 0,
        "es_brasileirao": False,
        "timestamp": datetime.now().isoformat(),
    }
    if data is None:
        return base

    base["followers"] = data.get("followersCount") or data.get("followers_count") or 0
    ls = data.get("livestream")
    if not ls:
        if canal in state["peaks_sesion"]:
            del state["peaks_sesion"][canal]
        return base

    base["estado"] = "live"
    base["viewers_actuales"] = ls.get("viewer_count") or 0
    base["titulo_stream"] = ls.get("session_title") or ""
    cats = ls.get("categories") or []
    base["categoria"] = cats[0].get("name") or "" if cats else ""

    # Peak sesión
    prev_peak = state["peaks_sesion"].get(canal, 0)
    if base["viewers_actuales"] > prev_peak:
        state["peaks_sesion"][canal] = base["viewers_actuales"]
    base["peak_sesion"] = state["peaks_sesion"].get(canal, base["viewers_actuales"])

    # Duración
    created = ls.get("created_at") or ls.get("start_time") or ""
    if created:
        try:
            start = datetime.fromisoformat(created.replace("Z", "+00:00"))
            now = datetime.now(start.tzinfo)
            base["duracion_min"] = int((now - start).total_seconds() / 60)
        except Exception:
            pass

    # Detectar Brasileirao
    texto = (base["titulo_stream"] + " " + base["categoria"]).lower()
    base["es_brasileirao"] = any(kw in texto for kw in KEYWORDS_BRA)
    if base["es_brasileirao"] and base["titulo_stream"]:
        state["partidos_detectados"].add(base["titulo_stream"])

    return base

# ─── HISTORIAL SPARKLINE ─────────────────────────────────────────────────────

def update_historial(canal: str, viewers: int):
    if canal not in state["historial_viewers"]:
        state["historial_viewers"][canal] = []
    hist = state["historial_viewers"][canal]
    hist.append(viewers)
    if len(hist) > 30:  # guardar últimos 30 puntos
        hist.pop(0)

# ─── LOG ─────────────────────────────────────────────────────────────────────

def log_msg(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    state["log"].append(line)
    if len(state["log"]) > 20:
        state["log"].pop(0)

# ─── LOOP PRINCIPAL ──────────────────────────────────────────────────────────

def monitor_loop():
    init_csv()
    log_msg("Agente iniciado. Monitoreando canales...")

    while True:
        state["total_checks"] += 1
        snapshot = []
        hay_brasileirao = False

        for info in CANALES:
            raw = fetch_channel(info["canal"])
            row = parse_channel(raw, info)
            snapshot.append(row)

            if row["es_brasileirao"]:
                hay_brasileirao = True
                save_to_csv(row)
                log_msg(
                    f"🇧🇷 {row['nombre']}: {row['viewers_actuales']:,} viewers "
                    f"— {row['titulo_stream'][:40]}"
                )
            elif row["estado"] == "live":
                log_msg(f"📺 {row['nombre']}: live pero no Brasileirao — {row['titulo_stream'][:30]}")

            update_historial(info["canal"], row["viewers_actuales"])
            time.sleep(0.4)

        state["canales"] = snapshot
        state["brasileirao_activo"] = hay_brasileirao
        state["ultimo_update"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        if not hay_brasileirao:
            log_msg("Sin partidos del Brasileirao detectados. Próxima consulta en 60s.")

        time.sleep(INTERVALO)

# ─── FLASK APP ────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)

@app.route("/api/state")
def api_state():
    return jsonify({
        "canales": state["canales"],
        "brasileirao_activo": state["brasileirao_activo"],
        "ultimo_update": state["ultimo_update"],
        "historial": state["historial_viewers"],
        "partidos_detectados": list(state["partidos_detectados"]),
        "total_checks": state["total_checks"],
        "log": state["log"],
        "csv_file": CSV_FILE,
        "csv_rows": sum(1 for _ in open(CSV_FILE)) - 1 if os.path.exists(CSV_FILE) else 0,
    })

@app.route("/")
def dashboard():
    return render_template_string(HTML_DASHBOARD)

# ─── HTML DASHBOARD ──────────────────────────────────────────────────────────

HTML_DASHBOARD = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Brasileirao en Kick — Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --green:   #00c244;
    --green2:  #00ff66;
    --yellow:  #f5c518;
    --red:     #ff3c3c;
    --bg:      #080c0a;
    --bg2:     #0e1510;
    --bg3:     #141c16;
    --border:  #1e2d22;
    --text:    #d4e8d8;
    --muted:   #4a6650;
    --font-display: 'Bebas Neue', sans-serif;
    --font-mono:    'IBM Plex Mono', monospace;
    --font-body:    'IBM Plex Sans', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* SCANLINE effect */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      rgba(0,194,68,0.015) 2px,
      rgba(0,194,68,0.015) 4px
    );
    pointer-events: none;
    z-index: 1000;
  }

  /* HEADER */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 18px 32px;
    border-bottom: 1px solid var(--border);
    background: var(--bg2);
    position: sticky;
    top: 0;
    z-index: 10;
  }
  .logo {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .logo-icon {
    width: 38px; height: 38px;
    background: var(--green);
    border-radius: 6px;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
  }
  .logo h1 {
    font-family: var(--font-display);
    font-size: 26px;
    letter-spacing: 2px;
    color: #fff;
    line-height: 1;
  }
  .logo span {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--green);
    letter-spacing: 3px;
    text-transform: uppercase;
    display: block;
    margin-top: 2px;
  }
  .header-right {
    display: flex;
    align-items: center;
    gap: 20px;
  }
  .status-pill {
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 2px;
    padding: 6px 14px;
    border-radius: 100px;
    text-transform: uppercase;
    transition: all 0.4s;
  }
  .status-pill.active {
    background: rgba(0,194,68,0.15);
    color: var(--green2);
    border: 1px solid var(--green);
    box-shadow: 0 0 12px rgba(0,194,68,0.3);
    animation: pulse-border 2s infinite;
  }
  .status-pill.inactive {
    background: rgba(255,60,60,0.08);
    color: var(--muted);
    border: 1px solid #1e2d22;
  }
  @keyframes pulse-border {
    0%, 100% { box-shadow: 0 0 8px rgba(0,194,68,0.3); }
    50%       { box-shadow: 0 0 20px rgba(0,194,68,0.6); }
  }
  .last-update {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--muted);
  }

  /* MAIN LAYOUT */
  main {
    padding: 28px 32px;
    max-width: 1400px;
    margin: 0 auto;
  }

  /* TOP STATS */
  .top-stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 28px;
  }
  .stat-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 22px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.3s;
  }
  .stat-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 3px; height: 100%;
    background: var(--green);
    opacity: 0.4;
  }
  .stat-card:hover { border-color: var(--green); }
  .stat-label {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 2px;
    color: var(--muted);
    text-transform: uppercase;
    margin-bottom: 8px;
  }
  .stat-value {
    font-family: var(--font-display);
    font-size: 36px;
    letter-spacing: 1px;
    color: #fff;
    line-height: 1;
  }
  .stat-value.green { color: var(--green2); }
  .stat-sub {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--muted);
    margin-top: 6px;
  }

  /* SECTION TITLE */
  .section-title {
    font-family: var(--font-display);
    font-size: 18px;
    letter-spacing: 3px;
    color: var(--green);
    text-transform: uppercase;
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .section-title::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  /* CANALES GRID */
  .canales-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
    margin-bottom: 28px;
  }
  .canal-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    transition: all 0.3s;
    position: relative;
    overflow: hidden;
  }
  .canal-card.live {
    border-color: rgba(0,194,68,0.3);
    background: linear-gradient(135deg, var(--bg2) 0%, #0a140c 100%);
  }
  .canal-card.live-bra {
    border-color: var(--green);
    background: linear-gradient(135deg, #091410 0%, #0a1a0d 100%);
    box-shadow: 0 0 20px rgba(0,194,68,0.1);
  }
  .canal-card:hover { transform: translateY(-2px); }

  .canal-top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 14px;
  }
  .canal-info {}
  .canal-nombre {
    font-family: var(--font-display);
    font-size: 22px;
    letter-spacing: 1px;
    color: #fff;
  }
  .canal-handle {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--muted);
    margin-top: 2px;
  }
  .canal-badge {
    font-family: var(--font-mono);
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 2px;
    padding: 4px 10px;
    border-radius: 100px;
    text-transform: uppercase;
  }
  .badge-offline {
    background: rgba(255,255,255,0.04);
    color: var(--muted);
    border: 1px solid var(--border);
  }
  .badge-live {
    background: rgba(0,194,68,0.1);
    color: var(--green2);
    border: 1px solid rgba(0,194,68,0.4);
  }
  .badge-bra {
    background: rgba(0,255,102,0.15);
    color: #fff;
    border: 1px solid var(--green2);
    box-shadow: 0 0 8px rgba(0,255,102,0.2);
  }

  .canal-viewers {
    font-family: var(--font-display);
    font-size: 44px;
    letter-spacing: 1px;
    color: #fff;
    line-height: 1;
    margin-bottom: 4px;
  }
  .canal-viewers.zero { color: var(--muted); font-size: 32px; }
  .canal-viewers.bra  { color: var(--green2); }

  .canal-meta {
    display: flex;
    gap: 16px;
    margin-top: 8px;
  }
  .meta-item {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--muted);
  }
  .meta-item strong {
    color: var(--text);
    font-size: 11px;
  }

  .canal-titulo {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .canal-titulo.bra { color: var(--green); }

  /* SPARKLINE */
  .sparkline-wrap {
    margin-top: 10px;
    height: 32px;
  }
  canvas.sparkline { width: 100%; height: 32px; }

  /* LIVE INDICATOR */
  .live-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green2);
    display: inline-block;
    margin-right: 6px;
    box-shadow: 0 0 6px var(--green2);
    animation: blink 1.2s infinite;
  }
  @keyframes blink {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.2; }
  }

  /* BOTTOM PANELS */
  .bottom-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
  }

  /* LOG */
  .log-panel {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
  }
  .log-lines {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--muted);
    line-height: 1.8;
    max-height: 220px;
    overflow-y: auto;
  }
  .log-lines .bra-line { color: var(--green2); }
  .log-lines .live-line { color: var(--text); }

  /* PARTIDOS DETECTADOS */
  .partidos-panel {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
  }
  .partido-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text);
  }
  .partido-item:last-child { border-bottom: none; }
  .partido-icon { color: var(--green2); font-size: 14px; }

  /* CSV STATUS */
  .csv-bar {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--muted);
  }
  .csv-bar strong { color: var(--green); }

  /* RESPONSIVE */
  @media (max-width: 900px) {
    .top-stats { grid-template-columns: repeat(2, 1fr); }
    .bottom-grid { grid-template-columns: 1fr; }
    main { padding: 16px; }
  }
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-icon">⚽</div>
    <div>
      <h1>BRASILEIRAO MONITOR</h1>
      <span>Kick Streaming Intelligence</span>
    </div>
  </div>
  <div class="header-right">
    <span class="last-update" id="last-update">—</span>
    <div class="status-pill inactive" id="status-pill">SIN PARTIDO</div>
  </div>
</header>

<main>

  <!-- TOP STATS -->
  <div class="top-stats" id="top-stats">
    <div class="stat-card">
      <div class="stat-label">Total Viewers Ahora</div>
      <div class="stat-value green" id="stat-total">0</div>
      <div class="stat-sub">sumados todos los canales live</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Canales en Vivo</div>
      <div class="stat-value" id="stat-live">0</div>
      <div class="stat-sub">de <span id="stat-total-canales">8</span> monitoreados</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Peak Máximo (sesión)</div>
      <div class="stat-value" id="stat-peak">0</div>
      <div class="stat-sub" id="stat-peak-canal">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Registros CSV</div>
      <div class="stat-value" id="stat-csv">0</div>
      <div class="stat-sub" id="stat-csv-file">solo partidos detectados</div>
    </div>
  </div>

  <!-- CSV STATUS BAR -->
  <div class="csv-bar">
    <span>💾 Guardando en: <strong id="csv-filename">kick_brasileirao_data.csv</strong></span>
    <span><strong id="csv-rows-2">0</strong> filas registradas · Solo cuando hay Brasileirao activo</span>
  </div>

  <!-- CANALES -->
  <div class="section-title">
    <span class="live-dot" id="main-dot" style="opacity:0.3"></span>
    Canales Monitoreados
  </div>
  <div class="canales-grid" id="canales-grid">
    <!-- se rellena por JS -->
  </div>

  <!-- BOTTOM -->
  <div class="bottom-grid">
    <div class="partidos-panel">
      <div class="section-title">🇧🇷 Partidos Detectados</div>
      <div id="partidos-list">
        <div style="font-family:var(--font-mono);font-size:11px;color:var(--muted);padding:12px 0">
          Esperando partidos…
        </div>
      </div>
    </div>
    <div class="log-panel">
      <div class="section-title">📡 Log en Vivo</div>
      <div class="log-lines" id="log-lines"></div>
    </div>
  </div>

</main>

<script>
const fmt = n => Number(n).toLocaleString('es-AR');

function sparkline(canvasEl, data) {
  const ctx = canvasEl.getContext('2d');
  const W = canvasEl.offsetWidth || 280;
  const H = 32;
  canvasEl.width = W;
  canvasEl.height = H;
  ctx.clearRect(0, 0, W, H);
  if (!data || data.length < 2) return;
  const max = Math.max(...data) || 1;
  const step = W / (data.length - 1);
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = i * step;
    const y = H - (v / max) * (H - 4) - 2;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.strokeStyle = 'rgba(0,255,102,0.6)';
  ctx.lineWidth = 1.5;
  ctx.stroke();
  // fill
  ctx.lineTo((data.length - 1) * step, H);
  ctx.lineTo(0, H);
  ctx.closePath();
  ctx.fillStyle = 'rgba(0,194,68,0.07)';
  ctx.fill();
}

async function refresh() {
  let data;
  try {
    const r = await fetch('/api/state');
    data = await r.json();
  } catch(e) {
    document.getElementById('last-update').textContent = 'Error de conexión';
    return;
  }

  // Header
  document.getElementById('last-update').textContent = 'Actualizado: ' + (data.ultimo_update || '—');
  const pill = document.getElementById('status-pill');
  const dot  = document.getElementById('main-dot');
  if (data.brasileirao_activo) {
    pill.className = 'status-pill active';
    pill.textContent = '🔴 PARTIDO EN VIVO';
    dot.style.opacity = '1';
  } else {
    pill.className = 'status-pill inactive';
    pill.textContent = 'SIN PARTIDO';
    dot.style.opacity = '0.3';
  }

  // Stats
  const live = data.canales.filter(c => c.estado === 'live');
  const bra  = data.canales.filter(c => c.es_brasileirao);
  const totalV = live.reduce((s, c) => s + (c.viewers_actuales || 0), 0);
  document.getElementById('stat-total').textContent = fmt(totalV);
  document.getElementById('stat-live').textContent  = live.length;
  document.getElementById('stat-total-canales').textContent = data.canales.length;
  document.getElementById('stat-csv').textContent   = fmt(data.csv_rows);
  document.getElementById('csv-rows-2').textContent = fmt(data.csv_rows);
  document.getElementById('csv-filename').textContent = data.csv_file;

  // Peak máximo
  let maxPeak = 0, maxCanal = '—';
  data.canales.forEach(c => {
    if ((c.peak_sesion || 0) > maxPeak) {
      maxPeak = c.peak_sesion;
      maxCanal = c.nombre;
    }
  });
  document.getElementById('stat-peak').textContent      = fmt(maxPeak);
  document.getElementById('stat-peak-canal').textContent = maxCanal;

  // Canales grid
  const grid = document.getElementById('canales-grid');
  grid.innerHTML = '';
  data.canales.forEach(c => {
    const isBra  = c.es_brasileirao;
    const isLive = c.estado === 'live';
    const hist   = data.historial[c.canal] || [];

    let cardClass = 'canal-card';
    if (isBra)  cardClass += ' live-bra';
    else if (isLive) cardClass += ' live';

    let badgeHTML;
    if (isBra)        badgeHTML = `<span class="canal-badge badge-bra">🇧🇷 Brasileirao</span>`;
    else if (isLive)  badgeHTML = `<span class="canal-badge badge-live"><span class="live-dot"></span>LIVE</span>`;
    else              badgeHTML = `<span class="canal-badge badge-offline">offline</span>`;

    let viewersClass = 'canal-viewers';
    if (!isLive)       viewersClass += ' zero';
    else if (isBra)    viewersClass += ' bra';

    const viewersText = isLive ? fmt(c.viewers_actuales) : '—';
    const tituloClass = isBra ? 'canal-titulo bra' : 'canal-titulo';
    const tituloText  = c.titulo_stream ? c.titulo_stream : (isLive ? c.categoria || 'Sin título' : 'Sin transmisión');

    const card = document.createElement('div');
    card.className = cardClass;
    card.id = 'card-' + c.canal;
    card.innerHTML = `
      <div class="canal-top">
        <div class="canal-info">
          <div class="canal-nombre">${c.nombre} <small style="font-size:14px;color:var(--muted)">${c.pais}</small></div>
          <div class="canal-handle">kick.com/${c.canal}</div>
        </div>
        ${badgeHTML}
      </div>
      <div class="${viewersClass}">${viewersText}</div>
      <div class="canal-meta">
        <div class="meta-item">Peak <strong>${fmt(c.peak_sesion || 0)}</strong></div>
        <div class="meta-item">Duración <strong>${c.duracion_min || 0}min</strong></div>
        <div class="meta-item">Followers <strong>${fmt(c.followers || 0)}</strong></div>
      </div>
      <div class="${tituloClass}" title="${tituloText}">${tituloText}</div>
      <div class="sparkline-wrap"><canvas class="sparkline" id="spark-${c.canal}"></canvas></div>
    `;
    grid.appendChild(card);

    // Dibujar sparkline
    setTimeout(() => {
      const cvs = document.getElementById('spark-' + c.canal);
      if (cvs) sparkline(cvs, hist);
    }, 50);
  });

  // Partidos detectados
  const partidosList = document.getElementById('partidos-list');
  if (data.partidos_detectados.length === 0) {
    partidosList.innerHTML = `<div style="font-family:var(--font-mono);font-size:11px;color:var(--muted);padding:12px 0">Esperando partidos…</div>`;
  } else {
    partidosList.innerHTML = data.partidos_detectados.map(p => `
      <div class="partido-item">
        <span class="partido-icon">⚽</span>
        <span>${p}</span>
      </div>
    `).join('');
  }

  // Log
  const logEl = document.getElementById('log-lines');
  logEl.innerHTML = [...data.log].reverse().map(l => {
    let cls = '';
    if (l.includes('🇧🇷')) cls = 'bra-line';
    else if (l.includes('📺')) cls = 'live-line';
    return `<div class="${cls}">${l}</div>`;
  }).join('');
}

// Refresh cada 15 segundos
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""

# ─── MAIN ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════╗
║   🏆  BRASILEIRAO EN KICK — AGENTE + DASHBOARD              ║
╠══════════════════════════════════════════════════════════════╣
║  1. Instalá dependencias:                                    ║
║     pip install requests flask flask-cors                    ║
║                                                              ║
║  2. Corré el agente:                                         ║
║     python brasileirao_agent.py                              ║
║                                                              ║
║  3. Abrí en tu browser:                                      ║
║     http://localhost:5000                                     ║
╚══════════════════════════════════════════════════════════════╝
""")

    # Hilo de monitoreo en background
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

    # Flask en el hilo principal
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=False, use_reloader=False)
