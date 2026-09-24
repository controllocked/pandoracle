# Pandora removable-drive OPSEC architecture

## Executive summary

Pandora is the Linux device layer around the path-based Pandoracle core. It turns a
dedicated removable drive into a portable workspace with a small public FAT32 area
and a LUKS2-protected private area. The core never sees block devices or encryption
credentials; after unlock it receives only a normal mounted POSIX path.

The design is OPSEC-first in the engineering sense: it declares a concrete threat
model, minimizes privileged code, separates credentials by role, avoids putting
secrets in command-line arguments or regular files, closes the device as a supervised
session, and documents residual host traces. It does not claim anonymity, deniability,
anti-forensics, or protection from a compromised running host.

## Security claim

The protected scenario is a powered-off Pandora drive acquired and copied by an
attacker who knows the source code and disk layout but possesses none of the enrolled
credentials.

In that scenario, LUKS2 protects the private partition containing:

- RAW source files;
- original-record and canonical Parquet artifacts;
- optional search accelerations;
- catalog metadata and stable record identities;
- operation journals, quarantine, and workspace-resident investigation material.

The public partition, partition layout, labels, approximate encrypted volume size,
and the fact that a LUKS2 volume exists are not confidential.

## Trust model

### Trusted while the workspace is open

- the active Linux kernel and root account;
- system firmware and the hardware execution environment;
- the installed Pandoracle code;
- root-owned `pkexec`, UDisks2, cryptsetup, and the packaged privileged helper;
- the user's active desktop session and Secret Service when automatic unlock is used;
- filesystem locking, atomic rename, `fsync`, and ext4 journal behavior.

### Explicit non-goals

- compromised root, kernel, firmware, or installed binaries;
- keyloggers, terminal capture, local D-Bus interception, process inspection, DMA,
  cold boot, swap, hibernation, or crash-report leakage;
- confidentiality while the private filesystem is unlocked;
- hidden device existence, hidden encrypted-volume size, or plausible deniability;
- authenticated storage against deliberate ciphertext corruption;
- guaranteed secure deletion on flash media;
- automatic filesystem repair after unsafe removal;
- safe concurrent writers from multiple hosts or cloned drives.

These boundaries are part of the claim, not footnotes. A diagram or product page
should present them beside the protection story.

## Physical layout

| Region | Format | Visibility | Contents | Security role |
| --- | --- | --- | --- | --- |
| GPT metadata | GPT | Public | Partition geometry and types | Standard portable layout |
| `PANDORA_PUB` | 64 MiB FAT32 | Public and unencrypted | Owner-supplied hierarchy plus opaque versioned marker | Interoperable public area |
| `PANDORA_PRIVATE` | LUKS2 container | Encrypted at rest | ext4 filesystem | Confidentiality boundary |
| Private root | ext4 inside LUKS2 | Available only while unlocked | Private binding descriptor and `workspace/` | Device identity and data workspace |

The public marker is stored at `.pandoracle-device/device.json`. It contains a
format version and an opaque random device ID. It contains no workspace ID, catalog
metadata, LUKS UUID, credential identifier, host/user identity, or usage history.
Hidden/System FAT attributes make the marker unobtrusive in ordinary Windows views;
they are cosmetic and never treated as access control.

Every owner-supplied public file is exactly that: public. Pandoracle does not assign
decoy, canary, recovery, evidentiary, or trusted semantics to it. Symbolic links and
special files are rejected because FAT32 cannot preserve their semantics.

## Credential compartmentalization

| LUKS2 slot | Credential | Generated or chosen by | Persistence | Intended use |
| --- | --- | --- | --- | --- |
| 0 | Main passphrase | User | Never persisted by Pandoracle | Normal manual ownership credential |
| 1 | 256-bit Base32 recovery credential | Cryptographic RNG | Displayed once; user stores it separately | Offline recovery independent of any host |
| 2–31 | Independent 256-bit host keys | Cryptographic RNG | One key in that host login's Secret Service | Optional automatic unlock and revocable host trust |

The roles never collapse into one reusable secret:

- the recovery credential is not derived from the main passphrase;
- a host key is not the main or recovery credential;
- each trusted host receives a distinct host key and credential ID;
- host configuration stores only device identifiers, policy, credential ID, and
  keyslot reference;
- the encrypted private descriptor stores slot roles and credential ID/keyslot
  pairs, not the credential itself or a host identity.

A Secret Service compromise exposes that host's key. That key is intentionally
sufficient to unlock the drive; “host-specific” limits credential reuse, not the
power of the credential.

## Privilege boundary

The regular CLI, virtual environment, source checkout, data core, and search engine
run as the invoking user. They are never elevated.

Destructive setup crosses the privilege boundary once through a separately packaged,
root-owned executable:

```text
/usr/libexec/pandoracle-device-provision
```

The helper is admitted only when it, its parent directory, `pkexec`, and every
system executable it invokes are root-owned and not group/world writable. A missing
or unsafe helper causes setup to fail closed; the client does not elevate or copy a
helper from a user-writable checkout.

The helper accepts a bounded, versioned protocol with only three operation families:

- provision one validated removable drive using the fixed layout;
- enroll one host credential into a declared non-reserved slot;
- remove one declared host credential after proving that credential belongs to the
  slot.

It has no arbitrary command, path, mount-option, partition recipe, or general
cryptsetup interface. Before destructive work, it revalidates device path, UDisks
object paths, serial, size, removability, caller UID, non-system status, and absence
from the live root-device chain. This closes the time-of-check/time-of-use gap between
unprivileged selection and privileged execution.

## Provisioning transaction

The intended setup sequence is:

1. Discover whole removable-drive candidates through UDisks2.
2. Present model, size, and exact erase target to the user.
3. Inspect an optional public-content source before elevation.
4. Collect the main passphrase twice without echo.
5. Generate recovery and optional host credentials in memory.
6. Start the fixed helper once through its dedicated interactive polkit action.
7. Revalidate the target under privilege.
8. Create GPT, public FAT32, private LUKS2/ext4, and reserved keyslots.
9. Return bounded device metadata and keep the helper transaction open.
10. Display the recovery credential exactly once and require acknowledgement.
11. If automatic unlock was requested, store its distinct host key in Secret Service.
12. Send `commit`, `prompt`, or `abort` to the helper.
13. Mount the private area as the invoking user, write binding descriptors, create the
    workspace, optionally publish public contents, sync, unmount, lock, and power off.

The helper remains alive until the client commits the outcome. EOF, client death, or
missing recovery acknowledgement causes rollback of undisclosed recovery and host
slots and closes the temporary mapping. If Secret Service enrollment fails, the new
host slot is rolled back while prompt-based unlock remains usable.

## Secret transport and memory handling

Secrets are excluded from:

- process arguments;
- environment variables;
- application logs;
- public storage;
- regular temporary files;
- Pandoracle configuration and device descriptors.

The unprivileged client and helper communicate through inherited anonymous pipes
using a bounded framed protocol. Inside the elevated helper, cryptsetup consumes
credentials through mode-`0600` FIFOs in a fresh mode-`0700` directory directly
below root-owned `/run`, never below the invoking user's runtime directory.

Mutable byte buffers are overwritten when practical. This reduces accidental
retention but is not a defense against privileged memory inspection, swap,
hibernation, core dumps, or a compromised process.

## Session lifecycle

### Open

1. Match the physical device against saved public UUID, LUKS UUID, and device ID.
2. Prefer an enrolled host key only when automatic unlock is explicitly enabled;
   otherwise prompt for a credential.
3. Mount the private filesystem.
4. Validate the encrypted private descriptor and ordinary Pandoracle workspace.
5. Run application recovery unconditionally.
6. Publish an ephemeral runtime pointer to the mounted workspace.
7. Start the existing Pandoracle shell in its own process group.
8. Monitor the whole drive, encrypted mapping, and cleartext mapping for removal.

Other CLI processes in the same login can resolve the ephemeral workspace pointer
without being given a device path or encryption credential.

### Clean close

1. Stop publishing the ephemeral workspace pointer.
2. Flush the filesystem with `syncfs`.
3. Request a non-forced unmount of the private filesystem.
4. Lock the LUKS2 mapping.
5. Unmount the public area if mounted.
6. Request safe power-off where supported.

Busy unmounts are never forced. The dedicated session reports same-user holder
processes where possible and retries while continuing to monitor physical removal.

### Physical removal

Exact UDisks2 disappearance or mapping-loss events clear runtime state and terminate
the supervised shell process group. SIGTERM is followed by SIGKILL after one second
if necessary. Operations that are no longer possible are skipped.

Removal during a write can still damage ext4. On the next open, ext4 journal replay,
workspace validation, and Pandoracle recovery reduce application-level inconsistency;
Pandoracle does not run automatic filesystem repair.

## Host persistence and revocation

Pandoracle does not maintain application-side query history and does not log query
values, result records, or imported record content. Direct command-line queries may
still enter shell history or appear briefly in process listings. Swap, hibernation,
terminal capture, desktop logs, Secret Service metadata, and crash reports remain
host-level considerations.

`device forget` removes local device policy and the Secret Service item. When the
drive is present and the key is available, it also removes the corresponding LUKS2
slot and encrypted descriptor entry. When the drive is absent, local trust is still
removed but an unusable orphan slot may remain; the command reports that condition.
Deletion from Secret Service does not constitute a secure-deletion claim for the
host's underlying storage.

## Compromise matrix

| Event | Confidentiality consequence | Recovery / containment |
| --- | --- | --- |
| Powered-off drive stolen; no credential compromised | Private area remains within the LUKS2 claim | Use separately stored recovery material only if the drive returns |
| Public partition copied | All public files and opaque marker are disclosed | No private secret should be present there |
| Main passphrase disclosed | Full drive unlock | Change the main credential through an audited recovery procedure |
| Recovery credential disclosed | Full drive unlock | Treat as owner-level compromise; recovery material must be rotated out of band |
| One host Secret Service compromised | That host key can unlock the drive | Remove host trust and its keyslot while the drive is present |
| Host forgotten while drive is absent | Local automatic unlock is removed; orphan slot may remain | Reconcile slots when the drive is available |
| Running host/root compromised | Data and credentials may be observable while active | Outside the at-rest threat model; rebuild trust on a clean host |
| Drive removed during write | Possible filesystem damage, not silent publication of partial versions | Journal replay, validate, recover; manual filesystem repair may be required |

## Operator checklist

### Before provisioning

- Verify that the selected drive is dedicated and expendable.
- Ensure the packaged helper is installed from a trusted system package.
- Decide where the recovery credential will be stored, separate from drive and host.
- Treat every file selected for `PANDORA_PUB` as intentionally public.

### During provisioning

- Verify model, size, and device identity at the destructive prompt.
- Use a strong unique main passphrase.
- Record the recovery credential before acknowledging it.
- Enable automatic unlock only on a host whose login and Secret Service are trusted.

### During use

- Prefer interactive search for sensitive query values; direct CLI values may enter
  shell history and process listings.
- Keep the host patched and control swap, hibernation, crash dumps, and terminal
  recording according to the operational environment.
- Close external processes holding files before ending the session.

### Before transport

- Exit the supervised shell cleanly.
- Confirm unmount, LUKS lock, and power-off completion.
- Do not rely on unplugging as the normal close mechanism.

## Diagram specification

Three complementary diagrams communicate the design best.

### Trust-boundary diagram

Show four zones: user session, root/system services, removable media, and the
unchanged Pandoracle core. Draw the one privileged transition through `pkexec` and
the fixed helper. Show credentials crossing anonymous pipes and helper-local FIFOs,
while the core receives only a mounted path.

### Credential topology

Show LUKS2 slots 0, 1, and 2–31 with separate arrows to user memory, offline recovery
storage, and per-host Secret Service instances. Explicitly show that host metadata
contains references, not secrets.

### Session state machine

Use states `detected → unlocking → mounted → validated → active → flushing →
unmounted → locked → powered off`, with failure branches for invalid identity, busy
unmount, physical removal, and recovery on next open.

## Implementation map

| Responsibility | Primary module |
| --- | --- |
| Device workflow and supervised session | `src/pandoracle/device_service.py` |
| Privileged protocol and helper validation | `src/pandoracle/device_privileged.py` |
| UDisks2 discovery, format, mount, lock, and monitoring | `src/pandoracle/device_udisks.py` |
| LUKS key generation and keyslot operations | `src/pandoracle/device_credentials.py` |
| Secret Service storage | `src/pandoracle/device_secrets.py` |
| Public hierarchy replacement | `src/pandoracle/device_public.py` |
| Public/private descriptors and host policy | `src/pandoracle/device_models.py` |

The normative threat model remains [SECURITY.md](../SECURITY.md), and the accepted
device decision is [ADR 0008](adr/0008-pandora-device-layer.md).
