#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import warnings

warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"google\.api_core\._python_version_support",
)

import os, sys, json, argparse, re, pathlib
from typing import Any, Dict, List, Optional, Set, Tuple

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
    if text.startswith("```"):
        for chunk in text.split("```"):
            c = chunk.strip()
            if c.startswith("{") and c.endswith("}"):
                text = c; break
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("Resposta não parece JSON.")
    return json.loads(text[i:j+1])

# ============================ SYSTEM PROMPT ==========================
SYSTEM_PROMPT = r"""
Você é um gerador de **Ansible** que produz SOMENTE arquivos de playbooks/roles, em YAML puro.

SAÍDA (JSON estrito):
{
  "files": [
    {"path": "caminho/relativo.ext", "content": "conteúdo do arquivo"}
  ],
  "notes": "opcional"
}
NÃO escreva nada fora desse JSON.

REGRAS:
- Indente com 2 espaços. Sempre inicie arquivos YAML com `---` (exceto arquivos de texto/README/inventory plano).
- Use módulos totalmente qualificados `ansible.builtin.*`.
- **NÃO** qualifique chaves de parâmetro que tenham o mesmo nome de módulos (ex.: `group:`, `user:`, `line:`).
- Idempotência: `state: present/started`, `enabled: true`, `update_cache: true` quando apropriado.
- Não crie inventário dentro do playbook. README pode trazer exemplo de inventário/execução quando solicitado.
- Modos numéricos como strings: `"0644"`, `"0755"`.
- Nada de comentários fora do YAML; sem Markdown além de README.

***DEBIAN/UBUNTU + APT (IMPORTANTE)***
- Se usar repositório externo com `signed-by=...`, CRIE o keyring:
  1) `file: /etc/apt/keyrings state=directory`
  2) `get_url` da chave ASCII → `/etc/apt/keyrings/<nome>.asc`
  3) `command: gpg --dearmor -o /etc/apt/keyrings/<nome>.gpg /etc/apt/keyrings/<nome>.asc` (com `args.creates`)
  4) `file` mode `"0644"` no `*.gpg`
  5) `apt_repository` com `signed-by=/etc/apt/keyrings/<nome>.gpg`
- Se optar por `ansible.builtin.apt_key`, então **NÃO** use `signed-by` no `apt_repository`.

CONTEXTOS:

1) CONTEXT.TYPE=playbook
   - Gere APENAS UM arquivo em `ARQUIVO_ALVO=<name>.yml`.
   - O play deve começar com: `- hosts: <PLAYBOOK_HOST>`.
   - Aplique `become: true` se o SPEC pedir.
   - Aplique `tags: [...]` quando o SPEC citar tags.
   - Se pedir “inventário de exemplo no README”, crie `README.md` com exemplo e execução.

2) CONTEXT.TYPE=role
   - Estrutura obrigatória (substitua <ROLE_NAME>):
     <ROLE_NAME>/
       README.md
       defaults/main.yml
       handlers/main.yml
       meta/main.yml
       meta/.galaxy_install_info
       tasks/main.yml
       tasks/install.yml
       tests/inventory
       tests/test.yml
       vars/main.yml
   - `tasks/main.yml` deve fazer `import_tasks: install.yml`.
   - Handlers devem iniciar/reiniciar serviços quando aplicável.
   - `meta/main.yml` com `galaxy_info.role_name`, `author`, `description`, `license`, `min_ansible_version`, `platforms`, `galaxy_tags`, `dependencies: []`.
   - Use variáveis em `defaults/main.yml`.
NUNCA inclua nada além do JSON.
"""

# ============================ Helpers de instrução ===========================
def build_instruction(kind: str, args: argparse.Namespace) -> str:
    spec = (args.spec or "").strip()
    if kind == "playbook":
        ctx = f"CONTEXT.TYPE=playbook\nARQUIVO_ALVO={args.name}.yml\nPLAYBOOK_HOST={args.host}\n"
        return ctx + "\n" + spec
    if kind == "role":
        ctx = f"CONTEXT.TYPE=role\nROLE_NAME={args.name}\nROLE_HOST={args.host}\n"
        return ctx + "\n" + spec
    raise SystemExit(f"Comando não suportado: {kind}")

# ============================ Saneamento YAML ================================
_YAML_FILES = re.compile(r'\.(ya?ml)$', re.IGNORECASE)

def _ensure_yaml_header(path: str, content: str) -> str:
    if not _YAML_FILES.search(path):
        return content
    base = pathlib.Path(path).name
    if base in ("inventory", ".galaxy_install_info"):
        return content
    txt = content.lstrip()
    if not txt.startswith("---"):
        return "---\n" + content.lstrip()
    return content

def _force_hosts_in_playbook(content: str, host: str) -> str:
    return re.sub(r'(?m)^-\s*hosts\s*:\s*.+$', f"- hosts: {host}", content, count=1)

def _normalize_ansible_builtins(content: str) -> str:
    """
    Qualifica módulos SOMENTE na linha do módulo da task:
      - Início de task sem name: '- <module>:'
      - OU linha do módulo 2 espaços abaixo do '- name:'
    Nunca toca parâmetros (ex.: 'group:', 'user:', 'line:').
    """
    modules = {
        "apt","apt_key","apt_repository","yum","dnf","service",
        "copy","template","file","unarchive","get_url","pip",
        "shell","command","systemd","lineinfile"
    }
    lines = content.splitlines()
    out: List[str] = []
    current_task_indent: Optional[int] = None
    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if re.match(r'^\s*-\s*name\s*:\s*', line):
            current_task_indent = indent
            out.append(line); continue

        m_dash_module = re.match(r'^(\s*)-\s*([A-Za-z_]\w*)\s*:\s*$', line)
        if m_dash_module:
            ind = len(m_dash_module.group(1)); key = m_dash_module.group(2)
            if key in modules and not key.startswith("ansible.builtin."):
                out.append(f"{' ' * ind}- ansible.builtin.{key}:")
                current_task_indent = ind; continue
            out.append(line); current_task_indent = ind; continue

        m_module = re.match(r'^(\s*)([A-Za-z_]\w*)\s*:\s*$', line)
        if m_module and current_task_indent is not None and indent == current_task_indent + 2:
            key = m_module.group(2)
            if key in modules and not key.startswith("ansible.builtin."):
                out.append(f"{' ' * indent}ansible.builtin.{key}:"); continue

        m_nested_acc = re.match(r'^(\s*)ansible\.builtin\.(group|user|line)\s*:\s*$', line)
        if m_nested_acc and (current_task_indent is None or indent > current_task_indent + 1):
            out.append(f"{' ' * indent}{m_nested_acc.group(2)}:"); continue

        out.append(line)
    return "\n".join(out)

def _repair_common_nested_accidents(content: str) -> str:
    lines = content.splitlines()
    out: List[str] = []
    current_task_indent: Optional[int] = None
    for line in lines:
        indent = len(line) - len(line.lstrip())
        if re.match(r'^\s*-\s*name\s*:\s*', line) or re.match(r'^\s*-\s*[A-Za-z_]\w*\s*:\s*$', line):
            current_task_indent = indent
        m = re.match(r'^(\s*)ansible\.builtin\.(group|user)\s*:\s*$', line)
        if m and (current_task_indent is None or indent > current_task_indent + 1):
            out.append(f"{' ' * indent}{m.group(2)}:")
        else:
            out.append(line)
    return "\n".join(out)

def _quote_jinja_values(content: str) -> str:
    """
    Cota valores que começam com '{{' para evitar erro YAML:
      repo: {{ var }}  -> repo: "{{ var }}"
    """
    def repl(m):
        prefix = m.group(1)
        val = m.group(2).strip()
        if val.startswith('"') or val.startswith("'"):
            return m.group(0)
        return f'{prefix}"{val}"'
    pattern = re.compile(r'^(\s*[A-Za-z_][\w]*\s*:\s*)(\{\{.*\}\}.*)$', re.MULTILINE)
    return pattern.sub(repl, content)

def _has_keyring_setup(txt: str) -> bool:
    return ("/etc/apt/keyrings/" in txt) or ("gpg --dearmor" in txt) or ("/usr/share/keyrings/" in txt)

def _has_apt_key(txt: str) -> bool:
    return re.search(r'(?m)ansible\.builtin\.apt_key\s*:', txt) is not None

def _fix_apt_repo_signed_by_if_needed(txt: str) -> str:
    """
    Se houver apt_repository com 'signed-by=' mas não houver tasks de keyring
    e existir apt_key, removemos 'signed-by' (evita NO_PUBKEY).
    """
    if "ansible.builtin.apt_repository" not in txt: return txt
    if _has_keyring_setup(txt): return txt
    if not _has_apt_key(txt): return txt
    def _strip(line: str) -> str:
        return re.sub(r'(repo:\s*deb\s*\[[^\]]*?)\s*signed-by=[^\]\s]+', r'\1', line)
    return "\n".join(_strip(l) if ("repo:" in l and "signed-by=" in l) else l for l in txt.splitlines())

def _fold_top_level_signed_by_into_repo(content: str) -> str:
    """
    Conserta tasks apt_repository que tenham 'signed-by:' como parâmetro de topo.
    Move para dentro de repo: "deb [signed-by=...] <url> <dist> <comp>".
    """
    lines = content.splitlines()
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        out.append(line)
        if re.match(r'^\s*ansible\.builtin\.apt_repository\s*:\s*$', line):
            parent_indent = len(line) - len(line.lstrip())
            block = []
            i += 1
            while i < n and (len(lines[i]) - len(lines[i].lstrip())) > parent_indent:
                block.append(lines[i]); i += 1
            repo_idx = next((k for k,l in enumerate(block) if re.match(r'^\s*repo\s*:\s*', l)), None)
            sb_idx   = next((k for k,l in enumerate(block) if re.match(r'^\s*signed-by\s*:\s*', l, re.I)), None)
            if repo_idx is not None and sb_idx is not None:
                repo_line = block[repo_idx]
                sb_line   = block[sb_idx]
                path = re.sub(r'^\s*signed-by\s*:\s*', '', sb_line, flags=re.I).strip().strip('"\'')

                m = re.match(r'^(\s*repo\s*:\s*)(["\']?)(.+?)\2\s*$', repo_line)
                if m:
                    prefix, quote, repo_val = m.groups()
                    if re.match(r'^\s*deb\s+\[', repo_val):
                        repo_val = re.sub(r'^\s*(deb\s+\[)([^\]]*)(\])', rf'\1\2 signed-by={path}\3', repo_val, count=1)
                    elif re.match(r'^\s*deb\s+\S+', repo_val):
                        repo_val = re.sub(r'^\s*(deb\s+)', rf'\1[signed-by={path}] ', repo_val, count=1)
                    new_repo_line = f"{prefix}{quote}{repo_val}{quote}"
                    block[repo_idx] = new_repo_line
                    del block[sb_idx]
            out.extend(block)
            continue
        i += 1
    return "\n".join(out)

# ====================== Patch canônico: Grafana ==============================
def _grafana_canonical_install_yml() -> str:
    return """---
- name: Install prerequisites
  ansible.builtin.apt:
    name:
      - apt-transport-https
      - software-properties-common
      - wget
      - ca-certificates
    state: present
    update_cache: true

- name: Create APT keyring directory
  ansible.builtin.file:
    path: /etc/apt/keyrings
    state: directory
    mode: "0755"

- name: Download Grafana GPG key
  ansible.builtin.get_url:
    url: https://apt.grafana.com/gpg.key
    dest: /etc/apt/keyrings/grafana.asc
    mode: "0644"

- name: Convert Grafana key to GPG keyring
  ansible.builtin.command: gpg --dearmor -o /etc/apt/keyrings/grafana.gpg /etc/apt/keyrings/grafana.asc
  args:
    creates: /etc/apt/keyrings/grafana.gpg

- name: Set permissions on Grafana keyring
  ansible.builtin.file:
    path: /etc/apt/keyrings/grafana.gpg
    mode: "0644"

- name: Add Grafana repository
  ansible.builtin.apt_repository:
    repo: "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main"
    state: present
    filename: grafana
    update_cache: true

- name: Install Grafana
  ansible.builtin.apt:
    name: grafana
    state: present
    update_cache: true
  notify: restart grafana

- name: Enable and start Grafana service
  ansible.builtin.systemd:
    name: grafana-server
    state: started
    enabled: true
    daemon_reload: true
"""

def _ensure_grafana_handlers(content_map: Dict[str,str], role_dir: str) -> None:
    hpath = f"{role_dir}/handlers/main.yml"
    want = """---
- name: restart grafana
  ansible.builtin.systemd:
    name: grafana-server
    state: restarted
"""
    if hpath not in content_map or "restart grafana" not in content_map[hpath]:
        content_map[hpath] = want

# ====================== Patch canônico: Elasticsearch 8.x ====================
def _elasticsearch_canonical_install_yml() -> str:
    return """---
- name: Install prerequisites
  ansible.builtin.apt:
    name:
      - apt-transport-https
      - gnupg
      - curl
      - wget
      - ca-certificates
    state: present
    update_cache: true

- name: Create keyring directory
  ansible.builtin.file:
    path: /usr/share/keyrings
    state: directory
    mode: "0755"

- name: Download Elasticsearch GPG key (ASCII)
  ansible.builtin.get_url:
    url: https://artifacts.elastic.co/GPG-KEY-elasticsearch
    dest: /usr/share/keyrings/elasticsearch.asc
    mode: "0644"

- name: Convert GPG key to keyring
  ansible.builtin.command: gpg --dearmor -o /usr/share/keyrings/elasticsearch-keyring.gpg /usr/share/keyrings/elasticsearch.asc
  args:
    creates: /usr/share/keyrings/elasticsearch-keyring.gpg

- name: Set permissions on keyring
  ansible.builtin.file:
    path: /usr/share/keyrings/elasticsearch-keyring.gpg
    mode: "0644"

- name: Add Elasticsearch 8.x repository
  ansible.builtin.apt_repository:
    repo: "deb [signed-by=/usr/share/keyrings/elasticsearch-keyring.gpg] https://artifacts.elastic.co/packages/8.x/apt stable main"
    state: present
    filename: elastic-8.x
    update_cache: true

- name: Install Elasticsearch
  ansible.builtin.apt:
    name: elasticsearch
    state: present
    update_cache: true
  notify: restart elasticsearch

- name: Enable and start Elasticsearch
  ansible.builtin.systemd:
    name: elasticsearch
    state: started
    enabled: true
    daemon_reload: true
"""

def _ensure_elasticsearch_handlers(content_map: Dict[str,str], role_dir: str) -> None:
    hpath = f"{role_dir}/handlers/main.yml"
    want = """---
- name: restart elasticsearch
  ansible.builtin.systemd:
    name: elasticsearch
    state: restarted
"""
    if hpath not in content_map or "restart elasticsearch" not in content_map[hpath]:
        content_map[hpath] = want

# ============================ Checklist =====================================
EXTERNAL_REPOS = {
    "grafana": r"https?://(apt|packages)\.grafana\.com",
    "docker": r"https?://download\.docker\.com",
    "kubernetes": r"https?://apt\.kubernetes\.io|https?://pkgs\.k8s\.io",
    "elastic": r"https?://artifacts\.elastic\.co",
    "gitlab": r"https?://packages\.gitlab\.com",
}

def _find_notify_handlers(yaml_text: str) -> List[str]:
    pats = re.findall(r'^\s*notify\s*:\s*(.+)$', yaml_text, flags=re.MULTILINE)
    names: List[str] = []
    for p in pats:
        if p.strip().startswith("-"):
            names += [x.strip("- ").strip() for x in re.findall(r'-\s*([^\n]+)', p)]
        else:
            names.append(p.strip())
    return [n.strip('"\' ') for n in names]

def _find_handlers_defined(yaml_text: str) -> List[str]:
    return [m.group(1).strip('"\' ') for m in re.finditer(r'^\s*-\s*name\s*:\s*(.+)$', yaml_text, flags=re.MULTILINE)]

def _extract_templates_used(yaml_text: str) -> List[str]:
    return [m.group(1).strip(' "\'') for m in re.finditer(r'^\s*src\s*:\s*([^\n]+)$', yaml_text, flags=re.MULTILINE)]

def _extract_vars_used(yaml_text: str) -> List[str]:
    return list(set([m.strip() for m in re.findall(r'{{\s*([a-zA-Z_][\w\.]*)\s*}}', yaml_text)]))

def _load_content_map(files: List[Dict[str,str]]) -> Dict[str,str]:
    return {f["path"]: f.get("content","") for f in files}

def validate_generated_files(mode: str, files: List[Dict[str,str]], role_name: Optional[str], strict: bool=False) -> Tuple[int,int]:
    warnings = 0; errors = 0
    fm = _load_content_map(files)

    def warn(msg): nonlocal warnings; warnings += 1; print(f"[WARN] {msg}")
    def err(msg):  nonlocal errors; errors += 1; print(f"[ERROR] {msg}")

    # 1) Header '---'
    for p,c in fm.items():
        if p.endswith((".yml",".yaml")) and pathlib.Path(p).name not in ("inventory",):
            if not c.lstrip().startswith("---"):
                warn(f"Arquivo sem header '---': {p}")

    if mode == "role" and role_name:
        # 2) tasks/main.yml importa install.yml
        tmain = f"{role_name}/tasks/main.yml"
        if tmain in fm:
            if "import_tasks: install.yml" not in fm[tmain]:
                err(f"{tmain} não contém 'import_tasks: install.yml'")
        else:
            err(f"Ausente: {tmain}")

        # 3) handlers notificados existem
        hmain = f"{role_name}/handlers/main.yml"
        if hmain in fm:
            used = _find_notify_handlers("\n".join(fm[p] for p in fm if p.endswith(".yml")))
            defined = _find_handlers_defined(fm[hmain])
            missing = [n for n in used if n and n not in defined]
            if missing:
                err(f"Handlers notificados não definidos: {', '.join(sorted(set(missing)))}")
        else:
            warn(f"Ausente: {hmain}")

        # 4) templates existem
        templates = []
        for p,c in fm.items():
            if p.endswith(".yml"): templates += _extract_templates_used(c)
        for src in set(templates):
            tpath = f"{role_name}/templates/{pathlib.Path(src).name}"
            if tpath not in fm:
                warn(f"Template referenciado ausente: {tpath} (src: {src})")

        # 5) defaults contêm variáveis usadas
        dmain = f"{role_name}/defaults/main.yml"
        used_vars = set()
        for p,c in fm.items():
            if p.endswith(".yml") and not p.endswith("defaults/main.yml"):
                used_vars |= set(_extract_vars_used(c))
        if dmain in fm:
            defined_vars = set(re.findall(r'^([a-zA-Z_]\w*)\s*:', fm[dmain], flags=re.MULTILINE))
            missing_vars = [v for v in used_vars if v.split('.')[0] not in defined_vars and not v.startswith("ansible_")]
            if missing_vars:
                warn(f"Variáveis usadas e não definidas em defaults: {', '.join(sorted(set(missing_vars)))}")
        else:
            warn(f"Ausente: {dmain}")

        # 6) systemd: enabled+started
        for p,c in fm.items():
            if p.endswith(".yml") and "ansible.builtin.systemd:" in c:
                if "enabled:" not in c or "state: started" not in c:
                    warn(f"{p}: verifique systemd (esperado enabled: true e state: started)")

        # 7) modos como string
        for p,c in fm.items():
            if p.endswith(".yml"):
                bad_modes = re.findall(r'^\s*mode\s*:\s*(0[0-7]{3})\s*$', c, flags=re.MULTILINE)
                if bad_modes:
                    warn(f"{p}: modos não estão como string (ex.: \"{bad_modes[0]}\")")

        # 8) repos externos precisam de keyring + signed-by
        for repo_name, pattern in EXTERNAL_REPOS.items():
            hit = any(p.endswith(".yml") and re.search(pattern, c) for p,c in fm.items())
            if hit:
                if not _has_keyring_setup("\n".join(fm.values())):
                    err(f"Repo '{repo_name}' detectado sem keyring (file/get_url + gpg --dearmor).")
                ok_signed = any("repo:" in c and "signed-by=" in c for p,c in fm.items() if p.endswith(".yml"))
                if not ok_signed:
                    err(f"Repo '{repo_name}' detectado sem '[signed-by=...]' no apt_repository.")

    print(f"\nChecklist: {warnings} aviso(s), {errors} erro(s).")
    if strict and errors:
        print("Falha em modo --strict."); sys.exit(3)
    return warnings, errors

# ============================ Pós-processamento ==============================
def sanitize_files(mode: str, files: List[Dict[str,str]], name: str, host: str, spec: str) -> List[Dict[str,str]]:
    out: List[Dict[str,str]] = []
    have_readme = False
    lower_name = name.lower()
    is_es = lower_name in ("elasticsearch", "elastisearch")  # aceita grafia comum errada

    for f in files:
        p = f.get("path","").lstrip("./")
        c = f.get("content","")

        c = _ensure_yaml_header(p, c)

        if mode == "playbook" and p.endswith(".yml") and pathlib.Path(p).name.startswith(name):
            c = _force_hosts_in_playbook(c, host)

        c = _normalize_ansible_builtins(c)
        c = _repair_common_nested_accidents(c)
        c = _quote_jinja_values(c)
        c = _fold_top_level_signed_by_into_repo(c)  # <<<<<< fix GitLab (e afins)

        if mode == "playbook":
            c = _fix_apt_repo_signed_by_if_needed(c)

        if os.path.basename(p).lower() == "readme.md":
            have_readme = True

        # Patches canônicos por role
        if mode == "role" and lower_name == "grafana" and p == f"{name}/tasks/install.yml":
            c = _grafana_canonical_install_yml()
        if mode == "role" and is_es and p == f"{name}/tasks/install.yml":
            c = _elasticsearch_canonical_install_yml()

        c = re.sub(r'\n{3,}', '\n\n', c).rstrip() + "\n"
        out.append({"path": p, "content": c})

    # Garante handlers para roles com patch
    if mode == "role" and lower_name == "grafana":
        cmap = {f["path"]: f["content"] for f in out}
        _ensure_grafana_handlers(cmap, name)
        out = [{"path": k, "content": v} for k,v in cmap.items()]
    if mode == "role" and is_es:
        cmap = {f["path"]: f["content"] for f in out}
        _ensure_elasticsearch_handlers(cmap, name)
        out = [{"path": k, "content": v} for k,v in cmap.items()]

    if mode == "playbook" and ("inventário" in spec.lower() or "inventario" in spec.lower()) and not have_readme:
        readme = (
            f"# {name}\n\n"
            "## Inventário de exemplo\n\n"
            "```\n"
            "[local]\n"
            "localhost ansible_connection=local\n"
            "```\n\n"
            f"## Executando\n\n"
            f"ansible-playbook -i inventory {name}.yml\n"
        )
        out.append({"path":"README.md","content":readme})

    return out

# ============================ Escrita ========================================
def write_files(root: pathlib.Path, files: List[Dict[str,str]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    print(f"\nSalvando em: {root}")
    for f in files:
        path = root / f["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f.get("content",""))
        print(" -", f["path"])
    print("OK.")

# ============================ CLI ============================================
def build_parser():
    p = argparse.ArgumentParser(prog="iaops-ansible", description="IAOps Ansible — gera Playbooks e Roles com IA.")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Playbook
    spb = sub.add_parser("playbook", help="Gerar um playbook simples.")
    spb.add_argument("--name", required=True, help="Nome do playbook (sem extensão)")
    spb.add_argument("--host", required=True, help="Valor do hosts no playbook (ex.: all, local, monitoring)")
    spb.add_argument("--spec", required=True, help="Especificação em linguagem natural")
    spb.add_argument("--root", default="./", help="Diretório raiz para salvar")
    spb.add_argument("--model", default="gemini-2.5-pro")
    spb.add_argument("--temperature", type=float, default=0.2)
    spb.add_argument("--strict", action="store_true", help="Falha se o checklist achar erros")

    # Role
    srl = sub.add_parser("role", help="Gerar uma role com estrutura completa.")
    srl.add_argument("--name", required=True, help="Nome da role (diretório raiz da role)")
    srl.add_argument("--host", required=True, help="Host/grupo-alvo para testes (tests/inventory)")
    srl.add_argument("--spec", required=True, help="Especificação em linguagem natural")
    srl.add_argument("--root", default="./", help="Diretório raiz para salvar")
    srl.add_argument("--model", default="gemini-2.5-pro")
    srl.add_argument("--temperature", type=float, default=0.2)
    srl.add_argument("--strict", action="store_true", help="Falha se o checklist achar erros")

    return p

# ============================ main ===========================================
def main():
    ensure_api_key()
    parser = build_parser()
    args = parser.parse_args()

    instruction = build_instruction(args.cmd, args)
    print(f"\n[ {args.cmd} ]\nGerando com IA (modelo={args.model}, temp={args.temperature})\n")
    raw = call_model(instruction, model_name=args.model, temperature=args.temperature)

    try:
        payload = parse_ai_json(raw)
    except Exception:
        print("Resposta bruta da IA (início):\n", raw[:2000], "\n--- fim ---")
        raise SystemExit("Falha ao decodificar JSON da IA.")

    root_base = pathlib.Path(args.root).expanduser().resolve()
    files = payload.get("files") or []
    notes = payload.get("notes", "")

    if not files:
        print("A IA não retornou arquivos.")
        sys.exit(2)

    mode = "playbook" if args.cmd == "playbook" else "role"
    files = sanitize_files(mode, files, args.name, args.host, args.spec)

    # Artefatos mínimos para role
    if mode == "role":
        paths = {f["path"] for f in files}
        role_dir = args.name

        if f"{role_dir}/tests/inventory" not in paths:
            inv = f"[{args.host}]\nlocalhost ansible_connection=local\n"
            files.append({"path": f"{role_dir}/tests/inventory", "content": inv})
        if f"{role_dir}/tests/test.yml" not in paths:
            test_yml = ("---\n- hosts: {host}\n  roles:\n    - {role}\n").format(host=args.host, role=args.name)
            files.append({"path": f"{role_dir}/tests/test.yml", "content": test_yml})

        # Playbook raiz com o nome da role (para aplicar direto)
        root_playbook_path = f"{args.name}.yml"
        if root_playbook_path not in paths:
            root_playbook = ("---\n- hosts: {host}\n  roles:\n    - {role}\n").format(host=args.host, role=args.name)
            files.append({"path": root_playbook_path, "content": root_playbook})

        # meta/.galaxy_install_info padronizado
        galaxy_info_path = f"{role_dir}/meta/.galaxy_install_info"
        files = [f for f in files if f["path"] != galaxy_info_path]
        files.append({"path": galaxy_info_path, "content": "version: 1.0.0\n"})

        # Checklist
        validate_generated_files("role", files, args.name, strict=args.strict)
    else:
        validate_generated_files("playbook", files, None, strict=args.strict)

    write_files(root_base, files)

    if notes:
        print("\n[Notas da IA]\n" + str(notes))
    print("\nDicas:\n sudo ansible-playbook <playbook>.yml\n")

if __name__ == "__main__":
    main()
