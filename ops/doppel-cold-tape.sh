#!/bin/sh
set -eu

[ "$#" -eq 0 ] || exit 2
storage_dir=/opt/doppel-storage
rc=0
for tape_class in tape tape-rust; do
    /usr/bin/python3 "$storage_dir/archive_tape.py" --policy "$storage_dir/storage-policy.toml" \
        --storage-class "$tape_class" --state /var/lib/doppel-cold || rc=1
done
exit "$rc"
