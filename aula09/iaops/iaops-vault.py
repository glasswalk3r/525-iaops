#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import warnings

warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"google\.api_core\._python_version_support",
)

import os
import sys
import json
import argparse
import pathlib
import re
from typing import Any, Dict, List

# ============================ Dependência ============================
try:
    import google.generativeai as genai
except ImportError:
    print("Falta google-generativeai. Rode: pip install -U google-generativeai", file=sys.stderr)
    sys.exit(1)


# ============================ Gemini API =============================
def ensure_api_key():
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("Erro: defina GEMINI_API_KEY (ou GOOGLE_API_KEY).", file=sys.stderr)
        sys.exit(1)
    genai.configure(api_key=key)


def call_model(instruction: str, model_name: str = "gemini-2.5-pro", temperature: float = 0.2) -> str:
    resp = genai.GenerativeModel(model_name).generate_content(
        [
            {"role": "user", "parts": [SYSTEM_PROMPT]},
            {"role": "user", "parts": [instruction]},
        ],
        generation_config={
            "temperature": temperature,
            "top_p": 0.9,
            "top_k": 40,
            "response_mime_type": "application/json",
        },
    )
    return (getattr(resp, "text", "") or "").strip()


def parse_ai_json(text: str) -> Dict[str, Any]:
    # Remove possíveis cercas de código ```json ... ```
    if text.startswith("```"):
        for chunk in text.split("```"):
            c = chunk.strip()
            if c.startswith("{") and c.endswith("}"):
                text = c
                break
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("Resposta não parece JSON.")
    return json.loads(text[i:j + 1])


# ============================ SYSTEM PROMPT ==========================
SYSTEM_PROMPT = r"""
Você é um gerador de **artefatos HashiCorp Vault** para IAOps que produz
SOMENTE um JSON com arquivos (policy HCL, script AppRole e runbook de rotação).

SAÍDA (JSON estrito):
{
  "files": [
    {"path": "policies/<NOME>.hcl", "content": "conteúdo HCL"},
    {"path": "scripts/<NOME>-approle.sh", "content": "conteúdo Bash"},
    {"path": "docs/<NOME>-rotacao.md", "content": "conteúdo Markdown"}
  ],
  "notes": "opcional"
}
NÃO escreva nada fora desse JSON.

REGRAS GERAIS:
- Não use Markdown fora de arquivos .md.
- Não coloque ``` dentro do conteúdo; apenas texto puro (HCL, Bash, Markdown).
- Indente HCL e Shell de forma legível.
- Scripts Bash:
  - Começar com `#!/usr/bin/env bash`.
  - Usar `set -euo pipefail`.
  - Comandos `vault` explícitos.
- Use comentários em PT-BR, curtos e claros.

CONTEXTOS:

1) policies/<NAME>.hcl
   - Arquivo HCL de política Vault.
   - Quando for para GitLab CI da "loja-online", siga o padrão:
       path "secret/data/loja-online/*" {
         capabilities = ["read"]
       }
   - Política de **somente leitura** (read) para segredos do path indicado.

2) scripts/<NAME>-approle.sh
   - Script Bash que:
     - Escreve a policy no Vault (`vault policy write ...`).
     - Habilita o método de auth AppRole (`vault auth enable approle || ...`).
     - Cria/atualiza um AppRole para CI/CD (`auth/approle/role/<APPROLE_NAME>`).
     - TTLs razoáveis:
       - token_ttl entre 15m e 30m.
       - token_max_ttl até 2h.
       - secret_id_ttl curto (5m a 30m).
       - secret_id_num_uses=1 (uso único).
     - Ao final:
       - Lê o role_id (`vault read .../role-id`).
       - Gera um secret_id (`vault write -f .../secret-id`).
       - Imprime na tela algo como:
           VAULT_ROLE_ID: ...
           VAULT_SECRET_ID: ...
       - Inclui instruções finais para guardar os valores como variáveis de CI/CD no GitLab
         (VAULT_ROLE_ID visível, VAULT_SECRET_ID mascarado/protegido).

   - O script deve assumir que o arquivo de policy está em:
       policies/<POLICY_NAME>.hcl

3) docs/<NAME>-rotacao.md
   - Runbook de rotação em Markdown, com seções:
     - Visão Geral
     - Pré-requisitos
     - Processo de criação/atualização da policy/AppRole com o script gerado
     - Rotação de segredos:
       - Para o laboratório da loja-online, exemplo canônico:
           vault kv put secret/loja-online/productcatalogservice \
             CATALOG_API_KEY="rotated-$(date +%s)" \
             SENTRY_DSN="https://novo@sentry.io/2"
     - Rotação de Secret ID:
       - Exemplo:
           vault write -f auth/approle/role/<APPROLE_NAME>/secret-id
       - Instruções para atualizar VAULT_SECRET_ID no GitLab:
         Settings -> CI/CD -> Variables.
   - Linguagem em PT-BR, tom didático, sem ser prolixo.

REGRAS ESPECÍFICAS PARA O LAB "loja-online" (se o contexto indicar):
- Policy deve usar path "secret/data/loja-online/*" (KV v2).
- Exemplos de rotação de segredos devem usar o caminho:
    secret/loja-online/productcatalogservice
  e chaves:
    CATALOG_API_KEY, SENTRY_DSN.
- O AppRole típico é "gitlab-loja-online".
- Não altere código da aplicação nem do pipeline no runbook; enfatize que a troca
  de segredos é feita via Vault + nova execução do pipeline.

NUNCA escreva nada fora do JSON.
"""


# ============================ Helpers de instrução ===========================
def build_instruction(args: argparse.Namespace) -> str:
    """
    Monta a instrução para o modelo com contexto + spec em linguagem natural.
    """
    name = args.name.strip()
    spec = (args.spec or "").strip()

    ctx_lines = [
        "CONTEXT.TYPE=policy_bundle",
        f"NAME={name}",
        "",
        "Objetivo: gerar 3 arquivos coerentes entre si para integração Vault + GitLab CI:",
        "- policies/<NAME>.hcl: política de leitura para os segredos necessários.",
        "- scripts/<NAME>-approle.sh: script para criar policy + AppRole + imprimir ROLE_ID/SECRET_ID.",
        "- docs/<NAME>-rotacao.md: runbook de rotação de segredos e SecretID.",
        "",
        "IMPORTANTE: saída estritamente no formato JSON descrito no SYSTEM_PROMPT.",
    ]

    # Hint específico para o laboratório "gitlab-loja-online" da aula
    if name == "gitlab-loja-online":
        ctx_lines.append(
            "\nContexto específico do laboratório:\n"
            "- Vault com engine KV v2 em 'secret/'.\n"
            "- Segredos da aplicação em: 'secret/loja-online/productcatalogservice'.\n"
            "- Policy: path 'secret/data/loja-online/*' com capabilities [\"read\"].\n"
            "- AppRole: 'gitlab-loja-online'.\n"
            "- Runbook deve mostrar exemplo de 'vault kv put secret/loja-online/productcatalogservice "
            "CATALOG_API_KEY=... SENTRY_DSN=...'.\n"
        )

    if spec:
        ctx_lines.append("\nEspecificação do usuário:\n" + spec)

    return "\n".join(ctx_lines)


# ============================ Saneamento básico ==============================
def sanitize_files(files: List[Dict[str, str]], name: str) -> List[Dict[str, str]]:
    """
    Pequenos ajustes pós-processamento:
    - Remove cercas de código internas se aparecerem.
    - Garante newline final.
    - Para docs, ajusta exemplos muito genéricos de path para o padrão do lab loja-online.
    """
    sanitized: List[Dict[str, str]] = []

    for f in files:
        path = f.get("path", "").lstrip("./")
        content = f.get("content", "")

        # Remove ``` acidentais
        content = content.replace("```hcl", "").replace("```bash", "").replace("```sh", "")
        content = content.replace("```md", "").replace("```markdown", "").replace("```", "")

        # Patch específico em docs para alinhar com aula (opcional, mas útil):
        if path.startswith("docs/") and path.endswith(".md"):
            # Se aparecer um exemplo com secret/loja-online/database, atualiza para productcatalogservice
            content = content.replace(
                "secret/loja-online/database",
                "secret/loja-online/productcatalogservice",
            )
            # Se não houver nenhum exemplo de productcatalogservice, podemos injetar um pequeno trecho
            if "secret/loja-online/productcatalogservice" not in content:
                snippet = (
                    "\n\n### Exemplo de rotação para a aplicação loja-online\n\n"
                    "```bash\n"
                    "vault kv put secret/loja-online/productcatalogservice \\\n"
                    "  CATALOG_API_KEY=\"rotated-$(date +%s)\" \\\n"
                    "  SENTRY_DSN=\"https://novo@sentry.io/2\"\n"
                    "```\n"
                )
                content = content.rstrip() + snippet

        # Garante newline final
        if not content.endswith("\n"):
            content = content + "\n"

        sanitized.append({"path": path, "content": content})

    # Se o modelo não respeitou paths, força um layout mínimo
    paths = {f["path"] for f in sanitized}
    base = name

    def ensure_file(p: str, default_content: str):
        nonlocal sanitized, paths
        if p not in paths:
            sanitized.append({"path": p, "content": default_content})
            paths.add(p)

    # policies/<name>.hcl mínimo
    pol_path = f"policies/{base}.hcl"
    ensure_file(
        pol_path,
        'path "secret/data/loja-online/*" {\n  capabilities = ["read"]\n}\n',
    )

    # scripts/<name>-approle.sh mínimo
    script_path = f"scripts/{base}-approle.sh"
    ensure_file(
        script_path,
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f'POLICY_NAME="{base}"\n'
        f'APPROLE_NAME="{base}"\n'
        f'POLICY_FILE="policies/{base}.hcl"\n\n'
        'echo "[INFO] Escrevendo política no Vault..."\n'
        'vault policy write "${POLICY_NAME}" "${POLICY_FILE}"\n\n'
        'echo\n'
        'echo "[INFO] Habilitando auth method approle (se necessário)..." \n'
        'vault auth enable approle 2>/dev/null || echo "[INFO] AppRole já habilitado."\n\n'
        'echo\n'
        'echo "[INFO] Criando/atualizando AppRole..."\n'
        'vault write "auth/approle/role/${APPROLE_NAME}" \\\n'
        '  token_policies="${POLICY_NAME}" \\\n'
        '  token_ttl="15m" \\\n'
        '  token_max_ttl="2h" \\\n'
        '  secret_id_ttl="10m" \\\n'
        '  secret_id_num_uses="1"\n\n'
        'echo\n'
        'echo "[INFO] Lendo RoleID..."\n'
        'ROLE_ID=$(vault read -format=json "auth/approle/role/${APPROLE_NAME}/role-id" | jq -r .data.role_id)\n'
        'echo "VAULT_ROLE_ID: ${ROLE_ID}"\n\n'
        'echo\n'
        'echo "[INFO] Gerando SecretID..."\n'
        'SECRET_ID=$(vault write -format=json -f "auth/approle/role/${APPROLE_NAME}/secret-id" | jq -r .data.secret_id)\n'
        'echo "VAULT_SECRET_ID: ${SECRET_ID}"\n\n'
        'echo\n'
        'echo "[IMPORTANTE] Configure VAULT_ROLE_ID e VAULT_SECRET_ID como variáveis de CI/CD no GitLab."\n',
    )

    # docs/<name>-rotacao.md mínimo
    doc_path = f"docs/{base}-rotacao.md"
    ensure_file(
        doc_path,
        "# Runbook de Rotação\n\n"
        "Este documento descreve como rotacionar segredos e o SecretID de AppRole no Vault.\n",
    )

    return sanitized


# ============================ Escrita ========================================
def write_files(root: pathlib.Path, files: List[Dict[str, str]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    print(f"\nSalvando em: {root}")
    for f in files:
        path = root / f["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f.get("content", ""))
        print(" -", f["path"])
    print("OK.")


# ============================ CLI ============================================
def build_parser():
    p = argparse.ArgumentParser(
        prog="iaops-vault",
        description="IAOps Vault — gera política HCL, script AppRole e runbook de rotação com IA.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pol = sub.add_parser("policy", help="Gerar pacote (policy + AppRole script + runbook) para Vault.")
    pol.add_argument("--name", required=True, help="Nome lógico (ex.: gitlab-loja-online).")
    pol.add_argument("--spec", required=True, help="Especificação em linguagem natural.")
    pol.add_argument("--root", default="./", help="Diretório raiz para salvar os arquivos.")
    pol.add_argument("--model", default="gemini-2.5-pro", help="Modelo Gemini (default: gemini-2.5-pro).")
    pol.add_argument("--temperature", type=float, default=0.2, help="Temperatura da geração.")
    return p


# ============================ main ===========================================
def main():
    ensure_api_key()
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd != "policy":
        print("Comando não suportado.", file=sys.stderr)
        sys.exit(2)

    instruction = build_instruction(args)
    print(f"\n[ policy ] {args.name}\nGerando com IA (modelo={args.model}, temp={args.temperature})\n")
    raw = call_model(instruction, model_name=args.model, temperature=args.temperature)

    try:
        payload = parse_ai_json(raw)
    except Exception:
        print("Resposta bruta da IA (início):\n", raw[:2000], "\n--- fim ---", file=sys.stderr)
        raise SystemExit("Falha ao decodificar JSON da IA.")

    files = payload.get("files") or []
    if not files:
        print("[WARN] IA não retornou arquivos; criando base mínima.", file=sys.stderr)
        files = []

    files = sanitize_files(files, args.name)

    root_base = pathlib.Path(args.root).expanduser().resolve()
    write_files(root_base, files)

    notes = payload.get("notes")
    if notes:
        print("\n[Notas da IA]\n" + str(notes))

    print("\nDicas:")
    print(f" - Revise o arquivo policies/{args.name}.hcl antes de aplicar:")
    print(f"     vault policy write <NOME_POLICY> policies/{args.name}.hcl")
    print(f" - Execute o script para criar/atualizar AppRole e obter ROLE_ID/SECRET_ID:")
    print(f"     bash scripts/{args.name}-approle.sh")

if __name__ == "__main__":
    main()
