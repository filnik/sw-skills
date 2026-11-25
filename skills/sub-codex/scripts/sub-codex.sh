#!/bin/bash
# Lancia un sottotask su Codex (codex exec) e stampa solo il messaggio finale; il resto va nel log.
# Uso: sub-codex.sh [--full] [--search] [--fast] [--important] [--needs-codex] [--cd DIR] [--model M] [--effort E] [--name N] (--file PROMPT.md ["parametri"] | "prompt")

dir=$PWD; sandbox=workspace-write; model=gpt-6.2-sol; effort=xhigh; name=task; file=""; fast=auto; extra=()
important=no; model_set=no; needs_codex=no
while [ $# -gt 0 ]; do
  case "$1" in --cd|--model|--effort|--name|--file) [ $# -ge 2 ] || { echo "manca il valore di $1" >&2; exit 2; } ;; esac
  case "$1" in
    --full)  sandbox=danger-full-access; shift ;;
    --search) extra+=(-c web_search=live); shift ;;
    --fast)   fast=yes; shift ;;
    --important) important=yes; shift ;;
    --needs-codex) needs_codex=yes; shift ;;
    --cd)    dir=$2; shift 2 ;;
    --model)  model=$2; model_set=yes; shift 2 ;;
    --effort) effort=$2; shift 2 ;;
    --name)   name=$2; shift 2 ;;
    --file)   file=$2; shift 2 ;;
    --)       shift; break ;;
    -*)       echo "opzione sconosciuta: $1" >&2; exit 2 ;;
    *)        break ;;
  esac
done
task="$*"

# Codex solo per il lavoro DocuSketch: nei progetti personali il task lo fa Claude
root=$(cd "$HOME/Documents/projects/docusketch" 2>/dev/null && pwd -P)
real=$(cd "$dir" 2>/dev/null && pwd -P)
if [ -z "$root" ] || [ -z "$real" ] || { [ "$real" != "$root" ] && [ "${real#"$root"/}" = "$real" ]; }; then
  echo "sub-codex: Codex lavora solo nelle cartelle DocuSketch ($HOME/Documents/projects/docusketch);" \
    "${real:-$dir} non lo è. Fai il task con Claude." >&2
  exit 4
fi

# con --file il testo che segue si accoda al file: un prompt-modello e i suoi parametri
if [ -n "$file" ]; then task="$(cat "$file")${task:+$'\n\n'$task}" || exit 2; fi
[ -n "$task" ] || { echo "manca il prompt: --file PROMPT.md o testo" >&2; exit 2; }

# Codex di riserva per i suoi connettori, o finito: il task va a un agent Claude lanciato dalla sessione principale.
# Le run Luna (un ventesimo di Sol) e quelle --needs-codex restano su Codex finché non è finito.
read -r side kind < <(if [ -n "${SUB_CODEX_ROUTE:-}" ]; then echo "$SUB_CODEX_ROUTE"
  else python3 "$(dirname "$0")/quota-route.py" --sub-route 2>/dev/null; fi)
if [ "$side" = claude ] && { [ "$kind" = usedup ] || { [ "$needs_codex" = no ] && [ "$model" != gpt-6-luna ]; }; }; then
  out="${TMPDIR:-/tmp}/sub-codex"
  mkdir -p "$out" || exit 2
  handoff="$out/$(date +%Y%m%d-%H%M%S)-$(printf '%s' "$name" | tr -c 'A-Za-z0-9._-' '-').prompt.md"
  printf '%s\n' "$task" > "$handoff" || exit 2
  if [ "$important" = yes ]; then class=hard; elif [ "$effort" = xhigh ]; then class=judgement; else class=gathering; fi
  read -r m e < <(python3 "$(dirname "$0")/quota-route.py" --claude-pick "$class" 2>/dev/null)
  m=${m:-opus}; e=${e:-high}
  [ "$kind" = usedup ] && why="Codex is used up" || why="Codex kept for its connectors, Claude has margin"
  echo "sub-codex: Codex not started ($why), exit 5. This is not a Codex failure."
  echo "Run the task on Claude now with one Agent call, then go on with the calling skill's steps:"
  echo "  subagent_type: sub-claude, model: $m, effort: $e (or the Claude agent the calling skill names for this job,"
  echo "  e.g. device-tester for phones, with its own model) · run_in_background: true · description: $name"
  echo "  prompt: Working directory: $real. Your task is in $handoff: read it and do it."
  echo "Read its final message as Codex's: the report file first, by sub-codex §After."
  exit 5
fi

# dentro il sandbox di Claude codex muore con "Operation not permitted": fermarsi subito e dire perché
probe="$HOME/.codex/.sub-codex-probe.$$"
if ! ( : > "$probe" ) 2>/dev/null; then
  echo "sub-codex: gira nel sandbox di Claude, dove codex non parte. L'esclusione di settings.json non scatta" \
    "se il comando contiene backtick o \$(...): metti il prompt in un file (--file) e rilancia." >&2
  exit 3
fi
rm -f "$probe"

# tier e Astra decisi da quota-route.py sull'uso settimanale di Codex e sull'avanzo al reset; se illeggibile default e Sol
read -r weekly tier astra < <(python3 "$(dirname "$0")/quota-route.py" --codex-tier 2>/dev/null)
[ "$fast" = auto ] && { [ "$tier" = fast ] && fast=yes || fast=no; }
[ "$important" = yes ] && [ "$astra" = astra ] && [ "$model_set" = no ] && model=gpt-6.2-astra
[ "$fast" = yes ] && extra+=(-c service_tier=fast)
# Codex passa ai comandi solo le variabili di base: senza la tab di iTerm ds-notify non trova la riga del pannello
[ -n "${ITERM_SESSION_ID:-}" ] && extra+=(-c "shell_environment_policy.set.ITERM_SESSION_ID=\"$ITERM_SESSION_ID\"")

# regole fisse di ogni sottotask: git in sola lettura, risposta breve per chi lo ha lanciato (le stesse, per
# Claude, stanno in ~/.claude/agents/sub-claude.md)
preamble='You are a sub-task delegated by Claude Code. Do the task below in the working directory.
Never commit, push, rebase, merge, reset or switch branches: leave your changes in the working tree.
Search rule: on this Mac a git grep with a regex (-E, -G, -P, -i, .*) over the mobile repo grows by GBs per second
and once rebooted the machine. Use rg (never -P) on the working tree; use git grep only for a commit that is not
checked out, and a git grep that is not plain -F without -i gets LC_ALL=C in front
(LC_ALL=C git grep -n -E ... <sha> -- KMP); at most two repo-wide searches at once. Copy this rule and
the git rule above word for word into every agent you spawn.
Your final message is all the caller reads: in Italian, under 25 lines, with the outcome, the files you
changed, the commands you ran and their result, and what is left open or uncertain.
When writing a report file, start with ## Findings: one block per finding, at most 8 lines (id, severity if any,
file:line on the first line; up to 6 quoted lines; one-line scenario). Put details under ## Evidence, one
### <id> section per finding. List the findings by id in your final message.
Wait for a command that lasts more than 30 s with write_stdin, empty chars and yield_time_ms 300000 (in the exec
tool, first line // @exec: {"yield_time_ms": 300000}): no short checks, each one re-reads the whole context.'

# binario ufficiale del daemon, che si aggiorna da solo: i modelli nuovi arrivano senza rifare la build con la patch
codex_bin="$HOME/.codex/packages/app-server-daemon/current/bin/codex"
[ -x "$codex_bin" ] || codex_bin=codex

out="${TMPDIR:-/tmp}/sub-codex"
mkdir -p "$out" || exit 2
base="$out/$(date +%Y%m%d-%H%M%S)-$(printf '%s' "$name" | tr -c 'A-Za-z0-9._-' '-')"
printf '%s\n\n%s\n' "$preamble" "$task" |
  "$codex_bin" exec -C "$dir" -m "$model" -c "model_reasoning_effort=$effort" -s "$sandbox" "${extra[@]}" \
    -c sandbox_workspace_write.network_access=true \
    --skip-git-repo-check --color never -o "$base.md" - > "$base.log" 2>&1
rc=$?

# "model at capacity" è OpenAI pieno, non la nostra quota: si riprende la stessa sessione, subito e poi con backoff
from=1
for delay in 0 15 30 60 120; do
  [ "$rc" -ne 0 ] && tail -n "+$from" "$base.log" | tail -5 | grep -q 'Selected model is at capacity' || break
  id=$(sed -n 's/^session id: //p' "$base.log" | head -1)
  [ -n "$id" ] || break
  sleep "$delay"
  echo "--- sub-codex: model at capacity, resume $id dopo ${delay}s" >> "$base.log"
  from=$(( $(wc -l < "$base.log") + 1 ))
  ( cd "$dir" && "$codex_bin" exec resume "$id" -m "$model" -c "model_reasoning_effort=$effort" -c "sandbox_mode=$sandbox" \
      "${extra[@]}" -c sandbox_workspace_write.network_access=true --skip-git-repo-check -o "$base.md" \
      "The previous turn stopped because the model was at capacity. Continue the task from where you stopped." \
  ) >> "$base.log" 2>&1
  rc=$?
done

read -r weekly_end _ < <(python3 "$(dirname "$0")/quota-route.py" --codex-tier 2>/dev/null)
echo "log: $base.log"
echo "tier: $([ "$fast" = yes ] && echo fast || echo default), $model $effort, settimanale Codex ${weekly:-?}% -> ${weekly_end:-?}%"
if [ -s "$base.md" ]; then
  cat "$base.md"
else
  echo "codex exit $rc, nessun messaggio finale. Coda del log:"
  tail -20 "$base.log"
fi
exit $rc
