# ADR 0008: Pandora removable-device layer and independent LUKS credentials

Status: accepted

## Context

The data core intentionally accepts only a normal POSIX workspace path. A portable
private workspace nevertheless needs a safe, low-friction Linux device lifecycle,
including discovery, provisioning, unlock, recovery, supervised use, and close.
Owner, recovery, and unattended host access must not collapse into one credential.

## Decision

The Linux-only Pandora device layer surrounds the unchanged core. It uses UDisks2
and polkit for block-device discovery, a GPT with a 64 MiB FAT32 `PANDORA_PUB`
partition, a LUKS2/ext4 `PANDORA_PRIVATE` partition, mounting, locking, and safe
power-off. The user-visible contents of the public partition are a byte-for-byte file
and directory hierarchy supplied from one local directory, plus a versioned opaque
device marker managed by Pandoracle. No public file has application-defined decoy,
canary, recovery, or document semantics. Pandoracle gives the marker directory the
standard FAT Hidden and System attributes so normal Windows Explorer views show only
the user-supplied hierarchy; this is cosmetic and the marker remains readable when
hidden files are shown. Symbolic links and special files are rejected because FAT32
cannot preserve their semantics. The private partition contains a binding descriptor
and an ordinary Pandoracle workspace at `workspace/`.

Setup may populate the public hierarchy immediately. `device public DIRECTORY`
subsequently replaces the hierarchy as a unit while preserving and revalidating the
opaque marker. New files are copied to a staging directory before the existing
user-visible hierarchy is removed, so a source read or copy failure leaves the old
hierarchy in place. The final FAT32 replacement is not claimed to be atomic across
power loss.

LUKS2 keyslots have fixed roles:

- slot 0 is the user-selected main passphrase;
- slot 1 is a generated 256-bit Base32 recovery credential shown once;
- slots 2 through 31 are generated 256-bit, independently enrolled host keys.

Pandoracle never persists the owner or recovery credential. A host key is stored
only by that login's FreeDesktop Secret Service. Host configuration stores device
UUID expectations, opening policy, and a credential ID/keyslot reference, but no
secret. The encrypted descriptor stores slot roles and credential ID/keyslot pairs,
but no host identity or secret. `forget` removes local policy and the Secret Service
item even when the drive is absent; in that case it reports the possible orphaned,
now-unusable device keyslot.

Destructive setup is one privileged transaction. A small, separately installed,
root-owned helper at `/usr/libexec/pandoracle-device-provision` is started once via
`pkexec` under its own polkit action. The distribution package installs the helper;
the user-writable client never copies, updates, or selects its executable. The helper
accepts only fixed provision, host-key enrollment, and host-key removal operations.
For provisioning it revalidates one whole removable block device's serial, size,
removability, non-system status, and absence from the live root-device chain, then
performs the fixed UDisks2 partition/format sequence and fixed cryptsetup slot
enrollment. It has no shell-command, arbitrary-path, mount-option, or generic
cryptsetup interface. Pandoracle and the data core continue running as the invoking
user.

The helper remains alive until the user has acknowledged the displayed recovery
credential and, when requested, the unprivileged client has stored the host key in
Secret Service. A small bounded, versioned transaction channel over inherited
anonymous pipes carries Base64-framed credentials and a final commit/rollback result.
It is an operation protocol, not an authentication protocol: LUKS remains the sole
credential verifier. EOF or client death rolls back undisclosed recovery and host
slots and closes the temporary mapping. Secrets never enter arguments, environment
variables, logs, regular temporary files, or public storage. A Secret Service failure
rolls back only the new host slot before committing prompt mode.

Inside the already-elevated helper, cryptsetup receives its credential inputs through
separate FIFOs in a fresh mode-0700 directory directly below root-owned `/run`. The
helper never uses the invoking user's `$XDG_RUNTIME_DIR` for privileged FIFO nodes.

The helper executable, its parent directory, `pkexec`, and every system executable it
invokes are required to be root-owned and not group/world writable. Setup refuses to
fall back to elevating a venv, source checkout, shell, or other user-writable Python
program. The helper's polkit policy requires one explicit active-session administrator
authentication per provisioning transaction; no passwordless or blanket UDisks rule
is installed. Keyslot changes after setup use one separately authorized fixed helper
transaction each. Pandoracle does not implement a custom cryptographic protocol, KDF,
LUKS token, or encrypted credential file.

`pandoracle device open` is the device-session supervisor. It mounts, validates with
`Workspace.open()`, always runs application recovery, publishes ephemeral runtime
selection, and starts the existing shell in its own process group. Normal shell exit
clears runtime state, calls `syncfs`, requests a non-forced unmount, locks, and powers
off. Ordinary CLI processes started while that runtime selection exists resolve the
same workspace path through the unchanged core CLI boundary. A busy close is never
forced: the supervisor reports known same-user holders and retries automatically
while continuing to watch the physical drive. Exact UDisks2
removal/mapping-loss events clear runtime state and terminate that process group,
using SIGKILL after one second if SIGTERM does not work. Disk operations that are no
longer possible are skipped.

The per-user XDG autostart watcher launches a dedicated `Terminal=true` desktop
session with no hold behavior for trusted devices whose auto-open policy is enabled.
It does not alter manually launched terminals.

## Consequences

- Core code and all existing `--workspace` automation remain path-based and
  privilege-free.
- Secret Service compromise exposes only that host's key, but that key is sufficient
  to unlock the drive.
- Physical removal during writes can damage ext4. Journal replay plus workspace
  validation and recovery limit application-level inconsistency; filesystem repair is
  never attempted automatically.
- Setup is deliberately destructive and interactive. Recovery enrollment must be
  acknowledged or slot 1 is removed and setup aborts.
- A complete setup presents at most one administrator authentication dialog. Package
  installation may independently require administrator authorization; that is not
  deferred into the first device setup.
- GNOME and KDE desktop launch behavior remains a manual release acceptance check.

## Dependency impact

The Linux dependency is `SecretStorage` 3.5, the mature binding for the standard
Secret Service API. Direct UDisks2 calls use its already-required `jeepney` 0.9
transport, declared explicitly because Pandoracle imports it itself. Implementing
either protocol locally was rejected as more security-sensitive code to maintain.

On the 2026-09-09 development environment (Linux x86-64, Python 3.14.6), installed
module files occupied about 0.08 MB for SecretStorage, 0.37 MB for Jeepney, and
15.60 MB for SecretStorage's `cryptography` dependency. Across 31 fresh processes
with a warm page cache, median/p95 startup was 10.65/15.64 ms for the interpreter,
21.85/27.84 ms after importing Jeepney, and 33.80/37.67 ms after importing
SecretStorage. This is a local dependency-overhead check, not the documented 1 GB
dataset acceptance benchmark.

## References

- [UDisks2 Block formatting API](https://storaged.org/doc/udisks2-api/latest/gdbus-org.freedesktop.UDisks2.Block.html)
- [UDisks2 partition-table API](https://storaged.org/doc/udisks2-api/latest/gdbus-org.freedesktop.UDisks2.PartitionTable.html)
- [cryptsetup LUKS key addition](https://gitlab.com/cryptsetup/cryptsetup/-/blob/master/man/cryptsetup-luksAddKey.8.adoc)
- [Secret Service specification](https://specifications.freedesktop.org/secret-service/latest-single/)
