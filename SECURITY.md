# Security policy and threat model

## Current security status

The Pandoracle data core is unencrypted: a workspace in an ordinary directory has
**no confidentiality at rest**. The optional Linux Pandora device layer provisions
and supervises a workspace inside a LUKS2 filesystem while still passing the core a
normal mounted POSIX path. It uses UDisks2, polkit, cryptsetup, and FreeDesktop Secret
Service rather than custom cryptography.

## Encrypted-storage claim

For a closed LUKS2-backed Pandora drive, the protected scenario is an attacker who
obtains a powered-off drive, copies it, knows Pandoracle's source code and disk
layout, but lacks every enrolled passphrase or recovery secret.

The intended protected assets are RAW sources, original records, canonical sidecars, indexes,
catalog metadata, entities, and workspace-resident investigation artifacts.

## Trust assumptions

- The active Linux kernel, root account, firmware, and installed Pandoracle code
  are trusted.
- There is one writer process per physical workspace.
- The user stores the recovery credential separately from both workspace and host.
- The local filesystem implements file locking, atomic rename, and durable
  `fsync` with reasonable correctness.

## Non-goals

- A compromised root/kernel, keylogger, malicious firmware, DMA, or cold boot.
- Confidentiality while a workspace is unlocked.
- Hiding encrypted-volume existence or approximate size.
- Secure deletion guarantees on flash media.
- Authenticated storage or protection from deliberate ciphertext corruption.
- Zero host traces: swap, hibernation, crash reports, terminal capture, and
  privileged memory inspection remain possible.
- Concurrent read/write operation from multiple hosts or cloned drives.

## Host persistence behavior

Pandoracle does not log queries, results, or record values. The interactive
shell has no application-side history. Direct CLI queries may still be stored by
the user's shell and may briefly appear in process listings. Temporary workspace
artifacts remain under the workspace root.

The main unlock passphrase is selected by the user and never persisted by
Pandoracle. Setup generates a 256-bit recovery credential, enrolls it in LUKS2 slot
1, shows it exactly once after successful enrollment, and requires acknowledgement.
Pandoracle cannot redisplay it. Neither value is written to configuration, device
descriptors, public files, logs, temporary regular files, or Secret Service.

Automatic unlock is an explicit opt-in. It generates a separate random credential
for each trusted host, enrolls it in a LUKS2 slot from 2 through 31, and stores only
that host credential in the login's FreeDesktop Secret Service. Compromise of Secret
Service reveals only that host credential, but it is fully sufficient to unlock the
device. Deleting its item does not prove secure deletion from host storage. If the
drive is absent during `device forget`, its secret is deleted and an unusable orphan
keyslot may remain on the drive.

Passphrases necessarily exist transiently in process memory and, for UDisks2
operations, the local D-Bus path. Mutable application buffers are overwritten where
practical, but Pandoracle does not claim protection against process inspection,
swap, hibernation, terminal capture, a compromised login session, or local D-Bus.

Destructive provisioning uses one root-owned, separately packaged helper under a
dedicated interactive polkit action. The helper implements only the fixed removable
drive provisioning transaction and fixed LUKS keyslot changes. It revalidates the
whole device after elevation and refuses the live root-device chain. Neither the
Pandoracle client, its virtual environment, the source checkout, nor the data core is
executed as root. Pandoracle does not install passwordless polkit rules. Credentials
cross this boundary only through inherited anonymous pipes and remain subject to the
same process-memory limitations described above.

The public FAT partition contains an owner-supplied file hierarchy and an opaque,
versioned device marker. Every user-supplied file there is public and unencrypted;
Pandoracle neither interprets its purpose nor treats decoys, recovery documents,
Canarytokens, QR codes, or other content specially. The marker contains no workspace
ID, catalog metadata, LUKS UUID, credential identifier, host/user identity, or usage
information. Its directory uses FAT Hidden/System attributes for a cleaner default
view on Windows. Those attributes are cosmetic, not an access-control boundary:
users who show protected files can still read the non-sensitive marker.

Normal shell exit flushes the filesystem, requests a non-forced unmount, locks the
mapping, and safely powers off where supported. A busy unmount is not forced.
The supervisor stops publishing the device workspace before close, waits and retries
while an already-running process still holds it, and continues monitoring physical
removal throughout that wait.
Physical removal immediately terminates the supervised shell, but removal during a
write can still damage the filesystem. ext4 journal replay, catalog-last
publication, SQLite durability, validation, and unconditional recovery on the next
open reduce application-level inconsistency; Pandoracle never runs automatic
filesystem repair.

## Reporting vulnerabilities

Do not include real datasets, secrets, recovery material, or sensitive query
results in a report. Provide a minimal synthetic reproducer and the affected
version through [GitHub private vulnerability reporting](https://github.com/controllocked/pandoracle/security/advisories/new).
