#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# balrog domain dependencies (P0-4) -- ONE authoritative installer.
#
# WHY THIS IS A SCRIPT AND NOT A requirements.txt BLOCK
#   1. `git+https://github.com/...` cannot be used here: `git clone` stalls in
#      this environment (objects.githubusercontent.com unreachable) while
#      `codeload.github.com/<owner>/<repo>/tar.gz/<sha>` works. So the two
#      forks are installed from pinned TARBALLS (sha256 checked below).
#   2. `gym==0.23.0` MUST be installed with `--no-deps`: it declares
#      `pygame==2.1.0`, which has no cp312 wheel (the mirror only ships
#      >=2.5.0), so a normal resolve fails. Its real runtime dependency
#      (cloudpickle) is already present.
#   3. `gym==0.23.0` also imports `distutils`, removed from the 3.12 stdlib:
#      `setuptools` (installed first) supplies the shim.
#   4. The Minigrid FORK is required, not the PyPI release: minigrid 2.3.1 on
#      PyPI has neither `BabyAI-MixedTrainLocal-v0` nor `info["descriptions"]`
#      (measured 2026-11-03); the fork's `babyai_text` branch has both.
#
# WHAT IT INSTALLS (venv_nat; python 3.12, numpy 2.5.4 untouched)
#   setuptools, gymnasium==1.2.0, pygame>=2.5, gym==0.23.0 (--no-deps),
#   minigrid@fork cf73dd148e..., and with --only all: baba@40ae1b29... + opencv
#
# USAGE
#   bash domains/balrog/scripts/setup_balrog.sh              # babyai only
#   bash domains/balrog/scripts/setup_balrog.sh --only all   # + babaisai (baba)
#   bash domains/balrog/scripts/setup_balrog.sh --check      # verify only
#   bash domains/balrog/scripts/setup_balrog.sh --uninstall  # roll back
#
# ENV OVERRIDES
#   BALROG_PYTHON=...            interpreter to install into (default venv_nat)
#   BALROG_WORKDIR=...           scratch dir (default: mktemp -d)
#   BALROG_MINGRID_TARBALL=...   use a local tarball instead of downloading
#   BALROG_BABA_TARBALL=...      same for baba-is-ai
#   BALROG_ALLOW_SHA_DRIFT=1     accept a tarball whose sha256 differs (GitHub
#                                occasionally regenerates archives) -- warns
#   BALROG_FORCE=1               reinstall even when the state file matches
# ---------------------------------------------------------------------------
set -u -o pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
PY="${BALROG_PYTHON:-$REPO/venv_nat/bin/python}"

# --- pins (change here, nowhere else) --------------------------------------
MINIGRID_SHA="cf73dd148e51276bd675a37e3bfb0bf2b42329b2"   # branch babyai_text
MINIGRID_TARBALL_SHA256="b03f922fe8e875f4081082756c212e77cb392a59c917e6a5f1eda0d5f07680e9"
BABA_SHA="40ae1b29dbc67f2ecea06cc161ed5ca51719e5e3"       # branch main
BABA_TARBALL_SHA256="a674b5c14925e2701b78fe0f1d93445019ceadd120ab6dfce2415e9019d4d613"
GYM_VERSION="0.23.0"
GYMNASIUM_VERSION="1.2.0"
PYGAME_SPEC=">=2.5"
OPENCV_SPEC="opencv-python"

SCOPE="babyai"
MODE="install"
log()  { echo "[balrog-deps] $*"; }
die()  { echo "[balrog-deps][ERR] $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --only) [ $# -ge 2 ] || die "--only needs a value (babyai|all)"; SCOPE="$2"; shift 2 ;;
    --only=*) SCOPE="${1#--only=}"; shift ;;
    --check) MODE="check"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
case "$SCOPE" in babyai|all) ;; *) die "--only must be babyai or all (got $SCOPE)" ;; esac

[ -x "$PY" ] || die "interpreter not found: $PY (create venv_nat first)"
PY_VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
[ "$PY_VER" = "3.12" ] || log "WARNING: expected python 3.12, got $PY_VER (pins were verified on 3.12)"
PREFIX="$("$PY" -c 'import sys; print(sys.prefix)')"
STATE_FILE="$PREFIX/.balrog_deps.json"
WORK="${BALROG_WORKDIR:-}"

# --- verification (the acceptance test; also used for idempotency) ---------
check_babyai() {
  log "check: babyai surface (minigrid fork + gymnasium + old gym)"
  "$PY" - <<'PY'
import warnings; warnings.filterwarnings("ignore")
import gym, gymnasium, minigrid, pygame          # noqa: F401  (old gym: Strings space)
minigrid.register_minigrid_envs()
import gymnasium as g
env = g.make("BabyAI-MixedTrainLocal-v0")
obs, info = env.reset(seed=0)
assert "descriptions" in info, f"info keys = {sorted(info.keys())}"
assert hasattr(env.unwrapped, "get_pov_render"), "get_pov_render missing"
shape = env.unwrapped.get_pov_render(tile_size=16).shape
assert shape == (112, 112, 3), f"get_pov_render shape {shape}"
# max_steps is only meaningful AFTER reset and is 64 or 128 depending on the
# sub-task Level_MixedTrainLocal randomly sampled for this episode (goto /
# pickup / open -> 64, putnext / pick-up-seq-go-to -> 128). Assert the floor,
# print the value: the wall budget in domains.yaml is derived from the 128 case.
max_steps = env.unwrapped.max_steps
assert isinstance(max_steps, int) and max_steps >= 64, f"max_steps={max_steps!r}"
env.close()
print("babyai OK: gym", gym.__version__, "| gymnasium", gymnasium.__version__,
      "| minigrid", getattr(minigrid, "__version__", "?"), "| pygame", pygame.version.ver,
      "| max_steps(this episode)", max_steps)
PY
}

check_babaisai() {
  log "check: babaisai surface (baba-is-ai)"
  "$PY" - <<'PY'
import warnings; warnings.filterwarnings("ignore")
import gym  # noqa: F401  (baba builds on the old gym API)
import baba
from baba import make
env = make("env/goto_win", add_ruleset=True)
print("babaisai OK: baba", getattr(baba, "__version__", "?"), "| gym", gym.__version__)
PY
}

run_check() {
  local rc=0
  check_babyai || rc=1
  if [ "$SCOPE" = "all" ]; then
    check_babaisai || rc=1
  fi
  return $rc
}

# --- tarball fetch ---------------------------------------------------------
fetch_tarball() {  # url expected_sha dest label
  local url="$1" expected="$2" dest="$3" label="$4"
  if [ ! -s "$dest" ]; then
    log "download $label <- $url"
    curl -sSL --fail -m 300 -o "$dest" "$url" || die "download failed: $url"
  else
    log "reuse $dest"
  fi
  local actual
  actual="$(sha256sum "$dest" | cut -d' ' -f1)"
  if [ "$actual" != "$expected" ]; then
    if [ "${BALROG_ALLOW_SHA_DRIFT:-0}" = "1" ]; then
      log "WARNING: $label sha256 drift (expected $expected, got $actual) -- accepted by BALROG_ALLOW_SHA_DRIFT=1"
    else
      die "$label sha256 mismatch: expected $expected, got $actual (set BALROG_ALLOW_SHA_DRIFT=1 only if you verified the source)"
    fi
  else
    log "$label sha256 OK ($actual)"
  fi
}

write_state() {
  "$PY" - "$STATE_FILE" "$SCOPE" "$MINIGRID_SHA" "$BABA_SHA" "$GYM_VERSION" "$GYMNASIUM_VERSION" <<'PY'
import datetime, json, sys
path, scope, mg, baba, gym, gymnasium = sys.argv[1:7]
data = {"schema": 1, "scope": scope, "minigrid_sha": mg, "baba_sha": baba,
        "gym": gym, "gymnasium": gymnasium,
        "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
print(f"[balrog-deps] state written: {path}")
PY
}

state_matches() {
  [ -f "$STATE_FILE" ] || return 1
  "$PY" - "$STATE_FILE" "$SCOPE" "$MINIGRID_SHA" "$BABA_SHA" "$GYM_VERSION" "$GYMNASIUM_VERSION" <<'PY'
import json, sys
path, scope, mg, baba, gym, gymnasium = sys.argv[1:7]
try:
    d = json.load(open(path, encoding="utf-8"))
except Exception:
    raise SystemExit(1)
ok = (d.get("minigrid_sha") == mg and d.get("gym") == gym
      and d.get("gymnasium") == gymnasium
      and (scope != "all" or d.get("baba_sha") == baba))
raise SystemExit(0 if ok else 1)
PY
}

# --- install ---------------------------------------------------------------
pip_install() {  # args...
  log "pip install $*"
  "$PY" -m pip install "$@" || die "pip install failed: $*"
}

install_deps() {
  log "interpreter: $PY (python $PY_VER, prefix $PREFIX)"
  # 1. setuptools FIRST: gym 0.23 imports `distutils`, absent from py3.12.
  pip_install "setuptools"
  # 2. gymnasium + pygame before the fork, so the fork resolves against them.
  pip_install "gymnasium==$GYMNASIUM_VERSION" "pygame$PYGAME_SPEC" "gym-notices"
  # 3. old gym without its unbuildable pygame==2.1.0 pin (see header).
  pip_install --no-deps "gym==$GYM_VERSION"
  # 4. the Minigrid fork (deps already satisfied).
  fetch_tarball "https://codeload.github.com/BartekCupial/Minigrid/tar.gz/$MINIGRID_SHA" \
                "$MINIGRID_TARBALL_SHA256" "$WORK/minigrid-$MINIGRID_SHA.tar.gz" "minigrid fork"
  pip_install --no-deps "$WORK/minigrid-$MINIGRID_SHA.tar.gz"
  if [ "$SCOPE" = "all" ]; then
    # baba needs opencv + typing_extensions (matplotlib/pygame/gym/numpy already there)
    pip_install "$OPENCV_SPEC" typing_extensions
    fetch_tarball "https://codeload.github.com/nacloos/baba-is-ai/tar.gz/$BABA_SHA" \
                  "$BABA_TARBALL_SHA256" "$WORK/baba-$BABA_SHA.tar.gz" "baba-is-ai"
    pip_install --no-deps "$WORK/baba-$BABA_SHA.tar.gz"
  fi
  write_state
  log "pip check:"
  "$PY" -m pip check || true
}

uninstall_deps() {
  log "uninstalling balrog deps from $PY"
  "$PY" -m pip uninstall -y baba "$OPENCV_SPEC" minigrid gymnasium gym pygame setuptools < /dev/null || true
  rm -f "$STATE_FILE"
  log "done (numpy/pandas/matplotlib/pillow were never touched)"
}

main() {
  case "$MODE" in
    check)
      if run_check; then log "ALL CHECKS PASSED (scope=$SCOPE)"; exit 0; fi
      log "CHECK FAILED (scope=$SCOPE) -- run without --check to install"
      exit 1 ;;
    uninstall) uninstall_deps; exit 0 ;;
  esac

  if [ "${BALROG_FORCE:-0}" != "1" ] && state_matches && run_check; then
    log "already installed and verified (state: $STATE_FILE) -- nothing to do (BALROG_FORCE=1 to reinstall)"
    exit 0
  fi
  if [ -z "$WORK" ]; then
    WORK="$(mktemp -d -t balrog-deps-XXXXXX)" || die "mktemp failed"
    trap 'rm -rf "$WORK"' EXIT
  fi
  install_deps
  if run_check; then
    log "ALL CHECKS PASSED (scope=$SCOPE)"
  else
    die "install finished but verification failed -- see the check output above"
  fi
}

main
