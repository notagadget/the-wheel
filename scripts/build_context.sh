#!/bin/bash
# build_context.sh — Regenerate PROJECT_CONTEXT.md from source files
# Usage: scripts/build_context.sh

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="$REPO_ROOT/PROJECT_CONTEXT.md"

cat > "$OUTPUT" << 'EOF'
<!-- PROJECT_CONTEXT.md — generated file for Claude Project upload. Do not edit directly. -->
<!-- Source files: CLAUDE.md, docs/*.md, db/schema.sql -->
<!-- Regenerate with: scripts/build_context.sh -->
# WHEEL TRADER — PROJECT CONTEXT

> This file is the complete context for the Wheel Trader project. Read it in full before responding to any request.

---

<!-- SOURCE: CLAUDE.md -->

EOF

cat "$REPO_ROOT/CLAUDE.md" >> "$OUTPUT"

cat >> "$OUTPUT" << 'EOF'

---

<!-- SOURCE: docs/architecture.md -->

EOF

cat "$REPO_ROOT/docs/architecture.md" >> "$OUTPUT"

cat >> "$OUTPUT" << 'EOF'

---

<!-- SOURCE: docs/data-model.md -->

EOF

cat "$REPO_ROOT/docs/data-model.md" >> "$OUTPUT"

cat >> "$OUTPUT" << 'EOF'

---

<!-- SOURCE: docs/state-machine.md -->

EOF

cat "$REPO_ROOT/docs/state-machine.md" >> "$OUTPUT"

cat >> "$OUTPUT" << 'EOF'

---

<!-- SOURCE: docs/cost-basis-rules.md -->

EOF

cat "$REPO_ROOT/docs/cost-basis-rules.md" >> "$OUTPUT"

cat >> "$OUTPUT" << 'EOF'

---

<!-- SOURCE: docs/conventions.md -->

EOF

cat "$REPO_ROOT/docs/conventions.md" >> "$OUTPUT"

cat >> "$OUTPUT" << 'EOF'

---

<!-- SOURCE: db/schema.sql -->

## Schema (canonical DDL)

\`\`\`sql
EOF

cat "$REPO_ROOT/db/schema.sql" >> "$OUTPUT"

cat >> "$OUTPUT" << 'EOF'
\`\`\`

EOF

echo "✓ Generated $OUTPUT"
