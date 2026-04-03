# Host Overrides

Use this directory for host-specific Ansible overrides that should not live in the shared topology manifest.

Typical examples:

- `ansible/inventory/host_vars/ai2.yml`
- `ansible/inventory/host_vars/ai1.yml`
- `ansible/inventory/host_vars/ada2.yml`

Example `ansible/inventory/host_vars/ai2.yml`:

```yaml
nexus_repo_dir: /Users/ai/nexus
ansible_python_interpreter: /usr/bin/python3
nexus_mlx_pf_allowlist_enabled: true
```

Keep secrets out of this directory unless you are also using Ansible Vault.
