#!/usr/bin/env bash
# Install the third-party Claude Code skills this project recommends (see AGENTS.md).
#
#   tools/install-skills.sh
#
# They are not committed to the repo; this script downloads each one at a pinned version and
# refuses to install anything whose checksum doesn't match the copy that was reviewed.
#   - frontend-design, webapp-testing   Anthropic (Apache-2.0)  github.com/anthropics/skills
#   - frontend-ui-engineering            Addy Osmani (MIT)        github.com/addyosmani/agent-skills
#   - modern-web-guidance                Google Chrome (Apache-2.0, npm package, ~17 MB, works offline)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
skills="$root/.claude/skills"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

fetch() {  # fetch <url> <sha256> <destination>
  curl -fsSL "$1" -o "$work/file"
  echo "$2  $work/file" | sha256sum -c --quiet - || { echo "Checksum mismatch for $1; not installing." >&2; exit 1; }
  mkdir -p "$(dirname "$3")"
  cp "$work/file" "$3"
}

A="https://raw.githubusercontent.com/anthropics/skills/e4bb5d960d87126ff3b3a8af1a104b7db67560ba/skills"
O="https://raw.githubusercontent.com/addyosmani/agent-skills/39b723ceebf81474d0557c1fecd01eddab585563"

echo "Installing frontend-design"
fetch "$A/frontend-design/SKILL.md"   d91970639e9f5c37682ac7ab60094d35f1c7c1f38d731bd56396563aee10c1d3 "$skills/frontend-design/SKILL.md"
fetch "$A/frontend-design/LICENSE.txt" 0d542e0c8804e39aa7f37eb00da5a762149dc682d7829451287e11b938e94594 "$skills/frontend-design/LICENSE.txt"

echo "Installing webapp-testing"
fetch "$A/webapp-testing/SKILL.md"    51b7349e77ec63b7744a6f63647e7566a0b4d2e301121cc10e8c2113af6556a2 "$skills/webapp-testing/SKILL.md"
fetch "$A/webapp-testing/LICENSE.txt" bc6b3af2f331cbc7fb0da1344efb2cbe5877a31498b4d70dbc7000f3405a1362 "$skills/webapp-testing/LICENSE.txt"
fetch "$A/webapp-testing/scripts/with_server.py" b0dcf4918935b795f4eda9821579b9902119235ff4447f687a30286e7d0925fd "$skills/webapp-testing/scripts/with_server.py"
fetch "$A/webapp-testing/examples/console_logging.py" ea46877289acb82da7e7ce59d0bc37c8977cd57e2a006d0c88d7a1c625bf95da "$skills/webapp-testing/examples/console_logging.py"
fetch "$A/webapp-testing/examples/element_discovery.py" d63c89604a22f8845d724e95dda45db49b1bf57c25ce0a83afbb7b8da3d402f0 "$skills/webapp-testing/examples/element_discovery.py"
fetch "$A/webapp-testing/examples/static_html_automation.py" 9d533aafb875ee3ab8b8ebf8f5b9003ac8d999da3d09b285cce252e623140064 "$skills/webapp-testing/examples/static_html_automation.py"

echo "Installing frontend-ui-engineering"
fetch "$O/skills/frontend-ui-engineering/SKILL.md" 5c322ec445ed5a2094617c8c01b4b5b138aaf73fccf5c89ec95081fe4e50923a "$skills/frontend-ui-engineering/SKILL.md"
fetch "$O/LICENSE" 6f202f8bd568cd730dbb2b0d1f8e243bc74c2fa1f64dbce9b2c7ea08bd5c9fd7 "$skills/frontend-ui-engineering/LICENSE"
fetch "$O/references/accessibility-checklist.md" bf6448af1a98833dcd5974ca4a823aef3c5d99873446fdfe9e64020d3fb8cd2f "$skills/frontend-ui-engineering/references/accessibility-checklist.md"
# The upstream skill links to ../../references/; point it at the copy bundled alongside it.
sed -i 's#`../../references/accessibility-checklist.md`#`references/accessibility-checklist.md` (bundled with this skill)#' "$skills/frontend-ui-engineering/SKILL.md"

echo "Installing modern-web-guidance 1.0.1"
MWG_INTEGRITY="sha512-5ztUh5F1p2PewnSkA6EhsGUXqomaZY9qe6iWJCU4JbIQxHTky7s5lMJcPE4EbD1uhbQZIhl0HROH0+s6fIiGzg=="
curl -fsSL "https://registry.npmjs.org/modern-web-guidance/-/modern-web-guidance-1.0.1.tgz" -o "$work/mwg.tgz"
actual="sha512-$(openssl dgst -sha512 -binary "$work/mwg.tgz" | base64 -w0)"
[[ "$actual" == "$MWG_INTEGRITY" ]] || { echo "Integrity check failed for modern-web-guidance; not installing." >&2; exit 1; }
mkdir "$work/mwg"
tar -xzf "$work/mwg.tgz" -C "$work/mwg"
rm -rf "$skills/modern-web-guidance"
cp -r "$work/mwg/package/skills/modern-web-guidance" "$skills/modern-web-guidance"
cp "$work/mwg/package/LICENSE" "$skills/modern-web-guidance/LICENSE"

echo "Done. Skills are in $skills (restart Claude Code to pick them up)."
echo "modern-web-guidance works offline: node .claude/skills/modern-web-guidance/modern-web.mjs search \"<query>\""
