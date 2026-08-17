# samye

samye is a self-hosted daemon that responds to commands in Google Docs comments. It asks a configured language model for replacement text and, by default, stores the result locally as a proposal for review in its web UI. Accepted proposals are written to the document by the bot account and recorded in Google Docs version history.

## Setup

1. Create a Google Cloud project, enable the Google Docs API and Google Drive API, and configure its OAuth consent screen.
2. Create an OAuth client with application type **Desktop app**. Download its JSON file to `~/.config/samye/client_secret.json`.
3. Use a dedicated Google account for the bot. If the OAuth app is in testing, add that account as a test user.
4. Copy `config.example.toml` to `~/.config/samye/config.toml`, choose a provider and model, and set any provider API key named by `api_key_env` in the process environment. An empty `docs` list auto-discovers visible Google Docs; otherwise list document IDs explicitly.
5. Install and authorize samye on a machine with a browser:

   ```console
   uv sync
   uv run samye auth
   ```

   This creates `~/.local/state/samye/token.json` with mode 0600. For a headless server, perform this step on a browser-equipped machine and securely copy or mount that token file on the server.

6. Invite the dedicated bot account to each document as an editor. Run `uv run samye docs` to confirm visibility, then start the daemon with `uv run samye run`.
7. Open <http://127.0.0.1:8321> to review proposals.

The default `propose` mode and the `reply` mode use generally available Google APIs and do not require Developer Preview enrollment.

## Local models

Local models are fully supported and require no cloud model provider. Configure a provider with `type = "openai_compat"`; samye sends chat-completion requests to `{base_url}/v1/chat/completions`, which works with Ollama, llama.cpp server, vLLM, LocalAI, and other OpenAI-compatible servers. The API key is optional for local endpoints.

For example, an Ollama model running directly on the same machine can be configured as:

```toml
default_provider = "local"

[providers.local]
type = "openai_compat"
base_url = "http://127.0.0.1:11434"
model = "gpt-oss:120b"
timeout_s = 120.0
```

When samye runs in Docker and the model server runs on the host, use `http://host.docker.internal:11434` instead. The included Compose file supplies that host-gateway mapping.

## Comment commands

Commands must begin the comment text with `@ai`. For commands that edit or propose text, highlight the target text before creating the comment.

| Comment | Effect |
| --- | --- |
| `@ai <instruction>` | Generate replacement text for the uniquely matched highlighted text. |
| `@ai pin <name>` | Save the highlighted text as `@[name]`. |
| `@ai unpin <name>` | Remove every range saved as `@[name]`; no highlighted text is required. |
| `@ai rewrite using @[name]` | Include the current pinned text as model context. |

Pin names contain 1–32 lowercase letters, numbers, or hyphens.

## Write modes

- `propose` is the default. It stores the replacement locally, leaves the source comment open, and waits for accept or reject in the web UI. Accepting revalidates the target before applying a normal Google Docs edit.
- `reply` makes no document edit. It posts the replacement in a fenced block in the comment thread and resolves the comment.
- `suggest` is reserved for the optional Developer Preview extension described below. The current MVP exits at startup with a clear error if this mode is selected.

## Docker

Set `web_bind_host = "0.0.0.0"` in `~/.config/samye/config.toml` for the container, create `~/.local/state/samye`, and authorize once before starting Docker so the token exists. Then run:

```console
docker compose up --build
```

The compose file mounts the config and OAuth client read-only, mounts token/state storage read-write, and publishes the UI only at `127.0.0.1:8321` on the host. Add provider secret environment variables under the service's `environment` or `env_file` setting without writing their values into TOML.

## Operational limits

- Version 1 reads every tab and document segment to prove a quote is unique, but writes only to the first tab body. Targets in nested or later tabs, headers, footers, or footnotes are refused.
- A mutation is attempted once. If samye loses contact while a write may have landed, it records the result as indeterminate and asks a collaborator to verify the document manually instead of retrying.
- Thread notifications are best-effort. After three failed delivery attempts, samye abandons the reply and retains the outcome only in its local state and logs.
- The web UI has CSRF and Host-header checks but no login. Keep it on loopback unless it is behind an authenticated reverse proxy or VPN; set `web_base_url` only to that protected external URL.
- State is a single atomic JSON file at `~/.local/state/samye/state.json`. Back it up together with the OAuth token when moving hosts.

## Optional: suggest mode

Native Google Docs suggestions require Workspace Developer Preview enrollment and preview-specific API support. They are not used by `propose` or `reply`, and this repository does not enable `suggest` mode until that optional extension is implemented and verified.
