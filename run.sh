#!/usr/bin/env bash
#
# Run the cascade — driver alone, or with a live observer listening.
#
# The two scripts share a cascade id and nothing else.  Ordering is the
# whole reason this wrapper exists: a one-stage run finishes in about
# seven seconds, which is less time than a second Python process needs
# to boot, connect and register, so an observer started after the driver
# attaches to a cascade that has already ended and hears nothing.  With
# --observe the observer is started FIRST and the driver waits until it
# reports itself attached.
#
#   ./run.sh                                  driver only
#   ./run.sh -o                               driver + live audio
#   ./run.sh -p "Why is the sea salty?"       ask something else
#   ./run.sh -o -q -p "Count to three."       observe, trace without sound
#
# PYTHON overrides the interpreter (default: python3, so an activated
# virtualenv is used as-is).  The daemon must speak protocol >= 1.4; the
# scripts declare that and refuse an older one by name.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

observe=0
no_audio=0
prompt=""
observer_log="$HERE/.jaato/observer.log"

usage() {
    # Print the header comment block, whatever length it grows to: from
    # line 2 until the first line that is not a comment.  A hardcoded
    # line range goes stale the first time the header is edited, and
    # spills shell code into the help text.
    awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^#ic? ?/, ""); sub(/^# ?/, ""); print }' \
        "${BASH_SOURCE[0]}"
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--observe)  observe=1; shift ;;
        -q|--no-audio) no_audio=1; shift ;;
        -p|--prompt)
            [[ $# -ge 2 ]] || { echo "--prompt needs a question" >&2; exit 2; }
            prompt="$2"; shift 2 ;;
        -p=*|--prompt=*) prompt="${1#*=}"; shift ;;
        -h|--help)     usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 2 ;;
    esac
done

# -q only means anything alongside -o; say so rather than ignoring it.
if (( no_audio && ! observe )); then
    echo "--no-audio applies to the observer; add --observe" >&2
    exit 2
fi

# Both processes must agree on this before either starts.
cascade_id="$($PYTHON -c 'import uuid; print(uuid.uuid4().hex)')" || exit 1

driver_args=("$cascade_id")
[[ -n "$prompt" ]] && driver_args+=(--prompt "$prompt")

observer_pid=""
cleanup() {
    if [[ -n "$observer_pid" ]] && kill -0 "$observer_pid" 2>/dev/null; then
        # SIGINT, not SIGTERM: the observer treats an interrupt as a
        # normal end, draining whatever audio is still buffered and
        # printing its summary.  SIGTERM would cut playback short.
        kill -INT "$observer_pid" 2>/dev/null
        # Bounded: a plain `wait` here hangs forever if the observer is
        # stuck draining audio, and a wrapper that never returns is
        # worse than one that reports a straggler.
        for _ in $(seq 1 40); do
            kill -0 "$observer_pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$observer_pid" 2>/dev/null; then
            echo "observer did not stop on SIGINT; terminating" >&2
            kill -TERM "$observer_pid" 2>/dev/null
        fi
        wait "$observer_pid" 2>/dev/null
    fi
}
trap cleanup EXIT INT TERM

if (( observe )); then
    mkdir -p "$(dirname "$observer_log")"
    : > "$observer_log"
    obs_args=("$cascade_id")
    (( no_audio )) && obs_args+=(--no-audio)
    "$PYTHON" "$HERE/run_observer.py" "${obs_args[@]}" >"$observer_log" 2>&1 &
    observer_pid=$!

    # Wait for the observer to SAY it is attached rather than sleeping a
    # guessed interval.  It flushes that line for exactly this purpose.
    # A cold daemon autostart is slow, hence the generous ceiling; the
    # loop exits the moment the line appears.
    printf 'waiting for the observer to attach'
    for _ in $(seq 1 180); do
        grep -q "observing cascade" "$observer_log" && break
        kill -0 "$observer_pid" 2>/dev/null || break
        printf '.'; sleep 1
    done
    echo
    if ! grep -q "observing cascade" "$observer_log"; then
        echo "observer never attached — its log:" >&2
        cat "$observer_log" >&2
        exit 1
    fi
    echo "observer attached; firing (listen now)"
fi

"$PYTHON" "$HERE/run_cascade.py" "${driver_args[@]}"
status=$?

if (( observe )); then
    # Chunks are fed to paplay as they arrive, so the tail of the answer
    # is still playing when the driver returns.  Let it finish before
    # cleanup() interrupts the observer.
    sleep 6
    cleanup
    observer_pid=""
    echo "--- observer ---"
    cat "$observer_log"
fi

exit "$status"
