#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import warnings

warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"google\.api_core\._python_version_support",
)

import os, sys, json, argparse, re, pathlib
from typing import Any, Dict, List, Optional, Tuple

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
# Idioma
- Responda sempre em Português (pt-BR). O conteúdo de "notes" também deve estar em pt-BR.

Você é um gerador de **Dockerfile** e artefatos (.dockerignore, README, arquivos mínimos da app).
A saída deve ser SOMENTE JSON:

{
  "files": [
    {"path": "caminho/relativo", "content": "conteúdo"}
  ],
  "notes": "opcional"
}

Contexto (o chamador envia a seguir):
- PROFILE=simple|secure  → nível de endurecimento.
- APP_NAME=<nome>
- PRESET=<preset ou vazio>
- SCHEMA=<json> (opcional; se presente, seguir o schema)
- SPEC=<texto livre>

REGRAS:
- **Siga o SCHEMA integralmente** se fornecido (tem prioridade sobre PRESET e SPEC).
- Se não houver SCHEMA, use PRESET (se houver) como base e complete com SPEC.
- Se não houver PRESET, atenda o SPEC livre.
- **PROFILE=simple**: mantenha a simplicidade. Permite 1 estágio; não adicione USER/HEALTHCHECK/STOPSIGNAL/labels a menos que o SPEC peça.
- **PROFILE=secure**: use boas práticas (multi-stage, USER não-root, WORKDIR, HEALTHCHECK, STOPSIGNAL SIGTERM, labels OCI).
- Sempre gere **CMD/ENTRYPOINT em JSON exec form** (sem shell): ex. ["python","app.py"].
- Gere **.dockerignore** coerente com a stack.
- README curto com comandos de build/run.
- Crie **arquivos mínimos** quando o SPEC/PRESET indicar a stack (exemplos):
  - Flask → app.py com rota "/" ("Olá, Mundo!") e /healthz; requirements.txt com Flask.
  - FastAPI → main.py + requirements.txt (fastapi, uvicorn); CMD usar ["python","-m","uvicorn",...].
  - Node/Express → package.json + server.js (porta padrão 3000).
  - Go → main.go (escuta :8080); no secure, multi-stage com binário estático.
  - Java JAR → App.java + build/run simples ou usar jar fornecido, conforme SPEC.
  - Nginx static → copiar ./public para /usr/share/nginx/html.
- Respeite **ao pé da letra** o que o SPEC disser (ex.: base image, porta, 1 estágio).
- Não invente arquivos extras além dos úteis (Dockerfile, .dockerignore, README e mínimos da app).
- **NÃO** escreva nada fora do JSON.
"""

# ============================ PRESETS ========================================
PRESETS: Dict[str, str] = {
    "python-flask": (
        "Gerar Dockerfile de 1 estágio com base python:3.12-slim-bookworm para um app Flask na porta 5000. "
        "Criar app.py (rota '/'='Olá, Mundo!', rota '/healthz' JSON ok), requirements.txt (Flask). "
        "WORKDIR /app, COPY requirements.txt, RUN pip install -r requirements.txt, COPY . ., EXPOSE 5000, CMD [\"python\",\"app.py\"]."
    ),
    "python-fastapi": (
        "Gerar Dockerfile de 1 estágio com base python:3.12-slim para FastAPI porta 8080. "
        "Criar main.py e requirements.txt (fastapi, uvicorn). CMD [\"python\",\"-m\",\"uvicorn\",\"main:app\",\"--host\",\"0.0.0.0\",\"--port\",\"8080\"]."
    ),
    "node-express": (
        "Gerar Dockerfile 1 estágio com node:20-alpine para Express na porta 3000. "
        "Criar package.json mínimo e server.js (rota '/' e '/healthz'). EXPOSE 3000, CMD [\"node\",\"server.js\"]."
    ),
    "go": (
        "Gerar Dockerfile multi-stage: build com golang:1.22-alpine (CGO_ENABLED=0), runtime alpine:3.20. "
        "Criar main.go que escuta :8080 com rotas '/' e '/healthz'. COPY binário final e CMD [\"/app\"] no runtime."
    ),
    "java-jar": (
        "Gerar Dockerfile multi-stage: build eclipse-temurin:21-jdk compila App.java; runtime eclipse-temurin:21-jre roda java -jar app.jar. "
        "Criar App.java minimal (Hello), gerar jar e rodar."
    ),
    "nginx-static": (
        "Gerar Dockerfile 1 estágio com nginx:1.26-alpine, copiar ./public para /usr/share/nginx/html, EXPOSE 80."
    ),
}

# ============================ Helpers de instrução ===========================
def load_schema(path: Optional[str]) -> Optional[str]:
    if not path: return None
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        print(f"[WARN] SCHEMA não encontrado: {p}", file=sys.stderr)
        return None
    return p.read_text(encoding="utf-8")

def build_instruction(args: argparse.Namespace) -> str:
    spec = (args.spec or "").strip()
    preset = (args.preset or "").strip()
    preset_text = PRESETS.get(preset, "")
    schema_text = load_schema(args.schema)

    ctx = [
        "CONTEXT.TYPE=dockerfile",
        f"PROFILE={args.profile}",
        f"APP_NAME={args.name}",
        f"PRESET={preset}",
        f"SCHEMA={schema_text if schema_text else ''}",
        "SPEC=" + (spec if spec else preset_text),
    ]
    return "\n".join(ctx)

# ============================ Detecção/heurísticas ===========================
def _detect(spec: str, preset: str) -> Dict[str,bool]:
    s = (spec + " " + preset).lower()
    return {
        "flask": "flask" in s,
        "fastapi": "fastapi" in s,
        "node": "node" in s or "express" in s,
        "go": bool(re.search(r"\bgo\b|golang", s)),
        "java": "java" in s or "jar" in s,
        "nginx": "nginx" in s,
        "single_stage": bool(re.search(r"sem\s+multi[- ]?stage|1\s*est[aá]gio|um\s*est[aá]gio|single[- ]?stage", s)),
        "multi_stage": "multi-stage" in s or "multistage" in s or "multi stage" in s,
    }

# ============================ Sanitização & Utilitários ======================
def _prefix_paths(files: List[Dict[str,str]], dir_name: str) -> List[Dict[str,str]]:
    out: List[Dict[str,str]] = []
    for f in files:
        p = f.get("path","").lstrip("./")
        if not p: continue
        if not p.startswith(dir_name + "/"):
            p = f"{dir_name}/{p}"
        out.append({"path": p, "content": f.get("content","")})
    return out

def _ensure_file(files: List[Dict[str,str]], path: str, content: str) -> None:
    if not any(f["path"] == path for f in files):
        files.append({"path": path, "content": content})

def _ensure_dockerfile_present(files: List[Dict[str,str]]) -> None:
    if not any(pathlib.Path(f["path"]).name == "Dockerfile" for f in files):
        raise SystemExit("A IA não retornou um Dockerfile. Ajuste SPEC/PRESET/SCHEMA e tente novamente.")

def _insert_if_missing_dockerignore(files: List[Dict[str,str]], dir_name: str, spec: str) -> List[Dict[str,str]]:
    paths = {f["path"] for f in files}
    if f"{dir_name}/.dockerignore" in paths:
        return files
    patterns = [
        ".git",".gitignore",".env",".venv","__pycache__","*.pyc","*.pyo",
        "node_modules","dist","build","target",".mypy_cache",".pytest_cache",
        ".DS_Store","*.log","*.sqlite","*.db",".idea",".vscode"
    ]
    if re.search(r"\bgo\b|golang", spec, re.I): patterns += ["bin","*.test","coverage.out"]
    if re.search(r"\bnode\b|express", spec, re.I): patterns += ["package-lock.json",".npm","npm-debug.log","yarn.lock",".yarn"]
    if re.search(r"\bpython\b|flask|fastapi|django", spec, re.I): patterns += ["*.whl","*.egg-info",".ruff_cache"]
    content = "\n".join(sorted(set(patterns))) + "\n"
    files.append({"path": f"{dir_name}/.dockerignore", "content": content})
    return files

def _normalize_cmd_exec_form(dockerfile: str) -> str:
    # CMD python app.py -> CMD ["python","app.py"]
    df = re.sub(r'(?mi)^\s*CMD\s+python\s+([^\n]+)$',
                lambda m: 'CMD ["python",' + ",".join([f'"{p}"' for p in m.group(1).split()]) + "]",
                dockerfile)
    # CMD node server.js -> CMD ["node","server.js"]
    df = re.sub(r'(?mi)^\s*CMD\s+node\s+([^\n]+)$',
                lambda m: 'CMD ["node",' + ",".join([f'"{p}"' for p in m.group(1).split()]) + "]",
                df)
    # remove placeholders tipo [CMD]
    df = re.sub(r'(?mi)^\s*CMD\s+\[?\s*"?\[CMD\]"?.*$', 'CMD ["python","app.py"]', df)
    return df

def _fix_uvicorn_invocation(dockerfile: str) -> str:
    # ENTRYPOINT/CMD ["uvicorn", ...] -> ["python","-m","uvicorn", ...]
    def _arr_replace(m):
        arr = m.group(2)
        if re.search(r'["\']uvicorn["\']', arr) and not re.search(r'["\']-m["\']\s*,\s*["\']uvicorn["\']', arr):
            rest = re.sub(r'^\s*["\']uvicorn["\']\s*,\s*', '', arr)
            return m.group(1) + '["python","-m","uvicorn",' + rest.strip() + "]"
        return m.group(0)
    df = re.sub(r'(?mi)^(\s*ENTRYPOINT\s*)\[(.+)\]\s*$', _arr_replace, dockerfile)
    df = re.sub(r'(?mi)^(\s*CMD\s*)\[(.+)\]\s*$', _arr_replace, df)
    return df

def _ensure_pythonpath_for_sitepackages(dockerfile: str) -> str:
    if "site-packages" not in dockerfile or re.search(r'(?mi)^\s*ENV\s+PYTHONPATH\s*=', dockerfile):
        return dockerfile
    lines = dockerfile.strip().splitlines()
    last_from_idx = max([i for i,l in enumerate(lines) if re.match(r'(?mi)^\s*FROM\s+', l)], default=-1)
    insert_pos = last_from_idx + 1 if last_from_idx != -1 else 0
    lines.insert(insert_pos, "ENV PYTHONPATH=/app/site-packages")
    return "\n".join(lines) + ("\n" if not dockerfile.endswith("\n") else "")

def _ensure_stack_minimals(files: List[Dict[str,str]], dir_name: str, intent: Dict[str,bool]) -> None:
    # Python Flask
    if intent["flask"]:
        if not any(pathlib.Path(f["path"]).name == "app.py" for f in files):
            _ensure_file(files, f"{dir_name}/app.py",
                "from flask import Flask\n\napp = Flask(__name__)\n\n@app.get('/')\ndef hello():\n    return 'Olá, Mundo!'\n\n@app.get('/healthz')\ndef healthz():\n    return {'status':'ok'}\n\nif __name__ == '__main__':\n    app.run(host='0.0.0.0', port=5000)\n")
        if not any(pathlib.Path(f["path"]).name == "requirements.txt" for f in files):
            _ensure_file(files, f"{dir_name}/requirements.txt", "Flask\n")

    # Python FastAPI
    if intent["fastapi"]:
        if not any(pathlib.Path(f["path"]).name == "main.py" for f in files):
            _ensure_file(files, f"{dir_name}/main.py",
                "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/healthz')\ndef h(): return {'status':'ok'}\n@app.get('/')\ndef r(): return {'msg':'hello'}\n")
        if not any(pathlib.Path(f["path"]).name == "requirements.txt" for f in files):
            _ensure_file(files, f"{dir_name}/requirements.txt", "fastapi\nuvicorn\n")

    # Node/Express
    if intent["node"]:
        if not any(pathlib.Path(f["path"]).name == "server.js" for f in files):
            _ensure_file(files, f"{dir_name}/server.js",
                "const express=require('express');const app=express();\napp.get('/healthz',(req,res)=>res.json({status:'ok'}));\napp.get('/',(req,res)=>res.send('Hello from Express!'));\napp.listen(3000,'0.0.0.0',()=>console.log('listening on :3000'));\n")
        if not any(pathlib.Path(f["path"]).name == "package.json" for f in files):
            _ensure_file(files, f"{dir_name}/package.json",
                '{\n  "name": "node-app",\n  "private": true,\n  "version": "1.0.0",\n  "main": "server.js",\n  "type": "module",\n  "dependencies": { "express": "^4.19.2" }\n}\n')

    # Go
    if intent["go"]:
        if not any(pathlib.Path(f["path"]).name in ("main.go","app.go") for f in files):
            _ensure_file(files, f"{dir_name}/main.go",
                'package main\nimport ("fmt";"log";"net/http")\nfunc main(){http.HandleFunc("/healthz",func(w http.ResponseWriter,r *http.Request){w.Header().Set("Content-Type","application/json");w.WriteHeader(200);w.Write([]byte(`{"status":"ok"}`))});http.HandleFunc("/",func(w http.ResponseWriter,r *http.Request){fmt.Fprintln(w,"hello from go")});log.Fatal(http.ListenAndServe(":8080",nil))}\n')

    # Java (minimal)
    if intent["java"]:
        if not any(str(f["path"]).endswith(".java") for f in files):
            _ensure_file(files, f"{dir_name}/App.java",
                'public class App { public static void main(String[] args){ System.out.println("Hello, World!"); } }')

def _inject_secure_defaults(dockerfile: str, want_uid_gid: str = "10001:10001") -> str:
    lines = dockerfile.strip().splitlines()
    idx = [i for i,l in enumerate(lines) if re.match(r'(?mi)^\s*FROM\s+', l)]
    if not idx: return dockerfile
    last_from_idx = idx[-1]
    tail = "\n".join(lines[last_from_idx:])
    insert_pos = last_from_idx + 1
    if not re.search(r'(?mi)^\s*WORKDIR\s+', tail):
        lines.insert(insert_pos, "WORKDIR /app"); insert_pos += 1
    if not re.search(r'(?mi)^\s*USER\s+', tail):
        lines.insert(insert_pos, f"USER {want_uid_gid}"); insert_pos += 1
    if not re.search(r'(?mi)^\s*STOPSIGNAL\b', tail):
        lines.insert(insert_pos, "STOPSIGNAL SIGTERM"); insert_pos += 1
    return "\n".join(lines) + ("\n" if not dockerfile.endswith("\n") else "")

def _ensure_labels_oci(dockerfile: str, app_name: str, version: str = "1.0.0") -> str:
    if re.search(r'(?mi)^\s*LABEL\s+org\.opencontainers\.image\.', dockerfile): return dockerfile
    lines = dockerfile.strip().splitlines()
    idx = [i for i,l in enumerate(lines) if re.match(r'(?mi)^\s*FROM\s+', l)]
    if not idx: return dockerfile
    i = idx[-1]
    labels = [
        f'LABEL org.opencontainers.image.title="{app_name}"',
        'LABEL org.opencontainers.image.description="Generated via IAOps"',
        f'LABEL org.opencontainers.image.version="{version}"',
        f'LABEL org.opencontainers.image.source="https://example.com/{app_name}"'
    ]
    for k, lab in enumerate(labels, start=1):
        lines.insert(i + k, lab)
    return "\n".join(lines) + ("\n" if not dockerfile.endswith("\n") else "")

def sanitize_files(files: List[Dict[str,str]], name: str, spec: str, preset: str, profile: str) -> List[Dict[str,str]]:
    files = _prefix_paths(files, name)
    files = [f for f in files if pathlib.Path(f["path"]).name != "dockerignore"]
    _ensure_dockerfile_present(files)
    files = _insert_if_missing_dockerignore(files, name, spec + " " + preset)

    intent = _detect(spec, preset)
    _ensure_stack_minimals(files, name, intent)

    # Pós-processamento do Dockerfile
    for f in files:
        if pathlib.Path(f["path"]).name != "Dockerfile":
            continue
        df = f.get("content","")
        df = _normalize_cmd_exec_form(df)
        if intent["fastapi"]:
            df = _fix_uvicorn_invocation(df)
            df = _ensure_pythonpath_for_sitepackages(df)
        if profile == "secure":
            df = _inject_secure_defaults(df)
            df = _ensure_labels_oci(df, app_name=name)
        df = re.sub(r'\n{3,}', '\n\n', df).rstrip() + "\n"
        f["content"] = df
    return files

# ============================ Checklist =====================================
def validate_dockerfile(files: List[Dict[str,str]], name: str, spec: str, preset: str, profile: str, strict: bool=False) -> Tuple[int,int]:
    warnings = 0; errors = 0
    def warn(msg): nonlocal warnings; warnings += 1; print(f"[WARN] {msg}")
    def err(msg):  nonlocal errors; errors += 1; print(f"[ERROR] {msg}")

    df = next((f["content"] for f in files if pathlib.Path(f["path"]).name == "Dockerfile"), "")
    if not df:
        err("Dockerfile ausente."); 
        if strict: sys.exit(3)
        return warnings, errors

    intent = _detect(spec, preset)
    multi = len(re.findall(r'(?m)^\s*FROM\s+.+$', df)) >= 2

    # Multi-stage esperado só se profile secure OU SPEC/PRESET pedir
    if (profile == "secure" or intent["multi_stage"]) and not intent["single_stage"]:
        if not multi:
            err("Esperado multi-stage de acordo com o perfil/SPEC/PRESET.")

    # USER não-root exigido só no secure
    if profile == "secure" and not re.search(r'(?mi)^\s*USER\s+([^\n]+)$', df):
        err("Esperado USER não-root no último estágio (perfil secure).")

    # CMD/ENTRYPOINT é obrigatório sempre
    if not re.search(r'(?mi)^\s*(CMD|ENTRYPOINT)\s+', df):
        err("CMD/ENTRYPOINT ausente.")

    # WORKDIR avisado se faltar
    if not re.search(r'(?mi)^\s*WORKDIR\s+/', df):
        warn("WORKDIR não definido.")

    print(f"\nChecklist: {warnings} aviso(s), {errors} erro(s).")
    if strict and errors:
        print("Falha em modo --strict."); sys.exit(3)
    return warnings, errors

# ============================ Dicas (porta) ==================================
def _get_first_exposed_port(files: List[Dict[str,str]]) -> Optional[str]:
    """Lê o Dockerfile gerado e retorna a primeira porta do EXPOSE, se houver."""
    df = next((f["content"] for f in files if pathlib.Path(f["path"]).name == "Dockerfile"), "")
    if not df:
        return None
    m = re.search(r'(?mi)^\s*EXPOSE\s+(\d+)', df)
    if m:
        return m.group(1)
    return None

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
    p = argparse.ArgumentParser(prog="iaops-dockerfile", description="IAOps — gera Dockerfile(s) com IA (genérico).")
    p.add_argument("--name", required=True, help="Nome do app (diretório onde os arquivos serão salvos)")
    p.add_argument("--spec", default="", help="Especificação em linguagem natural")
    p.add_argument("--schema", default="", help="Caminho para um SCHEMA JSON estruturado (opcional)")
    p.add_argument("--preset", choices=list(PRESETS.keys()), default="", help="Preset opcional (ex.: python-flask, go)")
    p.add_argument("--root", default="./", help="Diretório raiz para salvar (default=./)")
    p.add_argument("--model", default="gemini-2.5-pro")
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--profile", choices=["simple","secure"], default="simple", help="Nível de endurecimento (default: simple)")
    p.add_argument("--strict", action="store_true", help="Falha se o checklist achar erros críticos")
    return p

# ============================ main ===========================================
def main():
    ensure_api_key()
    parser = build_parser()
    args = parser.parse_args()

    if not args.spec and not args.preset and not args.schema:
        print("Forneça ao menos um: --spec, --preset ou --schema.", file=sys.stderr)
        sys.exit(2)

    instruction = build_instruction(args)
    print(f"\n[ dockerfile ]\nGerando com IA (modelo={args.model}, temp={args.temperature}, profile={args.profile}, preset={args.preset or '-'} )\n")
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

    files = sanitize_files(files, args.name, args.spec, args.preset, args.profile)
    validate_dockerfile(files, args.name, args.spec, args.preset, args.profile, strict=args.strict)
    write_files(root_base, files)

    if notes:
        print("\n[Notas da IA]\n" + str(notes))

    # Dicas em português + comandos com sudo e placeholders; porta sugerida via EXPOSE
    detected_port = _get_first_exposed_port(files)

    print("\nDicas:")
    print(f"  cd {args.name}")
    print("  sudo docker image build -t <nome-da-image>:1.0.0 .")
    print("  sudo docker container run -d -p <porta-da-image>:<porta-da-image> <nome-da-image>:1.0.0")
    if detected_port:
        print(f"  # porta sugerida detectada no Dockerfile: {detected_port}")

if __name__ == "__main__":
    main()
