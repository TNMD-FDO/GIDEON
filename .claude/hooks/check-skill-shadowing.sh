#!/usr/bin/env bash
# SessionStart hook, shared through .claude/settings.json so every clone runs it.
#
# Two drift vectors this repo cannot see from git alone:
#   1. A personal skill under ~/.claude/skills/ with the same name as a repo
#      skill wins over the repo's (Claude Code: personal overrides project).
#   2. `npx skills add` writes lowercase directory names (trip-1-plan), leaving
#      a second copy beside the frontmatter-cased one (TRIP-1-plan).
# Both are reported on stdout (added to the session's context) and stderr.
# The hook never blocks a session: it always exits 0.  See docs/agents/tooling.md.
set -u

cat >/dev/null 2>&1 || true   # consume the hook's JSON input

project="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
repo_skills="$project/.claude/skills"
personal_skills="${CLAUDE_PERSONAL_SKILLS_DIR:-$HOME/.claude/skills}"

[ -d "$repo_skills" ] || exit 0

warn() {
    printf 'WARNING %s\n' "$1"
    printf 'WARNING %s\n' "$1" >&2
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# 1. Personal skills that shadow a repo skill (exact or case-insensitive name).
if [ -d "$personal_skills" ]; then
    for p in "$personal_skills"/*/; do
        [ -d "$p" ] || continue
        pname="$(basename "$p")"
        for r in "$repo_skills"/*/; do
            [ -d "$r" ] || continue
            rname="$(basename "$r")"
            if [ "$(lower "$pname")" = "$(lower "$rname")" ]; then
                warn "skill shadowing: the personal skill ~/.claude/skills/$pname overrides this repo's .claude/skills/$rname (personal wins by name). Rename or remove the personal one; see docs/agents/tooling.md."
            fi
        done
    done
fi

# 2. Repo skill directories that differ only by case.
for r in "$repo_skills"/*/; do [ -d "$r" ] && basename "$r"; done \
    | tr '[:upper:]' '[:lower:]' | sort | uniq -d \
    | while IFS= read -r dup; do
        warn "skill duplicates: .claude/skills holds more than one directory named '$dup' ignoring case (an npx skills run leaves a lowercase copy). Keep the frontmatter-cased one; see docs/agents/tooling.md."
    done

exit 0
