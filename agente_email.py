"""
Agente de Email Financeiro
Lê emails do Gmail de remetentes financeiros e interpreta com Claude.
"""

import imaplib
import email
import os
import json
import time
import logging
import schedule
from datetime import datetime
from email.header import decode_header
from dotenv import load_dotenv
import anthropic

load_dotenv()

# Configuração de logging
LOG_FILE = os.getenv("LOG_FILE", "agente.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),  # também exibe no terminal
    ],
)
log = logging.getLogger(__name__)

# Configurações
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
INTERVALO_MINUTOS = int(os.getenv("INTERVALO_MINUTOS", "15"))
ARQUIVO_SAIDA = os.getenv("ARQUIVO_SAIDA", "financeiro.md")
ARQUIVO_IDS_PROCESSADOS = ".emails_processados.json"

# Remetentes/assuntos financeiros a monitorar
FILTROS_FINANCEIROS = {
    "nubank": ["nubank.com.br", "nubank"],
    "nomad": ["nomad.com", "nomad"],
    "deel": ["deel.com", "deel"],
    "ontop": ["ontop.ai", "ontop"],
}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def carregar_ids_processados():
    if os.path.exists(ARQUIVO_IDS_PROCESSADOS):
        with open(ARQUIVO_IDS_PROCESSADOS, "r") as f:
            return set(json.load(f))
    return set()


def salvar_id_processado(email_id: str):
    ids = carregar_ids_processados()
    ids.add(email_id)
    with open(ARQUIVO_IDS_PROCESSADOS, "w") as f:
        json.dump(list(ids), f)


def decodificar_header(valor):
    if not valor:
        return ""
    partes = decode_header(valor)
    resultado = []
    for parte, encoding in partes:
        if isinstance(parte, bytes):
            resultado.append(parte.decode(encoding or "utf-8", errors="ignore"))
        else:
            resultado.append(parte)
    return " ".join(resultado)


def extrair_corpo(msg):
    """Extrai o texto do email (prefere texto puro, fallback para HTML)."""
    corpo = ""
    if msg.is_multipart():
        for parte in msg.walk():
            content_type = parte.get_content_type()
            if content_type == "text/plain":
                corpo = parte.get_payload(decode=True).decode("utf-8", errors="ignore")
                break
            elif content_type == "text/html" and not corpo:
                html = parte.get_payload(decode=True).decode("utf-8", errors="ignore")
                # Remove tags HTML básicas
                import re
                corpo = re.sub(r"<[^>]+>", " ", html)
                corpo = re.sub(r"\s+", " ", corpo).strip()
    else:
        corpo = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

    return corpo[:8000]  # Limita tamanho para não estourar contexto


def identificar_remetente(remetente: str, assunto: str) -> str | None:
    """Verifica se o email é de um remetente financeiro monitorado."""
    remetente_lower = remetente.lower()
    assunto_lower = assunto.lower()
    for empresa, palavras_chave in FILTROS_FINANCEIROS.items():
        for palavra in palavras_chave:
            if palavra in remetente_lower or palavra in assunto_lower:
                return empresa
    return None


def interpretar_com_claude(remetente: str, assunto: str, corpo: str, empresa: str) -> str:
    """Usa Claude para interpretar o email financeiro."""
    prompt = f"""Você é um assistente financeiro pessoal. Analise o email abaixo e extraia as informações relevantes de forma clara e concisa em português.

Empresa identificada: {empresa.upper()}
De: {remetente}
Assunto: {assunto}

Corpo do email:
{corpo}

Por favor, forneça:
1. Tipo de comunicação (extrato, fatura, recebimento, notificação, etc.)
2. Valores envolvidos (se houver)
3. Data de referência (se houver)
4. Resumo em 2-3 linhas do que esse email significa

Seja direto e objetivo. Se não houver informações financeiras relevantes, diga brevemente o que é.
"""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def salvar_no_arquivo(empresa: str, remetente: str, assunto: str, data: str, interpretacao: str):
    """Adiciona o resultado ao arquivo de saída."""
    entrada = f"""
---

## {empresa.upper()} — {data}

**De:** {remetente}
**Assunto:** {assunto}

{interpretacao}

"""
    with open(ARQUIVO_SAIDA, "a", encoding="utf-8") as f:
        f.write(entrada)

    log.info("Salvo no arquivo: [%s] %s", empresa.upper(), assunto[:50])


def conectar_gmail():
    """Conecta ao Gmail via IMAP."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    return mail


def buscar_emails_financeiros():
    """Busca e processa emails financeiros não processados."""
    ids_processados = carregar_ids_processados()
    emails_encontrados = 0
    emails_processados = 0

    log.info("--- Iniciando verificação de emails ---")

    try:
        log.info("Conectando ao Gmail...")
        mail = conectar_gmail()
        mail.select("inbox")
        log.info("Conectado com sucesso")

        _, mensagens = mail.search(None, "ALL")
        todos_ids = mensagens[0].split()
        log.info("%d emails na caixa de entrada (verificando os 200 mais recentes)", len(todos_ids))

        for email_id in reversed(todos_ids[-200:]):
            email_id_str = email_id.decode()

            if email_id_str in ids_processados:
                continue

            _, dados = mail.fetch(email_id, "(RFC822)")
            msg = email.message_from_bytes(dados[0][1])

            remetente = decodificar_header(msg.get("From", ""))
            assunto = decodificar_header(msg.get("Subject", ""))
            data_email = msg.get("Date", datetime.now().strftime("%a, %d %b %Y %H:%M:%S"))

            empresa = identificar_remetente(remetente, assunto)

            if empresa:
                emails_encontrados += 1
                log.info("Email financeiro encontrado: [%s] %s", empresa.upper(), assunto[:60])

                log.info("Extraindo corpo do email...")
                corpo = extrair_corpo(msg)
                log.info("Corpo extraído (%d caracteres). Enviando para Claude...", len(corpo))

                interpretacao = interpretar_com_claude(remetente, assunto, corpo, empresa)
                log.info("Claude interpretou o email com sucesso")

                salvar_no_arquivo(empresa, remetente, assunto, data_email, interpretacao)
                salvar_id_processado(email_id_str)
                emails_processados += 1

                time.sleep(1)

        mail.logout()
        log.info("Desconectado do Gmail")

    except Exception as e:
        log.error("Erro durante a verificação: %s", e, exc_info=True)

    if emails_encontrados == 0:
        log.info("Nenhum email financeiro novo encontrado")
    log.info("--- Verificação concluída: %d email(s) processado(s) ---", emails_processados)


def inicializar_arquivo():
    """Cria o cabeçalho do arquivo se não existir."""
    if not os.path.exists(ARQUIVO_SAIDA):
        with open(ARQUIVO_SAIDA, "w", encoding="utf-8") as f:
            f.write(f"# Resumo Financeiro por Email\n")
            f.write(f"Gerado automaticamente pelo Agente de Email Financeiro\n")
            f.write(f"Iniciado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n")
        log.info("Arquivo de saída criado: %s", ARQUIVO_SAIDA)


def main():
    log.info("========================================")
    log.info("  Agente de Email Financeiro iniciado")
    log.info("========================================")
    log.info("Monitorando: %s", ", ".join(FILTROS_FINANCEIROS.keys()).upper())
    log.info("Intervalo: a cada %d minutos", INTERVALO_MINUTOS)
    log.info("Arquivo de saída: %s", ARQUIVO_SAIDA)
    log.info("Log salvo em: %s", LOG_FILE)
    log.info("========================================")

    inicializar_arquivo()

    buscar_emails_financeiros()

    schedule.every(INTERVALO_MINUTOS).minutes.do(buscar_emails_financeiros)

    log.info("Agente rodando... (Ctrl+C para parar)")
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
