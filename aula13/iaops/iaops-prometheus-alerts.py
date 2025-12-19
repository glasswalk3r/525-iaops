#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import os, sys, json, argparse, pathlib, warnings, time
from typing import Any, Dict, Tuple, Optional

# Silencia aviso ruidoso de versão do google.api_core (não impacta)
warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"google\.api_core\._python_version_support",
)

# ================ deps =================
try:
    from google import genai
    from google.genai import types
except ImportError:
    print("Falta google-genai. Rode: pip install -U google-genai", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("Falta pyyaml. Rode: pip install -U pyyaml", file=sys.stderr)
    sys.exit(1)

# ============== SYSTEM PROMPT ===========
SYSTEM_PROMPT = r"""
Você é um gerador de **Prometheus alert rules** e deve retornar SOMENTE um JSON no formato:

{
  "files": [
    {"path": "NAME.rules.yml", "content": "conteúdo YAML das alert rules"}
  ]
}

REGRAS:
- Gere **apenas** um arquivo NAME.rules.yml.
- Formato válido do Prometheus RuleFile:
  groups:
  - name: <GroupName>
    rules:
    - alert: <AlertName>
      expr: <PromQL>
      for: <dur>            # ex.: 1m, 5m, 10m
      labels:
        severity: <page|critical|warning|info>
      annotations:
        summary: "<resumo curto PT-BR>"
        description: "<detalhes PT-BR; pode usar {{ $labels.* }} e {{ $value }}>"
- Use boas práticas:
  * nomes de alerta em CamelCase sem espaços
  * inclua 'for' apropriado
  * inclua summary e description (PT-BR)
- Converta pedidos em linguagem natural (ex.: “máquina não responde há 1 minuto”) em PromQL adequado
  com métricas clássicas de Node Exporter / Prometheus (ex.: `up`, `node_cpu_seconds_total`, `node_memory_*`, `node_filesystem_*`).
- NÃO inclua Markdown fora do JSON. Retorne JSON estrito.
"""

# ================ Gemini (google-genai) ===============
def get_api_key() -> str:
    # docs do SDK: se ambos existirem, GOOGLE_API_KEY tende a prevalecer
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        print("Erro: defina GEMINI_API_KEY (ou GOOGLE_API_KEY).", file=sys.stderr)
        sys.exit(1)
    return key

def build_client(timeout_seconds: int) -> genai.Client:
    key = get_api_key()
    # HttpOptions.timeout é em MILISSEGUNDOS
    timeout_ms = max(1000, int(timeout_seconds) * 1000)
    return genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=timeout_ms),
    )

def call_model(
    client: genai.Client,
    instruction: str,
    model_name: str = "gemini-2.5-pro",
    temperature: float = 0.2,
    max_retries: int = 2,
) -> str:
    last_err: Optional[Exception] = None
    backoff = 1.5

    # config pode ser dict (mais tolerante) ou types.GenerateContentConfig
    config = {
        "system_instruction": SYSTEM_PROMPT,
        "temperature": temperature,
        "top_p": 0.9,
        "top_k": 40,
        "response_mime_type": "application/json",
    }

    for attempt in range(1, max_retries + 2):
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=instruction,
                config=config,
            )
            return (getattr(resp, "text", "") or "").strip()
        except Exception as e:
            last_err = e
            if attempt <= max_retries:
                time.sleep(backoff ** attempt)
            else:
                print(f"[ERRO] Falha ao chamar a IA: {e}", file=sys.stderr)
                break
    _ = last_err
    return ""

def parse_ai_json(text: str) -> Dict[str, Any]:
    if not text:
        raise ValueError("Resposta vazia da IA.")
    if text.startswith("```"):
        for chunk in text.split("```"):
            c = chunk.strip()
            if c.startswith("{") and c.endswith("}"):
                text = c
                break
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("Resposta não parece JSON.")
    return json.loads(text[i : j + 1])

# ============== helpers =================
def build_instruction(args: argparse.Namespace) -> str:
    ctx = (
        "CONTEXT.TYPE=prometheus_rules\n"
        f"RULESET_NAME={args.name}\n"
        f"DEFAULT_GROUP={args.group or args.name}\n"
        "Estilo: regras objetivas, PT-BR.\n"
    )
    spec = (args.spec or "").strip()
    return "\n\n".join([ctx, spec])

def parse_labels_kv(csv: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not csv:
        return out
    for pair in csv.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            print(f"[WARN] Ignorando label sem '=': {pair}", file=sys.stderr)
            continue
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out

def validate_rules_yaml(yaml_text: str) -> Tuple[int, int]:
    warns = 0
    errs = 0

    def warn(m: str):
        nonlocal warns
        warns += 1
        print(f"[WARN] {m}", file=sys.stderr)

    def err(m: str):
        nonlocal errs
        errs += 1
        print(f"[ERROR] {m}", file=sys.stderr)

    try:
        data = yaml.safe_load(yaml_text) or {}
    except Exception as e:
        err(f"YAML inválido: {e}")
        return warns, errs or 1

    if "groups" not in data:
        err("Arquivo não contém 'groups'.")
    else:
        groups = data.get("groups") or []
        if not isinstance(groups, list) or not groups:
            err("'groups' vazio ou inválido.")
        else:
            for g in groups:
                if "name" not in g:
                    warn("Group sem 'name'.")
                if "rules" not in g or not g["rules"]:
                    warn("Group sem 'rules'.")
                for r in g.get("rules", []):
                    if "alert" not in r:
                        warn("Regra sem 'alert'.")
                    if "expr" not in r:
                        warn("Regra sem 'expr'.")
                    if "annotations" not in r:
                        warn("Regra sem 'annotations'.")
    return warns, errs

def inject_labels_and_severity(
    yaml_text: str,
    extra_labels: Dict[str, str],
    default_severity: str | None,
) -> str:
    """
    Reescreve o YAML **somente** se houver injeção de labels ou severity.
    Caso contrário, retorna exatamente o texto original (preserva acentuação/estilo).
    """
    if not extra_labels and not default_severity:
        return yaml_text if yaml_text.endswith("\n") else (yaml_text + "\n")

    try:
        data = yaml.safe_load(yaml_text) or {}
    except Exception:
        return yaml_text if yaml_text.endswith("\n") else (yaml_text + "\n")

    for group in data.get("groups", []):
        for rule in group.get("rules", []):
            labels = rule.get("labels") or {}
            for k, v in (extra_labels or {}).items():
                labels[k] = v
            if default_severity and "severity" not in labels:
                labels["severity"] = default_severity
            rule["labels"] = labels

    return yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )

def write_file(path: pathlib.Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content.endswith("\n") else (content + "\n"), encoding="utf-8")
    print(f"\nArquivo salvo: {path}")

# ============== CLI =====================
def build_parser():
    p = argparse.ArgumentParser(
        prog="iaops-prometheus-alerts",
        description="Gera arquivos de alertas do Prometheus a partir de uma especificação em linguagem natural (sempre via IA Gemini).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("alerts", help="Gerar um .rules.yml com alertas do Prometheus (via IA).")
    gen.add_argument("--name", required=True, help="Nome lógico do ruleset (usado em arquivo).")
    gen.add_argument("--group", default=None, help="Nome do grupo de regras (default: --name).")
    gen.add_argument("--spec", required=True, help="Especificação em linguagem natural (PT-BR).")
    gen.add_argument("--labels", default="", help="Labels extras (CSV key=value, ex.: team=devops,env=prod).")
    gen.add_argument(
        "--severity",
        choices=["page", "critical", "warning", "info"],
        default=None,
        help="Severity default se a regra não definir.",
    )
    gen.add_argument("--output", default="./", help="Diretório/arquivo destino (se terminar com .yml usa esse nome).")
    gen.add_argument("--model", default="gemini-2.5-pro")
    gen.add_argument("--temperature", type=float, default=0.2)
    gen.add_argument("--timeout", type=int, default=40, help="Timeout (segundos) aplicado no client.")
    return p

# ============== main ====================
def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd != "alerts":
        print("Comando não suportado.", file=sys.stderr)
        sys.exit(2)

    client = build_client(timeout_seconds=args.timeout)

    instruction = build_instruction(args)
    print(f"\n[ alerts ] {args.name}\nGerando com IA (modelo={args.model}, temp={args.temperature}, timeout={args.timeout}s)\n")

    raw = call_model(
        client=client,
        instruction=instruction,
        model_name=args.model,
        temperature=args.temperature,
    )

    # Sempre IA — se falhar, erra e explica
    try:
        payload = parse_ai_json(raw)
    except Exception:
        print("Resposta bruta da IA (início):\n", (raw or "")[:2000], "\n--- fim ---", file=sys.stderr)
        raise SystemExit(
            "Falha ao decodificar JSON da IA. O script sempre usa IA; ajuste seu --spec e tente novamente."
        )

    files = payload.get("files") or []
    first = next((f for f in files if str(f.get("path", "")).endswith(".rules.yml")), None)
    if not first:
        raise SystemExit("A IA não retornou um arquivo .rules.yml. Ajuste seu --spec e tente novamente.")

    content = first.get("content", "")
    if not content.strip():
        raise SystemExit("A IA retornou conteúdo vazio para o .rules.yml. Ajuste seu --spec e tente novamente.")

    # Validar YAML (sem reescrever; apenas checa)
    w, e = validate_rules_yaml(content)
    if e > 0:
        print("Conteúdo da IA:\n", content[:2000], "\n--- fim ---", file=sys.stderr)
        raise SystemExit("YAML inválido gerado pela IA. Ajuste seu --spec e tente novamente.")

    # Injeção opcional de labels e severity (aqui sim pode reserializar, com allow_unicode)
    extra_labels = parse_labels_kv(args.labels)
    content_final = inject_labels_and_severity(content, extra_labels, args.severity)

    # Caminho de saída
    out = pathlib.Path(args.output)
    desired_name = f"{args.name}.rules.yml"
    out_path = out if out.suffix in (".yml", ".yaml") else (out / desired_name)

    write_file(out_path.resolve(), content_final)

    print("\nDicas:")
    print(" - Copie o arquivo de regras para o diretório /etc/prometheus (ou o diretório de rules da sua distro).")
    print(" - Referencie no arquivo prometheus.yml:")
    print("   rule_files:")
    print(f"     - {out_path}")
    print(" - Recarregue o serviço do Prometheus (ou faça reload via endpoint /-/reload, se habilitado).")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Execução interrompida pelo usuário.")
