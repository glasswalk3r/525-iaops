# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

Coursework repository for 4Linux's "Infraestrutura Ágil: DevOps, SRE e Automação Inteligente em Nuvem" (course 525). It is not a single application — it's a series of per-class (`aulaNN/`) exercises that pair a hand-written infra/app artifact (Terraform, Ansible, Dockerfiles, Kubernetes/Istio/ArgoCD manifests, GitLab CI, Vault, Prometheus/Kibana alerts) with a small Python CLI tool that uses Google's Gemini API to *generate or manipulate* that artifact from a natural-language spec. Content and code comments are in Portuguese (pt-BR).

Requires a free GCP account (see README.md) for the cloud-provisioning classes (aula02+).

## Repository layout

Each `aulaNN/` directory is self-contained and maps to one class topic:

| Dir | Topic | Generator script |
|---|---|---|
| `aula01` | Gemini API hello-world | `hello_genai.py` |
| `aula02` | Terraform / GCP networking | `iaops/iaops-terraform.py` |
| `aula03`, `aula04` | Ansible playbooks/roles | `iaops/iaops-ansible.py` |
| `aula05` | Dockerfiles + a sample microservices app (`images/projeto`) | `iaops/iaops-dockerfile.py` |
| `aula06` | Kubernetes cluster | `iaops-cluster.py` |
| `aula07` | Kubernetes manifests for a microservices demo (`projeto/*.yaml`) | `iaops/iaops.kubernetes.py` |
| `aula08` | GitLab CI/CD + a Go product-catalog service and a Flask app | `iaops/iaops-gitlab.py` |
| `aula09` | HashiCorp Vault | `iaops/iaops-vault.py` |
| `aula10` | ArgoCD / Argo Rollouts canary strategies | `iaops/iaops-argocd.py` |
| `aula11` | Istio service mesh, mTLS, service accounts | `iaops/iaops-istio.py` |
| `aula12` | Prometheus metrics analysis | `iaops/iaops-analyze-prometheus-metrics.py` |
| `aula13` | Prometheus alert rules | `iaops/iaops-prometheus-alerts.py` |
| `aula14` | Elasticsearch log analysis | `iaops/iaops-analyze-elasticsearch-logs.py` |
| `aula15` | Kibana alerts | `iaops/iaops-kibana-alerts.py` |

The `aula05`/`aula07` `projeto/` trees and the `aula08/produtos` Go service and `aula08/web-flask` Flask app are sample applications the generated Dockerfiles/manifests/pipelines target — not part of the tooling itself.

## The `iaops-*.py` generator pattern

Every `iaops/iaops-*.py` script follows the same shape; understand one and you understand them all:

1. **Auth**: reads `GEMINI_API_KEY` or `GOOGLE_API_KEY` from the environment (order of preference is inconsistent between scripts — some check `GEMINI_API_KEY` first, some `GOOGLE_API_KEY` first) and exits with an error message if neither is set. Set one before running any script:
   ```sh
   export GEMINI_API_KEY="your-key-from-ai-studio"
   ```
2. **SDK**: most scripts use the older `google.generativeai` package (`pip install -U google-generativeai`); a few newer ones (`aula01`, `aula13`, `aula15`) use the newer `google-genai` package (`from google import genai`). Check a script's imports before assuming which SDK/API shape it needs.
3. **System prompt**: a large Portuguese `SYSTEM_PROMPT` string constrains the model to emit **strict JSON only** (typically `{"files":[{"path":...,"content":...}], "notes":...}` or a narrower schema like the Argo Rollouts `{"replicas":...,"strategy":...}` patch), scoped to one "mode" (e.g. Terraform's `network`/`ip`/`firewall`/`instance`).
4. **JSON extraction**: raw model output is stripped of ``` fences and parsed defensively (brace-matching fallback) since models occasionally wrap or prefix the JSON.
5. **Post-processing/sanitization**: regex-based fixups are applied to the generated HCL/YAML before writing — e.g. `aula02`'s script strips duplicate `terraform{}`/`provider{}` blocks, normalizes GCP resource names, forces heredoc startup scripts, and centralizes SSH key resources into `ssh_keys.tf`. This sanitization logic is often the most important/fragile part of a script — don't remove it without understanding what generated-code defect it's working around.
6. **CLI**: `argparse` with subcommands (e.g. `terraform network --spec "..."`, `rollout --yaml-file ... --name ... --spec ...`), a `--model` flag (default usually `gemini-2.5-pro`), `--temperature`, and an output directory flag. Output is written to disk, then usage hints (e.g. `terraform fmt && terraform validate`) are printed.

There is no shared library between scripts — each `iaops-*.py` is copy-adapted from the others per class, so expect duplicated helpers (`ensure_api`, `parse_or_die`/`parse_ai_json`, etc.) with small per-script variations. When fixing a bug in one script's helper, check whether the same bug exists in the analogous helper in other `iaops-*.py` scripts before assuming it's isolated.

## Running things

There's no top-level build/test/lint tooling — each subproject uses its own ecosystem:

- **Python generator scripts**: run directly, e.g. `python3 aula02/iaops/iaops-terraform.py terraform network --spec "..."`. No `requirements.txt` covers these; install `google-generativeai` (or `google-genai` for the newer-SDK scripts), `pyyaml`, `requests`, `pandas`, `matplotlib` as needed per script.
- **`aula08/produtos`** (Go): standard Go tooling — `go build`, `go test ./...` (see `product_catalog_test.go`). `genproto.sh` regenerates protobuf code into `genproto/`.
- **`aula08/web-flask`**: `pip install -r requirements.txt`, then `docker build -t web-flask .` / `docker run -p 5000:5000 web-flask` (per its own README.md); health check at `/healthz`.
- **`aula05/images/projeto/*`**: each service (`emailservice`, `loadgenerator`, `recommendationservice`) has its own `requirements.txt`.
- **Terraform** (`aula02/terraform`, and output of `iaops-terraform.py`): `terraform fmt -recursive && terraform validate`, then `terraform plan && terraform apply`.
- **Ansible** (`aula03/aula04` playbooks): run with `ansible-playbook <file>.yml`.
