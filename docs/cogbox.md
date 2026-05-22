# Running a project inside cogbox

cogbox is a NixOS microvm sandbox that runs Claude Code (and other AI coding
agents) inside an isolated QEMU VM with permissions disabled and network
filtered. See https://github.com/illustris/cogbox.

**Requirements:** Nix with flakes enabled, Linux host with KVM.

## First-time setup

```sh
nix run github:illustris/cogbox
```

On first run it prompts which harnesses to enable (`claude-code`, `opencode`,
or both), shows what host paths will be created, and asks for confirmation.
Existing `~/.claude/` and `~/.claude.json` are picked up automatically.

## Claude config

cogbox automatically mounts `~/.claude/` and `~/.claude.json` from the host
into the VM as read-only lower layers. Your auth token and config are available
inside the VM without any extra steps.

If your configs live elsewhere, override before launching:

```sh
COGBOX_CLAUDE_CONFIG=/path/to/.claude \
COGBOX_CLAUDE_AUTH=/path/to/.claude.json \
nix run github:illustris/cogbox -- start
```

## Running qbit-filter (or any project) in the VM

### 1. Start the VM in the background

```sh
nix run github:illustris/cogbox -- start
```

### 2. Copy the project into the VM over SSH

```sh
rsync -avz -e "ssh -p 2222 -o StrictHostKeyChecking=no" \
  /home/hritwik/Projects/qbit-filter \
  root@127.0.0.1:/root/
```

### 3. SSH in and launch Claude

```sh
nix run github:illustris/cogbox -- ssh
# inside the VM:
cd /root/qbit-filter
c .
```

The `c` launcher already passes `--dangerously-skip-permissions` and sets
`IS_SANDBOX=1` — full automatic mode is built in, no extra flags needed.

### One-liner variant

```sh
nix run github:illustris/cogbox -- start && \
rsync -avz -e "ssh -p 2222 -o StrictHostKeyChecking=no" \
  /home/hritwik/Projects/qbit-filter root@127.0.0.1:/root/ && \
nix run github:illustris/cogbox -- ssh -- bash -c "cd /root/qbit-filter && c ."
```

## Lifecycle

```sh
nix run github:illustris/cogbox -- status   # check if running
nix run github:illustris/cogbox -- stop     # stop the VM
nix run github:illustris/cogbox -- ssh      # connect to running VM
```

## Named instances

Run multiple isolated VMs simultaneously:

```sh
nix run github:illustris/cogbox -- start --name work
nix run github:illustris/cogbox -- ssh --name work
nix run github:illustris/cogbox -- list     # show all instances + ports
```

Each instance gets its own SSH port (default starts at 2222, increments by 1).

## Resources

- vCPUs: 16, RAM: 32 GB (override with `--vcpu N --mem N`)
- Network: private/LAN blocked, public internet allowed (default `rules` mode)
- Writable nix store: 16 GB tmpfs (resets on reboot)
- SSH: `127.0.0.1:2222 -> guest:22`
