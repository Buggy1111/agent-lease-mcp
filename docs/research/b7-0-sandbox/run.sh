#!/bin/sh
# Vnější: vlastní user+net namespace, jen loopback (testy s lokálními sockety), žádná cesta ven.
ip link set lo up 2>/dev/null
exec bwrap --unshare-all --die-with-parent --new-session --clearenv \
  --setenv PATH /usr/bin:/bin --setenv HOME /tmp/home --setenv TMPDIR /tmp \
  --setenv PYTHONPATH /work/src:/tools/site:/tools/testdeps --setenv PYTHONDONTWRITEBYTECODE 1 \
  --setenv COVERAGE_FILE /out/.coverage \
  --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --proc /proc --dev /dev --tmpfs /tmp --dir /tmp/home \
  --ro-bind "$R/python" /tools/python --ro-bind "$R/venv/lib/python3.13/site-packages" /tools/site \
  --ro-bind "$TD" /tools/testdeps \
  --ro-bind "$SNAP" /work --bind "$OUT" /out --chdir /work \
  prlimit --fsize=52428800 --nproc=256 -- "$@"
