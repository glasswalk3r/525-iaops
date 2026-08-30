---
name: ansible-gen
description: Generate an Ansible playbook or role directly on disk, following the aula04 conventions
  (idempotent fully-qualified ansible.builtin modules, apt keyring/signed-by handling for external
  repos, standard role layout). Use when asked to create/generate an Ansible playbook or role for
  aula04 — this replaces calling iaops-ansible.py / the Gemini API.
---

You are generating Ansible content **directly as files on disk** — you are not producing JSON for
another program to parse, and you must not call any Gemini/Google API or run `iaops-ansible.py`.
Write the files yourself with your own file tools, following the rules below exactly.

## Inputs

Parse from the user's request (ask for anything required that's missing):

- `mode`: `playbook` or `role` (required)
- `name`: playbook name (no extension) or role name (required)
- `spec`: natural-language description of what it should do (required)
- `root`: directory to write into (default: current directory)
- `strict`: if the user says to be strict, treat any checklist error (see below) as something you
  must fix before finishing, not just flag

## Output rules (apply to every file)

- Indent YAML with 2 spaces. Every YAML file starts with `---`, except plain text files (README,
  inventory).
- Use fully-qualified modules: `ansible.builtin.apt`, `ansible.builtin.copy`,
  `ansible.builtin.service`, etc. **Never** qualify a *parameter* key just because it shares a name
  with a module (`group:`, `user:`, `line:` stay bare when they're parameters, not the task's
  module).
- Idempotency: `state: present` / `state: started`, `enabled: true`, `update_cache: true` where
  appropriate.
- Don't create an inventory file inside a playbook. Only add a README with an example
  inventory/run command if the user's spec asks for one.
- File modes are quoted strings: `"0644"`, `"0755"` — never bare octal.
- No comments outside YAML content; no Markdown files besides README.md.
- Quote any value that starts with a Jinja expression: `repo: "{{ some_var }}"`, not
  `repo: {{ some_var }}`.

### Check each tool authoritative documentation

When the spec names specific third-party software to install or configure, always check that software's official
documentation before writing the setup tasks — don't rely on a fixed/remembered recipe.

If the fetch tool is unavailable, times out, is network-blocked, or the page no longer contains install instructions
(e.g. the vendor restructured the docs), don't guess: stop and tell the user, so they can help decide.

Fill out `meta/main.yml` and `defaults/main.yml` as with any role.

#### GitLab exceptions

The role must not contain any task that runs `gitlab-ctl reconfigure`, or that starts/enables
`gitlab-runsvdir.service` — writing such a task counts as doing it, even though it's the role that executes it, not
you. Skip both because they'd bring up a full, resource-heavy GitLab instance (Postgres, Redis, Gitaly, Puma,
Sidekiq...), too demanding for a simple local PoC.

Since nothing here should reconfigure or restart the service, this role should have no handler that does either —
only add a `handlers/main.yml` entry if something else in the role actually needs to notify one.

If the official documentation states:
- additional parameters for lighter environments, stop and ask for revision.
- that those configurations are not used anymore, stop and ask for help.

### External APT repositories (important)

If a task adds a third-party APT repo with `signed-by=...`, you must use the module
`ansible.builtin.deb822_repository` to handle the new repository and its key/signature checking. Always fetch the
GPG key and repo metadata over `https://` — never a plain `http://` source.

Always consider that I'm going to use VMs with Ubuntu 24.04 installed and using the amd64
processor architecture, components are always "stable".

If the vendor documentation gives you the classic shell recipe, for example:

```
mkdir /etc/apt/keyrings
gpg --dearmor
echo "deb [signed-by=...] ..." > /etc/apt/sources.list.d/....list
```

Don't do it directly, translate the details to feed into the module ansible.builtin.deb822_repository module.

## Mode: playbook

- Write a single file `<root>/<name>.yml`.
- The play starts with `- hosts: <host>`.
- Add `become: true` if the spec implies privileged actions (installing packages, managing
  services, etc.) or asks for it explicitly.
- Add `tags: [...]` only if the spec mentions tags.
- If the spec asks for an example inventory, add `<root>/README.md` with a minimal inventory
  (e.g. `[<host>]\nlocalhost ansible_connection=local`) and the run command
  `ansible-playbook -i inventory <name>.yml`.

## Mode: role

Create the full standard layout under `<root>/<name>/`:

```
<name>/
  README.md
  defaults/main.yml
  handlers/main.yml
  meta/main.yml
  meta/.galaxy_install_info   (content: "version: 1.0.0\n")
  tasks/main.yml              (must contain: import_tasks: install.yml)
  tasks/install.yml
  tests/inventory             ([<host>]\nlocalhost ansible_connection=local)
  tests/test.yml              (- hosts: <host>\n  roles:\n    - <name>)
  vars/main.yml
```

Also write a root playbook `<root>/<name>.yml` that applies the role:
`- hosts: <host>\n  roles:\n    - <name>`.

- `meta/main.yml` needs `galaxy_info.role_name`, `author`, `description`, `license`,
  `min_ansible_version`, `platforms`, `galaxy_tags`, and `dependencies: []`.
- Put configurable values in `defaults/main.yml`, referenced elsewhere as `{{ var }}`.
- Handlers must restart/start the relevant service and be `notify`'d by the task that changes its
  config or installs it.

## Self-check before finishing

Before declaring the task done, verify the files you wrote satisfy all of:

1. Every YAML file (except `tests/inventory`) starts with `---`.
2. (role) `tasks/main.yml` contains `import_tasks: install.yml`.
3. (role) every handler name used in a `notify:` is actually defined in `handlers/main.yml`.
4. (role) every `template`/`copy` `src:` referenced exists under `templates/`.
5. (role) every `{{ var }}` used outside `defaults/main.yml` is defined in `defaults/main.yml`
   (ignore `ansible_*` facts).
6. Any `ansible.builtin.systemd` task has both `enabled:` and `state: started`.
7. All file modes are quoted strings.
8. Any external APT repo uses ansible.builtin.deb822_repository to be configured, the keyring must have the corresponding key, the repository never accepts insecure downloads that cannot be validated (by TLS certificate for the URL to download and the public GPG key)

Fix anything that fails before finishing — don't just report it, unless the user only asked for a
check. Then print a short summary of what was written and the run command
(`ansible-playbook <name>.yml` for a playbook, or
`ansible-playbook -i <name>/tests/inventory <name>/tests/test.yml` to test a role), the same way
`iaops-ansible.py` used to.
