# vault-policies/ocp-workloads.hcl
#
# Vault policy for OCP workloads using the Vault Agent Injector.
# Grants read access to secrets needed by hub services (GitLab, Artifactory, etc.)
# TODO: Scope to specific paths per workload namespace.

path "secret/data/hub/*" {
  capabilities = ["read"]
}

path "secret/data/gitlab/*" {
  capabilities = ["read"]
}

path "secret/data/artifactory/*" {
  capabilities = ["read"]
}

path "secret/data/storage/*" {
  capabilities = ["read"]
}
