"""
Agente de Email Financeiro
Lê emails do Gmail de remetentes financeiros e interpreta com Claude.
Mantém uma base de conhecimento financeiro com histórico de transações.
"""

import imaplib
imaplib._MAXLINE = 300_000_000  # aumenta limite para 300MB
import csv
import email
import hashlib
import io
import os
import json
import re
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
ARQUIVO_PAYCHECKS = ".paychecks_pendentes.json"

# Valor fixo do paycheck por empresa (USD)
VALOR_PAYCHECK = {
    "ontop": "3200.00",
    "deel": "2000.00",
}

# Remetentes/assuntos financeiros a monitorar
FILTROS_FINANCEIROS = {
    "nubank": ["nubank.com.br", "nubank"],
    "deel": ["deel.com", "deel"],
    "ontop": ["ontop.ai", "ontop"],
}

# Padrões de assunto para IGNORAR (não enviar ao Claude)
# Mesmo que venham de remetentes financeiros conhecidos
ASSUNTOS_IGNORAR = [
    # Marketing / promoção
    "promoç", "promoc", "oferta", "desconto", "black friday", "cyber monday",
    "cashback especial", "ganhe mais", "aproveite", "exclusivo para você",
    "não perca", "nao perca", "últimas horas", "ultimas horas",
    # Newsletter / informativo genérico
    "newsletter", "novidades", "atualização do app", "atualizacao do app",
    "nova funcionalidade", "conheça", "conheca", "lançamento", "lancamento",
    "blog", "dica", "dicas",
    # Confirmação / segurança / conta
    "confirme seu email", "confirme seu e-mail", "verifique seu email",
    "valide seu", "ative sua conta", "bem-vindo", "bem vindo", "boas-vindas",
    "bem-vinda", "cadastro realizado", "senha alterada", "senha redefinida",
    "login realizado", "acesso à sua conta",
    # Pesquisa / avaliação
    "pesquisa de satisfação", "avalie", "nos dê sua opinião", "feedback",
    "nps", "como foi sua experiência",
    # Votação / assembleia
    "votação", "votacao", "vote agora", "vote já", "vote ja", "assembleia",
    "edital de convocação", "edital de convocacao",
    # Outros irrelevantes
    "indicação", "indicacao", "convide", "programa de pontos",
    "regulamento", "termos de uso", "política de privacidade",
]

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


def assunto_deve_ignorar(assunto: str) -> bool:
    """Retorna True se o assunto bater com algum padrão não-financeiro a ignorar."""
    assunto_lower = assunto.lower()
    return any(padrao in assunto_lower for padrao in ASSUNTOS_IGNORAR)


# ─── Anexos / Extratos ──────────────────────────────────────────────────────

def extrair_anexos(msg) -> list[dict]:
    """
    Percorre as partes do email e retorna uma lista de anexos CSV/OFX encontrados.
    Cada item: {"nome": str, "tipo": "csv"|"ofx", "conteudo": bytes}
    """
    anexos = []
    for parte in msg.walk():
        content_disposition = parte.get("Content-Disposition", "")
        if "attachment" not in content_disposition.lower():
            continue
        nome_raw = parte.get_filename() or ""
        nome = decodificar_header(nome_raw).strip()
        nome_lower = nome.lower()
        if nome_lower.endswith(".csv"):
            tipo = "csv"
        elif nome_lower.endswith(".ofx") or nome_lower.endswith(".qfx"):
            tipo = "ofx"
        else:
            continue
        conteudo = parte.get_payload(decode=True)
        if conteudo:
            anexos.append({"nome": nome, "tipo": tipo, "conteudo": conteudo})
            log.info("Anexo encontrado: %s (%s)", nome, tipo.upper())
    return anexos


def _hash_transacao(data: str, valor: str, descricao: str) -> str:
    chave = f"{data}|{valor}|{descricao}".lower().strip()
    return hashlib.md5(chave.encode()).hexdigest()


def _transacao_ja_existe(hash_id: str) -> bool:
    base = carregar_base()
    return any(t.get("hash_extrato") == hash_id for t in base)


def _inferir_categoria(descricao: str, valor: float) -> str:
    desc = descricao.lower()
    if any(p in desc for p in ["salário", "salario", "pagamento recebido", "crédito em conta"]):
        return "salário"
    if any(p in desc for p in ["freelance", "transferência recebida", "pix recebido"]):
        return "freelance"
    if any(p in desc for p in ["fatura", "pagamento fatura"]):
        return "fatura"
    if any(p in desc for p in ["pix enviado", "transferência enviada", "ted", "doc"]):
        return "transferência"
    if any(p in desc for p in ["cashback", "rewards"]):
        return "cashback"
    if any(p in desc for p in ["compra de aç", "compra de ac", "compra ação", "compra acao"]):
        return "compra_ação"
    if any(p in desc for p in ["venda de aç", "venda de ac", "venda ação", "venda acao"]):
        return "venda_ação"
    if any(p in desc for p in ["invest", "rdb", "cdb", "lci", "lca", "tesouro"]):
        return "investimento"
    return "pagamento" if valor < 0 else "outro"


def _extrair_ticker(memo: str) -> str | None:
    """Extrai o ticker da ação (ex: PETR4, VALE3) de uma descrição OFX."""
    m = re.search(r'\b([A-Z]{3,5}\d{1,2})\b', memo.upper())
    return m.group(1) if m else None


def _eh_operacao_acoes(memo: str) -> tuple[bool, str]:
    """Retorna (é_ação, 'compra'|'venda') para memos de operações de bolsa."""
    desc = memo.lower()
    if any(p in desc for p in ["compra de aç", "compra de ac", "compra ação", "compra acao"]):
        return True, "compra"
    if any(p in desc for p in ["venda de aç", "venda de ac", "venda ação", "venda acao"]):
        return True, "venda"
    return False, ""


# ── Parser CSV (Nubank e formato genérico) ───────────────────────────────────

def parsear_csv(conteudo: bytes, empresa: str) -> list[dict]:
    """
    Suporta formatos com separador vírgula ou tabulação.
    Colunas esperadas: Data, Valor, Descrição (+ Identificador opcional).
    Retorna lista de dicts prontos para salvar_na_base.
    """
    texto = conteudo.decode("utf-8", errors="ignore")

    # Detecta delimitador: tab ou vírgula
    primeira_linha = texto.split("\n")[0]
    delimitador = "\t" if "\t" in primeira_linha else ","

    reader = csv.DictReader(io.StringIO(texto), delimiter=delimitador)

    # Normaliza nomes de colunas (remove BOM, espaços, lowercase)
    def norm(s):
        return s.strip().lstrip("\ufeff").lower()

    transacoes = []
    for row in reader:
        row_norm = {norm(k): v.strip() for k, v in row.items() if k}

        # Tenta mapear colunas em português e inglês
        data_str = (
            row_norm.get("data") or row_norm.get("date") or
            row_norm.get("dt lancto") or ""
        ).strip()
        desc = (
            row_norm.get("descrição") or row_norm.get("descricao") or
            row_norm.get("description") or row_norm.get("memo") or ""
        ).strip()
        valor_str = (
            row_norm.get("valor") or row_norm.get("amount") or
            row_norm.get("value") or "0"
        ).strip().replace(",", ".")

        # Identificador UUID (campo do extrato Nomad/Nubank) — usado como hash único
        identificador = (
            row_norm.get("identificador") or row_norm.get("id") or
            row_norm.get("fitid") or ""
        ).strip()

        if not data_str or not desc:
            continue

        try:
            valor = float(valor_str)
        except ValueError:
            valor = 0.0

        # Tenta parsear a data em vários formatos
        dt = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
            try:
                dt = datetime.strptime(data_str, fmt)
                break
            except ValueError:
                pass
        data_iso = dt.strftime("%Y-%m-%d") if dt else data_str

        # Prefere UUID do extrato como hash; fallback para MD5
        hash_id = identificador if identificador else _hash_transacao(data_iso, str(valor), desc)
        if _transacao_ja_existe(hash_id):
            log.debug("Transação já existe (CSV), ignorando: %s %s", data_iso, desc)
            continue

        tipo = "entrada" if valor > 0 else "saída"
        categoria = _inferir_categoria(desc, valor)
        origem = empresa if valor < 0 else desc
        destino = desc if valor < 0 else empresa

        transacoes.append({
            "hash_extrato": hash_id,
            "data": data_iso,
            "empresa": empresa,
            "remetente": f"extrato@{empresa}.com.br",
            "assunto": f"Extrato {empresa.upper()} importado via CSV",
            "tipo": tipo,
            "valor": str(abs(valor)),
            "moeda": "BRL",
            "origem": origem,
            "destino": destino,
            "categoria": categoria,
            "descricao": desc,
            "observacoes": f"Importado de arquivo CSV — {data_iso}",
            "resumo_markdown": f"**{desc}** — {tipo} de R$ {abs(valor):.2f} em {data_iso}",
            "fonte": "extrato_csv",
        })

    return transacoes


# ── Parser OFX ───────────────────────────────────────────────────────────────

def parsear_ofx(conteudo: bytes, empresa: str) -> list[dict]:
    """
    Parser para OFX/QFX (SGML ou XML).
    Extrai blocos <STMTTRN>...</STMTTRN>.
    """
    texto = conteudo.decode("utf-8", errors="ignore")

    # Captura todos os blocos de transação
    blocos = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", texto, re.DOTALL | re.IGNORECASE)

    def get_tag(bloco, tag):
        m = re.search(rf"<{tag}>\s*([^\r\n<]+)", bloco, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    transacoes = []
    for bloco in blocos:
        fitid  = get_tag(bloco, "FITID")
        dtpost = get_tag(bloco, "DTPOSTED")
        amount = get_tag(bloco, "TRNAMT")
        memo   = get_tag(bloco, "MEMO") or get_tag(bloco, "NAME")

        if not amount:
            continue

        # Formata data: 20240115120000[-3:BRT] → 2024-01-15
        data_iso = dtpost[:8]
        if len(data_iso) == 8 and data_iso.isdigit():
            data_iso = f"{data_iso[:4]}-{data_iso[4:6]}-{data_iso[6:8]}"

        try:
            valor = float(amount.replace(",", "."))
        except ValueError:
            valor = 0.0

        hash_id = fitid if fitid else _hash_transacao(data_iso, str(valor), memo)
        if _transacao_ja_existe(hash_id):
            log.debug("Transação já existe (OFX), ignorando: %s %s", data_iso, memo)
            continue

        eh_acao, subtipo_acao = _eh_operacao_acoes(memo)
        if eh_acao:
            ticker = _extrair_ticker(memo)
            tipo = f"{subtipo_acao}_ação"
            categoria = "ação"
            ticker_str = f" ({ticker})" if ticker else ""
            resumo_md = (
                f"**{subtipo_acao.capitalize()} de Ação{ticker_str}** — "
                f"R$ {abs(valor):.2f} em {data_iso} | {memo}"
            )
            obs = f"Operação de bolsa detectada no extrato OFX — {data_iso}"
        else:
            tipo = "entrada" if valor > 0 else "saída"
            categoria = _inferir_categoria(memo, valor)
            ticker = None
            resumo_md = f"**{memo}** — {tipo} de R$ {abs(valor):.2f} em {data_iso}"
            obs = f"Importado de arquivo OFX — {data_iso}"

        origem = empresa if valor < 0 else memo
        destino = memo if valor < 0 else empresa

        entrada = {
            "hash_extrato": hash_id,
            "data": data_iso,
            "empresa": empresa,
            "remetente": f"extrato@{empresa}.com.br",
            "assunto": f"Extrato {empresa.upper()} importado via OFX",
            "tipo": tipo,
            "valor": str(abs(valor)),
            "moeda": "BRL",
            "origem": origem,
            "destino": destino,
            "categoria": categoria,
            "descricao": memo,
            "observacoes": obs,
            "resumo_markdown": resumo_md,
            "fonte": "extrato_ofx",
        }
        if ticker:
            entrada["ticker"] = ticker
        transacoes.append(entrada)

    return transacoes


def _perguntar_saldo_carteira(empresa: str, data_email: str):
    """Pergunta o saldo atual da carteira de investimentos após operações de ações."""
    print("\n" + "─" * 60)
    print(f"  📈  Operações de ações detectadas no extrato {empresa.upper()}")
    print("─" * 60)
    try:
        resposta = input("  Saldo atual da carteira de investimentos? [Enter para pular]: ").strip()
    except (EOFError, KeyboardInterrupt):
        resposta = ""

    if not resposta:
        return

    try:
        valor_float = float(resposta.replace("R$", "").replace(".", "").replace(",", ".").strip())
        valor = f"{valor_float:.2f}"
    except ValueError:
        log.warning("Saldo inválido informado: '%s'", resposta)
        return

    data_iso = datetime.now().strftime("%Y-%m-%d")
    hash_id = _hash_transacao(data_iso, valor, "snapshot_carteira")
    if _transacao_ja_existe(hash_id):
        log.info("Snapshot de carteira já registrado para hoje.")
        return

    snapshot = {
        "hash_extrato": hash_id,
        "data": data_iso,
        "empresa": empresa,
        "remetente": "usuario",
        "assunto": f"Saldo carteira — {empresa.upper()}",
        "tipo": "snapshot_carteira",
        "valor": valor,
        "moeda": "BRL",
        "origem": "carteira_investimentos",
        "destino": "carteira_investimentos",
        "categoria": "investimento",
        "descricao": f"Saldo total da carteira de investimentos: R$ {valor}",
        "observacoes": "Informado pelo usuário após operações de ações no extrato.",
        "resumo_markdown": f"**Saldo carteira de investimentos** — R$ {valor} em {data_iso}",
        "fonte": "confirmacao_usuario",
        "confianca": "alta",
    }
    salvar_na_base(snapshot)
    salvar_no_arquivo(empresa, "usuario", snapshot["assunto"], data_iso, snapshot)
    log.info("Saldo carteira registrado: R$ %s", valor)
    print(f"  ✓ Saldo registrado: R$ {valor}\n")


def processar_anexos_extrato(msg, empresa: str, data_email: str) -> int:
    """
    Extrai anexos CSV/OFX do email, parseia e importa na base.
    Retorna o número de transações importadas.
    """
    anexos = extrair_anexos(msg)
    if not anexos:
        return 0

    total = 0
    tem_operacoes_acoes = False
    for anexo in anexos:
        log.info("Processando anexo: %s", anexo["nome"])
        if anexo["tipo"] == "csv":
            transacoes = parsear_csv(anexo["conteudo"], empresa)
        else:
            transacoes = parsear_ofx(anexo["conteudo"], empresa)

        for t in transacoes:
            salvar_na_base(t)
            salvar_no_arquivo(
                empresa, t["remetente"], t["assunto"], t["data"], t
            )
            total += 1
            if t.get("categoria") == "ação":
                tem_operacoes_acoes = True

        log.info(
            "%d transação(ões) importada(s) de %s", len(transacoes), anexo["nome"]
        )

    if tem_operacoes_acoes:
        _perguntar_saldo_carteira(empresa, data_email)

    return total


# ─── Claude ─────────────────────────────────────────────────────────────────

def _chamar_claude(messages: list) -> str:
    """Faz a chamada à API e retorna o texto da resposta."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=messages,
    )
    return response.content[0].text.strip()


def _parse_json_claude(texto: str, empresa: str) -> dict:
    """Extrai JSON da resposta do Claude, com fallback."""
    if texto.startswith("```"):
        texto = texto.split("```")[1]
        if texto.startswith("json"):
            texto = texto[4:]
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return {
            "tipo": "desconhecido",
            "valor": None,
            "moeda": None,
            "origem": empresa,
            "destino": "desconhecido",
            "categoria": "outro",
            "descricao": texto[:200],
            "observacoes": "",
            "resumo_markdown": texto,
            "confianca": "baixa",
            "duvida": None,
        }


def interpretar_com_claude(remetente: str, assunto: str, corpo: str, empresa: str, data_email: str) -> dict:
    """
    Usa Claude para interpretar o email. Se Claude tiver dúvida (confianca != alta),
    pergunta ao usuário no terminal e refaz a interpretação com o esclarecimento.
    """
    contexto_historico = resumo_base_para_contexto()

    prompt_sistema = f"""Você é um assistente financeiro pessoal inteligente. Analise o email abaixo e extraia informações financeiras detalhadas.

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
  "resumo_markdown": "texto formatado em markdown para o relatório, com todos os detalhes importantes",
  "confianca": "alta | media | baixa  — sua confiança na interpretação acima",
  "duvida": "se confianca for media ou baixa, escreva aqui UMA pergunta objetiva para o usuário esclarecer; caso contrário null"
}}

Responda SOMENTE o JSON, sem texto adicional."""

    messages = [{"role": "user", "content": prompt_sistema}]
    texto = _chamar_claude(messages)
    dados = _parse_json_claude(texto, empresa)

    # Se Claude tiver dúvida, pergunta ao usuário e refaz
    confianca = dados.get("confianca", "alta")
    duvida = dados.get("duvida")

    if confianca in ("media", "baixa") and duvida:
        log.info("Claude tem dúvida sobre este email (confiança: %s)", confianca)
        print("\n" + "─" * 60)
        print(f"  Email: [{empresa.upper()}] {assunto[:70]}")
        print(f"  Interpretação atual: {dados.get('tipo','?')} | {dados.get('valor','?')} {dados.get('moeda','')}")
        print(f"\n  Claude pergunta: {duvida}")
        print("─" * 60)

        try:
            resposta_usuario = input("  Sua resposta (Enter para pular): ").strip()
        except (EOFError, KeyboardInterrupt):
            resposta_usuario = ""

        if resposta_usuario:
            # Adiciona o esclarecimento ao histórico de mensagens e chama novamente
            messages.append({"role": "assistant", "content": texto})
            messages.append({"role": "user", "content": (
                f"Esclarecimento do usuário: {resposta_usuario}\n\n"
                "Com base nesse esclarecimento, reanalise e retorne o JSON corrigido."
            )})
            texto2 = _chamar_claude(messages)
            dados = _parse_json_claude(texto2, empresa)
            log.info("Claude reinterpretou com esclarecimento do usuário.")
        else:
            log.info("Usuário pulou a pergunta — mantendo interpretação original.")

        print()

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
                if assunto_deve_ignorar(assunto):
                    log.debug("Ignorado (assunto não-financeiro): %s", assunto[:70])
                    salvar_id_processado(email_id_str)
                    continue

                emails_encontrados += 1
                log.info("Email financeiro encontrado: [%s] %s", empresa.upper(), assunto[:60])

                # 0) Detecta "paycheck received" — registra valor fixo sem chamar Claude
                corpo_preview = extrair_corpo(msg)
                if empresa in VALOR_PAYCHECK and _detectar_paycheck(assunto, corpo_preview):
                    log.info("Paycheck detectado: [%s]", empresa.upper())
                    registrar_paycheck(empresa, data_email, email_id_str)
                    salvar_id_processado(email_id_str)
                    emails_processados += 1
                    continue

                # 1) Tenta importar anexos CSV/OFX — prioridade sobre interpretação do corpo
                n_importadas = processar_anexos_extrato(msg, empresa, data_email)

                if n_importadas > 0:
                    log.info(
                        "%d transação(ões) importada(s) do extrato anexo em [%s]",
                        n_importadas, empresa.upper(),
                    )
                else:
                    # 2) Sem anexo: interpreta o corpo do email com Claude
                    corpo = corpo_preview  # já extraído acima
                    log.info("Corpo extraído (%d caracteres). Enviando para Claude...", len(corpo))

                    dados = interpretar_com_claude(remetente, assunto, corpo, empresa, data_email)
                    log.info(
                        "Interpretado: %s | %s %s | %s → %s",
                        dados.get("tipo"), dados.get("valor"), dados.get("moeda"),
                        dados.get("origem"), dados.get("destino"),
                    )

                    transacao = {
                        "id_email": email_id_str,
                        "data": data_email,
                        "empresa": empresa,
                        "remetente": remetente,
                        "assunto": assunto,
                        "fonte": "email_corpo",
                        **dados,
                    }
                    salvar_na_base(transacao)
                    salvar_no_arquivo(empresa, remetente, assunto, data_email, dados)
                    time.sleep(1)

                salvar_id_processado(email_id_str)
                emails_processados += 1

        salvar_ultima_execucao()
        mail.logout()
        log.info("Desconectado do Gmail")

    except Exception as e:
        log.error("Erro durante a verificação: %s", e, exc_info=True)

    if emails_encontrados == 0:
        log.info("Nenhum email financeiro novo encontrado")
    log.info("--- Verificação concluída: %d email(s) processado(s) ---", emails_processados)


# ─── Paycheck ────────────────────────────────────────────────────────────────

def _carregar_paychecks() -> list:
    if os.path.exists(ARQUIVO_PAYCHECKS):
        with open(ARQUIVO_PAYCHECKS, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _salvar_paychecks(paychecks: list):
    with open(ARQUIVO_PAYCHECKS, "w", encoding="utf-8") as f:
        json.dump(paychecks, f, ensure_ascii=False, indent=2)


def _detectar_paycheck(assunto: str, corpo: str) -> bool:
    texto = (assunto + " " + corpo).lower()
    return "paycheck received" in texto or "you've been paid" in texto or "you have been paid" in texto


def registrar_paycheck(empresa: str, data_email: str, id_email: str):
    """Salva paycheck na base financeira e adiciona à fila de confirmação diária."""
    valor_padrao = VALOR_PAYCHECK.get(empresa)
    if not valor_padrao:
        return

    # Pergunta o valor ao usuário; Enter usa o padrão
    print("\n" + "─" * 60)
    print(f"  💰  PAYCHECK {empresa.upper()} detectado!")
    print(f"  Padrão para {empresa.upper()}: ${valor_padrao} USD")
    print("─" * 60)
    try:
        resposta = input(f"  Quanto entrou? [Enter = ${valor_padrao}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        resposta = ""

    if resposta:
        # Aceita formatos: "2000", "2.000", "2,000", "2000.00"
        valor = resposta.replace(",", "").replace(".", "").lstrip("$").strip()
        # Tenta preservar decimais se o usuário digitou com ponto decimal
        try:
            valor_float = float(resposta.replace(",", ""))
            valor = f"{valor_float:.2f}"
        except ValueError:
            valor = valor_padrao
            log.warning("Valor inválido informado ('%s'), usando padrão: %s", resposta, valor_padrao)
    else:
        valor = valor_padrao

    print(f"  ✓ Valor registrado: ${valor} USD\n")

    hash_id = _hash_transacao(data_email, valor, f"paycheck {empresa}")
    if _transacao_ja_existe(hash_id):
        log.info("Paycheck já registrado: %s %s", empresa, data_email)
        return

    data_iso = datetime.now().strftime("%Y-%m-%d")
    transacao = {
        "hash_extrato": hash_id,
        "data": data_iso,
        "empresa": empresa,
        "remetente": f"payments@{empresa}.{'ai' if empresa == 'ontop' else 'com'}",
        "assunto": f"Paycheck received — {empresa.upper()}",
        "tipo": "entrada",
        "valor": valor,
        "moeda": "USD",
        "origem": empresa.upper(),
        "destino": "conta pessoal",
        "categoria": "salário",
        "descricao": f"Paycheck {empresa.upper()} — ${valor}",
        "observacoes": "Registrado automaticamente via detecção de paycheck no email.",
        "resumo_markdown": f"**Paycheck {empresa.upper()}** — ${valor} USD recebido em {data_iso}",
        "fonte": "email_corpo",
        "id_email": id_email,
        "confianca": "alta",
    }
    salvar_na_base(transacao)
    salvar_no_arquivo(empresa, transacao["remetente"], transacao["assunto"], data_iso, transacao)
    log.info("Paycheck registrado: %s $%s USD", empresa.upper(), valor)

    # Adiciona à fila de confirmação diária
    paychecks = _carregar_paychecks()
    paychecks.append({
        "hash": hash_id,
        "empresa": empresa,
        "valor": valor,
        "data_recebimento": data_iso,
        "ultima_pergunta": None,
        "status": "pendente",
    })
    _salvar_paychecks(paychecks)
    log.info("Paycheck adicionado à fila de confirmação diária.")


def verificar_paychecks_pendentes():
    """
    Job diário: pergunta se o dinheiro ainda está na conta.
    Se sim, agenda para amanhã. Se não, registra o destino.
    """
    paychecks = _carregar_paychecks()
    hoje = datetime.now().strftime("%Y-%m-%d")
    pendentes = [
        p for p in paychecks
        if p["status"] == "pendente" and p.get("ultima_pergunta") != hoje
    ]

    if not pendentes:
        return

    alterados = False
    for p in pendentes:
        empresa = p["empresa"].upper()
        valor = p["valor"]
        data = p["data_recebimento"]

        print("\n" + "═" * 60)
        print(f"  💰  PAYCHECK {empresa} — ${valor} USD")
        print(f"  Recebido em: {data}")
        print("═" * 60)
        print("  O dinheiro ainda está na conta?")
        print("  [1] Sim, ainda está")
        print("  [2] Não, já enviei")
        print("─" * 60)

        try:
            resp = input("  Escolha (1/2): ").strip()
        except (EOFError, KeyboardInterrupt):
            resp = ""

        if resp == "1":
            p["ultima_pergunta"] = hoje
            log.info("Paycheck %s ainda na conta — perguntarei amanhã.", empresa)
            alterados = True

        elif resp == "2":
            print("\n  Você enviou para?")
            print("  [1] Carteira cripto")
            print("  [2] Brasil — Nubank")
            print("  [3] Outro (digitar)")
            print("─" * 60)
            try:
                dest_resp = input("  Escolha (1/2/3): ").strip()
            except (EOFError, KeyboardInterrupt):
                dest_resp = ""

            if dest_resp == "1":
                destino = "carteira cripto"
            elif dest_resp == "2":
                destino = "Brasil — Nubank"
            elif dest_resp == "3":
                try:
                    destino = input("  Para onde? ").strip() or "outro"
                except (EOFError, KeyboardInterrupt):
                    destino = "outro"
            else:
                destino = "desconhecido"

            # Registra a transferência na base
            data_iso = datetime.now().strftime("%Y-%m-%d")
            hash_transf = _hash_transacao(data_iso, valor, f"envio {destino} {empresa}")
            if not _transacao_ja_existe(hash_transf):
                transacao_envio = {
                    "hash_extrato": hash_transf,
                    "data": data_iso,
                    "empresa": p["empresa"],
                    "remetente": "usuario",
                    "assunto": f"Envio paycheck {empresa} → {destino}",
                    "tipo": "saída",
                    "valor": valor,
                    "moeda": "USD",
                    "origem": "conta pessoal",
                    "destino": destino,
                    "categoria": "transferência",
                    "descricao": f"Paycheck {empresa} enviado para {destino}",
                    "observacoes": f"Confirmado pelo usuário em {data_iso}.",
                    "resumo_markdown": (
                        f"**Envio** — ${valor} USD do paycheck {empresa} "
                        f"transferido para **{destino}** em {data_iso}"
                    ),
                    "fonte": "confirmacao_usuario",
                    "confianca": "alta",
                }
                salvar_na_base(transacao_envio)
                salvar_no_arquivo(
                    p["empresa"], "usuario",
                    transacao_envio["assunto"], data_iso, transacao_envio
                )
                log.info("Transferência registrada: %s $%s → %s", empresa, valor, destino)

            p["status"] = "enviado"
            p["destino_final"] = destino
            alterados = True
            print(f"\n  ✓ Registrado: ${valor} USD enviado para {destino}\n")

        else:
            # Sem resposta — pula, pergunta amanhã
            p["ultima_pergunta"] = hoje
            alterados = True

    if alterados:
        _salvar_paychecks(paychecks)


# ─── Init ────────────────────────────────────────────────────────────────────

def inicializar_arquivo():
    if not os.path.exists(ARQUIVO_SAIDA):
        with open(ARQUIVO_SAIDA, "w", encoding="utf-8") as f:
            f.write("# Resumo Financeiro por Email\n")
            f.write("Gerado automaticamente pelo Agente de Email Financeiro\n")
            f.write(f"Iniciado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n")
        log.info("Arquivo de saída criado: %s", ARQUIVO_SAIDA)


def restart():
    """Apaga todos os dados locais: base, relatório, emails lidos, estado e paychecks."""
    arquivos = [
        ARQUIVO_BASE,
        ARQUIVO_SAIDA,
        ARQUIVO_IDS_PROCESSADOS,
        ARQUIVO_ESTADO,
        ARQUIVO_PAYCHECKS,
    ]
    print("\n  ⚠️  RESTART — os seguintes arquivos serão apagados:")
    for arq in arquivos:
        status = "existe" if os.path.exists(arq) else "não existe"
        print(f"    {arq}  ({status})")
    print()
    try:
        confirmacao = input("  Confirma? (s/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        confirmacao = ""
    if confirmacao != "s":
        print("  Cancelado.\n")
        return
    for arq in arquivos:
        if os.path.exists(arq):
            os.remove(arq)
            log.info("Removido: %s", arq)
    print("  Dados resetados. O agente vai reprocessar tudo do zero.\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agente de Email Financeiro")
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Limpa todos os dados locais (base, relatório, emails lidos, estado, paychecks) e reinicia do zero.",
    )
    args = parser.parse_args()

    if args.restart:
        restart()

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

    # Verificação diária de paychecks pendentes (10h da manhã)
    schedule.every().day.at("10:00").do(verificar_paychecks_pendentes)
    # Roda também agora, caso já haja paychecks aguardando resposta
    verificar_paychecks_pendentes()

    log.info("Agente rodando... (Ctrl+C para parar)")
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
