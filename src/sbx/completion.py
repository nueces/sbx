SUPPORTED_SHELLS = ("bash", "zsh", "fish")

COMMANDS = (
    "run",
    "create",
    "recreate",
    "rm",
    "stop",
    "shell",
    "ls",
    "network",
    "image",
    "doctor",
    "completion",
)
NETWORK_COMMANDS = ("forward", "auth-port", "close-auth-port", "status")
IMAGE_COMMANDS = ("build-debian", "ls")
AGENTS = ("pi", "claude", "codex")

GLOBAL_OPTIONS = ("--config", "--debug", "--help")
START_OPTIONS = (
    "--agent",
    "--name",
    "--memory",
    "--cpus",
    "--disk-size",
    "--os",
    "--image",
    "--mount",
    "--project-path",
    "--run-user",
    "--env",
    "--auth-port",
    "--no-auth-port",
    "--auth-host-port",
    "--auth-guest-port",
    "--copy-host-credentials",
    "--no-copy-host-credentials",
    "--git-config",
    "--no-git-config",
    "--writable-mounts",
    "--attach",
    "--no-attach",
    "--stop-on-exit",
    "--keep-running",
    "--boot-timeout",
    "--install-timeout",
    "--write-config",
    "--no-write-config",
    "--json",
    "--help",
)
SHELL_OPTIONS = (
    "--force-start",
    "--keep-running",
    "--run-user",
    "--project-path",
    "--git-config",
    "--no-git-config",
    "--root",
    "--help",
)
LS_OPTIONS = ("--all", "-a", "--help")
RM_OPTIONS = ("--force", "--help")
STOP_OPTIONS = ("--help",)
RUN_OPTIONS = ("--force-start", *START_OPTIONS)
AUTH_PORT_OPTIONS = ("--guest-port", "--host-port", "--replace", "--help")
NETWORK_STATUS_OPTIONS = ("--host-port", "--help")
IMAGE_BUILD_DEBIAN_OPTIONS = (
    "--name",
    "--base-image",
    "--containerfile",
    "--dockerfile",
    "--base-containerfile",
    "--agent-containerfile",
    "--with-docker",
    "--rootfs-size-mb",
    "--ssh-public-key",
    "--cache-dir",
    "--kernel-url",
    "--json",
    "--sdk-sketch",
    "--print-sdk-sketch",
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
    stop_options = _words(STOP_OPTIONS)
    network_commands = _words(NETWORK_COMMANDS)
    image_commands = _words(IMAGE_COMMANDS)
    auth_port_options = _words(AUTH_PORT_OPTIONS)
    network_status_options = _words(NETWORK_STATUS_OPTIONS)
    image_build_debian_options = _words(IMAGE_BUILD_DEBIAN_OPTIONS)
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
        ls)
            COMPREPLY=( $(compgen -W "{ls_options}" -- "$cur") )
            ;;
        rm)
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
                COMPREPLY=( $(compgen -W "--help" -- "$cur") )
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
            elif [[ "$subcmd" == "build-debian" ]]; then
                COMPREPLY=( $(compgen -W "{image_build_debian_options}" -- "$cur") )
            elif [[ "$subcmd" == "ls" ]]; then
                COMPREPLY=( $(compgen -W "{image_ls_options}" -- "$cur") )
            fi
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
    stop_options = _zsh_words(STOP_OPTIONS)
    network_commands = _zsh_words(NETWORK_COMMANDS)
    auth_port_options = _zsh_words(AUTH_PORT_OPTIONS)
    network_status_options = _zsh_words(NETWORK_STATUS_OPTIONS)
    image_commands = _zsh_words(IMAGE_COMMANDS)
    image_build_debian_options = _zsh_words(IMAGE_BUILD_DEBIAN_OPTIONS)
    image_ls_options = _zsh_words(IMAGE_LS_OPTIONS)
    shells = _zsh_words(COMPLETION_SHELLS)
    return f"""#compdef sbx
# zsh completion for sbx
_sbx() {{
  local -a commands run_options create_options recreate_options shell_options
  local -a ls_options rm_options stop_options network_commands
  local -a auth_port_options network_status_options
  local -a image_commands image_build_debian_options image_ls_options shells
  commands=({commands})
  run_options=({run_options})
  create_options=({create_options})
  recreate_options=({recreate_options})
  shell_options=({shell_options})
  ls_options=({ls_options})
  rm_options=({rm_options})
  stop_options=({stop_options})
  network_commands=({network_commands})
  auth_port_options=({auth_port_options})
  network_status_options=({network_status_options})
  image_commands=({image_commands})
  image_build_debian_options=({image_build_debian_options})
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
        ls)
          _describe 'option' ls_options
          ;;
        rm)
          _describe 'option' rm_options
          ;;
        stop)
          _describe 'option' stop_options
          ;;
        network)
          if (( CURRENT == 3 )); then
            _describe 'network command' network_commands
          elif [[ $words[3] == "forward" ]]; then
            _describe 'option' '(--help)'
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
          elif [[ $words[3] == "build-debian" ]]; then
            _describe 'option' image_build_debian_options
          elif [[ $words[3] == "ls" ]]; then
            _describe 'option' image_ls_options
          else
            _arguments '*: :->args'
          fi
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
        lines.append(
            "complete -c sbx -f -n '__fish_use_subcommand' "
            f"-a {command}"
        )
    for command, options in (
        ("run", RUN_OPTIONS),
        ("create", START_OPTIONS),
        ("recreate", ("--force", *START_OPTIONS)),
    ):
        for option in options:
            lines.append(
                f"complete -c sbx -f -n '__fish_seen_subcommand_from {command}' "
                f"{_fish_flag(option)}"
            )
    for agent in AGENTS:
        lines.append(
            "complete -c sbx -f -n '__fish_seen_argument -l agent' "
            f"-a {agent}"
        )
    for option in SHELL_OPTIONS:
        lines.append(
            f"complete -c sbx -f -n '__fish_seen_subcommand_from shell' {_fish_flag(option)}"
        )
    for option in LS_OPTIONS:
        lines.append(
            f"complete -c sbx -f -n '__fish_seen_subcommand_from ls' {_fish_flag(option)}"
        )
    for option in RM_OPTIONS:
        lines.append(f"complete -c sbx -f -n '__fish_seen_subcommand_from rm' {_fish_flag(option)}")
    for option in STOP_OPTIONS:
        lines.append(
            f"complete -c sbx -f -n '__fish_seen_subcommand_from stop' {_fish_flag(option)}"
        )
    network_subcommands = _words(NETWORK_COMMANDS)
    for command in NETWORK_COMMANDS:
        lines.append(
            "complete -c sbx -f -n '__fish_seen_subcommand_from network; "
            f"and not __fish_seen_subcommand_from {network_subcommands}' -a {command}"
        )
    for command, options in (
        ("forward", ("--help",)),
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
    for option in IMAGE_BUILD_DEBIAN_OPTIONS:
        lines.append(
            "complete -c sbx -f -n '__fish_seen_subcommand_from build-debian' "
            f"{_fish_flag(option)}"
        )
    for option in IMAGE_LS_OPTIONS:
        lines.append(
            "complete -c sbx -f -n '__fish_seen_subcommand_from ls' "
            f"{_fish_flag(option)}"
        )
    for shell in COMPLETION_SHELLS:
        lines.append(f"complete -c sbx -f -n '__fish_seen_subcommand_from completion' -a {shell}")
    return "\n".join(lines) + "\n"
