"""Pausa exclusiva do emissor Acme, compartilhada por API e processos de jobs.

Não pausa ingestão, persistência de inbound ou celulares de coexistência.
`pause()` fecha admissões antes de aguardar os envios em voo. Só retorna quando
eles terminaram. Não implica que um POST com timeout não chegou à Meta.
"""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import fcntl
import os

DIRECTORY = Path('/var/lib/copiloto-emission-gate')


class EmissionPaused(RuntimeError):
    pass


def _paused():
    try:
        return (DIRECTORY / 'paused').read_text().strip() != '0'
    except FileNotFoundError:
        return False


@contextmanager
def admission():
    # Erro de permissões/IO falha fechado. O deploy provisiona a pasta.
    if _paused():
        raise EmissionPaused('emissor Acme pausado; envio não iniciado')
    fd = os.open(DIRECTORY / 'inflight.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        if _paused():
            raise EmissionPaused('emissor Acme pausado; envio não iniciado')
        yield
    finally:
        os.close(fd)


def guarded(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with admission():
            return fn(*args, **kwargs)
    return wrapped


def set_paused(paused: bool):
    """Uso operacional explícito; pause fecha entrada e drena, resume abre."""
    # Serializa operadores, sem bloquear a conclusão de admissões existentes.
    with (DIRECTORY / 'control.lock').open('a+') as control:
        fcntl.flock(control, fcntl.LOCK_EX)
        flag = DIRECTORY / 'paused'
        def write_flag(value):
            tmp = DIRECTORY / 'paused.tmp'
            with tmp.open('w') as out:
                out.write(value); out.flush(); os.fsync(out.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, flag)
        if paused:
            write_flag('1')
        with (DIRECTORY / 'inflight.lock').open('a+') as active:
            fcntl.flock(active, fcntl.LOCK_EX)
            if not paused:
                write_flag('0')
        return {'paused': paused, 'inflight_drained': True}


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['status', 'pause', 'resume'])
    args = parser.parse_args()
    print(json.dumps({'paused': _paused()} if args.action == 'status'
                     else set_paused(args.action == 'pause')))
