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
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    print("Falta PyYAML. Rode: pip install -U pyyaml", file=sys.stderr)
    sys.exit(1)

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
    if text.startswith("```"):
        for chunk in text.split("```"):
            c = chunk.strip()
            if c.startswith("{") and c.endswith("}"):
                text = c
                break

    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("Resposta não parece JSON válido.")
    return json.loads(text[i:j + 1])


# ============================ SYSTEM PROMPT ==========================
SYSTEM_PROMPT = r"""
Você é um gerador de **SNIPPETS Argo Rollouts**.

SAÍDA (JSON estrito):
{
  "replicas": 3,
  "strategy": {
    "canary": {
      "steps": [
        {"setWeight": 30},
        {"pause": {"duration": "30s"}},
        {"setWeight": 50},
        {"pause": {"duration": "30s"}},
        {"setWeight": 90},
        {"pause": {"duration": "30s"}}
      ]
    }
  }
}

REGRAS:
- NÃO gere YAML.
- NÃO inclua nada além desse JSON.
- "replicas" deve ser inteiro >= 1.
- "strategy.canary.steps" deve refletir os steps pedidos pelo usuário.
- Durações devem estar como string (ex.: "30s", "60s").
"""


# ============================ Helpers ================================
def build_instruction(args: argparse.Namespace) -> str:
    lines = [
        "CONTEXT.TYPE=rollout_strategy_patch",
        f"NAME={args.name}",
        "",
        "Gere APENAS o JSON com replicas e strategy.canary.steps.",
        "Não gere YAML.",
        "",
        "Especificação do usuário:",
        (args.spec or "").strip(),
    ]
    return "\n".join(lines)


def load_yaml_documents(path: pathlib.Path) -> List[Dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    docs = list(yaml.safe_load_all(raw))
    return [d for d in docs if d is not None]


def dump_yaml_documents(docs: List[Dict[str, Any]]) -> str:
    return yaml.safe_dump_all(
        docs,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        width=120,
        allow_unicode=True,
    )


def find_doc_by_kind_and_name(
    docs: List[Dict[str, Any]],
    kind: str,
    name: str
) -> Optional[int]:
    for i, d in enumerate(docs):
        if not isinstance(d, dict):
            continue
        if d.get("kind") != kind:
            continue
        meta = d.get("metadata") or {}
        if meta.get("name") == name:
            return i
    return None


def apply_patch_to_rollout(doc: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    spec = doc.setdefault("spec", {})

    replicas = patch.get("replicas")
    if isinstance(replicas, int) and replicas >= 1:
        spec["replicas"] = replicas

    strategy = patch.get("strategy")
    if isinstance(strategy, dict) and strategy:
        spec["strategy"] = strategy

    doc["spec"] = spec
    return doc


def convert_deployment_to_rollout(deploy: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    meta = deploy.get("metadata") or {}
    dspec = deploy.get("spec") or {}
    selector = dspec.get("selector") or {}
    template = dspec.get("template") or {}

    rollout: Dict[str, Any] = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Rollout",
        "metadata": {
            "name": meta.get("name"),
        },
        "spec": {
            # selector e template vêm do Deployment
            "selector": selector if selector else {"matchLabels": (template.get("metadata") or {}).get("labels", {})},
            "template": template,
        },
    }

    # preserva namespace se existir
    if meta.get("namespace"):
        rollout["metadata"]["namespace"] = meta["namespace"]

    # preserva labels se existir
    if meta.get("labels"):
        rollout["metadata"]["labels"] = meta["labels"]

    # replicas: usa patch se vier da IA,
    # senão tenta herdar do Deployment, senão default 1
    replicas = patch.get("replicas")
    if isinstance(replicas, int) and replicas >= 1:
        rollout["spec"]["replicas"] = replicas
    else:
        if isinstance(dspec.get("replicas"), int):
            rollout["spec"]["replicas"] = dspec["replicas"]
        else:
            rollout["spec"]["replicas"] = 1

    # strategy: vem do patch da IA se existir
    strategy = patch.get("strategy")
    if isinstance(strategy, dict) and strategy:
        rollout["spec"]["strategy"] = strategy

    return rollout


# ============================ CLI ================================
def build_parser():
    p = argparse.ArgumentParser(
        prog="iaops-argocd",
        description="IAOps Argo Rollouts — injeta strategy canary em Deployment/Rollout existente.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    roll = sub.add_parser("rollout", help="Gerar strategy canary via IA e aplicar/convert em um YAML existente.")
    roll.add_argument("--yaml-file", required=True, help="Caminho do YAML existente (pode ser multi-doc).")
    roll.add_argument("--name", required=True, help="metadata.name do Deployment/Rollout alvo.")
    roll.add_argument("--spec", required=True, help="Especificação em linguagem natural (replicas/steps/pausas).")
    roll.add_argument("--out-dir", default=".", help="Diretório de saída.")
    roll.add_argument("--model", default="gemini-2.5-pro")
    roll.add_argument("--temperature", type=float, default=0.2)

    return p


# ============================ main ================================
def main():
    ensure_api_key()
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd != "rollout":
        print("Comando não suportado.", file=sys.stderr)
        sys.exit(2)

    yaml_path = pathlib.Path(args.yaml_file).expanduser().resolve()
    if not yaml_path.exists():
        print(f"Erro: arquivo não encontrado: {yaml_path}", file=sys.stderr)
        sys.exit(1)

    instruction = build_instruction(args)
    print(f"\n[ rollout.smart ] {args.name}")
    print(f"Arquivo alvo: {yaml_path}")
    print(f"Gerando strategy via IA (modelo={args.model}, temp={args.temperature})...\n")

    raw = call_model(instruction, model_name=args.model, temperature=args.temperature)

    try:
        patch = parse_ai_json(raw)
    except Exception:
        print("Resposta bruta da IA (início):\n", raw[:2000], "\n--- fim ---", file=sys.stderr)
        raise SystemExit("Falha ao decodificar JSON da IA.")

    if "strategy" not in patch and "replicas" not in patch:
        raise SystemExit("IA não retornou replicas/strategy no JSON.")

    docs = load_yaml_documents(yaml_path)

    rollout_idx = find_doc_by_kind_and_name(docs, "Rollout", args.name)
    deploy_idx = find_doc_by_kind_and_name(docs, "Deployment", args.name)

    out_dir = pathlib.Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------
    # Caso 1: Atualiza Rollout
    # -------------------------
    if rollout_idx is not None:
        docs[rollout_idx] = apply_patch_to_rollout(docs[rollout_idx], patch)

        out_file = out_dir / yaml_path.name
        out_file.write_text(dump_yaml_documents(docs), encoding="utf-8")

        print("OK: Rollout atualizado.")
        print(f"Arquivo atualizado salvo em: {out_file}")
        print(f" - Copie o arquivo {out_file} para o repositório da aplicação, faça commit/push e deixe o ArgoCD reconciliar.")        
        return

    # -------------------------
    # Caso 2: Converte Deployment -> Rollout
    # -------------------------
    if deploy_idx is not None:
        deploy_doc = docs[deploy_idx]
        rollout_doc = convert_deployment_to_rollout(deploy_doc, patch)

        # Monta docs de saída:
        # - inclui o Rollout novo
        # - inclui todos os outros docs exceto o Deployment original alvo
        out_docs: List[Dict[str, Any]] = [rollout_doc]
        for i, d in enumerate(docs):
            if i == deploy_idx:
                continue
            out_docs.append(d)

        out_file = out_dir / f"{args.name}.yaml"
        out_file.write_text(dump_yaml_documents(out_docs), encoding="utf-8")

        print("OK: Deployment convertido para Rollout com strategy canary.")
        print(f"Novo arquivo salvo em: {out_file}")
        print("\nDica:")
        print(f" - Copie o arquivo {out_file} para o repositório da aplicação, faça commit/push e deixe o ArgoCD reconciliar.")
        return

    # -------------------------
    # Caso 3: Não encontrou nada
    # -------------------------
    raise SystemExit(
        f"Não encontrei um documento kind: Deployment OU Rollout com metadata.name={args.name} em {yaml_path}"
    )


if __name__ == "__main__":
    main()
