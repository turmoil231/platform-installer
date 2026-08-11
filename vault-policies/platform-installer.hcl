# vault-policies/platform-installer.hcl
#
# Vault policy for the platform-installer AppRole.
# Grants read access to all secrets the installer needs to deploy the platform.
# TODO: Tighten paths to exactly what each phase needs.

path "secret/data/*" {
  capabilities = ["read", "list"]
}

path "secret/metadata/*" {
  capabilities = ["read", "list"]
}

path "pki/*" {
  capabilities = ["read", "list", "create", "update"]
}

path "auth/approle/*" {
  capabilities = ["read"]
}
