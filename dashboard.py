"""
Dashboard financeiro — lê financeiro_base.json e expõe uma API Flask.
"""

import json
import os
from collections import defaultdict
from datetime import datetime
from email.utils import parsedate_to_datetime

import anthropic
from flask import Flask, jsonify, render_template

from config import ARQUIVO_BASE, ANTHROPIC_API_KEY, MODEL

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

app = Flask(__name__)


def carregar_base() -> list:
    if not os.path.exists(ARQUIVO_BASE):
        return []
    with open(ARQUIVO_BASE, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_data(data_str: str) -> datetime | None:
    """Tenta parsear datas no formato RFC 2822 (email) ou ISO."""
    if not data_str:
        return None
    try:
        return parsedate_to_datetime(data_str)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(data_str, fmt)
        except ValueError:
            pass
    return None


def to_float(valor) -> float:
    try:
        return float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/resumo")
def api_resumo():
    base = carregar_base()

    total_entradas_brl = 0.0
    total_entradas_usd = 0.0
    total_saidas = 0.0
    por_empresa: dict[str, float] = defaultdict(float)      # só entradas
    por_empresa_moeda: dict[str, str] = {}                   # moeda predominante por empresa
    por_categoria: dict[str, float] = defaultdict(float)
    por_mes: dict[str, float] = defaultdict(float)
    transacoes = []

    # Investimentos
    total_comprado = 0.0
    total_vendido = 0.0
    ultimo_snapshot: float | None = None
    ultimo_snapshot_dt: datetime | None = None
    operacoes_acoes = []

    TIPOS_ACOES = {"compra_ação", "compra_acao", "venda_ação", "venda_acao"}
    CATS_ENTRADA = {"salário", "freelance", "cashback"}

    for t in base:
        valor = to_float(t.get("valor"))
        tipo = t.get("tipo", "").lower()
        empresa = t.get("empresa", "desconhecida").upper()
        categoria = t.get("categoria", "outro")
        moeda = (t.get("moeda") or "BRL").upper()
        dt = parse_data(t.get("data", ""))
        mes = dt.strftime("%Y-%m") if dt else "desconhecido"
        data_fmt = dt.strftime("%d/%m/%Y") if dt else t.get("data", "?")

        # ── snapshot da carteira ───────────────────────────────────────
        if tipo == "snapshot_carteira":
            if ultimo_snapshot_dt is None or (dt and dt > ultimo_snapshot_dt):
                ultimo_snapshot = valor
                ultimo_snapshot_dt = dt
            continue

        # ── operações de ações ─────────────────────────────────────────
        if tipo in TIPOS_ACOES or categoria == "ação":
            is_compra = "compra" in tipo
            if is_compra:
                total_comprado += valor
            else:
                total_vendido += valor
            operacoes_acoes.append({
                "data": data_fmt,
                "empresa": empresa,
                "tipo": tipo,
                "ticker": t.get("ticker", ""),
                "valor": valor,
                "moeda": moeda,
                "descricao": t.get("descricao", ""),
            })
            continue

        # ── transações normais ─────────────────────────────────────────
        is_entrada = tipo == "entrada" or categoria in CATS_ENTRADA
        is_saida = tipo in ("saída", "saida", "pagamento", "fatura", "transferência", "transferencia")

        if is_entrada and valor > 0:
            if moeda == "USD":
                total_entradas_usd += valor
            else:
                total_entradas_brl += valor
            # por_empresa e por_mes só contam entradas
            por_empresa[empresa] += valor
            por_empresa_moeda.setdefault(empresa, moeda)
            por_mes[mes] += valor
            por_categoria[categoria] += valor
        elif is_saida and valor > 0:
            total_saidas += valor
            por_categoria[categoria] += valor

        transacoes.append({
            "data": data_fmt,
            "empresa": empresa,
            "tipo": tipo,
            "categoria": categoria,
            "valor": valor,
            "moeda": moeda,
            "origem": t.get("origem", "?"),
            "destino": t.get("destino", "?"),
            "descricao": t.get("descricao", ""),
        })

    # Ordena transações da mais recente para a mais antiga
    def _sort_key(x):
        try:
            return datetime.strptime(x["data"], "%d/%m/%Y")
        except Exception:
            return datetime.min

    transacoes_sorted = sorted(transacoes, key=_sort_key, reverse=True)
    operacoes_acoes_sorted = sorted(operacoes_acoes, key=_sort_key, reverse=True)
    meses_sorted = dict(sorted(por_mes.items()))

    return jsonify({
        "total_transacoes": len(base),
        "total_entradas_brl": round(total_entradas_brl, 2),
        "total_entradas_usd": round(total_entradas_usd, 2),
        "total_saidas": round(total_saidas, 2),
        "saldo_brl": round(total_entradas_brl - total_saidas, 2),
        "por_empresa": dict(por_empresa),
        "por_empresa_moeda": por_empresa_moeda,
        "por_categoria": dict(por_categoria),
        "por_mes": meses_sorted,
        "transacoes": transacoes_sorted,
        "investimentos": {
            "total_comprado": round(total_comprado, 2),
            "total_vendido": round(total_vendido, 2),
            "saldo_liquido": round(total_comprado - total_vendido, 2),
            "ultimo_snapshot": round(ultimo_snapshot, 2) if ultimo_snapshot is not None else None,
            "operacoes": operacoes_acoes_sorted,
        },
    })


@app.route("/api/resumo-ia")
def api_resumo_ia():
    base = carregar_base()
    if not base:
        return jsonify({"resumo": "Nenhuma transação encontrada na base."})

    # Monta um resumo compacto das transações para enviar ao Claude
    linhas = []
    for t in base:
        dt = parse_data(t.get("data", ""))
        data_fmt = dt.strftime("%d/%m/%Y") if dt else t.get("data", "?")
        linhas.append(
            f"- [{data_fmt}] {t.get('empresa','?').upper()} | "
            f"{t.get('tipo','?')} | R$ {t.get('valor','0')} {t.get('moeda','')} | "
            f"{t.get('categoria','?')} | {t.get('descricao','')}"
        )

    historico = "\n".join(linhas)
    prompt = f"""Você é um analista financeiro pessoal. Com base nas transações abaixo, gere um resumo financeiro completo em markdown.

TRANSAÇÕES:
{historico}

Inclua:
1. **Visão geral** — total de entradas, saídas e saldo
2. **Principais fontes de renda** — de onde veio o dinheiro
3. **Principais gastos** — onde o dinheiro foi
4. **Padrões identificados** — recorrências, tendências
5. **Insights e recomendações** — o que chama atenção, sugestões práticas

Seja direto, use números reais do histórico, formate bem com markdown."""

    response = claude.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return jsonify({"resumo": response.content[0].text})


if __name__ == "__main__":
    print("Dashboard rodando em http://localhost:5000")
    app.run(debug=True, port=5000)
