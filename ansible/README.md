# Ansible Scaffold

This directory adds a versioned Ansible control layer on top of the existing Nexus deploy scripts.

Design boundaries:

- `deploy/topology/production.json` remains the desired-state source of truth for host placement.
- Ansible uses that topology to build inventory and orchestrate deploys.
- Existing `deploy/scripts/*.sh` remain the execution layer for env rendering, preflight, compose deploys, and verification.
- etcd remains the live runtime registry populated by service registrars after deployment.

## Layout

- `ansible.cfg`: local Ansible defaults
- `inventory/topology_inventory.py`: dynamic inventory generated from `deploy/topology/production.json`
- `inventory/group_vars/all.yml`: common Nexus deployment defaults
- `inventory/group_vars/platform_*.yml`: per-hosttype defaults such as repo layout
- `inventory/host_vars/README.md`: host-specific override guidance
- `playbooks/bootstrap.yml`: bootstrap Python, install host prerequisites, prepare Docker, and verify repo presence
- `playbooks/deploy.yml`: render topology env, run preflight, deploy, and verify
- `playbooks/site.yml`: import bootstrap then deploy
- `roles/*`: small wrapper roles around existing Nexus scripts and host bootstrap tasks

## Usage

The examples below assume you run them from the repo root and point Ansible at `ansible/ansible.cfg`.

Inspect inventory derived from topology:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-inventory -i ansible/inventory/topology_inventory.py --graph
```

Bootstrap all topology hosts:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/topology_inventory.py ansible/playbooks/bootstrap.yml
```

Deploy a single host profile:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/topology_inventory.py ansible/playbooks/deploy.yml -l ai2
```

Run the full topology serially:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory/topology_inventory.py ansible/playbooks/site.yml
```

## Important variables

Defaults live in `inventory/group_vars/all.yml`.

Common overrides:

- `nexus_branch`
- `nexus_environment`
- `nexus_repo_dir`
- `nexus_workspace_root`
- `nexus_repo_url`
- `nexus_manage_checkout`
- `nexus_manage_host_prereqs`
- `nexus_manage_docker_runtime`
- `nexus_colima_launchd_enabled`
- `nexus_mlx_pf_allowlist_enabled`
- `nexus_verify_gateway`
- `nexus_extra_deploy_args`

Use `inventory/host_vars/<host>.yml` for host-specific overrides such as a different repo path on `ai2`.
Use `inventory/group_vars/platform_macos.yml` and `inventory/group_vars/platform_linux.yml` for per-hosttype defaults.

## Notes

- This scaffold now covers the main non-interactive host bootstrap path. `deploy/scripts/install-host-deps.sh` remains available as a manual fallback for one-off host prep.
- The deploy role delegates to `deploy/scripts/deploy.sh --topology-host ...` so there is still one deploy implementation path.
- Inventory exposes `platform_macos` and `platform_linux` groups from the topology manifest so repo layout and other hosttype defaults can live in group vars.
- The bootstrap playbook now covers the main non-interactive host setup path: Python bootstrap, common packages, Linux Docker engine setup, macOS Colima setup, and optional MLX pf allowlisting.
- Optional GPU-specific Linux runtime setup such as NVIDIA Container Toolkit is not yet modeled as an Ansible role.
- Homebrew itself is not auto-installed. On macOS hosts, install Homebrew first or override the Docker/bootstrap strategy in `host_vars`.
