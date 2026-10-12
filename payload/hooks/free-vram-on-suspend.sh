#!/bin/sh
# Libera la VRAM prima di sospendere.
#
# Perche': con NVreg_PreserveVideoMemoryAllocations=1 il driver NVIDIA deve
# evacuare tutta la VRAM allocata a ogni sospensione. Con un modello caricato
# (~10 GiB) il sistema resta fermo nella fase PM_SUSPEND_PREPARE per ~25 s:
# schermo nero, niente risponde, e un evento di wakeup che arrivi in quella
# finestra fa abortire la sospensione. Senza modello la stessa fase dura ~5 s.
# Misurato: 24,5 s con il modello, 5,4 s senza.
#
# Copre i due modi in cui questa macchina tiene un modello in VRAM:
#   - llama-server avviato da ~/.local/lib/llm-serve.sh (comandi `ai`, `ai-server`)
#   - ollama.service
#
# llama-server non viene riavviato al risveglio: `ai` chiama ls_ensure_model,
# che lo riavvia da solo al primo uso. ollama invece si', perche' e' un servizio
# di sistema che ci si aspetta sia sempre su.
#
# Installazione (systemd legge SOLO questa directory, non /etc/systemd/system-sleep):
#   sudo install -Dm755 free-vram-on-suspend.sh \
#        /usr/lib/systemd/system-sleep/free-vram-on-suspend.sh
#
# systemd esegue *ogni* file eseguibile in quella directory, in parallelo:
# non lasciarci copie di backup con il bit di esecuzione.

PIDFILE=/home/mircogiorgi/.local/state/llm-serve/llama-server.pid
FLAG=/run/free-vram-on-suspend.ollama-stopped

log() { logger -t free-vram-on-suspend "$*"; }

# Aspetta che il pid esca: e' l'uscita del processo che rilascia la VRAM,
# non il segnale. Dopo 10 s passa a SIGKILL.
wait_exit() {
    i=0
    while kill -0 "$1" 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -gt 100 ]; then
            kill -9 "$1" 2>/dev/null
            sleep 1
            return
        fi
        sleep 0.1
    done
}

stop_llama_server() {
    [ -f "$PIDFILE" ] || return 0
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 0
    case "$pid" in ''|*[!0-9]*) return 0 ;; esac
    kill -0 "$pid" 2>/dev/null || { rm -f "$PIDFILE"; return 0; }
    # Che sia davvero llama-server e non un pid riciclato.
    case "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" in
        *llama-server*) ;;
        *) return 0 ;;
    esac
    log "fermo llama-server (pid $pid)"
    kill "$pid" 2>/dev/null
    wait_exit "$pid"
    rm -f "$PIDFILE"
}

# Ollama tiene il modello in VRAM in un processo figlio ("ollama runner").
# Lo fermiamo solo se sta davvero occupando la GPU.
stop_ollama() {
    systemctl is-active --quiet ollama.service || return 0
    nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader 2>/dev/null \
        | grep -qi ollama || return 0
    log "fermo ollama.service (tiene un modello in VRAM)"
    touch "$FLAG"
    systemctl stop ollama.service
}

case "$1" in
    pre)
        stop_llama_server
        stop_ollama
        ;;
    post)
        if [ -e "$FLAG" ]; then
            rm -f "$FLAG"
            log "riavvio ollama.service"
            systemctl start ollama.service
        fi
        ;;
esac

exit 0
