#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import warnings

warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"google\.api_core\._python_version_support",
)

import os, sys, json, argparse, pathlib, re
from typing import Any, Dict, List, Tuple

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
Você é um gerador de **pipelines GitLab CI** que produz SOMENTE um arquivo `.gitlab-ci.yml`, em YAML puro.

SAÍDA (JSON estrito):
{
  "files": [
    {"path": ".gitlab-ci.yml", "content": "conteúdo YAML do CI"}
  ],
  "notes": "opcional"
}
NÃO escreva nada fora desse JSON.

REGRAS:
- Gere **apenas** `.gitlab-ci.yml` (um único arquivo).
- Indente com 2 espaços. Sem comentários fora do YAML. Nada de Markdown.
- Use chaves: stages, variables, before_script, jobs (build/push/deploy), rules, needs, artifacts, cache, tags.
- Quando Docker + GCP:
  - variables: IMAGE_TAG="${CI_COMMIT_SHORT_SHA}", IMAGE_URI="${GAR_HOST}/${GCP_PROJECT_ID}/${REGISTRY_REPO}/${IMAGE_NAME}:${IMAGE_TAG}", RENDERED_MANIFEST, USE_GKE_GCLOUD_AUTH_PLUGIN="True".
  - before_script: autenticar com **GCP_SA_KEY** (suportar file variable OU conteúdo), `gcloud config set project`, `auth configure-docker` para GAR, instalar `gke-gcloud-auth-plugin` tolerando erro.
  - Stages: [build, push, deploy], jobs com tags configuráveis; needs; artifacts do gcp-key.json.
  - Deploy: `gcloud container clusters get-credentials`, criar namespace, `sed` para render, `kubectl apply` e `kubectl rollout status`.
- Ajuste ao que o usuário pedir, mantendo YAML válido. NÃO inclua nada além do JSON.
"""

# ============================ Preset canônico ================================
# (Sem GCP_SA_KEY em variables — deve vir das CI/CD Variables do GitLab)
CANONICAL_GKE_GAR_YML = """stages: [build, push, deploy]

variables:
  IMAGE_TAG: "${CI_COMMIT_SHORT_SHA}"
  IMAGE_URI: "${GAR_HOST}/${GCP_PROJECT_ID}/${REGISTRY_REPO}/${IMAGE_NAME}:${IMAGE_TAG}"
  RENDERED_MANIFEST: "k8s/deploy.rendered.yaml"
  USE_GKE_GCLOUD_AUTH_PLUGIN: "True"

before_script:
  - |
    if [ -n "$GCP_SA_KEY" ] && [ -f "$GCP_SA_KEY" ]; then
      echo "Usando GCP_SA_KEY como arquivo (File variable)."
      KEY_FILE="$GCP_SA_KEY"
    else
      echo "GCP_SA_KEY é conteúdo; salvando em gcp-key.json."
      printf '%s' "$GCP_SA_KEY" > "${CI_PROJECT_DIR}/gcp-key.json"
      KEY_FILE="${CI_PROJECT_DIR}/gcp-key.json"
    fi
  - gcloud auth activate-service-account --key-file="${KEY_FILE}"
  - gcloud --quiet config set project "${GCP_PROJECT_ID}"
  - gcloud --quiet auth configure-docker "${GAR_HOST}"
  - gcloud components install -q gke-gcloud-auth-plugin || true

build:
  stage: build
  tags: [docker]
  script:
    - docker build -t "${IMAGE_URI}" .
  artifacts:
    when: always
    paths: [ gcp-key.json ]
    expire_in: 1h

push:
  stage: push
  needs: [build]
  tags: [docker]
  script:
    - docker push "${IMAGE_URI}"

deploy:
  stage: deploy
  needs: [push]
  tags: [docker]
  script:
    - gcloud container clusters get-credentials "${GKE_CLUSTER}" --zone "${GKE_LOCATION}"
    - kubectl get ns "${GKE_NAMESPACE}" >/dev/null 2>&1 || kubectl create ns "${GKE_NAMESPACE}"
    - sed -e "s#REPO_IMAGE_PLACEHOLDER#${IMAGE_URI}#g" "${K8S_MANIFEST}" > "${RENDERED_MANIFEST}"
    - kubectl apply -n "${GKE_NAMESPACE}" -f "${RENDERED_MANIFEST}"
    - kubectl rollout status -n "${GKE_NAMESPACE}" deploy/${DEPLOY_NAME} --timeout=90s
"""

# ============================ Helpers CLI/Prompt =============================
def build_instruction(args: argparse.Namespace) -> str:
    ctx = (
        "CONTEXT.TYPE=pipeline\n"
        f"PIPELINE_NAME={args.name}\n"
        f"RUNNER_TAGS={','.join(args.tags)}\n"
        f"PRESET={args.preset}\n"
    )
    hints = []
    if args.preset == "gke-gar":
        hints.append(
            "Gere um .gitlab-ci.yml para Docker + Google Artifact Registry + deploy no GKE "
            "com stages [build, push, deploy], variables (IMAGE_TAG, IMAGE_URI, RENDERED_MANIFEST, USE_GKE_GCLOUD_AUTH_PLUGIN), "
            "before_script com autenticação via GCP_SA_KEY (arquivo ou conteúdo), gcloud configure-docker e plugin GKE; "
            "jobs devem usar as tags de RUNNER_TAGS; needs [build]→push e [push]→deploy; kubectl apply e rollout status."
        )
    spec = (args.spec or "").strip()
    body = "\n\n".join([s for s in [ctx, spec, "\n".join(hints)] if s])
    return body

def parse_var_kv(items: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for it in items or []:
        if "=" not in it:
            print(f"[WARN] Ignorando variável sem '=': {it}", file=sys.stderr)
            continue
        k, v = it.split("=", 1)
        k = k.strip(); v = v.strip()
        if not k:
            print(f"[WARN] Chave vazia em --var: {it}", file=sys.stderr); continue
        out[k] = v
    return out

def parse_tags_list(val: str) -> List[str]:
    parts = [p.strip() for chunk in val.split(",") for p in chunk.split()]
    return [p for p in parts if p]

# ============================ Validação ======================================
def _contains_all(txt: str, keys: List[str]) -> bool:
    return all(k in txt for k in keys)

def validate_ci_yaml(yaml_text: str) -> Tuple[int, int]:
    warnings = 0
    errors = 0
    def warn(msg):
        nonlocal warnings; warnings += 1; print(f"[WARN] {msg}", file=sys.stderr)
    def err(msg):
        nonlocal errors; errors += 1; print(f"[ERROR] {msg}", file=sys.stderr)

    if not yaml_text.strip():
        err(".gitlab-ci.yml vazio")

    must_have = ["stages:", "variables:", "before_script:"]
    if not _contains_all(yaml_text, must_have):
        warn("YAML não contém todas as seções básicas (stages/variables/before_script).")

    if "build:" in yaml_text and "docker build" not in yaml_text:
        warn("Job 'build' sem 'docker build'?")
    if "push:" in yaml_text and "docker push" not in yaml_text:
        warn("Job 'push' sem 'docker push'?")
    if "deploy:" in yaml_text and ("kubectl apply" not in yaml_text and "gcloud run" not in yaml_text):
        warn("Job 'deploy' sem comando de deploy detectável (kubectl/gcloud).")

    if "push:" in yaml_text and "needs:" not in yaml_text:
        warn("Job 'push' sem 'needs: [build]'?")
    if "deploy:" in yaml_text and "needs:" not in yaml_text:
        warn("Job 'deploy' sem 'needs: [push]'?")

    return warnings, errors

# ============================ Injeções (variables & tags) ====================
VAR_SECTION_RE = re.compile(r'(?ms)^variables:\s*\n')

def _existing_variable_keys(yaml_text: str) -> List[str]:
    keys = []
    m = VAR_SECTION_RE.search(yaml_text)
    if not m:
        return keys
    start = m.end()
    rest = yaml_text[start:]
    for line in rest.splitlines():
        if not line.strip():
            continue
        if not line.startswith("  "):
            break
        kv = re.match(r'^\s{2}([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$', line)
        if kv:
            keys.append(kv.group(1))
    return keys

def inject_variables(yaml_text: str, extra_vars: Dict[str, str], override: bool=False) -> str:
    if not extra_vars:
        return yaml_text if yaml_text.endswith("\n") else (yaml_text + "\n")

    m = VAR_SECTION_RE.search(yaml_text)
    if m:
        start = m.end()
        before = yaml_text[:start]
        rest = yaml_text[start:]
        lines = rest.splitlines()
        out = []
        existing_idx = {}

        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("  "):
                mkv = re.match(r'^\s{2}([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$', line)
                if mkv:
                    existing_idx[mkv.group(1)] = len(out)
                out.append(line)
                i += 1
            else:
                break

        for k, v in extra_vars.items():
            new_line = f'  {k}: "{v}"'
            if k in existing_idx:
                if override:
                    out[existing_idx[k]] = new_line
            else:
                out.append(new_line)

        rebuilt = before + "\n".join(out) + "\n" + "\n".join(lines[i:]).lstrip("\n")
        return rebuilt if rebuilt.endswith("\n") else (rebuilt + "\n")

    # se não havia variables:, cria uma
    block = "variables:\n" + "\n".join(f'  {k}: "{v}"' for k,v in extra_vars.items()) + "\n\n"
    result = block + yaml_text
    return result if result.endswith("\n") else (result + "\n")

def _inject_or_replace_tags_in_job(block: str, tags_str: str) -> str:
    if re.search(r'(?m)^\s{2}tags\s*:\s*\[[^\]]*\]\s*$', block):
        return re.sub(r'(?m)^\s{2}tags\s*:\s*\[[^\]]*\]\s*$',
                      f"  tags: [{tags_str}]", block, count=1)
    return re.sub(r'(?m)^(  stage\s*:\s*[^\n]+\n)',
                  r'\1  tags: [' + tags_str + ']\n',
                  block, count=1)

def enforce_job_tags(yaml_text: str, tags: List[str]) -> str:
    if not tags:
        return yaml_text
    tags_str = ", ".join(tags)

    def patch_job(job_name: str, text: str) -> str:
        m = re.search(rf'(?ms)^{job_name}:\n(.*?)(?=^\S|\Z)', text)
        if not m:
            return text
        block = m.group(0)
        changed = _inject_or_replace_tags_in_job(block, tags_str)
        return text.replace(block, changed, 1)

    for job in ["build", "push", "deploy"]:
        yaml_text = patch_job(job, yaml_text)
    return yaml_text

# ============================ Saneamento & Fallback ==========================
def sanitize_or_fallback(yaml_text: str, force_preset: bool, strict: bool) -> str:
    w, e = validate_ci_yaml(yaml_text)
    if force_preset or (strict and (e > 0)):
        print("[INFO] Aplicando preset canônico GKE+GAR ao .gitlab-ci.yml.", file=sys.stderr)
        return CANONICAL_GKE_GAR_YML.strip() + "\n"
    return yaml_text if yaml_text.endswith("\n") else (yaml_text + "\n")

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
    p = argparse.ArgumentParser(prog="iaops-gitlab", description="IAOps GitLab CI — gera .gitlab-ci.yml com IA, injeta variáveis e define tags de runner.")
    sub = p.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("pipeline", help="Gerar um .gitlab-ci.yml a partir de uma especificação (linguagem natural).")
    gen.add_argument("--name", required=True, help="Nome lógico do pipeline/projeto (contexto para IA)")
    gen.add_argument("--spec", default="", help="Especificação em linguagem natural do pipeline")
    gen.add_argument("--tags", dest="tags_raw", default="docker", help="Tags do runner (ex.: 'docker, loja-online')")
    gen.add_argument("--runner-tags", dest="tags_raw_alias", default=None, help="Alias para --tags")
    gen.add_argument("--preset", choices=["auto", "gke-gar"], default="auto", help="Força preset canônico GAR+GKE")
    gen.add_argument("--root", default="./", help="Diretório para salvar (default: .)")
    gen.add_argument("--model", default="gemini-2.5-pro")
    gen.add_argument("--temperature", type=float, default=0.2)
    gen.add_argument("--strict", action="store_true", help="Usa fallback se falhar validação")
    gen.add_argument("--override-vars", action="store_true", help="Sobrescreve chaves existentes ao injetar --var")
    gen.add_argument("--var", action="append", default=[], help="Define variável (ex.: --var GAR_HOST=us-central1-docker.pkg.dev). Pode repetir.")
    return p

# ============================ main ===========================================
def main():
    ensure_api_key()
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd != "pipeline":
        print("Comando não suportado.", file=sys.stderr)
        sys.exit(2)

    # Resolve tags (suporta --tags e/ou --runner-tags)
    tags_raw = args.tags_raw_alias if args.tags_raw_alias is not None else args.tags_raw
    args.tags = parse_tags_list(tags_raw)

    user_vars = parse_var_kv(args.var)

    instruction = build_instruction(args)
    print(f"\n[ pipeline ] {args.name}\nGerando com IA (modelo={args.model}, temp={args.temperature})\n")
    raw = call_model(instruction, model_name=args.model, temperature=args.temperature)

    try:
        payload = parse_ai_json(raw)
    except Exception:
        print("Resposta bruta da IA (início):\n", raw[:2000], "\n--- fim ---", file=sys.stderr)
        raise SystemExit("Falha ao decodificar JSON da IA.")

    files = payload.get("files") or []
    first = next((f for f in files if pathlib.Path(f.get("path","")).name == ".gitlab-ci.yml"), None)

    if not first:
        print("[WARN] Sem .gitlab-ci.yml na resposta. Usando preset canônico GKE+GAR.", file=sys.stderr)
        first = {"path": ".gitlab-ci.yml", "content": CANONICAL_GKE_GAR_YML}

    force_preset = (args.preset == "gke-gar")
    content = sanitize_or_fallback(first.get("content",""), force_preset=force_preset, strict=args.strict)

    # Injeções pós-processamento
    content = inject_variables(content, user_vars, override=args.override_vars)
    content = enforce_job_tags(content, args.tags)

    # Validação final (avisos)
    validate_ci_yaml(content)

    root_base = pathlib.Path(args.root).expanduser().resolve()
    write_files(root_base, [{"path": ".gitlab-ci.yml", "content": content}])

    print("\nDicas:")
    print(" - No GitLab, crie a variável em: Projects -> Selecione o seu Projeto -> Settings -> CI/CD -> Variables -> Add variable")
    print("   • Type: File")
    print("   • Environments (scope): All")
    print("   • Visibility: Visible")
    print("   • Flags: Protect variable = ON; Expand variable reference = OFF")
    print("   • Key: GCP_SA_KEY")
    print("   • Value (File): cole o conteúdo do arquivo JSON da Service Account e salve")

if __name__ == "__main__":
    main()
