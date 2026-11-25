#!/bin/bash
# Verifica che sub-codex e l'hook di quota lavorino solo nelle cartelle DocuSketch. Non lancia mai Codex:
# dentro DocuSketch lo script si ferma prima, per il prompt mancante (exit 2).
here=$(dirname "$0"); ds="$HOME/Documents/projects/docusketch"; fail=0
check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: atteso $3, ottenuto $2"; fail=1; fi; }

bash "$here/sub-codex.sh" --cd "$HOME/Documents/projects" 2>/dev/null; check "fuori DocuSketch: rifiuto" $? 4
bash "$here/sub-codex.sh" --cd "$ds-fake-prefix" 2>/dev/null; check "prefisso simile ma fuori: rifiuto" $? 4
bash "$here/sub-codex.sh" --cd "$ds" 2>/dev/null; check "radice DocuSketch: ammessa" $? 2
bash "$here/sub-codex.sh" --cd "$ds/docusketch-product" 2>/dev/null; check "sottocartella DocuSketch: ammessa" $? 2

handoff() { SUB_CODEX_ROUTE="$1" bash "$here/sub-codex.sh" --cd "$ds" --name test-handoff "${@:2}" "prova" 2>/dev/null |
  grep -cE 'subagent_type: sub-claude, model: (sonnet|opus|fable), effort: (medium|high|xhigh)'; return "${PIPESTATUS[0]}"; }
out=$(handoff "claude reserve"); check "Codex di riserva: exit 5" $? 5; check "Codex di riserva: chiamata sub-claude" "$out" 1
out=$(handoff "claude usedup" --needs-codex); check "Codex finito, anche --needs-codex: exit 5" $? 5
SUB_CODEX_ROUTE="claude reserve" bash "$here/sub-codex.sh" --cd "$ds" 2>/dev/null; check "riserva senza prompt: exit 2" $? 2

hook() { printf '{"cwd": "%s"}' "$1" | python3 "$here/quota-route.py" | grep -c 'not a DocuSketch folder'; }
check "hook fuori DocuSketch: Claude" "$(hook "$HOME/Documents/projects")" 1
check "hook dentro DocuSketch: routing normale" "$(hook "$ds/docusketch-product")" 0
exit $fail
