#!/usr/bin/env bash
# Create the local files a fresh clone needs, from the checked-in templates.
#
# config.json, .claude/settings.json and state/ are gitignored: they are the
# live operating configuration and runtime state of whoever runs this, and this
# repository publishes the framework, never a portfolio. This script creates
# them from the templates that ARE checked in.
#
# It never overwrites an existing file.
set -euo pipefail
cd "$(dirname "$0")/.."

created=0
copy_if_absent() {
  if [ -e "$2" ]; then
    echo "  exists, left alone : $2"
  else
    cp "$1" "$2"
    echo "  created            : $2"
    created=$((created + 1))
  fi
}

echo "Bootstrapping local configuration and state:"
mkdir -p state logs
copy_if_absent config.example.json config.json
copy_if_absent .claude/settings.example.json .claude/settings.json
copy_if_absent state/budget.example.json state/budget.json

echo
echo "Created $created file(s). Execution ships disabled:"
python3 -c "import json;c=json.load(open('config.json'));print('  execution_mode=%s agent_enabled=%s live_trading=%s' % (c['execution_mode'], c['agent_enabled'], c['live_trading']))"
echo
echo "Next:  python3 -m unittest discover -s tests -t ."
