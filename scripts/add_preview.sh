#!/usr/bin/env bash
# Put the preview page into _site/portfolio-lab-preview/, from the branch given, or else from the
# newest open pull request whose branch starts with claude/. With neither, the preview is removed,
# so it disappears on its own once a pull request is merged or closed.
set -euo pipefail
BR="${1:-}"
rm -rf _site/portfolio-lab-preview
if [ -z "$BR" ]; then
  BR=$(gh pr list --repo "$GITHUB_REPOSITORY" --state open --base main --json headRefName,updatedAt \
       --jq '[.[] | select(.headRefName | startswith("claude/"))] | sort_by(.updatedAt) | last | .headRefName // ""')
fi
if [ -z "$BR" ]; then echo "No open preview."; exit 0; fi
git fetch -q --depth=1 origin "$BR"
mkdir -p _site/portfolio-lab-preview
git show FETCH_HEAD:site/portfolio-lab/index.html > "$RUNNER_TEMP/preview-src.html"
python3 scripts/make_preview.py "$RUNNER_TEMP/preview-src.html" _site/portfolio-lab-preview/index.html \
  "$(git rev-parse --short FETCH_HEAD)"
echo "Preview from $BR at $(git rev-parse --short FETCH_HEAD)."
