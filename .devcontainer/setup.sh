#!/bin/bash
# Dev box for PLE runs: kit at the pinned SHA, uv, harbor. Task data is downloaded per run (never committed).
set -e
sudo apt-get update -qq && sudo apt-get install -y -qq python3 >/dev/null
curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
export PATH=$HOME/.local/bin:$PATH
mkdir -p $HOME/ple && cd $HOME/ple
[ -d kit-src ] || (git init -q kit-src && cd kit-src && git remote add origin https://github.com/google-research/ple && git fetch -q --depth 1 origin 8320f0402fdca9f42476607f72bcfd5271cd50c9 && git checkout -q FETCH_HEAD)
uv tool install -q harbor==0.20.0 --with websockets
# NOTE: keep docker on its default /var/lib/docker: /tmp carries ACLs that break the kit egress container (runs as uid 65534)
