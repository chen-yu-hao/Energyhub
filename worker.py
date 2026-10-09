"""Private subprocess entrypoint. Numerical work uses the existing PySCF env."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import sys
import traceback


from .storage import write_json


def extrapolate(lower, upper, exponent=3.0, *, pair="34", family="cc"):
    """HF exponential and correlation inverse-power two-point extrapolation."""
    if pair not in {"34", "45"} or family not in {"cc", "aug"}:
        raise ValueError("Unsupported CBS pair or basis family")
    lo, hi = (int(value) for value in pair)
    decay = math.exp(-1.63 * (hi - lo))
    hf = (upper['hf_hartree'] - decay * lower['hf_hartree']) / (1 - decay)
    corr = (hi**exponent * upper['correlation_hartree'] - lo**exponent * lower['correlation_hartree']) / (hi**exponent - lo**exponent)
    names = basis_pair(pair, family)
    return {'energy_hartree': hf + corr, 'hf_hartree': hf, 'correlation_hartree': corr,
            'pyscf_version': upper['pyscf_version'], 'reference_type': upper['reference_type'],
            'converged': True, 'components': {names[0]: lower, names[1]: upper},
            'cbs': {'hf_alpha': 1.63, 'correlation_exponent': exponent, 'pair': pair, 'basis_family': family}}


def basis_pair(pair, family):
    if pair not in {"34", "45"} or family not in {"cc", "aug"}:
        raise ValueError("Unsupported CBS pair or basis family")
    prefix = 'aug-' if family == 'aug' else ''
    labels = {'3': 't', '4': 'q', '5': '5'}
    return tuple(prefix + 'cc-pv' + labels[value] + 'z' for value in pair)


def main():
    mode, request_path, result_path = sys.argv[1:]
    request = json.loads(Path(request_path).read_text())
    result_path = Path(result_path)
    try:
        if mode == 'molecule':
            from .core import _cc_energy, _read_xyz
            molecule = _read_xyz(Path(request['xyz']))
            opts = request['settings']
            def run(basis):
                def observer(stage):
                    write_json(result_path.parent / 'status.json', {'stage': stage, 'basis': basis})
                    print(f'{basis}: {stage}', flush=True)
                return _cc_energy(molecule, opts['method'], basis, opts['memory_mb'], opts['task_threads'], None, observer=observer)
            if opts['basis'] == 'CBS':
                pair, family = opts.get('cbs_pair', '34'), opts.get('basis_family', 'cc')
                lower, upper = basis_pair(pair, family)
                result = extrapolate(run(lower), run(upper), opts['cbs_exponent'], pair=pair, family=family)
            else:
                result = run(opts['basis'])
        elif mode == 'reference':
            from .core import compute_reference
            def progress(value):
                write_json(result_path.parent / 'progress.json', value)
            print('Preparing reference calculation', flush=True)
            report = compute_reference(request['tgz'], request['ref'], request['output'],
                                       progress=progress, log_callback=lambda line: print(line, flush=True), **request['options'])
            print('Reference file completed', flush=True)
            result = report.to_dict()
        else:
            raise ValueError('Unknown worker mode')
        write_json(result_path, {'ok': True, 'result': result})
    except BaseException as error:
        write_json(result_path, {'ok': False, 'error': f'{type(error).__name__}: {error}'})
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
