#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

def call_model(genai, prompt: str, model="gemini-2.5-pro", temperature=0.2) -> str:
    m = genai.GenerativeModel(model)
    r = m.generate_content(
        [
            {"role":"user","parts":[SYSTEM_PROMPT]},
            {"role":"user","parts":[prompt]},
        ],
        generation_config={"temperature":temperature,"top_p":0.9,"top_k":40,"response_mime_type":"application/json"},
        safety_settings=[{"category":"HARM_CATEGORY_DANGEROUS_CONTENT","threshold":"BLOCK_NONE"}],
    )
    if hasattr(r,"text") and r.text: return r.text.strip()
    print("Erro: resposta vazia do modelo.", file=sys.stderr); sys.exit(1)

# ============================ Prompt =========================================
SYSTEM_PROMPT = r"""
Você é um assistente IaC que GERA **APENAS Terraform (HCL)** para Google Cloud.

SAÍDA OBRIGATÓRIA (JSON estrito):
{
  "files":[{"path":"arquivo.tf","content":"conteúdo HCL"}],
  "notes":"opcional"
}
NÃO escreva nada fora desse JSON.

SE RECEBER UMA SEÇÃO "OBS: Defaults detectados do SPEC:", use esses valores como DEFAULT
das variáveis/atributos correspondentes (ex.: project_id, region, zone).

MODOS (CONTEXT.TYPE) E REGRAS:

- network  (arquivo: network.tf)
  * Um único arquivo com: terraform { required_providers }, provider "google", variables (project_id, region, zone).
  * Se houver defaults na "OBS", aplique-os nas variáveis.
  * Recursos: VPC, Subrede e firewall ICMP básico quando pedido.
  * Use EXATAMENTE nomes/valores do SPEC. Não crie variables.tf separado.

- ip  (arquivo: ip.tf)
  * Gere SOMENTE google_compute_address para **IP interno** na sub-rede.
  * **Sempre** defina `address_type = "INTERNAL"`; **não use `purpose`** a menos que for pedido explicitamente.
  * NÃO inclua terraform{}, provider{} ou variáveis duplicadas.
  * Reutilize sub-rede existente (ex.: google_compute_subnetwork.subnet_devops.self_link) e var.region.
  * O atributo name deve corresponder a ^[a-z]([-a-z0-9]{0,61}[a-z0-9])$, **NÃO use underscore (_)**.

- firewall  (arquivo: firewall_rules.tf)
  * Gere SOMENTE google_compute_firewall.
  * NÃO inclua terraform{}, provider{} ou variáveis duplicadas.
  * Reutilize rede existente (ex.: google_compute_network.vpc_devops.name).
  * Use nomes/ports/tags EXATOS do SPEC.

- instance  (arquivo: instance.tf)
  * NÃO inclua terraform{}, provider{} ou variáveis duplicadas (project_id/region/zone).
  * NÃO crie recursos tls/local_file aqui. As chaves SSH são centralizadas em `ssh_keys.tf` (tls_private_key "ssh_key" + local_file "private_key_pem"/"public_key_openssh").
  * Reutilize: google_compute_subnetwork.subnet_devops, google_compute_address.<LABEL_DO_IP>.address.
  * Em metadata/ssh-keys use: deploy:${tls_private_key.ssh_key.public_key_openssh} (NÃO use file()).
  * **Por padrão adicione IP externo efêmero** com `access_config {}` no `network_interface`, exceto se o SPEC pedir explicitamente *sem IP externo*.
  * Converta startup script multi-linha para heredoc.
  * Use EXATAMENTE nomes/valores do SPEC.

- gke  (arquivo: main.tf)
  * Um único arquivo **completo** contendo:
    - `provider "google"` com `project`, `region` e `credentials = file("chave.json")` (a menos que o SPEC peça outro caminho).
    - `resource "google_container_cluster"` com:
      - `name`, `location` (use a **zone**), `initial_node_count` (default 3),
      - `node_config { machine_type (default "e2-standard-2"), disk_size_gb (default 50) }`,
      - `deletion_protection = false`.
  * **Não** inclua outros recursos, variáveis, outputs ou blocos extras.
  * Se houver defaults na "OBS" (project_id/region/zone), aplique-os nos campos correspondentes.
  * Use EXATAMENTE os nomes/valores do SPEC quando fornecidos.
"""

# ============================ Extração de defaults do SPEC ===================
RE_PROJECT = re.compile(r'(?i)\b(?:projeto|project|project_id)\s*[:=`"]*\s*([a-z][a-z0-9-]{4,})\b')
RE_REGION  = re.compile(r'(?i)\b(?:região|region)\s*[:=`"]*\s*([a-z]+-[a-z0-9]+[a-z0-9-]*)\b')
RE_ZONE    = re.compile(r'(?i)\b(?:zona|zone)\s*[:=`"]*\s*([a-z]+-[a-z0-9]+-[a-z])\b')

def extract_defaults(text: str) -> Dict[str,str]:
    d: Dict[str,str] = {}
    m = RE_PROJECT.search(text);  d["project_id"] = m.group(1) if m else ""
    m = RE_REGION.search(text);   d["region"]     = m.group(1) if m else ""
    m = RE_ZONE.search(text);     d["zone"]       = m.group(1) if m else ""
    return {k:v for k,v in d.items() if v}

def build_instruction(mode: str, spec: str) -> str:
    obs = extract_defaults(spec)
    ctx = {
        "network":"CONTEXT.TYPE=network\nARQUIVO_ALVO=network.tf\n",
        "ip":"CONTEXT.TYPE=ip\nARQUIVO_ALVO=ip.tf\n",
        "firewall":"CONTEXT.TYPE=firewall\nARQUIVO_ALVO=firewall_rules.tf\n",
        "instance":"CONTEXT.TYPE=instance\nARQUIVO_ALVO=instance.tf\n",
        "gke":"CONTEXT.TYPE=gke\nARQUIVO_ALVO=main.tf\n",
    }.get(mode,"CONTEXT.TYPE=unknown\n")
    parts = [ctx, spec.strip()]
    if obs and mode in ("network","gke"):
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
        data.setdefault("notes",""); return data
    if isinstance(data, str) and data.strip():
        return {"files":[{"path":fallback_name,"content":data.strip()}], "notes":""}
    print("Erro: JSON fora do esquema esperado.", file=sys.stderr); sys.exit(1)

# ============================ Sanitização e Normalizações =====================
RE_TERRAFORM = re.compile(r'(?sm)^\s*terraform\s*\{.*?\}\s*')
RE_PROVIDER  = re.compile(r'(?sm)^\s*provider\s*"google"\s*\{.*?\}\s*')
RE_VAR_PROJ  = re.compile(r'(?sm)^\s*variable\s*"project_id"\s*\{.*?\}\s*')
RE_VAR_REGION= re.compile(r'(?sm)^\s*variable\s*"region"\s*\{.*?\}\s*')
RE_VAR_ZONE  = re.compile(r'(?sm)^\s*variable\s*"zone"\s*\{.*?\}\s*')

RE_ADDR_BLOCK = re.compile(r'(?sm)resource\s+"google_compute_address"\s+"[^"]+"\s*\{.*?\}')
RE_TLS_ANY    = re.compile(r'(?sm)^\s*resource\s+"tls_private_key"\s+"[^"]+"\s*\{.*?\}\s*')
RE_LOCAL_SSH  = re.compile(r'(?sm)^\s*resource\s+"local_file"\s+"[^"]+"\s*\{[^}]*?\bfilename\s*=\s*"ssh-key(?:\.pub)?"[^}]*?\}\s*')
RE_NET_IFACE  = re.compile(r'(?sm)network_interface\s*\{.*?\}')

def _strip_dupes(hcl: str) -> str:
    hcl = RE_TERRAFORM.sub("", hcl)
    hcl = RE_PROVIDER.sub("", hcl)
    return hcl

def sanitize(mode: str, files: List[Dict[str,str]], spec_text: str) -> List[Dict[str,str]]:
    out=[]
    for f in files:
        p, c = f.get("path",""), f.get("content","")
        if not p.endswith(".tf"): out.append(f); continue
        # Em ip/firewall/instance removemos provider/vars duplicadas; em gke preservamos provider
        if mode in ("ip","firewall","instance"):
            c = _strip_dupes(c)
            c = RE_VAR_PROJ.sub("", c); c = RE_VAR_REGION.sub("", c); c = RE_VAR_ZONE.sub("", c)
        out.append({"path":p,"content":(re.sub(r'\n{3,}','\n\n',c).strip()+"\n")})
    # Em instance mantemos o arquivo de chaves; em gke não adicionamos nada
    if mode == "instance":
        out.append({"path":"ssh_keys.tf","content":(
            'resource "tls_private_key" "ssh_key" {\n'
            '  algorithm = "RSA"\n'
            '  rsa_bits  = 4096\n'
            '}\n\n'
            'resource "local_file" "private_key_pem" {\n'
            '  filename        = "ssh-key"\n'
            '  content         = tls_private_key.ssh_key.private_key_pem\n'
            '  file_permission = "0600"\n'
            '}\n\n'
            'resource "local_file" "public_key_openssh" {\n'
            '  filename        = "ssh-key.pub"\n'
            '  content         = tls_private_key.ssh_key.public_key_openssh\n'
            '  file_permission = "0644"\n'
            '}\n'
        )})
    return out

# ============================ CLI + Exec =====================================
def write_files(base: str, files: List[Dict[str,str]]):
    os.makedirs(base, exist_ok=True)
    print(f"\nSalvando em: {os.path.abspath(base)}")
    for f in files:
        path = os.path.join(base, f["path"])
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as w: w.write(f["content"])
        print(" -", f["path"])
    print("OK.")

def main():
    ap = argparse.ArgumentParser(description="IAOps Terraform (compacto, com defaults do SPEC)")
    sub = ap.add_subparsers(dest="cmd")

    t = sub.add_parser("terraform", help="Gerar Terraform por categoria")
    t.add_argument("type", choices=["network","ip","firewall","instance","gke"])
    t.add_argument("--spec", required=True)
    t.add_argument("--model", default="gemini-2.5-pro")
    t.add_argument("--temperature", type=float, default=0.2)
    t.add_argument("--output-dir", default="./terraform_infra")

    g = sub.add_parser("generate", help="Atalho")
    g.add_argument("type", choices=["instance","gke"])
    g.add_argument("--spec", required=True)
    g.add_argument("--model", default="gemini-2.5-pro")
    g.add_argument("--temperature", type=float, default=0.2)
    g.add_argument("--output-dir", default="./terraform_infra")

    args = ap.parse_args()
    if not args.cmd: ap.print_help(); sys.exit(2)

    mode = args.type
    genai = ensure_api()
    instr = build_instruction(mode, args.spec)
    raw = call_model(genai, instr, model=args.model, temperature=args.temperature)

    fallback = {"network":"network.tf","ip":"ip.tf","firewall":"firewall_rules.tf","instance":"instance.tf","gke":"main.tf"}[mode]
    payload = parse_or_die(raw, fallback)
    payload["files"] = sanitize(mode, payload.get("files", []), args.spec)

    write_files(args.output_dir, payload["files"])
    notes = payload.get("notes","").strip()
    if notes: print("\n[Notas]\n"+notes)
    print("\nDicas:\n terraform fmt -recursive && terraform validate\n terraform plan && terraform apply")

if __name__ == "__main__":
    main()
