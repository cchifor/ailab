# Bounded observer transport refusals

## Concurrent gate refusals

The gate reads declined busy or terminal connections in at most eight separate
workers. Each reader keeps the existing 64 KiB frame limit and five-second I/O
budget, then rechecks terminal state before replying. Refusal workers never own
the admitted request's token or forward requests to the host. A slow declined
writer therefore cannot prevent a completed request's successor from being
accepted. When all eight refusal slots are occupied, additional declined sockets
are closed immediately; an overload connection may observe EOF/reset instead of
a JSON refusal. The admitted slot remains independent of that cap.

When the accept loop exits, it closes all owned refusal sockets and joins their
workers within one five-second budget. An unexpected worker failure retains the
gate's existing immediate process-exit behavior, which closes process-owned
sockets. These transport limits do not change observer authority or source pins.

This change preserves the operator admission, terminal priority and request deadline contracts.
Qualify the actual Unix transport with `python3 -m unittest -v scripts.tests.test_relay_phase_a_observer.Gate`.
It supplies no collector, authority, deployment qualification or live execution permission.
