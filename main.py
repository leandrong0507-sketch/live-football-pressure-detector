import os
import json
import logging
from datetime import datetime, timezone
import httpx
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# Credenciais
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Parâmetros
COOLDOWN_MINUTOS = 45
DELTA_CHUTES_ESCANTEIOS = 4
DELTA_ATAQUES_PERIGOSOS = 15

LIGAS_MONITORADAS = {
    'Premier League': 'England',
    'Championship': 'England',
    'La Liga': 'Spain',
    'Bundesliga': 'Germany',
    'Serie A': 'Italy',
    'Ligue 1': 'France',
    'Eredivisie': 'Netherlands',
    'Primeira Liga': 'Portugal',
    'Brasileiro Série A': 'Brazil',
    'Brasileiro Série B': 'Brazil',
    'Copa Libertadores': 'South America',
    'Copa Sudamericana': 'South America',
    'UEFA Champions League': 'Europe',
    'UEFA Europa League': 'Europe',
    'UEFA Conference League': 'Europe',
    'Liga Profesional Argentina': 'Argentina',
    'Primera A': 'Colombia',
    'Liga MX': 'Mexico',
    'Major League Soccer': 'USA',
    'J1 League': 'Japan',
    'Saudi Pro League': 'Saudi Arabia',
    'Super Lig': 'Turkey',
    'Pro League': 'Belgium',
    'Premiership': 'Scotland',
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


def enviar_alerta_telegram(mensagem: str):
    """Envia alerta formatado para o seu Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Credenciais do Telegram ausentes.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown"
    }
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                logging.error(f"Erro Telegram: {resp.text}")
    except Exception as e:
        logging.error(f"Falha de rede no Telegram: {e}")


def executar_monitoramento():
    logging.info("Iniciando varredura de pressao ao vivo...")
    headers = {"User-Agent": "Mozilla/5.0"}
    agora_iso = datetime.now(timezone.utc).isoformat()

    with httpx.Client(headers=headers, timeout=20) as client:
        try:
            res = client.get("https://m2.sokkerpro.com/livescores")
            if res.status_code != 200:
                logging.error(f"Erro SokkerPro: {res.status_code}")
                return
            data = res.json()
        except Exception as e:
            logging.error(f"Falha de conexao: {e}")
            return

        for liga_group in data.get("categories", []):
            nome_liga = liga_group.get("name")
            if nome_liga not in LIGAS_MONITORADAS:
                continue

            for jogo in liga_group.get("matches", []):
                try:
                    minuto = int(jogo.get("minute") or 0)
                except ValueError:
                    continue

                if minuto < 50 or minuto > 85:
                    continue

                jogo_id = str(jogo.get("id"))
                home_name = jogo.get("home_name", "Casa")
                away_name = jogo.get("away_name", "Fora")
                home_score = jogo.get("home_score", 0)
                away_score = jogo.get("away_score", 0)
                placar_atual = f"{home_score}-{away_score}"

                home_stats = jogo.get("stats", {}).get("home", {})
                away_stats = jogo.get("stats", {}).get("away", {})

                chutes_totais = (home_stats.get("shots_total") or 0) + (away_stats.get("shots_total") or 0)
                escanteios = (home_stats.get("corners") or 0) + (away_stats.get("corners") or 0)
                ataques_perigosos = (home_stats.get("dangerous_attacks") or 0) + (away_stats.get("dangerous_attacks") or 0)

                # Buscar snapshot anterior
                snap_ant = None
                if supabase:
                    res_snap = supabase.table("SnapshotJogo").select("*").eq("jogo_id", jogo_id).limit(1).execute()
                    if res_snap.data:
                        snap_ant = res_snap.data[0]

                disparar = False
                motivo = ""

                if snap_ant:
                    dt_chutes_esc = (chutes_totais + escanteios) - (
                        (snap_ant.get("chutes_totais") or 0) + (snap_ant.get("escanteios") or 0)
                    )
                    dt_ataques = ataques_perigosos - (snap_ant.get("ataques_perigosos") or 0)

                    alerta_em = snap_ant.get("alerta_em")
                    diff_min = 999
                    if alerta_em:
                        ultimo_dt = datetime.fromisoformat(alerta_em)
                        diff_min = (datetime.now(timezone.utc) - ultimo_dt).total_seconds() / 60

                    if diff_min >= COOLDOWN_MINUTOS:
                        if dt_chutes_esc >= DELTA_CHUTES_ESCANTEIOS:
                            disparar = True
                            motivo = f"+{dt_chutes_esc} chutes/cantos em ~15min"
                        elif dt_ataques >= DELTA_ATAQUES_PERIGOSOS:
                            disparar = True
                            motivo = f"+{dt_ataques} ataques perigosos em ~15min"

                # Disparar alerta
                odd_bet365 = None
                if disparar:
                    try:
                        fix_res = client.get(f"https://m2.sokkerpro.com/fixture/{jogo_id}")
                        if fix_res.status_code == 200:
                            fix_data = fix_res.json()
                            raw_odd = fix_data.get("odds", {}).get("bet365", {}).get("over_05_2h")
                            if raw_odd:
                                parts = str(raw_odd).split("#")
                                if len(parts) > 1 and parts[1] == "0":
                                    odd_bet365 = float(parts[0])
                    except Exception as e:
                        logging.warning(f"Erro ao buscar odd do jogo {jogo_id}: {e}")

                    if supabase:
                        supabase.table("RegistroAposta").insert({
                            "data": agora_iso,
                            "jogo_id": jogo_id,
                            "liga": nome_liga,
                            "time_casa": home_name,
                            "time_fora": away_name,
                            "minuto_alerta": minuto,
                            "placar_no_alerta": placar_atual,
                            "mercado": "Over Gols 2T",
                            "odds_no_momento": odd_bet365,
                            "resultado": "pendente",
                            "tipo": "automatico",
                            "motivo": motivo,
                        }).execute()

                    msg = (
                        f"🔥 *ALERTA DE PRESSAO DETECTADA* 🔥\n\n"
                        f"⚽ *Jogo:* {home_name} {placar_atual} {away_name}\n"
                        f"🏆 *Liga:* {nome_liga}\n"
                        f"⏱ *Minuto:* {minuto}'\n"
                        f"📊 *Motivo:* {motivo}\n"
                        f"💰 *Odd Bet365:* `{odd_bet365 if odd_bet365 else 'Indisponivel'}`\n"
                        f"🎯 *Mercado:* Gol ate o fim"
                    )
                    enviar_alerta_telegram(msg)
                    logging.info(f"Alerta enviado: {home_name} x {away_name}")

                # Salvar Snapshot via UPSERT
                if supabase:
                    supabase.table("SnapshotJogo").upsert({
                        "jogo_id": jogo_id,
                        "liga": nome_liga,
                        "time_casa": home_name,
                        "time_fora": away_name,
                        "placar": placar_atual,
                        "minuto": minuto,
                        "chutes_totais": chutes_totais,
                        "escanteios": escanteios,
                        "ataques_perigosos": ataques_perigosos,
                        "alerta_em": agora_iso if disparar else (snap_ant.get("alerta_em") if snap_ant else None)
                    }, on_conflict="jogo_id").execute()

    logging.info("Varredura concluida com sucesso.")


if __name__ == "__main__":
    executar_monitoramento()
