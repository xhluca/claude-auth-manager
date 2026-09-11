# Project conventions

- Keep releases in `0.0.x`, incrementing only the last component until instructed otherwise.
- Credentials belong only in CAM's private registry/key store, never in source, logs, examples, or commits.
- Discover saved credentials with `cam list --key`; read a key programmatically through `registry.read_key`, never print it. Hugging Face keys use provider `huggingface` (the standard example nickname is `hf`).
- Credential files are under `$XDG_CONFIG_HOME/claude-auth-manager/keys/PROVIDER/NICKNAME`, defaulting to `~/.config/claude-auth-manager/keys/PROVIDER/NICKNAME`. Do not copy secret values into memory notes.
- Use synthetic account identities in tests and documentation. Do not publish personal account configuration.
- Hugging Face inference is free-only: pin a catalog-advertised zero-price provider and recheck before inference; never silently fall back to a paid route.
- Native account switching must not overwrite credentials underneath running native Claude sessions.
