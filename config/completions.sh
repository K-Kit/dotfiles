# ZSH completion functions

# Python module completion for `python -m <module>`
# Finds modules by looking for __main__.py files and standalone .py files
_python_module_complete() {
    local cur="${words[CURRENT]}"
    if [[ "${words[CURRENT-1]}" == "-m" ]]; then
        local modules=()
        # Find __main__.py files
        for dir in $(find . -type f -name "__main__.py" | sed 's|/__main__.py||' | sed 's|^\./||'); do
            modules+=(${dir//\//.})
        done
        # Find standalone .py files
        for file in $(find . -type f -name "*.py" ! -name "__*" | sed 's|\.py$||' | sed 's|^\./||'); do
            modules+=(${file//\//.})
        done
        compadd -a modules
    fi
}

compdef _python_module_complete python python3

# Tmux session name completion
_tmux_sessions() {
    local sessions
    sessions=(${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null)"})
    compadd -a sessions
}
compdef _tmux_sessions taa tad tdel

# hz (Hetzner server manager) completion — custom_bins/hz
# Server names come from the API via the hidden `hz _servers` helper (needs
# the fnox token; ~1 API round-trip per tab), aliases from ~/.ssh/config.
_hz() {
    local -a subcmds
    subcmds=(
        'list:servers with status, ip, type, idle label'
        'create:create server + ssh-add (requires --yes)'
        'up:shortcut — create + ssh-add with 2h idle-shutdown, implies --yes'
        'delete:delete server + its ssh entries (requires --yes)'
        'ssh-add:upsert Host block in ~/.ssh/config'
        'ssh-sync:Host entry per running server'
        'ssh-rm:remove an hz-managed Host block'
        'idle:manage on-box idle-shutdown watchdog'
        'poweron:wake a powered-off server'
    )
    if (( CURRENT == 2 )); then
        _describe 'hz command' subcmds
        return
    fi
    local -a opts
    case "${words[2]}" in
        create|up)
            opts=(--type --image --ssh-key --user --idle-shutdown --idle-hours --yes)
            compadd -a opts
            ;;
        ssh-add)
            if [[ "${words[CURRENT-1]}" == "--user" ]]; then
                compadd k-kit root
            elif [[ "${words[CURRENT]}" == -* ]]; then
                opts=(--user --alias); compadd -a opts
            else
                compadd -- ${(f)"$(hz _servers 2>/dev/null)"}
            fi
            ;;
        delete)
            if [[ "${words[CURRENT]}" == -* ]]; then
                compadd -- --yes
            else
                compadd -- ${(f)"$(hz _servers 2>/dev/null)"}
            fi
            ;;
        poweron)
            compadd -- ${(f)"$(hz _servers 2>/dev/null)"}
            ;;
        ssh-rm)
            compadd -- ${(f)"$(hz _aliases 2>/dev/null)"}
            ;;
        ssh-sync)
            compadd -- --dry-run
            ;;
        idle)
            if (( CURRENT == 3 )); then
                compadd enable disable status
            elif [[ "${words[CURRENT]}" == -* ]]; then
                compadd -- --hours
            else
                compadd -- ${(f)"$(hz _servers 2>/dev/null)"}
            fi
            ;;
    esac
}
compdef _hz hz
