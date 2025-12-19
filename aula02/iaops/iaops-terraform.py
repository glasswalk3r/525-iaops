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
das variáveis correspondentes (apenas no modo network). Ex.: project_id, region, zone.

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

- instance  (arquivo: instance*.tf)
  * NÃO inclua terraform{}, provider{} ou variáveis duplicadas (project_id/region/zone).
  * NÃO crie recursos tls/local_file aqui. As chaves SSH são centralizadas em `ssh_keys.tf` (tls_private_key "ssh_key" + local_file "private_key_pem"/"public_key_openssh").
  * Reutilize: google_compute_subnetwork.subnet_devops, google_compute_address.<LABEL_DO_IP>.address.
  * Em metadata/ssh-keys use: deploy:${tls_private_key.ssh_key.public_key_openssh} (NÃO use file()).
  * **Por padrão adicione IP externo efêmero** com `access_config {}` no `network_interface`, exceto se o SPEC pedir explicitamente *sem IP externo*.
  * Converta startup script multi-linha para heredoc.
  * Use EXATAMENTE nomes/valores do SPEC.
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
    }.get(mode,"CONTEXT.TYPE=unknown\n")
    parts = [ctx, spec.strip()]
    if obs and mode == "network":
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

def _norm_gcp_name(s: str) -> str:
    s = s.lower()
    s = re.sub(r'[^a-z0-9-]', '-', s)
    s = re.sub(r'-+', '-', s).strip('-')
    if not s or not s[0].isalpha(): s = 'a' + s
    return s[:63]

def _snake_from_kebab(s: str) -> str:
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return re.sub(r'_+', '_', s).strip('_')[:63]

def _valid_label(lbl: Optional[str]) -> bool:
    if not lbl: return False
    lbl = lbl.strip().lower()
    if len(lbl) < 3: return False
    if lbl in {"da","de","do","a","o","ip","name","nome"}: return False
    return True

# extrai nome do IP só quando estiver perto de "ip/endereço/address"
def _extract_ip_name_from_spec(spec: str) -> Optional[str]:
    if not spec: return None
    for m in re.finditer(r'(?i)(?:nome|name)\s*[:=]?\s*["`“]([a-z0-9][a-z0-9\-\.]{2,})["`”]', spec):
        ctx = (spec[max(0,m.start()-80):m.start()] + spec[m.end():m.end()+80]).lower()
        if " ip" in ctx or "ip " in ctx or "endere" in ctx or "address" in ctx:
            return m.group(1)
    return None

def _extract_ip_label_from_spec_ref(spec: str) -> Optional[str]:
    if not spec: return None
    m = re.search(r'google_compute_address\.([A-Za-z0-9_]{3,})\.(?:address|self_link)\b', spec)
    if m: return m.group(1)
    return None

def _sanitize_ip_block_names(hcl: str) -> str:
    def fix(m):
        blk = m.group(0)
        return re.sub(r'(?m)^(\s*name\s*=\s*")([^"]+)(")',
                      lambda mm: mm.group(1)+_norm_gcp_name(mm.group(2))+mm.group(3),
                      blk)
    return RE_ADDR_BLOCK.sub(fix, hcl)

def _ensure_internal_ip(hcl: str) -> str:
    # remove 'purpose = ...' e força address_type = "INTERNAL"
    def fix(m):
        blk = m.group(0)
        blk = re.sub(r'(?m)^\s*purpose\s*=.*\n', '', blk)
        if re.search(r'(?m)^\s*address_type\s*=', blk):
            blk = re.sub(r'(?m)^(\s*address_type\s*=\s*").*?(")', r'\1INTERNAL\2', blk)
        else:
            blk = re.sub(r'(\{\s*\n)', r'\1  address_type = "INTERNAL"\n', blk, count=1)
        return blk
    return RE_ADDR_BLOCK.sub(fix, hcl)

def _retag_address_labels_from_name(hcl: str) -> str:
    # para CADA bloco address: usa o name => label snake_case
    def fix(m):
        blk = m.group(0)
        nm = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', blk)
        if not nm:
            return blk
        label = _snake_from_kebab(nm.group(1))
        if not _valid_label(label):
            return blk
        blk = re.sub(
            r'(?m)^(resource\s+"google_compute_address"\s+")([^"]+)(")',
            lambda hh: hh.group(1) + label + hh.group(3),
            blk,
            count=1
        )
        return blk
    return RE_ADDR_BLOCK.sub(fix, hcl)

def _ensure_access_config(hcl: str) -> str:
    # injeta access_config {} em todo network_interface que não tiver
    def add_access(m):
        blk = m.group(0)
        if re.search(r'(?m)^\s*access_config\s*\{', blk):
            return blk
        return re.sub(r'\n\}', '\n    access_config {}\n  }', blk, count=1)
    return RE_NET_IFACE.sub(add_access, hcl)

def _strip_dupes(hcl: str) -> str:
    hcl = RE_TERRAFORM.sub("", hcl)
    hcl = RE_PROVIDER.sub("", hcl)
    return hcl

def _canonical_tls_blocks() -> str:
    return (
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
        '}\n\n'
    )

def _normalize_image_families(hcl: str) -> str:
    rep = {
        'ubuntu-2204-lts': 'ubuntu-os-cloud/ubuntu-2204-lts',
        'ubuntu-2004-lts': 'ubuntu-os-cloud/ubuntu-2004-lts',
        'debian-12':       'debian-cloud/debian-12',
        'debian-11':       'debian-cloud/debian-11',
    }
    for k,v in rep.items():
        hcl = re.sub(r'(?m)^(\s*image\s*=\s*")'+re.escape(k)+r'(")', r'\1'+v+r'\2', hcl)
    return hcl

def _startup_to_heredoc(hcl: str) -> str:
    pat = re.compile(r'(?sm)metadata_startup_script\s*=\s*"((?:[^"\\]|\\.|[\n\r])*)"')
    def conv(m):
        body = m.group(1).replace(r'\"','"')
        if '\n' in body or '\r' in body:
            return 'metadata_startup_script = <<-EOT\n'+body+'\nEOT'
        return m.group(0)
    return pat.sub(conv, hcl)

def _spec_asks_no_public_ip(text: str) -> bool:
    if not text: return False
    t = text.lower()
    phrases = [
        "sem ip externo", "sem ip público", "sem ip publico", "sem endereço externo",
        "sem ip público externo", "no public ip", "without public ip", "apenas ip interno",
        "internal only", "private only"
    ]
    return any(p in t for p in phrases)

def _fix_instance_file(hcl: str, spec_text: str, ip_label_from_spec: Optional[str]) -> str:
    # Remove TLS/local_file da instância (centralizados em ssh_keys.tf)
    hcl = RE_TLS_ANY.sub("", hcl)
    hcl = RE_LOCAL_SSH.sub("", hcl)
    # Normalizações úteis
    hcl = _normalize_image_families(hcl)
    hcl = _startup_to_heredoc(hcl)
    # Corrigir referência do IP estático
    label = ip_label_from_spec if _valid_label(ip_label_from_spec) else "devops_management_ip"
    hcl = re.sub(r'google_compute_address\.management_ip\b',
                 f'google_compute_address.{label}', hcl)
    # IP externo efêmero por padrão (a menos que o SPEC peça o contrário)
    if not _spec_asks_no_public_ip(spec_text):
        hcl = _ensure_access_config(hcl)
    return hcl

def sanitize(mode: str, files: List[Dict[str,str]], spec_text: str) -> List[Dict[str,str]]:
    out=[]
    # para instance: tenta label explícito; se não houver, tenta pelo nome; senão usa default
    explicit_label = _extract_ip_label_from_spec_ref(spec_text)
    name_from_spec = _extract_ip_name_from_spec(spec_text)
    ip_label_for_instance = None
    if _valid_label(explicit_label):
        ip_label_for_instance = explicit_label
    elif _valid_label(name_from_spec):
        ip_label_for_instance = _snake_from_kebab(name_from_spec)

    for f in files:
        p, c = f.get("path",""), f.get("content","")
        if not p.endswith(".tf"): out.append(f); continue
        if mode in ("ip","firewall","instance"):
            c = _strip_dupes(c)
            c = RE_VAR_PROJ.sub("", c); c = RE_VAR_REGION.sub("", c); c = RE_VAR_ZONE.sub("", c)
        if mode == "ip":
            c = _sanitize_ip_block_names(c)   # normaliza name válido
            c = _ensure_internal_ip(c)        # força INTERNAL e remove purpose
            c = _retag_address_labels_from_name(c)  # label de cada recurso segue o name
        if mode == "instance":
            c = _fix_instance_file(c, spec_text, ip_label_for_instance)
        out.append({"path":p,"content":(re.sub(r'\n{3,}','\n\n',c).strip()+"\n")})

    # Em modo instance: sempre garanta o arquivo central de chaves (1x)
    if mode == "instance":
        out.append({"path":"ssh_keys.tf","content":_canonical_tls_blocks()})

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
    t.add_argument("type", choices=["network","ip","firewall","instance"])
    t.add_argument("--spec", required=True)
    t.add_argument("--model", default="gemini-2.5-pro")
    t.add_argument("--temperature", type=float, default=0.2)
    t.add_argument("--output-dir", default="./terraform_infra")

    g = sub.add_parser("generate", help="Atalho para gerar instância")
    g.add_argument("type", choices=["instance"])
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

    fallback = {"network":"network.tf","ip":"ip.tf","firewall":"firewall_rules.tf","instance":"instance.tf"}[mode]
    payload = parse_or_die(raw, fallback)
    payload["files"] = sanitize(mode, payload.get("files", []), args.spec)

    write_files(args.output_dir, payload["files"])
    notes = payload.get("notes","").strip()
    if notes: print("\n[Notas]\n"+notes)
    print("\nDicas:\n terraform fmt -recursive && terraform validate\n terraform plan && terraform apply")

if __name__ == "__main__":
    main()
