#!/usr/bin/env bash
# Runs the Chatterbox TTS server (github.com/devnen/Chatterbox-TTS-Server) in
# Docker for the ChatterboxTTS Red cog, then tells you the [p]ttsset url to use.
#
#   ./chatterbox-docker.sh up        build and start the server (default)
#   ./chatterbox-docker.sh down      stop it
#   ./chatterbox-docker.sh restart   restart it
#   ./chatterbox-docker.sh logs      follow its logs
#   ./chatterbox-docker.sh status    show whether it's up and answering
#   ./chatterbox-docker.sh update    pull the newest server code and rebuild
#
# Options (or the environment variables in brackets):
#   --gpu nvidia|cpu|cu128|cu130|rocm|rdna4|strixhalo   [CHATTERBOX_GPU]
#        default: nvidia if nvidia-smi works, otherwise cpu.
#        Use cu128 for RTX 50-series cards.
#   --port 8000        host port to publish                [CHATTERBOX_PORT]
#   --dir PATH         where the server code lives         [CHATTERBOX_DIR]
#                      default: ~/chatterbox-tts-server
#   --ref REF          git commit/branch/tag to run        [CHATTERBOX_REF]
#   HF_TOKEN=...       optional Hugging Face token (the model is public, so
#                      you normally don't need one)
#
# Settings chosen on the first `up` are saved in <dir>/.env, so later commands
# (down, logs, ...) reuse them without repeating the options.

set -euo pipefail

REPO_URL="https://github.com/devnen/Chatterbox-TTS-Server.git"
# Known-good upstream commit. `update` (or --ref main) moves to the latest.
PINNED_REF="915ae289340e10c6047f27f47e22eae9bf350c32"
PROJECT="chatterbox-tts"

DIR="${CHATTERBOX_DIR:-$HOME/chatterbox-tts-server}"
GPU="${CHATTERBOX_GPU:-}"
PORT="${CHATTERBOX_PORT:-}"
REF="${CHATTERBOX_REF:-}"
CMD="up"

die()  { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    up|down|restart|logs|status|update) CMD="$1" ;;
    --gpu)  GPU="${2:?--gpu needs a value}"; shift ;;
    --port) PORT="${2:?--port needs a value}"; shift ;;
    --dir)  DIR="${2:?--dir needs a value}"; shift ;;
    --ref)  REF="${2:?--ref needs a value}"; shift ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

command -v git  >/dev/null || die "git isn't installed."
command -v curl >/dev/null || die "curl isn't installed."
command -v docker >/dev/null || die "Docker isn't installed: https://docs.docker.com/engine/install/"
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose isn't installed (the docker-compose-plugin package)."
fi
docker info >/dev/null 2>&1 || die "Can't talk to Docker. Is it running, and is your user in the docker group (or use sudo)?"

compose() { (cd "$DIR" && "${COMPOSE[@]}" -p "$PROJECT" "$@"); }

# Reuse settings saved by an earlier run unless overridden on the command line.
saved() { [[ -f "$DIR/.env" ]] && sed -n "s/^$1=//p" "$DIR/.env" | tail -n1 || true; }
[[ -z "$GPU"  ]] && GPU="$(saved CHATTERBOX_GPU)"
[[ -z "$PORT" ]] && PORT="$(saved PORT)"
PORT="${PORT:-8000}"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "--port must be a number"

lan_ip() {
  local ip
  ip="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
  [[ -n "$ip" ]] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "$ip"
}

wait_for_server() {
  local url="http://127.0.0.1:$PORT/get_predefined_voices" waited=0 limit=1200
  info "Waiting for the server to answer on port $PORT."
  info "The first start downloads the model (a few GB), so this can take a while..."
  until curl -fsS -m 5 "$url" >/dev/null 2>&1; do
    if [[ "$(docker inspect -f '{{.State.Running}}' chatterbox-tts 2>/dev/null)" != "true" ]]; then
      compose logs --tail 40 || true
      die "The container stopped. The log above should say why."
    fi
    (( waited >= limit )) && die "Still not answering after $((limit / 60)) minutes. Check: $0 logs"
    sleep 10; waited=$((waited + 10))
    (( waited % 60 == 0 )) && echo "    ...still starting ($((waited / 60)) min)"
  done
}

print_next_steps() {
  local ip; ip="$(lan_ip)"; ip="${ip:-<this-machine-IP>}"
  cat <<EOF

Chatterbox TTS is running.
  Web UI:  http://$ip:$PORT
  Voice:   copy your voice.wav into $DIR/voices (the cog only uses that file)

Point the cog at it from Discord:
  [p]ttsset url http://$ip:$PORT
  [p]ttsset status

If Red runs on this same machine you can also use http://127.0.0.1:$PORT
(or http://host.docker.internal:$PORT if Red itself is in a Docker container).
EOF
}

checkout_code() {
  local ref="$1"
  if [[ ! -d "$DIR/.git" ]]; then
    [[ -e "$DIR" && -n "$(ls -A "$DIR" 2>/dev/null)" ]] && die "$DIR exists and isn't the Chatterbox repo. Pick another --dir."
    info "Downloading Chatterbox-TTS-Server into $DIR"
    git clone --quiet "$REPO_URL" "$DIR"
  else
    git -C "$DIR" fetch --quiet origin
  fi
  if [[ "$ref" == "main" || "$ref" == "latest" ]]; then
    ref="origin/main"
  fi
  # Keep config.yaml edits made through the web UI across updates.
  local stashed=0
  if ! git -C "$DIR" diff --quiet -- config.yaml; then
    git -C "$DIR" stash push --quiet -- config.yaml && stashed=1
  fi
  git -C "$DIR" checkout --quiet --detach "$ref" || die "Couldn't check out '$ref'."
  if (( stashed )); then
    git -C "$DIR" stash pop --quiet || echo "warning: your config.yaml changes are kept in 'git stash' in $DIR" >&2
  fi
}

write_settings() {
  if [[ -z "$GPU" ]]; then
    if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then GPU=nvidia; else GPU=cpu; fi
    info "No --gpu given; using '$GPU'."
  fi
  local file
  case "$GPU" in
    nvidia) file="docker-compose.yml" ;;
    *)      file="docker-compose-$GPU.yml" ;;
  esac
  [[ -f "$DIR/$file" ]] || die "Unknown --gpu '$GPU' (no $file upstream). Options: nvidia, cpu, cu128, cu130, rocm, rdna4, strixhalo"

  if [[ "$GPU" != "cpu" && "$GPU" != rocm* && "$GPU" != rdna4 && "$GPU" != strixhalo ]] \
     && ! docker info 2>/dev/null | grep -qi nvidia; then
    echo "warning: Docker doesn't list an NVIDIA runtime. If the start fails with a GPU error," >&2
    echo "         install the NVIDIA Container Toolkit, or rerun with --gpu cpu." >&2
  fi

  # Upstream hardcodes HF_TOKEN=YOUR_TOKEN_HERE, which Hugging Face rejects as a
  # bad token. Replace it with yours, or blank (fine for the public model).
  cat > "$DIR/docker-compose.override.yml" <<'EOF'
# Written by chatterbox-docker.sh from the crewcogs repo.
services:
  chatterbox-tts-server:
    container_name: chatterbox-tts
    environment:
      - HF_TOKEN=${HF_TOKEN:-}
EOF
  local token="${HF_TOKEN:-$(saved HF_TOKEN)}"
  umask 077
  cat > "$DIR/.env" <<EOF
# Written by chatterbox-docker.sh. Docker Compose reads this automatically.
CHATTERBOX_GPU=$GPU
PORT=$PORT
COMPOSE_FILE=$file:docker-compose.override.yml
HF_TOKEN=$token
EOF
}

port_in_use() {
  if command -v ss >/dev/null; then ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; else return 1; fi
}

case "$CMD" in
  up|update)
    if [[ "$CMD" == "update" ]]; then
      checkout_code "${REF:-main}"
    elif [[ ! -d "$DIR/.git" ]]; then
      checkout_code "${REF:-$PINNED_REF}"
    elif [[ -n "$REF" ]]; then
      checkout_code "$REF"
    fi
    write_settings
    if port_in_use && ! compose ps --quiet 2>/dev/null | grep -q .; then
      die "Port $PORT is already taken (an old Chatterbox server not in Docker?). Stop it or use --port."
    fi
    info "Building and starting Chatterbox ($GPU, port $PORT). The first build takes a while."
    compose up -d --build
    wait_for_server
    print_next_steps
    ;;
  down)
    [[ -d "$DIR" ]] || die "Nothing at $DIR."
    compose down
    ;;
  restart)
    compose restart
    wait_for_server
    print_next_steps
    ;;
  logs)
    compose logs -f --tail 100
    ;;
  status)
    [[ -d "$DIR" ]] || die "Not installed at $DIR yet. Run: $0 up"
    compose ps
    if curl -fsS -m 5 "http://127.0.0.1:$PORT/get_predefined_voices" >/dev/null 2>&1; then
      echo "Server is answering on port $PORT."
    else
      echo "Server is not answering on port $PORT yet."
    fi
    ;;
esac
