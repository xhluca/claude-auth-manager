#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 /path/to/openrouter-key /path/to/gemini-key /path/to/claude-config-dir" >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
openrouter_key="$(realpath "$1")"
google_key="$(realpath "$2")"
claude_config="$(realpath "$3")"
claude_binary="$(readlink -f "$(command -v claude)")"

[[ -f "$openrouter_key" ]] || { echo "OpenRouter key file is missing" >&2; exit 1; }
[[ -f "$google_key" ]] || { echo "Google key file is missing" >&2; exit 1; }
[[ -f "$claude_config/.credentials.json" ]] || {
  echo "Claude OAuth credential is missing" >&2
  exit 1
}

docker_home="$(mktemp -d "${TMPDIR:-/tmp}/cam-docker-home.XXXXXXXX")"
cleanup() {
  rm -rf -- "$docker_home"
}
trap cleanup EXIT

mkdir -p "$docker_home/.claude"
install -m 600 "$claude_config/.credentials.json" "$docker_home/.claude/.credentials.json"
if [[ -f "$HOME/.claude.json" ]]; then
  install -m 600 "$HOME/.claude.json" "$docker_home/.claude.json"
fi

image="claude-auth-manager-live:local"
docker build --quiet --file "$repo_dir/docker/Dockerfile.integration" --tag "$image" "$repo_dir"

docker run --rm --interactive --user "$(id -u):$(id -g)" \
  --hostname cam-docker \
  --env HOME=/root \
  --mount "type=bind,src=$docker_home,dst=/root" \
  --mount "type=bind,src=$openrouter_key,dst=/run/secrets/openrouter,readonly" \
  --mount "type=bind,src=$google_key,dst=/run/secrets/google,readonly" \
  --mount "type=bind,src=$claude_binary,dst=/usr/local/bin/claude,readonly" \
  "$image" bash -s <<'CONTAINER'
set -euo pipefail

mkdir -p /workspace
printf 'CAM_DOCKER_WORKSPACE\n' > /workspace/CLAUDE.md
python - <<'PY'
import json
from pathlib import Path

state = Path('/root/.claude.json')
document = json.loads(state.read_text()) if state.exists() else {}
document['hasCompletedOnboarding'] = True
document.setdefault('projects', {})['/workspace'] = {
    'allowedTools': [],
    'mcpContextUris': [],
    'mcpServers': {},
    'enabledMcpjsonServers': [],
    'disabledMcpjsonServers': [],
    'hasTrustDialogAccepted': True,
    'hasClaudeMdExternalIncludesApproved': False,
    'hasClaudeMdExternalIncludesWarningShown': False,
}
state.write_text(json.dumps(document))
state.chmod(0o600)
settings = Path('/root/.claude/settings.json')
settings.write_text(json.dumps({'theme': 'dark', 'autoUpdates': False}))
settings.chmod(0o600)
PY

cd /workspace
auth_email="$(claude auth status --json | python -c 'import json,sys; print(json.load(sys.stdin)["email"])')"
echo "NATIVE_LOGIN_OK:$auth_email"

baseline="$(claude -p --model fable --tools '' --no-session-persistence \
  'Reply with exactly CAM_NATIVE_BASELINE_OK and nothing else.' </dev/null)"
[[ "$baseline" == *CAM_NATIVE_BASELINE_OK* ]] || {
  echo "native Claude baseline response failed" >&2
  exit 1
}

cam account add --current
account_id="$(cam list --account --json | python -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
cp /root/.claude/.credentials.json /tmp/native-credentials-after-registration.json
chmod 0600 /tmp/native-credentials-after-registration.json
cam key add router --provider openrouter --key-path /run/secrets/openrouter
cam key add google --provider google --key-path /run/secrets/google
cmp /tmp/native-credentials-after-registration.json /root/.claude/.credentials.json
echo PROVIDER_KEYS_DID_NOT_CHANGE_NATIVE_CREDENTIAL

cam index --key router --json > /tmp/openrouter-models.json
cam index --key google --json > /tmp/google-models.json
python - <<'PY'
import json
from pathlib import Path

def select(path, preferred, provider):
    models = json.loads(Path(path).read_text())
    by_id = {model['id']: model for model in models}
    for model_id in preferred:
        if model_id in by_id:
            return model_id
    candidates = [
        model['id'] for model in models
        if provider != 'openrouter'
        or (
            not model['id'].startswith('anthropic/')
            and model['id'] != 'openrouter/auto'
            and 'tools' in model.get('supported_parameters', [])
        )
    ]
    if not candidates:
        raise SystemExit(f'no tool-capable {provider} model is available')
    return candidates[0]

openrouter = select(
    '/tmp/openrouter-models.json',
    ['z-ai/glm-5.3-flash', 'qwen/qwen3-coder', 'google/gemini-3-flash-preview'],
    'openrouter',
)
google = select(
    '/tmp/google-models.json',
    ['gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.5-flash', 'gemini-2.5-flash'],
    'google',
)
Path('/tmp/openrouter-model').write_text(openrouter)
Path('/tmp/google-model').write_text(google)
print(f'OPENROUTER_MODEL_DISCOVERED:{openrouter}')
print(f'GOOGLE_MODEL_DISCOVERED:{google}')
PY

openrouter_model="$(cat /tmp/openrouter-model)"
google_model="$(cat /tmp/google-model)"
subscription_route="cam/anthropic/$account_id/claude-fable-5[1m]"
openrouter_route="cam/openrouter/router/$openrouter_model"
google_route="cam/google/google/$google_model"

cam select \
  "anthropic/$account_id/claude-fable-5" \
  "openrouter/router/$openrouter_model" \
  "google/google/$google_model"

cam check --json > /tmp/doctor.json
python - <<'PY'
import json
from pathlib import Path

doctor = json.loads(Path('/tmp/doctor.json').read_text())
assert doctor['configured'] and doctor['router'] and doctor['native_login'], doctor
assert doctor['accounts'] == 1 and doctor['keys'] == 2, doctor
assert len(doctor['routes']) == 3, doctor
assert all(route['ready'] for route in doctor['routes']), doctor

settings = json.loads(Path('/root/.claude/settings.json').read_text())
rows = settings['modelPicker']['options']
assert len(rows) == 3, rows
assert any(row['label'] == 'Fable 5' and row['model'].startswith('cam/anthropic/') and row['model'].endswith('[1m]') for row in rows), rows
assert any('/router/' in row['model'] and 'OpenRouter' not in row['label'] for row in rows), rows
assert any('/google/' in row['model'] and not row['label'].startswith('Google:') for row in rows), rows
assert all(row['description'].startswith('Claude · ') and row['description'].endswith(' via claude-auth-manager') for row in rows if row['model'].startswith('cam/anthropic/')), rows
assert all(row['model'].split('/', 3)[-1].removesuffix('[1m]') in row['description'] for row in rows if not row['model'].startswith('cam/anthropic/')), rows
assert all('tools' not in row['description'] and 'context' not in row['description'] for row in rows), rows
print('DOCTOR_AND_MODEL_PICKER_OK')
PY

subscription="$(claude -p --model "$subscription_route" --tools '' --no-session-persistence \
  'Reply with exactly CAM_SUBSCRIPTION_OK and nothing else.' </dev/null)"
[[ "$subscription" == *CAM_SUBSCRIPTION_OK* ]] || {
  echo "named Claude subscription route failed" >&2
  exit 1
}
echo NAMED_SUBSCRIPTION_ROUTE_OK

openrouter="$(claude -p --model "$openrouter_route" --tools '' --no-session-persistence \
  'Reply with exactly CAM_OPENROUTER_OK and nothing else.' </dev/null)"
[[ "$openrouter" == *CAM_OPENROUTER_OK* ]] || {
  echo "OpenRouter route failed" >&2
  exit 1
}
echo OPENROUTER_ROUTE_OK

google="$(claude -p --model "$google_route" --tools '' --no-session-persistence \
  'Reply with exactly CAM_GOOGLE_OK and nothing else.' </dev/null)"
[[ "$google" == *CAM_GOOGLE_OK* ]] || {
  echo "Google route failed" >&2
  exit 1
}
echo GOOGLE_ROUTE_OK

cam check "$openrouter_route" --yes
cam check "$google_route" --yes
echo PROVIDER_TOOL_ROUND_TRIPS_OK

claude -p --model "$openrouter_route" --tools '' --no-session-persistence \
  'Reply with exactly CAM_PARALLEL_OPENROUTER_OK and nothing else.' \
  </dev/null > /tmp/parallel-openrouter.txt &
openrouter_pid=$!
claude -p --model "$google_route" --tools '' --no-session-persistence \
  'Reply with exactly CAM_PARALLEL_GOOGLE_OK and nothing else.' \
  </dev/null > /tmp/parallel-google.txt &
google_pid=$!
wait "$openrouter_pid"
wait "$google_pid"
grep -q CAM_PARALLEL_OPENROUTER_OK /tmp/parallel-openrouter.txt
grep -q CAM_PARALLEL_GOOGLE_OK /tmp/parallel-google.txt
echo PARALLEL_PROVIDER_ROUTES_OK

cam list --route --json > /tmp/routes.json
python - <<'PY'
import json
from pathlib import Path

routes = json.loads(Path('/tmp/routes.json').read_text())
assert {route['provider'] for route in routes} == {'anthropic', 'openrouter', 'google'}
serialized = json.dumps(routes)
for secret_path in ('/run/secrets/openrouter', '/run/secrets/google'):
    secret = Path(secret_path).read_text().strip()
    assert secret not in serialized
print('ROUTE_METADATA_CONTAINS_NO_SECRETS')
PY

cam reset
python - <<'PY'
import json
from pathlib import Path

settings = json.loads(Path('/root/.claude/settings.json').read_text())
assert settings == {'theme': 'dark', 'autoUpdates': False}, settings
assert Path('/root/.claude/.credentials.json').exists()
assert not Path('/root/.config/claude-auth-manager').exists()
assert not Path('/root/.cache/claude-auth-manager').exists()
assert not Path('/root/.local/state/claude-auth-manager').exists()
print('RESET_AND_NATIVE_CREDENTIAL_PRESERVATION_OK')
PY

echo CLAUDE_AUTH_MANAGER_LIVE_DOCKER_OK
CONTAINER
