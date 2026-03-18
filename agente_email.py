"""
Agente de Email Financeiro
Lê emails do Gmail de remetentes financeiros e interpreta com Claude.
Mantém uma base de conhecimento financeiro com histórico de transações.
"""

import imaplib
imaplib._MAXLINE = 300_000_000  # aumenta limite para 300MB
import email
import os
import json
import time
import logging
import schedule
from datetime import datetime, timedelta
from email.header import decode_header
import anthropic
from config import (
    GMAIL_USER,
    GMAIL_APP_PASSWORD,
    ANTHROPIC_API_KEY,
    MODEL,
    MAX_TOKENS,
    INTERVALO_MINUTOS,
    DIAS_ATRAS,
    ARQUIVO_SAIDA,
    ARQUIVO_BASE,
    LOG_FILE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)
ARQUIVO_IDS_PROCESSADOS = ".emails_processados.json"
ARQUIVO_ESTADO = ".agente_estado.json"

# Remetentes/assuntos financeiros a monitorar
FILTROS_FINANCEIROS = {
    "nubank": ["nubank.com.br", "nubank"],
    "nomad": ["nomad.com", "nomad"],
    "deel": ["deel.com", "deel"],
    "ontop": ["ontop.ai", "ontop"],
}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ─── IDs processados ────────────────────────────────────────────────────────

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


def carregar_ultima_execucao() -> str:
    """Retorna a data da última execução no formato IMAP (dd-Mon-yyyy)."""
    if os.path.exists(ARQUIVO_ESTADO):
        with open(ARQUIVO_ESTADO, "r") as f:
            estado = json.load(f)
            return estado.get("ultima_execucao")
    return None


def salvar_ultima_execucao():
    with open(ARQUIVO_ESTADO, "w") as f:
        json.dump({"ultima_execucao": datetime.now().strftime("%d-%b-%Y")}, f)


# ─── Base de conhecimento financeiro ────────────────────────────────────────

def carregar_base() -> list:
    if os.path.exists(ARQUIVO_BASE):
        with open(ARQUIVO_BASE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def salvar_na_base(transacao: dict):
    base = carregar_base()
    base.append(transacao)
    with open(ARQUIVO_BASE, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False, indent=2)


def resumo_base_para_contexto() -> str:
    """Gera um resumo das últimas transações para enviar como contexto ao Claude."""
    base = carregar_base()
    if not base:
        return "Nenhuma transação registrada ainda."

    linhas = []
    for t in base[-30:]:  # últimas 30 transações
        linha = (
            f"- [{t.get('data', '?')}] {t.get('empresa', '?').upper()} | "
            f"{t.get('tipo', '?')} | "
            f"{t.get('valor', 'sem valor')} {t.get('moeda', '')} | "
            f"De: {t.get('origem', '?')} → Para: {t.get('destino', '?')} | "
            f"Categoria: {t.get('categoria', '?')} | "
            f"{t.get('descricao', '')}"
        )
        linhas.append(linha)

    return "\n".join(linhas)


# ─── Email helpers ───────────────────────────────────────────────────────────

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
    import re
    corpo = ""
    if msg.is_multipart():
        for parte in msg.walk():
            content_type = parte.get_content_type()
            if content_type == "text/plain":
                corpo = parte.get_payload(decode=True).decode("utf-8", errors="ignore")
                break
            elif content_type == "text/html" and not corpo:
                html = parte.get_payload(decode=True).decode("utf-8", errors="ignore")
                corpo = re.sub(r"<[^>]+>", " ", html)
                corpo = re.sub(r"\s+", " ", corpo).strip()
    else:
        corpo = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

    return corpo[:8000]


def identificar_remetente(remetente: str, assunto: str) -> str | None:
    remetente_lower = remetente.lower()
    assunto_lower = assunto.lower()
    for empresa, palavras_chave in FILTROS_FINANCEIROS.items():
        for palavra in palavras_chave:
            if palavra in remetente_lower or palavra in assunto_lower:
                return empresa
    return None


# ─── Claude ─────────────────────────────────────────────────────────────────

def interpretar_com_claude(remetente: str, assunto: str, corpo: str, empresa: str, data_email: str) -> dict:
    """
    Usa Claude para interpretar o email e retornar um dict estruturado.
    Também recebe o histórico da base para cruzar informações.
    """
    contexto_historico = resumo_base_para_contexto()

    prompt = f"""Você é um assistente financeiro pessoal inteligente. Analise o email abaixo e extraia informações financeiras detalhadas.

━━━ HISTÓRICO DE TRANSAÇÕES ANTERIORES ━━━
{contexto_historico}

━━━ EMAIL ATUAL ━━━
Empresa: {empresa.upper()}
De: {remetente}
Assunto: {assunto}
Data: {data_email}

Corpo:
{corpo}

━━━ INSTRUÇÕES ━━━
Com base no email e no histórico acima, responda em JSON com exatamente estes campos:

{{
  "tipo": "entrada | saída | extrato | fatura | notificação | transferência",
  "valor": "valor numérico como string, ex: '1500.00' ou null se não houver",
  "moeda": "BRL | USD | EUR ou null",
  "origem": "de onde veio o dinheiro (empresa, pessoa, conta)",
  "destino": "para onde foi o dinheiro (empresa, pessoa, conta)",
  "categoria": "salário | freelance | transferência | pagamento | fatura | cashback | investimento | outro",
  "descricao": "resumo claro em 1-2 frases do que aconteceu",
  "observacoes": "cruzamentos com histórico anterior, padrões identificados, ou observações relevantes",
  "resumo_markdown": "texto formatado em markdown para o relatório, com todos os detalhes importantes"
}}

Responda SOMENTE o JSON, sem texto adicional."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    texto = response.content[0].text.strip()

    # Remove blocos de código markdown se presentes
    if texto.startswith("```"):
        texto = texto.split("```")[1]
        if texto.startswith("json"):
            texto = texto[4:]

    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        # Fallback se Claude não retornar JSON válido
        dados = {
            "tipo": "desconhecido",
            "valor": None,
            "moeda": None,
            "origem": empresa,
            "destino": "desconhecido",
            "categoria": "outro",
            "descricao": texto[:200],
            "observacoes": "",
            "resumo_markdown": texto,
        }

    return dados


# ─── Persistência ────────────────────────────────────────────────────────────

def salvar_no_arquivo(empresa: str, remetente: str, assunto: str, data: str, dados: dict):
    """Adiciona o resultado ao arquivo markdown de saída."""
    valor_str = f"**Valor:** {dados.get('valor', 'N/A')} {dados.get('moeda', '')}".strip()
    entrada = f"""
---

## {empresa.upper()} — {data}

**De:** {remetente}
**Assunto:** {assunto}
**Tipo:** {dados.get('tipo', '?')} | **Categoria:** {dados.get('categoria', '?')}
{valor_str}
**Origem:** {dados.get('origem', '?')} → **Destino:** {dados.get('destino', '?')}

{dados.get('resumo_markdown', dados.get('descricao', ''))}

> **Observações:** {dados.get('observacoes', 'Nenhuma.')}

"""
    with open(ARQUIVO_SAIDA, "a", encoding="utf-8") as f:
        f.write(entrada)

    log.info("Salvo no arquivo: [%s] %s", empresa.upper(), assunto[:50])


# ─── Gmail ───────────────────────────────────────────────────────────────────

def conectar_gmail():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    return mail


def buscar_emails_financeiros():
    ids_processados = carregar_ids_processados()
    emails_encontrados = 0
    emails_processados = 0

    log.info("--- Iniciando verificação de emails ---")

    try:
        log.info("Conectando ao Gmail...")
        mail = conectar_gmail()
        mail.select("inbox")
        log.info("Conectado com sucesso")

        ultima = carregar_ultima_execucao()
        if ultima:
            desde = ultima
            log.info("Buscando emails desde última execução: %s", desde)
        else:
            desde = (datetime.now() - timedelta(days=DIAS_ATRAS)).strftime("%d-%b-%Y")
            log.info("Primeira execução — buscando últimos %d dias", DIAS_ATRAS)

        _, mensagens = mail.search(None, f'SINCE "{desde}"')
        todos_ids = mensagens[0].split()
        log.info("%d emails encontrados desde %s", len(todos_ids), desde)

        for email_id in reversed(todos_ids):
            email_id_str = email_id.decode()

            if email_id_str in ids_processados:
                continue

            _, dados_raw = mail.fetch(email_id, "(RFC822)")
            msg = email.message_from_bytes(dados_raw[0][1])

            remetente = decodificar_header(msg.get("From", ""))
            assunto = decodificar_header(msg.get("Subject", ""))
            data_email = msg.get("Date", datetime.now().strftime("%a, %d %b %Y %H:%M:%S"))

            empresa = identificar_remetente(remetente, assunto)

            if empresa:
                emails_encontrados += 1
                log.info("Email financeiro encontrado: [%s] %s", empresa.upper(), assunto[:60])

                corpo = extrair_corpo(msg)
                log.info("Corpo extraído (%d caracteres). Enviando para Claude...", len(corpo))

                dados = interpretar_com_claude(remetente, assunto, corpo, empresa, data_email)
                log.info(
                    "Interpretado: %s | %s %s | %s → %s",
                    dados.get("tipo"), dados.get("valor"), dados.get("moeda"),
                    dados.get("origem"), dados.get("destino"),
                )

                # Salva na base de conhecimento
                transacao = {
                    "id_email": email_id_str,
                    "data": data_email,
                    "empresa": empresa,
                    "remetente": remetente,
                    "assunto": assunto,
                    **dados,
                }
                salvar_na_base(transacao)

                salvar_no_arquivo(empresa, remetente, assunto, data_email, dados)
                salvar_id_processado(email_id_str)
                emails_processados += 1

                time.sleep(1)

        salvar_ultima_execucao()
        mail.logout()
        log.info("Desconectado do Gmail")

    except Exception as e:
        log.error("Erro durante a verificação: %s", e, exc_info=True)

    if emails_encontrados == 0:
        log.info("Nenhum email financeiro novo encontrado")
    log.info("--- Verificação concluída: %d email(s) processado(s) ---", emails_processados)


# ─── Init ────────────────────────────────────────────────────────────────────

def inicializar_arquivo():
    if not os.path.exists(ARQUIVO_SAIDA):
        with open(ARQUIVO_SAIDA, "w", encoding="utf-8") as f:
            f.write("# Resumo Financeiro por Email\n")
            f.write("Gerado automaticamente pelo Agente de Email Financeiro\n")
            f.write(f"Iniciado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n")
        log.info("Arquivo de saída criado: %s", ARQUIVO_SAIDA)


def main():
    log.info("========================================")
    log.info("  Agente de Email Financeiro iniciado")
    log.info("========================================")
    log.info("Monitorando: %s", ", ".join(FILTROS_FINANCEIROS.keys()).upper())
    log.info("Intervalo: a cada %d minutos", INTERVALO_MINUTOS)
    log.info("Arquivo de saída: %s", ARQUIVO_SAIDA)
    log.info("Base de conhecimento: %s", ARQUIVO_BASE)
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
