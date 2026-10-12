# exo-panel — one panel to run your exo cluster

A single zero-dependency Python 3 script (`exoctl.py`) that gives you a web
control panel for the [exo](https://github.com/exo-explore/exo) distributed-AI
platform across all your Macs. No Docker, nothing to `pip install` — it runs on
the stock `/usr/bin/python3`.

> The project is **exo-panel**; the command you run is `python3 exoctl.py`.

## What it does
- **Cluster view** — every Mac in one table (status, launch method, downloaded
  model count, exo uptime). Start / Stop / Manage any machine from one screen.
- **Thunderbolt discovery** — scans the bridge (`bridge0`) and finds peer Macs
  on the link; one click to add one as a managed node, or copy a one-line
  bootstrap command for a Mac that isn't running the agent yet.
- **Install exo** — a prerequisite wizard that runs the steps it safely can on
  the selected machine (and downloads Xcode CLT, Homebrew & uv when missing),
  streaming output; steps that need a GUI or password show the exact command.
  Works on a peer too (its agent runs the install, the hub watches).
- **Per-machine aware** — auto-detects how exo launches on *each* host
  (`uv run`, a repo `.venv`, system `exo`, or `exo.app`) and remembers it.
- **Local models, no Hugging Face** — toggle offline mode and point exo at your
  downloaded model folders; wires `EXO_OFFLINE`, `EXO_DEFAULT_MODELS_DIR`,
  `EXO_MODELS_DIRS`, `EXO_MODELS_READ_ONLY_DIRS`; confirms against exo's own
  `/models?status=downloaded`.
- **Push config to all machines** — set offline + model dirs + options once and
  replicate them to every node (each machine keeps its own repo path & launch
  method).
- **Troubleshooter** — reads live log output (and a paste box for errors you hit
  in your own terminal) and gives you the exact command to fix it.
- **Add machines any time** — type a name + Thunderbolt IP; future Macs just get
  added.

## Run it

On each Mac you want in the cluster, bind the agent to that Mac's Thunderbolt IP:

```bash
# Mac mini (10.0.0.1)
python3 exoctl.py ui --host 10.0.0.1

# MacBook Air (10.0.0.2)
python3 exoctl.py ui --host 10.0.0.2
```

Then open the panel on whichever machine you're sitting at — it becomes the hub
and manages every registered node over the Thunderbolt link. The config already
has `mac-mini` (10.0.0.1) and `macbook-air` (10.0.0.2) registered.

Loopback-only (single machine, can't be managed by peers):

```bash
python3 exoctl.py            # binds 127.0.0.1:8577, opens the panel
```

### CLI (no UI)
```bash
python3 exoctl.py detect            # show how exo launches on this Mac
python3 exoctl.py status            # is exo up?
python3 exoctl.py diagnose err.log  # run the troubleshooter on a log file
```

## Security on the mesh
- The agent trusts the local browser (loopback) and this machine's own IPs.
- A **peer** machine calling in over the link is allowed only if either no token
  is set (fine on a private Thunderbolt link between your own Macs) **or** it
  presents the shared token. Set one token on every machine (Cluster → *Shared
  cluster token*) if the link is ever on a wider network.
- Bind to the Thunderbolt IP (`--host 10.0.0.x`), not `0.0.0.0`, so the agent is
  only reachable over the direct Mac-to-Mac link.

## Notes
- exo's API/dashboard runs on **52415**; the exoctl panel runs on **8577**.
- Config lives at `~/.config/exoctl/config.json` (per machine).
- exo's own offline support has had rough edges across releases; if offline mode
  still tries to reach Hugging Face, the troubleshooter will flag it — that's an
  exo limitation, not the panel's.

## License
MIT — see [LICENSE](LICENSE). Free to use, modify, and redistribute.

exoctl is an independent tool. It does not include any exo source code; it
drives [exo](https://github.com/exo-explore/exo) (Apache-2.0) as a separate
program over HTTP and the shell. "exo" is a trademark of its respective owners;
this project is not affiliated with or endorsed by them.
