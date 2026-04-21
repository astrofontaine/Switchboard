#!/usr/bin/env bash
set -euo pipefail

# Remote data collector using Bash + native ssh.
# Usage: ./collector.sh --config collector_config.json

MUX_CONTROL_DIR=""
MUX_CONTROL_PATH=""
MUX_SSH_TARGET=""
declare -a MUX_SSH_OPTS=()
SUDO_PASSWORD=""
ASKPASS_SCRIPT=""

usage() {
  echo "Usage: $0 [--config <path>]"
}

log() {
  printf '%s\n' "$1"
}

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

ensure_askpass_script() {
  if [[ -n "${ASKPASS_SCRIPT:-}" && -f "$ASKPASS_SCRIPT" ]]; then
    return
  fi

  ASKPASS_SCRIPT="$(mktemp)"
  chmod 700 "$ASKPASS_SCRIPT"
  cat > "$ASKPASS_SCRIPT" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${COLLECTOR_ASKPASS_PASSWORD:-}"
EOF
}

cleanup_mux() {
  if [[ -n "${MUX_CONTROL_PATH:-}" && -n "${MUX_SSH_TARGET:-}" ]]; then
    ssh -S "$MUX_CONTROL_PATH" -O exit "${MUX_SSH_OPTS[@]}" "$MUX_SSH_TARGET" >/dev/null 2>&1 || true
  fi
  if [[ -n "${MUX_CONTROL_DIR:-}" ]]; then
    rm -rf "$MUX_CONTROL_DIR"
  fi
  if [[ -n "${ASKPASS_SCRIPT:-}" ]]; then
    rm -f "$ASKPASS_SCRIPT"
  fi
}

trap cleanup_mux EXIT

expand_path() {
  local p="$1"
  case "$p" in
    "~") printf '%s\n' "$HOME" ;;
    "~/"*) printf '%s/%s\n' "$HOME" "${p#~/}" ;;
    *) printf '%s\n' "$p" ;;
  esac
}

json_get_section_string() {
  local file="$1"
  local section="$2"
  local key="$3"

  awk -v section="$section" -v key="$key" '
    BEGIN { in_section = 0 }
    $0 ~ "\"" section "\"[[:space:]]*:[[:space:]]*\\{" { in_section = 1 }
    in_section && $0 ~ /^[[:space:]]*\}[[:space:]]*,?[[:space:]]*$/ { in_section = 0 }
    in_section {
      pattern = "\\\"" key "\\\"[[:space:]]*:[[:space:]]*\\\"([^\\\"]*)\\\""
      if (match($0, pattern, m)) {
        print m[1]
        exit
      }
    }
  ' "$file"
}

json_get_section_number() {
  local file="$1"
  local section="$2"
  local key="$3"

  awk -v section="$section" -v key="$key" '
    BEGIN { in_section = 0 }
    $0 ~ "\"" section "\"[[:space:]]*:[[:space:]]*\\{" { in_section = 1 }
    in_section && $0 ~ /^[[:space:]]*\}[[:space:]]*,?[[:space:]]*$/ { in_section = 0 }
    in_section {
      pattern = "\\\"" key "\\\"[[:space:]]*:[[:space:]]*([0-9]+)"
      if (match($0, pattern, m)) {
        print m[1]
        exit
      }
    }
  ' "$file"
}

json_get_commands() {
  local file="$1"

  awk '
    BEGIN { in_commands = 0 }
    /"commands"[[:space:]]*:[[:space:]]*\[/ { in_commands = 1; next }
    in_commands && /\]/ { in_commands = 0; exit }
    in_commands {
      line = $0
      while (match(line, /"([^"\\]|\\.)*"/)) {
        token = substr(line, RSTART + 1, RLENGTH - 2)
        gsub(/\\"/, "\"", token)
        print token
        line = substr(line, RSTART + RLENGTH)
      }
    }
  ' "$file"
}

resolve_output_file() {
  local out_dir="$1"
  local base_name="$2"
  local host="$3"
  local timestamp stem suffix safe_host

  timestamp="$(date +"%Y%m%d_%H%M")"
  safe_host="$(printf '%s' "$host" | sed 's/[^A-Za-z0-9._-]/_/g' | sed 's/^[._-]*//; s/[._-]*$//')"
  [[ -n "$safe_host" ]] || safe_host="unknown_host"

  if [[ "$base_name" == *.* ]]; then
    stem="${base_name%.*}"
    suffix=".${base_name##*.}"
  else
    stem="$base_name"
    suffix=".txt"
  fi

  if [[ -z "$stem" ]]; then
    stem="collector_output"
  fi

  printf '%s/%s_%s_%s%s\n' "$out_dir" "$stem" "$safe_host" "$timestamp" "$suffix"
}

run() {
  local config_path="collector_config.json"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config)
        [[ $# -lt 2 ]] && fail "--config requires a value"
        config_path="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        fail "Unknown argument: $1"
        ;;
    esac
  done

  [[ -f "$config_path" ]] || fail "Config file not found: $config_path"

  local access_method host port username password timeout_seconds prompt
  local output_path default_filename expanded_output_path

  access_method="$(json_get_section_string "$config_path" "connection" "access_method")"
  host="$(json_get_section_string "$config_path" "connection" "host")"
  port="$(json_get_section_number "$config_path" "connection" "port")"
  username="$(json_get_section_string "$config_path" "connection" "username")"
  password="$(json_get_section_string "$config_path" "connection" "password")"
  timeout_seconds="$(json_get_section_number "$config_path" "connection" "timeout_seconds")"
  prompt="$(json_get_section_string "$config_path" "connection" "prompt")"
  output_path="$(json_get_section_string "$config_path" "output" "path")"
  default_filename="$(json_get_section_string "$config_path" "output" "default_filename")"

  [[ -n "$access_method" ]] || fail "Missing connection.access_method"
  [[ -n "$host" ]] || fail "Missing connection.host"
  [[ -n "$username" ]] || fail "Missing connection.username"
  [[ -n "$output_path" ]] || output_path="."
  [[ -n "$default_filename" ]] || default_filename="collector_output.txt"
  [[ -n "$port" ]] || port=22
  [[ -n "$timeout_seconds" ]] || timeout_seconds=20
  [[ -n "$prompt" ]] || prompt='$'

  if [[ "$access_method" != "ssh" ]]; then
    fail "Bash collector supports only ssh access_method. Found: $access_method"
  fi

  mapfile -t commands < <(json_get_commands "$config_path")
  [[ ${#commands[@]} -gt 0 ]] || fail "commands array must contain at least one command"

  expanded_output_path="$(expand_path "$output_path")"
  mkdir -p "$expanded_output_path" || fail "Failed to create output path: $expanded_output_path"

  local output_file
  output_file="$(resolve_output_file "$expanded_output_path" "$default_filename" "$host")"

  log "[INFO] Loaded config: $config_path"
  log "[INFO] Output file:   $output_file"

  {
    printf '==============================================================================\n'
    printf 'SESSION START\n'
    printf '==============================================================================\n'
    printf 'timestamp: %s\n' "$(date -Iseconds)"
    printf 'method: ssh\n'
    printf 'target: %s:%s\n' "$host" "$port"
    printf 'username: %s\n' "$username"
    printf '\n'
  } >> "$output_file"

  local ssh_target ssh_opts rc
  ssh_target="${username}@${host}"
  ssh_opts=(
    -p "$port"
    -o ConnectTimeout="$timeout_seconds"
    -o StrictHostKeyChecking=accept-new
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=2
  )

  # Use a single multiplexed SSH master connection so password is entered once.
  local control_dir control_path
  control_dir="$(mktemp -d)"
  control_path="${control_dir}/collector_mux_%h_%p_%r"
  MUX_CONTROL_DIR="$control_dir"
  MUX_CONTROL_PATH="$control_path"
  MUX_SSH_TARGET="$ssh_target"
  MUX_SSH_OPTS=("${ssh_opts[@]}")

  log "[INFO] Opening SSH session (you may be prompted for password once)..."
  set +e
  ssh -fN \
    "${ssh_opts[@]}" \
    -o BatchMode=yes \
    -o ControlMaster=yes \
    -o ControlPath="$control_path" \
    -o ControlPersist=600 \
    "$ssh_target" >/dev/null 2>&1
  rc=$?
  set -e

  if [[ $rc -ne 0 && -n "$password" && "$password" != "change_me" ]]; then
    ensure_askpass_script
    set +e
    COLLECTOR_ASKPASS_PASSWORD="$password" \
    DISPLAY=dummy \
    SSH_ASKPASS="$ASKPASS_SCRIPT" \
    SSH_ASKPASS_REQUIRE=force \
    setsid ssh -fN \
      "${ssh_opts[@]}" \
      -o PreferredAuthentications=password \
      -o PubkeyAuthentication=no \
      -o NumberOfPasswordPrompts=1 \
      -o ControlMaster=yes \
      -o ControlPath="$control_path" \
      -o ControlPersist=600 \
      "$ssh_target" </dev/null >/dev/null 2>&1
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
      SUDO_PASSWORD="$password"
    fi
  fi

  if [[ $rc -ne 0 ]]; then
    local entered_password
    read -r -s -p "[INFO] SSH password required for ${username}@${host}: " entered_password
    echo
    if [[ -z "$entered_password" ]]; then
      fail "SSH authentication failed and no password was entered."
    fi

    ensure_askpass_script
    set +e
    COLLECTOR_ASKPASS_PASSWORD="$entered_password" \
    DISPLAY=dummy \
    SSH_ASKPASS="$ASKPASS_SCRIPT" \
    SSH_ASKPASS_REQUIRE=force \
    setsid ssh -fN \
      "${ssh_opts[@]}" \
      -o PreferredAuthentications=password \
      -o PubkeyAuthentication=no \
      -o NumberOfPasswordPrompts=1 \
      -o ControlMaster=yes \
      -o ControlPath="$control_path" \
      -o ControlPersist=600 \
      "$ssh_target" </dev/null >/dev/null 2>&1
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
      fail "SSH authentication failed. Check credentials in config or entered password."
    fi
    SUDO_PASSWORD="$entered_password"
  fi

  ssh -O check \
    "${ssh_opts[@]}" \
    -o ControlPath="$control_path" \
    "$ssh_target" >/dev/null

  local cmd
  for cmd in "${commands[@]}"; do
    printf '\n[CMD] %s\n' "$cmd"
    {
      printf '==============================================================================\n'
      printf 'COMMAND: %s\n' "$cmd"
      printf '==============================================================================\n'
    } >> "$output_file"

    local run_with_sudo=0 base_cmd tmp_output
    tmp_output="$(mktemp)"

    if [[ "$cmd" =~ ^[[:space:]]*sudo[[:space:]]+(.+) ]]; then
      run_with_sudo=1
      base_cmd="${BASH_REMATCH[1]}"
    else
      base_cmd="$cmd"
    fi

    # Prime sudo password from config if available; do not prompt yet.
    if [[ $run_with_sudo -eq 1 && -z "$SUDO_PASSWORD" ]]; then
      if [[ -n "$password" && "$password" != "change_me" ]]; then
        SUDO_PASSWORD="$password"
      fi
    fi

    if [[ $run_with_sudo -eq 1 ]]; then
      local quoted_base_cmd
      printf -v quoted_base_cmd '%q' "$base_cmd"
      if [[ -n "$SUDO_PASSWORD" ]]; then
        set +e
        printf '%s\n' "$SUDO_PASSWORD" | ssh \
          "${ssh_opts[@]}" \
          -o ControlMaster=no \
          -o ControlPath="$control_path" \
          "$ssh_target" \
          "sudo -k -S -p '' bash -lc $quoted_base_cmd" 2>&1 | tee "$tmp_output" | tee -a "$output_file"
        rc=${PIPESTATUS[1]}
        set -e
      else
        set +e
        ssh \
          "${ssh_opts[@]}" \
          -o ControlMaster=no \
          -o ControlPath="$control_path" \
          "$ssh_target" \
          "$cmd" 2>&1 | tee "$tmp_output" | tee -a "$output_file"
        rc=${PIPESTATUS[0]}
        set -e
      fi

      # If cached/config password failed, prompt once for sudo and retry.
      if [[ $rc -ne 0 ]] && grep -Eiq "Sorry, try again|no password was provided|incorrect password attempt|a password is required" "$tmp_output"; then
        if [[ -n "$password" && "$password" != "change_me" && "$SUDO_PASSWORD" != "$password" ]]; then
          printf '[INFO] Retrying sudo with password from config.\n' | tee -a "$output_file"
          SUDO_PASSWORD="$password"
          set +e
          printf '%s\n' "$SUDO_PASSWORD" | ssh \
            "${ssh_opts[@]}" \
            -o ControlMaster=no \
            -o ControlPath="$control_path" \
            "$ssh_target" \
            "sudo -k -S -p '' bash -lc $quoted_base_cmd" 2>&1 | tee "$tmp_output" | tee -a "$output_file"
          rc=${PIPESTATUS[1]}
          set -e
        fi
      fi

      if [[ $rc -ne 0 ]] && grep -Eiq "Sorry, try again|no password was provided|incorrect password attempt|a password is required" "$tmp_output"; then
        read -r -s -p "[INFO] Sudo password failed. Re-enter sudo password for ${username}@${host}: " SUDO_PASSWORD
        echo
        if [[ -n "$SUDO_PASSWORD" ]]; then
          set +e
          printf '%s\n' "$SUDO_PASSWORD" | ssh \
            "${ssh_opts[@]}" \
            -o ControlMaster=no \
            -o ControlPath="$control_path" \
            "$ssh_target" \
            "sudo -k -S -p '' bash -lc $quoted_base_cmd" 2>&1 | tee "$tmp_output" | tee -a "$output_file"
          rc=${PIPESTATUS[1]}
          set -e
        fi
      fi
    else
      set +e
      ssh \
        "${ssh_opts[@]}" \
        -o ControlMaster=no \
        -o ControlPath="$control_path" \
        "$ssh_target" \
        "$cmd" 2>&1 | tee "$tmp_output" | tee -a "$output_file"
      rc=${PIPESTATUS[0]}
      set -e

      # Auto-retry once with sudo when command fails due to permissions/root requirement.
      if [[ $rc -ne 0 ]] && grep -Eiq "permission denied|must be root|operation not permitted|requires root" "$tmp_output"; then
        if [[ -z "$SUDO_PASSWORD" ]]; then
          if [[ -n "$password" && "$password" != "change_me" ]]; then
            SUDO_PASSWORD="$password"
          else
            read -r -s -p "[INFO] Command may require sudo. Enter password for ${username}@${host} (leave blank to skip retry): " SUDO_PASSWORD
            echo
          fi
        fi

        if [[ -n "$SUDO_PASSWORD" ]]; then
          printf '[INFO] Retrying with sudo for command: %s\n' "$cmd" | tee -a "$output_file"
          local quoted_cmd
          printf -v quoted_cmd '%q' "$cmd"
          set +e
          printf '%s\n' "$SUDO_PASSWORD" | ssh \
            "${ssh_opts[@]}" \
            -o ControlMaster=no \
            -o ControlPath="$control_path" \
            "$ssh_target" \
            "sudo -k -S -p '' bash -lc $quoted_cmd" 2>&1 | tee "$tmp_output" | tee -a "$output_file"
          rc=${PIPESTATUS[1]}
          set -e

          if [[ $rc -ne 0 ]] && grep -Eiq "Sorry, try again|no password was provided|incorrect password attempt|a password is required" "$tmp_output"; then
            if [[ -n "$password" && "$password" != "change_me" && "$SUDO_PASSWORD" != "$password" ]]; then
              printf '[INFO] Retrying sudo with password from config.\n' | tee -a "$output_file"
              SUDO_PASSWORD="$password"
              set +e
              printf '%s\n' "$SUDO_PASSWORD" | ssh \
                "${ssh_opts[@]}" \
                -o ControlMaster=no \
                -o ControlPath="$control_path" \
                "$ssh_target" \
                "sudo -k -S -p '' bash -lc $quoted_cmd" 2>&1 | tee "$tmp_output" | tee -a "$output_file"
              rc=${PIPESTATUS[1]}
              set -e
            fi
          fi

          if [[ $rc -ne 0 ]] && grep -Eiq "Sorry, try again|no password was provided|incorrect password attempt|a password is required" "$tmp_output"; then
            read -r -s -p "[INFO] Sudo password failed. Re-enter sudo password for ${username}@${host}: " SUDO_PASSWORD
            echo
            if [[ -n "$SUDO_PASSWORD" ]]; then
              set +e
              printf '%s\n' "$SUDO_PASSWORD" | ssh \
                "${ssh_opts[@]}" \
                -o ControlMaster=no \
                -o ControlPath="$control_path" \
                "$ssh_target" \
                "sudo -k -S -p '' bash -lc $quoted_cmd" 2>&1 | tee "$tmp_output" | tee -a "$output_file"
              rc=${PIPESTATUS[1]}
              set -e
            fi
          fi
        fi
      fi
    fi

    rm -f "$tmp_output"

    printf '[EXIT CODE] %s\n' "$rc" | tee -a "$output_file"
  done

  {
    printf '==============================================================================\n'
    printf 'SESSION END\n'
    printf '==============================================================================\n'
    printf 'timestamp: %s\n' "$(date -Iseconds)"
  } >> "$output_file"

  log "[INFO] Session log saved to: $output_file"
}

run "$@"
