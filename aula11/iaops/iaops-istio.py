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
    # remove fences se existirem
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
Você é um gerador de **JSON para Istio Traffic Shifting**.

SAÍDA (JSON estrito):
{
  "destinationRule": {
    "apiVersion": "networking.istio.io/v1",
    "kind": "DestinationRule",
    "metadata": {
      "name": "<SERVICE>",
      "namespace": "<NAMESPACE>",
      "labels": { "kiali_wizard": "traffic_shifting" }
    },
    "spec": {
      "host": "<SERVICE>.<NAMESPACE>.svc.cluster.local",
      "subsets": [
        { "name": "v1", "labels": { "version": "v1" } },
        { "name": "v2", "labels": { "version": "v2" } }
      ]
    }
  },
  "virtualService": {
    "apiVersion": "networking.istio.io/v1",
    "kind": "VirtualService",
    "metadata": {
      "name": "<SERVICE>",
      "namespace": "<NAMESPACE>",
      "labels": { "kiali_wizard": "traffic_shifting" }
    },
    "spec": {
      "hosts": [
        "<SERVICE>.<NAMESPACE>.svc.cluster.local"
      ],
      "http": [
        {
          "name": "default",
          "route": [
            {
              "destination": {
                "host": "<SERVICE>.<NAMESPACE>.svc.cluster.local",
                "subset": "v1"
              },
              "weight": 0
            },
            {
              "destination": {
                "host": "<SERVICE>.<NAMESPACE>.svc.cluster.local",
                "subset": "v2"
              },
              "weight": 100
            }
          ]
        }
      ]
    }
  }
}

REGRAS:
- NÃO gere YAML.
- NÃO inclua texto fora do JSON.
- Use apiVersion networking.istio.io/v1.
- O host deve ser FQDN do serviço.
- Cada subset deve ter name e labels.
- Weights devem ser inteiros >=0 e idealmente somar 100.
"""


# ============================ Helpers ================================
def fqdn(service: str, namespace: str) -> str:
    return f"{service}.{namespace}.svc.cluster.local"


def build_instruction(args: argparse.Namespace) -> str:
    lines = [
        "CONTEXT.TYPE=istio_traffic_pair",
        f"SERVICE={args.service}",
        f"NAMESPACE={args.namespace}",
        "",
        "Gere APENAS o JSON com destinationRule e virtualService.",
        "Não gere YAML.",
        "",
        "Especificação do usuário:",
        (args.spec or "").strip(),
    ]
    return "\n".join(lines)


def validate_subsets(subsets: Any) -> List[Dict[str, Any]]:
    if not isinstance(subsets, list) or not subsets:
        raise ValueError("destinationRule.spec.subsets inválido (esperado array não vazio).")

    out: List[Dict[str, Any]] = []
    for s in subsets:
        if not isinstance(s, dict):
            raise ValueError("subset inválido (não é objeto).")
        name = s.get("name")
        labels = s.get("labels")
        if not name or not isinstance(name, str):
            raise ValueError("subset inválido: faltando name.")
        if not labels or not isinstance(labels, dict):
            raise ValueError("subset inválido: faltando labels dict.")
        out.append({"name": name, "labels": labels})
    return out


def validate_routes(http: Any) -> List[Dict[str, Any]]:
    if not isinstance(http, list) or not http:
        raise ValueError("virtualService.spec.http inválido (esperado array).")
    block = http[0]
    if not isinstance(block, dict):
        raise ValueError("virtualService.spec.http[0] inválido.")
    route = block.get("route")
    if not isinstance(route, list) or not route:
        raise ValueError("virtualService.spec.http[0].route inválido.")
    # valida mínimos
    for r in route:
        if not isinstance(r, dict):
            raise ValueError("route item inválido.")
        dest = r.get("destination") or {}
        if not isinstance(dest, dict):
            raise ValueError("destination inválido.")
        if not dest.get("host") or not dest.get("subset"):
            raise ValueError("destination precisa de host e subset.")
        w = r.get("weight")
        if not isinstance(w, int) or w < 0:
            raise ValueError("weight inválido (esperado int >= 0).")
    return http


def normalize_weights(http: List[Dict[str, Any]]) -> None:
    """
    Se weights não somarem 100, não explode o script:
    apenas preserva o que veio.
    O Kiali aceita, mas o ideal é 100.
    """
    try:
        route = http[0]["route"]
        total = sum(int(r.get("weight", 0)) for r in route)
        # não força ajuste automático para não surpreender aluno
        # mas poderíamos normalizar aqui se quisesse
        _ = total
    except Exception:
        pass


def add_argocd_wave(meta: Dict[str, Any], wave: str) -> None:
    ann = meta.setdefault("annotations", {})
    ann["argocd.argoproj.io/sync-wave"] = wave


def ensure_kiali_label(meta: Dict[str, Any]) -> None:
    labels = meta.setdefault("labels", {})
    labels.setdefault("kiali_wizard", "traffic_shifting")


def build_destination_rule(service: str, namespace: str, subsets: List[Dict[str, Any]]) -> Dict[str, Any]:
    meta = {"name": service, "namespace": namespace}
    ensure_kiali_label(meta)
    add_argocd_wave(meta, "10")

    return {
        "apiVersion": "networking.istio.io/v1",
        "kind": "DestinationRule",
        "metadata": meta,
        "spec": {
            "host": fqdn(service, namespace),
            "subsets": subsets,
        },
    }


def build_virtual_service(service: str, namespace: str, http: List[Dict[str, Any]]) -> Dict[str, Any]:
    meta = {"name": service, "namespace": namespace}
    ensure_kiali_label(meta)
    add_argocd_wave(meta, "20")

    # força host único FQDN (Kiali-friendly)
    host_fqdn = fqdn(service, namespace)

    # garante nome default do http[0]
    if isinstance(http, list) and http:
        http0 = http[0]
        if isinstance(http0, dict):
            http0.setdefault("name", "default")

            # força host FQDN em cada destination
            route = http0.get("route")
            if isinstance(route, list):
                for r in route:
                    dest = r.get("destination")
                    if isinstance(dest, dict):
                        dest["host"] = host_fqdn

    normalize_weights(http)

    return {
        "apiVersion": "networking.istio.io/v1",
        "kind": "VirtualService",
        "metadata": meta,
        "spec": {
            "hosts": [host_fqdn],
            "http": http,
        },
    }


def dump_yaml_documents(docs: List[Dict[str, Any]]) -> str:
    return yaml.safe_dump_all(
        docs,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        width=120,
        allow_unicode=True,
    )


# ============================ CLI ================================
def build_parser():
    p = argparse.ArgumentParser(
        prog="iaops-istio",
        description="IAOps Istio Traffic Shifting — gera DestinationRule + VirtualService (Kiali-friendly).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    traffic = sub.add_parser("traffic", help="Gerar DestinationRule + VirtualService via IA.")
    traffic.add_argument("--service", required=True, help="Nome do Service Istio alvo.")
    traffic.add_argument("--namespace", required=True, help="Namespace do Service.")
    traffic.add_argument("--spec", required=True, help="Especificação em linguagem natural.")
    traffic.add_argument("--out-dir", default=".", help="Diretório de saída.")
    traffic.add_argument("--model", default="gemini-2.5-pro")
    traffic.add_argument("--temperature", type=float, default=0.2)

    return p


# ============================ main ================================
def main():
    ensure_api_key()
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd != "traffic":
        print("Comando não suportado.", file=sys.stderr)
        sys.exit(2)

    service = args.service.strip()
    namespace = args.namespace.strip()

    instruction = build_instruction(args)
    print(f"\n[ traffic.smart ] service={service} ns={namespace}")
    print(f"Gerando DestinationRule + VirtualService via IA (modelo={args.model}, temp={args.temperature})...\n")

    raw = call_model(instruction, model_name=args.model, temperature=args.temperature)

    try:
        data = parse_ai_json(raw)
    except Exception:
        print("Resposta bruta da IA (início):\n", raw[:2000], "\n--- fim ---", file=sys.stderr)
        raise SystemExit("Falha ao decodificar JSON da IA.")

    # valida estrutura base
    dr_in = data.get("destinationRule")
    vs_in = data.get("virtualService")
    if not isinstance(dr_in, dict) or not isinstance(vs_in, dict):
        raise SystemExit("JSON da IA inválido: esperado destinationRule e virtualService como objetos.")

    # tenta obter subsets do caminho correto
    dr_spec = dr_in.get("spec") or {}
    subsets_in = dr_spec.get("subsets")

    # fallback defensivo: caso IA tenha colocado "subsets" fora de spec
    if subsets_in is None:
        subsets_in = dr_in.get("subsets")

    try:
        subsets = validate_subsets(subsets_in)
    except Exception as e:
        print("Resposta bruta da IA (início):\n", raw[:2000], "\n--- fim ---", file=sys.stderr)
        raise SystemExit(f"Falha ao validar JSON da IA: {e}")

    # http/route
    vs_spec = vs_in.get("spec") or {}
    http_in = vs_spec.get("http")
    # fallback defensivo
    if http_in is None:
        http_in = vs_in.get("http")

    try:
        http = validate_routes(http_in)
    except Exception as e:
        print("Resposta bruta da IA (início):\n", raw[:2000], "\n--- fim ---", file=sys.stderr)
        raise SystemExit(f"Falha ao validar JSON da IA: {e}")

    # monta docs finais normalizados
    dr_doc = build_destination_rule(service, namespace, subsets)
    vs_doc = build_virtual_service(service, namespace, http)

    out_dir = pathlib.Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{service}-traffic.yaml"
    out_file.write_text(dump_yaml_documents([dr_doc, vs_doc]), encoding="utf-8")

    print("OK: DestinationRule + VirtualService gerados (Kiali-friendly).")
    print(f"Arquivo salvo em: {out_file}")
    print(" - Copie este arquivo para o repositório GitOps e faça commit/push.")
    print(" - O ArgoCD aplicará o DR antes do VS graças ao sync-wave.")


if __name__ == "__main__":
    main()
