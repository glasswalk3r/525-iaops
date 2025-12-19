#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import warnings

warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"google\.api_core\._python_version_support",
)

import os, sys, json, argparse, re
from typing import Any, Dict, Optional, List

# ============================ Gemini =========================================
def ensure_api():
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("Erro: defina GEMINI_API_KEY ou GOOGLE_API_KEY.", file=sys.stderr); sys.exit(1)
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        return genai
    except ImportError:
        print("Instale: pip install -U google-generativeai", file=sys.stderr); sys.exit(1)

def call_model(genai, prompt: str, model="gemini-2.5-pro", temperature=0.15) -> str:
    m = genai.GenerativeModel(model)
    r = m.generate_content(
        [
            {"role":"user","parts":[SYSTEM_PROMPT]},
            {"role":"user","parts":[prompt]},
        ],
        generation_config={
            "temperature":temperature,
            "top_p":0.9,
            "top_k":40,
            "response_mime_type":"application/json"
        },
        safety_settings=[{"category":"HARM_CATEGORY_DANGEROUS_CONTENT","threshold":"BLOCK_NONE"}],
    )
    if hasattr(r,"text") and r.text: return r.text.strip()
    print("Erro: resposta vazia do modelo.", file=sys.stderr); sys.exit(1)

# ============================ Prompt =========================================
SYSTEM_PROMPT = r"""
Você é um assistente IaC que GERA **APENAS manifests Kubernetes (YAML)**.

SAÍDA OBRIGATÓRIA (JSON estrito):
{
  "files":[{"path":"arquivo.yaml","content":"conteúdo YAML"}],
  "notes":"opcional"
}
NÃO escreva nada fora desse JSON.

O **NOME DO ARQUIVO** deve ser **ARQUIVO_ALVO** (por ex.: NAME+".yaml").
O campo **metadata.name** deve ser **exatamente NAME**.

Se houver uma seção "OBS: Defaults detectados do SPEC:", esses valores são obrigatórios de usar.
Normalizações:
- Se a imagem vier com sufixo ":latest", **remova o sufixo** e use só o nome base (ex.: "nginx:latest" -> "nginx").
- Não inclua campos desnecessários: gere o YAML **mínimo e válido**.

MODOS (CONTEXT.TYPE) E REGRAS:

- pod  (arquivo: NAME.yaml)
  * apiVersion: v1, kind: Pod
  * metadata: name = NAME; labels: `run: NAME` (inclua labels adicionais do SPEC se existirem)
  * spec.containers: um container
    - image: (normalizada)
    - name: **igual a NAME**
  * **Ignore** qualquer "port" do SPEC no Pod (não declarar `ports`), espelhe o exemplo.

- deploy  (arquivo: NAME.yaml)
  * apiVersion: apps/v1, kind: Deployment
  * metadata: name = NAME; labels conforme SPEC (ex.: `app: webserver`)
  * spec:
    - replicas: do SPEC (default 1)
    - selector.matchLabels: igual às labels do template
    - template.metadata.labels: as labels do SPEC
    - template.spec.containers:
      - image: (normalizada)
      - name: **nome base da imagem** (antes de ":")
  * **Não** declare containerPort/ports por padrão (mesmo que o SPEC mencione port), espelhe o exemplo.

- service  (arquivo: NAME.yaml)
  * apiVersion: v1, kind: Service
  * metadata: name = NAME; labels conforme SPEC (ex.: `app: webserver`)
  * spec:
    - type: do SPEC (default ClusterIP)
    - selector: igual às labels do SPEC
    - ports:
      - port: do SPEC
        targetPort: do SPEC (se não vier, repetir `port`)
        protocol: TCP

Formato do SPEC (livre): pode conter fragmentos como:
- "imagem nginx:latest", "port 80", "target-port 8080"
- "label app:webserver" (pode repetir para várias labels)
- "replicas 2", "type NodePort"

Use EXATAMENTE o NAME, labels, e valores do SPEC quando fornecidos.
"""

# ============================ Extração de defaults do SPEC ===================
RE_IMAGE   = re.compile(r'(?i)\b(?:imagem|image)\s+([^\s;]+)')
RE_PORT    = re.compile(r'(?i)\bport(?:a)?\s+(\d+)')
RE_TPORT   = re.compile(r'(?i)\btarget-?port\s+(\d+)')
RE_REPL    = re.compile(r'(?i)\breplicas?\s+(\d+)')
RE_TYPE    = re.compile(r'(?i)\btype\s+([A-Za-z][A-Za-z0-9-]*)')
RE_LABELS  = re.compile(r'(?i)\blabel\s+([A-Za-z0-9_.\-]+)\s*[:=]\s*([A-Za-z0-9_.\-]+)')

def extract_defaults(text: str) -> Dict[str,str]:
    d: Dict[str,str] = {}
    m = RE_IMAGE.search(text);  d["image"] = m.group(1) if m else ""
    m = RE_PORT.search(text);   d["port"] = m.group(1) if m else ""
    m = RE_TPORT.search(text);  d["target_port"] = m.group(1) if m else ""
    m = RE_REPL.search(text);   d["replicas"] = m.group(1) if m else ""
    m = RE_TYPE.search(text);   d["type"] = m.group(1) if m else ""
    labs = RE_LABELS.findall(text)
    if labs:
        d["labels"] = ";".join([f"{k}={v}" for k,v in labs])
    return {k:v for k,v in d.items() if v}

def build_instruction(kind: str, name: str, spec: str) -> str:
    ctx = {
        "pod":"CONTEXT.TYPE=pod\n",
        "deploy":"CONTEXT.TYPE=deploy\n",
        "service":"CONTEXT.TYPE=service\n",
    }.get(kind,"CONTEXT.TYPE=unknown\n")
    obs = extract_defaults(spec)
    parts = [
        ctx,
        f"NAME={name}\nARQUIVO_ALVO={name}.yaml\n",
        spec.strip()
    ]
    if obs:
        lines = [f"- {k}={v}" for k,v in obs.items()]
        parts.append("\nOBS: Defaults detectados do SPEC:\n" + "\n".join(lines))
    return "\n\n".join(parts).replace('`','\\`').replace('\\n','\n')

# ============================ JSON parse curto ===============================
def _first_json(s: str) -> Optional[str]:
    i = s.find('{')
    if i < 0: return None
    d, ins, esc = 0, False, False
    for k in range(i, len(s)):
        c = s[k]
        if ins:
            if esc: esc=False
            elif c=='\\': esc=True
            elif c=='"': ins=False
        else:
            if c=='"': ins=True
            elif c=='{': d+=1
            elif c=='}':
                d-=1
                if d==0: return s[i:k+1]
    return None

def parse_or_die(raw: str, fallback_name: str) -> Dict[str,Any]:
    t = raw.strip()
    if t.startswith("```"):
        for chunk in t.split("```"):
            c = chunk.strip()
            if c.startswith("{") and c.endswith("}"): t=c; break
    t = t.replace("\\$", "$").replace("\r\n","\n")
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        j = _first_json(t)
        if not j: print("Erro: resposta não contém JSON válido.", file=sys.stderr); sys.exit(1)
        data = json.loads(j)
    if isinstance(data, dict) and "files" in data:
        # normaliza notes para string vazia quando vier None
        if data.get("notes") is None:
            data["notes"] = ""
        else:
            data["notes"] = str(data.get("notes", ""))
        return data
    if isinstance(data, str) and data.strip():
        return {"files":[{"path":fallback_name,"content":data.strip()}], "notes":""}
    print("Erro: JSON fora do esquema esperado.", file=sys.stderr); sys.exit(1)

# ============================ Sanitização/Nomes ===============================
def sanitize_files(name: str, files: List[Dict[str,str]]) -> List[Dict[str,str]]:
    out=[]
    target = f"{name}.yaml"
    for f in files:
        p, c = f.get("path",""), f.get("content","")
        p = target  # força o nome do arquivo
        c = (c.strip() + "\n")
        out.append({"path":p,"content":c})
    return out

# ============================ IO =============================================
def write_files(base: str, files: List[Dict[str,str]]):
    os.makedirs(base, exist_ok=True)
    print(f"\nSalvando em: {os.path.abspath(base)}")
    for f in files:
        path = os.path.join(base, f["path"])
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as w: w.write(f["content"])
        print(" -", f["path"])
    print("OK.")

# ============================ CLI ============================================
def make_alias_parser(sub, alias: str):
    a = sub.add_parser(alias, help=f"Atalho para {alias}")
    a.add_argument("--name", required=True)
    a.add_argument("--spec", required=True)
    a.add_argument("--model", default="gemini-2.5-pro")
    a.add_argument("--temperature", type=float, default=0.15)
    a.add_argument("--output-dir", default="./k8s_manifests")
    a.set_defaults(type=alias)  # garante args.type nos atalhos
    return a

def main():
    ap = argparse.ArgumentParser(description="IAOps Kubernetes Manifests (via IA)")
    sub = ap.add_subparsers(dest="cmd")

    gen = sub.add_parser("k8s", help="Gerar manifest")
    gen.add_argument("type", choices=["pod","deploy","service"])
    gen.add_argument("--name", required=True)
    gen.add_argument("--spec", required=True, help="Ex.: 'imagem nginx:latest; port 80; label app:web; replicas 2'")
    gen.add_argument("--model", default="gemini-2.5-pro")
    gen.add_argument("--temperature", type=float, default=0.15)
    gen.add_argument("--output-dir", default="./k8s_manifests")

    # atalhos: pod/deploy/service
    make_alias_parser(sub, "pod")
    make_alias_parser(sub, "deploy")
    make_alias_parser(sub, "service")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help(); sys.exit(2)

    # fallback defensivo: usa args.type se existir; senão, usa o nome do subcomando
    kind = getattr(args, "type", None) or args.cmd
    name = args.name
    spec = args.spec

    genai = ensure_api()
    instr = build_instruction(kind, name, spec)
    raw = call_model(genai, instr, model=args.model, temperature=args.temperature)

    payload = parse_or_die(raw, f"{name}.yaml")
    payload["files"] = sanitize_files(name, payload.get("files", []))

    write_files(args.output_dir, payload["files"])
    notes = (payload.get("notes") or "").strip()
    if notes:
        print("\n[Notas]\n" + notes)
    print("\nDicas:\n kubectl apply -f " + os.path.join(args.output_dir, f"{name}.yaml"))

if __name__ == "__main__":
    main()
