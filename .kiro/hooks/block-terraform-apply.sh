#!/bin/sh
# PreToolUse guard for the apply rule in .kiro/steering/delegation-and-safety.md.
# Reads the hook JSON on stdin, extracts the shell command, and exits 2 (block) when it:
#   - runs `terraform ... destroy` or `terraform ... apply -destroy` (any global flags before the subcommand);
#   - runs `terraform ... apply` unless the ONLY thing applied is a saved plan file named *.approved.tfplan,
#     on a single line, with no command chaining. Flags such as -no-color / -input=false are allowed
#     before the plan file; output redirections (*> file, > file, 2>&1) are ignored.
# Known limit: approval is a filename convention, not a binding to the plan the user read.
# Requires a POSIX sh with grep/sed and python3 or python (Git for Windows provides them).

cmd=$(python3 -c "import sys,json
d=json.load(sys.stdin)
ti=d.get('toolInput') or d.get('tool_input') or {}
print(ti.get('command','') if isinstance(ti,dict) else '')" 2>/dev/null || python -c "import sys,json
d=json.load(sys.stdin)
ti=d.get('toolInput') or d.get('tool_input') or {}
print(ti.get('command','') if isinstance(ti,dict) else '')" 2>/dev/null)

# Not a terraform command at all: allow.
printf '%s\n' "$cmd" | grep -Eiq 'terraform' || exit 0

# Normalise: lower case (Windows is case-insensitive) and `terraform.exe` -> `terraform`.
cmd=$(printf '%s' "$cmd" | tr 'A-Z' 'a-z' | sed -E 's/terraform\.exe/terraform/g')

# Any token sequence between `terraform` and the subcommand (global flags, -chdir with = or space).
TF='terraform([[:space:]]+[^;|&[:space:]]+)*[[:space:]]+'

if printf '%s\n' "$cmd" | grep -Eq "${TF}destroy([[:space:]]|$)" ||
   printf '%s\n' "$cmd" | grep -Eq "${TF}apply([[:space:]][^;|&]*)?[[:space:]]-destroy([[:space:]=]|$)"; then
  echo "BLOCKED: terraform destroy is not allowed from agents (.kiro/steering/delegation-and-safety.md)." >&2
  exit 2
fi

printf '%s\n' "$cmd" | grep -Eq "${TF}apply([[:space:]]|$)" || exit 0

# From here on the command applies something. Allow only one approved plan file.
lines=$(printf '%s\n' "$cmd" | grep -c .)
# Strip a leading PowerShell env-assignment prefix ($env:AWS_PROFILE="..."; ...) and output redirections.
clean=$(printf '%s' "$cmd" | sed -E 's/^[[:space:]]*(\$env:[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=[[:space:]]*("[^"]*"|'"'"'[^'"'"']*'"'"')[[:space:]]*;[[:space:]]*)+//; s/[0-9*]?>&[0-9]//g; s/[*0-9]?>{1,2}[[:space:]]*("[^"]*"|[^[:space:]]+)//g')
if [ "$lines" -eq 1 ] &&
   ! printf '%s\n' "$clean" | grep -Eq '[;|#`]|&&|\$\(' &&
   [ "$(printf '%s\n' "$clean" | grep -Eo "${TF}apply" | wc -l)" -eq 1 ] &&
   printf '%s\n' "$clean" | grep -Eq "${TF}apply([[:space:]]+-[^[:space:]]+)*[[:space:]]+\"?[^-\"[:space:]][^\"[:space:]]*\.approved\.tfplan\"?[[:space:]]*$"; then
  exit 0
fi

echo "BLOCKED: terraform apply is only allowed for a saved plan file *.approved.tfplan (single command, no chaining), created by the orchestrating session after the user agreed to the plan (.kiro/steering/delegation-and-safety.md)." >&2
exit 2
