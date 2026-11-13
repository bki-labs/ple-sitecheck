# Source me: export Codespaces secrets (base64 in .env-secrets) into this shell without printing them.
while IFS='=' read -r k v; do
  case "$k" in HF_TOKEN|MODEL_API_KEY|GITHUB_TOKEN) x=$(printf '%s' "$v" | base64 -d 2>/dev/null || printf '%s' "$v"); [ "$k" = GITHUB_TOKEN ] || x=$(printf '%s' "$x" | tr -cd 'A-Za-z0-9_-'); export "$k=$x";; esac
done < /workspaces/.codespaces/shared/.env-secrets
