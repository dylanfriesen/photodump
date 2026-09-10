#!/usr/bin/env bash
# Install the repo's git hooks. .git/hooks is not versioned, so a fresh clone
# has none - run this once after cloning.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .git/hooks
cp tools/hooks/post-commit .git/hooks/post-commit
chmod +x .git/hooks/post-commit
echo "installed: .git/hooks/post-commit (appends each commit to PROGRESS.md)"
