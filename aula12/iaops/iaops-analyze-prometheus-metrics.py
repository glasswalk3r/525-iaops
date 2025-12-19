#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sys
import json
import time
import math
import argparse
import warnings
from typing import Dict, Any, List, Optional

# ==== silenciar avisos ruidosos ==============================================
warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"google\.api_core\._python_version_support",
)
# opcional: silencie todos FutureWarnings do ambiente
warnings.filterwarnings("ignore", category=FutureWarning)

import requests
import pandas as pd

# matplotlib: backend não-interativo por padrão (evita janela travando)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ==== Gemini SDK ==============================================================
try:
    import google.generativeai as genai
except ImportError:
    print("Falta google-generativeai. Rode: pip install -U google-generativeai", file=sys.stderr)
    sys.exit(1)

# ==== Prometheus ==============================================================
def get_prometheus_jobs(prometheus_url: str) -> List[str]:
    resp = requests.get(f"{prometheus_url}/api/v1/targets", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Erro ao consultar jobs do Prometheus: {data}")
    jobs = sorted(
        set(
            it.get("labels", {}).get("job")
            for it in data.get("data", {}).get("activeTargets", [])
            if ":9100" in it.get("labels", {}).get("instance", "")
        )
    )
    return [j for j in jobs if j]

def get_prometheus_metrics(prometheus_url: str, query: str, start: float, end: float, step: str = "300s") -> pd.DataFrame:
    resp = requests.get(
        f"{prometheus_url}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": step},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Erro ao consultar métricas: {data}")
    rows: List[Dict[str, Any]] = []
    for result in data.get("data", {}).get("result", []):
        metric = result.get("metric", {})
        for ts, val in result.get("values", []):
            try:
                v = float(val)
            except Exception:
                continue
            rows.append(
                {
                    "instance": metric.get("instance", "unknown"),
                    "job": metric.get("job", "unknown"),
                    "value": v,
                    "timestamp": pd.to_datetime(float(ts), unit="s", utc=True),
                }
            )
    return pd.DataFrame(rows)

# ==== Gemini analysis =========================================================
def _ensure_gemini_config(api_key: Optional[str] = None):
    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Defina GEMINI_API_KEY (ou GOOGLE_API_KEY).")
    genai.configure(api_key=key)

def _groupwise_sample(df: pd.DataFrame, group_col: str, n_per_group: int = 2, max_rows: int = 12) -> pd.DataFrame:
    if group_col in df.columns and not df.empty:
        g = df.groupby(group_col, group_keys=False)
        # pandas 2.1+ tem include_groups; nem toda versão tem. Tentamos sem warning.
        try:
            sample = g.apply(lambda x: x.sample(min(n_per_group, len(x)), random_state=42), include_groups=False)
        except TypeError:
            # versões sem include_groups
            sample = g.apply(lambda x: x.sample(min(n_per_group, len(x)), random_state=42))
        if len(sample) > max_rows:
            sample = sample.sample(max_rows, random_state=42)
        return sample
    return df.sample(min(max_rows, len(df)), random_state=42) if not df.empty else df

def get_gemini_analysis(
    data: pd.DataFrame,
    model_name: str = "gemini-2.5-pro",
    temperature: float = 0.2,
    timeout_s: int = 40,
    max_retries: int = 2,
) -> str:
    _ensure_gemini_config()

    # compacta estatísticas (somente numéricas principais)
    numeric = data.select_dtypes(include="number")
    summary = numeric.describe().to_dict() if not numeric.empty else {"info": "sem colunas numéricas"}
    # amostra bem pequena para não estourar tokens
    sample = _groupwise_sample(data[["timestamp", "instance", "job", "value"]].copy(), "instance", n_per_group=2, max_rows=12)
    # converte de forma compacta
    # -> timestamps como iso curtos
    if not sample.empty:
        sample["timestamp"] = sample["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
    sample_records = sample.to_dict(orient="records")
    summary_json = json.dumps(summary, ensure_ascii=False)

    system_hint = (
        "Você é um assistente especializado em análise de métricas de observabilidade (Prometheus/Node Exporter). "
        "Responda em português, com tópicos objetivos."
    )
    user_prompt = (
        "Analise as métricas a seguir e produza:\n"
        "• Tendências principais\n"
        "• Possíveis anomalias\n"
        "• Interpretações e hipóteses\n"
        "• Itens acionáveis (runbooks/alertas)\n\n"
        f"Resumo estatístico (JSON): {summary_json}\n"
        f"Amostras ({len(sample_records)} linhas): {sample_records}\n"
        "Notas: a série de interesse está em 'value'; 'timestamp' é a linha do tempo; 'instance' e 'job' identificam a origem."
    )

    model = genai.GenerativeModel(model_name)
    backoff = 1.5
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
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_err = e
            if attempt <= max_retries:
                # pequeno backoff antes de tentar de novo
                time.sleep(backoff ** attempt)
            else:
                break
    raise RuntimeError(f"Falha na análise da IA após {max_retries+1} tentativa(s): {last_err}")

# ==== Plot helper =============================================================
def save_plot(df: pd.DataFrame, title: str, out_path: str, show: bool = False):
    if df.empty:
        return
    df_plot = df.copy()
    df_plot.set_index("timestamp", inplace=True)
    ax = None
    for inst, g in df_plot.groupby("instance"):
        series = g["value"].sort_index()
        ax = series.plot(legend=True, label=inst, title=title)
    plt.xlabel("Timestamp (UTC)")
    plt.ylabel("Value")
    plt.title(title)
    plt.tight_layout()
    if show:
        # se usuário explicitou --show, abre janela interativa
        plt.show()
    else:
        plt.savefig(out_path, dpi=120)
        plt.close()
        print(f"Gráfico salvo em: {out_path}")

# ==== CLI / Main ==============================================================
def parse_args():
    p = argparse.ArgumentParser(prog="iaops-analyze-prometheus-metrics", description="Analisa métricas do Prometheus e usa Gemini para insights.")
    p.add_argument("--prometheus-url", default="http://localhost:9090", help="URL do Prometheus (default: %(default)s)")
    p.add_argument("--hours", type=float, default=1.0, help="Janela em horas para coletar métricas (default: %(default)s)")
    p.add_argument("--step", default="300s", help="Step do query_range (ex.: 60s, 300s) (default: %(default)s)")
    p.add_argument("--model", default="gemini-2.5-pro", help="Modelo do Gemini (default: %(default)s)")
    p.add_argument("--timeout", type=int, default=40, help="Timeout (s) da chamada ao Gemini (default: %(default)s)")
    p.add_argument("--no-ai", action="store_true", help="Pula análise da IA")
    p.add_argument("--show", action="store_true", help="Exibe o gráfico interativamente (ao invés de apenas salvar PNG)")
    return p.parse_args()

def main():
    args = parse_args()

    # Descobrir jobs
    try:
        jobs = get_prometheus_jobs(args.prometheus_url)
        if not jobs:
            print("Nenhum job encontrado no Prometheus para instâncias :9100.")
            return
    except Exception as e:
        print(f"[ERRO] {e}")
        return

    # Seleção de job
    print("Selecione o job para análise:")
    for idx, job in enumerate(jobs, start=1):
        print(f"{idx}. {job}")
    try:
        job_index = int(input("Digite o número do job selecionado:\n").strip()) - 1
        assert 0 <= job_index < len(jobs)
        job_name = jobs[job_index]
    except (ValueError, AssertionError):
        print("Seleção de job inválida.")
        return

    # Seleção de métrica
    metric_types = ["CPU", "Memória", "Disco", "Processos"]
    print("Selecione o tipo de métrica para análise:")
    for idx, metric in enumerate(metric_types, start=1):
        print(f"{idx}. {metric}")
    try:
        metric_index = int(input("Digite o número do tipo de métrica selecionado:\n").strip()) - 1
        assert 0 <= metric_index < len(metric_types)
        metric_type = metric_types[metric_index].lower()
    except (ValueError, AssertionError):
        print("Seleção de métrica inválida.")
        return

    # Janela de tempo
    end_time = pd.Timestamp.now(tz="UTC")
    start_time = end_time - pd.Timedelta(hours=float(args.hours))

    # Queries
    queries = {
        "cpu": f'rate(node_cpu_seconds_total{{job="{job_name}",mode="idle"}}[5m])',
        "memória": f'node_memory_MemAvailable_bytes{{job="{job_name}"}} / node_memory_MemTotal_bytes{{job="{job_name}"}}',
        "disco": f'rate(node_disk_io_time_seconds_total{{job="{job_name}"}}[5m])',
        "processos": f'node_procs_running{{job="{job_name}"}}',
    }
    if metric_type not in queries:
        print("Tipo de métrica inválido. Escolha entre CPU, Memória, Disco ou Processos.")
        return

    # Coleta
    try:
        df = get_prometheus_metrics(
            args.prometheus_url, queries[metric_type], start_time.timestamp(), end_time.timestamp(), step=args.step
        )
        if df.empty:
            print("Sem dados retornados para a query/intervalo escolhido.")
            return
        df["metric_name"] = metric_type.capitalize()
        # Mostra primeiras linhas para contexto
        print(df.head(10).to_string(index=False))
    except Exception as e:
        print(f"[ERRO] {e}")
        return

    # Plot
    title = f"{metric_type.capitalize()} Usage — job={job_name}"
    out_png = f"prom_{metric_type}_{job_name}.png".replace("/", "_")
    save_plot(df, title, out_png, show=args.show)

    # IA (opcional)
    if not args.no_ai:
        try:
            analysis = get_gemini_analysis(
                df,
                model_name=args.model,
                temperature=0.2,
                timeout_s=args.timeout,
            )
            print("\n=== Análise da IA (Gemini) ===")
            print(analysis)
        except KeyboardInterrupt:
            print("\n[INFO] Análise interrompida pelo usuário (Ctrl+C).")
        except Exception as e:
            print(f"[ERRO] Falha na análise da IA: {e}")
    else:
        print("\n(IA desativada por --no-ai)")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Execução interrompida pelo usuário.")
