#!/bin/bash

OMEN_FX=/usr/local/bin/omen-fx
BAT=/sys/class/power_supply/BAT0

# NOTA (03/09/2026): qui NON si parla mai col systemd dell'utente.
# Durante gli hook "post" systemd tiene user.slice CONGELATA e la scongela solo
# dopo che gli hook sono terminati: qualsiasi `systemctl --user` da qui si
# blocca in attesa dello scongelamento -> stallo circolare, risolto solo dal
# timeout di 90 s di systemd-sleep (risvegli da 90 s invece di 4).
#
# COROLLARIO IMPORTANTE: la durata di questo hook E' la latenza di sblocco
# percepita, perche' kwin sta in user.slice e resta congelato fino alla fine.
# Quindi qui dentro va SOLO cio' che deve essere pronto prima che l'utente
# tocchi la tastiera; tutto il resto sta in omen-fx-notifyd-resume.service,
# che gira dopo il thaw e non costa nulla. Niente `sleep` a occhio: si aspetta
# la condizione vera, con un tetto massimo.

# Attende che un path compaia / sparisca. Ritorna 1 al timeout (in secondi).
wait_for()  { local p="$1" t="${2:-3}" n=0; while [ ! -e "$p" ]; do [ "$n" -ge $((t*50)) ] && return 1; sleep 0.02; n=$((n+1)); done; }
wait_gone() { local p="$1" t="${2:-3}" n=0; while [ -e "$p" ];   do [ "$n" -ge $((t*50)) ] && return 1; sleep 0.02; n=$((n+1)); done; }

# Traccia batteria e residency S0ix ai due estremi del sonno, per poter
# misurare a posteriori il consumo reale (vedi ~/.local/bin/sleep-drain).
log_power() {
    logger -t sleep-power "$1" \
        "charge_uAh=$(cat $BAT/charge_now 2>/dev/null)" \
        "volt_uV=$(cat $BAT/voltage_now 2>/dev/null)" \
        "status=$(cat $BAT/status 2>/dev/null)" \
        "hw_sleep_us=$(cat /sys/power/suspend_stats/total_hw_sleep 2>/dev/null)"
}

case "$1" in
    pre)
        log_power pre

        # Con acpi_x86.sleep_no_lps0=1 il kernel non annuncia piu' al firmware
        # l'ingresso in Modern Standby (e' cio' che impedisce all'MCU di cedere
        # F2/F3/F4 all'host). Effetto collaterale: l'EC non riceve SCREEN_OFF e
        # lascia la tastiera illuminata per tutta la sospensione. Lo spegnimento
        # lo facciamo noi, e passando da omen-fx invece che scrivendo sull'MCU:
        # e' lui il proprietario di quei LED, quindi niente conflitti.
        # Il ripristino non serve aggiungerlo: la fase "post" chiama gia'
        # `omen-fx stop`, che scioglie l'effetto trattenuto e rimette la base.
        [ -x "$OMEN_FX" ] && "$OMEN_FX" fx solid --color 000000 --target keys \
            --hold --key suspend >/dev/null 2>&1
        # Smonta il driver del controller USB-C per evitare i risvegli fantasma (s2idle bug)
        modprobe -r ucsi_acpi 2>/dev/null
        ;;
    post)
        log_power post

        # Ferma qualsiasi animazione in corso: omen-fxd gira in system.slice,
        # non e' congelato, e scriverebbe sull'HID proprio mentre lo resettiamo.
        [ -x "$OMEN_FX" ] && "$OMEN_FX" stop >/dev/null 2>&1

        # Respiro all'EC/USB prima di toccare l'i8042.
        # Erano 2 s, messi quando la tastiera si rompeva al risveglio e il
        # prezzo del fallimento era un power cycle. Quella rottura ora e'
        # eliminata alla radice (acpi_x86.sleep_no_lps0=1: il kernel non
        # annuncia piu' al firmware l'ingresso in Modern Standby, e l'MCU non
        # cede piu' F2/F3/F4 all'host). Restava solo il respiro per l'i8042,
        # che non ha bisogno di due secondi. 2.0 -> 0.3 s.
        sleep 0.3

        # Ricarica il driver USB-C
        modprobe ucsi_acpi 2>/dev/null

        # 1. Reset hardware della tastiera INTERNA
        if [ -e /sys/bus/serio/drivers/atkbd/serio0 ]; then
            echo -n "serio0" > /sys/bus/serio/drivers/atkbd/unbind 2>/dev/null
            wait_gone /sys/bus/serio/drivers/atkbd/serio0 2
            sleep 0.1
        fi
        echo -n "serio0" > /sys/bus/serio/drivers/atkbd/bind 2>/dev/null
        wait_for /sys/bus/serio/drivers/atkbd/serio0 3

        # 2. Reset hardware della tastiera ESTERNA (porta USB 3-9), solo se
        #    e' davvero collegata: senza questa guardia, da staccata si
        #    aspetterebbe invano il timeout pieno.

        # 3. Aggiorna il server grafico (Wayland/Libinput)
        udevadm trigger --subsystem-match=input --action=change

        # Da qui in poi e' tutto estetico (attesa che omen-fxd riagganci il
        # LampArray + stop difensivo): non blocca lo sblocco, sta nella unit.
        ;;
esac
