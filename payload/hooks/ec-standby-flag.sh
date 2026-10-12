#!/bin/bash
# Con acpi_x86.sleep_no_lps0=1 il kernel non chiama il _DSM LPS0, quindi la
# funzione 5 ("[PEP] deep standby entry") non scrive PECF=1 nell'EC e l'EC non
# sa che l'host dorme (ventole dopo 2-5 min, tasto power che va a vuoto).
# Qui scriviamo solo quel bit: EC RAM offset 0x57, bit 0. Uscita: bit a 0,
# come fa la funzione 6 ("[PEP] deep standby exit").
EC=/sys/kernel/debug/ec/ec0/io
OFF=$((0x57))
BIT=0x01

[ -w "$EC" ] && [ "$(cat /sys/module/ec_sys/parameters/write_support 2>/dev/null)" = Y ] \
    || modprobe ec_sys write_support=1 || exit 0

rd() { dd if="$EC" bs=1 skip=$OFF count=1 2>/dev/null | od -An -tu1 | tr -d ' '; }
wr() { printf "\\$(printf '%03o' "$1")" | dd of="$EC" bs=1 seek=$OFF count=1 conv=notrunc 2>/dev/null; }

keys_black() {
    /usr/local/bin/omen-fx --json status 2>/dev/null | python3 -c '
import json, sys
s = json.load(sys.stdin)["status"]
sys.exit(0 if s["active"]["keys"] == "suspend" and not s["fading"]["keys"] else 1)'
}

case "$1" in
    # keyboard-fix.sh gira in parallelo e accoda il nero su omen-fx (con una
    # transizione): scrivere PECF prima che sia finita lascia la tastiera accesa.
    # Aspettiamo che omen-fxd confermi l'effetto "suspend" sui tasti senza
    # dissolvenze in corso (max 2 s, poi si va avanti comunque), più un margine
    # per la rampa del firmware della tastiera.
    pre)  t0=$(date +%s%N); ok=no
          for _ in $(seq 20); do keys_black && { ok=yes; break; }; sleep 0.1; done
          extra=" (nero confermato: $ok dopo $(( ($(date +%s%N) - t0) / 1000000 )) ms)"
          sleep 0.5
          v=$(rd); [ -n "$v" ] && wr $(( v | BIT ))  ;;
    post) v=$(rd); [ -n "$v" ] && wr $(( v & ~BIT & 0xff )) ;;
esac
logger -t ec-standby-flag "$1: 0x57 $(printf '0x%02x' "$v") -> $(printf '0x%02x' "$(rd)")$extra"
exit 0
