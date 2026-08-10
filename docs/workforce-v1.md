# Workforce v1

Workforce v1 adds one public coordinator, three public domain teams, two deterministic workflows, private
specialists, a private quality council, operation-level capabilities, candidate-level learning promotion, and a
standalone Buzz adapter.

## Public Surface

- Team: `workforce-router`, `engineering-team`, `growth-team`, `research-team`
- Workflow: `engineering-delivery`, `research-pipeline`
- Existing public agents remain `chief`, `agent-builder`, and `platform-manager`
- Specialists and `quality-council` are intentionally absent from `AgentOS(agents=..., teams=...)`
- Every public team returns the same `WorkforceOutcome` schema. Clients inspect its `status` instead of inferring
  success from the outer HTTP or AgentOS run status.

Python tests may import specialists directly. API clients must enter through a public team or workflow.

## Prompt Quality Gate

Prompt text is not invented ad hoc. Every Workforce component ID maps to pinned open-source evidence in
`agents/workforce/prompt_provenance.py`. Construction fails without that mapping. The exact research policy and
source list are documented in `docs/workforce-prompt-provenance.md`; deterministic tests cover provenance and
permission language, while `python -m evals --tag workforce` covers routing, engineering independence, and research
verification behavior. The eval tag uses model calls and should be run deliberately.

## Workspace Executor

The executor is a separate MCP service and is the only container with a Docker socket. AgentOS never receives that
socket. It accepts repository IDs, workspace IDs, relative paths, unified patches, and fixed check IDs only. It does
not accept command strings.

```bash
export WORKSPACE_EXECUTOR_TOKEN="$(openssl rand -hex 32)"
export WORKFORCE_WORKSPACE_ROOT="$PWD/tmp/workforce-workspaces"
docker compose --profile workforce-executor up -d --build workspace-executor
```

Set `WORKSPACE_EXECUTOR_MCP_URL=http://workspace-executor:8100/mcp` and the same token for `agentos-api`. Repository
profiles live outside model control. Each check is an argv array executed with `shell=False` in a disposable,
non-root container with no network, no added capabilities, a read-only root filesystem, and resource limits.
Every repository profile must pin a sandbox image; configuration without one is rejected instead of running
repository code in the Docker-socket controller.

The bundled local catalog registers both `agentos-railway` and `device-farm`. Compose mounts both local source
repositories read-write, while the executor enforces their different policies: `agentos-railway` is trusted and
Device Farm from `${DEVICE_FARM_SOURCE_ROOT:-../device-farm}` requires explicit patch approval. For a named internal
project, the router lists this catalog
first, delegates a match to Engineering with the exact `repo_id`, and requires repository file or code evidence. Web
Research is reserved for external facts or an explicit research request. This rule is derived from the pinned
SWE-agent, OpenHands, and Agno routing sources in `agents/workforce/prompt_provenance.py`.

Checks must be self-contained because the sandbox cannot reach Postgres, cloud APIs, package registries, or other
Compose services. Use CI/integration gates separately when a test legitimately needs those services.

## Buzz Adapter

Buzz sends OpenAI chat-completions requests to the adapter's virtual `buzz-agent` model. The adapter authenticates a
different bearer per Buzz user, maps it to `sub=buzz:<user-id>`, and signs a short-lived AgentOS JWT containing only
`teams:workforce-router:run`. AgentOS verifies the public key with per-user isolation enabled.

```bash
export BUZZ_SECRETS_DIR="${HOME}/.config/agentos-workforce"
mkdir -p "$BUZZ_SECRETS_DIR"
chmod 700 "$BUZZ_SECRETS_DIR"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$BUZZ_SECRETS_DIR/buzz-jwt-private.pem"
openssl pkey -in "$BUZZ_SECRETS_DIR/buzz-jwt-private.pem" -pubout -out "$BUZZ_SECRETS_DIR/buzz-jwt-public.pem"
chmod 600 "$BUZZ_SECRETS_DIR/buzz-jwt-private.pem"
export BUZZ_TOKEN_PEPPER="$(openssl rand -hex 32)"
BUZZ_TOKEN_PEPPER="$BUZZ_TOKEN_PEPPER" python -m services.buzz_adapter.provision --user-id <buzz-user-id>
```

Put only the emitted hash record in `$BUZZ_SECRETS_DIR/buzz-identities.json`; deliver the one-time bearer to that user
through a secret channel. Configure the adapter paths to files in that external directory. AgentOS accepts the public
key either as `BUZZ_JWT_VERIFICATION_KEY` PEM content (recommended for Railway) or through
`BUZZ_JWT_VERIFICATION_KEY_FILE`; the private key must never be mounted into AgentOS. Start with
`docker compose --profile buzz up -d --build buzz-adapter`. Buzz uses `/v1/models` and `/v1/chat/completions` on port
8200. Do not reuse one Buzz bearer for multiple people. `.dockerignore` excludes the conventional in-repo Buzz secret
names as defense in depth, but the supported setup keeps them outside the repository entirely.

Configure the managed Buzz agent with the virtual model and queue each human message as its own turn:

```text
model=buzz-agent
OPENAI_COMPAT_API=chat
OPENAI_COMPAT_BASE_URL=https://<adapter-origin>/v1
OPENAI_COMPAT_API_KEY=<per-user-buzz-bearer>
BUZZ_ACP_SUBSCRIBE=mentions
BUZZ_ACP_MULTIPLE_EVENT_HANDLING=queue
```

Do not use `BUZZ_ACP_SUBSCRIBE=all` for this adapter. Buzz emits empty control events in addition to chat messages;
the adapter ignores those events, but mention-only subscription also prevents them from consuming queue capacity.
The adapter extracts only the current Buzz event, derives the AgentOS session from the authenticated subject and
channel UUID, and returns a fixed `buzz-dev-mcp__shell` tool call that publishes the result with
`buzz messages send`. The model output is shell-quoted and the channel/event identifiers are validated before the
command is constructed; the model never supplies an arbitrary command. Keep `BUZZ_TOKEN_PEPPER` in the deployment
secret store so rebuilding the adapter does not invalidate authentication.

Repository reads run without confirmation. `open_repository(repo_id)` returns the opaque `workspace_id` required by
all later file, Git, check, and patch operations; a repository id is never a workspace id. Write permission is declared
per repository in `WORKFORCE_REPOS_FILE`. The local `agentos-railway` profile is `trusted`, so
`apply_trusted_patch` can update its disposable workspace without prompting. Code/Fixer cannot run checks, Tester
cannot patch, and only Reviewer can bind an exact `VERDICT: PASS` to the current diff. After every named check and the
review grant match the same diff digest, `publish_changes` applies it back only if the source repository still
matches the snapshot opened for the task. Other profiles default to
`approval_required`: `apply_patch` pauses, shows the patch preview, and waits in the same Buzz channel. Reply exactly
`đồng ý cập nhật` to approve that exact diff or `từ chối cập nhật` to reject it. After checks and independent review,
the approved diff can be published to that registered local source. Buzz signs a short-lived, one-time approval token
bound to the authenticated user, session, workspace id, and SHA-256 of the exact patch; the executor verifies it with
the Buzz public key before applying the patch. A patch larger than the 8,000-character approval preview is rejected and
must be split, so approval never covers content the user could not inspect. Direct MCP calls with only the executor
bearer cannot mint this proof. No profile grants production writes.

Set `JWT_AUDIENCE` on AgentOS and the identical `BUZZ_JWT_AUDIENCE` on the adapter. AgentOS rejects Buzz signer
configuration without an audience and rejects tokens minted for another audience. When the same AgentOS accepts
another JWT issuer, that issuer must use the same audience or run behind a separately configured authorization
boundary.

## Deployment Gates

`deployment-check` verifies Workforce imports and consumes the optional `WORKFORCE_MODEL_PROBE_REPORT`. Generate the
report with `python -m workforce.model_probe --output <path>`. The probe makes model calls to test reachability,
structured output, tool calling, streaming, context metadata, and timeouts; it never silently substitutes another
model. Reports are bound to the configured model endpoint and expected context requirement, expire after
`WORKFORCE_MODEL_PROBE_MAX_AGE_HOURS` (24 by default), and fail when future-dated. A missing report is a local-dev
warning and a production deployment-check failure. Run the probe only when model-call spend is approved.
