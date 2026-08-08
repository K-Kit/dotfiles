# aliases/fnox.sh — fnox (age-encrypted secrets in ~/fnox.toml) opt-in helpers
# shellcheck shell=bash
#
# `fnox activate` installs a precmd/chpwd hook that AUTO-EXPORTS every secret
# from the nearest fnox.toml into the environment — and since the config lives
# at ~/fnox.toml, that means every shell under $HOME. That fights the
# supply-chain defense (secrets are per-project, never globally exported), so
# activation is OPT-IN per shell:
#
#   fnox-on     # enable auto-loading in THIS shell only
#   fnox-off    # remove the hook again (already-exported vars persist)
#
# Prefer scoping over activation:
#   fnox exec -- <cmd>     # run one command with secrets in its env
#   fnox get NAME          # print one secret value (careful in logs)
#
# Escape hatch: export FNOX_AUTO=1 (e.g. from config.local.sh or a private
# rc file) to activate every new shell. Deliberately NOT the default.

if command -v fnox >/dev/null 2>&1; then
    fnox-on() {
        local _fnox_shell=bash
        [[ -n "${ZSH_VERSION:-}" ]] && _fnox_shell=zsh
        eval "$(command fnox activate "$_fnox_shell")"
        echo "fnox active in this shell — secrets from ~/fnox.toml auto-export. fnox-off to stop."
    }

    fnox-off() {
        if typeset -f fnox >/dev/null 2>&1; then
            fnox deactivate
            echo "fnox shell hook removed (vars already exported stay until the shell exits)"
        else
            echo "fnox shell integration is not active" >&2
            return 1
        fi
    }

    [[ "${FNOX_AUTO:-0}" == "1" ]] && fnox-on >/dev/null
fi
