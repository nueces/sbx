SUPPORTED_SHELLS = ("bash", "zsh", "fish")

COMMANDS = (
    "run",
    "create",
    "recreate",
    "remove",
    "rm",
    "stop",
    "shell",
    "list",
    "ls",
    "network",
    "image",
    "doctor",
    "completion",
)
NETWORK_COMMANDS = ("forward", "auth-port", "close-auth-port", "status")
IMAGE_COMMANDS = ("build", "list", "ls")
AGENTS = ("pi", "claude", "codex")

GLOBAL_OPTIONS = ("--config", "--debug", "--help")
START_OPTIONS = (
    "--agent",
    "--memory",
    "--cpus",
    "--disk-size",
    "--image",
    "--mount",
    "--project-path",
    "--run-user",
    "--env",
    "--writable-mounts",
    "--no-attach",
    "--stop-on-exit",
    "--keep-running",
    "--boot-timeout",
    "--install-timeout",
    "--write-config",
    "--json",
    "--help",
)
SHELL_OPTIONS = ("--keep-running", "--run-user", "--project-path", "--root", "--help")
LS_OPTIONS = ("--running", "--json", "--help")
RM_OPTIONS = ("--force", "--help")
DOCTOR_OPTIONS = ("--fix", "--help")
STOP_OPTIONS = ("--help",)
RUN_OPTIONS = START_OPTIONS
FORWARD_OPTIONS = ("--name", "--help")
AUTH_PORT_OPTIONS = ("--guest-port", "--host-port", "--replace", "--help")
NETWORK_STATUS_OPTIONS = ("--host-port", "--json", "--help")
IMAGE_BUILD_OPTIONS = (
    "--name",
    "--base-image",
    "--containerfile",
    "--dockerfile",
    "--base-containerfile",
    "--agent-containerfile",
    "--rootfs-size-mb",
    "--cache-dir",
    "--json",
    "--help",
)
IMAGE_LS_OPTIONS = ("--json", "--help")
COMPLETION_SHELLS = SUPPORTED_SHELLS


def _words(values: tuple[str, ...]) -> str:
    return " ".join(values)


def _zsh_words(values: tuple[str, ...]) -> str:
    return " ".join(f"'{value}'" for value in values)


def completion_script(shell: str) -> str:
    if shell == "bash":
        return bash_completion()
    if shell == "zsh":
        return zsh_completion()
    if shell == "fish":
        return fish_completion()
    raise ValueError(f"unsupported shell: {shell}")


def bash_completion() -> str:
    commands = _words(COMMANDS)
    global_options = _words(GLOBAL_OPTIONS)
    run_options = _words(RUN_OPTIONS)
    create_options = _words(START_OPTIONS)
    recreate_options = _words(("--force", *START_OPTIONS))
    shell_options = _words(SHELL_OPTIONS)
    ls_options = _words(LS_OPTIONS)
    rm_options = _words(RM_OPTIONS)
    doctor_options = _words(DOCTOR_OPTIONS)
    stop_options = _words(STOP_OPTIONS)
    network_commands = _words(NETWORK_COMMANDS)
    image_commands = _words(IMAGE_COMMANDS)
    forward_options = _words(FORWARD_OPTIONS)
    auth_port_options = _words(AUTH_PORT_OPTIONS)
    network_status_options = _words(NETWORK_STATUS_OPTIONS)
    image_build_options = _words(IMAGE_BUILD_OPTIONS)
    image_ls_options = _words(IMAGE_LS_OPTIONS)
    agents = _words(AGENTS)
    shells = _words(COMPLETION_SHELLS)
    return f"""# bash completion for sbx
_sbx_complete() {{
    local cur prev cmd subcmd line redir_cur
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    line="${{COMP_LINE:0:COMP_POINT}}"

    if [[ "$line" =~ (^|[[:space:]])([0-9]?>|[0-9]?>>|<|&>)[[:space:]]*([^[:space:]]*)$ ]]; then
        redir_cur="${{BASH_REMATCH[3]}}"
        compopt -o filenames 2>/dev/null
        COMPREPLY=( $(compgen -f -- "$redir_cur") )
        return 0
    fi

    case "$prev" in
        --agent)
            COMPREPLY=( $(compgen -W "{agents}" -- "$cur") )
            return 0
            ;;
        --config|--image|--mount|--project-path)
            COMPREPLY=( $(compgen -f -- "$cur") )
            return 0
            ;;
    esac

    cmd=""
    for word in "${{COMP_WORDS[@]:1:COMP_CWORD-1}}"; do
        case "$word" in
            --*) ;;
            *) cmd="$word"; break ;;
        esac
    done

    if [[ -z "$cmd" ]]; then
        COMPREPLY=( $(compgen -W "{commands} {global_options}" -- "$cur") )
        return 0
    fi

    case "$cmd" in
        run)
            COMPREPLY=( $(compgen -W "{run_options}" -- "$cur") )
            ;;
        create)
            COMPREPLY=( $(compgen -W "{create_options}" -- "$cur") )
            ;;
        recreate)
            COMPREPLY=( $(compgen -W "{recreate_options}" -- "$cur") )
            ;;
        shell)
            COMPREPLY=( $(compgen -W "{shell_options}" -- "$cur") )
            ;;
        list|ls)
            COMPREPLY=( $(compgen -W "{ls_options}" -- "$cur") )
            ;;
        remove|rm)
            COMPREPLY=( $(compgen -W "{rm_options}" -- "$cur") )
            ;;
        stop)
            COMPREPLY=( $(compgen -W "{stop_options}" -- "$cur") )
            ;;
        network)
            subcmd=""
            for word in "${{COMP_WORDS[@]:2:COMP_CWORD-2}}"; do
                case "$word" in
                    --*) ;;
                    *) subcmd="$word"; break ;;
                esac
            done
            if [[ -z "$subcmd" ]]; then
                COMPREPLY=( $(compgen -W "{network_commands}" -- "$cur") )
            elif [[ "$subcmd" == "forward" ]]; then
                COMPREPLY=( $(compgen -W "{forward_options}" -- "$cur") )
            elif [[ "$subcmd" == "auth-port" ]]; then
                COMPREPLY=( $(compgen -W "{auth_port_options}" -- "$cur") )
            elif [[ "$subcmd" == "close-auth-port" ]]; then
                COMPREPLY=( $(compgen -W "--help" -- "$cur") )
            elif [[ "$subcmd" == "status" ]]; then
                COMPREPLY=( $(compgen -W "{network_status_options}" -- "$cur") )
            fi
            ;;
        image)
            subcmd=""
            for word in "${{COMP_WORDS[@]:2:COMP_CWORD-2}}"; do
                case "$word" in
                    --*) ;;
                    *) subcmd="$word"; break ;;
                esac
            done
            if [[ -z "$subcmd" ]]; then
                COMPREPLY=( $(compgen -W "{image_commands}" -- "$cur") )
            elif [[ "$subcmd" == "build" ]]; then
                COMPREPLY=( $(compgen -W "{image_build_options}" -- "$cur") )
            elif [[ "$subcmd" == "list" || "$subcmd" == "ls" ]]; then
                COMPREPLY=( $(compgen -W "{image_ls_options}" -- "$cur") )
            fi
            ;;
        doctor)
            COMPREPLY=( $(compgen -W "{doctor_options}" -- "$cur") )
            ;;
        completion)
            COMPREPLY=( $(compgen -W "{shells}" -- "$cur") )
            ;;
    esac
}}
complete -o default -F _sbx_complete sbx
"""


def zsh_completion() -> str:
    commands = _zsh_words(COMMANDS)
    run_options = _zsh_words(RUN_OPTIONS)
    create_options = _zsh_words(START_OPTIONS)
    recreate_options = _zsh_words(("--force", *START_OPTIONS))
    shell_options = _zsh_words(SHELL_OPTIONS)
    ls_options = _zsh_words(LS_OPTIONS)
    rm_options = _zsh_words(RM_OPTIONS)
    doctor_options = _zsh_words(DOCTOR_OPTIONS)
    stop_options = _zsh_words(STOP_OPTIONS)
    network_commands = _zsh_words(NETWORK_COMMANDS)
    forward_options = _zsh_words(FORWARD_OPTIONS)
    auth_port_options = _zsh_words(AUTH_PORT_OPTIONS)
    network_status_options = _zsh_words(NETWORK_STATUS_OPTIONS)
    image_commands = _zsh_words(IMAGE_COMMANDS)
    image_build_options = _zsh_words(IMAGE_BUILD_OPTIONS)
    image_ls_options = _zsh_words(IMAGE_LS_OPTIONS)
    shells = _zsh_words(COMPLETION_SHELLS)
    return f"""#compdef sbx
# zsh completion for sbx
_sbx() {{
  local -a commands run_options create_options recreate_options shell_options
  local -a ls_options rm_options doctor_options stop_options network_commands
  local -a forward_options auth_port_options network_status_options
  local -a image_commands image_build_options image_ls_options shells
  commands=({commands})
  run_options=({run_options})
  create_options=({create_options})
  recreate_options=({recreate_options})
  shell_options=({shell_options})
  ls_options=({ls_options})
  rm_options=({rm_options})
  doctor_options=({doctor_options})
  stop_options=({stop_options})
  network_commands=({network_commands})
  forward_options=({forward_options})
  auth_port_options=({auth_port_options})
  network_status_options=({network_status_options})
  image_commands=({image_commands})
  image_build_options=({image_build_options})
  image_ls_options=({image_ls_options})
  shells=({shells})

  case $CURRENT in
    2)
      _describe 'command' commands
      ;;
    *)
      case $words[2] in
        run)
          _describe 'option' run_options
          ;;
        create)
          _describe 'option' create_options
          ;;
        recreate)
          _describe 'option' recreate_options
          ;;
        shell)
          _describe 'option' shell_options
          ;;
        list|ls)
          _describe 'option' ls_options
          ;;
        remove|rm)
          _describe 'option' rm_options
          ;;
        stop)
          _describe 'option' stop_options
          ;;
        network)
          if (( CURRENT == 3 )); then
            _describe 'network command' network_commands
          elif [[ $words[3] == "forward" ]]; then
            _describe 'option' forward_options
          elif [[ $words[3] == "auth-port" ]]; then
            _describe 'option' auth_port_options
          elif [[ $words[3] == "close-auth-port" ]]; then
            _describe 'option' '(--help)'
          elif [[ $words[3] == "status" ]]; then
            _describe 'option' network_status_options
          else
            _arguments '*: :->args'
          fi
          ;;
        image)
          if (( CURRENT == 3 )); then
            _describe 'image command' image_commands
          elif [[ $words[3] == "build" ]]; then
            _describe 'option' image_build_options
          elif [[ $words[3] == "list" || $words[3] == "ls" ]]; then
            _describe 'option' image_ls_options
          else
            _arguments '*: :->args'
          fi
          ;;
        doctor)
          _describe 'option' doctor_options
          ;;
        completion)
          _describe 'shell' shells
          ;;
        *)
          _arguments '*: :->args'
          ;;
      esac
      ;;
  esac
}}
_sbx "$@"
"""


def _fish_flag(option: str) -> str:
    if option.startswith("--"):
        return "-l " + option.removeprefix("--")
    return "-s " + option.removeprefix("-")


def fish_completion() -> str:
    lines = ["# fish completion for sbx"]
    for option in GLOBAL_OPTIONS:
        lines.append(f"complete -c sbx -f -l {option.removeprefix('--')}")
    for command in COMMANDS:
        lines.append(f"complete -c sbx -f -n '__fish_use_subcommand' -a {command}")
    for command, options in (
        ("run", RUN_OPTIONS),
        ("create", START_OPTIONS),
        ("recreate", ("--force", *START_OPTIONS)),
        ("shell", SHELL_OPTIONS),
        ("list", LS_OPTIONS),
        ("ls", LS_OPTIONS),
        ("remove", RM_OPTIONS),
        ("rm", RM_OPTIONS),
        ("doctor", DOCTOR_OPTIONS),
        ("stop", STOP_OPTIONS),
    ):
        for option in options:
            lines.append(
                f"complete -c sbx -f -n '__fish_seen_subcommand_from {command}' "
                f"{_fish_flag(option)}"
            )
    for agent in AGENTS:
        lines.append(f"complete -c sbx -f -n '__fish_seen_argument -l agent' -a {agent}")
    network_subcommands = _words(NETWORK_COMMANDS)
    for command in NETWORK_COMMANDS:
        lines.append(
            "complete -c sbx -f -n '__fish_seen_subcommand_from network; "
            f"and not __fish_seen_subcommand_from {network_subcommands}' -a {command}"
        )
    for command, options in (
        ("forward", FORWARD_OPTIONS),
        ("auth-port", AUTH_PORT_OPTIONS),
        ("close-auth-port", ("--help",)),
        ("status", NETWORK_STATUS_OPTIONS),
    ):
        for option in options:
            lines.append(
                f"complete -c sbx -f -n '__fish_seen_subcommand_from {command}' "
                f"{_fish_flag(option)}"
            )
    image_subcommands = _words(IMAGE_COMMANDS)
    for command in IMAGE_COMMANDS:
        lines.append(
            "complete -c sbx -f -n '__fish_seen_subcommand_from image; "
            f"and not __fish_seen_subcommand_from {image_subcommands}' -a {command}"
        )
    for option in IMAGE_BUILD_OPTIONS:
        lines.append(
            f"complete -c sbx -f -n '__fish_seen_subcommand_from build' {_fish_flag(option)}"
        )
    for option in IMAGE_LS_OPTIONS:
        lines.append(f"complete -c sbx -f -n '__fish_seen_subcommand_from ls' {_fish_flag(option)}")
    for shell in COMPLETION_SHELLS:
        lines.append(f"complete -c sbx -f -n '__fish_seen_subcommand_from completion' -a {shell}")
    return "\n".join(lines) + "\n"
