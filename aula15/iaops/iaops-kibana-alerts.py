#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import os
import sys
import json
import time
import argparse
import pathlib
import re
from typing import Any, Dict, Optional

import warnings
warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"google\.api_core\._python_version_support",
)

# ==== deps ===================================================================
try:
    from google import genai
    from google.genai import types
except ImportError:
    print("Falta google-genai. Rode: pip install -U google-genai", file=sys.stderr)
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Falta requests. Rode: pip install -U requests", file=sys.stderr)
    sys.exit(1)

# ==== SYSTEM PROMPT ==========================================================
SYSTEM_PROMPT = """
Você é um gerador de regras de alerta do Kibana (Stack 8.x+) para o tipo .es-query.

RETORNE APENAS O JSON:
{
  "files": [
    {"path": "NAME.rule.json", "content": "{ ... o corpo da regra Kibana em JSON ... }"}
  ],
  "notes": "opcional"
}

Regras IMPORTANTES:
- Gere SOMENTE UMA regra .es-query (alerta baseado em query Elasticsearch).
- O campo "content" DEVE SER:
  * uma string JSON válida do corpo da regra (sem wrapper de API), OU
  * um objeto JSON (que será tratado como corpo da regra).
- Não use aspas triplas (três aspas duplas seguidas) nas strings.
- Gere JSON estritamente válido.
- Escreva mensagens (message / name / tags) em PT-BR.
- Use params.searchType = "esQuery" quando usar DSL no campo esQuery.
- Se o usuário pedir KQL, você pode embutir a KQL em um query_string.query dentro de esQuery.
- Ajuste timeWindowSize, timeWindowUnit, threshold, schedule.interval
  de acordo com o enunciado (ex.: 2 minutos, 1 minuto, > 5 eventos etc.).
- Não escreva nada fora do JSON de controle (files/notes).
""".strip()

# ==== Schema do "wrapper" que o script espera ================================
RESPONSE_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": ["string", "object"]},
                },
                "required": ["path", "content"],
            },
        },
        "notes": {"type": ["string", "null"]},
    },
    "required": ["files"],
}

# ==== Gemini helpers =========================================================
def ensure_api_key() -> str:
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        print("Erro: defina GEMINI_API_KEY (ou GOOGLE_API_KEY).", file=sys.stderr)
        sys.exit(1)
    return key

def call_model(
    instruction: str,
    model_name: str = "gemini-2.5-pro",
    temperature: float = 0.2,
    timeout: int = 40,
    max_retries: int = 2,
) -> str:
    """
    SDK novo: genai.Client() + client.models.generate_content(...)

    Observação:
      - HttpOptions.timeout é em MILISSEGUNDOS (ms).
      - --timeout do script continua em SEGUNDOS (s) e aqui convertemos para ms.
    """
    api_key = ensure_api_key()
    client = genai.Client(api_key=api_key)

    timeout_ms = int(timeout * 1000)
    last_err: Optional[Exception] = None
    backoff = 1.5

    for attempt in range(1, max_retries + 2):
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=instruction,
                config={
                    "system_instruction": SYSTEM_PROMPT,
                    "temperature": temperature,
                    "top_p": 0.9,
                    "top_k": 40,
                    "response_mime_type": "application/json",
                    "response_json_schema": RESPONSE_JSON_SCHEMA,
                    "http_options": types.HttpOptions(timeout=timeout_ms),
                },
            )

            # Quando há schema, normalmente vem preenchido em resp.parsed
            parsed = getattr(resp, "parsed", None)
            if parsed is not None:
                return json.dumps(parsed, ensure_ascii=False)

            return (getattr(resp, "text", "") or "").strip()

        except Exception as e:
            last_err = e
            if attempt <= max_retries:
                time.sleep(backoff ** attempt)
            else:
                print(f"[ERRO] Falha ao chamar a IA: {e}", file=sys.stderr)
                break

    return ""

# ==== JSON helper com correção de aspas triplas em esQuery ===================
def _fix_triple_quoted_esquery(payload: str) -> str:
    """
    Corrige casos onde o campo esQuery vem envolvido por aspas triplas,
    transformando em uma string JSON normal.
    """
    pattern = r'("esQuery"\s*:\s*)"""(.*?)"""'

    def repl(match: re.Match) -> str:
        prefix = match.group(1)
        inner = match.group(2).strip()
        escaped = json.dumps(inner)  # vira uma string JSON válida
        return f"{prefix}{escaped}"

    return re.sub(pattern, repl, payload, flags=re.DOTALL)

def parse_ai_json(text: str) -> Dict[str, Any]:
    if not text:
        raise ValueError("Resposta vazia da IA.")

    # Remove cercas de código, se tiver
    if text.startswith("```"):
        for chunk in text.split("```"):
            c = chunk.strip()
            if c.startswith("{") and c.endswith("}"):
                text = c
                break

    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("Resposta não parece JSON.")

    core = text[i:j + 1]

    # 1ª tentativa: JSON direto
    try:
        return json.loads(core)
    except Exception:
        # 2ª tentativa: corrigir aspas triplas em esQuery
        fixed = _fix_triple_quoted_esquery(core)
        return json.loads(fixed)

# ==== Prompt builder =========================================================
def build_instruction(args: argparse.Namespace) -> str:
    ctx = (
        "CONTEXT.TYPE=kibana_alert_rule\n"
        f"RULE_NAME={args.name}\n"
        f"INDEX_PATTERN={args.index}\n"
        "Estilo: regras objetivas, PT-BR, tipo .es-query.\n"
    )
    spec = (args.spec or "").strip()
    return "\n\n".join([ctx, spec])

# ==== Normalização da regra ==================================================
def _load_content_as_dict(content: Any) -> Dict[str, Any]:
    """
    content pode vir como string JSON ou já como dict.
    Normalizamos para dict.
    """
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        txt = content.strip()
        if txt.startswith("{") and txt.endswith("}"):
            try:
                return json.loads(txt)
            except Exception as e:
                print(f"[WARN] Falha ao parsear content como JSON: {e}", file=sys.stderr)
                return {}
        return {}
    return {}

def _normalize_time_window_unit(params: Dict[str, Any]) -> None:
    """
    Converte timeWindowUnit textual (minutes, hours, etc.) para m/s/h.
    """
    unit = params.get("timeWindowUnit", "m")
    unit = str(unit).strip().lower()

    if unit in ("m", "min", "mins", "minute", "minutes"):
        params["timeWindowUnit"] = "m"
    elif unit in ("s", "sec", "secs", "second", "seconds"):
        params["timeWindowUnit"] = "s"
    elif unit in ("h", "hr", "hrs", "hour", "hours"):
        params["timeWindowUnit"] = "h"
    else:
        params["timeWindowUnit"] = "m"

def _normalize_schedule(schedule: Dict[str, Any]) -> Dict[str, Any]:
    """
    Garante que schedule.interval esteja no formato aceito (ex.: '1m').
    """
    interval = schedule.get("interval") or "1m"
    interval = str(interval).strip().lower()

    if "minute" in interval:
        m = re.match(r"(\d+)", interval)
        num = m.group(1) if m else "1"
        interval = f"{num}m"

    schedule["interval"] = interval
    return schedule

def _build_fallback_esquery_from_kql(kql: str) -> Dict[str, Any]:
    """
    Constrói um esQuery mínimo com 'query' na raiz, usando um query_string com KQL.
    """
    return {
        "query": {
            "bool": {
                "filter": [
                    {
                        "query_string": {
                            "query": kql,
                            "analyze_wildcard": True,
                        }
                    }
                ]
            }
        }
    }

def _normalize_threshold(params: Dict[str, Any]) -> None:
    """
    Garante que params['threshold'] seja uma lista de números.
    Corrige casos como ['>', 5] para [5], converte strings numéricas, etc.
    """
    raw = params.get("threshold")

    if isinstance(raw, list):
        nums = []
        for item in raw:
            if isinstance(item, (int, float)):
                nums.append(item)
            elif isinstance(item, str):
                try:
                    nums.append(float(item))
                except Exception:
                    continue
        if not nums:
            nums = [5]
        params["threshold"] = nums
        return

    if isinstance(raw, (int, float)):
        params["threshold"] = [raw]
        return

    if isinstance(raw, str):
        try:
            params["threshold"] = [float(raw)]
        except Exception:
            params["threshold"] = [5]
        return

    params["threshold"] = [5]

def normalize_es_query_rule(
    rule: Dict[str, Any],
    name: str,
    index_pattern: str,
    connector_id: Optional[str],
) -> Dict[str, Any]:
    """
    Normaliza a regra para o formato aceito pela API de alerting do Kibana:
    - name correto
    - params.index com o padrão desejado
    - groupBy = "all"
    - timeField = "@timestamp"
    - searchType = "esQuery"
    - esQuery sempre definido como string JSON cujo conteúdo TEM 'query' na raiz
    - threshold sempre lista de números
    - remove campos não suportados (filterKuery, esqlQuery, kqlQuery)
    - normaliza timeWindowUnit e schedule.interval
    - remove alertTypeId / notify_when / notifyWhen / throttle / producer no nível da regra
    - se tiver connector_id, sobrescreve completamente actions com uma action padrão.
    """
    if not isinstance(rule, dict):
        rule = {}

    if not rule.get("name"):
        rule["name"] = name

    rule.setdefault("rule_type_id", ".es-query")
    rule.setdefault("consumer", "alerts")
    rule.setdefault("enabled", True)
    rule.setdefault("tags", [])
    if "iaops" not in rule["tags"]:
        rule["tags"].append("iaops")

    rule.pop("producer", None)

    schedule = rule.get("schedule") or {}
    rule["schedule"] = _normalize_schedule(schedule)

    params = rule.get("params") or {}
    params["index"] = [index_pattern]
    params.setdefault("searchType", "esQuery")

    if params.get("searchType") == "esQuery":
        es_query_raw = params.get("esQuery")
        safe_es: Optional[Dict[str, Any]] = None

        if es_query_raw:
            if isinstance(es_query_raw, dict):
                es_obj = es_query_raw
            elif isinstance(es_query_raw, str):
                try:
                    es_obj = json.loads(es_query_raw)
                except Exception:
                    es_obj = None
            else:
                es_obj = None

            if isinstance(es_obj, dict):
                if "query" not in es_obj:
                    es_obj = {"query": es_obj}
                if "query" in es_obj:
                    safe_es = es_obj

        if safe_es is None:
            kql = params.get("filterKuery") or "*"
            safe_es = _build_fallback_esquery_from_kql(kql)

        params["esQuery"] = json.dumps(safe_es)

        for key in ["filterKuery", "esqlQuery", "kqlQuery"]:
            params.pop(key, None)

    params.setdefault("timeWindowSize", 2)
    params.setdefault("timeWindowUnit", "m")
    _normalize_time_window_unit(params)

    params.setdefault("thresholdComparator", ">")
    _normalize_threshold(params)

    params.setdefault("size", 100)
    params.setdefault("aggType", "count")

    params["groupBy"] = "all"
    params.setdefault("timeField", "@timestamp")

    rule["params"] = params

    rule.pop("alertTypeId", None)
    for k in ("notify_when", "notifyWhen", "throttle"):
        rule.pop(k, None)

    if connector_id:
        rule["actions"] = [
            {
                "group": "query matched",
                "id": connector_id,
                "params": {
                    "level": "info",
                    "message": (
                        "Alerta gerado pela regra '{{rule.name}}' em Kibana. "
                        "Verifique os eventos relacionados e o contexto no dashboard."
                    ),
                },
                "frequency": {
                    "summary": False,
                    "notify_when": "onActionGroupChange",
                    "throttle": None,
                },
            }
        ]
    else:
        rule.pop("actions", None)

    return rule

# ==== Escrita em disco =======================================================
def write_file(path: pathlib.Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content = content + "\n"
    path.write_text(content, encoding="utf-8")
    print(f"\nArquivo salvo: {path}")

# ==== Kibana API =============================================================
def create_kibana_rule(kibana_url: str, auth_basic_b64: str, rule_body: Dict[str, Any]) -> None:
    url = kibana_url.rstrip("/") + "/api/alerting/rule"
    headers = {
        "kbn-xsrf": "true",
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth_basic_b64}",
    }
    resp = requests.post(url, headers=headers, json=rule_body, verify=False)
    if resp.status_code not in (200, 201):
        print(f"[ERRO] Kibana retornou {resp.status_code}: {resp.text}", file=sys.stderr)
    else:
        try:
            data = resp.json()
        except Exception:
            data = {}
        rid = data.get("id") or "<sem id>"
        print(f"Regra criada no Kibana com id: {rid}")

# ==== CLI ====================================================================
def build_parser():
    p = argparse.ArgumentParser(
        prog="iaops-kibana-alerts",
        description="Gera regras de alerta (.es-query) para Kibana via IA e opcionalmente cria o alerta via API.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("alerts", help="Gerar uma regra .es-query (Kibana) via IA.")
    gen.add_argument("--name", required=True, help="Nome lógico do alerta/regra.")
    gen.add_argument("--index", required=True, help="Padrão do índice (ex.: .ds-logs-system.auth-*)")
    gen.add_argument("--spec", required=True, help="Especificação em linguagem natural (PT-BR).")
    gen.add_argument("--connector-id", default=None, help="ID de um conector já existente (ex.: kibana-notifications).")
    gen.add_argument("--kibana-url", default=None, help="URL do Kibana para criar a regra (ex.: http://10.10.0.13:5601)")
    gen.add_argument("--auth-basic-base64", default=None, help="Credencial Basic em Base64 (ex.: echo -n user:pass | base64).")
    gen.add_argument("--output", default="./", help="Diretório de saída para o arquivo .rule.json")
    gen.add_argument("--model", default="gemini-2.5-pro")
    gen.add_argument("--temperature", type=float, default=0.2)
    gen.add_argument("--timeout", type=int, default=40)
    return p

# ==== main ===================================================================
def main():
    # valida chave cedo
    _ = ensure_api_key()

    parser = build_parser()
    args = parser.parse_args()

    if args.cmd != "alerts":
        print("Comando não suportado.", file=sys.stderr)
        sys.exit(2)

    instruction = build_instruction(args)
    print(f"\n[ alerts ] {args.name}\nGerando com IA (modelo={args.model}, temp={args.temperature})\n")

    raw = call_model(
        instruction,
        model_name=args.model,
        temperature=args.temperature,
        timeout=args.timeout,
    )

    try:
        payload = parse_ai_json(raw)
    except Exception:
        print("Resposta bruta da IA (início):\n", (raw or "")[:2000], "\n--- fim ---", file=sys.stderr)
        raise SystemExit(
            "Falha ao decodificar JSON da IA. O script sempre usa IA; ajuste seu --spec e tente novamente."
        )

    files = payload.get("files") or []
    first = next((f for f in files if f.get("path", "").endswith(".rule.json")), None)
    if not first:
        raise SystemExit("A IA não retornou um arquivo .rule.json. Ajuste seu --spec e tente novamente.")

    content_raw = first.get("content", "")
    rule_body = _load_content_as_dict(content_raw)
    if not rule_body:
        if isinstance(content_raw, str) and not content_raw.strip():
            raise SystemExit("A IA retornou conteúdo vazio para o .rule.json. Ajuste seu --spec e tente novamente.")
        try:
            rule_body = json.loads(content_raw)
        except Exception as e:
            print(f"[ERRO] Não foi possível interpretar o 'content' como JSON: {e}", file=sys.stderr)
            raise SystemExit("Falha ao interpretar o conteúdo do .rule.json retornado pela IA.")

    rule_body = normalize_es_query_rule(
        rule=rule_body,
        name=args.name,
        index_pattern=args.index,
        connector_id=args.connector_id,
    )

    out_dir = pathlib.Path(args.output).expanduser().resolve()
    out_path = out_dir / f"{args.name}.rule.json"
    write_file(out_path, json.dumps(rule_body, ensure_ascii=False, indent=2))

    if args.kibana_url and args.auth_basic_base64:
        print("\nCriando alerta no Kibana (via API)...")
        create_kibana_rule(
            kibana_url=args.kibana_url,
            auth_basic_b64=args.auth_basic_base64,
            rule_body=rule_body,
        )
    else:
        print("\n(Parâmetros --kibana-url ou --auth-basic-base64 não informados; apenas o arquivo foi gerado.)")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Execução interrompida pelo usuário.")
