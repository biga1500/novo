"""
importar_extrato.py — Importa arquivos CSV/OFX diretamente para financeiro_base.json

Uso:
    python importar_extrato.py arquivo.csv
    python importar_extrato.py extrato.ofx --empresa nubank
    python importar_extrato.py *.csv --empresa nomad
"""

import argparse
import sys
from pathlib import Path

# Reutiliza toda a lógica do agente
from agente_email import (
    parsear_csv,
    parsear_ofx,
    salvar_na_base,
    salvar_no_arquivo,
    FILTROS_FINANCEIROS,
)


EMPRESAS_VALIDAS = list(FILTROS_FINANCEIROS.keys())


def detectar_empresa(caminho: Path) -> str | None:
    """Tenta inferir a empresa pelo nome do arquivo."""
    nome = caminho.stem.lower()
    for empresa in EMPRESAS_VALIDAS:
        if empresa in nome:
            return empresa
    return None


def importar_arquivo(caminho: Path, empresa: str) -> int:
    conteudo = caminho.read_bytes()
    ext = caminho.suffix.lower()

    if ext == ".csv":
        transacoes = parsear_csv(conteudo, empresa)
    elif ext in (".ofx", ".qfx"):
        transacoes = parsear_ofx(conteudo, empresa)
    else:
        print(f"  [IGNORADO] Formato não suportado: {caminho.name}")
        return 0

    novas = 0
    for t in transacoes:
        salvar_na_base(t)
        salvar_no_arquivo(empresa, t["remetente"], t["assunto"], t["data"], t)
        novas += 1

    return novas


def main():
    parser = argparse.ArgumentParser(description="Importa extratos CSV/OFX para o dashboard financeiro")
    parser.add_argument("arquivos", nargs="+", help="Arquivos CSV ou OFX a importar")
    parser.add_argument(
        "--empresa",
        choices=EMPRESAS_VALIDAS,
        help=f"Empresa do extrato ({', '.join(EMPRESAS_VALIDAS)}). Se omitido, detecta pelo nome do arquivo.",
    )
    args = parser.parse_args()

    total = 0
    for padrao in args.arquivos:
        # Suporta glob manual (ex: *.csv no Windows que não expande no shell)
        caminhos = list(Path(".").glob(padrao)) if "*" in padrao else [Path(padrao)]

        for caminho in sorted(caminhos):
            if not caminho.exists():
                print(f"  [ERRO] Arquivo não encontrado: {caminho}")
                continue

            empresa = args.empresa or detectar_empresa(caminho)
            if not empresa:
                print(f"  [AVISO] Não foi possível detectar a empresa de '{caminho.name}'.")
                print(f"          Use --empresa ({', '.join(EMPRESAS_VALIDAS)})")
                continue

            print(f"  Importando {caminho.name} [{empresa.upper()}]...", end=" ")
            n = importar_arquivo(caminho, empresa)
            print(f"{n} transação(ões) adicionada(s).")
            total += n

    print(f"\nTotal importado: {total} transação(ões).")
    if total > 0:
        print("Abra http://localhost:5000 para ver no dashboard.")


if __name__ == "__main__":
    main()
