# System device helper packaging

Linux distribution packages that enable Pandora device provisioning install:

- `pandoracle-device-provision` as
  `/usr/libexec/pandoracle-device-provision`, mode `0755`, owned by root;
- `io.pandoracle.device-provision.policy` under
  `/usr/share/polkit-1/actions`, mode `0644`, owned by root;
- the `pandoracle` Python package used by that helper under a root-owned system
  Python path, with no group- or world-writable path component.

The ordinary `pandoracle` client may remain in a user virtual environment. It
executes the system helper once through `pkexec`; it never installs or updates the
helper at runtime. A missing or unsafe helper makes destructive setup fail closed.
