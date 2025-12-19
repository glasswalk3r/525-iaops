#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import os
import sys
import json
import time
import argparse
import warnings
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone

# ==== Warnings (inclusive RequestsDependencyWarning) ==========================
warnings.filterwarnings("ignore", category=FutureWarning)
try:
    # Silenciar: /usr/lib/python3/dist-packages/requests/__init__.py: RequestsDependencyWarning
    from requests.exceptions import RequestsDependencyWarning  # type: ignore
    warnings.filterwarnings("ignore", category=RequestsDependencyWarning)
except Exception:
    pass

import pandas as pd

# matplotlib: backend não-interativo por padrão
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ==== Gemini SDK ==============================================================
try:
    import google.generativeai as genai
except ImportError:
    print("Falta google-generativeai. Rode: pip install -U google-generativeai", file=sys.stderr)
    sys.exit(1)

# ==== Elasticsearch client ====================================================
try:
    from elasticsearch import Elasticsearch
    from elasticsearch.exceptions import NotFoundError, ConnectionError as ESConnectionError
except ImportError:
    print("Falta elasticsearch. Rode: pip install -U 'elasticsearch>=8.12,<9'", file=sys.stderr)
    sys.exit(1)

# ====================== Helpers ==============================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def _ensure_gemini_config(api_key: Optional[str] = None):
    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Defina GEMINI_API_KEY (ou GOOGLE_API_KEY).")
    genai.configure(api_key=key)

# ====================== ES: conexão / descoberta =============================
def get_es_client(es_url: str,
                  username: Optional[str],
                  password: Optional[str],
                  api_key: Optional[str],
                  ca_path: Optional[str],
                  verify_certs: bool = True,
                  ssl_no_hostname_check: bool = False) -> Elasticsearch:
    """
    Prioriza API Key se fornecida; caso contrário, Basic Auth.
    """
    kwargs: Dict[str, Any] = {
        "hosts": [es_url],
        "verify_certs": verify_certs,
    }
    if ca_path:
        kwargs["ca_certs"] = ca_path
    if ssl_no_hostname_check:
        # apenas para diagnóstico de hostname; evite em produção
        kwargs["ssl_assert_hostname"] = False

    if api_key:
        if ":" in api_key:
            api_id, api_key_val = api_key.split(":", 1)
            kwargs["api_key"] = (api_id, api_key_val)
        else:
            kwargs["api_key"] = api_key
    else:
        if not username:
            username = "elastic"
        kwargs["basic_auth"] = (username, password)

    return Elasticsearch(**kwargs)

def ping_or_info(es: Elasticsearch) -> bool:
    try:
        if es.ping():
            return True
        _ = es.info()
        return True
    except Exception as e:
        print(f"[ERRO] Falha ao conectar ao Elasticsearch: {repr(e)}")
        return False

def discover_indices(es: Elasticsearch, match: str, include_hidden: bool, limit: int) -> List[str]:
    """
    Usa _cat/indices para listar índices que correspondem ao padrão `match`.
    Ex.: match='.ds-logs-*' ou '*' para todos.
    """
    try:
        cats = es.cat.indices(index=match, format="json", expand_wildcards="all" if include_hidden else "open")
        idxs = sorted({row.get("index") for row in cats if row.get("index")})
        if limit and len(idxs) > limit:
            idxs = idxs[:limit]
        return idxs
    except Exception as e:
        print(f"[ERRO] Não foi possível listar índices (match='{match}'): {e}")
        return []

# ====================== Busca / agregações ===================================
def build_time_query(hours: float, extra_query: Optional[str]) -> Dict[str, Any]:
    end = now_utc()
    start = end - timedelta(hours=float(hours))
    q: Dict[str, Any] = {
        "bool": {
            "filter": [
                {"range": {"@timestamp": {"gte": to_iso(start), "lte": to_iso(end)}}}
            ]
        }
    }
    if extra_query:
        q["bool"]["must"] = [{"query_string": {"query": extra_query, "default_field": "*"}}]
    return q

def fetch_logs(es: Elasticsearch, index: str, hours: float, size: int, extra_query: Optional[str]) -> List[Dict[str, Any]]:
    q = build_time_query(hours, extra_query)
    try:
        resp = es.search(
            index=index,
            size=size,
            sort=[{"@timestamp": {"order": "desc"}}],
            query=q,
            _source_includes=[
                "@timestamp",
                "message", "message.original",
                "log.level", "log",
                "host.name", "host.hostname", "host",
                "service.name", "service",
                "data_stream.dataset", "data_stream",
                "event.dataset",
            ],
        )
        hits = resp.get("hits", {}).get("hits", []) or []
        return [h.get("_source", {}) for h in hits]
    except NotFoundError:
        print(f"Índice {index} não encontrado.")
        return []
    except ESConnectionError as e:
        print(f"Erro de conexão: {e}")
        return []
    except Exception as e:
        print(f"Erro ao buscar logs: {e}")
        return []

def fetch_volume_agg(es: Elasticsearch, index: str, hours: float, step: str, extra_query: Optional[str]) -> pd.DataFrame:
    q = build_time_query(hours, extra_query)
    try:
        resp = es.search(
            index=index,
            size=0,
            query=q,
            aggs={
                "by_time": {
                    "date_histogram": {"field": "@timestamp", "fixed_interval": step, "min_doc_count": 0}
                }
            }
        )
        buckets = resp.get("aggregations", {}).get("by_time", {}).get("buckets", []) or []
        df = pd.DataFrame([{"timestamp": pd.to_datetime(b["key"], unit="ms", utc=True), "count": b["doc_count"]} for b in buckets])
        return df.sort_values("timestamp") if not df.empty else df
    except Exception as e:
        print(f"Erro ao agregar volume: {e}")
        return pd.DataFrame()

# ====================== Exibição do volume (SEM PNG) =========================
def show_or_print_volume(df: pd.DataFrame, title: str, show: bool, tail_n: int = 12):
    if df.empty:
        print("Sem dados para volume.")
        return
    if show:
        df_plot = df.set_index("timestamp")
        df_plot["count"].plot(legend=False, title=title)
        plt.xlabel("Timestamp (UTC)")
        plt.ylabel("Eventos")
        plt.tight_layout()
        plt.show()
    else:
        print("\nResumo de volume (últimos {} intervalos):".format(min(tail_n, len(df))))
        tail = df.tail(tail_n).copy()
        # imprimir timestamps em ISO curto
        tail["timestamp"] = tail["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
        print(tail.to_string(index=False))

# ====================== IA (Gemini) ==========================================
def summarize_fields(logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    import collections
    levels = collections.Counter()
    hosts  = collections.Counter()
    svcs   = collections.Counter()
    dsets  = collections.Counter()
    for doc in logs:
        lvl = (doc.get("log") or {}).get("level")
        if lvl:
            levels[str(lvl).lower()] += 1
        host = doc.get("host") or {}
        hn = host.get("name") or host.get("hostname")
        if hn:
            hosts[hn] += 1
        svc = (doc.get("service") or {}).get("name")
        if svc:
            svcs[svc] += 1
        ds = (doc.get("data_stream") or {}).get("dataset") or (doc.get("event") or {}).get("dataset")
        if ds:
            dsets[ds] += 1
    def topn(c: collections.Counter, n=8):
        return [{"key": k, "count": v} for k, v in c.most_common(n)]
    return {
        "total_logs": len(logs),
        "top_levels": topn(levels),
        "top_hosts": topn(hosts),
        "top_services": topn(svcs),
        "top_datasets": topn(dsets),
    }

def _sample_logs(logs: List[Dict[str, Any]], n: int = 40) -> List[Dict[str, Any]]:
    if not logs:
        return []
    head = logs[: min(15, len(logs))]
    rest = logs[len(head):]
    import random; random.seed(42)
    tail = random.sample(rest, k=min(max(0, n - len(head)), len(rest))) if rest else []
    sample = head + tail
    compact = []
    for d in sample:
        msg = d.get("message")
        if isinstance(msg, (dict, list)):
            msg = json.dumps(msg, ensure_ascii=False)
        if isinstance(msg, str) and len(msg) > 800:
            msg = msg[:800]
        compact.append({
            "@timestamp": d.get("@timestamp"),
            "level": ((d.get("log") or {}).get("level") or None),
            "host": (d.get("host") or {}).get("name") or (d.get("host") or {}).get("hostname"),
            "service": (d.get("service") or {}).get("name"),
            "dataset": (d.get("data_stream") or {}).get("dataset") or (d.get("event") or {}).get("dataset"),
            "message": msg,
        })
    return compact

def analyze_with_gemini(logs: List[Dict[str, Any]],
                        model_name: str = "gemini-2.5-pro",
                        timeout_s: int = 50,
                        temperature: float = 0.2,
                        max_retries: int = 2) -> str:
    _ensure_gemini_config()
    stats = summarize_fields(logs)
    sample = _sample_logs(logs, n=40)

    system_hint = (
        "Você é um SRE especialista em observabilidade (Elastic/ECS). "
        "Responda em português, de forma objetiva e acionável."
    )
    user_prompt = (
        "Analise os logs a seguir e entregue:\n"
        "• Tendências/padrões e possíveis picos\n"
        "• Erros recorrentes e serviços/hosts impactados\n"
        "• Hipóteses de causa-raiz\n"
        "• Ações recomendadas (runbooks, queries, alertas)\n\n"
        f"Estatísticas (JSON): {json.dumps(stats, ensure_ascii=False)}\n"
        f"Amostra de logs ({len(sample)}): {json.dumps(sample, ensure_ascii=False)}\n"
        "Observação: '@timestamp' é o tempo; mensagens podem conter PII — trate genericamente."
    )

    model = genai.GenerativeModel(model_name)
    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 2):
        try:
            resp = model.generate_content(
                [system_hint, user_prompt],
                generation_config={"temperature": temperature, "top_p": 0.9, "top_k": 40},
                request_options={"timeout": timeout_s},
            )
            text = (getattr(resp, "text", "") or "").strip()
            return text if text else "(A IA não retornou texto.)"
        except Exception as e:
            last_err = e
            if attempt <= max_retries:
                time.sleep(1.5 ** attempt)
            else:
                break
    return f"[ERRO] Falha na análise da IA: {last_err}"

# ====================== CLI / Main ===========================================
def parse_args():
    p = argparse.ArgumentParser(
        prog="iaops-analyze-elasticsearch-logs",
        description="Analisa logs do Elasticsearch e usa Gemini para insights (descoberta dinâmica de índices) — sem salvar PNG."
    )
    p.add_argument("--es-url", default="https://localhost:9200", help="URL do Elasticsearch (default: %(default)s)")
    p.add_argument("--username", default=os.environ.get("ELASTIC_USERNAME", "elastic"), help="Usuário para Basic Auth")
    p.add_argument("--password", default=os.environ.get("ELASTIC_PASSWORD"), help="Senha para Basic Auth")
    p.add_argument("--api-key", default=os.environ.get("ELASTIC_API_KEY"), help="API Key (id:key ou chave única)")
    p.add_argument("--ca", dest="ca_path", default=None, help="Caminho do certificado CA (ex.: /etc/elasticsearch/certs/http_ca.crt)")
    p.add_argument("--insecure", action="store_true", help="Não verificar certificados TLS (NÃO recomendado)")
    p.add_argument("--ssl-no-hostname-check", action="store_true", help="Desabilita verificação de hostname (diagnóstico)")
    # Descoberta de índices
    p.add_argument("--match", default=".ds-logs-*", help="Padrão para descoberta de índices (ex.: .ds-logs-*, logs-*, *)")
    p.add_argument("--include-hidden", action="store_true", help="Incluir índices fechados/ocultos na descoberta")
    p.add_argument("--list-limit", type=int, default=50, help="Limite de índices listados no menu (default: %(default)s)")
    # Coleta/análise
    p.add_argument("--hours", type=float, default=1.0, help="Janela em horas (default: %(default)s)")
    p.add_argument("--step", default="1m", help="Intervalo do date_histogram (ex.: 30s, 1m, 5m) (default: %(default)s)")
    p.add_argument("--size", type=int, default=100, help="Qtd máx. de logs para amostrar (default: %(default)s)")
    p.add_argument("--query", default=None, help="Query Lucene/Query String (ex.: 'error OR status:500')")
    p.add_argument("--model", default="gemini-2.5-pro", help="Modelo do Gemini (default: %(default)s)")
    p.add_argument("--timeout", type=int, default=50, help="Timeout (s) da chamada ao Gemini (default: %(default)s)")
    p.add_argument("--no-ai", action="store_true", help="Pula análise da IA")
    p.add_argument("--show", action="store_true", help="Exibe o gráfico interativamente (não salva PNG)")
    p.add_argument("--debug-es", action="store_true", help="Mostra erro detalhado ao conectar")
    return p.parse_args()

def main():
    args = parse_args()

    # Conexão ES
    try:
        es = get_es_client(
            es_url=args.es_url,
            username=args.username,
            password=args.password,
            api_key=args.api_key,
            ca_path=args.ca_path,
            verify_certs=not args.insecure,
            ssl_no_hostname_check=args.ssl_no_hostname_check,
        )
    except Exception as e:
        print(f"[ERRO] Falha ao instanciar cliente ES: {e}")
        return

    try:
        ok = ping_or_info(es)
    except Exception as e:
        if args.debug_es:
            print(f"[ERRO] Exceção no ping/info: {repr(e)}")
        else:
            print("[ERRO] Não foi possível pingar o Elasticsearch. Use --debug-es para detalhes.")
        return
    if not ok:
        print("[ERRO] Conexão não estabelecida.")
        return
    print("Conectado ao Elasticsearch.")

    # Descobrir índices dinamicamente
    indices = discover_indices(es, match=args.match, include_hidden=args.include_hidden, limit=args.list_limit)
    if not indices:
        print(f"Nenhum índice encontrado para match: {args.match}")
        return

    # Menu de seleção
    print("Selecione o índice para análise:")
    for i, idx in enumerate(indices, start=1):
        print(f"{i}. {idx}")
    try:
        index_sel = int(input("Digite o número do índice selecionado:\n").strip()) - 1
        assert 0 <= index_sel < len(indices)
        index_name = indices[index_sel]
    except (ValueError, AssertionError):
        print("Seleção inválida.")
        return

    # Buscar logs
    logs = fetch_logs(es, index=index_name, hours=args.hours, size=args.size, extra_query=args.query)
    if not logs:
        print("Nenhum log retornado para a query/intervalo escolhido.")
        return

    # ---- Amostra robusta (funciona com ECS aninhado ou campos achatados) ----
    df = pd.DataFrame(logs)
    n = len(df)
    def s(col: str):
        return df[col] if col in df.columns else pd.Series([None]*n)

    # level
    level_from_obj = s("log").apply(lambda x: (x or {}).get("level") if isinstance(x, dict) else None)
    level = level_from_obj.where(level_from_obj.notna(), s("log.level"))

    # host
    host_from_obj = s("host").apply(lambda x: (x or {}).get("name") or (x or {}).get("hostname") if isinstance(x, dict) else None)
    host = host_from_obj.where(host_from_obj.notna(), s("host.name"))
    host = host.where(host.notna(), s("host.hostname"))

    # service
    service_from_obj = s("service").apply(lambda x: (x or {}).get("name") if isinstance(x, dict) else None)
    service = service_from_obj.where(service_from_obj.notna(), s("service.name"))

    # dataset
    ds_from_obj = s("data_stream").apply(lambda x: (x or {}).get("dataset") if isinstance(x, dict) else None)
    dataset = ds_from_obj.where(ds_from_obj.notna(), s("data_stream.dataset"))
    dataset = dataset.where(dataset.notna(), s("event.dataset"))

    # message
    message = s("message").where(s("message").notna(), s("message.original"))

    df_print = pd.DataFrame({
        "@timestamp": s("@timestamp"),
        "level": level,
        "host": host,
        "service": service,
        "dataset": dataset,
        "message": message,
    })
    print()
    print(df_print.head(10).to_string(index=False))

    # Agregar volume (SEM salvar PNG)
    vol_df = fetch_volume_agg(es, index=index_name, hours=args.hours, step=args.step, extra_query=args.query)
    title = f"Volume de logs — index={index_name}"
    show_or_print_volume(vol_df, title, show=args.show, tail_n=12)

    # IA (opcional)
    if not args.no_ai:
        print("\n=== Análise da IA (Gemini) ===")
        print(
            analyze_with_gemini(
                logs=logs,
                model_name=args.model,
                timeout_s=args.timeout,
                temperature=0.2,
            )
        )
    else:
        print("\n(IA desativada por --no-ai)")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Execução interrompida pelo usuário.")
